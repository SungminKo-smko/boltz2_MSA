from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy.orm import Session

from boltz2_service.api.deps import get_current_api_key, get_db
from boltz2_service.config import get_settings
from boltz2_service.enums import JobStatus
from boltz2_service.models import Boltz2Job
from boltz2_service.schemas.jobs import PredictionJobCreate, PredictionJobListResponse, PredictionJobResponse
from boltz2_service.services.aca_logs import AcaLogService
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


@router.get("/{job_id}/status/public")
def get_job_status_public(
    job_id: str,
    db: Session = Depends(get_db),
):
    """인증 없이 job 상태를 조회한다 (artifact 전용)."""
    from boltz2_service.models import Boltz2Job

    job = db.get(Boltz2Job, job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.id,
        "status": job.status,
        "current_stage": job.current_stage,
        "progress_percent": job.progress_percent,
        "status_message": job.status_message,
    }


@router.get("/{job_id}/logs/public")
async def stream_job_logs_public(
    job_id: str,
    tail: int = Query(default=50, ge=1, le=300),
    db: Session = Depends(get_db),
):
    """인증 없이 job_id로 로그를 스트리밍한다 (artifact 전용).

    job_id가 UUID라 추측 불가능하며, 로그에 민감 정보가 포함되지 않으므로
    브라우저 artifact에서 CORS 제약 없이 호출할 수 있도록 공개 엔드포인트로 제공.
    """
    job = db.get(Boltz2Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.worker_job_name:
        raise HTTPException(status_code=404, detail="No worker execution linked")

    log_service = AcaLogService(get_settings())

    async def generate():
        async for chunk in log_service.stream_async(job.worker_job_name, tail=tail):
            yield chunk

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


@router.get("/{job_id}/logs/public/text")
def get_job_logs_public_text(
    job_id: str,
    tail: int = Query(default=50, ge=1, le=300),
    db: Session = Depends(get_db),
):
    """인증 없이 최근 로그를 plain text로 반환 (non-streaming 대안).

    stream_async가 실패하거나 브라우저에서 SSE를 처리하기 어려울 때
    간단한 폴링 방식으로 사용할 수 있다.
    """
    job = db.get(Boltz2Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.worker_job_name:
        raise HTTPException(status_code=404, detail="No worker execution linked")

    log_service = AcaLogService(get_settings())
    lines = log_service.get_recent_lines(job.worker_job_name, tail=tail)
    stage, progress = log_service.parse_live_progress(lines)

    body = "\n".join(lines)
    headers = {}
    if stage:
        headers["X-Live-Stage"] = stage
    if progress is not None:
        headers["X-Live-Progress"] = str(progress)

    return PlainTextResponse(body, headers=headers)


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
