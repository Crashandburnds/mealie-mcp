"""Library management tools: edits with backups, images, parsing, audit, meal plans, shopping lists."""

import copy
import json
import uuid

import httpx
import pytest
from fastmcp import Client

from mealie_mcp.hellofresh import HelloFreshClient
from mealie_mcp.manage import ingredients_from_text, minutes_text
from mealie_mcp.mealie import MealieClient
from mealie_mcp.server import build_server

from .conftest import HF, MEALIE, TOKEN

LIST_ID = "11111111-2222-3333-4444-555555555555"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def recipe(slug, name, **extra):
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, slug)),
        "slug": slug,
        "name": name,
        "groupId": "g1",
        "description": "",
        "image": "img1",
        "orgURL": None,
        "recipeServings": 2,
        "totalTime": "30 minutes",
        "prepTime": None,
        "cookTime": None,
        "performTime": None,
        "recipeIngredient": [
            {"note": "400g chicken thighs", "display": "400g chicken thighs", "food": None, "unit": None, "quantity": None},
            {"note": "1 onion", "display": "1 onion", "food": None, "unit": None, "quantity": None},
        ],
        "recipeInstructions": [{"text": "Cook it."}],
        "nutrition": {"calories": None},
        "tags": [],
        "recipeCategory": [],
        "tools": [],
        "notes": [],
        "rating": None,
        **extra,
    }


