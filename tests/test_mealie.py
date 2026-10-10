import httpx
import pytest

from mealie_mcp.mealie import MealieClient, MealieError

from .conftest import MEALIE, THIGH_URL, TOKEN


async def test_status_ok(fake_mealie, mealie_client):
    status = await mealie_client.status()
    assert status["authenticated"] is True
    assert status["mealie_version"] == "v3.3.0"
    assert status["group"] == "home"


async def test_status_bad_token(fake_mealie):
    client = MealieClient(MEALIE, "wrong-token", max_retries=1)
    with pytest.raises(MealieError) as exc:
        await client.status()
    assert exc.value.kind == "auth_error"
    assert "wrong-token" not in str(exc.value)


async def test_list_and_search(fake_mealie, mealie_client):
    fake_mealie.recipes["pasta"] = {"slug": "pasta", "name": "Pasta Bake", "orgURL": None}
    fake_mealie.recipes["curry"] = {"slug": "curry", "name": "Thai Curry", "orgURL": None}
    listed = await mealie_client.list_recipes()
    assert listed["total"] == 2
    assert listed["recipes"][0]["mealie_url"].startswith(f"{MEALIE}/g/home/r/")
    found = await mealie_client.list_recipes(search="curry")
    assert [r["slug"] for r in found["recipes"]] == ["curry"]


async def test_get_recipe(fake_mealie, mealie_client):
    fake_mealie.recipes["curry"] = {"slug": "curry", "name": "Thai Curry", "orgURL": None}
    recipe = await mealie_client.get_recipe("curry")
    assert recipe["ingredients"] == ["450g Potatoes", "1 Korma Curry Paste"]
    assert recipe["instructions"] == ["Preheat oven.", "Roast."]
    assert recipe["nutrition"]["calories"] == "885 kcal"
    with pytest.raises(MealieError) as exc:
        await mealie_client.get_recipe("missing")
    assert exc.value.kind == "not_found"
    with pytest.raises(MealieError):
        await mealie_client.get_recipe("../admin")


async def test_import_then_duplicate(fake_mealie, mealie_client):
    first = await mealie_client.import_url(THIGH_URL)
    assert first.status == "imported"
    assert first.recipe["ingredient_count"] == 2
    second = await mealie_client.import_url(THIGH_URL + "?utm_source=x")
    assert second.status == "duplicate"
    assert second.duplicates[0]["match"] == "source_url"
    assert len(fake_mealie.created) == 1


async def test_duplicate_by_name(fake_mealie, mealie_client):
    fake_mealie.recipes["c"] = {"slug": "c", "name": "Curried Chicken Thigh Traybake (1)", "orgURL": "https://elsewhere.com/x"}
    dups = await mealie_client.find_duplicates(name="curried chicken thigh traybake")
    assert dups and dups[0]["match"] == "same_name"


async def test_dry_run_does_not_create(fake_mealie, mealie_client):
    outcome = await mealie_client.import_url(THIGH_URL, dry_run=True)
    assert outcome.status == "dry_run"
    assert outcome.recipe["ingredient_count"] == 2
    assert fake_mealie.created == []


async def test_failed_scrape_reported(fake_mealie, mealie_client):
    fake_mealie.fail_import = "broken"
    outcome = await mealie_client.import_url("https://www.hellofresh.co.uk/recipes/broken-64ad73db48c7b9302c333118")
    assert outcome.status == "failed"
    assert "scraping failed" in outcome.message


async def test_retries_on_503_then_succeeds(router):
    route = router.get(f"{MEALIE}/api/app/about")
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={"version": "v3"})]
    router.get(f"{MEALIE}/api/users/self").mock(return_value=httpx.Response(200, json={"username": "u"}))
    client = MealieClient(MEALIE, TOKEN, max_retries=2)
    assert (await client.status())["mealie_version"] == "v3"
    assert route.call_count == 2


async def test_timeout_and_connection_errors(router):
    router.get(f"{MEALIE}/api/app/about").mock(side_effect=httpx.ReadTimeout("slow"))
    client = MealieClient(MEALIE, TOKEN, max_retries=1)
    with pytest.raises(MealieError) as exc:
        await client.status()
    assert exc.value.kind == "timeout"
    router.get(f"{MEALIE}/api/app/about").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(MealieError) as exc:
        await client.status()
    assert exc.value.kind == "connection_error"


async def test_import_timeout_but_recipe_created(fake_mealie, router, mealie_client, monkeypatch):
    # Simulate Mealie finishing the import after our client gave up waiting.
    def slow_create(req):
        fake_mealie._create(req)
        raise httpx.ReadTimeout("slow")

    router.post(f"{MEALIE}/api/recipes/create/url").mock(side_effect=slow_create)
    monkeypatch.setattr("mealie_mcp.mealie.asyncio.sleep", _no_sleep)
    outcome = await mealie_client.import_url(THIGH_URL)
    assert outcome.status == "imported"


async def _no_sleep(_):
    return None


async def test_rate_limited(router):
    router.get(f"{MEALIE}/api/app/about").mock(return_value=httpx.Response(429))
    client = MealieClient(MEALIE, TOKEN, max_retries=1)
    with pytest.raises(MealieError) as exc:
        await client.status()
    assert exc.value.kind == "rate_limited"
