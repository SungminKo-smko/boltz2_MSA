from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PlatformSettings(BaseSettings):
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

    # Azure Blob Storage
    blob_backend: Literal["local", "azure"] = "local"
    local_storage_root: Path = Path(".local-storage")
    azure_storage_account_url: str | None = None
    azure_storage_connection_string: str | None = None
    azure_storage_account_name: str | None = None
    azure_storage_account_key: str | None = None

    # Azure Service Bus
    queue_backend: Literal["local", "azure"] = "local"
    service_bus_connection_string: str | None = None

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


@lru_cache(maxsize=1)
def get_settings() -> PlatformSettings:
    return PlatformSettings()