class Lib:
    """Stateful fake of the Mealie endpoints the management tools use."""

    def __init__(self, router):
        self.recipes = {
            "curry": recipe("curry", "Chicken Curry", orgURL="https://www.pinchofnom.com/recipes/curry/"),
            "pie": recipe("pie", "Shepherd's Pie", image=None, tags=[{"id": "t1", "name": "Comfort", "slug": "comfort"}]),
            "pie-1": recipe("pie-1", "Shepherd's Pie (1)", image=None),
        }
        self.tags = [{"id": "t1", "groupId": "g1", "name": "Comfort", "slug": "comfort"}]
        self.patches: list[tuple[str, dict]] = []
        self.images: list[tuple[str, bytes]] = []
        self.plan: list[dict] = []
        self.items: list[dict] = []
        self.foods: list[dict] = []
        self.list_recipes: list[dict] = []
        r = router
        r.get(f"{MEALIE}/api/users/self").mock(return_value=httpx.Response(200, json={"groupSlug": "home"}))
        r.get(f"{MEALIE}/api/recipes").mock(side_effect=self._list)
        r.get(url__regex=rf"{MEALIE}/api/recipes/[^/?]+$").mock(side_effect=self._get)
        r.patch(url__regex=rf"{MEALIE}/api/recipes/[^/?]+$").mock(side_effect=self._patch)
        r.put(url__regex=rf"{MEALIE}/api/recipes/[^/]+/image$").mock(side_effect=self._image)
        r.get(url__regex=rf"{MEALIE}/api/media/recipes/.+").mock(return_value=httpx.Response(200, content=b"old-image"))
        r.get(f"{MEALIE}/api/organizers/tags").mock(side_effect=self._tags)
        r.post(f"{MEALIE}/api/organizers/tags").mock(side_effect=self._new_tag)
        r.post(f"{MEALIE}/api/parser/ingredients").mock(side_effect=self._parse)
        r.get(f"{MEALIE}/api/foods").mock(side_effect=lambda req: httpx.Response(200, json={"items": self.foods}))
        r.post(f"{MEALIE}/api/foods").mock(side_effect=self._new_food)
        r.get(f"{MEALIE}/api/units").mock(return_value=httpx.Response(200, json={"items": [{"id": "u1", "name": "gram"}]}))
        r.get(f"{MEALIE}/api/households/mealplans").mock(side_effect=self._plan_get)
        r.post(f"{MEALIE}/api/households/mealplans").mock(side_effect=self._plan_add)
        r.delete(url__regex=rf"{MEALIE}/api/households/mealplans/\d+$").mock(side_effect=self._plan_del)
        r.get(f"{MEALIE}/api/households/shopping/lists/{LIST_ID}").mock(side_effect=self._list_get)
        r.post(f"{MEALIE}/api/households/shopping/lists/{LIST_ID}/recipe").mock(side_effect=self._list_add_recipes)
        r.post(f"{MEALIE}/api/households/shopping/items/create-bulk").mock(side_effect=self._items_add)
        r.put(f"{MEALIE}/api/households/shopping/items").mock(side_effect=self._items_put)
        r.delete(f"{MEALIE}/api/households/shopping/items").mock(side_effect=self._items_del)
        r.get("https://www.pinchofnom.com/recipes/curry/").mock(
            return_value=httpx.Response(
                200,
                text='<script type="application/ld+json">{"@graph":[{"@type":"Recipe","name":"Curry",'
                '"image":["https://cdn.pinchofnom.com/curry.png"]}]}</script>',
            )
        )
        r.get("https://cdn.pinchofnom.com/curry.png").mock(
            return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        )
        r.get("https://example.org/not-image").mock(
            return_value=httpx.Response(200, text="<html>", headers={"content-type": "text/html"})
        )

    def _list(self, req):
        items = list(self.recipes.values())
        return httpx.Response(200, json={"page": 1, "total_pages": 1, "items": items})

    def _get(self, req):
        slug = req.url.path.rsplit("/", 1)[-1]
        if slug not in self.recipes:
            return httpx.Response(404, json={"detail": "missing"})
        return httpx.Response(200, json=self.recipes[slug])

    def _patch(self, req):
        slug = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content)
        self.patches.append((slug, body))
        self.recipes[slug].update(copy.deepcopy(body))
        return httpx.Response(200, json=self.recipes[slug])

    def _image(self, req):
        self.images.append((req.url.path.split("/")[-2], req.content))
        return httpx.Response(200, json={"image": "new"})

    def _tags(self, req):
        q = (req.url.params.get("search") or "").lower()
        return httpx.Response(200, json={"items": [t for t in self.tags if q in t["name"].lower()]})

    def _new_tag(self, req):
        name = json.loads(req.content)["name"]
        tag = {"id": f"t{len(self.tags) + 1}", "groupId": "g1", "name": name, "slug": name.lower().replace(" ", "-")}
        self.tags.append(tag)
        return httpx.Response(201, json=tag)

    def _parse(self, req):
        out = []
        for text in json.loads(req.content)["ingredients"]:
            if "chicken" in text:
                ing = {"quantity": 400, "unit": {"name": "gram"}, "food": {"name": "chicken thighs"}, "note": ""}
                out.append({"input": text, "confidence": {"average": 0.9}, "ingredient": ing})
            else:
                ing = {"quantity": 1, "unit": None, "food": {"name": "onion"}, "note": ""}
                out.append({"input": text, "confidence": {"average": 0.3}, "ingredient": ing})
        return httpx.Response(200, json=out)

    def _new_food(self, req):
        food = {"id": f"f{len(self.foods) + 1}", "name": json.loads(req.content)["name"]}
        self.foods.append(food)
        return httpx.Response(201, json=food)

    def _plan_get(self, req):
        return httpx.Response(200, json={"items": self.plan})

    def _plan_add(self, req):
        body = json.loads(req.content)
        r = next((r for r in self.recipes.values() if r["id"] == body.get("recipeId")), None)
        entry = {"id": len(self.plan) + 1, **body, "recipe": r and {"name": r["name"], "slug": r["slug"]}}
        self.plan.append(entry)
        return httpx.Response(201, json=entry)

    def _plan_del(self, req):
        entry_id = int(req.url.path.rsplit("/", 1)[-1])
        self.plan = [p for p in self.plan if p["id"] != entry_id]
        return httpx.Response(200, json={})

    def _list_get(self, req):
        return httpx.Response(
            200, json={"id": LIST_ID, "name": "Week", "listItems": self.items, "recipeReferences": self.list_recipes}
        )

    def _list_add_recipes(self, req):
        for ref in json.loads(req.content):
            r = next(r for r in self.recipes.values() if r["id"] == ref["recipeId"])
            self.list_recipes.append(
                {"recipe": {"name": r["name"], "slug": r["slug"]}, "recipeQuantity": ref["recipeIncrementQuantity"]}
            )
            for ing in r["recipeIngredient"]:
                self.items.append({"id": str(uuid.uuid4()), "display": ing["display"], "checked": False})
        return httpx.Response(200, json={})

    def _items_add(self, req):
        for i in json.loads(req.content):
            self.items.append({"id": str(uuid.uuid4()), "note": i["note"], "display": i["note"], "checked": False})
        return httpx.Response(201, json={})

    def _items_put(self, req):
        for upd in json.loads(req.content):
            for item in self.items:
                if item["id"] == upd["id"]:
                    item["checked"] = upd["checked"]
        return httpx.Response(200, json={})

    def _items_del(self, req):
        ids = set(req.url.params.get_list("ids"))
        self.items = [i for i in self.items if i["id"] not in ids]
        return httpx.Response(200, json={})


