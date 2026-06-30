"""Application configuration via pydantic-settings.

Reads from environment variables and (when readable) the .env file. Mirrors
the verificationrotation pattern: if .env exists but isn't readable (wrong
permissions in Docker), skip it rather than crashing on startup.
"""

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_env_file = Path(".env")
_readable_env_file: Path | None = (
    _env_file if (_env_file.exists() and os.access(_env_file, os.R_OK)) else None
)


# Values that look like ".env template placeholders" — common patterns
# users copy/paste from .env.example without filling in. Treat them as
# "not configured" so the LLM router doesn't try to call a provider
# with a literal "redacted" / "changeme" key.
#
# Match (case-insensitive, anchored): "redacted", "changeme", "your-*-here",
# "replace-me", "todo", "xxx+", "<...>", "{...}", "[...]", "sk-...", all
# whitespace, or a single dash. Pydantic settings reads these as strings;
# any truthy non-empty string would otherwise make the LLM router think
# the provider is configured and try to call it.
_PLACEHOLDER_RE = re.compile(
    r"^\s*("
    r"redacted|changeme|change-?me|your[-_][a-z0-9_-]+-here|"
    r"replace[-_]?me|todo|xxx+|sk-xxx+|example|dummy|"
    r"<[^>]+>|\[[^\]]+\]|\{[^}]+\}|---+|\s*"
    r")\s*$",
    re.IGNORECASE,
)


