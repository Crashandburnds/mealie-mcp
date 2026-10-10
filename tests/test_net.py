import httpx
import pytest

from mealie_mcp.net import (
    OutboundBlockedError,
    UrlValidationError,
    allowlisted_client,
    canonicalize_url,
    hellofresh_id,
    validate_hellofresh_url,
    validate_recipe_url,
)

from .conftest import HF, THIGH_ID, THIGH_URL

ALLOWED = ["hellofresh.co.uk"]


async def test_accepts_hellofresh_recipe_url():
    assert await validate_recipe_url(THIGH_URL + "#top", ALLOWED) == THIGH_URL


@pytest.mark.parametrize(
    "url, reason",
    [
        ("http://www.hellofresh.co.uk/recipes/x", "https"),
        ("https://192.168.1.5/recipes/x", "IP-address"),
        ("https://[::1]/x", "IP-address"),
        ("https://localhost/x", "hostname"),
        ("https://truenas.local/x", "local network"),
        ("https://user:pw@www.hellofresh.co.uk/x", "credentials"),
        ("https://www.hellofresh.co.uk:8443/x", "ports"),
        ("https://www.evil.com/recipes/x", "allowlist"),
        ("https://hellofresh.co.uk.evil.com/x", "allowlist"),
        ("ftp://www.hellofresh.co.uk/x", "https"),
        ("", "empty"),
    ],
)
async def test_rejects_bad_urls(url, reason):
    with pytest.raises(UrlValidationError, match=reason):
        await validate_recipe_url(url, ALLOWED)


async def test_rejects_domain_resolving_to_lan():
    with pytest.raises(UrlValidationError, match="private"):
        await validate_recipe_url("https://www.evil-rebind.com/r", ["*"])


async def test_wildcard_allows_public_domains():
    assert await validate_recipe_url("https://www.bbcgoodfood.com/recipes/x", ["*"])


def test_canonicalize_and_id():
    assert canonicalize_url("HTTP://WWW.HelloFresh.co.uk/recipes/a-b/?utm=1#x") == "https://hellofresh.co.uk/recipes/a-b"
    assert hellofresh_id(THIGH_URL) == THIGH_ID
    assert hellofresh_id("https://example.com/recipes/foo") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.hellofresh.com/recipes/curried-chicken-thigh-traybake-64ad73db48c7b9302c333117",
        "https://www.hellofresh.co.uk/recipes/traybake-recipes",
        "https://www.hellofresh.co.uk/menus/curried-chicken-64ad73db48c7b9302c333117",
        "http://www.hellofresh.co.uk/recipes/curried-chicken-thigh-traybake-64ad73db48c7b9302c333117",
        "https://www.hellofresh.co.uk/recipes/x-64ad73db48c7b9302c33311",
    ],
)
def test_invalid_hellofresh_urls(url):
    with pytest.raises(UrlValidationError):
        validate_hellofresh_url(url, HF)


def test_valid_hellofresh_url_normalised():
    assert (
        validate_hellofresh_url("https://hellofresh.co.uk/recipes/curried-chicken-thigh-traybake-64ad73db48c7b9302c333117/", HF)
        == THIGH_URL
    )


async def test_allowlisted_client_blocks_other_hosts_and_redirects(router):
    router.get("https://allowed.example/start").mock(
        return_value=httpx.Response(302, headers={"Location": "http://192.168.1.1/admin"})
    )
    async with allowlisted_client(["allowed.example"], timeout=5, follow_redirects=True) as client:
        with pytest.raises(OutboundBlockedError):
            await client.get("https://other.example/")
        with pytest.raises(OutboundBlockedError):
            await client.get("https://allowed.example/start")
