"""Exercise the MCP tools end-to-end through an in-memory MCP client."""

import logging

import httpx
import pytest
from fastmcp import Client

from mealie_mcp.hellofresh import HelloFreshClient
from mealie_mcp.mealie import MealieClient
from mealie_mcp.server import build_server

from .conftest import BREAST_URL, HF, MEALIE, THIGH_URL, TOKEN
from .hf_fixtures import BREAST_PAGE, HOME_PAGE, SEARCH_API, SEARCH_HTML, THIGH_PAGE

SUBTITLE = "With Cumin Roast Potatoes, Carrots and Green Beans"


@pytest.fixture
def hf_site(router):
    router.get(f"{HF}/recipes").mock(return_value=httpx.Response(200, text=HOME_PAGE))
    router.get(f"{HF}/gw/recipes/recipes/search").mock(return_value=httpx.Response(200, json=SEARCH_API))
    router.get(f"{HF}/recipes/search").mock(return_value=httpx.Response(200, text=SEARCH_HTML))
    router.get(THIGH_URL).mock(return_value=httpx.Response(200, text=THIGH_PAGE))
    router.get(BREAST_URL).mock(return_value=httpx.Response(200, text=BREAST_PAGE))


@pytest.fixture
def mcp(settings, fake_mealie, hf_site):
    return build_server(
        settings, mealie=MealieClient(MEALIE, TOKEN, max_retries=1), hellofresh=HelloFreshClient(HF, min_interval=0)
    )


async def call(mcp, tool, **args):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    return result.structured_content


async def test_all_tools_registered_with_annotations(mcp):
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    expected = {
        "mealie_status",
        "mealie_list_recipes",
        "mealie_search_recipes",
        "mealie_get_recipe",
        "mealie_import_url",
        "mealie_bulk_import",
        "mealie_check_duplicate",
        "hellofresh_search",
        "hellofresh_match",
        "hellofresh_import",
        "hellofresh_bulk_import",
    }
    assert expected <= set(tools)
    assert tools["mealie_list_recipes"].annotations.read_only_hint is True
    assert tools["hellofresh_import"].annotations.destructive_hint is False
    assert not any("delete" in n for n in tools)


async def test_status_tool(mcp):
    out = await call(mcp, "mealie_status")
    assert out["ok"] and out["authenticated"] and out["mealie_version"] == "v3.3.0"
    assert TOKEN not in str(out)


async def test_status_reports_auth_failure(mcp, fake_mealie):
    fake_mealie.token_ok = False
    out = await call(mcp, "mealie_status")
    assert out == {
        "ok": False,
        "error": "auth_error",
        "message": "Mealie rejected the API token (check MEALIE_API_TOKEN)",
        "http_status": 401,
    }


async def test_first_recipe_end_to_end(mcp, fake_mealie):
    """The user's first recipe: imports the Thigh version, never the Breast version."""
    dry = await call(mcp, "hellofresh_import", name="Curried Chicken Thigh Traybake", subtitle=SUBTITLE, dry_run=True)
    assert dry["ok"] and dry["import_status"] == "dry_run"
    assert dry["match"]["url"] == THIGH_URL
    assert fake_mealie.created == []

    real = await call(mcp, "hellofresh_import", name="Curried Chicken Thigh Traybake", subtitle=SUBTITLE)
    assert real["ok"] and real["import_status"] == "imported"
    assert real["import"]["recipe"]["name"] == "Curried Chicken Thigh Traybake"
    assert len(fake_mealie.created) == 1
    assert "breast" not in fake_mealie.created[0]

    again = await call(mcp, "hellofresh_import", name="Curried Chicken Thigh Traybake", subtitle=SUBTITLE)
    assert again["import_status"] == "duplicate"
    assert len(fake_mealie.created) == 1


async def test_ambiguous_name_is_not_imported(mcp, fake_mealie):
    out = await call(mcp, "hellofresh_import", name="Curried Chicken Traybake")
    assert out["ok"] is False and out["import_status"] == "not_imported"
    assert fake_mealie.created == []


async def test_hellofresh_search_and_match_tools(mcp):
    search = await call(mcp, "hellofresh_search", query="curried chicken")
    assert len(search["results"]) == 2
    match = await call(mcp, "hellofresh_match", name="Curried Chicken Thigh Traybake", subtitle=SUBTITLE)
    assert match["status"] == "matched" and match["details"]["servings"] == 2


async def test_import_url_validation(mcp, fake_mealie):
    out = await call(mcp, "mealie_import_url", url="https://192.168.1.20/admin")
    assert out["ok"] is False and out["error"] == "invalid_url"
    out = await call(mcp, "mealie_import_url", url="https://www.example.com/recipe")
    assert out["error"] == "invalid_url" and "allowlist" in out["message"]
    assert fake_mealie.created == []


async def test_bulk_import(mcp, fake_mealie):
    fake_mealie.fail_import = "broken"
    urls = [
        THIGH_URL,
        THIGH_URL,
        BREAST_URL,
        "https://www.hellofresh.co.uk/recipes/broken-cccccccccccccccccccccccc",
        "http://insecure.example.com/x",
    ]
    out = await call(mcp, "mealie_bulk_import", urls=urls)
    assert out["summary"] == {"imported": 2, "duplicate_in_request": 1, "failed": 1, "invalid_url": 1}
    out = await call(mcp, "mealie_bulk_import", urls=[THIGH_URL])
    assert out["summary"] == {"duplicate": 1}


async def test_hellofresh_bulk_import(mcp, fake_mealie):
    out = await call(
        mcp,
        "hellofresh_bulk_import",
        recipes=[
            {"name": "Curried Chicken Thigh Traybake", "subtitle": SUBTITLE},
            {"name": "Totally Unknown Dish", "subtitle": None},
        ],
        dry_run=True,
    )
    assert out["summary"] == {"dry_run": 1, "not_found": 1}


async def test_check_duplicate_and_get(mcp, fake_mealie):
    await call(mcp, "mealie_import_url", url=THIGH_URL)
    dup = await call(mcp, "mealie_check_duplicate", url=THIGH_URL)
    assert dup["exists"] is True
    listed = await call(mcp, "mealie_list_recipes")
    slug = listed["recipes"][0]["slug"]
    got = await call(mcp, "mealie_get_recipe", slug=slug)
    assert got["recipe"]["instructions"]
    found = await call(mcp, "mealie_search_recipes", query="Curried")
    assert found["total"] == 1


async def test_forced_dry_run(settings, fake_mealie, hf_site):
    settings.dry_run = True
    server = build_server(
        settings, mealie=MealieClient(MEALIE, TOKEN, max_retries=1), hellofresh=HelloFreshClient(HF, min_interval=0)
    )
    out = await call(server, "mealie_import_url", url=THIGH_URL)
    assert out["status"] == "dry_run"
    assert fake_mealie.created == []


async def test_mealie_down(mcp, router):
    router.get(f"{MEALIE}/api/app/about").mock(side_effect=httpx.ConnectError("refused"))
    out = await call(mcp, "mealie_status")
    assert out["ok"] is False and out["error"] == "connection_error"


async def test_token_never_logged(mcp, fake_mealie, caplog):
    from mealie_mcp.logging_setup import RedactingFilter, Redactor

    caplog.handler.addFilter(RedactingFilter(Redactor([TOKEN])))
    caplog.set_level(logging.DEBUG)
    fake_mealie.token_ok = False
    await call(mcp, "mealie_status")
    logging.getLogger("t").warning("header was Bearer %s", TOKEN)
    assert TOKEN not in caplog.text
