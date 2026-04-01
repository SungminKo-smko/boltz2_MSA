"""
Boltz-2 MCP Server — FastMCP 기반 MCP 서버.

boltz2_service 서비스 레이어를 직접 호출하여 HTTP 왕복 없이 동작한다.
13개 tool: create_upload_url, upload_structure, validate_spec, render_template,
submit_job, get_job, list_jobs, cancel_job, get_logs,
get_artifacts, list_templates, list_workers, submit_nanobody_structure_prediction.

전송 모드:
  - stdio: 로컬 개발용 (mcp/stdio.py 진입점)
  - Streamable HTTP: FastAPI에 마운트 (/mcp 경로)
"""

from __future__ import annotations

import base64
import functools
from pathlib import Path

import structlog
import yaml
from fastapi import HTTPException
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http import TransportSecuritySettings

from boltz2_service.config import get_blob_storage, get_settings
from boltz2_service.enums import AssetKind
from boltz2_service.mcp.auth import mcp_auth
from boltz2_service.mcp.oauth_provider import Boltz2OAuthProvider
from boltz2_service.models import Boltz2Asset
from boltz2_service.repositories import AssetRepository
from boltz2_service.schemas.jobs import Boltz2RuntimeOptions, PredictionJobCreate
from boltz2_service.schemas.specs import RenderSpecRequest
from boltz2_service.services.jobs import JobService
from boltz2_service.services.spec_renderer import SpecRendererService
from boltz2_service.services.spec_validator import SpecValidatorService
from platform_core.auth.api_key_auth import ApiKeyAuthService

logger = structlog.get_logger(__name__)

_settings = get_settings()
_oauth_provider = Boltz2OAuthProvider()

