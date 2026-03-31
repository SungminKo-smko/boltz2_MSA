from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from platform_core.models.base import Base
from platform_core.time_utils import utc_now
from boltz2_service.enums import AssetKind, JobStatus, SpecSourceType, ValidationStatus


def _uuid() -> str:
    return str(uuid4())


class Boltz2Asset(Base):
    __tablename__ = "boltz2_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    created_by_api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"))
    filename: Mapped[str] = mapped_column(String(512))
    relative_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default=AssetKind.structure.value)
    blob_path: Mapped[str] = mapped_column(String(1024), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Boltz2Spec(Base):
    __tablename__ = "boltz2_specs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    created_by_api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"))
    source_type: Mapped[str] = mapped_column(String(32), default=SpecSourceType.raw_yaml.value)
    template_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rendered_yaml: Mapped[str] = mapped_column(Text)
    normalized_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(32), default=ValidationStatus.pending.value)
    validation_errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    validation_warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    assets: Mapped[list["Boltz2SpecAsset"]] = relationship(
        back_populates="spec", cascade="all, delete-orphan"
    )


class Boltz2SpecAsset(Base):
    __tablename__ = "boltz2_spec_assets"
    __table_args__ = (UniqueConstraint("spec_id", "asset_id", name="uq_boltz2_spec_asset"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    spec_id: Mapped[str] = mapped_column(ForeignKey("boltz2_specs.id"))
    asset_id: Mapped[str] = mapped_column(ForeignKey("boltz2_assets.id"))

    spec: Mapped[Boltz2Spec] = relationship(back_populates="assets")
    asset: Mapped[Boltz2Asset] = relationship()


class Boltz2Job(Base):
    __tablename__ = "boltz2_jobs"
    __table_args__ = (
        UniqueConstraint("created_by_api_key_id", "client_request_id", name="uq_boltz2_key_request"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    created_by_api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"))
    spec_id: Mapped[str] = mapped_column(ForeignKey("boltz2_specs.id"))
    prediction_type: Mapped[str] = mapped_column(String(64), default="structure")
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.queued.value)
    client_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitted_payload_hash: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    runtime_options: Mapped[dict] = mapped_column(JSON, default=dict)
    queue_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker_pod_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker_job_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_stage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    progress_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    spec: Mapped[Boltz2Spec] = relationship()
