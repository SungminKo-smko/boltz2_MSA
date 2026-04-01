from __future__ import annotations

import structlog
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from platform_core.config import get_settings
from platform_core.models.api_key import ApiKey
from platform_core.security import hash_api_key
from platform_core.time_utils import utc_now

logger = structlog.get_logger(__name__)


class ApiKeyAuthService:
    # Override in subclasses for service-specific status values.
    heartbeat_active_states: list[str] = ["running", "uploading"]
    all_active_states: list[str] = ["queued", "running", "uploading"]

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def authenticate(self, plaintext_key: str | None) -> ApiKey:
        if not plaintext_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing API key",
            )
        from sqlalchemy import select

        api_key = self.db.scalar(
            select(ApiKey).where(ApiKey.key_hash == hash_api_key(plaintext_key))
        )
        if api_key is None or not api_key.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )
        api_key.last_used_at = utc_now()
        self.db.add(api_key)
        self.db.flush()
        return api_key

    def assert_can_submit(self, api_key: ApiKey, job_model) -> None:
        """Check rate limits before job submission.

        Args:
            api_key: The authenticated API key.
            job_model: The SQLAlchemy Job model class (e.g., Boltz2Job).
        """
        from sqlalchemy import func, select

        from platform_core.time_utils import utc_now as _now
        from datetime import date, timedelta

        today = date.today()
        daily_count = self.db.scalar(
            select(func.count(job_model.id)).where(
                job_model.created_by_api_key_id == api_key.id,
                func.date(job_model.created_at) == today,
            )
        ) or 0

        if daily_count >= api_key.daily_job_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Daily job limit exceeded",
            )

        # Expire stale jobs
        now = _now()
        heartbeat_cutoff = now - timedelta(seconds=self.settings.job_heartbeat_timeout_seconds)
        queued_cutoff = now - timedelta(seconds=self.settings.job_queued_timeout_seconds)

        from sqlalchemy import update

        stale_active = self.db.execute(
            select(job_model.id).where(
                job_model.created_by_api_key_id == api_key.id,
                job_model.status.in_(self.heartbeat_active_states),
                job_model.updated_at < heartbeat_cutoff,
            )
        ).scalars().all()

        stale_queued = self.db.execute(
            select(job_model.id).where(
                job_model.created_by_api_key_id == api_key.id,
                job_model.status == "queued",
                job_model.created_at < queued_cutoff,
            )
        ).scalars().all()

        expired_ids = list(stale_active) + list(stale_queued)
        if expired_ids:
            self.db.execute(
                update(job_model)
                .where(job_model.id.in_(expired_ids))
                .values(
                    status="failed",
                    failure_code="worker_timeout",
                    failure_message="Job expired: no heartbeat received",
                    finished_at=now,
                )
            )
            self.db.flush()
            logger.info("expired_stale_jobs", count=len(expired_ids), api_key_id=api_key.id)

        # Check concurrent limit
        active_count = self.db.scalar(
            select(func.count(job_model.id)).where(
                job_model.created_by_api_key_id == api_key.id,
                job_model.status.in_(self.all_active_states),
            )
        ) or 0

        if active_count >= api_key.max_concurrent_jobs:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Concurrent job limit exceeded",
            )
