"""FastMCP server exposing Mealie + HelloFresh tools over Streamable HTTP."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .auth import OwnerOAuthProvider
from .config import Settings
from .hellofresh import HelloFreshClient, HelloFreshError, title_core
from .logging_setup import log
from .mealie import MealieClient, MealieError
from .net import OutboundBlockedError, UrlValidationError, validate_hellofresh_url, validate_recipe_url

logger = logging.getLogger("mealie_mcp.server")

INSTRUCTIONS = """\
Tools for the user's self-hosted Mealie recipe manager, plus HelloFresh UK lookup and import.
Works with any MCP client (ChatGPT, Claude, ...).

Typical workflow for HelloFresh recipe cards (photos or names):
1. Read the recipe title AND its subtitle (the "with ..." line) from the card.
2. Call hellofresh_import with name + subtitle, dry_run=true first if the user wants to
   review matches. For several recipes use hellofresh_bulk_import.
3. Only a verified exact match is imported. If the result is "ambiguous" or
   "not_found", show the alternatives to the user and do NOT guess. If you find the
   recipe URL yourself (e.g. via web search), pass it as `url` and it will be verified.
   For many recipes, send batches of about 5 per hellofresh_bulk_import call.
4. Existing recipes are never modified or deleted; duplicates are reported and skipped.
Report imported, duplicate, not-found and failed recipes back to the user.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
READ_ONLY_WEB = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
IMPORTS = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


class RecipeCard(BaseModel):
    name: Annotated[str, Field(max_length=200, description="Recipe title from the card")] = ""
    subtitle: Annotated[str | None, Field(max_length=300, description="The 'with ...' line from the card")] = None
    url: Annotated[str | None, Field(max_length=2048, description="hellofresh.co.uk recipe URL, if known")] = None


