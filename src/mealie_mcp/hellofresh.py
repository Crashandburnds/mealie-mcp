"""HelloFresh UK recipe search, verification and matching.

HelloFresh has no public API. Search uses two independent strategies:

1. The JSON search gateway the hellofresh.co.uk website itself uses, with the
   anonymous token embedded in the public site's ``__NEXT_DATA__``.
2. Parsing recipe links out of the public ``/recipes/search`` page.

Neither is trusted on its own: every candidate is *verified* by fetching the
recipe page and reading its schema.org Recipe data before it can be reported
as a match. A candidate is only "confident" when the normalised recipe name is
identical (and the subtitle matches, when one is supplied); near-misses such as
"Curried Chicken *Breast* Traybake" are reported as alternatives, never imported.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from rapidfuzz import fuzz

from .logging_setup import log
from .net import (
    USER_AGENT,
    UrlValidationError,
    allowlisted_client,
    hellofresh_id,
    validate_hellofresh_url,
)

logger = logging.getLogger("mealie_mcp.hellofresh")

NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
LD_JSON_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
RECIPE_LINK_RE = re.compile(r'href="((?:https://www\.hellofresh\.co\.uk)?/recipes/[a-z0-9-]+-[0-9a-f]{24})"')
# token_sort_ratio on normalised subtitles. Tight on purpose: HelloFresh variants often share most words
# ("Mangetout, Jasmine Rice and Peanuts" vs "Zesty Rice, Mangetout, Coriander and Peanuts" scores 79).
SUBTITLE_THRESHOLD = 92


class HelloFreshError(Exception):
    def __init__(self, message: str, kind: str = "hellofresh_error") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Candidate:
    name: str
    url: str
    id: str | None = None
    headline: str | None = None
    total_time: str | None = None
    image: str | None = None
    source: str = "search"
    verified: bool = False
    name_score: float = 0.0
    subtitle_score: float | None = None
    exact_name: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class MatchResult:
    status: str  # matched | ambiguous | not_found | error
    query: dict[str, Any]
    match: Candidate | None = None
    alternatives: list[Candidate] = field(default_factory=list)
    equivalent_urls: list[str] = field(default_factory=list)
    message: str = ""
    search_strategies: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "query": self.query,
            "message": self.message,
        }
        if self.match:
            out["match"] = self.match.to_dict()
        if self.alternatives:
            out["alternatives"] = [c.to_dict() for c in self.alternatives]
        if self.equivalent_urls:
            out["equivalent_urls"] = self.equivalent_urls
        if self.search_strategies:
            out["search_strategies"] = self.search_strategies
        if self.errors:
            out["errors"] = self.errors
        if self.details:
            out["details"] = self.details
        return out


# --------------------------------------------------------------------- helpers
def normalise(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text).lower().replace("&", " and ")
    text = re.sub(r"[’'`]", "", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


def _strip_with(text: str) -> str:
    return re.sub(r"^with\s+", "", normalise(text))


def title_core(name: str | None, headline: str | None) -> str:
    """Normalised title without the subtitle. HelloFresh pages often title a recipe
    "X  with Y and Z", repeating the headline; strip it so X can be compared exactly."""
    n, h = normalise(name), normalise(headline)
    if h and n.endswith(h) and len(n) > len(h):
        return n[: -len(h)].strip()
    return n


def split_title(name: str) -> tuple[str, str | None]:
    """'X with Y and Z' -> ('X', 'with Y and Z')."""
    match = re.match(r"^(.*?)\s+(with\s+.+)$", name.strip(), re.I)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return name.strip(), None


def _find_key(obj: Any, key: str, depth: int = 0) -> Any:
    if depth > 12:
        return None
    if isinstance(obj, dict):
        if key in obj and obj[key]:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_key(value, key, depth + 1)
            if found:
                return found
    return None


def _find_dicts(obj: Any, predicate, depth: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if depth > 14:
        return out
    if isinstance(obj, dict):
        if predicate(obj):
            out.append(obj)
        for value in obj.values():
            out += _find_dicts(value, predicate, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            out += _find_dicts(value, predicate, depth + 1)
    return out


def _next_data(page: str) -> dict[str, Any] | None:
    match = NEXT_DATA_RE.search(page)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _ld_recipe(page: str) -> dict[str, Any] | None:
    for block in LD_JSON_RE.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for item in _find_dicts(data, lambda d: d.get("@type") in ("Recipe", ["Recipe"])):
            return item
    return None


def _iso_minutes(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?", value)
    if not match or not any(match.groups()):
        return value or None
    hours, minutes = int(match.group(1) or 0), int(match.group(2) or 0)
    total = hours * 60 + minutes
    return f"{total} minutes"


def _instructions(raw: Any) -> list[str]:
    steps: list[str] = []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split("\n") if s.strip()]
    for item in raw or []:
        if isinstance(item, str):
            steps.append(item.strip())
        elif isinstance(item, dict):
            if item.get("@type") == "HowToSection":
                steps += _instructions(item.get("itemListElement"))
            elif item.get("text"):
                steps.append(html.unescape(re.sub(r"<[^>]+>", " ", item["text"])).strip())
    return [" ".join(s.split()) for s in steps if s]


# ---------------------------------------------------------------------- client
class HelloFreshClient:
    def __init__(
        self,
        base_url: str = "https://www.hellofresh.co.uk",
        *,
        country: str = "GB",
        locale: str = "en-GB",
        timeout: float = 20.0,
        min_interval: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.country = country
        self.locale = locale
        self.min_interval = min_interval
        host = urlsplit(self.base_url).hostname or ""
        bare = host.removeprefix("www.")
        self._client = allowlisted_client(
            [host, bare],
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": f"{locale},en;q=0.8"},
            transport=transport,
        )
        self._token: str | None = None
        self._token_expiry = 0.0
        self._throttle = asyncio.Lock()
        self._last_request = 0.0
        self._page_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(3):
            async with self._throttle:
                wait = self.min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
            try:
                response = await self._client.get(url, **kwargs)
            except httpx.TimeoutException as exc:
                raise HelloFreshError("HelloFresh did not respond in time", kind="timeout") from exc
            except httpx.HTTPError as exc:
                raise HelloFreshError(f"Could not reach HelloFresh ({type(exc).__name__})", kind="connection_error") from exc
            if response.status_code == 429 or response.status_code >= 500:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            return response
        raise HelloFreshError("HelloFresh is rate limiting or unavailable; try again later", kind="rate_limited")

    # -------------------------------------------------------------- search
    async def _anonymous_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        response = await self._get(f"{self.base_url}/recipes")
        data = _next_data(response.text) if response.status_code == 200 else None
        token = _find_key(data, "access_token") if data else None
        if not isinstance(token, str):
            raise HelloFreshError("could not obtain HelloFresh search token", kind="search_unavailable")
        expires = _find_key(data, "expires_in")
        self._token = token
        self._token_expiry = time.time() + min(int(expires) if isinstance(expires, int) else 1800, 3600) - 60
        return token

    def _candidate_from_api(self, item: dict[str, Any]) -> Candidate | None:
        rid, name = item.get("id"), item.get("name")
        if not rid or not name:
            return None
        url = item.get("websiteUrl") or (f"{self.base_url}/recipes/{item['slug']}-{rid}" if item.get("slug") else None)
        if not url:
            return None
        try:
            url = validate_hellofresh_url(url, self.base_url)
        except UrlValidationError:
            return None
        image = item.get("imageLink") or item.get("imagePath")
        return Candidate(
            name=name.strip(),
            url=url,
            id=rid,
            headline=(item.get("headline") or None),
            total_time=_iso_minutes(item.get("totalTime")),
            image=image,
            source="hellofresh_api",
        )

    async def _search_api(self, query: str, limit: int) -> list[Candidate]:
        token = await self._anonymous_token()
        response = await self._get(
            f"{self.base_url}/gw/recipes/recipes/search",
            params={"country": self.country, "locale": self.locale, "q": query, "take": limit, "skip": 0},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if response.status_code in (401, 403):
            self._token = None
            raise HelloFreshError("HelloFresh search API refused the anonymous token", kind="search_unavailable")
        if response.status_code != 200:
            raise HelloFreshError(f"HelloFresh search API returned HTTP {response.status_code}", kind="search_unavailable")
        items = response.json().get("items", [])
        return [c for c in (self._candidate_from_api(i) for i in items) if c]

    async def _search_html(self, query: str, limit: int) -> list[Candidate]:
        response = await self._get(f"{self.base_url}/recipes/search", params={"q": query})
        if response.status_code != 200:
            raise HelloFreshError(f"HelloFresh search page returned HTTP {response.status_code}", kind="search_unavailable")
        page = response.text
        candidates: dict[str, Candidate] = {}
        data = _next_data(page)
        if data:
            for item in _find_dicts(
                data, lambda d: isinstance(d.get("id"), str) and len(d.get("id", "")) == 24 and d.get("name")
            ):
                cand = self._candidate_from_api(item)
                if cand:
                    cand.source = "hellofresh_search_page"
                    candidates.setdefault(cand.url, cand)
        for href in RECIPE_LINK_RE.findall(page):
            url = href if href.startswith("http") else f"{self.base_url}{href}"
            try:
                url = validate_hellofresh_url(url, self.base_url)
            except UrlValidationError:
                continue
            if url not in candidates:
                slug = urlsplit(url).path.rsplit("/", 1)[-1]
                guessed = re.sub(r"-[0-9a-f]{24}$", "", slug).replace("-", " ").title()
                # Name guessed from the slug; must be verified before use.
                candidates[url] = Candidate(name=guessed, url=url, id=hellofresh_id(url), source="hellofresh_search_links")
        return list(candidates.values())[:limit]

    async def search(self, query: str, limit: int = 10) -> tuple[list[Candidate], list[str], list[str]]:
        """Return (candidates, strategies_used, errors)."""
        query = " ".join(query.split())[:120]
        if not query:
            raise HelloFreshError("search query is empty", kind="invalid_input")
        strategies, errors = [], []
        for name, fn in (("hellofresh_api", self._search_api), ("hellofresh_search_page", self._search_html)):
            try:
                results = await fn(query, limit)
            except HelloFreshError as exc:
                errors.append(f"{name}: {exc}")
                log(logger, logging.WARNING, "hellofresh search strategy failed", strategy=name, error=str(exc))
                continue
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(f"{name}: unexpected response format ({type(exc).__name__})")
                continue
            strategies.append(name)
            if results:
                return results, strategies, errors
        return [], strategies, errors

    # ------------------------------------------------------------- verify
    async def fetch_recipe(self, url: str) -> dict[str, Any]:
        """Fetch and parse a HelloFresh recipe page. Raises HelloFreshError if it is not a real recipe."""
        url = validate_hellofresh_url(url, self.base_url)
        cached = self._page_cache.get(url)
        if cached and time.time() - cached[0] < 900:
            return cached[1]
        response = await self._get(url)
        if response.status_code == 404:
            raise HelloFreshError("HelloFresh recipe page not found (404)", kind="not_found")
        if response.status_code != 200:
            raise HelloFreshError(f"HelloFresh returned HTTP {response.status_code}", kind="fetch_failed")
        final = str(response.url)
        try:
            final = validate_hellofresh_url(final, self.base_url)
        except UrlValidationError as exc:
            raise HelloFreshError(
                "recipe URL redirected to a non-recipe page (recipe may be withdrawn)", kind="not_found"
            ) from exc
        page = response.text
        ld = _ld_recipe(page)
        rid = hellofresh_id(final)
        headline = None
        data = _next_data(page)
        if data and rid:
            for d in _find_dicts(data, lambda d: d.get("id") == rid and d.get("headline")):
                headline = d["headline"]
                break
        if not ld:
            raise HelloFreshError("page has no recipe data (it may not be a recipe page)", kind="not_a_recipe")
        # The page's own title is authoritative: search listings for re-runs can carry a headline that
        # differs from the recipe the page actually shows.
        _, title_sub = split_title(" ".join(html.unescape(ld.get("name") or "").split()))
        if title_sub:
            headline = title_sub
        nutrition = {k: v for k, v in (ld.get("nutrition") or {}).items() if not k.startswith("@") and v not in (None, "")}
        image = ld.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        details = {
            "name": html.unescape(ld.get("name") or "").strip(),
            "subtitle": headline,
            "url": final,
            "id": rid,
            "description": html.unescape(ld.get("description") or "").strip() or None,
            "servings": ld.get("recipeYield"),
            "total_time": _iso_minutes(ld.get("totalTime")),
            "prep_time": _iso_minutes(ld.get("prepTime")),
            "cook_time": _iso_minutes(ld.get("cookTime")),
            "ingredients": [html.unescape(i).strip() for i in ld.get("recipeIngredient") or [] if isinstance(i, str)],
            "instructions": _instructions(ld.get("recipeInstructions")),
            "nutrition": nutrition or None,
            "image": image,
            "cuisine": ld.get("recipeCuisine"),
            "category": ld.get("recipeCategory"),
        }
        if not details["name"]:
            raise HelloFreshError("recipe page has no recipe name", kind="not_a_recipe")
        self._page_cache[url] = (time.time(), details)
        return details

    # -------------------------------------------------------------- match
    async def match(
        self,
        name: str,
        subtitle: str | None = None,
        *,
        candidate_urls: list[str] | None = None,
        limit: int = 10,
        include_details: bool = False,
    ) -> MatchResult:
        name = " ".join((name or "").split())
        if not name and not candidate_urls:
            raise HelloFreshError("a recipe name or candidate URL is required", kind="invalid_input")
        if not subtitle and name:
            base, maybe_sub = split_title(name)
            if maybe_sub:
                name, subtitle = base, maybe_sub
        query = {"name": name, "subtitle": subtitle}
        result = MatchResult(status="not_found", query=query)

        candidates: list[Candidate] = []
        for url in candidate_urls or []:
            try:
                clean = validate_hellofresh_url(url, self.base_url)
            except UrlValidationError as exc:
                result.errors.append(f"{url}: {exc}")
                continue
            candidates.append(Candidate(name="", url=clean, id=hellofresh_id(clean), source="provided_url"))
        if not candidates and name:
            found, result.search_strategies, errs = await self.search(name, limit)
            result.errors += errs
            candidates = found
            if not found and errs and not result.search_strategies:
                result.status = "error"
                result.message = (
                    "HelloFresh search is unavailable from the server. Find the recipe URL on hellofresh.co.uk "
                    "(e.g. via web search) and pass it as candidate_urls so it can be verified."
                )
                return result

        # Pre-score on search data, then verify the most promising candidates against the real page.
        target, target_sub = normalise(name), _strip_with(subtitle) if subtitle else None
        full_target = normalise(f"{name} {subtitle}") if subtitle else None

        def prescore(c: Candidate) -> tuple[float, float]:
            name_score = fuzz.token_sort_ratio(target, title_core(c.name, c.headline)) if target and c.name else 0.0
            sub_score = fuzz.token_sort_ratio(target_sub, _strip_with(c.headline)) if target_sub and c.headline else 0.0
            return name_score, sub_score

        # Collapse re-runs (new ids, same page) before choosing what to verify, so duplicates don't use up the
        # verification slots, and verify the best title + subtitle candidates first.
        best: dict[str, tuple[tuple[float, float], Candidate]] = {}
        for c in candidates:
            score = prescore(c)
            c.name_score = score[0]
            if c.url not in best or score > best[c.url][0]:
                best[c.url] = (score, c)
        ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)
        candidates = [c for _, c in ranked]
        to_verify = [c for c in candidates if c.source == "provided_url" or c.name_score >= 60][:5]
        details_by_url: dict[str, dict[str, Any]] = {}
        for c in to_verify:
            try:
                details = await self.fetch_recipe(c.url)
            except (HelloFreshError, UrlValidationError) as exc:
                result.errors.append(f"{c.url}: {exc}")
                c.verified = False
                continue
            c.name, c.url, c.verified = details["name"], details["url"], True
            c.headline = details.get("subtitle") or c.headline
            c.total_time = details.get("total_time") or c.total_time
            c.image = details.get("image") or c.image
            details_by_url[c.url] = details
        # HelloFresh lists re-runs of a recipe under new ids that resolve to the same page: keep one per URL.
        unique: dict[str, Candidate] = {}
        for c in candidates:
            if c.url not in unique or (c.verified and not unique[c.url].verified):
                unique[c.url] = c
        candidates = list(unique.values())
        for c in candidates:
            core = title_core(c.name, c.headline)
            c.exact_name = bool(target) and (core == target or (full_target is not None and normalise(c.name) == full_target))
            c.name_score = fuzz.token_sort_ratio(target, core) if target else 0.0
            if target_sub and c.headline:
                c.subtitle_score = float(fuzz.token_sort_ratio(target_sub, _strip_with(c.headline)))

        def confident(c: Candidate) -> bool:
            if not c.verified:
                return False
            if not target:  # URL-only verification: the page itself is the match
                return True
            if not c.exact_name:
                return False
            if target_sub:
                return c.subtitle_score is not None and c.subtitle_score >= SUBTITLE_THRESHOLD
            return True

        winners = [c for c in candidates if confident(c)]
        others = [c for c in candidates if c not in winners and (c.name_score >= 50 or c.source == "provided_url")][:5]
        result.alternatives = others

        if winners:
            # HelloFresh republishes identical recipes under new ids; treat same name+subtitle as equivalent.
            first = winners[0]
            key = (normalise(first.name), normalise(first.headline))
            equivalent = [w for w in winners[1:] if (normalise(w.name), normalise(w.headline)) == key]
            different = [w for w in winners[1:] if w not in equivalent]
            if different:
                result.status = "ambiguous"
                result.alternatives = winners + others
                result.message = "Several different HelloFresh recipes match this name; supply the subtitle or pick a URL."
                return result
            result.status = "matched"
            result.match = first
            result.equivalent_urls = [w.url for w in equivalent]
            result.message = "Verified exact match on the HelloFresh recipe page."
            if target_sub and first.subtitle_score is not None:
                result.message += f" Subtitle matched ({first.subtitle_score:.0f}%)."
            elif subtitle and not first.headline:
                result.message += " (Subtitle could not be checked.)"
            if include_details:
                result.details = details_by_url.get(first.url)
            return result

        if candidates:
            result.status = "ambiguous" if others else "not_found"
            unverified_exact = [c for c in candidates if c.exact_name and not c.verified]
            if unverified_exact:
                result.message = "A recipe with this name was found but its page could not be verified; nothing will be imported."
            elif any(c.exact_name for c in candidates) and target_sub:
                result.message = "A recipe with this name exists but the subtitle does not match; check the alternatives."
            else:
                result.message = (
                    "No exact match. Similar recipes are listed as alternatives; none will be imported automatically."
                )
        else:
            result.message = "No HelloFresh UK recipes found for this name."
        return result
