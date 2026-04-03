from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
import structlog
from azure.identity import DefaultAzureCredential

from boltz2_service.config import Boltz2Settings

logger = structlog.get_logger(__name__)

_MGMT_SCOPE = "https://management.azure.com/.default"
_MGMT_BASE = "https://management.azure.com"
_AUTH_TOKEN_API_VERSION = "2023-11-02-preview"
_REPLICA_API_VERSION = "2023-11-02-preview"

PIPELINE_STEP_RE = re.compile(r"(\d+)/(\d+)")
TQDM_PCT_RE = re.compile(r"(\d{1,3})%\|")


class AcaLogService:
    def __init__(self, settings: Boltz2Settings) -> None:
        self.settings = settings

    @property
    def _configured(self) -> bool:
        return bool(
            self.settings.aca_subscription_id
            and self.settings.aca_resource_group
            and self.settings.aca_worker_job_name
        )

    def _mgmt_token(self) -> str:
        return DefaultAzureCredential().get_token(_MGMT_SCOPE).token

    def _get_jit_token(self, mgmt_token: str) -> str | None:
        sub = self.settings.aca_subscription_id
        rg = self.settings.aca_resource_group
        job = self.settings.aca_worker_job_name
        url = (
            f"{_MGMT_BASE}/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.App/jobs/{job}"
            f"/getAuthToken?api-version={_AUTH_TOKEN_API_VERSION}"
        )
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(url, headers={"Authorization": f"Bearer {mgmt_token}"}, content=b"")
                resp.raise_for_status()
                return resp.json()["properties"]["token"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("aca_jit_token_failed", error=str(exc))
            return None

    def _get_log_stream_endpoint(self, execution_name: str, mgmt_token: str) -> str | None:
        sub = self.settings.aca_subscription_id
        rg = self.settings.aca_resource_group
        job = self.settings.aca_worker_job_name
        url = (
            f"{_MGMT_BASE}/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.App/jobs/{job}"
            f"/executions/{execution_name}/replicas"
            f"?api-version={_REPLICA_API_VERSION}"
        )
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url, headers={"Authorization": f"Bearer {mgmt_token}"})
                resp.raise_for_status()
                replicas = resp.json().get("value", [])
                if not replicas:
                    logger.warning("aca_no_replicas", execution=execution_name)
                    return None
                replica = replicas[0]
                containers = replica.get("properties", {}).get("containers", [])
                if not containers:
                    logger.warning("aca_no_containers", execution=execution_name)
                    return None
                endpoint: str | None = containers[0].get("logStreamEndpoint")
                return endpoint
        except Exception as exc:  # noqa: BLE001
            logger.warning("aca_replica_lookup_failed", execution=execution_name, error=str(exc))
            return None

    # ── 최근 로그 라인 조회 (status 폴링용) ─────────────────────────────────

    def get_recent_lines(self, execution_name: str, tail: int = 50) -> list[str]:
        """ACA 로그 스트림에서 최근 N줄을 동기 방식으로 반환한다."""
        if not self._configured:
            return []
        try:
            mgmt_token = self._mgmt_token()
            jit_token = self._get_jit_token(mgmt_token)
            if not jit_token:
                return []
            endpoint = self._get_log_stream_endpoint(execution_name, mgmt_token)
            if not endpoint:
                return []
            url = f"{endpoint}?follow=false&tailLines={tail}"
            with httpx.Client(timeout=15) as client:
                resp = client.get(url, headers={"Authorization": f"Bearer {jit_token}"})
                resp.raise_for_status()
            return resp.text.splitlines()
        except Exception as exc:  # noqa: BLE001
            logger.warning("aca_log_fetch_failed", execution=execution_name, error=str(exc))
            return []

    def parse_live_progress(self, lines: list[str]) -> tuple[str | None, int | None]:
        """로그 라인에서 현재 stage와 종합 progress_percent를 파싱한다.

        - PIPELINE_STEP_RE (N/M): stage 기본 progress 계산
        - TQDM_PCT_RE: stage 내 세부 진행률로 보정
        """
        stage: str | None = None
        stage_base_pct: int | None = None
        total_steps = 1
        tqdm_pct: int | None = None

        for line in lines:
            m = PIPELINE_STEP_RE.search(line)
            if m:
                step_index = int(m.group(1))
                total_steps = int(m.group(2))
                stage = "predicting"
                stage_base_pct = min(90, 10 + int((step_index / max(total_steps, 1)) * 80))
                tqdm_pct = None  # 단계 전환 시 초기화

            m2 = TQDM_PCT_RE.search(line)
            if m2:
                tqdm_pct = int(m2.group(1))

        if stage is None:
            return None, None

        if tqdm_pct is not None and stage_base_pct is not None:
            step_width = max(1, int(80 / total_steps))
            progress_percent = min(90, stage_base_pct + int(tqdm_pct / 100 * step_width))
        else:
            progress_percent = stage_base_pct

        return stage, progress_percent

    # ── 비동기 스트리밍 (로그 프록시 엔드포인트용) ─────────────────────────

    async def stream_async(self, execution_name: str, tail: int = 20) -> AsyncIterator[bytes]:
        """ACA 로그 스트림을 비동기로 프록시한다 (follow=true)."""
        if not self._configured:
            return
        try:
            mgmt_token = self._mgmt_token()
            jit_token = self._get_jit_token(mgmt_token)
            if not jit_token:
                return
            endpoint = self._get_log_stream_endpoint(execution_name, mgmt_token)
            if not endpoint:
                return
            url = f"{endpoint}?follow=true&tailLines={tail}"
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET", url, headers={"Authorization": f"Bearer {jit_token}"}
                ) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(1024):
                        yield chunk
        except Exception as exc:  # noqa: BLE001
            logger.warning("aca_log_stream_failed", execution=execution_name, error=str(exc))