@pytest.fixture
def lib(router):
    return Lib(router)


@pytest.fixture
def mcp(settings, lib):
    return build_server(
        settings, mealie=MealieClient(MEALIE, TOKEN, max_retries=1), hellofresh=HelloFreshClient(HF, min_interval=0)
    )


async def call(mcp, tool, **args):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    return result.structured_content


def test_helpers():
    assert minutes_text(75) == "1 hour 15 minutes"
    assert minutes_text(120) == "2 hours"
    lines = ingredients_from_text(["## For the sauce", "1 tbsp soy", "", "2 garlic cloves"])
    assert lines[0]["title"] == "For the sauce" and lines[0]["note"] == "1 tbsp soy"
    assert "title" not in lines[1]


async def test_no_recipe_delete_tool(mcp):
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"mealie_update_recipe", "mealie_audit_recipes", "mealie_add_meal_plan_entries", "mealie_get_shopping_list"} <= names
    assert not any("delete" in n or ("remove" in n and "recipe" in n and "shopping" not in n) for n in names)


async def test_update_dry_run_changes_nothing(mcp, lib):
    out = await call(mcp, "mealie_update_recipe", slug="curry", servings=4, add_tags=["Fakeaway"], dry_run=True)
    assert out["status"] == "dry_run"
    assert out["changes"]["recipeServings"] == {"from": 2, "to": 4}
    assert out["changes"]["tags"]["to"] == ["Fakeaway"]
    assert lib.patches == [] and len(lib.tags) == 1


async def test_update_backs_up_and_restores(mcp, lib, settings):
    out = await call(
        mcp,
        "mealie_update_recipe",
        slug="pie",
        description="Rich lamb pie",
        prep_minutes=20,
        add_tags=["comfort", "Freezable"],
        nutrition={"calories": "450 kcal"},
    )
    assert out["status"] == "updated" and out["backup_id"]
    slug, patch = lib.patches[0]
    assert slug == "pie" and patch["description"] == "Rich lamb pie" and patch["prepTime"] == "20 minutes"
    # Existing tag matched case-insensitively, new one created once.
    assert [t["name"] for t in patch["tags"]] == ["Comfort", "Freezable"]
    assert patch["nutrition"]["calories"] == "450 kcal"
    assert (settings.data_dir / "backups" / "pie" / f"{out['backup_id']}.json").exists()

    backups = await call(mcp, "mealie_list_recipe_backups", slug="pie")
    assert backups["backups"][0]["backup_id"] == out["backup_id"]
    restored = await call(mcp, "mealie_restore_recipe", slug="pie", backup_id=out["backup_id"])
    assert restored["status"] == "restored"
    assert lib.recipes["pie"]["description"] == "" and [t["name"] for t in lib.recipes["pie"]["tags"]] == ["Comfort"]


async def test_update_no_changes(mcp, lib):
    out = await call(mcp, "mealie_update_recipe", slug="curry", servings=2)
    assert out["status"] == "no_changes" and lib.patches == []


async def test_replace_ingredients(mcp, lib):
    out = await call(mcp, "mealie_update_recipe", slug="curry", ingredients=["## Curry", "500g chicken", "2 onions"])
    assert out["status"] == "updated"
    ings = lib.patches[0][1]["recipeIngredient"]
    assert [i["note"] for i in ings] == ["500g chicken", "2 onions"] and ings[0]["title"] == "Curry"
    # Mealie's PATCH drops unset defaults: without explicit ids it stores rows it can no longer load.
    assert all(i["referenceId"] for i in ings)
    assert all(st["id"] for st in lib.patches[0][1].get("recipeInstructions") or [{"id": 1}])


async def test_set_image_from_source_page(mcp, lib):
    out = await call(mcp, "mealie_set_recipe_image", slug="curry")
    assert out["status"] == "updated"
    assert out["image_url"] == "https://cdn.pinchofnom.com/curry.png"
    assert lib.images and lib.images[0][0] == "curry" and PNG in lib.images[0][1]


async def test_set_image_rejects_lan_and_non_images(mcp, lib):
    out = await call(mcp, "mealie_set_recipe_image", slug="pie", image_url="https://192.168.1.5/a.jpg")
    assert out["ok"] is False and out["error"] == "invalid_url"
    out = await call(mcp, "mealie_set_recipe_image", slug="pie", image_url="https://example.org/not-image")
    assert out["status"] == "failed" and out["error"] == "not_an_image"
    out = await call(mcp, "mealie_set_recipe_image", slug="pie")
    assert out["error"] == "no_source"
    assert lib.images == []


