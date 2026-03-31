from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from boltz2_service.config import get_blob_storage, get_queue_service, get_settings
from boltz2_service.enums import JobStatus, ValidationStatus
from boltz2_service.models import Boltz2Job
from boltz2_service.repositories import JobRepository, SpecRepository
from boltz2_service.schemas.jobs import (
    PredictionJobCreate,
    PredictionJobListResponse,
    PredictionJobResponse,
)
from platform_core.models.api_key import ApiKey
from platform_core.time_utils import utc_now


class JobService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.jobs = JobRepository(db)
        self.specs = SpecRepository(db)

    def submit(
        self, api_key: ApiKey, request: PredictionJobCreate
    ) -> tuple[Boltz2Job, bool]:
        runtime_options = request.runtime_options.model_dump(exclude_none=True)

        if request.client_request_id:
            existing = self.jobs.get_by_client_request_id(
                api_key.id, request.client_request_id
            )
            if existing is not None:
                return existing, True

        spec = self.specs.get(request.spec_id)
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found"
            )
        if spec.created_by_api_key_id != api_key.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Spec does not belong to this API key",
            )
        if spec.validation_status != ValidationStatus.valid.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Spec must be validated before submission",
            )

        payload_hash = hashlib.sha256(
            json.dumps(
                {"spec_id": spec.id, "runtime_options": runtime_options},
                sort_keys=True,
            ).encode()
        ).hexdigest()

        job = Boltz2Job(
            created_by_api_key_id=api_key.id,
            spec_id=spec.id,
            prediction_type=request.prediction_type,
            client_request_id=request.client_request_id,
            submitted_payload_hash=payload_hash,
            runtime_options=runtime_options,
        )
        self.jobs.create(job)
        self.db.flush()

        send_result = get_queue_service().send({"job_id": job.id})
        job.queue_message_id = send_result.message_id
        self.db.add(job)
        self.db.commit()
        return job, False

    def get(self, job_id: str, api_key_id: str | None = None) -> Boltz2Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
            )
        if api_key_id and job.created_by_api_key_id != api_key_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Job does not belong to this API key",
            )
        return job

    def list(
        self,
        api_key_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PredictionJobListResponse:
        jobs = self.jobs.list_jobs(api_key_id, status=status, limit=limit, offset=offset)
        total = self.jobs.count_total(api_key_id, status=status)
        return PredictionJobListResponse(
            jobs=[self.to_response(j) for j in jobs],
            total=total,
        )

    def artifact_urls(self, job: Boltz2Job) -> dict:
        blob = get_blob_storage()
        artifacts = {}
        for key, blob_path in (job.artifact_manifest or {}).items():
            if not blob_path:
                continue
            artifacts[key] = blob.generate_download_url(
                self.settings.azure_results_container, blob_path
            )
        return {"job_id": job.id, "artifacts": artifacts}

    def cancel(self, job: Boltz2Job) -> Boltz2Job:
        terminal = {JobStatus.succeeded.value, JobStatus.failed.value, JobStatus.canceled.value}
        if job.status in terminal:
            return job
        job.status = JobStatus.canceled.value
        job.current_stage = "canceled"
        job.progress_percent = 100
        job.status_message = "Job canceled by user request"
        job.finished_at = utc_now()
        self.db.add(job)
        self.db.commit()
        return job

    def to_response(self, job: Boltz2Job) -> PredictionJobResponse:
        return PredictionJobResponse(
            id=job.id,
            prediction_type=job.prediction_type,
            status=job.status,
            current_stage=job.current_stage,
            progress_percent=job.progress_percent,
            status_message=job.status_message,
            failure_code=job.failure_code,
            failure_message=job.failure_message,
            artifact_manifest=job.artifact_manifest or {},
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
