from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from boltz2_service.config import get_blob_storage, get_settings
from boltz2_service.enums import ValidationStatus
from boltz2_service.models import Boltz2Spec
from boltz2_service.repositories import SpecRepository
from boltz2_service.schemas.specs import ErrorDetail, ValidateSpecResponse


class SpecValidatorService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = SpecRepository(db)
        self.settings = get_settings()
        self.blob = get_blob_storage()

    def get(self, spec_id: str) -> Boltz2Spec:
        spec = self.repo.get(spec_id)
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found"
            )
        return spec

    def validate(self, spec: Boltz2Spec) -> ValidateSpecResponse:
        errors: list[ErrorDetail] = []
        warnings: list[str] = []
        valid = False

        preflight_error = self._preflight_yaml(spec.rendered_yaml)
        if preflight_error is not None:
            errors.append(preflight_error)
            self._save_status(spec, ValidationStatus.invalid, errors, warnings)
            return ValidateSpecResponse(
                spec_id=spec.id,
                valid=False,
                errors=errors,
                warnings=warnings,
                normalized_yaml=spec.rendered_yaml,
            )

        with TemporaryDirectory(prefix="spec-validate-") as tmpdir:
            base = Path(tmpdir)
            input_dir = base / "inputs"
            spec_path = input_dir / "spec.yaml"
            output_dir = base / "checked"
            input_dir.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(spec.rendered_yaml, encoding="utf-8")

            for spec_asset in spec.assets:
                asset = spec_asset.asset
                destination = input_dir / (asset.relative_path or asset.filename)
                self.blob.download_to_path(
                    self.settings.azure_input_container, asset.blob_path, destination
                )

            command = [
                self.settings.boltz2_bin,
                "predict",
                str(spec_path),
                "--out_dir", str(output_dir),
                "--cache", self.settings.boltz2_cache_dir,
                "--override",
                "--recycling_steps", "1",
                "--sampling_steps", "1",
                "--diffusion_samples", "1",
                "--num_workers", "0",
            ]

            try:
                completed = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    cwd=input_dir,
                    timeout=self.settings.boltz2_validate_timeout_seconds,
                )
                valid = True
                if completed.stderr.strip():
                    warnings.append(completed.stderr.strip())
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"boltz2 binary not available: {exc}",
                ) from exc
            except subprocess.CalledProcessError as exc:
                errors.append(
                    ErrorDetail(
                        code="boltz2_check_failed",
                        message=exc.stderr.strip() or exc.stdout.strip() or "boltz2 check failed",
                    )
                )
            except subprocess.TimeoutExpired:
                errors.append(
                    ErrorDetail(code="validation_timeout", message="boltz2 validation timed out")
                )

        final_status = ValidationStatus.valid if valid else ValidationStatus.invalid
        self._save_status(spec, final_status, errors, warnings)

        return ValidateSpecResponse(
            spec_id=spec.id,
            valid=valid,
            errors=errors,
            warnings=warnings,
            normalized_yaml=spec.rendered_yaml,
        )

    def _save_status(
        self,
        spec: Boltz2Spec,
        validation_status: ValidationStatus,
        errors: list[ErrorDetail],
        warnings: list[str],
    ) -> None:
        spec.validation_status = validation_status.value
        spec.validation_errors = [e.model_dump() for e in errors]
        spec.validation_warnings = warnings
        self.db.add(spec)
        self.db.flush()

    def _preflight_yaml(self, raw_yaml: str) -> ErrorDetail | None:
        try:
            parsed = yaml.safe_load(raw_yaml)
        except yaml.YAMLError as exc:
            return ErrorDetail(code="invalid_yaml", message=str(exc))
        if not isinstance(parsed, dict):
            return ErrorDetail(
                code="invalid_spec_shape",
                message="Top-level YAML document must be a mapping",
            )
        entities = parsed.get("entities")
        if not isinstance(entities, list) or not entities:
            return ErrorDetail(
                code="missing_entities",
                message="Boltz-2 spec must contain a non-empty entities list",
            )
        return None
