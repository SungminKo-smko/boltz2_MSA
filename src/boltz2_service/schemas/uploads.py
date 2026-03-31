from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from boltz2_service.enums import AssetKind


class UploadCreateRequest(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    kind: AssetKind = AssetKind.structure
    relative_path: str | None = None


class UploadCreateResponse(BaseModel):
    asset_id: str
    upload_url: str
    expires_at: datetime