mcp = FastMCP(
    "boltz2",
    auth_server_provider=_oauth_provider,
    auth=AuthSettings(
        issuer_url=_settings.mcp_issuer_url,
        resource_server_url=_settings.mcp_issuer_url,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["boltz2"],
            default_scopes=["boltz2"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    ),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# OAuth callback route — Supabase redirects here after Google login
# {session_id} is in the URL path to avoid Supabase redirect URL matching issues
@mcp.custom_route("/oauth/callback/{session_id}", methods=["GET", "POST"])
async def oauth_callback(request):
    return await _oauth_provider.handle_oauth_callback(request)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CONTENT_TYPE_MAP = {
    ".cif": "chemical/x-cif",
    ".pdb": "chemical/x-pdb",
    ".ent": "chemical/x-pdb",
}


def _resolve_content_type(filename: str) -> str | None:
    return _CONTENT_TYPE_MAP.get(Path(filename).suffix.lower())


def _handle_http_exception(e: HTTPException) -> dict:
    detail = e.detail
    if isinstance(detail, dict):
        return {"error": detail.get("message", str(detail)), **detail}
    return {"error": str(detail)}


def _mcp_error_handler(func):
    """Unified error handling for all MCP tools."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ValueError as e:
            return {"error": str(e)}
        except HTTPException as e:
            return _handle_http_exception(e)
        except Exception as e:
            logger.exception("mcp_tool_error", tool=func.__name__)
            return {"error": str(e)}

    return wrapper


# ---------------------------------------------------------------------------
# Tool 1: create_upload_url
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def create_upload_url(filename: str, api_key: str = "") -> dict:
    """
    Create a pre-signed upload URL for a structure file (.cif or .pdb).

    Returns asset_id and upload_url. Upload the file directly to upload_url
    using HTTP PUT (curl), then pass asset_id to render_template or validate_spec.

    Typical usage:
        result = create_upload_url(filename="target.cif", api_key=...)
        # Then in Bash:
        # curl -s -X PUT -T /path/to/target.cif \\
        #   -H "x-ms-blob-type: BlockBlob" \\
        #   -H "Content-Type: chemical/x-cif" \\
        #   "<result['upload_url']>"

    Args:
        filename: Structure filename e.g. "target.cif" or "protein.pdb".
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'asset_id', 'upload_url', 'expires_at', 'content_type',
        and 'curl_hint' on success, or 'error' on failure.
    """
    content_type = _resolve_content_type(filename)
    if content_type is None:
        return {"error": f"Unsupported file extension: {Path(filename).suffix}. Use .cif or .pdb"}

    relative_path = f"targets/{filename}"

    with mcp_auth(api_key) as (db, key):
        storage = get_blob_storage()
        blob_path = storage.build_asset_blob_path(relative_path)
        upload_url, expires_at = storage.create_upload_target(blob_path, content_type)

        asset = Boltz2Asset(
            created_by_api_key_id=key.id,
            filename=filename,
            relative_path=relative_path,
            content_type=content_type,
            kind=AssetKind.structure,
            blob_path=blob_path,
        )
        AssetRepository(db).create(asset)
        db.commit()

        curl_hint = (
            f'curl -s -X PUT -T "<FILE_PATH>" '
            f'-H "x-ms-blob-type: BlockBlob" '
            f'-H "Content-Type: {content_type}" '
            f'"{upload_url}"'
        )

        return {
            "asset_id": asset.id,
            "upload_url": upload_url,
            "expires_at": expires_at.isoformat(),
            "content_type": content_type,
            "curl_hint": curl_hint,
        }


# ---------------------------------------------------------------------------
# Tool 2: upload_structure (stdio / local mode)
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def upload_structure(
    file_path: str = "",
    file_content_base64: str = "",
    filename: str = "",
    api_key: str = "",
) -> dict:
    """
    Upload a structure file (.cif or .pdb) to Boltz-2 and return the asset_id.

    Two input modes:
      - file_path: local file path (stdio mode)
      - file_content_base64 + filename: base64 encoded content (remote mode)

    Args:
        file_path: Absolute or relative path to the .cif or .pdb structure file.
        file_content_base64: Base64 encoded file content (alternative to file_path).
        filename: Filename when using base64 mode (e.g. "target.cif").
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'asset_id' on success, or 'error' on failure.
    """
    if file_path:
        path = Path(file_path).expanduser()
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return {"error": f"File not found: {file_path}"}
        name = path.name
    elif file_content_base64 and filename:
        data = base64.b64decode(file_content_base64)
        name = filename
    else:
        return {"error": "Either file_path or (file_content_base64 + filename) is required."}

    content_type = _resolve_content_type(name)
    if content_type is None:
        return {"error": f"Unsupported file extension: {Path(name).suffix}. Use .cif or .pdb"}

    relative_path = f"targets/{name}"

    with mcp_auth(api_key) as (db, key):
        settings = get_settings()
        storage = get_blob_storage()
        blob_path = storage.build_asset_blob_path(relative_path)
        storage.upload_bytes(settings.azure_input_container, blob_path, data)

        asset = Boltz2Asset(
            created_by_api_key_id=key.id,
            filename=name,
            relative_path=relative_path,
            content_type=content_type,
            kind=AssetKind.structure,
            blob_path=blob_path,
        )
        AssetRepository(db).create(asset)
        db.commit()

        return {"asset_id": asset.id, "filename": name}


# ---------------------------------------------------------------------------
# Tool 3: validate_spec
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def validate_spec(raw_yaml: str, asset_ids: list[str], api_key: str = "") -> dict:
    """
    Validate a raw Boltz-2 spec YAML and return spec_id if valid.

    Args:
        raw_yaml: YAML string content of the spec file.
        asset_ids: List of asset_ids from upload_structure.
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'spec_id' and 'warnings' on success, or 'error' on failure.
    """
    with mcp_auth(api_key) as (db, key):
        renderer = SpecRendererService(db)
        validator = SpecValidatorService(db)

        spec = renderer.create_raw_spec(key.id, raw_yaml, asset_ids)
        result = validator.validate(spec)
        db.commit()

        if not result.valid:
            return {
                "error": "Spec validation failed",
                "errors": [e.model_dump() for e in result.errors],
                "hint": "Check entity definitions, chain IDs, and YAML structure.",
            }

        return {
            "spec_id": result.spec_id,
            "warnings": result.warnings,
            "valid": True,
        }


# ---------------------------------------------------------------------------
# Tool 4: render_template
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def render_template(
    target_asset_id: str,
    additional_sequences: list[dict] | None = None,
    constraints: list[dict] | None = None,
    api_key: str = "",
) -> dict:
    """
    Render a Boltz-2 structure prediction spec from the template.

    This is the recommended way to create a spec. The server generates YAML
    automatically from a target structure asset and optional sequences/constraints.

    Args:
        target_asset_id: asset_id from upload_structure (target .cif or .pdb).
        additional_sequences: Optional list of extra sequence dicts, e.g.
            [{"protein": {"id": "B", "sequence": "MKTL..."}}]
        constraints: Optional Boltz-2 constraints block as a list of dicts.
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'spec_id' and 'canonical_yaml' on success, or 'error' on failure.
    """
    payload = RenderSpecRequest(
        template_name="boltz2_structure_prediction",
        target_asset_id=target_asset_id,
        additional_sequences=additional_sequences or [],
        constraints=constraints or [],
    )

    with mcp_auth(api_key) as (db, key):
        result = SpecRendererService(db).render_template(key.id, payload)
        db.commit()
        return {
            "spec_id": result.spec_id,
            "canonical_yaml": result.canonical_yaml,
        }


# ---------------------------------------------------------------------------
# Tool 5: submit_job
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def submit_job(
    spec_id: str,
    prediction_type: str = "structure",
    diffusion_samples: int = 1,
    sampling_steps: int = 200,
    recycling_steps: int = 3,
    step_scale: float | None = None,
    max_parallel_samples: int = 5,
    output_format: str = "mmcif",
    use_potentials: bool = False,
    use_msa_server: bool = True,
    seed: int | None = None,
    write_full_pae: bool = False,
    client_request_id: str | None = None,
    api_key: str = "",
) -> dict:
    """
    Submit a Boltz-2 structure prediction job and return job_id.

    Args:
        spec_id: Validated spec_id from validate_spec or render_template.
        prediction_type: "structure", "affinity", "structure+affinity", or "virtual_screening".
        diffusion_samples: Number of diffusion samples (1-1000, default: 1). Use higher values (e.g. 100) for ensemble predictions.
        sampling_steps: Number of sampling steps (50-1000, default: 200).
        recycling_steps: Number of recycling steps (1-10, default: 3).
        step_scale: Step scale factor (0.5-3.0, optional).
        max_parallel_samples: Max samples processed in parallel on GPU (1-100, default: 5). Lower if OOM.
        output_format: "mmcif" or "pdb" (default: "mmcif").
        use_potentials: Enable potential energy guidance (default: False).
        use_msa_server: Use MSA server for alignment (default: True).
        seed: Random seed (optional).
        write_full_pae: Write full PAE matrix (default: False).
        client_request_id: Idempotency key. Same key returns same job_id.
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'job_id', 'status', 'idempotent_replay' on success,
        or 'error' on failure.
    """
    runtime = Boltz2RuntimeOptions(
        diffusion_samples=diffusion_samples,
        sampling_steps=sampling_steps,
        recycling_steps=recycling_steps,
        step_scale=step_scale,
        max_parallel_samples=max_parallel_samples,
        output_format=output_format,
        use_potentials=use_potentials,
        use_msa_server=use_msa_server,
        seed=seed,
        write_full_pae=write_full_pae,
    )
    request = PredictionJobCreate(
        spec_id=spec_id,
        prediction_type=prediction_type,
        runtime_options=runtime,
        client_request_id=client_request_id,
    )

    with mcp_auth(api_key) as (db, key):
        from boltz2_service.models import Boltz2Job

        ApiKeyAuthService(db).assert_can_submit(key, Boltz2Job)
        job, replay = JobService(db).submit(key, request)
        return {
            "job_id": job.id,
            "status": job.status,
            "idempotent_replay": replay,
        }


# ---------------------------------------------------------------------------
# Tool 6: get_job
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def get_job(job_id: str, api_key: str = "") -> dict:
    """
    Get the current status and details of a prediction job.

    Args:
        job_id: The job UUID returned by submit_job.
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        Full job details dict including: id, status, current_stage,
        progress_percent, prediction_type, runtime_options, timestamps,
        failure_message. Or 'error' on failure.
    """
    with mcp_auth(api_key) as (db, key):
        service = JobService(db)
        job = service.get(job_id, key.id)
        return service.to_response(job).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool 7: list_jobs
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def list_jobs(
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
    api_key: str = "",
) -> dict:
    """
    List prediction jobs with optional filters.

    Args:
        status: Filter by status: "queued", "running", "succeeded", "failed",
            "canceled" (optional).
        limit: Maximum number of jobs to return (default: 20).
        offset: Pagination offset (default: 0).
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'jobs' (list) and 'total' (int), or 'error' on failure.
    """
    with mcp_auth(api_key) as (db, key):
        result = JobService(db).list(key.id, status=status, limit=limit, offset=offset)
        return result.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool 8: cancel_job
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def cancel_job(job_id: str, api_key: str = "") -> dict:
    """
    Cancel a running or queued prediction job.

    Args:
        job_id: The job UUID to cancel.
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'job_id', 'status' on success, or 'error' on failure.
    """
    with mcp_auth(api_key) as (db, key):
        service = JobService(db)
        job = service.cancel(service.get(job_id, key.id))
        return {"job_id": job.id, "status": job.status}


# ---------------------------------------------------------------------------
# Tool 9: get_logs
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def get_logs(job_id: str, tail: int = 100, api_key: str = "") -> dict:
    """
    Get worker progress and log info for a prediction job.

    Args:
        job_id: The job UUID.
        tail: Reserved for future ACA log streaming (default: 100).
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with job progress info, or 'error' on failure.
    """
    with mcp_auth(api_key) as (db, key):
        service = JobService(db)
        job = service.get(job_id, key.id)

        result = {
            "job_id": job.id,
            "status": job.status,
            "current_stage": job.current_stage,
            "progress_percent": job.progress_percent,
            "status_message": job.status_message,
            "failure_code": job.failure_code,
            "failure_message": job.failure_message,
            "tail": tail,
        }
        return result


# ---------------------------------------------------------------------------
# Tool 10: get_artifacts
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def get_artifacts(job_id: str, api_key: str = "") -> dict:
    """
    Get download URLs for all artifacts produced by a completed prediction job.

    Only available after job status is 'succeeded'.

    Args:
        job_id: The job UUID.
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'artifacts' (dict mapping artifact name -> download URL),
        or 'error' on failure.
    """
    with mcp_auth(api_key) as (db, key):
        service = JobService(db)
        job = service.get(job_id, key.id)
        result = service.artifact_urls(job)
        return {"artifacts": result["artifacts"]}


# ---------------------------------------------------------------------------
# Tool 11: list_templates
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def list_templates(api_key: str = "") -> dict:
    """
    List available Boltz-2 spec templates.

    Args:
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'templates' (list of template info dicts),
        or 'error' on failure.
    """
    with mcp_auth(api_key) as (db, _key):
        result = SpecRendererService(db).list_templates()
        return result.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool 12: list_workers
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def list_workers(api_key: str = "") -> dict:
    """
    List active worker information (admin).

    Currently returns basic worker status. ACA management integration
    will be added when deployed to Azure Container Apps.

    Args:
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'workers' (list), 'total' (int), or 'error' on failure.
    """
    # Auth check only — no DB work needed
    with mcp_auth(api_key) as (_db, _key):
        pass

    return {
        "workers": [],
        "total": 0,
        "message": "ACA worker management not yet configured. Use get_job/list_jobs to check job status.",
    }


# ---------------------------------------------------------------------------
# Tool 13: submit_nanobody_structure_prediction (cross-model workflow)
# ---------------------------------------------------------------------------


@mcp.tool()
@_mcp_error_handler
def submit_nanobody_structure_prediction(
    nanobody_sequence: str,
    target_asset_id: str,
    nanobody_chain_id: str = "N",
    prediction_type: str = "structure",
    diffusion_samples: int = 1,
    client_request_id: str | None = None,
    api_key: str = "",
) -> dict:
    """
    Submit a nanobody-target complex structure prediction job (cross-model workflow).

    Bridges boltzgen (nanobody design) output to Boltz-2 structure prediction.
    Takes a nanobody amino-acid sequence from boltzgen and a pre-uploaded target
    structure asset, automatically generates a Boltz-2 v1 YAML spec, validates it,
    and submits a prediction job in one step.

    Typical workflow:
        1. Design nanobodies with boltzgen -> get FASTA sequences
        2. Upload target structure with upload_structure -> get target_asset_id
        3. Call this tool with nanobody_sequence + target_asset_id
        4. Poll with get_job to track progress

    Args:
        nanobody_sequence: Nanobody amino-acid sequence in 1-letter code (e.g. "EVQLV...").
        target_asset_id: asset_id of the target structure file (uploaded via upload_structure).
        nanobody_chain_id: Chain ID for the nanobody entity (default: "N").
        prediction_type: Prediction type: "structure", "affinity", etc. (default: "structure").
        diffusion_samples: Number of diffusion samples (default: 1).
        client_request_id: Idempotency key. Same key returns same job_id.
        api_key: Boltz-2 API key (x-api-key).

    Returns:
        dict with 'job_id', 'spec_id', 'status', 'spec_yaml' on success,
        or 'error' on failure.
    """
    clean_seq = nanobody_sequence.strip().upper()
    valid_aa = set("ACDEFGHIKLMNPQRSTVWY")
    invalid_chars = set(clean_seq) - valid_aa
    if invalid_chars:
        return {
            "error": f"Invalid amino-acid characters in nanobody sequence: {sorted(invalid_chars)}",
            "hint": "Provide a valid 1-letter amino-acid sequence (e.g. 'EVQLVESGGGLVQPGG...').",
        }
    if len(clean_seq) < 10:
        return {"error": "Nanobody sequence too short (minimum 10 residues)."}

    with mcp_auth(api_key) as (db, key):
        asset_repo = AssetRepository(db)
        assets = asset_repo.list_by_ids([target_asset_id])
        if not assets:
            return {"error": f"Target asset not found: {target_asset_id}"}
        target_asset = assets[0]

        target_path = target_asset.relative_path or target_asset.filename
        spec_data = {
            "version": 1,
            "sequences": [
                {"protein": {"id": nanobody_chain_id, "sequence": clean_seq}},
            ],
            "templates": [
                {"cif": target_path},
            ],
        }
        raw_yaml = yaml.safe_dump(spec_data, sort_keys=False)

        renderer = SpecRendererService(db)
        validator = SpecValidatorService(db)

        spec = renderer.create_raw_spec(key.id, raw_yaml, [target_asset_id])
        result = validator.validate(spec)
        db.commit()

        if not result.valid:
            return {
                "error": "Spec validation failed",
                "errors": [e.model_dump() for e in result.errors],
                "spec_yaml": raw_yaml,
                "hint": "Check nanobody sequence and target asset.",
            }

        from boltz2_service.models import Boltz2Job

        ApiKeyAuthService(db).assert_can_submit(key, Boltz2Job)

        runtime = Boltz2RuntimeOptions(diffusion_samples=diffusion_samples)
        request = PredictionJobCreate(
            spec_id=spec.id,
            prediction_type=prediction_type,
            runtime_options=runtime,
            client_request_id=client_request_id,
        )
        job, replay = JobService(db).submit(key, request)

        return {
            "job_id": job.id,
            "spec_id": spec.id,
            "status": job.status,
            "idempotent_replay": replay,
            "spec_yaml": raw_yaml,
            "workflow": "boltzgen -> boltz2 structure prediction",
        }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def get_mcp_app():
    """FastAPI에 마운트할 Starlette ASGI 앱 반환 (Streamable HTTP)."""
    return mcp.streamable_http_app()
