import httpx
import pytest

from mealie_mcp.hellofresh import HelloFreshError, normalise, split_title

from .conftest import BREAST_URL, HF, THIGH_URL
from .hf_fixtures import BREAST_PAGE, HOME_PAGE, SEARCH_API, SEARCH_HTML, THIGH_PAGE

SUBTITLE = "With Cumin Roast Potatoes, Carrots and Green Beans"


@pytest.fixture
def hf_site(router):
    router.get(f"{HF}/recipes").mock(return_value=httpx.Response(200, text=HOME_PAGE))
    api = router.get(f"{HF}/gw/recipes/recipes/search").mock(return_value=httpx.Response(200, json=SEARCH_API))
    router.get(f"{HF}/recipes/search").mock(return_value=httpx.Response(200, text=SEARCH_HTML))
    router.get(THIGH_URL).mock(return_value=httpx.Response(200, text=THIGH_PAGE))
    router.get(BREAST_URL).mock(return_value=httpx.Response(200, text=BREAST_PAGE))
    return api


def test_title_helpers():
    assert split_title("Curried Chicken Thigh Traybake with Cumin Roast Potatoes") == (
        "Curried Chicken Thigh Traybake",
        "with Cumin Roast Potatoes",
    )
    assert normalise("Fish & Chips!") == "fish and chips"


async def test_search_uses_api_with_anonymous_token(hf_site, hf_client):
    results, strategies, errors = await hf_client.search("curried chicken traybake")
    assert strategies == ["hellofresh_api"]
    assert {r.url for r in results} == {THIGH_URL, BREAST_URL}
    assert hf_site.calls.last.request.headers["Authorization"] == "Bearer anon-hf-token"


async def test_search_falls_back_to_html(hf_site, hf_client):
    hf_site.mock(return_value=httpx.Response(403))
    results, strategies, errors = await hf_client.search("curried")
    assert strategies == ["hellofresh_search_page"]
    assert THIGH_URL in {r.url for r in results}
    assert errors


async def test_match_picks_thigh_not_breast(hf_site, hf_client):
    result = await hf_client.match("Curried Chicken Thigh Traybake", SUBTITLE, include_details=True)
    assert result.status == "matched"
    assert result.match.url == THIGH_URL
    assert result.match.verified
    assert BREAST_URL in [a.url for a in result.alternatives]
    assert result.details["ingredients"][0] == "450 grams Potatoes"
    assert result.details["nutrition"]["calories"] == "885 kcal"
    assert result.details["total_time"] == "40 minutes"


async def test_match_handles_title_with_subtitle_in_one_string(hf_site, hf_client):
    result = await hf_client.match(f"Curried Chicken Thigh Traybake {SUBTITLE}")
    assert result.status == "matched" and result.match.url == THIGH_URL


async def test_no_fuzzy_import_for_similar_name(hf_site, hf_client):
    result = await hf_client.match("Curried Chicken Leg Traybake")
    assert result.status in ("ambiguous", "not_found")
    assert result.match is None


async def test_subtitle_mismatch_is_not_matched(hf_site, hf_client):
    result = await hf_client.match("Curried Chicken Thigh Traybake", "with Rice and Naan")
    assert result.status != "matched"
    assert "subtitle" in result.message


async def test_provided_url_is_verified(hf_site, hf_client):
    result = await hf_client.match("Curried Chicken Thigh Traybake", candidate_urls=[THIGH_URL])
    assert result.status == "matched"
    wrong = await hf_client.match("Curried Chicken Thigh Traybake", candidate_urls=[BREAST_URL])
    assert wrong.status != "matched"


async def test_invalid_hellofresh_url_rejected(hf_site, hf_client):
    result = await hf_client.match("x", candidate_urls=["https://evil.com/recipes/a-64ad73db48c7b9302c333117"])
    assert result.status != "matched"
    assert any("not a www.hellofresh.co.uk URL" in e for e in result.errors)


async def test_withdrawn_recipe(router, hf_client):
    url = f"{HF}/recipes/gone-recipe-aaaaaaaaaaaaaaaaaaaaaaaa"
    router.get(url).mock(return_value=httpx.Response(404))
    with pytest.raises(HelloFreshError) as exc:
        await hf_client.fetch_recipe(url)
    assert exc.value.kind == "not_found"


async def test_page_without_recipe_data(router, hf_client):
    url = f"{HF}/recipes/odd-page-bbbbbbbbbbbbbbbbbbbbbbbb"
    router.get(url).mock(return_value=httpx.Response(200, text="<html>no recipe</html>"))
    with pytest.raises(HelloFreshError) as exc:
        await hf_client.fetch_recipe(url)
    assert exc.value.kind == "not_a_recipe"


async def test_search_unavailable_gives_guidance(router, hf_client):
    router.get(f"{HF}/recipes").mock(side_effect=httpx.ConnectError("blocked"))
    router.get(f"{HF}/recipes/search").mock(side_effect=httpx.ConnectError("blocked"))
    result = await hf_client.match("Curried Chicken Thigh Traybake")
    assert result.status == "error"
    assert "candidate_urls" in result.message
