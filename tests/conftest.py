from __future__ import annotations

import json

import httpx
import pytest
import respx

from mealie_mcp import net
from mealie_mcp.config import Settings
from mealie_mcp.hellofresh import HelloFreshClient
from mealie_mcp.mealie import MealieClient

MEALIE = "https://mealie.example.com"
HF = "https://www.hellofresh.co.uk"
TOKEN = "mealie-secret-token-value-123456"
OWNER_PASSWORD = "correct-horse-battery-staple"
AUTH_SECRET = "x" * 48
STATIC_TOKEN = "static-token-for-tests-0123456789abcdef"

THIGH_ID = "64ad73db48c7b9302c333117"
THIGH_URL = f"{HF}/recipes/curried-chicken-thigh-traybake-{THIGH_ID}"
BREAST_ID = "64ef6810c09936aa866c1a15"
BREAST_URL = f"{HF}/recipes/curried-chicken-breast-traybake-{BREAST_ID}"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Pretend every hostname resolves to a public IP unless a test overrides it."""

    async def fake_resolve(host: str) -> list[str]:
        if host.endswith("evil-rebind.com"):
            return ["192.168.1.10"]
        return ["93.184.216.34"]

    monkeypatch.setattr(net, "_resolve", fake_resolve)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    for key in list(__import__("os").environ):
        if key.startswith(("MEALIE_", "AUTH_", "OWNER_", "PUBLIC_", "STATIC_", "DRY_RUN", "IMPORT_")):
            monkeypatch.delenv(key, raising=False)
    return Settings(
        _env_file=None,
        mealie_url=MEALIE,
        mealie_api_token=TOKEN,
        public_base_url="https://mcp.example.com",
        owner_password=OWNER_PASSWORD,
        auth_secret=AUTH_SECRET,
        static_tokens=[STATIC_TOKEN],
        data_dir=tmp_path,
        hellofresh_min_interval_seconds=0,
        log_format="text",
    )


def recipe_summary(slug: str, name: str, org_url: str | None = None) -> dict:
    return {
        "id": f"id-{slug}",
        "slug": slug,
        "name": name,
        "orgURL": org_url,
        "description": "",
        "totalTime": "40 minutes",
        "recipeServings": 2,
        "rating": None,
        "dateAdded": "2026-10-01",
    }


def full_recipe(slug: str, name: str, org_url: str | None = None) -> dict:
    return {
        **recipe_summary(slug, name, org_url),
        "image": "abc",
        "recipeIngredient": [{"display": "450g Potatoes"}, {"display": "1 Korma Curry Paste"}],
        "recipeInstructions": [{"text": "Preheat oven."}, {"text": "Roast."}],
        "nutrition": {"calories": "885 kcal", "proteinContent": "49.8 g"},
        "tags": [{"name": "HelloFresh"}],
        "recipeCategory": [],
    }


class FakeMealie:
    """A tiny stateful stand-in for the Mealie API, wired up through respx."""

    def __init__(self, router: respx.Router) -> None:
        self.recipes: dict[str, dict] = {}
        self.created: list[str] = []
        self.fail_import: str | None = None
        self.token_ok = True
        r = router
        r.get(f"{MEALIE}/api/app/about").mock(side_effect=lambda req: httpx.Response(200, json={"version": "v3.3.0"}))
        r.get(f"{MEALIE}/api/users/self").mock(side_effect=self._me)
        r.get(f"{MEALIE}/api/recipes").mock(side_effect=self._list)
        r.post(f"{MEALIE}/api/recipes/create/url").mock(side_effect=self._create)
        r.post(f"{MEALIE}/api/recipes/test-scrape-url").mock(side_effect=self._test_scrape)
        r.post(f"{MEALIE}/api/recipes/create/html-or-json").mock(side_effect=self._create_from_data)
        self.created_data: list[dict] = []
        r.get(url__regex=rf"{MEALIE}/api/recipes/[^/?]+$").mock(side_effect=self._get)

    def _auth(self, req) -> httpx.Response | None:
        if not self.token_ok or req.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "Not authenticated"})
        return None

    def _me(self, req):
        return self._auth(req) or httpx.Response(200, json={"username": "owner", "groupSlug": "home", "householdSlug": "family"})

    def _list(self, req):
        if err := self._auth(req):
            return err
        items = [recipe_summary(r["slug"], r["name"], r.get("orgURL")) for r in self.recipes.values()]
        q = req.url.params.get("search")
        if q:
            items = [i for i in items if q.lower() in i["name"].lower()]
        f = req.url.params.get("queryFilter")
        if f:
            needle = f.split('"%')[1].split('%"')[0]
            items = [i for i in items if needle in (i["orgURL"] or "")]
        # Real Mealie pagination fields are snake_case.
        return httpx.Response(200, json={"page": 1, "per_page": 50, "total": len(items), "total_pages": 1, "items": items})

    def _get(self, req):
        if err := self._auth(req):
            return err
        slug = req.url.path.rsplit("/", 1)[-1]
        if slug not in self.recipes:
            return httpx.Response(404, json={"detail": {"message": "No Entry Found"}})
        r = self.recipes[slug]
        return httpx.Response(200, json=full_recipe(slug, r["name"], r.get("orgURL")))

    def _create(self, req):
        if err := self._auth(req):
            return err
        body = json.loads(req.content)
        if self.fail_import and self.fail_import in body["url"]:
            return httpx.Response(400, json={"detail": {"message": "Recipe scraping failed"}})
        slug = body["url"].rstrip("/").rsplit("/", 1)[-1][:40]
        name = slug.replace("-", " ").title()
        if THIGH_ID in body["url"]:
            name = "Curried Chicken Thigh Traybake"
        self.recipes[slug] = {"slug": slug, "name": name, "orgURL": body["url"]}
        self.created.append(slug)
        return httpx.Response(201, json=slug)

    def _create_from_data(self, req):
        if err := self._auth(req):
            return err
        body = json.loads(req.content)
        recipe = json.loads(body["data"])
        self.created_data.append(recipe)
        slug = recipe["name"].lower().replace(" ", "-")
        self.recipes[slug] = {"slug": slug, "name": recipe["name"], "orgURL": None}
        self.created.append(slug)
        return httpx.Response(201, json=slug)

    def _test_scrape(self, req):
        if err := self._auth(req):
            return err
        return httpx.Response(
            200,
            json={
                "name": "Preview",
                "recipeIngredient": ["a", "b"],
                "recipeInstructions": [{"text": "x"}],
                "totalTime": "PT40M",
                "image": "i.jpg",
                "nutrition": {"calories": "1"},
            },
        )


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def fake_mealie(router) -> FakeMealie:
    return FakeMealie(router)


@pytest.fixture
def mealie_client():
    return MealieClient(MEALIE, TOKEN, max_retries=1)


@pytest.fixture
def hf_client():
    return HelloFreshClient(HF, min_interval=0)
