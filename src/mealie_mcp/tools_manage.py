"""MCP tools for managing the Mealie library: audit, edit, images, meal plans, shopping lists."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .config import Settings
from .manage import AUDIT_CHECKS, Manager

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
EDIT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
EDIT_WEB = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
ADD = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
REMOVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

Slug = Annotated[str, Field(min_length=1, max_length=200, description="Recipe slug from list/search results")]
Meal = Literal["breakfast", "lunch", "dinner", "side"]

MANAGE_INSTRUCTIONS = """\
Managing the library (edits, images, meal plans, shopping lists):
- Before changing a recipe, show the user what will change (dry_run=true returns a diff) unless they
  already asked for that exact change. Every recipe edit saves a backup first; tell the user the
  backup_id. mealie_restore_recipe undoes a change. Recipes are never deleted.
- mealie_audit_recipes finds gaps (no image, no tags, unparsed ingredients, duplicates ...). Fix them with
  mealie_update_recipe, mealie_set_recipe_image and mealie_parse_ingredients.
- Weekly plans: mealie_get_recipes_with_ingredients reads many recipes' ingredients at once (use it to apply the
  user's criteria, e.g. proteins or an estimated budget), or mealie_suggest_recipes for random picks; then
  mealie_add_meal_plan_entries. Shopping: mealie_add_meal_plan_to_shopping_list collects every planned
  recipe's ingredients into a list; mealie_add_shopping_items adds extras.
