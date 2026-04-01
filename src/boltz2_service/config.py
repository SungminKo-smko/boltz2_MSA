from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from platform_core.config import PlatformCoreSettings


class Boltz2Settings(PlatformCoreSettings):
    # Azure Blob Storage
    blob_backend: str = "local"
    local_storage_root: str = ".local-storage"
    azure_storage_account_url: str | None = None
    azure_storage_connection_string: str | None = None
    azure_storage_account_name: str | None = None
    azure_storage_account_key: str | None = None

    # Azure Service Bus
    queue_backend: str = "local"
    service_bus_connection_string: str | None = None

    # MCP OAuth
    mcp_issuer_url: str = "https://boltz2-api.politebay-55ff119b.westus3.azurecontainerapps.io/mcp"

    # Azure containers / queue
    azure_input_container: str = "boltz2-inputs"
    azure_results_container: str = "boltz2-results"
    service_bus_queue_name: str = "boltz2-predict-jobs"

    # Boltz-2 runtime
    boltz2_bin: str = "boltz"
    boltz2_cache_dir: str = "/cache"
    boltz2_run_timeout_seconds: int = 14400
    boltz2_validate_timeout_seconds: int = 120
    boltz2_devices: int = Field(default=1, ge=1)

    # MSA server
    msa_server_url: str = "https://api.colabfold.com"
    msa_server_username: str | None = None
    msa_server_password: str | None = None

    # Defaults
    default_max_diffusion_samples: int = 10

    # ACA (optional)
    aca_subscription_id: str | None = None
    aca_resource_group: str | None = None
    aca_worker_job_name: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Boltz2Settings:
    return Boltz2Settings()


@lru_cache(maxsize=1)
def get_blob_storage():
    from platform_core.services.blob_storage import BlobStorageService

    settings = get_settings()
    return BlobStorageService(
        settings,
        input_container=settings.azure_input_container,
        results_container=settings.azure_results_container,
    )


@lru_cache(maxsize=1)
def get_queue_service():
    from platform_core.services.queue import QueueService

    settings = get_settings()
    return QueueService(settings, queue_name=settings.service_bus_queue_name)
