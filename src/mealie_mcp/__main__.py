"""Entry point: python -m mealie_mcp"""

from __future__ import annotations

import logging
import os

import uvicorn
from pydantic import ValidationError

from .config import Settings
from .logging_setup import configure_logging


def config_problems(exc: ValidationError) -> list[dict[str, str]]:
    """Setting names and messages only; never input values (they may be secrets)."""
    problems = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]).upper() or "SETTINGS"
        problems.append({"setting": field, "problem": err["msg"].removeprefix("Value error, ")})
    return problems


def config_error_app(problems: list[dict[str, str]]):
    """Stand-in app served when configuration is invalid.

    Exiting would make Docker/TrueNAS discard the container and its logs, leaving no
    clue on a web UI. Staying up lets the owner open /health and read what to fix.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    body = {
        "status": "config_error",
        "message": "mealie-mcp is not configured correctly. Fix these settings in the app's environment and restart.",
        "problems": problems,
    }

    async def handler(request):
        return JSONResponse(body, status_code=503)

    return Starlette(routes=[Route("/{path:path}", handler, methods=["GET", "POST", "HEAD"])])


def main() -> None:
    log = logging.getLogger("mealie_mcp")
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        configure_logging()
        problems = config_problems(exc)
        for p in problems:
            log.error("configuration error: %s: %s", p["setting"], p["problem"])
        try:
            port = int(os.environ.get("PORT", "8000"))
        except ValueError:
            port = 8000
        log.error("serving configuration errors on port %s (open /health to read them)", port)
        uvicorn.run(config_error_app(problems), host="0.0.0.0", port=port, log_config=None, access_log=False)
        return

    configure_logging(settings.log_level, settings.log_format, settings.secret_values())
    from .server import create_app

    app = create_app(settings)
    logging.getLogger("mealie_mcp").info(
        "starting mealie-mcp on %s:%s, MCP endpoint %s, auth=%s",
        settings.host,
        settings.port,
        settings.resource_url,
        settings.auth_mode,
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_config=None,
        access_log=False,
        timeout_keep_alive=30,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
