"""Structured (JSON) logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
_QUERY_SECRET_RE = re.compile(r"(?i)\b(code|access_token|refresh_token|token|password|client_secret)=([^&\s\"']+)")


class Redactor:
    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = sorted({s for s in (secrets or []) if len(s) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, "***")
        text = _BEARER_RE.sub(r"\1***", text)
        text = _JWT_RE.sub("***", text)
        return _QUERY_SECRET_RE.sub(r"\1=***", text)


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log args
            message = str(record.msg)
        record.msg = self.redactor(message)
        record.args = None
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            record.fields = {k: self.redactor(v) if isinstance(v, str) else v for k, v in extra.items()}
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "json", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter(Redactor(secrets)))
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Route uvicorn / fastmcp loggers through the same redacting handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastmcp", "mcp", "httpx"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # httpx logs full URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields: object) -> None:
    logger.log(level, msg, extra={"fields": fields})
