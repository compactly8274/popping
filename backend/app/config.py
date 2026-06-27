"""Application configuration via pydantic-settings.

Reads from environment variables and (when readable) the .env file. Mirrors
the verificationrotation pattern: if .env exists but isn't readable (wrong
permissions in Docker), skip it rather than crashing on startup.
"""

import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_env_file = Path(".env")
_readable_env_file: Path | None = (
    _env_file if (_env_file.exists() and os.access(_env_file, os.R_OK)) else None
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_readable_env_file,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database -----------------------------------------------------------
    postgres_user: str = "popping"
    postgres_password: str = "popping"
    postgres_db: str = "popping"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # --- Redis --------------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"

    # --- LLM (phase 2+, listed here so the env file has one home) ----------
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model_scoring: str = "llama3.1:8b"
    ollama_model_brief: str = "llama3.1:8b"
    ollama_model_embedding: str = "all-minilm"

    anthropic_api_key: str = ""
    claude_model_scoring: str = "claude-haiku-4-5"
    claude_model_brief: str = "claude-sonnet-4-6"

    openai_api_key: str = ""
    openai_model_scoring: str = "gpt-4o-mini"
    openai_model_brief: str = "gpt-4o"

    groq_api_key: str = ""
    groq_model_scoring: str = "llama-3.1-8b-instant"
    groq_model_brief: str = "llama-3.1-70b-versatile"

    # --- Source API keys (phase 2+) ----------------------------------------
    keepa_api_key: str = ""
    podcast_index_api_key: str = ""
    podcast_index_api_secret: str = ""

    # --- Notifications (phase 2+) -----------------------------------------
    pushover_user_key: str = ""
    pushover_app_token: str = ""
    apprise_url: str = ""

    # --- OIDC (single-tenant, optional) ------------------------------------
    # When oidc_enabled=False (the default), the backend ships no auth and
    # the /auth/* routes aren't mounted — single-user deployments don't need
    # to think about it. Flip on for any LAN/public exposure.
    oidc_enabled: bool = False
    oidc_issuer: str = ""            # e.g. https://auth.example.com
    oidc_client_id: str = ""
    oidc_scopes: str = "openid email profile"
    public_url: str = ""             # e.g. https://popping.example.com
    session_secret: str = ""         # required when oidc_enabled (openssl rand -hex 32)
    session_ttl_seconds: int = 28800  # 8 h
    session_cookie_name: str = "popping_session"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
logger = logging.getLogger("popping")

if _readable_env_file is None and _env_file.exists():
    logger.warning(
        ".env file exists but is not readable (permission denied). "
        "Configuration will be loaded from environment variables only. "
        "Fix with: chmod 644 .env"
    )