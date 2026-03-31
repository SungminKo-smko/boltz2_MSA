from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from boltz2_service.api.deps import get_current_api_key, get_db
from boltz2_service.schemas.specs import (
    ListSpecTemplatesResponse,
    RenderSpecRequest,
    RenderSpecResponse,
    ValidateSpecRequest,
    ValidateSpecResponse,
)
from boltz2_service.services.spec_renderer import SpecRendererService
from boltz2_service.services.spec_validator import SpecValidatorService
from platform_core.models.api_key import ApiKey

router = APIRouter(tags=["specs"])


@router.get("/v1/boltz2/spec-templates", response_model=ListSpecTemplatesResponse)
def list_templates(
    api_key: ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
) -> ListSpecTemplatesResponse:
    return SpecRendererService(db).list_templates()


@router.post("/v1/boltz2/spec-templates/render", response_model=RenderSpecResponse)
def render_template(
    payload: RenderSpecRequest,
    api_key: ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
) -> RenderSpecResponse:
    result = SpecRendererService(db).render_template(api_key.id, payload)
    db.commit()
    return result


@router.post("/v1/boltz2/specs/validate", response_model=ValidateSpecResponse)
def validate_spec(
    payload: ValidateSpecRequest,
    api_key: ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
) -> ValidateSpecResponse:
    renderer = SpecRendererService(db)
    validator = SpecValidatorService(db)
    if payload.spec_id:
        spec = validator.get(payload.spec_id)
    else:
        spec = renderer.create_raw_spec(api_key.id, payload.raw_yaml or "", payload.asset_ids)
    result = validator.validate(spec)
    db.commit()
    return result
