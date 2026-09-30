"""Application configuration (OPS-2: everything through environment variables)."""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=os.environ.get("RF_ENV_FILE", ".env"), extra="ignore")

    app_name: str = "Remote Flow"
    app_version: str = "0.1.0"
    environment: str = "development"
    public_url: str = "http://localhost:8080"

    # Infrastructure
    database_url: str = "sqlite+aiosqlite:///./remote_flow.db"
    redis_url: str | None = None
    storage_dir: str = "./storage"

    # Secrets
    secret_key: str = "dev-secret-key-change-me"
    master_key: str | None = None  # base64-encoded 32 bytes

    #: SEC-9: comma-separated IPs/CIDRs allowed to use manager endpoints (empty = any).
    manager_ip_allowlist: str = ""

    # Sessions (AUTH-5)
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 7
    cookie_secure: bool = False
    cookie_domain: str | None = None
    cookie_samesite: str = "lax"
    csrf_cookie_name: str = "rf_csrf"
    access_cookie_name: str = "rf_access"
    refresh_cookie_name: str = "rf_refresh"

    # Pipeline
    worker_stale_after_s: int = 180
    llm_concurrency: int = 16
    render_concurrency: int = 1
    llm_timeout_s: int = 600
    render_timeout_s: int = 180
    conversion_timeout_s: int = 60
    # RB_PDF_BACKEND (deploy naming) / PDF_BACKEND: unoserver | soffice | word | stub
    pdf_backend: str = Field(
        default="unoserver", validation_alias=AliasChoices("RB_PDF_BACKEND", "PDF_BACKEND")
    )
    max_llm_attempts: int = 10
    max_render_attempts: int = 3
    lease_grace_s: int = 30
    dispatch_sweep_seconds: int = 30
    lease_watchdog_seconds: int = 30
    release_scan_seconds: int = 60
    outbox_relay_seconds: int = 5
    retention_sweep_seconds: int = 24 * 3600
    disk_check_seconds: int = 300
    stats_refresh_seconds: int = 300
    #: CONC-2: how often the dynamic-pool controller measures and re-decides.
    pool_tick_seconds: int = 10
    #: CONC-2: who runs the LLM stage. ``celery`` - one prefork child per provider
    #: call (~270 MB each); ``runner`` - the async runner (``manage.py llm-runner``)
    #: that keeps many calls in flight inside one process.
    llm_executor: str = "celery"
    #: Most provider calls one runner process keeps in flight, sized to its
    #: container's memory limit; the Manager's number is clamped to it.
    llm_runner_max_concurrency: int = 150
    #: How often the runner looks for claimable builds while nothing finished.
    llm_runner_poll_s: float = 1.0
    #: Longest wait for a provider slot before the build is left for the next pass.
    llm_runner_slot_wait_s: float = 5.0
    #: How long a stopping runner lets in-flight calls finish (keep the container's
    #: stop_grace_period above it).
    llm_runner_drain_s: int = 600
    #: The runner's own DB pool. Provider calls never hold a connection, so a small
    #: pool serves hundreds of calls.
    llm_runner_db_pool_size: int = 8
    llm_runner_db_max_overflow: int = 8
    provider_default_max_concurrency: int = 8
    provider_default_rpm: int = 60

    # Mock provider (OPS-7)
    mock_llm_delay_ms_min: int = 1000
    mock_llm_delay_ms_max: int = 10000
    mock_llm_fail_rate: float = 0.0
    mock_render_fail_rate: float = 0.0

    # Development conveniences
    inline_pipeline: bool = False
    auto_create_schema: bool = False
    autocreate_tables_on_start: bool = False
    seed_on_start: bool = False

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def llm_runner_enabled(self) -> bool:
        """CONC-2: the LLM stage is run by the async runner, not Celery."""
        return (self.llm_executor or "").strip().lower() == "runner"

    def storage_path(self) -> Path:
        return Path(self.storage_dir).expanduser().resolve()

    def resolved_master_key(self) -> bytes:
        """Return the 32-byte AES key (SET-5).

        In development a deterministic key is derived from the secret so the app
        boots without a MASTER_KEY; production requires an explicit key.
        """
        if self.master_key:
            raw = self.master_key.strip()
            try:
                decoded = base64.b64decode(raw, validate=True)
                if len(decoded) == 32:
                    return decoded
            except Exception:  # noqa: BLE001 - fall through to hashing
                pass
            return hashlib.sha256(raw.encode("utf-8")).digest()
        if self.is_production:
            raise RuntimeError("MASTER_KEY must be set in production (SET-5)")
        seed = os.environ.get("RF_DEV_MASTER_SEED", self.secret_key)
        return hashlib.sha256(f"remote-flow-dev::{seed}".encode("utf-8")).digest()


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
