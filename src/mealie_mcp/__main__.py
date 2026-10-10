"""Entry point: python -m mealie_mcp"""

from __future__ import annotations

import logging
import sys

import uvicorn
from pydantic import ValidationError

from .config import Settings
from .logging_setup import configure_logging


def main() -> None:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # Print field names and messages only; never echo input values (they may be secrets).
        configure_logging()
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"]) or "settings"
            logging.getLogger("mealie_mcp").error("configuration error: %s: %s", field.upper(), err["msg"])
        sys.exit(2)

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
