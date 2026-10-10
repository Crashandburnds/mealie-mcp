"""Phone capture app: snap recipe photos, Claude reads them, the server imports them into Mealie.

Served at /capture as an installable web app (add to home screen). Owner-password login sets a signed,
HttpOnly, SameSite=Strict session cookie. Uploads become background jobs so long batches never hit proxy
timeouts; the page polls for results.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from importlib import resources
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .logging_setup import log
from .vision import MEDIA_TYPES, ReadRecipe, RecipeReader, VisionError

logger = logging.getLogger("mealie_mcp.capture")

COOKIE = "mmcp_capture"
SESSION_TTL = 30 * 86400
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_JOBS = 30
JOB_TTL = 24 * 3600
CSRF_HEADER = "x-mealie-capture"

HfImport = Callable[..., Awaitable[dict[str, Any]]]
CreateRecipe = Callable[..., Awaitable[dict[str, Any]]]
ImportUrl = Callable[[str, bool], Awaitable[dict[str, Any]]]


@dataclass
class Item:
    index: int
    filename: str
    status: str = "queued"  # queued | reading | imported | created | duplicate | dry_run | needs_review | skipped | failed
    kind: str | None = None
    title: str | None = None
    message: str | None = None
    mealie_url: str | None = None
    alternatives: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Job:
    id: str
    created: float
    dry_run: bool
    items: list[Item]

    @property
    def done(self) -> bool:
        return all(i.status not in ("queued", "reading") for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created": self.created,
            "dry_run": self.dry_run,
            "done": self.done,
            "items": [asdict(i) for i in self.items],
        }


def _static(name: str) -> bytes:
    return resources.files("mealie_mcp").joinpath("static", name).read_bytes()


class CaptureApp:
    def __init__(
        self,
        *,
        auth: Any,
        reader: RecipeReader | None,
        hf_import: HfImport,
        create_recipe: CreateRecipe,
        import_url: ImportUrl,
        secure_cookie: bool,
        max_images: int = 20,
        concurrency: int = 3,
    ) -> None:
        self.auth = auth
        self.reader = reader
        self.hf_import = hf_import
        self.create_recipe = create_recipe
        self.import_url = import_url
        self.secure_cookie = secure_cookie
        self.max_images = max_images
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self._sem = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- auth
    def _authed(self, request: Request) -> bool:
        if self.auth is None:  # AUTH_MODE=none (local testing only)
            return True
        token = request.cookies.get(COOKIE)
        return bool(token) and self.auth.verify_session("capture", token)

    def _guard(self, request: Request, *, write: bool = False) -> JSONResponse | None:
        if not self._authed(request):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        if write and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"error": "missing capture header"}, status_code=403)
        return None

    # --------------------------------------------------------------- pages
    async def page(self, request: Request) -> Response:
        return HTMLResponse(
            _static("capture.html"),
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    async def asset(self, request: Request) -> Response:
        name = request.path_params["name"]
        types = {
            "manifest.webmanifest": "application/manifest+json",
            "icon-192.png": "image/png",
            "icon-512.png": "image/png",
            "sw.js": "text/javascript",
        }
        if name not in types:
            return Response(status_code=404)
        headers = {"Cache-Control": "public, max-age=86400"}
        if name == "sw.js":
            headers["Service-Worker-Allowed"] = "/capture"
        return Response(_static(name), media_type=types[name], headers=headers)

    # ----------------------------------------------------------------- api
    async def session(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "authenticated": self._authed(request),
                "configured": self.reader is not None,
                "auth_required": self.auth is not None,
                "max_images": self.max_images,
            }
        )

    async def login(self, request: Request) -> JSONResponse:
        if self.auth is None:
            return JSONResponse({"ok": True})
        if request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"error": "missing capture header"}, status_code=403)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        ip = request.client.host if request.client else "unknown"
        result = self.auth.check_password(ip, str(body.get("password", "")))
        if result == "locked":
            return JSONResponse({"error": "Too many failed attempts. Try again in 15 minutes."}, status_code=429)
        if result != "ok":
            return JSONResponse({"error": "Incorrect password."}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            self.auth.issue_session("capture", SESSION_TTL),
            max_age=SESSION_TTL,
            path="/capture",
            httponly=True,
            secure=self.secure_cookie,
            samesite="strict",
        )
        log(logger, logging.INFO, "capture login", ip=ip)
        return response

    async def logout(self, request: Request) -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/capture")
        return response

    async def create_job(self, request: Request) -> JSONResponse:
        if denied := self._guard(request, write=True):
            return denied
        if self.reader is None:
            return JSONResponse({"error": "Photo reading is not configured (set CLAUDE_API_TOKEN)."}, status_code=503)
        form = await request.form(max_files=self.max_images, max_fields=10)
        files = [f for f in form.getlist("images") if hasattr(f, "read")]
        if not files:
            return JSONResponse({"error": "No photos received."}, status_code=400)
        if len(files) > self.max_images:
            return JSONResponse({"error": f"At most {self.max_images} photos per batch."}, status_code=400)
        book = str(form.get("book") or "").strip()[:150]
        dry_run = str(form.get("dry_run") or "").lower() in ("1", "true", "on")

        images: list[tuple[str, bytes, str]] = []
        for upload in files:
            media_type = (upload.content_type or "").split(";")[0].strip().lower()
            if media_type not in MEDIA_TYPES:
                return JSONResponse({"error": f"{upload.filename}: not a supported image type."}, status_code=400)
            data = await upload.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                return JSONResponse({"error": f"{upload.filename}: photo is larger than 10 MB."}, status_code=413)
            images.append((upload.filename or f"photo-{len(images) + 1}", data, media_type))

        self._gc()
        job = Job(
            id=secrets.token_urlsafe(9),
            created=time.time(),
            dry_run=dry_run,
            items=[Item(index=i, filename=name) for i, (name, _, _) in enumerate(images)],
        )
        self.jobs[job.id] = job
        task = asyncio.create_task(self._run(job, images, book))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log(logger, logging.INFO, "capture job started", job=job.id, photos=len(images), dry_run=dry_run)
        return JSONResponse(job.to_dict(), status_code=202)

    async def get_job(self, request: Request) -> JSONResponse:
        if denied := self._guard(request):
            return denied
        job = self.jobs.get(request.path_params["job_id"])
        if not job:
            return JSONResponse({"error": "Job not found (it may have expired)."}, status_code=404)
        return JSONResponse(job.to_dict())

    async def list_jobs(self, request: Request) -> JSONResponse:
        if denied := self._guard(request):
            return denied
        return JSONResponse({"jobs": [j.to_dict() for j in reversed(self.jobs.values())]})

    async def import_choice(self, request: Request) -> JSONResponse:
        """Import a specific URL the owner picked from the alternatives of a 'needs review' item."""
        if denied := self._guard(request, write=True):
            return denied
        try:
            body = await request.json()
        except ValueError:
            body = {}
        url = str(body.get("url") or "").strip()
        if not url:
            return JSONResponse({"error": "url is required"}, status_code=400)
        result = await self.import_url(url, bool(body.get("dry_run")))
        return JSONResponse(result)

    # ---------------------------------------------------------- processing
    def _gc(self) -> None:
        now = time.time()
        for job_id in [k for k, j in self.jobs.items() if now - j.created > JOB_TTL and j.done]:
            self.jobs.pop(job_id, None)
        while len(self.jobs) >= MAX_JOBS:
            oldest = next(iter(self.jobs))
            self.jobs.pop(oldest)

    async def _run(self, job: Job, images: list[tuple[str, bytes, str]], book: str) -> None:
        await asyncio.gather(*(self._one(job.items[i], data, mt, book, job.dry_run) for i, (_, data, mt) in enumerate(images)))
        counts: dict[str, int] = {}
        for item in job.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        log(logger, logging.INFO, "capture job finished", job=job.id, **counts)

    async def _one(self, item: Item, data: bytes, media_type: str, book: str, dry_run: bool) -> None:
        try:
            async with self._sem:
                item.status = "reading"
                read = await self.reader.read(data, media_type)  # type: ignore[union-attr]
            item.kind = read.kind
            item.title = " ".join(x for x in (read.title, read.subtitle) if x) or None
            item.warnings = list(read.warnings)
            if read.kind == "hellofresh_card":
                await self._hellofresh(item, read, dry_run)
            elif read.kind == "cookbook_page":
                await self._cookbook(item, read, book, dry_run)
            else:
                item.status = "skipped"
                item.message = "This photo doesn't look like a recipe (or it couldn't be read)."
        except VisionError as exc:
            item.status = "failed"
            item.message = str(exc)
        except Exception:
            logger.exception("capture item failed")
            item.status = "failed"
            item.message = "Unexpected error; see server logs."

    async def _hellofresh(self, item: Item, read: ReadRecipe, dry_run: bool) -> None:
        if not read.title:
            item.status = "needs_review"
            item.message = "Couldn't read the recipe title on the card."
            return
        out = await self.hf_import(name=read.title, subtitle=read.subtitle or None, url=None, dry_run=dry_run)
        if out.get("error") and not out.get("import_status"):  # error result from the tool wrapper
            item.status = "failed"
            item.message = out.get("message")
            return
        status = out.get("import_status")
        imported = out.get("import") or {}
        recipe = imported.get("recipe") or {}
        if status in ("imported", "duplicate", "dry_run"):
            item.status = status
            item.mealie_url = recipe.get("mealie_url") or next(
                (d.get("mealie_url") for d in imported.get("duplicates") or []), None
            )
            item.message = imported.get("message")
        elif status == "failed":
            item.status = "failed"
            item.message = imported.get("message") or out.get("message")
        else:
            item.status = "needs_review"
            item.message = out.get("message")
            item.alternatives = [
                {
                    "name": " ".join(
                        x
                        for x in (a.get("name", ""), "" if a.get("headline", "") in a.get("name", "") else a.get("headline", ""))
                        if x
                    ),
                    "url": a["url"],
                }
                for a in out.get("alternatives") or []
                if a.get("url") and a.get("verified")
            ][:5]

    async def _cookbook(self, item: Item, read: ReadRecipe, book: str, dry_run: bool) -> None:
        if not read.title or not read.ingredients or not read.instructions:
            item.status = "needs_review"
            item.message = "Couldn't read a complete recipe (title, ingredients and steps) from this photo."
            return
        source = ", ".join(x for x in (book, f"page {read.book_page}" if read.book_page else "", read.section) if x)
        description = "\n\n".join(x for x in [read.description, *read.notes] if x)
        nutrition = {k: v for k, v in (("calories", read.calories), ("carbohydrates", read.carbs)) if v}
        tags = list(dict.fromkeys([*(t for t in read.tags if t), *([book] if book else [])]))
        out = await self.create_recipe(
            name=read.title,
            ingredients=read.ingredients,
            instructions=read.instructions,
            description=description or None,
            source=source or None,
            servings=read.servings or None,
            prep_minutes=read.prep_minutes or None,
            cook_minutes=read.cook_minutes or None,
            nutrition=nutrition or None,
            tags=tags or None,
            dry_run=dry_run,
            skip_duplicates=True,
        )
        status = out.get("status")
        item.status = status if status in ("created", "duplicate", "dry_run") else "failed"
        item.mealie_url = out.get("mealie_url") or next((d.get("mealie_url") for d in out.get("duplicates") or []), None)
        item.message = out.get("message")
        item.warnings += out.get("warnings") or []

    def routes(self) -> list[tuple[str, list[str], Callable]]:
        return [
            ("/capture", ["GET"], self.page),
            ("/capture/{name:str}", ["GET"], self.asset),
            ("/capture/api/session", ["GET"], self.session),
            ("/capture/api/login", ["POST"], self.login),
            ("/capture/api/logout", ["POST"], self.logout),
            ("/capture/api/jobs", ["POST"], self.create_job),
            ("/capture/api/jobs", ["GET"], self.list_jobs),
            ("/capture/api/jobs/{job_id:str}", ["GET"], self.get_job),
            ("/capture/api/import-url", ["POST"], self.import_choice),
        ]
