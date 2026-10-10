"""Self-contained OAuth 2.1 authorization server for a single owner.

ChatGPT's custom connectors authenticate to remote MCP servers with OAuth 2.1
(MCP authorization spec): protected-resource metadata (RFC 9728),
authorization-server metadata (RFC 8414), dynamic client registration
(RFC 7591), authorization code + PKCE (S256) and bearer access tokens.

FastMCP provides the endpoints; this provider supplies the policy:

* Client registration is open (ChatGPT registers itself), but redirect URIs
  must be https on an allowlisted host (chatgpt.com by default).
* Every authorization requires the owner password on a consent page served by
  this server. Failed attempts are rate limited per IP and globally.
* Access/refresh tokens are HS256 JWTs bound to this server's resource URL,
  so they survive container restarts. Rotating AUTH_SECRET revokes them all.
* Registered clients are persisted to DATA_DIR/clients.json (if writable) so
  ChatGPT does not need to re-register after a restart.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import secrets
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from fastmcp.server.auth.auth import (
    AccessToken,
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .logging_setup import log

logger = logging.getLogger("mealie_mcp.auth")

SCOPE = "mealie"
AUTH_CODE_TTL = 300
PENDING_TTL = 600
LOGIN_PATH = "/oauth/login"
# form-action must also allow the client's origin: browsers apply it to the redirect after POST.
_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' {origin}; frame-ancestors 'none'"
MAX_FAILURES_PER_IP = 5
FAILURE_WINDOW = 900
MAX_FAILURES_GLOBAL = 25


@dataclass
class _Pending:
    client_id: str
    client_name: str | None
    params: AuthorizationParams
    created: float
    csrf: str


class OwnerOAuthProvider(OAuthProvider):
    def __init__(
        self,
        *,
        base_url: str,
        resource_url: str,
        owner_password: str,
        secret: str,
        allowed_redirect_hosts: list[str],
        data_dir: Path | None = None,
        access_ttl: int = 3600,
        refresh_ttl: int = 30 * 86400,
        static_tokens: list[str] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
        )
        self._public_base = base_url.rstrip("/")
        self._resource = resource_url
        self._password = owner_password.encode()
        self._secret = secret
        self._allowed_hosts = [h.lower() for h in allowed_redirect_hosts]
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        self._static_tokens = [t.encode() for t in static_tokens or []]
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._pending: dict[str, _Pending] = {}
        self._used_refresh: dict[str, float] = {}
        self._revoked: dict[str, float] = {}
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._global_failures: deque[float] = deque()
        self._clients_file = (data_dir / "clients.json") if data_dir else None
        self._load_clients()

    # ------------------------------------------------------------ persistence
    def _load_clients(self) -> None:
        if not self._clients_file or not self._clients_file.exists():
            return
        try:
            raw = json.loads(self._clients_file.read_text())
            for item in raw:
                client = OAuthClientInformationFull.model_validate(item)
                if client.client_id:
                    self._clients[client.client_id] = client
            log(logger, logging.INFO, "loaded oauth clients", count=len(self._clients))
        except (OSError, ValueError) as exc:
            log(logger, logging.WARNING, "could not load oauth clients", error=type(exc).__name__)

    def _save_clients(self) -> None:
        if not self._clients_file:
            return
        try:
            self._clients_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._clients_file.with_suffix(".tmp")
            tmp.write_text(json.dumps([c.model_dump(mode="json") for c in self._clients.values()]))
            tmp.chmod(0o600)
            tmp.replace(self._clients_file)
        except OSError as exc:
            log(logger, logging.WARNING, "could not persist oauth clients (in-memory only)", error=str(exc))

    # ------------------------------------------------------------ clients
    def _redirect_ok(self, uri: str) -> bool:
        parts = urlsplit(uri)
        host = (parts.hostname or "").lower()
        if parts.scheme == "https" and host in self._allowed_hosts:
            return True
        # Loopback redirects for local tools (MCP Inspector) only if explicitly allowlisted.
        return parts.scheme == "http" and host in ("localhost", "127.0.0.1") and host in self._allowed_hosts

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or not all(self._redirect_ok(u) for u in uris):
            log(logger, logging.WARNING, "rejected client registration", redirect_hosts=[urlsplit(u).hostname for u in uris])
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="redirect_uri host is not allowed by this server",
            )
        if len(self._clients) >= 50:
            # Bound memory/disk: drop the oldest registration.
            oldest = min(self._clients.values(), key=lambda c: c.client_id_issued_at or 0)
            self._clients.pop(oldest.client_id or "", None)
        assert client_info.client_id
        self._clients[client_info.client_id] = client_info
        self._save_clients()
        log(
            logger,
            logging.INFO,
            "oauth client registered",
            client_name=client_info.client_name,
            redirect_hosts=sorted({urlsplit(u).hostname for u in uris}),
        )

    # ------------------------------------------------------------ authorize
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self._redirect_ok(str(params.redirect_uri)):
            raise AuthorizeError(error="invalid_request", error_description="redirect_uri not allowed")
        if params.resource and params.resource.rstrip("/") != self._resource.rstrip("/"):
            raise AuthorizeError(error="invalid_request", error_description="unknown resource")
        self._gc()
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = _Pending(
            client_id=client.client_id or "",
            client_name=client.client_name,
            params=params,
            created=time.time(),
            csrf=secrets.token_urlsafe(24),
        )
        return f"{self._public_base}{LOGIN_PATH}?txn={txn}"

    def _gc(self) -> None:
        now = time.time()
        for store, ttl in ((self._pending, PENDING_TTL),):
            for key in [k for k, v in store.items() if now - v.created > ttl]:
                store.pop(key, None)
        for key in [k for k, v in self._codes.items() if v.expires_at < now]:
            self._codes.pop(key, None)
        for d in (self._used_refresh, self._revoked):
            for key in [k for k, exp in d.items() if exp < now]:
                d.pop(key, None)

    def _locked_out(self, ip: str) -> bool:
        now = time.time()
        q = self._failures[ip]
        while q and now - q[0] > FAILURE_WINDOW:
            q.popleft()
        while self._global_failures and now - self._global_failures[0] > FAILURE_WINDOW:
            self._global_failures.popleft()
        return len(q) >= MAX_FAILURES_PER_IP or len(self._global_failures) >= MAX_FAILURES_GLOBAL

    def _login_page(self, pending: _Pending, txn: str, error: str | None = None, status: int = 200) -> HTMLResponse:
        client = html.escape(pending.client_name or "An MCP client")
        redirect = urlsplit(str(pending.params.redirect_uri))
        redirect_host = html.escape(redirect.hostname or "")
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Authorise Mealie MCP</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f4f5;color:#18181b;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{background:#fff;padding:2rem;border-radius:12px;max-width:380px;width:calc(100% - 32px);box-shadow:0 2px 12px #0001}}
h1{{font-size:1.25rem;margin-top:0}}input{{width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box;margin:.5rem 0 1rem}}
button{{padding:.6rem 1rem;font-size:1rem;border:0;border-radius:8px;margin-right:.5rem;cursor:pointer}}
.ok{{background:#16a34a;color:#fff}}.err{{color:#b91c1c}}small{{color:#52525b}}</style></head><body><main>
<h1>Authorise access to your Mealie</h1>
<p><b>{client}</b> wants to read your recipes and import new ones.</p>
<p><small>You will be returned to <b>{redirect_host}</b>.</small></p>{err}
<form method="post" action="{LOGIN_PATH}">
<input type="hidden" name="txn" value="{html.escape(txn)}"><input type="hidden" name="csrf" value="{html.escape(pending.csrf)}">
<label for="pw">Owner password</label>
<input id="pw" type="password" name="password" autocomplete="current-password" required autofocus>
<button class="ok" type="submit" name="action" value="approve">Approve</button>
<button type="submit" name="action" value="deny">Deny</button></form></main></body></html>"""
        return HTMLResponse(
            body,
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": _CSP.format(origin=f"{redirect.scheme}://{redirect.netloc}"),
                "Referrer-Policy": "no-referrer",
            },
        )

    async def login_endpoint(self, request: Request) -> Response:
        self._gc()
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            pending = self._pending.get(txn)
            if not pending:
                return HTMLResponse("<h1>This sign-in link has expired. Start again from ChatGPT.</h1>", status_code=400)
            return self._login_page(pending, txn)

        form = await request.form()
        txn = str(form.get("txn", ""))
        pending = self._pending.get(txn)
        if not pending or not hmac.compare_digest(str(form.get("csrf", "")), pending.csrf):
            return HTMLResponse("<h1>This sign-in link has expired. Start again from ChatGPT.</h1>", status_code=400)
        params = pending.params
        if form.get("action") == "deny":
            self._pending.pop(txn, None)
            return RedirectResponse(
                construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state, iss=self._issuer), 302
            )
        ip = request.client.host if request.client else "unknown"
        if self._locked_out(ip):
            log(logger, logging.WARNING, "login locked out", ip=ip)
            return self._login_page(pending, txn, "Too many failed attempts. Try again in 15 minutes.", 429)
        password = str(form.get("password", "")).encode()
        if not hmac.compare_digest(password, self._password):
            now = time.time()
            self._failures[ip].append(now)
            self._global_failures.append(now)
            log(logger, logging.WARNING, "login failed", ip=ip)
            return self._login_page(pending, txn, "Incorrect password.", 401)

        self._pending.pop(txn, None)
        self._failures.pop(ip, None)
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            client_id=pending.client_id,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            scopes=[SCOPE],
            expires_at=time.time() + AUTH_CODE_TTL,
            code_challenge=params.code_challenge,
            resource=self._resource,
            subject="owner",
        )
        log(logger, logging.INFO, "authorization granted", client_name=pending.client_name, ip=ip)
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state, iss=self._issuer), 302
        )

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        for i, route in enumerate(routes):
            if isinstance(route, Route) and route.path == "/.well-known/oauth-authorization-server":
                # Advertise RFC 9207 so ChatGPT uses its stable redirect URI.
                metadata = build_metadata(
                    self.base_url,  # type: ignore[arg-type]
                    self.service_documentation_url,
                    self.client_registration_options or ClientRegistrationOptions(),
                    self.revocation_options or RevocationOptions(),
                )
                metadata.issuer = self.issuer_url  # type: ignore[assignment]
                metadata.authorization_response_iss_parameter_supported = True
                handler = MetadataHandler(metadata)
                routes[i] = Route(route.path, cors_middleware(handler.handle, ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"])
        routes.append(Route(LOGIN_PATH, self.login_endpoint, methods=["GET", "POST"]))
        return routes

    @property
    def _issuer(self) -> str:
        # RFC 9207: must equal the `issuer` in the authorization server metadata exactly.
        return str(self.issuer_url)

    # ------------------------------------------------------------ tokens
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if not code or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        common = {
            "iss": self._public_base,
            "aud": self._resource,
            "sub": "owner",
            "client_id": client_id,
            "scope": " ".join(scopes),
            "iat": now,
        }
        access = jwt.encode(
            {**common, "typ": "access", "exp": now + self._access_ttl, "jti": uuid.uuid4().hex}, self._secret, algorithm="HS256"
        )
        refresh = jwt.encode(
            {**common, "typ": "refresh", "exp": now + self._refresh_ttl, "jti": uuid.uuid4().hex}, self._secret, algorithm="HS256"
        )
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=self._access_ttl, refresh_token=refresh, scope=" ".join(scopes)
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used or expired")
        return self._issue(client.client_id or "", authorization_code.scopes)

    def _decode(self, token: str, typ: str) -> dict | None:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=self._resource,
                issuer=self._public_base,
                options={"require": ["exp", "iat", "jti", "aud", "iss"]},
            )
        except jwt.PyJWTError:
            return None
        if claims.get("typ") != typ or claims.get("jti") in self._revoked:
            return None
        return claims

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        claims = self._decode(refresh_token, "refresh")
        if not claims or claims.get("client_id") != client.client_id or claims["jti"] in self._used_refresh:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=claims["client_id"],
            scopes=claims["scope"].split(),
            expires_at=claims["exp"],
            resource=self._resource,
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        claims = self._decode(refresh_token.token, "refresh")
        if not claims or claims["jti"] in self._used_refresh:
            raise TokenError("invalid_grant", "refresh token is invalid or already used")
        self._used_refresh[claims["jti"]] = claims["exp"]  # rotation: single use
        granted = [s for s in (scopes or refresh_token.scopes) if s in refresh_token.scopes] or refresh_token.scopes
        return self._issue(client.client_id or "", granted)

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        for static in self._static_tokens:
            if hmac.compare_digest(token.encode(), static):
                return AccessToken(
                    token=token,
                    client_id="static-token",
                    scopes=[SCOPE],
                    expires_at=None,
                    resource=self._resource,
                    subject="owner",
                )
        claims = self._decode(token, "access")
        if not claims:
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims["scope"].split(),
            expires_at=claims["exp"],
            resource=self._resource,
            subject="owner",
        )

    async def verify_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for typ in ("access", "refresh"):
            claims = self._decode(token.token, typ)
            if claims:
                self._revoked[claims["jti"]] = claims["exp"]