async def test_parse_ingredients_preview_then_apply(mcp, lib):
    preview = await call(mcp, "mealie_parse_ingredients", slug="curry")
    assert preview["status"] == "dry_run" and lib.patches == []
    assert [p["will_apply"] for p in preview["parsed"]] == [True, False]
    out = await call(mcp, "mealie_parse_ingredients", slug="curry", apply=True)
    assert out["parsed_count"] == 1 and out["left_unparsed"] == 1
    ings = lib.patches[0][1]["recipeIngredient"]
    assert ings[0]["food"]["id"] == "f1" and ings[0]["unit"]["id"] == "u1" and ings[0]["quantity"] == 400
    assert ings[1]["food"] is None  # low confidence: left alone


async def test_audit(mcp, lib):
    out = await call(mcp, "mealie_audit_recipes")
    assert out["recipes_checked"] == 3
    assert {r["slug"] for r in out["issues"]["no_image"]} == {"pie", "pie-1"}
    assert out["counts"]["unparsed_ingredients"] == 3
    dup = out["issues"]["possible_duplicates"][0]
    assert {r["slug"] for r in dup["recipes"]} == {"pie", "pie-1"}


async def test_meal_plan_and_shopping(mcp, lib):
    out = await call(
        mcp,
        "mealie_add_meal_plan_entries",
        entries=[
            {"date": "2026-10-12", "recipe_slug": "curry"},
            {"date": "2026-10-13", "title": "Takeaway"},
            {"date": "2026-10-14"},
        ],
    )
    statuses = [r["status"] for r in out["results"]]
    assert statuses == ["added", "added", "failed"]
    plan = await call(mcp, "mealie_get_meal_plan", start_date="2026-10-12")
    assert plan["entries"][0]["recipe"]["slug"] == "curry"

    out = await call(mcp, "mealie_add_meal_plan_to_shopping_list", list_id=LIST_ID, start_date="2026-10-12")
    assert out["status"] == "added" and out["added_recipes"] == ["Chicken Curry"]
    out = await call(mcp, "mealie_add_shopping_items", list_id=LIST_ID, items=[{"item": "milk"}])
    items = out["list"]["to_buy"]
    assert [i["item"] for i in items] == ["400g chicken thighs", "1 onion", "milk"]

    out = await call(mcp, "mealie_check_shopping_items", list_id=LIST_ID, item_ids=[items[2]["id"]])
    assert out["updated"] == 1
    out = await call(mcp, "mealie_remove_shopping_items", item_ids=[items[0]["id"]])
    assert out["removed"] == 1
    lst = await call(mcp, "mealie_get_shopping_list", list_id=LIST_ID)
    assert [i["item"] for i in lst["to_buy"]] == ["1 onion"] and lst["checked_count"] == 1

    out = await call(mcp, "mealie_remove_meal_plan_entries", entry_ids=[1])
    assert out["results"][0]["status"] == "removed" and len(lib.plan) == 1


async def test_global_dry_run_blocks_writes(settings, lib):
    settings.dry_run = True
    mcp = build_server(
        settings, mealie=MealieClient(MEALIE, TOKEN, max_retries=1), hellofresh=HelloFreshClient(HF, min_interval=0)
    )
    await call(mcp, "mealie_update_recipe", slug="curry", servings=6)
    await call(mcp, "mealie_parse_ingredients", slug="curry", apply=True)
    await call(mcp, "mealie_remove_shopping_items", item_ids=[LIST_ID])
    await call(mcp, "mealie_add_meal_plan_entries", entries=[{"date": "2026-10-12", "recipe_slug": "curry"}])
    assert lib.patches == [] and lib.plan == []


async def test_recipes_with_ingredients(mcp, lib):
    out = await call(mcp, "mealie_get_recipes_with_ingredients")
    assert out["total"] == 3 and len(out["recipes"]) == 3
    curry = next(r for r in out["recipes"] if r["slug"] == "curry")
    assert curry["ingredients"] == ["400g chicken thighs", "1 onion"] and curry["servings"] == 2
    out = await call(mcp, "mealie_get_recipes_with_ingredients", slugs=["pie", "missing"])
    assert out["recipes"][0]["name"] == "Shepherd's Pie" and "error" in out["recipes"][1]