"""


class PlanEntry(BaseModel):
    date: Annotated[dt.date, Field(description="YYYY-MM-DD")]
    meal: Meal = "dinner"
    recipe_slug: Annotated[str | None, Field(max_length=200, description="Recipe to cook")] = None
    title: Annotated[str | None, Field(max_length=200, description="Free-text entry instead of a recipe, e.g. 'Takeaway'")] = None
    note: Annotated[str | None, Field(max_length=500)] = None


class RecipeScale(BaseModel):
    slug: Annotated[str, Field(min_length=1, max_length=200)]
    scale: Annotated[float, Field(gt=0, le=20, description="1 = the recipe's servings, 2 = double")] = 1


class ShoppingItem(BaseModel):
    item: Annotated[str, Field(min_length=1, max_length=200, description="e.g. 'semi-skimmed milk'")]
    quantity: Annotated[float | None, Field(gt=0, le=1000)] = None


def _week(start: dt.date | None, days: int) -> tuple[dt.date, dt.date]:
    start = start or dt.date.today()
    return start, start + dt.timedelta(days=days - 1)


def register_manage_tools(mcp: FastMCP, manager: Manager, settings: Settings, safe: Callable) -> None:
    def dry(flag: bool) -> bool:
        return flag or settings.dry_run

    # ================================================================ audit
    @mcp.tool(annotations=READ)
    @safe
    async def mealie_audit_recipes(
        checks: Annotated[
            list[str] | None,
            Field(description=f"Checks to run (default all): {', '.join(AUDIT_CHECKS)}"),
        ] = None,
        max_recipes: Annotated[int, Field(ge=1, le=2000)] = 1000,
        limit_per_issue: Annotated[int, Field(ge=1, le=500, description="Max recipes listed per issue")] = 50,
    ) -> dict[str, Any]:
        """Audit the whole recipe library for missing images, ingredients, steps, servings, times, tags,
        categories, sources or nutrition, unparsed ingredients, and possible duplicates."""
        return {"ok": True, **(await manager.audit(checks=checks, max_recipes=max_recipes, limit_per_issue=limit_per_issue))}

    @mcp.tool(annotations=READ)
    @safe
    async def mealie_get_recipes_with_ingredients(
        slugs: Annotated[list[str] | None, Field(max_length=50, description="Specific recipes; omit to page through")] = None,
        search: Annotated[str | None, Field(max_length=200, description="Only recipes matching this text")] = None,
        tags: Annotated[list[str] | None, Field(description="Only recipes with any of these tags")] = None,
        categories: Annotated[list[str] | None, Field(description="Only recipes in any of these categories")] = None,
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=50)] = 25,
    ) -> dict[str, Any]:
        """Get many recipes at once with their full ingredient lists, servings and tags: for choosing a week's
        meals against criteria (e.g. proteins, budget) and estimating shopping costs. Page through with `page`."""
        out = await manager.recipes_with_ingredients(
            slugs=slugs, search=search, tags=tags, categories=categories, page=page, per_page=per_page
        )
        return {"ok": True, **out}

    @mcp.tool(annotations=READ)
    @safe
    async def mealie_list_organizers(
        kind: Annotated[Literal["tags", "categories", "tools"], Field(description="Which organizer to list")] = "tags",
    ) -> dict[str, Any]:
        """List all tags, categories or kitchen tools defined in Mealie."""
        return {"ok": True, "kind": kind, "items": await manager.list_organizers(kind)}

    # ============================================================== editing
    @mcp.tool(annotations=EDIT)
    @safe
    async def mealie_update_recipe(
        slug: Slug,
        name: Annotated[str | None, Field(min_length=2, max_length=200)] = None,
        description: Annotated[str | None, Field(max_length=5000)] = None,
        servings: Annotated[float | None, Field(gt=0, le=100)] = None,
        prep_minutes: Annotated[int | None, Field(ge=0, le=2880)] = None,
        cook_minutes: Annotated[int | None, Field(ge=0, le=2880)] = None,
        total_minutes: Annotated[int | None, Field(ge=0, le=5760)] = None,
        ingredients: Annotated[
            list[str] | None,
            Field(
                max_length=150,
                description="REPLACES the whole ingredient list. One per entry, e.g. '400g chicken thighs'. "
                "Start a section with '## For the sauce'. Parsed food links are reset; run mealie_parse_ingredients after.",
            ),
        ] = None,
        instructions: Annotated[list[str] | None, Field(max_length=80, description="REPLACES all steps, one per entry")] = None,
        notes: Annotated[list[str] | None, Field(max_length=20, description="REPLACES the recipe notes")] = None,
        nutrition: Annotated[
            dict[str, str] | None,
            Field(
                description="Per serving; merged into existing. Keys: calories, protein, fat, saturated_fat, "
                "carbohydrates, sugar, fibre, salt, cholesterol"
            ),
        ] = None,
        tags: Annotated[list[str] | None, Field(max_length=40, description="REPLACES all tags")] = None,
        add_tags: Annotated[list[str] | None, Field(max_length=40)] = None,
        remove_tags: Annotated[list[str] | None, Field(max_length=40)] = None,
        categories: Annotated[list[str] | None, Field(max_length=20, description="REPLACES all categories")] = None,
        add_categories: Annotated[list[str] | None, Field(max_length=20)] = None,
        remove_categories: Annotated[list[str] | None, Field(max_length=20)] = None,
        add_tools: Annotated[list[str] | None, Field(max_length=20, description="Kitchen tools, e.g. 'Slow Cooker'")] = None,
        remove_tools: Annotated[list[str] | None, Field(max_length=20)] = None,
        rating: Annotated[float | None, Field(ge=0, le=5)] = None,
        source_url: Annotated[str | None, Field(max_length=2048)] = None,
        dry_run: Annotated[bool, Field(description="Show the changes without saving")] = False,
    ) -> dict[str, Any]:
        """Edit an existing recipe: name, description, servings, times, ingredients, steps, notes, nutrition,
        tags, categories, tools, rating or source. Only the fields you pass change. A backup is saved first
        (undo with mealie_restore_recipe). Missing tags/categories/tools are created."""
        fields = dict(
            name=name,
            description=description,
            servings=servings,
            prep_minutes=prep_minutes,
            cook_minutes=cook_minutes,
            total_minutes=total_minutes,
            ingredients=ingredients,
            instructions=instructions,
            notes=notes,
            nutrition=nutrition,
            tags=tags,
            add_tags=add_tags,
            remove_tags=remove_tags,
            categories=categories,
            add_categories=add_categories,
            remove_categories=remove_categories,
            add_tools=add_tools,
            remove_tools=remove_tools,
            rating=rating,
            source_url=source_url,
        )
        changes = {k: v for k, v in fields.items() if v is not None}
        return {"ok": True, **(await manager.update_recipe(slug, changes, dry_run=dry(dry_run)))}

    @mcp.tool(annotations=EDIT_WEB)
    @safe
    async def mealie_set_recipe_image(
        slug: Slug,
        image_url: Annotated[
            str | None,
            Field(max_length=2048, description="Public https image URL. Omit to use the image from the recipe's source page."),
        ] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Set or replace a recipe's photo, from an image URL or from the recipe's original web page.
        The old image is backed up first."""
        return {"ok": True, **(await manager.set_image(slug, image_url, dry_run=dry(dry_run)))}

    @mcp.tool(annotations=EDIT)
    @safe
    async def mealie_parse_ingredients(
        slug: Slug,
        apply: Annotated[bool, Field(description="false = preview only; true = save the confident results")] = False,
        min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.6,
    ) -> dict[str, Any]:
        """Run Mealie's ingredient parser on a recipe's unparsed ingredients (splits quantity, unit, food), so
        shopping lists merge and scale properly. Preview first; apply=true saves (with a backup). Missing
        foods and units are created."""
        return {
            "ok": True,
            **(await manager.parse_ingredients(slug, apply=apply and not settings.dry_run, min_confidence=min_confidence)),
        }

    @mcp.tool(annotations=READ)
    @safe
    async def mealie_list_recipe_backups(slug: Slug) -> dict[str, Any]:
        """List the backups saved before each change to a recipe (newest first)."""
        return {"ok": True, "slug": slug, "backups": manager.list_backups(slug)}

    @mcp.tool(annotations=EDIT)
    @safe
    async def mealie_restore_recipe(
        slug: Slug,
        backup_id: Annotated[
            str, Field(min_length=1, max_length=40, description="From mealie_list_recipe_backups or an edit result")
        ],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Undo changes: put a recipe back the way it was in a backup (the current version is backed up too)."""
        return {"ok": True, **(await manager.restore(slug, backup_id, dry_run=dry(dry_run)))}

    # ============================================================ meal plan
    @mcp.tool(annotations=READ)
    @safe
    async def mealie_suggest_recipes(
        count: Annotated[int, Field(ge=1, le=30)] = 7,
        tags: Annotated[list[str] | None, Field(description="Only recipes with any of these tags")] = None,
        categories: Annotated[list[str] | None, Field(description="Only recipes in any of these categories")] = None,
        exclude_planned_within_days: Annotated[int, Field(ge=0, le=90, description="Skip recipes planned recently")] = 14,
    ) -> dict[str, Any]:
        """Pick random recipes from the library for meal planning, skipping ones already planned recently."""
        picks = await manager.suggest(count=count, tags=tags, categories=categories, exclude_days=exclude_planned_within_days)
        return {"ok": True, "recipes": picks}

    @mcp.tool(annotations=READ)
    @safe
    async def mealie_get_meal_plan(
        start_date: Annotated[dt.date | None, Field(description="YYYY-MM-DD, default today")] = None,
        days: Annotated[int, Field(ge=1, le=62)] = 7,
    ) -> dict[str, Any]:
        """Show the meal plan for a date range (default: the next 7 days)."""
        start, end = _week(start_date, days)
        return {"ok": True, "start": start.isoformat(), "end": end.isoformat(), "entries": await manager.meal_plan(start, end)}

    @mcp.tool(annotations=ADD)
    @safe
    async def mealie_add_meal_plan_entries(
        entries: Annotated[list[PlanEntry], Field(min_length=1, max_length=60)],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Add recipes (or free-text entries) to the meal plan on given dates. Check mealie_get_meal_plan first
        to avoid doubling up a day."""
        results = await manager.add_plan_entries([e.model_dump(mode="json") for e in entries], dry_run=dry(dry_run))
        return {"ok": True, "results": results}

    @mcp.tool(annotations=EDIT)
    @safe
    async def mealie_update_meal_plan_entry(
        entry_id: Annotated[int, Field(ge=1, description="Entry id from mealie_get_meal_plan")],
        date: dt.date | None = None,
        meal: Meal | None = None,
        recipe_slug: Annotated[str | None, Field(max_length=200)] = None,
        title: Annotated[str | None, Field(max_length=200)] = None,
        note: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        """Move a meal plan entry to another day/meal or swap its recipe."""
        changes = {
            "date": date.isoformat() if date else None,
            "meal": meal,
            "recipe_slug": recipe_slug,
            "title": title,
            "note": note,
        }
        if settings.dry_run:
            return {"ok": True, "status": "dry_run", "changes": {k: v for k, v in changes.items() if v is not None}}
        return {"ok": True, "status": "updated", "entry": await manager.update_plan_entry(entry_id, changes)}

    @mcp.tool(annotations=REMOVE)
    @safe
    async def mealie_remove_meal_plan_entries(
        entry_ids: Annotated[list[int], Field(min_length=1, max_length=60, description="Entry ids from mealie_get_meal_plan")],
    ) -> dict[str, Any]:
        """Remove entries from the meal plan (the recipes themselves are not touched)."""
        if settings.dry_run:
            return {"ok": True, "status": "dry_run", "would_remove": entry_ids}
        return {"ok": True, "results": await manager.remove_plan_entries(entry_ids)}

    # ======================================================= shopping lists
    @mcp.tool(annotations=READ)
    @safe
    async def mealie_get_shopping_lists() -> dict[str, Any]:
        """List the household's shopping lists."""
        return {"ok": True, "lists": await manager.shopping_lists()}

    @mcp.tool(annotations=READ)
    @safe
    async def mealie_get_shopping_list(
        list_id: Annotated[str, Field(min_length=32, max_length=36)],
        include_checked: bool = False,
    ) -> dict[str, Any]:
        """Show the items on a shopping list, grouped with the recipes they came from."""
        return {"ok": True, **(await manager.shopping_list(list_id, include_checked=include_checked))}

    @mcp.tool(annotations=ADD)
    @safe
    async def mealie_create_shopping_list(
        name: Annotated[str, Field(min_length=1, max_length=100, description="e.g. 'Week of 13 Oct'")],
    ) -> dict[str, Any]:
        """Create a new, empty shopping list."""
        if settings.dry_run:
            return {"ok": True, "status": "dry_run", "name": name}
        return {"ok": True, "status": "created", **(await manager.create_shopping_list(name))}

    @mcp.tool(annotations=ADD)
    @safe
    async def mealie_add_recipes_to_shopping_list(
        list_id: Annotated[str, Field(min_length=32, max_length=36)],
        recipes: Annotated[list[RecipeScale], Field(min_length=1, max_length=40)],
    ) -> dict[str, Any]:
        """Add every ingredient of the given recipes to a shopping list (Mealie merges matching items)."""
        if settings.dry_run:
            return {"ok": True, "status": "dry_run", "recipes": [r.slug for r in recipes]}
        return {"ok": True, **(await manager.add_recipes_to_list(list_id, [r.model_dump() for r in recipes]))}

    @mcp.tool(annotations=ADD)
    @safe
    async def mealie_add_meal_plan_to_shopping_list(
        list_id: Annotated[str, Field(min_length=32, max_length=36)],
        start_date: Annotated[dt.date | None, Field(description="YYYY-MM-DD, default today")] = None,
        days: Annotated[int, Field(ge=1, le=31)] = 7,
    ) -> dict[str, Any]:
        """Add the ingredients of every recipe on the meal plan in a date range to a shopping list."""
        start, end = _week(start_date, days)
        entries = await manager.meal_plan(start, end)
        counts: dict[str, int] = {}
        for e in entries:
            slug = (e.get("recipe") or {}).get("slug")
            if slug:
                counts[slug] = counts.get(slug, 0) + 1
        if not counts:
            return {"ok": True, "status": "nothing_planned", "start": start.isoformat(), "end": end.isoformat()}
        if settings.dry_run:
            return {"ok": True, "status": "dry_run", "recipes": counts}
        result = await manager.add_recipes_to_list(list_id, [{"slug": s, "scale": n} for s, n in counts.items()])
        return {"ok": True, "status": "added", "start": start.isoformat(), "end": end.isoformat(), **result}

    @mcp.tool(annotations=ADD)
    @safe
    async def mealie_add_shopping_items(
        list_id: Annotated[str, Field(min_length=32, max_length=36)],
        items: Annotated[list[ShoppingItem], Field(min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        """Add free-text items (e.g. 'milk', 'bin bags') to a shopping list."""
        if settings.dry_run:
            return {"ok": True, "status": "dry_run", "items": [i.item for i in items]}
        return {"ok": True, **(await manager.add_items(list_id, [i.model_dump() for i in items]))}

    @mcp.tool(annotations=EDIT)
    @safe
    async def mealie_check_shopping_items(
        list_id: Annotated[str, Field(min_length=32, max_length=36)],
        item_ids: Annotated[list[str], Field(min_length=1, max_length=200)],
        checked: Annotated[bool, Field(description="true = tick off (bought), false = untick")] = True,
    ) -> dict[str, Any]:
        """Tick items off (or untick them) on a shopping list."""
        if settings.dry_run:
            return {"ok": True, "status": "dry_run", "items": item_ids}
        return {"ok": True, **(await manager.set_checked(list_id, item_ids, checked))}

    @mcp.tool(annotations=REMOVE)
    @safe
    async def mealie_remove_shopping_items(
        item_ids: Annotated[list[str], Field(min_length=1, max_length=200)],
    ) -> dict[str, Any]:
        """Remove items from a shopping list."""
        if settings.dry_run:
            return {"ok": True, "status": "dry_run", "would_remove": item_ids}
        return {"ok": True, **(await manager.remove_items(item_ids))}

    @mcp.tool(annotations=REMOVE)
    @safe
    async def mealie_remove_recipe_from_shopping_list(
        list_id: Annotated[str, Field(min_length=32, max_length=36)],
        slug: Slug,
    ) -> dict[str, Any]:
        """Take one recipe's ingredients back off a shopping list."""
        if settings.dry_run:
            return {"ok": True, "status": "dry_run", "slug": slug}
        return {"ok": True, **(await manager.remove_recipe_from_list(list_id, slug))}
