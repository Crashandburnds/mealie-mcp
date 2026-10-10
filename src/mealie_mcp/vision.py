"""Read recipe photos (HelloFresh cards, cookbook pages) with Claude and return structured data."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .logging_setup import log

logger = logging.getLogger("mealie_mcp.vision")

SYSTEM_PROMPT = """\
You read photos of recipes for a home cook who is building a recipe library.

Classify the photo:
- "hellofresh_card": a HelloFresh recipe card. Read only the title and the subtitle (the "with ..." line under it).
- "cookbook_page": a recipe printed in a book or magazine. Transcribe it.
- "not_a_recipe": anything else, or a recipe too blurry or cut off to read reliably.

When transcribing a cookbook page:
- Copy ingredients and steps exactly as printed, one ingredient or step per entry, keeping quantities and units.
  Keep section headings such as "For the topping" as their own ingredient entry.
- Include optional "to accompany" or "to serve" items as ingredients prefixed with "(optional) ".
- Use the printed numbers for servings, prep and cook time in minutes, calories and carbs per serving.
  Use 0 or an empty string when the page does not state a value; never estimate one.
- Put dietary badges as tags, spelled out: V -> Vegetarian, F -> Freezable, GF -> Gluten free, plus any noted
  substitution (e.g. "GF: use GF stock cubes") as a note.
- Put tips, batch-cooking and freezing notes in notes, verbatim.
- book_page is the printed page number if visible, and section is the running footer (e.g. "Quick Meals").
- If any word or number is unreadable, add a warning naming it rather than guessing.

The photo may be rotated; read it in whatever orientation the text runs."""

RECIPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["hellofresh_card", "cookbook_page", "not_a_recipe"]},
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "description": {"type": "string"},
        "servings": {"type": "integer"},
        "prep_minutes": {"type": "integer"},
        "cook_minutes": {"type": "integer"},
        "calories": {"type": "string"},
        "carbs": {"type": "string"},
        "ingredients": {"type": "array", "items": {"type": "string"}},
        "instructions": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
        "book_page": {"type": "string"},
        "section": {"type": "string"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "kind",
        "title",
        "subtitle",
        "description",
        "servings",
        "prep_minutes",
        "cook_minutes",
        "calories",
        "carbs",
        "ingredients",
        "instructions",
        "tags",
        "notes",
        "book_page",
        "section",
        "warnings",
    ],
    "additionalProperties": False,
}

MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class VisionError(Exception):
    def __init__(self, message: str, kind: str = "vision_error") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class ReadRecipe:
    kind: str
    title: str = ""
    subtitle: str = ""
    description: str = ""
    servings: int = 0
    prep_minutes: int = 0
    cook_minutes: int = 0
    calories: str = ""
    carbs: str = ""
    ingredients: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    book_page: str = ""
    section: str = ""
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReadRecipe:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


class RecipeReader:
    """Calls Claude with a recipe photo and returns a ReadRecipe."""

    def __init__(self, api_key: str, model: str = "claude-opus-5-5", *, client: Any = None) -> None:
        self.model = model
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=180.0, max_retries=2)

    async def read(self, image: bytes, media_type: str) -> ReadRecipe:
        if media_type not in MEDIA_TYPES:
            raise VisionError(f"unsupported image type {media_type}", kind="invalid_image")
        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(image).decode()},
            },
            {"type": "text", "text": "Read this recipe photo."},
        ]
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": RECIPE_SCHEMA}},
                # On a safety decline the API retries the request on a fallback model within the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise VisionError("Claude rejected the API key (check CLAUDE_API_TOKEN)", kind="auth_error") from exc
        except anthropic.PermissionDeniedError as exc:
            raise VisionError("Claude API key lacks permission for this model", kind="auth_error") from exc
        except anthropic.RateLimitError as exc:
            raise VisionError("Claude API rate limit reached; try again shortly", kind="rate_limited") from exc
        except anthropic.BadRequestError as exc:
            raise VisionError(f"Claude rejected the request: {exc.message}", kind="bad_request") from exc
        except anthropic.APIStatusError as exc:
            raise VisionError(f"Claude API error ({exc.status_code})", kind="api_error") from exc
        except anthropic.APIConnectionError as exc:
            raise VisionError("Could not reach the Claude API", kind="connection_error") from exc

        if response.stop_reason == "refusal":
            raise VisionError("Claude declined to read this image", kind="refusal")
        if response.stop_reason == "max_tokens":
            raise VisionError("The recipe was too long to read in one go", kind="too_long")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise VisionError("Claude returned an unreadable response", kind="bad_response") from exc
        usage = response.usage
        log(
            logger,
            logging.INFO,
            "photo read",
            kind=data.get("kind"),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        return ReadRecipe.from_json(data)
