from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, joinedload

from boltz2_service.enums import JobStatus
from boltz2_service.models import Boltz2Asset, Boltz2Job, Boltz2Spec, Boltz2SpecAsset
from platform_core.time_utils import utc_now


class AssetRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, asset: Boltz2Asset) -> Boltz2Asset:
        self.db.add(asset)
        self.db.flush()
        return asset

    def get(self, asset_id: str) -> Boltz2Asset | None:
        return self.db.get(Boltz2Asset, asset_id)

    def list_by_ids(self, asset_ids: Iterable[str]) -> list[Boltz2Asset]:
        ids = list(asset_ids)
        if not ids:
            return []
        rows = self.db.scalars(select(Boltz2Asset).where(Boltz2Asset.id.in_(ids))).all()
        lookup = {r.id: r for r in rows}
        return [lookup[aid] for aid in ids if aid in lookup]


class SpecRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, spec: Boltz2Spec, assets: Iterable[Boltz2Asset] | None = None) -> Boltz2Spec:
        self.db.add(spec)
        self.db.flush()
        for asset in assets or []:
            self.db.add(Boltz2SpecAsset(spec_id=spec.id, asset_id=asset.id))
        self.db.flush()
        return spec

    def get(self, spec_id: str) -> Boltz2Spec | None:
        result = self.db.execute(
            select(Boltz2Spec)
            .options(joinedload(Boltz2Spec.assets).joinedload(Boltz2SpecAsset.asset))
            .where(Boltz2Spec.id == spec_id)
        )
        return result.unique().scalar_one_or_none()


class JobRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, job: Boltz2Job) -> Boltz2Job:
        self.db.add(job)
        self.db.flush()
        return job

    def get(self, job_id: str) -> Boltz2Job | None:
        result = self.db.execute(
            select(Boltz2Job)
            .options(joinedload(Boltz2Job.spec).joinedload(Boltz2Spec.assets).joinedload(Boltz2SpecAsset.asset))
            .where(Boltz2Job.id == job_id)
        )
        return result.unique().scalar_one_or_none()

    def get_by_client_request_id(self, api_key_id: str, client_request_id: str) -> Boltz2Job | None:
        return self.db.scalar(
            select(Boltz2Job).where(
                Boltz2Job.created_by_api_key_id == api_key_id,
                Boltz2Job.client_request_id == client_request_id,
            )
        )

    def list_jobs(
        self,
        api_key_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Boltz2Job]:
        q = select(Boltz2Job).where(Boltz2Job.created_by_api_key_id == api_key_id)
        if status:
            q = q.where(Boltz2Job.status == status)
        q = q.order_by(Boltz2Job.created_at.desc()).limit(limit).offset(offset)
        return list(self.db.scalars(q).all())

    def count_total(self, api_key_id: str, *, status: str | None = None) -> int:
        q = select(func.count(Boltz2Job.id)).where(Boltz2Job.created_by_api_key_id == api_key_id)
        if status:
            q = q.where(Boltz2Job.status == status)
        return self.db.scalar(q) or 0

    def expire_stale_jobs(
        self,
        api_key_id: str,
        heartbeat_timeout_seconds: int,
        queued_timeout_seconds: int,
    ) -> list[str]:
        now = utc_now()
        heartbeat_cutoff = now - timedelta(seconds=heartbeat_timeout_seconds)
        queued_cutoff = now - timedelta(seconds=queued_timeout_seconds)

        active_states = [JobStatus.running.value, JobStatus.uploading.value]

        stale_condition = or_(
            and_(
                Boltz2Job.status.in_(active_states),
                Boltz2Job.updated_at < heartbeat_cutoff,
            ),
            and_(
                Boltz2Job.status == JobStatus.queued.value,
                Boltz2Job.created_at < queued_cutoff,
            ),
        )

        expired_ids = list(self.db.scalars(
            select(Boltz2Job.id).where(
                Boltz2Job.created_by_api_key_id == api_key_id,
                stale_condition,
            )
        ).all())

        if expired_ids:
            self.db.execute(
                update(Boltz2Job)
                .where(Boltz2Job.id.in_(expired_ids))
                .values(
                    status=JobStatus.failed.value,
                    failure_code="worker_timeout",
                    failure_message="Job expired: no heartbeat received",
                    finished_at=now,
                )
            )
            self.db.flush()
        return expired_ids
