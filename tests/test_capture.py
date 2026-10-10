"""Phone capture app: login, uploads, background jobs, and routing to the import pipelines."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from mealie_mcp.hellofresh import HelloFreshClient
from mealie_mcp.mealie import MealieClient
from mealie_mcp.server import create_app
from mealie_mcp.vision import ReadRecipe, RecipeReader, VisionError

from .conftest import BREAST_URL, HF, MEALIE, OWNER_PASSWORD, THIGH_URL, TOKEN
from .hf_fixtures import BREAST_PAGE, HOME_PAGE, SEARCH_API, SEARCH_HTML, THIGH_PAGE

BASE = "https://mcp.example.com"
H = {"x-mealie-capture": "1"}

CARDS = {
    b"thigh": ReadRecipe(
        kind="hellofresh_card",
        title="Curried Chicken Thigh Traybake",
        subtitle="with Cumin Roast Potatoes, Carrots and Green Beans",
    ),
    b"vague": ReadRecipe(kind="hellofresh_card", title="Curried Chicken Traybake"),
    b"book": ReadRecipe(
        kind="cookbook_page",
        title="Melt-in-the-Middle Fishcakes",
        description="Homemade fishcakes.",
        servings=4,
        prep_minutes=20,
        cook_minutes=25,
        calories="273 kcal",
        carbs="24g",
        ingredients=["500g potatoes, peeled and chopped", "250g smoked white fish"],
        instructions=["Preheat the oven.", "Cook the potatoes."],
        tags=["Freezable", "Gluten free"],
        notes=["GF: use GF soy sauce"],
        book_page="80",
        section="Quick Meals",
    ),
    b"cat": ReadRecipe(kind="not_a_recipe"),
}


class FakeReader:
    def __init__(self):
        self.calls = 0

    async def read(self, image: bytes, media_type: str) -> ReadRecipe:
        self.calls += 1
        if image == b"boom":
            raise VisionError("Claude declined to read this image", kind="refusal")
        return CARDS[image]


@pytest.fixture
def hf_site(router):
    router.get(f"{HF}/recipes").mock(return_value=httpx.Response(200, text=HOME_PAGE))
    router.get(f"{HF}/gw/recipes/recipes/search").mock(return_value=httpx.Response(200, json=SEARCH_API))
    router.get(f"{HF}/recipes/search").mock(return_value=httpx.Response(200, text=SEARCH_HTML))
    router.get(THIGH_URL).mock(return_value=httpx.Response(200, text=THIGH_PAGE))
    router.get(BREAST_URL).mock(return_value=httpx.Response(200, text=BREAST_PAGE))


def make_client(settings, reader):
    app = create_app(
        settings,
        mealie=MealieClient(MEALIE, TOKEN, max_retries=1),
        hellofresh=HelloFreshClient(HF, min_interval=0),
        reader=reader,
    )
    return TestClient(app, base_url=BASE)


@pytest.fixture
def client(settings, fake_mealie, hf_site):
    reader = FakeReader()
    with make_client(settings, reader) as c:
        c.reader = reader
        yield c


def login(c):
    r = c.post("/capture/api/login", json={"password": OWNER_PASSWORD}, headers=H)
    assert r.status_code == 200, r.text
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()


def upload(c, *blobs, **form):
    files = [("images", (f"p{i}.jpg", b, "image/jpeg")) for i, b in enumerate(blobs)]
    return c.post("/capture/api/jobs", files=files, data=form, headers=H)


def wait(c, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        job = c.get(f"/capture/api/jobs/{job_id}").json()
        if job["done"]:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_page_and_assets_served(client):
    r = client.get("/capture")
    assert r.status_code == 200 and "Recipe Capture" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.get("/capture/manifest.webmanifest").json()["start_url"] == "/capture"
    assert client.get("/capture/icon-192.png").content.startswith(b"\x89PNG")
    assert client.get("/capture/nope.txt").status_code == 404


def test_requires_login(client):
    assert client.get("/capture/api/session").json()["authenticated"] is False
    assert upload(client, b"thigh").status_code == 401
    bad = client.post("/capture/api/login", json={"password": "wrong-password"}, headers=H)
    assert bad.status_code == 401
    no_header = client.post("/capture/api/login", json={"password": OWNER_PASSWORD})
    assert no_header.status_code == 403
    login(client)
    assert client.get("/capture/api/session").json() == {
        "authenticated": True,
        "configured": True,
        "auth_required": True,
        "max_images": 20,
    }


def test_forged_cookie_rejected(client):
    client.cookies.set("mmcp_capture", "not-a-real-token", path="/capture")
    assert client.get("/capture/api/jobs").status_code == 401


def test_csrf_header_required_for_uploads(client):
    login(client)
    r = client.post("/capture/api/jobs", files=[("images", ("a.jpg", b"thigh", "image/jpeg"))])
    assert r.status_code == 403


def test_batch_routes_each_photo(client, fake_mealie):
    login(client)
    r = upload(client, b"thigh", b"book", b"cat", b"vague", b"boom", book="Pinch of Nom")
    assert r.status_code == 202
    job = wait(client, r.json()["id"])
    by = {i["filename"]: i for i in job["items"]}
    assert by["p0.jpg"]["status"] == "imported"
    assert by["p1.jpg"]["status"] == "created"
    assert by["p2.jpg"]["status"] == "skipped"
    assert by["p3.jpg"]["status"] == "needs_review"
    assert by["p4.jpg"]["status"] == "failed" and "declined" in by["p4.jpg"]["message"]

    sent = fake_mealie.created_data[0]
    assert sent["name"] == "Melt-in-the-Middle Fishcakes"
    assert sent["description"].endswith("Source: Pinch of Nom, page 80, Quick Meals")
    assert "GF: use GF soy sauce" in sent["description"]
    assert sent["nutrition"]["calories"] == "273 kcal" and sent["nutrition"]["carbohydrateContent"] == "24g"
    assert "Pinch of Nom" in sent["keywords"] and sent["totalTime"] == "PT45M"

    # Running the same batch again finds everything already in Mealie.
    again = wait(client, upload(client, b"thigh", b"book").json()["id"])
    assert [i["status"] for i in again["items"]] == ["duplicate", "duplicate"]


def test_review_choice_import(client, fake_mealie):
    login(client)
    job = wait(client, upload(client, b"vague").json()["id"])
    alts = job["items"][0]["alternatives"]
    assert {a["url"] for a in alts} >= {THIGH_URL, BREAST_URL}
    r = client.post("/capture/api/import-url", json={"url": BREAST_URL}, headers=H)
    assert r.json()["ok"] and r.json()["status"] == "imported"
    assert any("breast" in s for s in fake_mealie.created)


def test_dry_run_saves_nothing(client, fake_mealie):
    login(client)
    job = wait(client, upload(client, b"thigh", b"book", dry_run="1").json()["id"])
    assert [i["status"] for i in job["items"]] == ["dry_run", "dry_run"]
    assert fake_mealie.created == []


def test_upload_validation(client):
    login(client)
    r = client.post("/capture/api/jobs", files=[("images", ("a.txt", b"hello", "text/plain"))], headers=H)
    assert r.status_code == 400
    too_many = upload(client, *([b"thigh"] * 21))
    assert too_many.status_code == 400


def test_not_configured_without_api_key(settings, fake_mealie, hf_site):
    app = create_app(settings, mealie=MealieClient(MEALIE, TOKEN), hellofresh=HelloFreshClient(HF))
    with TestClient(app, base_url=BASE) as c:
        login(c)
        assert c.get("/capture/api/session").json()["configured"] is False
        assert upload(c, b"thigh").status_code == 503


# ---------------------------------------------------------------- vision


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_anthropic(response):
    messages = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def response(stop_reason="end_turn", payload=None):
    text = json.dumps(payload or {"kind": "hellofresh_card", "title": "Bulgogi Beef Bowl", "subtitle": "with Rice"})
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1500, output_tokens=200),
    )


async def test_reader_request_shape_and_parse():
    client, messages = fake_anthropic(response())
    reader = RecipeReader("key", client=client)
    out = await reader.read(b"jpegbytes", "image/jpeg")
    assert out.kind == "hellofresh_card" and out.title == "Bulgogi Beef Bowl"
    kw = messages.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    image = kw["messages"][0]["content"][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"


async def test_reader_refusal_and_bad_type():
    client, _ = fake_anthropic(response(stop_reason="refusal"))
    with pytest.raises(VisionError) as exc:
        await RecipeReader("key", client=client).read(b"x", "image/jpeg")
    assert exc.value.kind == "refusal"
    with pytest.raises(VisionError):
        await RecipeReader("key", client=client).read(b"x", "application/pdf")


def test_empty_api_token_means_disabled(settings):
    from mealie_mcp.config import Settings

    s = Settings(**{**settings.model_dump(), "claude_api_token": "  "})
    assert s.claude_api_token is None
