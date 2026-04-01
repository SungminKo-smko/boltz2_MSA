from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import structlog
from sqlalchemy.orm import Session, joinedload

from boltz2_service.config import Boltz2Settings, get_blob_storage
from boltz2_service.enums import JobStatus
from boltz2_service.models import Boltz2Job, Boltz2Spec, Boltz2SpecAsset
from platform_core.db import SessionLocal
from platform_core.time_utils import utc_now
from boltz2_service.worker.artifact_bundle import bundle_output
from boltz2_service.worker.boltz2_runner import Boltz2Runner, JobCanceledException

logger = structlog.get_logger(__name__)

STEP_PATTERN = re.compile(r"(\d+)/(\d+)")
PROGRESS_BAR_PATTERN = re.compile(r"\d+%\|")


class JobProcessor:
    def __init__(self, settings: Boltz2Settings) -> None:
        self.settings = settings
        self.storage = get_blob_storage()
        self.runner = Boltz2Runner(settings)

    def process(
        self,
        job_id: str,
        pod_name: str | None = None,
        job_name: str | None = None,
    ) -> None:
        with SessionLocal() as db:
            job = db.get(
                Boltz2Job,
                job_id,
                options=[
                    joinedload(Boltz2Job.spec)
                    .joinedload(Boltz2Spec.assets)
                    .joinedload(Boltz2SpecAsset.asset),
                ],
            )
            if job is None:
                logger.error("job_not_found", job_id=job_id)
                return
            if job.status == JobStatus.canceled:
                logger.info("job_already_canceled", job_id=job_id)
                return
            if not self._mark_running(db, job, pod_name=pod_name, job_name=job_name):
                return

            cancel_event = threading.Event()
            cancel_thread = threading.Thread(
                target=self._watch_for_cancel,
                args=(job.id, cancel_event),
                daemon=True,
            )
            cancel_thread.start()

            try:
                manifest = self._execute(job, cancel_event=cancel_event)
            except JobCanceledException:
                logger.info("job_canceled_mid_run", job_id=job_id)
                return
            except subprocess.CalledProcessError as exc:
                output = (exc.output or "").strip()
                # Remove NUL bytes — PostgreSQL text fields reject them
                output = output.replace("\x00", "")
                # Truncate to 4000 chars to avoid oversized failure messages
                if len(output) > 4000:
                    output = output[:4000] + "...(truncated)"
                self._mark_failed(
                    db, job, "boltz2_run_failed",
                    output or "boltz2 run failed",
                )
                return
            except subprocess.TimeoutExpired:
                self._mark_failed(db, job, "boltz2_run_timeout", "boltz2 run timed out")
                return
            except Exception as exc:  # noqa: BLE001
                self._mark_failed(db, job, "worker_unhandled_exception", str(exc))
                return
            finally:
                cancel_event.set()

            self._mark_succeeded(db, job, manifest)

    def _watch_for_cancel(self, job_id: str, cancel_event: threading.Event) -> None:
        while not cancel_event.wait(timeout=5):
            try:
                with SessionLocal() as db:
                    job = db.get(Boltz2Job, job_id)
                    if job is None or job.status == JobStatus.canceled:
                        cancel_event.set()
                        return
            except Exception:  # noqa: BLE001
                pass

    def _execute(
        self, job: Boltz2Job, cancel_event: threading.Event | None = None
    ) -> dict[str, str]:
        with TemporaryDirectory(prefix=f"job-{job.id}-") as tmpdir:
            base = Path(tmpdir)
            input_dir = base / "inputs"
            output_dir = base / "output"
            input_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)

            spec_path = input_dir / "spec.yaml"
            spec_path.write_text(job.spec.rendered_yaml, encoding="utf-8")

            for spec_asset in job.spec.assets:
                asset = spec_asset.asset
                rel = asset.relative_path or asset.filename
                self.storage.download_to_path(
                    self.settings.azure_input_container, asset.blob_path, input_dir / rel
                )

            self._update_progress(
                job.id,
                stage="preparing",
                progress_percent=5,
                status_message="Input downloaded, starting Boltz-2 prediction",
            )

            self.runner.run(
                spec_path=spec_path,
                output_dir=output_dir,
                runtime_options=job.runtime_options,
                line_handler=self._make_line_handler(job.id),
                cancel_event=cancel_event,
            )

            self._update_progress(
                job.id,
                status=JobStatus.uploading,
                stage="uploading",
                progress_percent=95,
                status_message="Uploading output artifacts",
            )

            manifest: dict[str, str] = {}

            results_zip = bundle_output(output_dir, base / "results.zip")
            manifest["results_zip"] = self._upload_artifact(job.id, "results.zip", results_zip.read_bytes())

            # Input spec for reproducibility
            manifest["input_spec_yaml"] = self._upload_artifact(
                job.id, "input_spec.yaml", spec_path.read_bytes()
            )

            manifest["run_manifest_json"] = self._upload_artifact(
                job.id,
                "run_manifest.json",
                json.dumps(
                    {"job_id": job.id, "runtime_options": job.runtime_options},
                    indent=2,
                ).encode("utf-8"),
            )

            for ext in ("*.cif", "*.pdb"):
                for path in output_dir.rglob(ext):
                    key = path.relative_to(output_dir).as_posix()
                    manifest[key] = self._upload_artifact(job.id, key, path.read_bytes())

            for path in output_dir.rglob("*.json"):
                key = path.relative_to(output_dir).as_posix()
                if key not in manifest:
                    manifest[key] = self._upload_artifact(job.id, key, path.read_bytes())

            return manifest

    def _upload_artifact(self, job_id: str, filename: str, data: bytes) -> str:
        blob_path = f"jobs/{job_id}/{filename}"
        self.storage.upload_bytes(self.settings.azure_results_container, blob_path, data)
        return blob_path

    # -- State management --------------------------------------------------

    def _mark_running(
        self,
        db: Session,
        job: Boltz2Job,
        pod_name: str | None = None,
        job_name: str | None = None,
    ) -> bool:
        db.refresh(job)
        if job.status in {JobStatus.canceled, JobStatus.failed}:
            logger.info("job_already_terminal", job_id=job.id, status=job.status)
            return False
        job.status = JobStatus.running
        job.current_stage = "preparing"
        job.progress_percent = 1
        job.status_message = "Worker claimed job"
        job.worker_pod_name = pod_name
        job.worker_job_name = job_name
        if job.started_at is None:
            job.started_at = utc_now()
        db.commit()
        return True

    def _mark_failed(
        self, db: Session, job: Boltz2Job, code: str, message: str
    ) -> None:
        db.refresh(job)
        if job.status == JobStatus.canceled:
            return
        job.status = JobStatus.failed
        job.current_stage = "failed"
        job.progress_percent = 100
        job.status_message = "Job failed"
        job.failure_code = code
        job.failure_message = message
        job.finished_at = utc_now()
        db.commit()

    def _mark_succeeded(
        self, db: Session, job: Boltz2Job, manifest: dict[str, str]
    ) -> None:
        db.refresh(job)
        if job.status == JobStatus.canceled:
            return
        job.status = JobStatus.succeeded
        job.current_stage = "completed"
        job.progress_percent = 100
        job.status_message = "Job completed successfully"
        job.artifact_manifest = manifest
        job.finished_at = utc_now()
        db.commit()

    def _update_progress(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress_percent: int | None = None,
        status_message: str | None = None,
    ) -> None:
        with SessionLocal() as db:
            job = db.get(Boltz2Job, job_id)
            if job is None or job.status == JobStatus.canceled:
                return
            if status is not None:
                job.status = status
            if stage is not None:
                job.current_stage = stage
            if progress_percent is not None:
                job.progress_percent = progress_percent
            if status_message is not None:
                job.status_message = status_message
            db.commit()

    def _touch_heartbeat(self, job_id: str) -> None:
        with SessionLocal() as db:
            job = db.get(Boltz2Job, job_id)
            if job is None or job.status in {JobStatus.canceled, JobStatus.failed}:
                return
            job.updated_at = utc_now()
            db.commit()

    def _make_line_handler(self, job_id: str):
        last_update = [utc_now()]
        interval = self.settings.job_heartbeat_interval_seconds

        def handle_line(line: str) -> None:
            now = utc_now()
            match = STEP_PATTERN.search(line)
            if match:
                # Throttle step progress updates to heartbeat interval
                if (now - last_update[0]).total_seconds() < interval:
                    return
                current = int(match.group(1))
                total = int(match.group(2))
                progress = min(90, 10 + int((current / max(total, 1)) * 80))
                self._update_progress(
                    job_id,
                    status=JobStatus.running,
                    stage="predicting",
                    progress_percent=progress,
                    status_message=f"Step {current}/{total}",
                )
                last_update[0] = now
                return

            if PROGRESS_BAR_PATTERN.search(line):
                if (now - last_update[0]).total_seconds() >= interval:
                    self._touch_heartbeat(job_id)
                    last_update[0] = now

        return handle_line
