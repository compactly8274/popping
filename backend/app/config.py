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
    # Optional PAT for the github_releases source. Unauthenticated = 60 req/hr;
    # with a PAT = 5000 req/hr. Recommended if you watch many repos.
    github_token: str = ""

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

    # --- Local fallback auth (optional) -------------------------------------
    # When local_auth_enabled=true, a hardcoded local account is available
    # at /auth/local as a backup when the IdP is down. The account has no
    # role or scope — it's the same level of access as an OIDC login.
    local_auth_enabled: bool = False
    local_user_name: str = ""
    local_user_password_hash: str = ""  # bcrypt hash; generate with bcrypt.hashpw
    local_user_email: str = ""

    # --- Local bypass (loopback OR LAN) ----------------------------------
    # When local_auth_bypass=true, requests from a private network
    # address (loopback, RFC1918, link-local, IPv6 ULA) are treated as
    # authenticated with a synthetic 'local-bypass' user. Useful for
    # headless LAN deployments without a full OIDC stack.
    #
    # SECURITY: the IP is taken from the TCP peer only. X-Forwarded-For
    # is ignored — it can be spoofed by any client and would let a LAN
    # attacker claim a loopback identity. If you need to run behind a
    # reverse proxy, terminate TLS at the proxy and have it speak to
    # the backend on a private interface, or add explicit proxy-trust
    # support (out of scope for the local bypass).
    local_auth_bypass: bool = False

    # --- Session hygiene ---------------------------------------------------
    # How often to delete expired rows from the sessions table.
    session_purge_interval_seconds: int = 3600

    # --- Phase 2: embeddings ------------------------------------------------
    # sentence-transformers model used to embed entry text at ingest time.
    # 384-dim output, matches the Vector(384) column on entries. Override
    # with a smaller model on memory-constrained hosts.
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_batch_size: int = 64
    # Set to false to skip the embedding pipeline entirely (e.g. on a
    # memory-constrained host). Entries get NULL embedding; personal
    # scoring degenerates to recency + source weight.
    embedding_enabled: bool = True

    # --- Phase 2: composite scoring weights ---------------------------------
    # final = w_recency * recency + w_personal * personal + w_source * source_weighted
    # Weights don't have to sum to 1 — the values are weights, not
    # probabilities — but the defaults do sum to 1.
    scoring_weight_recency: float = 0.4
    scoring_weight_personal: float = 0.4
    scoring_weight_source: float = 0.2

    # --- Phase 2: convergence boost ----------------------------------------
    # Cross-source story clusters (same normalized title in 24h) get a
    # multiplicative boost. Tweak to taste.
    convergence_window_hours: int = 24
    convergence_boost_2: float = 1.10
    convergence_boost_3plus: float = 1.20

    # --- Phase 4: The Brief + notifications -------------------------------
    # UTC hour at which the scheduler generates the daily brief. Brief is
    # idempotent — generating it twice on the same day just overwrites the
    # row. Set to -1 to disable the scheduled daily brief (manual only).
    brief_schedule_hour: int = 8
    # CVSS threshold for the post-ingest CVE notification. Default 7.0
    # (HIGH severity). Set to 0 to alert on every CVE ingest.
    cve_notify_min_cvss: float = 7.0
    # Convergence threshold for the periodic alert job — minimum number
    # of distinct sources a slug must appear in to trigger an alert.
    convergence_notify_threshold: int = 2
    # How often the convergence-check job runs. Cheap (one GROUP BY),
    # but doesn't need to be tight.
    convergence_check_interval_minutes: int = 15

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