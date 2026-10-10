"""HTTP-level tests: startup, health, and the OAuth 2.1 flow ChatGPT performs."""

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from mealie_mcp.hellofresh import HelloFreshClient
from mealie_mcp.mealie import MealieClient
from mealie_mcp.server import create_app

from .conftest import HF, MEALIE, OWNER_PASSWORD, STATIC_TOKEN, TOKEN

BASE = "https://mcp.example.com"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
}


@pytest.fixture
def client(settings, fake_mealie):
    app = create_app(settings, mealie=MealieClient(MEALIE, TOKEN, max_retries=1), hellofresh=HelloFreshClient(HF, min_interval=0))
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        yield c


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(client, redirect=REDIRECT):
    return client.post(
        "/register",
        json={
            "client_name": "ChatGPT",
            "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


def start_authorize(client, client_id, challenge, state="xyz"):
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "mealie",
            "resource": f"{BASE}/mcp",
        },
    )
    assert r.status_code in (302, 307), r.text
    login_url = r.headers["location"]
    assert login_url.startswith(f"{BASE}/oauth/login?txn=")
    page = client.get(login_url)
    assert page.status_code == 200
    assert "form-action 'self' https://chatgpt.com;" in page.headers["content-security-policy"]
    txn = parse_qs(urlsplit(login_url).query)["txn"][0]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return txn, csrf


def full_login(client):
    reg = register(client).json()
    verifier, challenge = pkce()
    txn, csrf = start_authorize(client, reg["client_id"], challenge)
    r = client.post("/oauth/login", data={"txn": txn, "csrf": csrf, "password": OWNER_PASSWORD, "action": "approve"})
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith(REDIRECT)
    q = parse_qs(urlsplit(location).query)
    assert q["state"] == ["xyz"]
    assert q["iss"] == [client.get("/.well-known/oauth-authorization-server").json()["issuer"]]
    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": reg["client_id"],
            "code_verifier": verifier,
            "resource": f"{BASE}/mcp",
        },
    )
    assert token.status_code == 200, token.text
    return reg, token.json(), q["code"][0], verifier


class McpResponse:
    """Wraps a Streamable HTTP response, which may be JSON or an SSE stream."""

    def __init__(self, response):
        self.response = response
        self.status_code = response.status_code
        self.headers = response.headers
        self.text = response.text

    def json(self):
        if self.response.headers.get("content-type", "").startswith("text/event-stream"):
            data = [line[5:].strip() for line in self.text.splitlines() if line.startswith("data:")]
            return json.loads(data[-1])
        return self.response.json()


def mcp_call(client, token, body=INIT):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return McpResponse(client.post("/mcp", json=body, headers=headers))


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert client.get("/health/ready").json() == {"status": "ok", "mealie": "ok"}


def test_unauthenticated_mcp_gets_401_with_discovery(client):
    r = mcp_call(client, None)
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]
    assert mcp_call(client, "garbage").status_code == 401


def test_discovery_metadata(client):
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{BASE}/mcp"
    assert prm["authorization_servers"][0].rstrip("/") == BASE
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["registration_endpoint"] == f"{BASE}/register"
    assert "S256" in asm["code_challenge_methods_supported"]
    assert asm["authorization_response_iss_parameter_supported"] is True
    assert asm["authorization_endpoint"] == f"{BASE}/authorize"


def test_full_oauth_flow_and_tool_listing(client):
    reg, tokens, code, verifier = full_login(client)
    assert tokens["token_type"].lower() == "bearer" and tokens["refresh_token"]
    r = mcp_call(client, tokens["access_token"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.json()["result"]["serverInfo"]["name"] == "Mealie"
    tools = mcp_call(client, tokens["access_token"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in tools.json()["result"]["tools"]]
    assert "hellofresh_import" in names
    annotated = {t["name"]: t.get("annotations", {}) for t in tools.json()["result"]["tools"]}
    assert annotated["mealie_list_recipes"]["readOnlyHint"] is True
    call = mcp_call(
        client,
        tokens["access_token"],
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "mealie_status", "arguments": {}}},
    )
    assert call.json()["result"]["structuredContent"]["authenticated"] is True
    # Authorization codes are single use.
    replay = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": reg["client_id"],
            "code_verifier": verifier,
        },
    )
    assert replay.status_code in (400, 401) and replay.json()["error"] == "invalid_grant"


