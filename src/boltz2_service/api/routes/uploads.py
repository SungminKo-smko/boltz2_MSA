from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from boltz2_service.api.deps import get_current_api_key, get_db
from boltz2_service.config import get_blob_storage
from boltz2_service.models import Boltz2Asset
from boltz2_service.repositories import AssetRepository
from boltz2_service.schemas.uploads import UploadCreateRequest, UploadCreateResponse
from platform_core.models.api_key import ApiKey

router = APIRouter(prefix="/v1/boltz2/uploads", tags=["uploads"])


@router.post("", response_model=UploadCreateResponse)
def create_upload(
    payload: UploadCreateRequest,
    api_key: ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
) -> UploadCreateResponse:
    storage = get_blob_storage()

    relative_path = payload.relative_path or payload.filename
    blob_path = storage.build_asset_blob_path(relative_path)
    upload_url, expires_at = storage.create_upload_target(blob_path, payload.content_type)

    asset = Boltz2Asset(
        created_by_api_key_id=api_key.id,
        filename=payload.filename,
        relative_path=relative_path,
        content_type=payload.content_type,
        kind=payload.kind.value,
        blob_path=blob_path,
    )
    AssetRepository(db).create(asset)
    db.commit()

    return UploadCreateResponse(
        asset_id=asset.id,
        upload_url=upload_url,
        expires_at=expires_at,
    )
