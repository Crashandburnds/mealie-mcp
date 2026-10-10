"""Library management on top of the Mealie API: audit and edit recipes, images, ingredient parsing,
meal plans and shopping lists.

Safety rules:
- Recipes are never deleted.
- Every recipe edit first saves a JSON backup of the recipe (and its old image) under DATA_DIR/backups,
  so any change can be undone with mealie_restore_recipe.
- Image URLs and source pages must be public https addresses (no LAN access).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .logging_setup import log
from .mealie import MAX_PER_PAGE, MealieClient, MealieError, _get, _norm_name
from .net import USER_AGENT, allowlisted_client, hellofresh_id, validate_recipe_url

logger = logging.getLogger("mealie_mcp.manage")

# Recipe fields a backup restores (everything a user can edit in the Mealie UI).
EDITABLE_FIELDS = (
    "name",
    "description",
    "recipeServings",
    "recipeYieldQuantity",
    "recipeYield",
    "totalTime",
    "prepTime",
    "cookTime",
    "performTime",
    "recipeIngredient",
    "recipeInstructions",
    "nutrition",
    "tags",
    "recipeCategory",
    "tools",
    "notes",
    "orgURL",
    "rating",
)

NUTRITION_FIELDS = {
    "calories": "calories",
    "protein": "proteinContent",
    "fat": "fatContent",
    "saturated_fat": "saturatedFatContent",
    "carbohydrates": "carbohydrateContent",
    "carbs": "carbohydrateContent",
    "sugar": "sugarContent",
    "fibre": "fiberContent",
    "fiber": "fiberContent",
    "salt": "sodiumContent",
    "sodium": "sodiumContent",
    "cholesterol": "cholesterolContent",
}

MEAL_TYPES = ("breakfast", "lunch", "dinner", "side")
AUDIT_CHECKS = (
    "no_image",
    "no_ingredients",
    "no_instructions",
    "no_servings",
    "no_time",
    "no_tags",
    "no_categories",
    "no_source",
    "no_nutrition",
    "unparsed_ingredients",
    "possible_duplicates",
)
MAX_IMAGE_BYTES = 15 * 1024 * 1024
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


def minutes_text(minutes: int | None) -> str | None:
    """Mealie stores times as display text, e.g. '1 hour 15 minutes'."""
    if minutes is None:
        return None
    if minutes <= 0:
        return ""
    hours, mins = divmod(int(minutes), 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if mins:
        parts.append(f"{mins} minute" + ("s" if mins != 1 else ""))
    return " ".join(parts)


def ingredients_from_text(lines: list[str]) -> list[dict[str, Any]]:
    """Plain-text ingredient lines -> Mealie ingredients. A line starting with '## ' starts a section."""
    out: list[dict[str, Any]] = []
    title: str | None = None
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        if line.startswith("#"):
            title = line.lstrip("#").strip() or None
            continue
        # Mealie's PATCH skips unset defaults, so ids must be sent explicitly or the row is stored without one.
        item: dict[str, Any] = {
            "referenceId": str(uuid.uuid4()),
            "quantity": None,
            "unit": None,
            "food": None,
            "note": line,
            "originalText": line,
            "display": line,
        }
        if title:
            item["title"] = title
            title = None
        out.append(item)
    return out


def instructions_from_text(lines: list[str]) -> list[dict[str, Any]]:
    steps = []
    title = ""
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            continue
        steps.append({"id": str(uuid.uuid4()), "title": title, "summary": "", "text": line, "ingredientReferences": []})
        title = ""
    return steps


def ingredient_text(ing: dict[str, Any]) -> str:
    return (ing.get("display") or ing.get("originalText") or ing.get("note") or "").strip()


def _clean_slug(slug: str) -> str:
    slug = (slug or "").strip()
    if not SLUG_RE.match(slug):
        raise MealieError("Invalid recipe slug", kind="invalid_input")
    return slug


def _plan_entry(item: dict[str, Any], base_url: str, group: str | None) -> dict[str, Any]:
    recipe = item.get("recipe") or None
    out: dict[str, Any] = {
        "id": item.get("id"),
        "date": item.get("date"),
        "meal": item.get("entryType"),
    }
    if recipe:
        slug = recipe.get("slug")
        out["recipe"] = {
            "name": recipe.get("name"),
            "slug": slug,
            "mealie_url": f"{base_url}/g/{group}/r/{slug}" if group else f"{base_url}/recipe/{slug}",
        }
    if item.get("title"):
        out["title"] = item["title"]
    if item.get("text"):
        out["note"] = item["text"]
    return out


def _shopping_item(item: dict[str, Any], recipe_names: dict[str, str]) -> dict[str, Any]:
    label = (item.get("label") or {}).get("name")
    recipes = [recipe_names.get(ref.get("recipeId") or "") for ref in item.get("recipeReferences") or []]
    out = {
        "id": item.get("id"),
        "item": (item.get("display") or item.get("note") or "").strip() or ((item.get("food") or {}).get("name") or ""),
        "checked": bool(item.get("checked")),
    }
    if label:
        out["section"] = label
    recipes = [r for r in recipes if r]
    if recipes:
        out["for"] = recipes
    return out


class Manager:
    def __init__(self, mealie: MealieClient, data_dir: Path, *, import_domains: list[str], image_timeout: float = 20.0) -> None:
        self.mealie = mealie
        self.backup_dir = Path(data_dir) / "backups"
        self.import_domains = import_domains
        self.image_timeout = image_timeout
        self._organizer_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return self.mealie.base_url

    async def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.mealie._request(method, path, **kwargs)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # ================================================================ recipes
    async def raw_recipe(self, slug: str) -> dict[str, Any]:
        return await self._req("GET", f"/api/recipes/{_clean_slug(slug)}")

    async def _all_summaries(self, max_recipes: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while len(items) < max_recipes:
            data = await self._req(
                "GET", "/api/recipes", params={"page": page, "perPage": MAX_PER_PAGE, "orderBy": "name", "orderDirection": "asc"}
            )
            items += data.get("items", [])
            if page >= _get(data, "total_pages", "totalPages", default=0) or not data.get("items"):
                break
            page += 1
        return items[:max_recipes]

    # ------------------------------------------------------------- backups
    def _backup_path(self, slug: str) -> Path:
        return self.backup_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", slug)

    async def backup_recipe(self, recipe: dict[str, Any], reason: str, *, with_image: bool = False) -> str:
        folder = self._backup_path(recipe["slug"])
        folder.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = {"reason": reason, "saved_at": stamp, "recipe": recipe}
        (folder / f"{stamp}.json").write_text(json.dumps(backup, ensure_ascii=False))
        if with_image and recipe.get("image") and recipe.get("id"):
            try:
                response = await self.mealie._request("GET", f"/api/media/recipes/{recipe['id']}/images/original.webp")
                if response.content and len(response.content) <= MAX_IMAGE_BYTES:
                    (folder / f"{stamp}.webp").write_bytes(response.content)
            except MealieError as exc:
                log(logger, logging.WARNING, "image backup failed", slug=recipe["slug"], kind=exc.kind)
        log(logger, logging.INFO, "recipe backed up", slug=recipe["slug"], backup=stamp, reason=reason)
        return stamp

    def list_backups(self, slug: str) -> list[dict[str, Any]]:
        folder = self._backup_path(_clean_slug(slug))
        if not folder.is_dir():
            return []
        out = []
        for path in sorted(folder.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            out.append(
                {
                    "backup_id": path.stem,
                    "reason": data.get("reason"),
                    "name": (data.get("recipe") or {}).get("name"),
                    "has_image": path.with_suffix(".webp").exists(),
                }
            )
        return out

    async def restore(self, slug: str, backup_id: str, *, dry_run: bool) -> dict[str, Any]:
        if not re.fullmatch(r"\d{8}T\d{12}Z", backup_id or ""):
            raise MealieError("Invalid backup id", kind="invalid_input")
        folder = self._backup_path(_clean_slug(slug))
        path = folder / f"{backup_id}.json"
        if not path.is_file():
            raise MealieError("Backup not found", status=404, kind="not_found")
        saved = json.loads(path.read_text())["recipe"]
        current = await self.raw_recipe(slug)
        patch = {k: saved.get(k) for k in EDITABLE_FIELDS if k in saved}
        image = path.with_suffix(".webp")
        if dry_run:
            return {
                "status": "dry_run",
                "changes": _diff(current, patch),
                "restores_image": image.exists(),
                "message": "Dry run: nothing was changed.",
            }
        before = await self.backup_recipe(current, f"before restoring {backup_id}", with_image=image.exists())
        target = current["slug"]
        await self._patch(target, patch)
        if image.exists():
            await self._upload_image(target, image.read_bytes(), "webp")
        return {
            "status": "restored",
            "backup_of_previous_version": before,
            "mealie_url": await self.mealie.recipe_link(target),
        }

    # --------------------------------------------------------------- update
    async def _patch(self, slug: str, patch: dict[str, Any]) -> dict[str, Any]:
        return await self._req("PATCH", f"/api/recipes/{slug}", json=patch)

    async def _organizers(self, kind: str, names: list[str]) -> list[dict[str, Any]]:
        """Resolve tag/category/tool names to Mealie objects, creating any that do not exist yet."""
        out: list[dict[str, Any]] = []
        async with self._organizer_lock:
            for name in names:
                name = name.strip()
                if not name:
                    continue
                data = await self._req("GET", f"/api/organizers/{kind}", params={"search": name, "perPage": 50})
                match = next(
                    (i for i in data.get("items", []) if _norm_name(i.get("name") or "") == _norm_name(name)),
                    None,
                )
                if match is None:
                    match = await self._req("POST", f"/api/organizers/{kind}", json={"name": name})
                    log(logger, logging.INFO, "organizer created", kind=kind, name=name)
                if match and match.get("id") not in {o.get("id") for o in out}:
                    out.append({k: match.get(k) for k in ("id", "groupId", "name", "slug") if match.get(k) is not None})
        return out

    async def update_recipe(self, slug: str, changes: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        current = await self.raw_recipe(slug)
        patch: dict[str, Any] = {}

        for key, field in (("name", "name"), ("description", "description"), ("source_url", "orgURL")):
            if changes.get(key) is not None:
                patch[field] = changes[key].strip()
        if changes.get("servings") is not None and changes["servings"] != current.get("recipeServings"):
            patch["recipeServings"] = changes["servings"]
            patch["recipeYieldQuantity"] = changes["servings"]
            patch["recipeYield"] = f"{changes['servings']:g} servings"
        for key, field in (
            ("prep_minutes", "prepTime"),
            ("cook_minutes", "performTime"),
            ("total_minutes", "totalTime"),
        ):
            if changes.get(key) is not None:
                patch[field] = minutes_text(changes[key])
        if changes.get("rating") is not None:
            patch["rating"] = changes["rating"]
        if changes.get("ingredients") is not None:
            patch["recipeIngredient"] = ingredients_from_text(changes["ingredients"])
        if changes.get("instructions") is not None:
            patch["recipeInstructions"] = instructions_from_text(changes["instructions"])
        if changes.get("notes") is not None:
            patch["notes"] = [{"title": "", "text": n.strip()} for n in changes["notes"] if n.strip()]
        if changes.get("nutrition"):
            nutrition = dict(current.get("nutrition") or {})
            for k, v in changes["nutrition"].items():
                field = NUTRITION_FIELDS.get(k.lower().strip())
                if field:
                    nutrition[field] = str(v).strip() or None
            patch["nutrition"] = nutrition

        for kind, field, key in (
            ("tags", "tags", "tags"),
            ("categories", "recipeCategory", "categories"),
            ("tools", "tools", "tools"),
        ):
            existing = [o.get("name") for o in current.get(field) or []]
            names: list[str] | None = None
            if changes.get(key) is not None:
                names = list(changes[key])
            if changes.get(f"add_{key}") or changes.get(f"remove_{key}"):
                names = list(names if names is not None else existing)
                for n in changes.get(f"add_{key}") or []:
                    if _norm_name(n) not in {_norm_name(x) for x in names}:
                        names.append(n)
                drop = {_norm_name(n) for n in changes.get(f"remove_{key}") or []}
                names = [n for n in names if _norm_name(n) not in drop]
            if names is not None and [_norm_name(n) for n in names] != [_norm_name(n) for n in existing]:
                if dry_run:
                    patch[field] = [{"name": n} for n in names]
                else:
                    patch[field] = await self._organizers(kind, names)

        if not patch:
            return {"status": "no_changes", "message": "Nothing to change."}
        diff = _diff(current, patch)
        if not diff:
            return {"status": "no_changes", "message": "The recipe already has these values."}
        if dry_run:
            return {"status": "dry_run", "changes": diff, "message": "Dry run: nothing was changed."}
        patch = {k: v for k, v in patch.items() if k in diff}
        backup = await self.backup_recipe(current, "before update")
        updated = await self._patch(current["slug"], patch)
        new_slug = (updated or {}).get("slug") or current["slug"]
        log(logger, logging.INFO, "recipe updated", slug=new_slug, fields=sorted(patch))
        return {
            "status": "updated",
            "slug": new_slug,
            "changes": diff,
            "backup_id": backup,
            "mealie_url": await self.mealie.recipe_link(new_slug),
            "message": "Updated. Undo with mealie_restore_recipe using backup_id.",
        }

    # ---------------------------------------------------------------- images
    async def _upload_image(self, slug: str, data: bytes, extension: str) -> None:
        await self.mealie._request(
            "PUT",
            f"/api/recipes/{slug}/image",
            files={"image": (f"image.{extension}", data, f"image/{extension}")},
            data={"extension": extension},
        )

    async def _fetch_public(self, url: str, *, allowed: list[str]) -> httpx.Response:
        """GET a public https URL, re-validating every redirect hop (no LAN access)."""
        current = url
        for _ in range(5):
            clean = await validate_recipe_url(current, allowed)
            host = urlsplit(clean).hostname or ""
            async with allowlisted_client([host], timeout=self.image_timeout, headers={"User-Agent": USER_AGENT}) as client:
                async with client.stream("GET", clean) as response:
                    if response.is_redirect and response.headers.get("location"):
                        current = str(response.url.join(response.headers["location"]))
                        continue
                    if response.status_code >= 400:
                        raise MealieError(f"{host} returned HTTP {response.status_code}", kind="fetch_failed")
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_IMAGE_BYTES:
                            raise MealieError("File is too large", kind="too_large")
                    # Body is already decompressed, so keep only the content type.
                    headers = {"content-type": response.headers.get("content-type", "")}
                    return httpx.Response(response.status_code, headers=headers, content=body, request=response.request)
        raise MealieError("Too many redirects", kind="fetch_failed")

    async def _image_from_source(self, page_url: str) -> str | None:
        response = await self._fetch_public(page_url, allowed=self.import_domains)
        html = response.text
        for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            for node in _walk_jsonld(data):
                if "Recipe" in _types(node) and node.get("image"):
                    image = node["image"]
                    if isinstance(image, list):
                        image = image[0] if image else None
                    if isinstance(image, dict):
                        image = image.get("url")
                    if isinstance(image, str):
                        return str(response.url.join(image))
        match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html, re.I
        )
        return str(response.url.join(match.group(1))) if match else None

    async def set_image(self, slug: str, image_url: str | None, *, dry_run: bool) -> dict[str, Any]:
        current = await self.raw_recipe(slug)
        source = None
        if not image_url:
            source = current.get("orgURL")
            if not source:
                return {"status": "failed", "error": "no_source", "message": "Recipe has no source URL; pass image_url."}
            image_url = await self._image_from_source(source)
            if not image_url:
                return {"status": "failed", "error": "no_image_found", "message": f"No recipe image found on {source}."}
        await validate_recipe_url(image_url, ["*"])
        if dry_run:
            return {
                "status": "dry_run",
                "image_url": image_url,
                "found_on": source,
                "replaces_existing_image": bool(current.get("image")),
                "message": "Dry run: image not changed.",
            }
        response = await self._fetch_public(image_url, allowed=["*"])
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/avif": "avif", "image/gif": "gif"}.get(
            content_type
        )
        if not extension or not response.content:
            return {
                "status": "failed",
                "error": "not_an_image",
                "message": f"URL did not return an image ({content_type or 'unknown type'}).",
            }
        backup = await self.backup_recipe(current, "before image change", with_image=True)
        await self._upload_image(current["slug"], response.content, extension)
        log(logger, logging.INFO, "recipe image set", slug=current["slug"], host=urlsplit(image_url).hostname)
        return {
            "status": "updated",
            "image_url": image_url,
            "found_on": source,
            "backup_id": backup,
            "mealie_url": await self.mealie.recipe_link(current["slug"]),
        }

    # ----------------------------------------------------- ingredient parser
    async def _ensure_food_or_unit(
        self, kind: str, obj: dict[str, Any] | None, cache: dict[str, dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not obj or not (obj.get("name") or "").strip():
            return None
        if obj.get("id"):
            return obj
        key = _norm_name(obj["name"])
        if key in cache:
            return cache[key]
        data = await self._req("GET", f"/api/{kind}", params={"search": obj["name"], "perPage": 50})
        match = next((i for i in data.get("items", []) if _norm_name(i.get("name") or "") == key), None)
        if match is None:
            match = await self._req("POST", f"/api/{kind}", json={"name": obj["name"].strip()})
            log(logger, logging.INFO, "created", kind=kind, name=obj["name"])
        cache[key] = match
        return match

    async def parse_ingredients(self, slug: str, *, apply: bool, min_confidence: float) -> dict[str, Any]:
        current = await self.raw_recipe(slug)
        ingredients = current.get("recipeIngredient") or []
        todo = [(i, ing) for i, ing in enumerate(ingredients) if not ing.get("food") and ingredient_text(ing)]
        if not todo:
            return {"status": "no_changes", "message": "All ingredients are already parsed."}
        texts = [ing.get("originalText") or ingredient_text(ing) for _, ing in todo]
        parsed = await self._req("POST", "/api/parser/ingredients", json={"parser": "nlp", "ingredients": texts})
        preview = []
        accepted: list[tuple[int, dict[str, Any]]] = []
        for (index, _original), result in zip(todo, parsed or [], strict=False):
            ing = result.get("ingredient") or {}
            confidence = (result.get("confidence") or {}).get("average")
            ok = bool((ing.get("food") or {}).get("name")) and (confidence is None or confidence >= min_confidence)
            preview.append(
                {
                    "text": texts[len(preview)],
                    "quantity": ing.get("quantity"),
                    "unit": (ing.get("unit") or {}).get("name"),
                    "food": (ing.get("food") or {}).get("name"),
                    "note": ing.get("note") or None,
                    "confidence": round(confidence, 2) if isinstance(confidence, float) else confidence,
                    "will_apply": ok,
                }
            )
            if ok:
                accepted.append((index, ing))
        if not apply:
            return {
                "status": "dry_run",
                "parsed": preview,
                "message": "Preview only. Call again with apply=true to save the ingredients marked will_apply.",
            }
        if not accepted:
            return {"status": "no_changes", "parsed": preview, "message": "No ingredient was parsed confidently enough."}
        backup = await self.backup_recipe(current, "before ingredient parsing")
        foods: dict[str, dict[str, Any]] = {}
        units: dict[str, dict[str, Any]] = {}
        new_list = [dict(i) for i in ingredients]
        for index, ing in accepted:
            original = ingredients[index]
            new_list[index] = {
                **original,
                "quantity": ing.get("quantity") or 0,
                "unit": await self._ensure_food_or_unit("units", ing.get("unit"), units),
                "food": await self._ensure_food_or_unit("foods", ing.get("food"), foods),
                "note": ing.get("note") or "",
                "originalText": original.get("originalText") or ingredient_text(original),
                "display": "",
            }
        await self._patch(current["slug"], {"recipeIngredient": new_list})
        return {
            "status": "updated",
            "parsed_count": len(accepted),
            "left_unparsed": len(todo) - len(accepted),
            "parsed": preview,
            "backup_id": backup,
            "mealie_url": await self.mealie.recipe_link(current["slug"]),
        }

    # ----------------------------------------------------------------- audit
    async def audit(self, *, checks: list[str] | None, max_recipes: int, limit_per_issue: int) -> dict[str, Any]:
        wanted = [c for c in (checks or AUDIT_CHECKS) if c in AUDIT_CHECKS]
        summaries = await self._all_summaries(max_recipes)
        group = await self.mealie._group()
        sem = asyncio.Semaphore(4)

        async def fetch(slug: str) -> dict[str, Any] | None:
            async with sem:
                try:
                    return await self.raw_recipe(slug)
                except MealieError as exc:
                    if exc.kind in ("auth_error", "connection_error"):
                        raise
                    return None

        need_detail = any(c in wanted for c in ("no_ingredients", "no_instructions", "no_nutrition", "unparsed_ingredients"))
        details = await asyncio.gather(*(fetch(s["slug"]) for s in summaries)) if need_detail else summaries
        issues: dict[str, list[dict[str, Any]]] = {c: [] for c in wanted}

        def ref(r: dict[str, Any], **extra: Any) -> dict[str, Any]:
            slug = r.get("slug")
            link = f"{self.base_url}/g/{group}/r/{slug}" if group else f"{self.base_url}/recipe/{slug}"
            return {"name": r.get("name"), "slug": slug, "mealie_url": link, **extra}

        for r in details:
            if not r:
                continue
            ingredients = r.get("recipeIngredient")
            checks_map = {
                "no_image": not r.get("image"),
                "no_servings": not (r.get("recipeServings") or r.get("recipeYieldQuantity") or r.get("recipeYield")),
                "no_time": not (r.get("totalTime") or r.get("prepTime") or r.get("cookTime") or r.get("performTime")),
                "no_tags": not r.get("tags"),
                "no_categories": not r.get("recipeCategory"),
                "no_source": not r.get("orgURL"),
            }
            if need_detail:
                checks_map["no_ingredients"] = not [i for i in ingredients or [] if ingredient_text(i)]
                checks_map["no_instructions"] = not [
                    s for s in r.get("recipeInstructions") or [] if (s.get("text") or "").strip()
                ]
                checks_map["no_nutrition"] = not any((r.get("nutrition") or {}).values())
            for check, failed in checks_map.items():
                if check in issues and failed:
                    issues[check].append(ref(r))
            if "unparsed_ingredients" in issues and ingredients:
                unparsed = sum(1 for i in ingredients if not i.get("food") and ingredient_text(i))
                if unparsed:
                    issues["unparsed_ingredients"].append(ref(r, unparsed=unparsed, total=len(ingredients)))

        if "possible_duplicates" in issues:
            groups: dict[str, list[dict[str, Any]]] = {}
            for r in summaries:
                keys = {f"name:{_norm_name(r.get('name') or '')}"}
                org = r.get("orgURL") or ""
                if org:
                    keys.add(f"src:{hellofresh_id(org) or org.rstrip('/').lower()}")
                for key in keys:
                    groups.setdefault(key, []).append(r)
            seen: set[frozenset[str]] = set()
            for key, members in groups.items():
                slugs = frozenset(m["slug"] for m in members)
                if len(slugs) > 1 and slugs not in seen:
                    seen.add(slugs)
                    issues["possible_duplicates"].append(
                        {"reason": "same source" if key.startswith("src:") else "same name", "recipes": [ref(m) for m in members]}
                    )

        return {
            "recipes_checked": len(summaries),
            "counts": {c: len(v) for c, v in issues.items()},
            "issues": {c: v[:limit_per_issue] for c, v in issues.items() if v},
            "truncated": any(len(v) > limit_per_issue for v in issues.values()),
        }

    # ======================================================= organizers
    async def recipes_with_ingredients(
        self,
        *,
        slugs: list[str] | None,
        search: str | None,
        tags: list[str] | None,
        categories: list[str] | None,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        """Many recipes with their ingredient lists in one call (for planning and costing)."""
        if slugs:
            chosen = [{"slug": _clean_slug(s)} for s in slugs[:per_page]]
            total, total_pages = len(slugs), 1
        else:
            params: dict[str, Any] = {"page": page, "perPage": per_page, "orderBy": "name", "orderDirection": "asc"}
            if search:
                params["search"] = search
            filters = []
            if tags:
                filters.append(f"tags.name IN [{', '.join(json.dumps(t) for t in tags)}]")
            if categories:
                filters.append(f"recipeCategory.name IN [{', '.join(json.dumps(c) for c in categories)}]")
            if filters:
                params["queryFilter"] = " AND ".join(filters)
            data = await self._req("GET", "/api/recipes", params=params)
            chosen = data.get("items", [])
            total = data.get("total", len(chosen))
            total_pages = _get(data, "total_pages", "totalPages", default=1)
        group = await self.mealie._group()
        sem = asyncio.Semaphore(4)

        async def one(slug: str) -> dict[str, Any]:
            async with sem:
                try:
                    r = await self.raw_recipe(slug)
                except MealieError as exc:
                    if exc.kind in ("auth_error", "connection_error"):
                        raise
                    return {"slug": slug, "error": str(exc)}
            ingredients = []
            for ing in r.get("recipeIngredient") or []:
                text = ingredient_text(ing)
                if not text:
                    continue
                if ing.get("title"):
                    ingredients.append(f"## {ing['title']}")
                ingredients.append(text)
            link = f"{self.base_url}/g/{group}/r/{r.get('slug')}" if group else f"{self.base_url}/recipe/{r.get('slug')}"
            return {
                "name": r.get("name"),
                "slug": r.get("slug"),
                "servings": r.get("recipeServings") or None,
                "total_time": r.get("totalTime") or None,
                "tags": [t.get("name") for t in r.get("tags") or []],
                "categories": [c.get("name") for c in r.get("recipeCategory") or []],
                "ingredients": ingredients,
                "mealie_url": link,
            }

        recipes = await asyncio.gather(*(one(c["slug"]) for c in chosen))
        return {"page": page, "total": total, "total_pages": total_pages, "recipes": list(recipes)}

    async def list_organizers(self, kind: str) -> list[dict[str, Any]]:
        data = await self._req(
            "GET", f"/api/organizers/{kind}", params={"perPage": -1, "orderBy": "name", "orderDirection": "asc"}
        )
        return [{"name": i.get("name"), "slug": i.get("slug")} for i in data.get("items", [])]

    async def suggest(
        self, *, count: int, tags: list[str] | None, categories: list[str] | None, exclude_days: int
    ) -> list[dict[str, Any]]:
        filters = []
        if tags:
            names = ", ".join(json.dumps(t) for t in tags)
            filters.append(f"tags.name IN [{names}]")
        if categories:
            names = ", ".join(json.dumps(c) for c in categories)
            filters.append(f"recipeCategory.name IN [{names}]")
        exclude: set[str] = set()
        if exclude_days > 0:
            today = dt.date.today()
            plan = await self.meal_plan(today - dt.timedelta(days=exclude_days), today + dt.timedelta(days=14))
            exclude = {(e.get("recipe") or {}).get("slug") for e in plan if e.get("recipe")}
        params: dict[str, Any] = {
            "perPage": min(MAX_PER_PAGE, count + len(exclude) + 5),
            "orderBy": "random",
            "paginationSeed": str(time.time_ns()),
        }
        if filters:
            params["queryFilter"] = " AND ".join(filters)
        data = await self._req("GET", "/api/recipes", params=params)
        group = await self.mealie._group()
        picks = [self.mealie._summary(i, group) for i in data.get("items", []) if i.get("slug") not in exclude]
        return [{k: p[k] for k in ("name", "slug", "total_time", "servings", "mealie_url")} for p in picks[:count]]

    # ======================================================== meal plans
    async def meal_plan(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        data = await self._req(
            "GET",
            "/api/households/mealplans",
            params={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "perPage": -1,
                "orderBy": "date",
                "orderDirection": "asc",
            },
        )
        group = await self.mealie._group()
        return [_plan_entry(i, self.base_url, group) for i in data.get("items", [])]

    async def _recipe_id(self, slug: str) -> tuple[str, str]:
        r = await self.raw_recipe(slug)
        return r["id"], r.get("name") or slug

    async def add_plan_entries(self, entries: list[dict[str, Any]], *, dry_run: bool) -> list[dict[str, Any]]:
        results = []
        group = await self.mealie._group()
        for e in entries:
            body: dict[str, Any] = {
                "date": e["date"],
                "entryType": e.get("meal") or "dinner",
                "title": e.get("title") or "",
                "text": e.get("note") or "",
            }
            label = e.get("title") or ""
            try:
                if e.get("recipe_slug"):
                    body["recipeId"], label = await self._recipe_id(e["recipe_slug"])
                elif not body["title"]:
                    results.append({"status": "failed", "date": e["date"], "message": "Give recipe_slug or title."})
                    continue
                if dry_run:
                    results.append({"status": "dry_run", "date": e["date"], "meal": body["entryType"], "recipe": label})
                    continue
                created = await self._req("POST", "/api/households/mealplans", json=body)
                results.append({"status": "added", **_plan_entry(created, self.base_url, group)})
            except MealieError as exc:
                results.append({"status": "failed", "date": e["date"], "message": str(exc)})
        return results

    async def update_plan_entry(self, entry_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        current = await self._req("GET", f"/api/households/mealplans/{entry_id}")
        body = {k: current.get(k) for k in ("id", "date", "entryType", "title", "text", "recipeId", "groupId", "userId")}
        if changes.get("date"):
            body["date"] = changes["date"]
        if changes.get("meal"):
            body["entryType"] = changes["meal"]
        if changes.get("recipe_slug"):
            body["recipeId"], _ = await self._recipe_id(changes["recipe_slug"])
        if changes.get("title") is not None:
            body["title"] = changes["title"]
        if changes.get("note") is not None:
            body["text"] = changes["note"]
        updated = await self._req("PUT", f"/api/households/mealplans/{entry_id}", json=body)
        return _plan_entry(updated, self.base_url, await self.mealie._group())

    async def remove_plan_entries(self, ids: list[int]) -> list[dict[str, Any]]:
        out = []
        for entry_id in ids:
            try:
                await self._req("DELETE", f"/api/households/mealplans/{int(entry_id)}")
                out.append({"id": entry_id, "status": "removed"})
            except MealieError as exc:
                out.append({"id": entry_id, "status": "failed", "message": str(exc)})
        return out

    # ===================================================== shopping lists
    async def shopping_lists(self) -> list[dict[str, Any]]:
        data = await self._req(
            "GET", "/api/households/shopping/lists", params={"perPage": -1, "orderBy": "createdAt", "orderDirection": "desc"}
        )
        return [
            {
                "id": i.get("id"),
                "name": i.get("name"),
                "recipes": [((r.get("recipe") or {}).get("name")) for r in i.get("recipeReferences") or []],
            }
            for i in data.get("items", [])
        ]

    async def shopping_list(self, list_id: str, *, include_checked: bool) -> dict[str, Any]:
        data = await self._req("GET", f"/api/households/shopping/lists/{_uuid(list_id)}")
        names = {
            r["recipeId"]: (r.get("recipe") or {}).get("name") for r in data.get("recipeReferences") or [] if r.get("recipeId")
        }
        items = [_shopping_item(i, names) for i in data.get("listItems") or []]
        unchecked = [i for i in items if not i["checked"]]
        return {
            "id": data.get("id"),
            "name": data.get("name"),
            "recipes": [
                {
                    "name": (r.get("recipe") or {}).get("name"),
                    "slug": (r.get("recipe") or {}).get("slug"),
                    "times_added": r.get("recipeQuantity"),
                }
                for r in data.get("recipeReferences") or []
            ],
            "to_buy": unchecked,
            "checked": [i for i in items if i["checked"]] if include_checked else None,
            "checked_count": len(items) - len(unchecked),
        }

    async def create_shopping_list(self, name: str) -> dict[str, Any]:
        data = await self._req("POST", "/api/households/shopping/lists", json={"name": name.strip()})
        return {"id": data.get("id"), "name": data.get("name")}

    async def add_recipes_to_list(self, list_id: str, recipes: list[dict[str, Any]]) -> dict[str, Any]:
        body = []
        names = []
        for r in recipes:
            recipe_id, name = await self._recipe_id(r["slug"])
            body.append({"recipeId": recipe_id, "recipeIncrementQuantity": r.get("scale") or 1})
            names.append(name)
        await self._req("POST", f"/api/households/shopping/lists/{_uuid(list_id)}/recipe", json=body)
        return {"added_recipes": names, "list": await self.shopping_list(list_id, include_checked=False)}

    async def add_items(self, list_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        lid = _uuid(list_id)
        body = [
            {"shoppingListId": lid, "note": i["item"].strip(), "quantity": i.get("quantity") or 0, "checked": False}
            for i in items
            if (i.get("item") or "").strip()
        ]
        await self._req("POST", "/api/households/shopping/items/create-bulk", json=body)
        return {"added": len(body), "list": await self.shopping_list(lid, include_checked=False)}

    async def set_checked(self, list_id: str, item_ids: list[str], checked: bool) -> dict[str, Any]:
        data = await self._req("GET", f"/api/households/shopping/lists/{_uuid(list_id)}")
        wanted = {_uuid(i) for i in item_ids}
        items = [i for i in data.get("listItems") or [] if i.get("id") in wanted]
        if items:
            for i in items:
                i["checked"] = checked
            await self._req("PUT", "/api/households/shopping/items", json=items)
        return {"updated": len(items), "not_found": sorted(wanted - {i["id"] for i in items}) or None}

    async def remove_items(self, item_ids: list[str]) -> dict[str, Any]:
        ids = [_uuid(i) for i in item_ids]
        await self._req("DELETE", "/api/households/shopping/items", params=[("ids", i) for i in ids])
        return {"removed": len(ids)}

    async def remove_recipe_from_list(self, list_id: str, slug: str) -> dict[str, Any]:
        recipe_id, name = await self._recipe_id(slug)
        await self._req(
            "POST",
            f"/api/households/shopping/lists/{_uuid(list_id)}/recipe/{recipe_id}/delete",
            json={"recipeDecrementQuantity": 1},
        )
        return {"removed_recipe": name, "list": await self.shopping_list(list_id, include_checked=False)}


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


def _uuid(value: str) -> str:
    value = (value or "").strip()
    if not UUID_RE.match(value):
        raise MealieError(f"Invalid id: {value[:40]!r}", kind="invalid_input")
    return value


def _types(node: dict[str, Any]) -> list[str]:
    t = node.get("@type")
    return t if isinstance(t, list) else [t] if isinstance(t, str) else []


def _walk_jsonld(data: Any):
    if isinstance(data, list):
        for item in data:
            yield from _walk_jsonld(item)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "mainEntity"):
            if key in data:
                yield from _walk_jsonld(data[key])


def _short(value: Any) -> Any:
    """Compact display of a field value for diffs."""
    if isinstance(value, list):
        out = []
        for v in value:
            if isinstance(v, dict):
                out.append(v.get("name") or v.get("display") or v.get("note") or v.get("text") or v.get("originalText"))
            else:
                out.append(v)
        return out
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if v not in (None, "")}
    return value


def _diff(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    diff = {}
    for key, new in patch.items():
        old_s, new_s = _short(current.get(key)), _short(new)
        if old_s != new_s and not (old_s in (None, "", [], {}) and new_s in (None, "", [], {})):
            diff[key] = {"from": old_s, "to": new_s}
    return diff