def test_refresh_rotation(client):
    reg, tokens, *_ = full_login(client)
    data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": reg["client_id"]}
    first = client.post("/token", data=data)
    assert first.status_code == 200
    assert mcp_call(client, first.json()["access_token"]).status_code == 200
    reuse = client.post("/token", data=data)  # single-use refresh tokens
    assert reuse.status_code in (400, 401) and reuse.json()["error"] == "invalid_grant"


def test_wrong_pkce_verifier_rejected(client):
    reg = register(client).json()
    _, challenge = pkce()
    txn, csrf = start_authorize(client, reg["client_id"], challenge)
    r = client.post("/oauth/login", data={"txn": txn, "csrf": csrf, "password": OWNER_PASSWORD, "action": "approve"})
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    bad = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": reg["client_id"],
            "code_verifier": "x" * 50,
        },
    )
    assert bad.status_code in (400, 401) and bad.json()["error"] == "invalid_grant"


def test_wrong_password_and_lockout(client):
    reg = register(client).json()
    _, challenge = pkce()
    txn, csrf = start_authorize(client, reg["client_id"], challenge)
    for _ in range(5):
        r = client.post("/oauth/login", data={"txn": txn, "csrf": csrf, "password": "nope", "action": "approve"})
        assert r.status_code == 401
    r = client.post("/oauth/login", data={"txn": txn, "csrf": csrf, "password": OWNER_PASSWORD, "action": "approve"})
    assert r.status_code == 429


def test_csrf_and_expired_txn(client):
    reg = register(client).json()
    _, challenge = pkce()
    txn, _ = start_authorize(client, reg["client_id"], challenge)
    r = client.post("/oauth/login", data={"txn": txn, "csrf": "forged", "password": OWNER_PASSWORD})
    assert r.status_code == 400
    assert client.get("/oauth/login?txn=unknown").status_code == 400


def test_deny_redirects_with_error(client):
    reg = register(client).json()
    _, challenge = pkce()
    txn, csrf = start_authorize(client, reg["client_id"], challenge)
    r = client.post("/oauth/login", data={"txn": txn, "csrf": csrf, "action": "deny"})
    assert "error=access_denied" in r.headers["location"]


def test_registration_rejects_foreign_redirect(client):
    assert register(client, "https://attacker.example/cb").status_code == 400
    assert register(client, "http://chatgpt.com/cb").status_code == 400


def test_static_token(client):
    assert mcp_call(client, STATIC_TOKEN).status_code == 200


def test_forged_token_rejected(client):
    import jwt

    forged = jwt.encode(
        {
            "typ": "access",
            "aud": f"{BASE}/mcp",
            "iss": BASE,
            "exp": 9999999999,
            "iat": 1,
            "jti": "a",
            "client_id": "c",
            "scope": "mealie",
        },
        "wrong-secret-wrong-secret-wrong-secret",
        algorithm="HS256",
    )
    assert mcp_call(client, forged).status_code == 401


def test_clients_persist_across_restart(settings, fake_mealie):
    app = create_app(settings, mealie=MealieClient(MEALIE, TOKEN), hellofresh=HelloFreshClient(HF))
    with TestClient(app, base_url=BASE) as c:
        client_id = register(c).json()["client_id"]
    app2 = create_app(settings, mealie=MealieClient(MEALIE, TOKEN), hellofresh=HelloFreshClient(HF))
    with TestClient(app2, base_url=BASE, follow_redirects=False) as c:
        _, challenge = pkce()
        start_authorize(c, client_id, challenge)
    assert (settings.data_dir / "clients.json").stat().st_mode & 0o077 == 0
