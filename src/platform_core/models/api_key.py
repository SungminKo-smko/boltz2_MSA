from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from platform_core.models.base import Base
from platform_core.time_utils import utc_now


def _uuid() -> str:
    return str(uuid4())


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("profile_id", "service", name="uq_profile_service"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"), index=True)
    service: Mapped[str] = mapped_column(String(64), default="boltz2")
    name: Mapped[str] = mapped_column(String(255))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    daily_job_limit: Mapped[int] = mapped_column(Integer, default=20)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, default=2)
    max_num_designs: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    extra_limits: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    profile: Mapped["Profile"] = relationship(back_populates="api_keys")  # noqa: F821
