from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas

from platform_core.config import PlatformSettings


class BlobStorageService:
    def __init__(self, settings: PlatformSettings, input_container: str, results_container: str) -> None:
        self.settings = settings
        self.input_container = input_container
        self.results_container = results_container
        self.root = settings.local_storage_root
        self.account_url = (settings.azure_storage_account_url or "").rstrip("/")
        if settings.blob_backend == "azure":
            self.client = self._build_azure_client()
        else:
            self.client = None
            self.root.mkdir(parents=True, exist_ok=True)

    def _build_azure_client(self) -> BlobServiceClient:
        if self.settings.azure_storage_connection_string:
            return BlobServiceClient.from_connection_string(self.settings.azure_storage_connection_string)
        if self.settings.azure_storage_account_url and self.settings.azure_storage_account_key:
            return BlobServiceClient(
                account_url=self.settings.azure_storage_account_url,
                credential=self.settings.azure_storage_account_key,
            )
        raise ValueError(
            "Azure blob storage requires either AZURE_STORAGE_CONNECTION_STRING or "
            "AZURE_STORAGE_ACCOUNT_URL with AZURE_STORAGE_ACCOUNT_KEY."
        )

    def build_asset_blob_path(self, relative_path: str) -> str:
        return f"assets/{uuid4()}/{relative_path}"

    def create_upload_target(self, blob_path: str, content_type: str) -> tuple[str, datetime]:
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.max_upload_url_ttl_seconds)
        if self.settings.blob_backend == "azure":
            container_client = self.client.get_container_client(self.input_container)
            try:
                container_client.create_container()
            except Exception:
                pass
            sas = generate_blob_sas(
                account_name=self.settings.azure_storage_account_name,
                container_name=self.input_container,
                blob_name=blob_path,
                account_key=self.settings.azure_storage_account_key,
                permission=BlobSasPermissions(write=True, create=True),
                expiry=expires_at,
                content_type=content_type,
            )
            url = f"{self.account_url}/{self.input_container}/{blob_path}?{sas}"
            return url, expires_at

        local_path = self.root / self.input_container / blob_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        return f"file://{local_path}", expires_at

    def generate_download_url(self, container: str, blob_path: str) -> str:
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.max_result_url_ttl_seconds)
        if self.settings.blob_backend == "azure":
            sas = generate_blob_sas(
                account_name=self.settings.azure_storage_account_name,
                container_name=container,
                blob_name=blob_path,
                account_key=self.settings.azure_storage_account_key,
                permission=BlobSasPermissions(read=True),
                expiry=expires_at,
            )
            return f"{self.account_url}/{container}/{blob_path}?{sas}"
        return f"file://{self.root / container / blob_path}"

    def upload_bytes(self, container: str, blob_path: str, data: bytes, overwrite: bool = True) -> str:
        if self.settings.blob_backend == "azure":
            container_client = self.client.get_container_client(container)
            try:
                container_client.create_container()
            except Exception:
                pass
            blob_client = container_client.get_blob_client(blob_path)
            blob_client.upload_blob(data, overwrite=overwrite)
            return blob_path
        local_path = self.root / container / blob_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        return blob_path

    def download_to_path(self, container: str, blob_path: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.settings.blob_backend == "azure":
            blob_client = self.client.get_blob_client(container=container, blob=blob_path)
            destination.write_bytes(blob_client.download_blob().readall())
        else:
            source = self.root / container / blob_path
            destination.write_bytes(source.read_bytes())
        return destination

    def download_prefix_to_path(self, container: str, prefix: str, destination_root: Path) -> int:
        normalized_prefix = prefix.strip("/")
        prefix_with_slash = f"{normalized_prefix}/" if normalized_prefix else ""
        destination_root.mkdir(parents=True, exist_ok=True)
        downloaded = 0

        if self.settings.blob_backend == "azure":
            container_client = self.client.get_container_client(container)
            for blob in container_client.list_blobs(name_starts_with=prefix_with_slash):
                blob_name = blob.name
                if not blob_name:
                    continue
                relative = blob_name[len(prefix_with_slash):] if prefix_with_slash else blob_name
                if not relative or relative.endswith("/"):
                    continue
                dest = destination_root / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                blob_client = container_client.get_blob_client(blob_name)
                dest.write_bytes(blob_client.download_blob().readall())
                downloaded += 1
            return downloaded

        source_root = self.root / container
        if prefix_with_slash:
            source_root = source_root / normalized_prefix
        if not source_root.exists():
            return 0
        for source in source_root.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            dest = destination_root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
            downloaded += 1
        return downloaded
