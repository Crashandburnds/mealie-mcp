"""Runtime configuration, loaded from environment variables (or Docker secrets)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Docker secrets are mounted here; pydantic-settings reads a file named after each field.
_SECRETS_DIR = "/run/secrets"


def _csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        secrets_dir=_SECRETS_DIR if os.path.isdir(_SECRETS_DIR) else None,
    )

    # --- Mealie -----------------------------------------------------------
    mealie_url: str = Field(..., description="Base URL of Mealie, e.g. https://mealie.example.com")
    mealie_api_token: SecretStr = Field(..., description="Mealie API token (Bearer)")
    mealie_timeout_seconds: float = 30.0
    mealie_import_timeout_seconds: float = 120.0
    mealie_verify_tls: bool = True
    mealie_import_include_tags: bool = True
    mealie_import_include_categories: bool = False

    # --- Import policy ----------------------------------------------------
    # Domains a recipe URL may come from. "*" allows any public https site
    # (private / LAN addresses are always refused).
    import_allowed_domains: CsvList = ["hellofresh.co.uk"]
    bulk_import_max: int = 50
    dry_run: bool = False  # force every import tool into dry-run mode

    # --- HelloFresh -------------------------------------------------------
    hellofresh_base_url: str = "https://www.hellofresh.co.uk"
    hellofresh_country: str = "GB"
    hellofresh_locale: str = "en-GB"
    hellofresh_timeout_seconds: float = 20.0
    hellofresh_min_interval_seconds: float = 1.0

    # --- HTTP server ------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    mcp_path: str = "/mcp"
    public_base_url: str = "http://localhost:8000"
    forwarded_allow_ips: str = "*"
    # false = Streamable HTTP responses as SSE (keep-alive pings stop Cloudflare's 100 s timeout on long imports)
    json_response: bool = False
    rate_limit_per_second: float = 5.0
    rate_limit_burst: int = 30

    # --- Auth -------------------------------------------------------------
    auth_mode: Literal["oauth", "none"] = "oauth"
    owner_password: SecretStr | None = None
    auth_secret: SecretStr | None = None
    static_tokens: Annotated[list[SecretStr], NoDecode] = []
    oauth_allowed_redirect_hosts: CsvList = ["chatgpt.com", "chat.openai.com", "claude.ai", "claude.com"]
    # http://localhost:<port>/... redirects for native apps on your own computer (Claude Code CLI).
    oauth_allow_loopback_redirects: bool = True
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    data_dir: Path = Path("/data")

    # --- Logging ----------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    _csv_fields = field_validator("import_allowed_domains", "oauth_allowed_redirect_hosts", mode="before")(_csv)

    @field_validator("static_tokens", mode="before")
    @classmethod
    def _split_tokens(cls, value: object) -> object:
        return _csv(value)

    @field_validator("mealie_url", "public_base_url", "hellofresh_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value

    @field_validator("mcp_path")
    @classmethod
    def _path(cls, value: str) -> str:
        return "/" + value.strip("/")

    @field_validator("import_allowed_domains", "oauth_allowed_redirect_hosts")
    @classmethod
    def _lower(cls, value: list[str]) -> list[str]:
        return [v.lower().removeprefix("www.") for v in value]

    @model_validator(mode="after")
    def _check_auth(self) -> Settings:
        if self.auth_mode == "oauth":
            if not self.owner_password or len(self.owner_password.get_secret_value()) < 12:
                raise ValueError("OWNER_PASSWORD must be set (min 12 characters) when AUTH_MODE=oauth")
            if not self.auth_secret or len(self.auth_secret.get_secret_value()) < 32:
                raise ValueError("AUTH_SECRET must be set (min 32 characters) when AUTH_MODE=oauth")
        for token in self.static_tokens:
            if len(token.get_secret_value()) < 32:
                raise ValueError("each STATIC_TOKENS entry must be at least 32 characters")
        return self

    @property
    def resource_url(self) -> str:
        return f"{self.public_base_url}{self.mcp_path}"

    def secret_values(self) -> list[str]:
        """Every secret, so the log filter can redact them."""
        values = [self.mealie_api_token.get_secret_value()]
        for s in (self.owner_password, self.auth_secret, *self.static_tokens):
            if s is not None:
                values.append(s.get_secret_value())
        return [v for v in values if v]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
