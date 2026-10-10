"""Async client for the Mealie REST API (reads and imports; editing lives in manage.py)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .logging_setup import log
from .net import allowlisted_client, canonicalize_url, hellofresh_id

logger = logging.getLogger("mealie_mcp.mealie")

RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_PER_PAGE = 100


class MealieError(Exception):
    def __init__(self, message: str, status: int | None = None, kind: str = "api_error") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.kind, "message": str(self), "http_status": self.status}


@dataclass
class ImportOutcome:
    status: str  # imported | duplicate | failed | dry_run
    url: str
    slug: str | None = None
    message: str | None = None
    recipe: dict[str, Any] | None = None
    duplicates: list[dict[str, Any]] | None = None
    warnings: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or response.reason_phrase
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)[:300]
    return str(detail)[:300]


def _trim(text: str | None, limit: int = 300) -> str | None:
    if not text:
        return text
    return text if len(text) <= limit else text[: limit - 1] + "…"


class MealieClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        import_timeout: float = 120.0,
        verify_tls: bool = True,
        include_tags: bool = True,
        include_categories: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.import_timeout = import_timeout
        self.include_tags = include_tags
        self.include_categories = include_categories
        self.max_retries = max_retries
        host = urlsplit(self.base_url).hostname or ""
        self._client = allowlisted_client(
            [host],
            timeout=timeout,
            verify=verify_tls,
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            transport=transport,
        )
        self._group_slug: str | None = None
        self._import_lock = asyncio.Semaphore(2)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core
    async def _request(self, method: str, path: str, *, retry: bool = True, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                raise MealieError(f"Mealie did not respond in time ({method} {path})", kind="timeout") from exc
            except httpx.ConnectError as exc:
                if retry and attempt < self.max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))
                    continue
                raise MealieError("Could not connect to Mealie", kind="connection_error") from exc
            except httpx.HTTPError as exc:
                raise MealieError(f"HTTP error talking to Mealie: {type(exc).__name__}", kind="connection_error") from exc

            if response.status_code in RETRYABLE_STATUS and retry and attempt < self.max_retries:
                delay = 2 ** (attempt - 1)
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = min(int(retry_after), 30)
                log(logger, logging.WARNING, "mealie retry", status=response.status_code, path=path, delay=delay)
                await asyncio.sleep(delay)
                continue
            if response.status_code in (401, 403):
                raise MealieError(
                    "Mealie rejected the API token (check MEALIE_API_TOKEN)", status=response.status_code, kind="auth_error"
                )
            if response.status_code == 404:
                raise MealieError("Not found in Mealie", status=404, kind="not_found")
            if response.status_code == 429:
                raise MealieError("Mealie is rate limiting requests; try again shortly", status=429, kind="rate_limited")
            if response.status_code >= 400:
                raise MealieError(_error_detail(response), status=response.status_code)
            return response

    # ---------------------------------------------------------------- status
    async def status(self) -> dict[str, Any]:
        about = (await self._request("GET", "/api/app/about")).json()
        me = (await self._request("GET", "/api/users/self")).json()
        self._group_slug = me.get("groupSlug") or self._group_slug
        return {
            "connected": True,
            "authenticated": True,
            "mealie_url": self.base_url,
            "mealie_version": about.get("version"),
            "user": me.get("username") or me.get("fullName"),
            "group": me.get("groupSlug") or me.get("group"),
            "household": me.get("householdSlug") or me.get("household"),
            "can_organize": me.get("canOrganize"),
        }

    async def _group(self) -> str | None:
        if self._group_slug is None:
            try:
                me = (await self._request("GET", "/api/users/self")).json()
                self._group_slug = me.get("groupSlug")
            except MealieError:
                return None
        return self._group_slug

    async def recipe_link(self, slug: str) -> str:
        group = await self._group()
        return f"{self.base_url}/g/{group}/r/{slug}" if group else f"{self.base_url}/recipe/{slug}"

    # ------------------------------------------------------------------ read
    def _summary(self, item: dict[str, Any], group: str | None) -> dict[str, Any]:
        slug = item.get("slug")
        return {
            "name": item.get("name"),
            "slug": slug,
            "id": item.get("id"),
            "description": _trim(item.get("description"), 200),
            "source_url": item.get("orgURL"),
            "total_time": item.get("totalTime"),
            "servings": item.get("recipeServings") or None,
            "rating": item.get("rating"),
            "date_added": item.get("dateAdded"),
            "mealie_url": f"{self.base_url}/g/{group}/r/{slug}" if group else f"{self.base_url}/recipe/{slug}",
        }

    async def list_recipes(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        search: str | None = None,
        order_by: str | None = "dateAdded",
        query_filter: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": max(1, page),
            "perPage": max(1, min(per_page, MAX_PER_PAGE)),
            "orderDirection": "desc",
        }
        if order_by:
            params["orderBy"] = order_by
        if search:
            params["search"] = search
        if query_filter:
            params["queryFilter"] = query_filter
        data = (await self._request("GET", "/api/recipes", params=params)).json()
        group = await self._group()
        return {
            "page": data.get("page", page),
            "per_page": _get(data, "per_page", "perPage", default=per_page),
            "total": data.get("total", 0),
            "total_pages": _get(data, "total_pages", "totalPages", default=0),
            "recipes": [self._summary(item, group) for item in data.get("items", [])],
        }

    async def get_recipe(self, slug: str) -> dict[str, Any]:
        if not slug or "/" in slug or len(slug) > 200:
            raise MealieError("Invalid recipe slug", kind="invalid_input")
        r = (await self._request("GET", f"/api/recipes/{slug}")).json()
        ingredients = []
        for ing in r.get("recipeIngredient") or []:
            text = ing.get("display") or ing.get("originalText") or ing.get("note") or ""
            if ing.get("title"):
                ingredients.append(f"## {ing['title']}")
            if text.strip():
                ingredients.append(text.strip())
        steps = [s.get("text", "").strip() for s in (r.get("recipeInstructions") or []) if s.get("text")]
        nutrition = {k: v for k, v in (r.get("nutrition") or {}).items() if v not in (None, "", "0")}
        recipe_id = r.get("id")
        return {
            "name": r.get("name"),
            "slug": r.get("slug"),
            "id": recipe_id,
            "description": r.get("description"),
            "source_url": r.get("orgURL"),
            "mealie_url": await self.recipe_link(r.get("slug", slug)),
            "servings": r.get("recipeServings") or None,
            "yield": r.get("recipeYield"),
            "total_time": r.get("totalTime"),
            "prep_time": r.get("prepTime"),
            "cook_time": r.get("cookTime") or r.get("performTime"),
            "ingredients": ingredients,
            "instructions": steps,
            "nutrition": nutrition or None,
            "tags": [t.get("name") for t in r.get("tags") or []],
            "categories": [c.get("name") for c in r.get("recipeCategory") or []],
            "image_url": f"{self.base_url}/api/media/recipes/{recipe_id}/images/original.webp"
            if recipe_id and r.get("image")
            else None,
            "date_added": r.get("dateAdded"),
        }

    # ------------------------------------------------------------ duplicates
    async def find_duplicates(self, *, url: str | None = None, name: str | list[str] | None = None) -> list[dict[str, Any]]:
        """Find recipes that already exist, by source URL (exact/HelloFresh id) and by exact name."""
        found: dict[str, dict[str, Any]] = {}
        group = await self._group()

        if url:
            canonical = canonicalize_url(url)
            hf_id = hellofresh_id(url)
            needle = hf_id or urlsplit(canonical).path.rsplit("/", 1)[-1] or canonical
            needle = needle.replace('"', "")
            items: list[dict[str, Any]] = []
            try:
                data = (
                    await self._request("GET", "/api/recipes", params={"queryFilter": f'orgUrl LIKE "%{needle}%"', "perPage": 50})
                ).json()
                items = data.get("items", [])
            except MealieError as exc:
                if exc.kind in ("auth_error", "connection_error", "timeout"):
                    raise
                # Older Mealie without query filters: fall back to scanning by search term.
                items = await self._scan_for_url(needle)
            for item in items:
                org = item.get("orgURL") or ""
                same = canonicalize_url(org) == canonical if org else False
                if hf_id and hellofresh_id(org) == hf_id:
                    same = True
                if same:
                    found[item["slug"]] = {**self._summary(item, group), "match": "source_url"}

        names = [name] if isinstance(name, str) else [n for n in (name or []) if n]
        if names:
            targets = {_norm_name(n) for n in names}
            # The shortest name is the broadest search; Mealie's search matches substrings.
            query = min(names, key=len)
            data = (await self._request("GET", "/api/recipes", params={"search": query, "perPage": 50})).json()
            for item in data.get("items", []):
                if _norm_name(item.get("name") or "") in targets and item["slug"] not in found:
                    found[item["slug"]] = {**self._summary(item, group), "match": "same_name"}
        return list(found.values())

    async def _scan_for_url(self, needle: str, max_pages: int = 20) -> list[dict[str, Any]]:
        matches = []
        for page in range(1, max_pages + 1):
            data = (await self._request("GET", "/api/recipes", params={"page": page, "perPage": MAX_PER_PAGE})).json()
            matches += [i for i in data.get("items", []) if needle in (i.get("orgURL") or "")]
            if page >= _get(data, "total_pages", "totalPages", default=0):
                break
        return matches

    # --------------------------------------------------------------- imports
    async def test_scrape(self, url: str) -> dict[str, Any]:
        """Ask Mealie to scrape the URL without saving anything (dry run)."""
        response = await self._request(
            "POST", "/api/recipes/test-scrape-url", json={"url": url}, timeout=self.import_timeout, retry=False
        )
        data = response.json()
        if isinstance(data, str):
            raise MealieError(f"Mealie could not scrape this URL: {data}", kind="scrape_failed")
        ingredients = data.get("recipeIngredient") or []
        instructions = data.get("recipeInstructions") or []
        return {
            "name": data.get("name"),
            "description": _trim(data.get("description")),
            "ingredient_count": len(ingredients),
            "instruction_count": len(instructions) if isinstance(instructions, list) else 1,
            "total_time": data.get("totalTime"),
            "servings": data.get("recipeYield"),
            "has_nutrition": bool(data.get("nutrition")),
            "has_image": bool(data.get("image")),
        }

    async def create_from_schema(self, recipe: dict[str, Any]) -> str:
        """Create a recipe from a schema.org Recipe object (e.g. transcribed from a cookbook photo)."""
        payload = {"data": json.dumps(recipe), "includeTags": True, "includeCategories": self.include_categories}
        async with self._import_lock:
            try:
                response = await self._request(
                    "POST", "/api/recipes/create/html-or-json", json=payload, timeout=self.import_timeout, retry=False
                )
            except MealieError as exc:
                if exc.status == 400:
                    exc.kind = "create_failed"
                raise
        slug = response.json()
        if not isinstance(slug, str) or not slug:
            raise MealieError("Mealie returned an unexpected response to the create request", kind="unexpected_response")
        log(logger, logging.INFO, "recipe created from data", slug=slug)
        return slug

    async def create_from_url(self, url: str) -> str:
        payload = {"url": url, "includeTags": self.include_tags, "includeCategories": self.include_categories}
        async with self._import_lock:
            try:
                response = await self._request(
                    "POST", "/api/recipes/create/url", json=payload, timeout=self.import_timeout, retry=False
                )
            except MealieError as exc:
                if exc.status == 400:
                    exc.kind = "scrape_failed"
                raise
        slug = response.json()
        if not isinstance(slug, str) or not slug:
            raise MealieError("Mealie returned an unexpected response to the import", kind="unexpected_response")
        return slug

    async def import_url(
        self,
        url: str,
        *,
        dry_run: bool = False,
        skip_duplicates: bool = True,
        expected_name: str | list[str] | None = None,
    ) -> ImportOutcome:
        duplicates = await self.find_duplicates(url=url, name=expected_name)
        if duplicates and skip_duplicates:
            return ImportOutcome(
                status="duplicate",
                url=url,
                duplicates=duplicates,
                message="Recipe already exists in Mealie; not imported (no existing recipe was changed).",
            )
        if dry_run:
            try:
                preview = await self.test_scrape(url)
            except MealieError as exc:
                return ImportOutcome(status="failed", url=url, message=str(exc), duplicates=duplicates or None)
            return ImportOutcome(
                status="dry_run",
                url=url,
                recipe=preview,
                duplicates=duplicates or None,
                message="Dry run: nothing was imported.",
                warnings=_completeness_warnings(preview) or None,
            )
        try:
            slug = await self.create_from_url(url)
        except MealieError as exc:
            if exc.kind == "timeout":
                # The import may have finished server-side; check before reporting failure.
                await asyncio.sleep(2)
                after = await self.find_duplicates(url=url)
                if after:
                    slug = after[0]["slug"]
                    log(logger, logging.INFO, "import completed after client timeout", slug=slug)
                else:
                    return ImportOutcome(status="failed", url=url, message=str(exc))
            else:
                return ImportOutcome(status="failed", url=url, message=str(exc))
        log(logger, logging.INFO, "recipe imported", slug=slug, source=canonicalize_url(url))
        try:
            recipe = await self.get_recipe(slug)
        except MealieError:
            recipe = {"slug": slug, "mealie_url": await self.recipe_link(slug)}
        summary = {
            "name": recipe.get("name"),
            "slug": slug,
            "mealie_url": recipe.get("mealie_url"),
            "ingredient_count": len(recipe.get("ingredients") or []),
            "instruction_count": len(recipe.get("instructions") or []),
            "total_time": recipe.get("total_time"),
            "servings": recipe.get("servings"),
            "has_nutrition": bool(recipe.get("nutrition")),
            "has_image": bool(recipe.get("image_url")),
        }
        return ImportOutcome(
            status="imported",
            url=url,
            slug=slug,
            recipe=summary,
            message="Imported successfully.",
            warnings=_completeness_warnings(summary) or None,
        )


def _get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Mealie's pagination fields are snake_case; accept camelCase too."""
    for key in keys:
        if key in data:
            return data[key]
    return default


def _completeness_warnings(summary: dict[str, Any]) -> list[str]:
    warnings = []
    if not summary.get("ingredient_count"):
        warnings.append("no ingredients were captured")
    if not summary.get("instruction_count"):
        warnings.append("no instructions were captured")
    if not summary.get("total_time"):
        warnings.append("no cooking time was captured")
    if not summary.get("has_image"):
        warnings.append("no image was captured")
    return warnings


def _norm_name(name: str) -> str:
    # Mealie suffixes repeated names with " (1)", " (2)" ...
    name = re.sub(r"\s*\(\d+\)\s*$", "", name.strip().lower()).replace("&", " and ")
    name = re.sub(r"[^a-z0-9 ]+", " ", name)
    return " ".join(name.split())
