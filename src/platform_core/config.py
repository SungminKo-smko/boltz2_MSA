from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PlatformCoreSettings(BaseSettings):
    """Core settings shared across all services.

    Service-specific settings should subclass this and add their own fields.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "bioai-platform"
    app_env: Literal["development", "test", "staging", "production"] = "development"
    api_key_header: str = "x-api-key"

    # Supabase
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_jwt_secret: SecretStr = SecretStr("")

    # Database (Supabase PostgreSQL direct connection)
    database_url: str = "sqlite+pysqlite:///./bioai_platform.db"

    # Domain rules
    auto_approve_domains: list[str] = ["shaperon.com"]

    # Rate limits
    default_daily_job_limit: int = Field(default=20, ge=1)
    default_max_concurrent_jobs: int = Field(default=2, ge=1)

    # Job lifecycle
    job_heartbeat_timeout_seconds: int = Field(default=900, ge=60)
    job_queued_timeout_seconds: int = Field(default=1800, ge=60)
    job_heartbeat_interval_seconds: int = Field(default=60, ge=10)

    # Artifact TTL
    max_upload_url_ttl_seconds: int = Field(default=3600, ge=60)
    max_result_url_ttl_seconds: int = Field(default=3600, ge=60)


# Backward compatibility alias
PlatformSettings = PlatformCoreSettings


# --- Settings Registry ---
# Services call register_settings() at startup; platform_core reads via get_settings().

_settings_instance: PlatformCoreSettings | None = None


def register_settings(settings: PlatformCoreSettings) -> None:
    """Register the service-specific settings instance for platform_core to use."""
    global _settings_instance
    _settings_instance = settings


def reset_settings() -> None:
    """Clear the registered settings (useful for tests with monkeypatch)."""
    global _settings_instance
    _settings_instance = None
    _default_settings.cache_clear()


@lru_cache(maxsize=1)
def _default_settings() -> PlatformCoreSettings:
    return PlatformCoreSettings()


def get_settings() -> PlatformCoreSettings:
    """Return the registered settings, or create default PlatformCoreSettings."""
    if _settings_instance is not None:
        return _settings_instance
    return _default_settings()
