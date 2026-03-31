from __future__ import annotations

from typing import Any

import yaml
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from boltz2_service.enums import SpecSourceType
from boltz2_service.models import Boltz2Spec
from boltz2_service.repositories import AssetRepository, SpecRepository
from boltz2_service.schemas.specs import (
    ListSpecTemplatesResponse,
    RenderSpecRequest,
    RenderSpecResponse,
    SpecTemplateDefinition,
    SpecTemplateField,
)


class SpecRendererService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.asset_repo = AssetRepository(db)
        self.spec_repo = SpecRepository(db)

    def list_templates(self) -> ListSpecTemplatesResponse:
        return ListSpecTemplatesResponse(
            templates=[
                SpecTemplateDefinition(
                    name="boltz2_structure_prediction",
                    description=(
                        "Render a Boltz-2 prediction spec for protein structure prediction. "
                        "Supports protein sequences, ligand SMILES, and uploaded structure files."
                    ),
                    required_asset_kinds=["structure"],
                    fields=[
                        SpecTemplateField(
                            name="target_asset_id",
                            type="asset_id",
                            required=True,
                            description="Uploaded target structure asset (.cif or .pdb).",
                        ),
                        SpecTemplateField(
                            name="additional_entities",
                            type="entity[]",
                            required=False,
                            description="Extra entities (protein sequences, ligands, etc.) to include.",
                        ),
                        SpecTemplateField(
                            name="constraints",
                            type="constraint[]",
                            required=False,
                            description="Boltz-2 constraints block.",
                        ),
                    ],
                    example_payload={
                        "template_name": "boltz2_structure_prediction",
                        "target_asset_id": "asset-uuid",
                        "additional_entities": [
                            {"protein": {"id": "B", "sequence": "MKTL..."}},
                        ],
                    },
                ),
            ]
        )

    def render_template(
        self, api_key_id: str, payload: RenderSpecRequest
    ) -> RenderSpecResponse:
        assets = self.asset_repo.list_by_ids([payload.target_asset_id])
        if not assets:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Asset not found: {payload.target_asset_id}",
            )

        target_asset = assets[0]

        entities: list[dict[str, Any]] = [
            {"cif": {"path": target_asset.relative_path or target_asset.filename}}
        ]
        for entity in payload.additional_entities:
            entities.append(entity)

        spec_data: dict[str, Any] = {"version": 2, "entities": entities}
        if payload.constraints:
            spec_data["constraints"] = payload.constraints

        canonical_yaml = yaml.safe_dump(spec_data, sort_keys=False)

        spec = Boltz2Spec(
            created_by_api_key_id=api_key_id,
            source_type=SpecSourceType.template.value,
            template_name=payload.template_name,
            rendered_yaml=canonical_yaml,
            normalized_json=spec_data,
        )
        stored = self.spec_repo.create(spec, assets=assets)
        self.db.flush()

        return RenderSpecResponse(
            spec_id=stored.id,
            template_name=payload.template_name,
            canonical_yaml=canonical_yaml,
            asset_ids=[a.id for a in assets],
        )

    def create_raw_spec(
        self, api_key_id: str, raw_yaml: str, asset_ids: list[str]
    ) -> Boltz2Spec:
        assets = self.asset_repo.list_by_ids(asset_ids)
        missing = [aid for aid in asset_ids if aid not in {a.id for a in assets}]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Assets not found: {', '.join(missing)}",
            )

        spec = Boltz2Spec(
            created_by_api_key_id=api_key_id,
            source_type=SpecSourceType.raw_yaml.value,
            rendered_yaml=raw_yaml,
            normalized_json={},
        )
        stored = self.spec_repo.create(spec, assets=assets)
        self.db.flush()
        return stored
