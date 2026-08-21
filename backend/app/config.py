from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import DEFAULT_TIMEZONE, VERSION

RATE_SOURCE_OPERATION_TIMEOUT_MAX_SECONDS = 25.0
DEFAULT_SCE_RATE_SOURCE_URL = (
    "https://www.sce.com/save-money/rates-financing/residential-rate-plans/tiered-rate-plan"
)
_ALLOWED_SCE_RATE_PATH_PREFIXES = (
    "/save-money/rates-financing/",
    "/regulatory/",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PM_", env_file=None, extra="forbid")

    env: Literal["production", "development", "test"] = "production"
    service_role: Literal["api", "worker", "migrate"] = "api"
    database_url: str | None = None
    database_host: str = "postgres"
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = "powermeter"
    database_user: str = "powermeter"
    database_password_file: Path | None = None
    public_origin: AnyHttpUrl = AnyHttpUrl("https://power-monitor.home.arpa:8443")
    timezone: str = DEFAULT_TIMEZONE
    session_secret: SecretStr | None = None
    session_secret_file: Path | None = None
    field_encryption_key: SecretStr | None = None
    field_encryption_key_file: Path | None = None
    ota_manifest_key_file: Path | None = None
    log_level: str = "INFO"
    log_dir: Path | None = None
    log_retention_days: int = Field(default=90, ge=1, le=3650)
    backup_status_dir: Path = Path("/data/backup-status")
    release_version: str = VERSION
    release_revision: str = "unknown"
    build_revision: str = "unknown"
    build_time: str = "unknown"
    api_image_digest: str = "unknown"
    frontend_version: str = "unknown"
    frontend_revision: str = "unknown"
    frontend_build_time: str = "unknown"
    frontend_image_digest: str = "unknown"
    frontend_static_asset_id: str = "unknown"
    expected_database_revision: str = "20260821_0019"
    bill_import_timeout_seconds: int = Field(default=30, ge=5, le=60)
    rate_artifact_dir: Path = Path("/data/rate-source-artifacts")
    firmware_dir: Path = Path("/data/firmware")
    session_absolute_hours: int = Field(default=12, ge=1, le=168)
    session_idle_minutes: int = Field(default=30, ge=1, le=1440)
    login_failure_window_minutes: int = Field(default=15, ge=1, le=1440)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)
    login_principal_max_failures: int = Field(default=5, ge=2, le=100)
    login_source_max_failures: int = Field(default=50, ge=2, le=10_000)
    allowed_sce_hosts: tuple[str, ...] = ("www.sce.com", "sce.com")
    sce_rate_source_url: AnyHttpUrl = AnyHttpUrl(DEFAULT_SCE_RATE_SOURCE_URL)
    rate_source_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    rate_source_read_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    rate_source_total_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    rate_source_max_bytes: int = Field(default=5_000_000, ge=1, le=10_000_000)
    rate_source_max_header_bytes: int = Field(default=65_536, ge=1_024, le=131_072)
    rate_source_max_header_count: int = Field(default=100, ge=10, le=200)
    rate_source_max_redirects: int = Field(default=3, ge=0, le=5)
    rate_source_due_limit: int = Field(default=10, ge=1, le=100)
    rate_source_retry_attempts: int = Field(default=3, ge=1, le=5)
    rate_source_retry_backoff_seconds: float = Field(default=0.25, ge=0, le=5)
    sce_catalog_crawl_max_documents: int = Field(default=24, ge=2, le=64)
    sce_catalog_crawl_max_depth: int = Field(default=2, ge=1, le=4)
    sce_catalog_crawl_max_total_bytes: int = Field(
        default=25_000_000,
        ge=1_000_000,
        le=50_000_000,
    )
    sce_catalog_crawl_request_delay_seconds: float = Field(default=0.1, ge=0, le=2.0)
    sce_catalog_parse_timeout_seconds: float = Field(default=2.0, ge=0.05, le=10.0)
    rate_source_operation_timeout_seconds: float = Field(
        default=20.0,
        ge=0.05,
        le=RATE_SOURCE_OPERATION_TIMEOUT_MAX_SECONDS,
    )

    @field_validator("allowed_sce_hosts", mode="before")
    @classmethod
    def official_sce_hosts_only(cls, value: object) -> object:
        if not isinstance(value, tuple | list):
            return value
        normalized = tuple(str(host).lower().rstrip(".") for host in value)
        if not normalized or not set(normalized) <= {"www.sce.com", "sce.com"}:
            raise ValueError("PM_ALLOWED_SCE_HOSTS may contain only official SCE hosts")
        return normalized

    @field_validator(
        "session_secret_file",
        "field_encryption_key_file",
        "ota_manifest_key_file",
        "database_password_file",
        "log_dir",
        mode="before",
    )
    @classmethod
    def empty_path_is_none(cls, value: object) -> object:
        return None if value in (None, "") else value

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        if self.session_idle_minutes > self.session_absolute_hours * 60:
            raise ValueError("PM_SESSION_IDLE_MINUTES cannot exceed PM_SESSION_ABSOLUTE_HOURS")
        if self.login_source_max_failures < self.login_principal_max_failures:
            raise ValueError(
                "PM_LOGIN_SOURCE_MAX_FAILURES cannot be less than PM_LOGIN_PRINCIPAL_MAX_FAILURES"
            )
        if self.rate_source_total_timeout_seconds <= self.rate_source_connect_timeout_seconds:
            raise ValueError("PM_RATE_SOURCE_TOTAL_TIMEOUT_SECONDS must exceed the connect timeout")
        source_url = self.sce_rate_source_url
        source_host = (source_url.host or "").lower().rstrip(".")
        if (
            source_url.scheme != "https"
            or source_host not in {"www.sce.com", "sce.com"}
            or source_url.port not in (None, 443)
            or source_url.username is not None
            or source_url.password is not None
            or source_url.fragment is not None
            or source_url.query is not None
            or not (source_url.path or "").startswith(_ALLOWED_SCE_RATE_PATH_PREFIXES)
        ):
            raise ValueError(
                "PM_SCE_RATE_SOURCE_URL must be ordinary HTTPS on an allowed SCE host/path"
            )
        if self.env == "production" and self.service_role == "api":
            if not (self.session_secret or self.session_secret_file):
                raise ValueError("production requires PM_SESSION_SECRET_FILE")
            if not (self.field_encryption_key or self.field_encryption_key_file):
                raise ValueError("production requires PM_FIELD_ENCRYPTION_KEY_FILE")
            if not self.ota_manifest_key_file:
                raise ValueError("production requires PM_OTA_MANIFEST_KEY_FILE")
            if self.public_origin.scheme != "https":
                raise ValueError("production public origin must use HTTPS")
        return self

    def read_secret(self, inline: SecretStr | None, file_path: Path | None, name: str) -> bytes:
        if inline is not None:
            raw = inline.get_secret_value().encode()
        elif file_path is not None:
            raw = file_path.read_bytes().strip()
        elif self.env == "test":
            raw = ("test-only-" + name + "-not-for-production").encode()
        else:
            raise RuntimeError(f"missing required secret: {name}")
        if not raw:
            raise RuntimeError(f"empty required secret: {name}")
        try:
            decoded = base64.b64decode(raw, validate=True)
        except ValueError:
            decoded = raw
        if len(decoded) < 32:
            raise RuntimeError(f"{name} must contain at least 32 bytes")
        return decoded

    @property
    def session_key(self) -> bytes:
        return self.read_secret(self.session_secret, self.session_secret_file, "session_secret")

    @property
    def master_key(self) -> bytes:
        return self.read_secret(
            self.field_encryption_key,
            self.field_encryption_key_file,
            "field_encryption_key",
        )[:32]

    @property
    def ota_manifest_key(self) -> bytes:
        return self.read_secret(None, self.ota_manifest_key_file, "ota_manifest_key")

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        password = ""
        if self.database_password_file:
            password = self.database_password_file.read_text(encoding="utf-8").strip()
        if self.env == "production" and not password:
            raise RuntimeError("production requires PM_DATABASE_PASSWORD_FILE")
        credentials = quote_plus(self.database_user)
        if password:
            credentials += ":" + quote_plus(password)
        return (
            f"postgresql+asyncpg://{credentials}@{self.database_host}:"
            f"{self.database_port}/{quote_plus(self.database_name)}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
