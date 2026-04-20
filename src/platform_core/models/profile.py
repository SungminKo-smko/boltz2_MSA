from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from platform_core.models.base import Base
from platform_core.time_utils import utc_now

if TYPE_CHECKING:
    from platform_core.models.api_key import ApiKey


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

    api_keys: Mapped[list["ApiKey"]] = relationship(  # noqa: F821
        back_populates="profile",
        uselist=True,
    )

    def get_api_key(self, service: str) -> "ApiKey | None":
        """Return the active API key for a specific service, or None."""
        for key in self.api_keys:
            if key.service == service and key.is_active:
                return key
        return None