def _is_placeholder(value: str) -> bool:
    """True if ``value`` looks like an unfilled .env template placeholder.

    Empty strings and whitespace-only strings are NOT placeholders —
    those are the canonical "not configured" form. Placeholders are the
    sneaky middle case: a non-empty string that's still semantically
    unset. See ``_PLACEHOLDER_RE``.
    """
    return bool(_PLACEHOLDER_RE.match(value))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_readable_env_file,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("*", mode="before")
    @classmethod
    def _strip_placeholders(cls, value):
        """Normalise obvious .env placeholders to empty strings so the
        LLM router's truthy-key check doesn't pick them up. Real keys
        (Anthropic ``sk-ant-…``, OpenAI ``sk-…``, Groq ``gsk_…``) are
        opaque enough that they won't match ``_PLACEHOLDER_RE``."""
        if isinstance(value, str) and _is_placeholder(value):
            return ""
        return value

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

    # Ollama Cloud (https://ollama.com) — paid SaaS that fronts the same
    # /api/generate endpoint as local Ollama. Select via OLLAMA_CLOUD_API_KEY;
    # model name is read from OLLAMA_CLOUD_MODEL_SCORING / _BRIEF (defaults
    # to the local Ollama model name since the same tags are typically
    # available — e.g. llama3.1:8b, gpt-oss:120b).
    ollama_cloud_api_key: str = ""
    ollama_cloud_model_scoring: str = ""
    ollama_cloud_model_brief: str = ""

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
    # Optional Hydra Reddit client-server URL. When set, the Reddit source
    # plugin (``app.sources.dynamic_reddit``) routes per-subreddit listings
    # through this server instead of hitting Reddit directly, and the
    # background cross-reference sweep in ``app.scheduler`` queries it for
    # "discussed on Reddit" footers on every other entry. Empty string =
    # fall back to direct mode (see ``reddit_direct_disabled`` below).
    reddit_hydra_url: str = ""
    # Bearer token for the Hydra server. Empty = unauthenticated Hydra.
    # The token rides on every Hydra call via the shared httpx client
    # (``app.reddit_client``), never per-stream — keeps the credentials
    # scoped to a single client and out of the assets client.
    reddit_hydra_token: str = ""
    # When ``reddit_hydra_url`` is empty, ``app.reddit_client`` falls
    # back to scraping Reddit's public JSON endpoints directly from
    # the backend container's IP (per-process token-bucket
    # rate-limited, contact-stamped User-Agent). Set this to True
    # to disable the direct path entirely — useful for deployments
    # where the TrueNAS IP is on a residential ISP that Reddit
    # throttles, or when the operator wants to enforce a strict
    # "no Reddit traffic from this network" rule. Defaults to
    # False so a fresh deploy works out of the box; an empty
    # ``reddit_hydra_url`` + this set to True is the "feature
    # fully off" state and surfaces as
    # ``reddit_client: disabled`` at startup.
    reddit_direct_disabled: bool = False

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
    # CIDR allow-list for the local bypass. Default is loopback-only
    # (127.0.0.0/8, ::1/128). Set to a comma-separated list of CIDRs
    # to grant bypass to additional networks — e.g. "127.0.0.0/8,
    # 10.0.0.0/8, ::1/128" for a private LAN. **Any host on the
    # allow-listed networks can authenticate without a password**;
    # only enable this on networks you trust.
    #
    # The previous default included RFC1918 + link-local + ULA, which
    # silently auto-granted bypass to any reverse-proxy peer that
    # talked to the backend on a private interface (Docker bridge,
    # k8s pod CIDRs, etc.). The loopback-only default closes that hole.
    local_bypass_allowed_cidrs: str = "127.0.0.0/8,::1/128"

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
    # final = w_recency * recency + w_personal * personal
    #       + w_source * source_weighted + w_engagement * engagement
    # Weights don't have to sum to 1 — the values are weights, not
    # probabilities — but the defaults do sum to 1.
    #
    # Engagement (votes/comments) joins recency+personal+source as a
    # fourth component. Sources that don't ship engagement signals
    # (BBC, NVD, CISA, Wikipedia) get a zero contribution here, so
    # re-weighting doesn't move them; engagement-aware sources (HN,
    # RFD, Reddit, GitHub) get a lift proportional to their signal.
    scoring_weight_recency: float = 0.30
    scoring_weight_personal: float = 0.30
    scoring_weight_source: float = 0.15
    scoring_weight_engagement: float = 0.25

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
    # Lookback window for the brief generator: only entries ingested within
    # this many hours are considered. Excludes historical content (e.g.
    # Wikipedia "on this day") that was ingested today but published long
    # ago. Overridable at runtime via ``brief.window_hours`` in
    # app_settings — same pattern as the LLM knobs.
    brief_window_hours: int = 24
    # CVSS threshold for the post-ingest CVE notification. Default 7.0
    # (HIGH severity). Set to 0 to alert on every CVE ingest.
    cve_notify_min_cvss: float = 7.0
    # Convergence threshold for the periodic alert job — minimum number
    # of distinct sources a slug must appear in to trigger an alert.
    convergence_notify_threshold: int = 2
    # How often the convergence-check job runs. Cheap (one GROUP BY),
    # but doesn't need to be tight.
    convergence_check_interval_minutes: int = 15

    # Retention window for ``notification_dedup``. The table grows
    # by one row per unique CVE URL / convergence slug that ever
    # fired (the ``ON CONFLICT DO UPDATE`` only bumps the timestamp
    # on re-fires); without a prune job it grows unbounded. 30 days
    # is long enough that a CVE re-reported tomorrow still dedups
    # against this week's ledger; short enough that a vuln-heavy
    # install tops out around "two weeks of CVE volume + a few
    # hundred convergence slugs". Set to 0 to disable pruning
    # (keep full history). Override with
    # POPPING_NOTIFICATION_DEDUP_RETENTION_DAYS.
    notification_dedup_retention_days: int = 30

    # --- Runtime settings (LLM picker) -----------------------------------
    # TTL for the /api/llm/tags response cache. The picker fetches this
    # on drawer-open; 1 h is plenty because the user's available models
    # change rarely (account-level quotas, not per-call). Override with
    # POPPING_LLM_TAGS_CACHE_TTL_SECONDS if you need tighter refresh.
    llm_tags_cache_ttl_seconds: int = 3600

    # --- Asset cache (favicons + thumbnails) ------------------------------
    # Where the ingest pipeline writes downloaded images. Must be
    # readable by the StaticFiles mount at /assets and writable by the
    # backend. Default /app/assets matches the named volume in
    # docker-compose.yml. Override with POPPING_ASSETS_DIR.
    assets_dir: str = "/app/assets"

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