from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from boltz2_service.api.deps import get_current_api_key, get_db
from boltz2_service.models import Boltz2Job
from boltz2_service.schemas.jobs import PredictionJobCreate, PredictionJobListResponse, PredictionJobResponse
from boltz2_service.services.jobs import JobService
from platform_core.auth.api_key_auth import ApiKeyAuthService
from platform_core.models.api_key import ApiKey

router = APIRouter(prefix="/v1/boltz2/prediction-jobs", tags=["jobs"])


@router.get("", response_model=PredictionJobListResponse)
def list_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    api_key: ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
) -> PredictionJobListResponse:
    return JobService(db).list(api_key.id, status=status, limit=limit, offset=offset)


@router.post("")
def create_job(
    payload: PredictionJobCreate,
    api_key: ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
):
    ApiKeyAuthService(db).assert_can_submit(api_key, Boltz2Job)
    job, replay = JobService(db).submit(api_key, payload)
    return {"job_id": job.id, "status": job.status, "idempotent_replay": replay}


@router.get("/{job_id}", response_model=PredictionJobResponse)
def get_job(
    job_id: str,
    api_key: ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
) -> PredictionJobResponse:
    service = JobService(db)
    return service.to_response(service.get(job_id, api_key.id))


@router.get("/{job_id}/artifacts")
def list_artifacts(
    job_id: str,
    api_key: ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
):
    service = JobService(db)
    return service.artifact_urls(service.get(job_id, api_key.id))


@router.post("/{job_id}:cancel")
def cancel_job(
    job_id: str,
    api_key: ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
):
    service = JobService(db)
    job = service.cancel(service.get(job_id, api_key.id))
    return {"job_id": job.id, "status": job.status}
