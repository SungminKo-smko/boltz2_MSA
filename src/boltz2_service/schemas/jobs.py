from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from boltz2_service.enums import PredictionType


class Boltz2RuntimeOptions(BaseModel):
    diffusion_samples: int = Field(default=1, ge=1, le=10)
    sampling_steps: int = Field(default=200, ge=50, le=1000)
    recycling_steps: int = Field(default=3, ge=1, le=10)
    step_scale: float | None = Field(default=None, ge=0.5, le=3.0)
    output_format: Literal["pdb", "mmcif"] = "mmcif"
    use_potentials: bool = False
    use_msa_server: bool = True
    seed: int | None = None
    write_full_pae: bool = False
    max_parallel_samples: int = Field(default=5, ge=1, le=10)
    affinity_mw_correction: bool = False
    sampling_steps_affinity: int = Field(default=200, ge=50, le=1000)
    diffusion_samples_affinity: int = Field(default=5, ge=1, le=20)
    vs: bool = False


class PredictionJobCreate(BaseModel):
    spec_id: str
    prediction_type: PredictionType = PredictionType.structure
    runtime_options: Boltz2RuntimeOptions = Boltz2RuntimeOptions()
    client_request_id: str | None = None


class PredictionJobResponse(BaseModel):
    id: str
    prediction_type: str
    status: str
    current_stage: str | None = None
    progress_percent: int | None = None
    status_message: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    artifact_manifest: dict = {}
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class PredictionJobListResponse(BaseModel):
    jobs: list[PredictionJobResponse]
    total: int
