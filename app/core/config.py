"""Application configuration.

Every tunable knob of the platform lives here. Values are read from the
environment (optionally via a local ``.env`` file) so that no secret ever has
to be committed to version control.

Configuration is grouped into nested sections; the environment variable for a
nested field is ``JOBINTEL_<SECTION>__<FIELD>``, e.g.::

    JOBINTEL_API__RATE_LIMIT_REQUESTS=120
    JOBINTEL_DATABASE__URL=postgresql+asyncpg://user:pass@localhost/jobintel
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class Environment(StrEnum):
    """Deployment environment."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class DatabaseSettings(BaseModel):
    """Operational database (PostgreSQL in production, SQLite for local work)."""

    url: str = f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'data' / 'jobintel.db').as_posix()}"
    echo: bool = False
    pool_size: int = Field(default=5, ge=1, le=100)
    max_overflow: int = Field(default=10, ge=0, le=100)
    pool_recycle_seconds: int = Field(default=1800, ge=60)
    pool_pre_ping: bool = True
    statement_timeout_ms: int = Field(default=30_000, ge=0)

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.url.startswith("postgresql")


class CacheSettings(BaseModel):
    """Redis-backed cache. Falls back to an in-process cache when unset."""

    url: str | None = None
    namespace: str = "jobintel"
    default_ttl_seconds: int = Field(default=300, ge=1)
    analytics_ttl_seconds: int = Field(default=900, ge=1)
    enabled: bool = True


class EventSettings(BaseModel):
    """Event bus used by the real-time pipeline."""

    backend: Literal["memory", "redis"] = "memory"
    stream_prefix: str = "jobintel:events"
    consumer_group: str = "processors"
    max_stream_length: int = Field(default=100_000, ge=1_000)
    block_timeout_ms: int = Field(default=2_000, ge=100)
    batch_size: int = Field(default=100, ge=1, le=10_000)


class SecuritySettings(BaseModel):
    """Authentication, authorization and transport hardening."""

    secret_key: SecretStr = SecretStr("")
    access_token_ttl_seconds: int = Field(default=3600, ge=60)
    api_key_header: str = "X-API-Key"
    # Bootstrap key used the first time the platform starts; rotate afterwards.
    bootstrap_admin_api_key: SecretStr | None = None
    password_hash_time_cost: int = Field(default=2, ge=1)
    password_hash_memory_cost_kib: int = Field(default=65_536, ge=8_192)
    password_hash_parallelism: int = Field(default=1, ge=1)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024)
    hsts_enabled: bool = True