def _safe(fn):
    """Turn expected failures into structured tool results; never leak internals."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except MealieError as exc:
            log(logger, logging.WARNING, "mealie error", tool=fn.__name__, kind=exc.kind, status=exc.status)
            return {"ok": False, **exc.to_dict()}
        except HelloFreshError as exc:
            log(logger, logging.WARNING, "hellofresh error", tool=fn.__name__, kind=exc.kind)
            return {"ok": False, "error": exc.kind, "message": str(exc)}
        except UrlValidationError as exc:
            return {"ok": False, "error": "invalid_url", "message": str(exc)}
        except OutboundBlockedError as exc:
            log(logger, logging.WARNING, "outbound blocked", tool=fn.__name__, detail=str(exc))
            return {"ok": False, "error": "blocked", "message": "Request blocked by the server's outbound allowlist."}
        except Exception:
            logger.exception("unexpected tool error in %s", fn.__name__)
            return {"ok": False, "error": "internal_error", "message": "Unexpected server error; see server logs."}

    return wrapper


def build_server(
    settings: Settings,
    *,
    mealie: MealieClient | None = None,
    hellofresh: HelloFreshClient | None = None,
) -> FastMCP:
    mealie = mealie or MealieClient(
        settings.mealie_url,
        settings.mealie_api_token.get_secret_value(),
        timeout=settings.mealie_timeout_seconds,
        import_timeout=settings.mealie_import_timeout_seconds,
        verify_tls=settings.mealie_verify_tls,
        include_tags=settings.mealie_import_include_tags,
        include_categories=settings.mealie_import_include_categories,
    )
    hellofresh = hellofresh or HelloFreshClient(
        settings.hellofresh_base_url,
        country=settings.hellofresh_country,
        locale=settings.hellofresh_locale,
        timeout=settings.hellofresh_timeout_seconds,
        min_interval=settings.hellofresh_min_interval_seconds,
    )

    auth = None
    if settings.auth_mode == "oauth":
        auth = OwnerOAuthProvider(
            base_url=settings.public_base_url,
            resource_url=settings.resource_url,
            owner_password=settings.owner_password.get_secret_value(),  # type: ignore[union-attr]
            secret=settings.auth_secret.get_secret_value(),  # type: ignore[union-attr]
            allowed_redirect_hosts=settings.oauth_allowed_redirect_hosts,
            allow_loopback_redirects=settings.oauth_allow_loopback_redirects,
            data_dir=settings.data_dir,
            access_ttl=settings.access_token_ttl_seconds,
            refresh_ttl=settings.refresh_token_ttl_seconds,
            static_tokens=[t.get_secret_value() for t in settings.static_tokens],
        )
    else:
        log(logger, logging.WARNING, "AUTH_MODE=none: MCP endpoint is UNAUTHENTICATED. Never expose this to the internet.")

    mcp = FastMCP(name="Mealie", instructions=INSTRUCTIONS, auth=auth, version=__version__)
    mcp.add_middleware(
        RateLimitingMiddleware(
            max_requests_per_second=settings.rate_limit_per_second,
            burst_capacity=settings.rate_limit_burst,
            global_limit=True,
        )
    )
    mcp.state = {"mealie": mealie, "hellofresh": hellofresh}  # type: ignore[attr-defined]

    def effective_dry_run(dry_run: bool) -> bool:
        return dry_run or settings.dry_run

    async def check_import_url(url: str) -> str:
        return await validate_recipe_url(url, settings.import_allowed_domains)

    # ---------------------------------------------------------------- Mealie
    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def mealie_status() -> dict[str, Any]:
        """Check that the Mealie server is reachable and the API token is valid. Returns Mealie version and user."""
        return {"ok": True, **(await mealie.status()), "dry_run_forced": settings.dry_run}

    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def mealie_list_recipes(
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1")] = 1,
        per_page: Annotated[int, Field(ge=1, le=100, description="Recipes per page (max 100)")] = 25,
        order_by: Annotated[str, Field(description="Sort field: dateAdded, name, rating, updatedAt")] = "dateAdded",
    ) -> dict[str, Any]:
        """List recipes in the Mealie library, newest first by default."""
        if order_by not in ("dateAdded", "name", "rating", "updatedAt", "createdAt", "lastMade"):
            order_by = "dateAdded"
        return {"ok": True, **(await mealie.list_recipes(page=page, per_page=per_page, order_by=order_by))}

    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def mealie_search_recipes(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Text to search for")],
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """Search the Mealie library by recipe name, description or ingredients."""
        return {
            "ok": True,
            "query": query,
            **(await mealie.list_recipes(page=page, per_page=per_page, search=query, order_by=None)),
        }

    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def mealie_get_recipe(
        slug: Annotated[str, Field(min_length=1, max_length=200, description="Recipe slug (or id) from list/search results")],
    ) -> dict[str, Any]:
        """Get the full details of one Mealie recipe: ingredients, instructions, times, servings, nutrition."""
        return {"ok": True, "recipe": await mealie.get_recipe(slug)}

    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def mealie_check_duplicate(
        url: Annotated[str | None, Field(description="Source recipe URL to look for")] = None,
        name: Annotated[str | None, Field(description="Recipe name to look for (exact, case-insensitive)")] = None,
    ) -> dict[str, Any]:
        """Check whether a recipe already exists in Mealie, by source URL and/or exact name."""
        if not url and not name:
            return {"ok": False, "error": "invalid_input", "message": "Provide url and/or name."}
        matches = await mealie.find_duplicates(url=url, name=name)
        return {"ok": True, "exists": bool(matches), "matches": matches}

    @mcp.tool(annotations=IMPORTS)
    @_safe
    async def mealie_import_url(
        url: Annotated[str, Field(description="https URL of the recipe page to import")],
        dry_run: Annotated[bool, Field(description="Preview what Mealie would import without saving")] = False,
        skip_duplicates: Annotated[bool, Field(description="Skip if the recipe already exists (recommended)")] = True,
    ) -> dict[str, Any]:
        """Import a recipe into Mealie from its web page URL using Mealie's built-in scraper.

        The URL must be on an allowed recipe domain. Existing recipes are never modified.
        """
        clean = await check_import_url(url)
        outcome = await mealie.import_url(clean, dry_run=effective_dry_run(dry_run), skip_duplicates=skip_duplicates)
        return {"ok": outcome.status in ("imported", "duplicate", "dry_run"), **outcome.to_dict()}

    @mcp.tool(annotations=IMPORTS)
    @_safe
    async def mealie_bulk_import(
        urls: Annotated[list[str], Field(min_length=1, description="Recipe page URLs to import")],
        dry_run: Annotated[bool, Field(description="Preview only; nothing is saved")] = False,
        skip_duplicates: bool = True,
    ) -> dict[str, Any]:
        """Import several recipe URLs into Mealie. Returns a per-URL result and a summary.

        For large lists, call in batches of about 10 to keep each call short.
        """
        if len(urls) > settings.bulk_import_max:
            return {"ok": False, "error": "too_many", "message": f"At most {settings.bulk_import_max} URLs per call."}
        seen: set[str] = set()
        results: list[dict[str, Any]] = []

        async def one(raw: str) -> dict[str, Any]:
            try:
                clean = await check_import_url(raw)
            except UrlValidationError as exc:
                return {"status": "invalid_url", "url": raw, "message": str(exc)}
            try:
                return (
                    await mealie.import_url(clean, dry_run=effective_dry_run(dry_run), skip_duplicates=skip_duplicates)
                ).to_dict()
            except MealieError as exc:
                return {"status": "failed", "url": raw, "message": str(exc)}

        for raw in urls:
            key = raw.strip()
            if key in seen:
                results.append({"status": "duplicate_in_request", "url": raw})
                continue
            seen.add(key)
            results.append(await one(raw))
        return {"ok": True, "summary": _summarise(results), "results": results}

    # ------------------------------------------------------------ HelloFresh
    @mcp.tool(annotations=READ_ONLY_WEB)
    @_safe
    async def hellofresh_search(
        query: Annotated[str, Field(min_length=2, max_length=120, description="Recipe name to search for")],
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> dict[str, Any]:
        """Search the HelloFresh UK recipe catalogue by name. Results are unverified search hits."""
        candidates, strategies, errors = await hellofresh.search(query, limit)
        return {
            "ok": bool(strategies),
            "query": query,
            "results": [c.to_dict() for c in candidates],
            "search_strategies": strategies,
            "errors": errors or None,
        }

    @mcp.tool(annotations=READ_ONLY_WEB)
    @_safe
    async def hellofresh_match(
        name: Annotated[str, Field(max_length=200, description="Recipe title, e.g. 'Curried Chicken Thigh Traybake'")] = "",
        subtitle: Annotated[
            str | None, Field(max_length=300, description="The 'with ...' line under the title, if known")
        ] = None,
        candidate_urls: Annotated[
            list[str] | None, Field(max_length=10, description="HelloFresh URLs to verify instead of searching")
        ] = None,
        include_details: Annotated[
            bool, Field(description="Include ingredients, instructions and nutrition of the match")
        ] = True,
    ) -> dict[str, Any]:
        """Find and verify the HelloFresh UK recipe that exactly matches a title (and subtitle).

        Only an exact, verified name match is returned as `match`; similar recipes are `alternatives`.
        """
        result = await hellofresh.match(name, subtitle, candidate_urls=candidate_urls, include_details=include_details)
        return {"ok": result.status == "matched", **result.to_dict()}

    async def _hf_import_one(name: str, subtitle: str | None, url: str | None, dry_run: bool) -> dict[str, Any]:
        if url:
            url = validate_hellofresh_url(url, settings.hellofresh_base_url)
        result = await hellofresh.match(name, subtitle, candidate_urls=[url] if url else None)
        out = result.to_dict()
        if result.status != "matched" or not result.match:
            out["import_status"] = "not_imported"
            return out
        target = result.match.url
        clean = await check_import_url(target)
        outcome = await mealie.import_url(clean, dry_run=effective_dry_run(dry_run), expected_name=_names_for(result.match, name))
        out["import_status"] = outcome.status
        out["import"] = outcome.to_dict()
        return out

    @mcp.tool(annotations=IMPORTS)
    @_safe
    async def hellofresh_import(
        name: Annotated[str, Field(max_length=200, description="Recipe title from the card")] = "",
        subtitle: Annotated[
            str | None, Field(max_length=300, description="The 'with ...' line from the card (strongly recommended)")
        ] = None,
        url: Annotated[str | None, Field(description="Optional hellofresh.co.uk recipe URL, if already known")] = None,
        dry_run: Annotated[bool, Field(description="Find, verify and duplicate-check only; do not import")] = False,
    ) -> dict[str, Any]:
        """Find a HelloFresh UK recipe by name, verify it is an exact match, check for duplicates and import it into Mealie."""
        if not name and not url:
            return {"ok": False, "error": "invalid_input", "message": "Provide the recipe name (and subtitle) or a URL."}
        out = await _hf_import_one(name, subtitle, url, dry_run)
        return {"ok": out.get("import_status") in ("imported", "duplicate", "dry_run"), **out}

    @mcp.tool(annotations=IMPORTS)
    @_safe
    async def hellofresh_bulk_import(
        recipes: Annotated[
            list[RecipeCard],
            Field(min_length=1, max_length=20, description="One entry per recipe card (max 20; batches of ~5 recommended)"),
        ],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Find, verify and import several HelloFresh UK recipes. Returns per-recipe status and a summary."""
        results = []
        for item in recipes:
            name = item.name.strip()
            try:
                r = await _hf_import_one(name, item.subtitle, item.url, dry_run)
            except (HelloFreshError, MealieError, UrlValidationError, OutboundBlockedError) as exc:
                r = {"status": "error", "query": {"name": name}, "message": str(exc), "import_status": "failed"}
            results.append(r)
        statuses = [r.get("import_status") if r.get("status") == "matched" else r.get("status") for r in results]
        summary = {s: statuses.count(s) for s in sorted({str(s) for s in statuses})}
        return {"ok": True, "summary": summary, "results": results}

    # --------------------------------------------------------------- health
    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    @mcp.custom_route("/health/ready", methods=["GET"])
    async def ready(request: Request) -> JSONResponse:
        try:
            await asyncio.wait_for(mealie.status(), timeout=10)
            return JSONResponse({"status": "ok", "mealie": "ok"})
        except (MealieError, TimeoutError, OutboundBlockedError):
            return JSONResponse({"status": "degraded", "mealie": "unavailable"}, status_code=503)

    return mcp


def _names_for(match: Any, requested: str) -> list[str]:
    """Names an existing copy of this recipe might be saved under in Mealie."""
    names = {match.name, requested}
    if match.headline:
        names.add(f"{title_core(match.name, match.headline)} {match.headline}")
        names.add(title_core(match.name, match.headline))
    return [n for n in names if n]


def _summarise(results: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for r in results:
        summary[r.get("status", "unknown")] = summary.get(r.get("status", "unknown"), 0) + 1
    return summary


def create_app(settings: Settings, **kwargs: Any):
    mcp = build_server(settings, **kwargs)
    from urllib.parse import urlsplit

    public_host = urlsplit(settings.public_base_url).netloc
    app = mcp.http_app(
        path=settings.mcp_path,
        stateless_http=True,
        json_response=settings.json_response,
        allowed_hosts=[public_host, "localhost", f"localhost:{settings.port}", "127.0.0.1", f"127.0.0.1:{settings.port}"],
    )
    app.state.mcp = mcp
    return app
