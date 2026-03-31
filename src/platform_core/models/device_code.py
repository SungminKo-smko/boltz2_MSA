from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.models.base import Base
from platform_core.time_utils import utc_now


def _uuid() -> str:
    return str(uuid4())


class DeviceCodeStatus(StrEnum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    EXPIRED = "expired"
    USED = "used"


class DeviceCode(Base):
    """Device Authorization Flow record.

    Allows MCP clients to obtain an API key without browser access.
    """

    __tablename__ = "device_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    device_code: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    user_code: Mapped[str] = mapped_column(String(9), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    api_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=DeviceCodeStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
