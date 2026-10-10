"""URL validation, SSRF protection and an outbound-allowlisted HTTP client."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx

HELLOFRESH_ID_RE = re.compile(r"([0-9a-f]{24})/?$")
HELLOFRESH_RECIPE_PATH_RE = re.compile(r"^/recipes/[a-z0-9-]+-[0-9a-f]{24}/?$")
MAX_URL_LENGTH = 2048

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36 mealie-mcp/1.0"


class UrlValidationError(ValueError):
    """Raised when a URL is not acceptable for import."""


class OutboundBlockedError(RuntimeError):
    """Raised when code tries to contact a host that is not on the allowlist."""


def host_matches(host: str, domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for domain in domains:
        if domain == "*":
            return True
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def canonicalize_url(url: str) -> str:
    """Normalise a recipe URL for comparison: https, lowercase host, no www, query, fragment or trailing slash."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def hellofresh_id(url: str) -> str | None:
    match = HELLOFRESH_ID_RE.search(urlsplit(url).path)
    return match.group(1) if match else None


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def validate_recipe_url(url: str, allowed_domains: list[str], *, resolve_dns: bool = True) -> str:
    """Validate a recipe URL before handing it to Mealie's scraper.

    Mealie fetches the URL server-side, so this guards against using it to reach
    LAN devices: https only, no credentials, no IP literals, no odd ports, host must
    be on the allowlist and must resolve only to public addresses.
    Returns the cleaned URL (fragment removed).
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlValidationError("URL is empty")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UrlValidationError("URL is too long")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UrlValidationError(f"URL is malformed: {exc}") from exc
    if parts.scheme != "https":
        raise UrlValidationError("only https:// URLs are accepted")
    if parts.username or parts.password:
        raise UrlValidationError("URLs with embedded credentials are not accepted")
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UrlValidationError("IP-address URLs are not accepted; use a public domain name")
    if not host or "." not in host:
        raise UrlValidationError("URL has no valid public hostname")
    if host == "localhost" or host.endswith((".local", ".lan", ".internal", ".home", ".localdomain", ".arpa")):
        raise UrlValidationError("local network hostnames are not accepted")
    if port not in (None, 443):
        raise UrlValidationError("non-standard ports are not accepted")
    if not host_matches(host, allowed_domains):
        raise UrlValidationError(f"domain '{host}' is not in the import allowlist ({', '.join(allowed_domains)})")
    if resolve_dns:
        try:
            addresses = await _resolve(host)
        except OSError as exc:
            raise UrlValidationError(f"could not resolve '{host}'") from exc
        if not addresses:
            raise UrlValidationError(f"could not resolve '{host}'")
        for address in addresses:
            if not _is_public_ip(ipaddress.ip_address(address.split("%")[0])):
                raise UrlValidationError(f"'{host}' resolves to a private or reserved address")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def validate_hellofresh_url(url: str, base_url: str) -> str:
    """Check a URL is a HelloFresh recipe page on the configured site. Returns the canonical URL."""
    parts = urlsplit(url.strip())
    expected = urlsplit(base_url).hostname or ""
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host.removeprefix("www.") != expected.lower().removeprefix("www."):
        raise UrlValidationError(f"not a {expected} URL")
    if not HELLOFRESH_RECIPE_PATH_RE.match(parts.path):
        raise UrlValidationError("not a HelloFresh recipe URL (expected /recipes/<name>-<24-character id>)")
    return f"{base_url.rstrip('/')}{parts.path.rstrip('/')}"


def allowlisted_client(allowed_hosts: list[str], *, timeout: float, verify: bool = True, **kwargs) -> httpx.AsyncClient:
    """An httpx client that refuses to send any request (including redirects) to a host off the allowlist."""
    allowed = [h.lower() for h in allowed_hosts]

    async def guard(request: httpx.Request) -> None:
        host = (request.url.host or "").lower()
        if host not in allowed:
            raise OutboundBlockedError(f"outbound request to '{host}' blocked by allowlist")

    hooks = kwargs.pop("event_hooks", {})
    hooks.setdefault("request", []).insert(0, guard)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        verify=verify,
        event_hooks=hooks,
        follow_redirects=kwargs.pop("follow_redirects", False),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        **kwargs,
    )