class ApiSettings(BaseModel):
    """HTTP API surface."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    root_path: str = ""
    docs_enabled: bool = True
    cors_origins: list[str] = Field(default_factory=list)
    default_page_size: int = Field(default=25, ge=1, le=200)
    max_page_size: int = Field(default=200, ge=1, le=1000)
    rate_limit_requests: int = Field(default=120, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    rate_limit_enabled: bool = True
    serve_dashboard: bool = True


class IngestionSettings(BaseModel):
    """Outbound data collection behaviour."""

    user_agent: str = (
        "JobIntelBot/1.0 (+https://github.com/mojtaba-py-code; contact: ops@example.com)"
    )
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    max_response_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    max_concurrent_requests: int = Field(default=4, ge=1, le=64)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=30.0)
    per_host_delay_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    respect_robots_txt: bool = True
    allow_private_networks: bool = False
    allowed_schemes: tuple[str, ...] = ("https",)
    default_batch_size: int = Field(default=500, ge=1, le=10_000)
    job_expiry_days: int = Field(default=45, ge=1)


class NlpSettings(BaseModel):
    """NLP pipeline configuration."""

    taxonomy_path: Path = PROJECT_ROOT / "configs" / "skill_taxonomy.yaml"
    titles_path: Path = PROJECT_ROOT / "configs" / "title_rules.yaml"
    min_skill_confidence: Probability = 0.5
    min_title_confidence: Probability = 0.35
    max_description_chars: int = Field(default=60_000, ge=1_000)


class DeduplicationSettings(BaseModel):
    """Near-duplicate detection thresholds."""

    enabled: bool = True
    shingle_size: int = Field(default=5, ge=1, le=16)
    minhash_permutations: int = Field(default=128, ge=16, le=512)
    lsh_bands: int = Field(default=16, ge=1, le=128)
    near_duplicate_threshold: Probability = 0.85
    candidate_window_days: int = Field(default=30, ge=1)
    max_candidates: int = Field(default=500, ge=1, le=10_000)

    @model_validator(mode="after")
    def _bands_divide_permutations(self) -> DeduplicationSettings:
        if self.minhash_permutations % self.lsh_bands != 0:
            raise ValueError("minhash_permutations must be divisible by lsh_bands")
        return self


class AnalyticsSettings(BaseModel):
    """Trend and anomaly detection parameters."""

    trend_windows_days: tuple[int, ...] = (7, 30, 90)
    moving_average_window: int = Field(default=7, ge=2, le=90)
    rising_threshold_pct: float = Field(default=10.0, ge=0.0)
    declining_threshold_pct: float = Field(default=-10.0, le=0.0)
    volatility_cv_threshold: float = Field(default=0.6, ge=0.0)
    emerging_max_baseline_share: Probability = 0.01
    emerging_min_growth_pct: float = Field(default=100.0, ge=0.0)
    emerging_min_current_count: int = Field(default=5, ge=1)
    emerging_zscore_threshold: float = Field(default=2.0, ge=0.0)
    min_cooccurrence_support: Probability = 0.005
    min_sample_size: int = Field(default=5, ge=1)


class StorageSettings(BaseModel):
    """Local filesystem layout for raw/processed/analytical artefacts."""

    data_dir: Path = PROJECT_ROOT / "data"
    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    processed_dir: Path = PROJECT_ROOT / "data" / "processed"
    analytics_dir: Path = PROJECT_ROOT / "data" / "analytics"
    parquet_compression: Literal["snappy", "zstd", "gzip", "none"] = "snappy"
    parquet_row_group_size: int = Field(default=50_000, ge=1_000)


class AlertSettings(BaseModel):
    """Alert delivery channels."""

    enabled: bool = True
    evaluation_interval_seconds: int = Field(default=900, ge=30)
    webhook_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    webhook_allow_private_networks: bool = False
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True
    smtp_from_address: str = "alerts@jobintel.local"
    max_notifications_per_run: int = Field(default=100, ge=1)


class SchedulerSettings(BaseModel):
    """Periodic task cadence (seconds)."""

    enabled: bool = True
    ingestion_interval_seconds: int = Field(default=900, ge=30)
    processing_interval_seconds: int = Field(default=300, ge=10)
    analytics_interval_seconds: int = Field(default=3600, ge=60)
    expiry_interval_seconds: int = Field(default=21_600, ge=300)
    jitter_seconds: int = Field(default=15, ge=0)


class ObservabilitySettings(BaseModel):
    """Logging and metrics."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_file: Path | None = None
    metrics_enabled: bool = True
    slow_query_ms: int = Field(default=500, ge=1)


class Settings(BaseSettings):
    """Root settings object, assembled once per process."""

    model_config = SettingsConfigDict(
        env_prefix="JOBINTEL_",
        env_nested_delimiter="__",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Real-Time Job Market Intelligence Platform"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    events: EventSettings = Field(default_factory=EventSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    nlp: NlpSettings = Field(default_factory=NlpSettings)
    deduplication: DeduplicationSettings = Field(default_factory=DeduplicationSettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @field_validator("environment", mode="before")
    @classmethod
    def _normalize_environment(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def _harden_production(self) -> Settings:
        """Refuse to start with insecure defaults outside development."""
        if not self.security.secret_key.get_secret_value():
            if self.environment.is_production_like:
                raise ValueError("JOBINTEL_SECURITY__SECRET_KEY must be set in staging/production")
            # Ephemeral per-process key: safe for local work, useless to an attacker.
            self.security.secret_key = SecretStr(secrets.token_urlsafe(48))

        if self.environment.is_production_like:
            if self.debug:
                raise ValueError("debug mode must be disabled in staging/production")
            if "*" in self.api.cors_origins:
                raise ValueError("wildcard CORS origins are not allowed in staging/production")
            if len(self.security.secret_key.get_secret_value()) < 32:
                raise ValueError("secret_key must be at least 32 characters")
            if self.database.is_sqlite:
                raise ValueError("SQLite is not supported in staging/production; use PostgreSQL")
            if self.ingestion.allow_private_networks:
                raise ValueError(
                    "outbound requests to private networks are forbidden in production"
                )

        if self.api.default_page_size > self.api.max_page_size:
            raise ValueError("default_page_size cannot exceed max_page_size")
        return self

    def ensure_directories(self) -> None:
        """Create the local data directories the platform writes to."""
        for path in (
            self.storage.data_dir,
            self.storage.raw_dir,
            self.storage.processed_dir,
            self.storage.analytics_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests that patch the environment)."""
    get_settings.cache_clear()
