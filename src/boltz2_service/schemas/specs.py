from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class SpecTemplateField(BaseModel):
    name: str
    type: str
    required: bool = False
    description: str = ""


class SpecTemplateDefinition(BaseModel):
    name: str
    description: str
    required_asset_kinds: list[str] = []
    fields: list[SpecTemplateField] = []
    example_payload: dict | None = None


class ListSpecTemplatesResponse(BaseModel):
    templates: list[SpecTemplateDefinition]


class RenderSpecRequest(BaseModel):
    """Render a Boltz-2 YAML spec from a template."""

    template_name: str
    target_asset_id: str
    additional_sequences: list[dict] = Field(default_factory=list)
    constraints: list[dict] = Field(default_factory=list)


class RenderSpecResponse(BaseModel):
    spec_id: str
    template_name: str
    canonical_yaml: str
    asset_ids: list[str]


class ValidateSpecRequest(BaseModel):
    spec_id: str | None = None
    raw_yaml: str | None = None
    asset_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_input(self) -> ValidateSpecRequest:
        if not self.spec_id and not self.raw_yaml:
            raise ValueError("Either spec_id or raw_yaml is required")
        return self


class ErrorDetail(BaseModel):
    code: str
    message: str


class ValidateSpecResponse(BaseModel):
    spec_id: str
    valid: bool
    errors: list[ErrorDetail] = []
    warnings: list[str] = []
    normalized_yaml: str | None = None
