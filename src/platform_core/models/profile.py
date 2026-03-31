from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from platform_core.models.base import Base
from platform_core.time_utils import utc_now


def _uuid() -> str:
    return str(uuid4())


class Profile(Base):
    """User profile linked to Supabase auth.users.

    Created automatically by a Supabase DB trigger on auth.users INSERT.
    The `id` matches auth.users.id (Supabase user UUID).
    """

    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=utc_now)

    api_key: Mapped["ApiKey | None"] = relationship(  # noqa: F821
        back_populates="profile",
        uselist=False,
    )
