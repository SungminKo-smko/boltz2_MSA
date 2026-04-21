# Boltz-2 MSA → 통합 게이트웨이 이관 문서

## 1. Executive Summary

**목적**: 현재 `boltz2_MSA` 서비스의 REST API + MCP 서버 + **Worker**를 별도의 **통합 게이트웨이** 저장소로 복제하고, gateway repo가 자체 Worker ACA Job을 배포·운영한다.

**범위**:
- **복제 (gateway repo로 코드 복사)**: API 엔드포인트 (30개 이상), MCP 도구 14개, OAuth 인증 (Supabase Google OAuth + Device Flow + MCP OAuth 2.1), **Worker 코드 전체** (`src/boltz2_service/worker/`), **Worker Dockerfile** (`worker.Dockerfile`), **Worker 배포 스크립트** (`scripts/aca_deploy.sh` worker 블록)
- **공유 (변경 금지)**: Azure Blob/Queue 인프라, DB 스키마 (같은 Supabase PostgreSQL)
- **Deprecate 예정**: 이 저장소의 Worker — gateway worker가 대체하면 비활성화

**전제**:
- 게이트웨이는 `platform_core` 라이브러리를 재사용 (공유 인증, Blob, Queue, DB 초기화 로직)
- 같은 Supabase PostgreSQL DB를 공유 (테이블 변경 금지)
- Worker는 gateway repo 소유 — 같은 Service Bus queue (`boltz2-predict-jobs`)에서 메시지 소비

또는 **옵션 B (queue 분리 영구 공존)** 선택 시 두 시스템이 같은 DB / Blob / Supabase Auth를 공유하며 병행 운영 가능 — 자세한 내용은 §4.10 참조.

---

### 현재 vs 게이트웨이 통합 후 아키텍처

**현재 상태**:
```
┌──────────────────────────────────────────────────────────┐
│  boltz2_MSA (이 저장소)                                   │
├────────────────────────────────────────────────────────┤
│                                                          │
│  ┌────────────────────┐  ┌────────────────────┐         │
│  │ FastAPI (API)      │  │ FastMCP (MCP)      │         │
│  │ Port 8001          │  │ Streamable HTTP    │         │
│  │ 30+ endpoints      │  │ 14 tools           │         │
│  │ ACA Container App  │  │ OAuth 2.1 support  │         │
│  └────────────────────┘  └────────────────────┘         │
│                                                          │
│  shared: platform_core (auth, blob, queue, db)          │
│                                                          │
│  ┌────────────────────┐                                 │
│  │ Worker (GPU)       │                                 │
│  │ ACA Job            │                                 │
│  │ A100 Workload      │                                 │
│  │ Event-triggered    │                                 │
│  └────────────────────┘                                 │
│                                                          │
│  External: Supabase DB, Azure Blob, Service Bus        │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

**게이트웨이 복제 후**:
```
┌──────────────────────────────────────────────────────────┐
│  gateway (별도 레포) — boltz2_MSA에서 복제               │
├────────────────────────────────────────────────────────┤
│                                                          │
│  ┌────────────────────┐  ┌────────────────────┐         │
│  │ FastAPI (API)      │  │ FastMCP (MCP)      │         │
│  │ 모든 서비스 통합     │  │ 모든 서비스의 MCP  │         │
│  │ 30+ endpoints      │  │ 14+ tools          │         │
│  │ (boltz2 + others)  │  │ (boltz2 + others)  │         │
│  │ ACA Container App  │  │ OAuth 2.1 support  │         │
│  └────────────────────┘  └────────────────────┘         │
│                                                          │
│  ┌────────────────────┐                                 │
│  │ Worker (GPU)       │ ← Service Bus queue 구독        │
│  │ ACA Job            │   (gateway API가 publish)      │
│  │ A100 Workload      │   ← boltz2_MSA에서 복제        │
│  │ Event-triggered    │                                 │
│  └────────────────────┘                                 │
│                                                          │
│  shared: platform_core (인증, Blob, Queue, DB)          │
│                                                          │
│  External: Supabase DB, Azure Blob, Service Bus        │
│                                                          │
└──────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────┐
│  boltz2_MSA (이 저장소) — 복제 완료 후 deprecate 예정    │
├────────────────────────────────────────────────────────┤
│  Worker는 gateway repo로 이전됨 — 이 저장소 Worker 비활성화 예정  │
└──────────────────────────────────────────────────────────┘
```

---

## 2. 현재 시스템 아키텍처

### 2.1 2-패키지 구조

**`src/platform_core/`** (공유 라이브러리 — 게이트웨이도 사용)
- 위치: `src/platform_core/pyproject.toml` (독립 패키지)
- 기능:
  - **JWT 검증**: Supabase ES256/HS256 (`supabase_auth.py`)
  - **API Key 인증**: `b2_<token>` 형식, SHA256 hash, rate limit (`api_key_auth.py`)
  - **도메인 자동 승인**: `AUTO_APPROVE_DOMAINS` 환경변수 기반 (`domain_rules.py`)
  - **Blob 저장소 추상화**: `local` ↔ `azure` 백엔드 전환 (`services/blob_storage.py`)
  - **Queue 추상화**: `local` (파일 기반) ↔ `azure` (Service Bus) (`services/queue.py`)
  - **SQLAlchemy 초기화**: `NullPool`, `prepare_threshold=0` (Supabase Pooler 호환)
  - **Gmail SMTP 알림**: 잡 상태 메일 발송
  - **ORM 베이스**: `Profile`, `ApiKey`, `DeviceCode` 테이블

**`src/boltz2_service/`** (Boltz-2 전용 — API, MCP, Worker)
- **API routes** (`api/routes/`): auth, uploads, specs, jobs, health
- **MCP 도구** (`mcp/server.py`): 14개 도구
- **Worker** (`worker/app.py`): GPU 추론 (별도 ACA Job — gateway repo로 복제 대상)
- **ORM 모델** (`models.py`): `Boltz2Asset`, `Boltz2Spec`, `Boltz2SpecAsset`, `Boltz2Job`

**중요**: `boltz2_service`는 `platform_core`를 import 가능하지만, 역방향 import 금지.

### 2.2 3개 실행 단위 (배포)

| 단위 | 파일 | 진입점 | 포트 | ACA 리소스 | 목적 |
|------|------|--------|------|----------|------|
| **API** | `api.Dockerfile` | `boltz2_service.api.app:create_app` (FastAPI factory) | 8001 | Container App (1-3 replica, 1 CPU, 2Gi) | REST API, MCP Streamable HTTP, OAuth callback |
| **MCP** | `api.Dockerfile` 내 포함 | `boltz2_service.mcp.stdio:main` | stdio | 로컬 dev 전용 | Claude Code local stdio 모드 |
| **Worker** | `worker.Dockerfile` | `boltz2_service.worker.app:main` | — | Container App Job (Event-triggered, A100, 0-10 replica) | Service Bus 메시지 소비, GPU 추론 |

### 2.3 실제 Azure 배포 상태 (2026-04-17 기준)

| 리소스 | 값 |
|--------|-----|
| **구독** | `Azure subscription 1` (`e80f86e3-b865-4248-92a5-90eb190f8bb7`) |
| **리소스 그룹** | `nanobody-designer-897d0b-rg` |
| **지역** | `westus3` |
| **ACA Managed Env** | `nanobody-aca-897d0b-env` |
| **API Container App** | `boltz2-api` — `boltz2-api.politebay-55ff119b.westus3.azurecontainerapps.io` (target-port 8001, Single revision) |
| **Worker ACA Job** | `boltz2-worker` — Event-triggered, A100 workload profile, 0-10 replica, 24h timeout |
| **Service Bus** | `nanobodydsb897d0b` (Standard) — queue `boltz2-predict-jobs` (5m lock, 3 max-delivery-count) |
| **Storage Account** | `nanomapstorage` (StorageV2) — containers: `boltz2-inputs`, `boltz2-results`, `boltz2cache` (가중치 캐시) |
| **ACR** | `shaperon.azurecr.io` — `boltz2-api:sha-<commit>`, `boltz2-worker:sha-<commit>` |
| **DB** | Supabase Tokyo (Session Pooler IPv4) |

---

## 3. 게이트웨이로 이관되는 요소 (상세)

### 3.1 REST API 엔드포인트 전체 목록

게이트웨이는 다음 엔드포인트를 모두 구현 또는 포팅해야 함.

#### 3.1.1 인증 관련 (`/auth`)

| 메서드 | 경로 | 인증 | 요청 | 응답 | 설명 |
|--------|------|------|------|------|------|
| GET | `/auth/login` | 없음 | — | `{"auth_url": "..."}` | Supabase Google OAuth 시작. redirect 링크 반환. |
| GET | `/auth/callback` | code (쿼리) | `code=<auth_code>` | `AuthCallbackResponse` (user_id, email, is_approved, api_key) | OAuth callback. JWT 검증, Profile upsert, API Key 자동 발급 |
| GET | `/auth/me` | Bearer JWT | — | `ProfileResponse` | 현재 사용자 profile 조회 |
| POST | `/auth/device-code` | 없음 | `DeviceCodeRequest` (optional) | `DeviceCodeResponse` (device_code, user_code, verification_url, expires_in) | Device Authorization Flow 시작 (TTL 15분) |
| GET | `/auth/device-verify` | Bearer JWT | `user_code` (쿼리) | `{"status": "authorized"}` | 사용자가 device code 승인 |
| POST | `/auth/device-token` | 없음 | `DeviceTokenRequest` (device_code) | `DeviceTokenResponse` (api_key) | MCP client가 polling하여 API Key 수령 |

**특이점**: 
- `/auth/callback`은 Supabase가 리다이렉트하는 엔드포인트.
- `/auth/device-verify`는 브라우저에서 Bearer JWT와 함께 접근 (사용자 승인).
- Device code 평문 key는 `TTLCache`에 메모리 보관 (15분).

#### 3.1.2 구조 업로드 (`/v1/boltz2/uploads`)

| 메서드 | 경로 | 인증 | 요청 | 응답 | 설명 |
|--------|------|------|------|------|------|
| POST | `/v1/boltz2/uploads` | x-api-key | `UploadCreateRequest` (filename, relative_path, content_type, kind) | `UploadCreateResponse` (asset_id, upload_url, expires_at) | SAS URL 생성. 업로드는 클라이언트가 HTTP PUT으로 수행 |

**스키마**:
- `content_type`: `chemical/x-cif` 또는 `chemical/x-pdb`
- `kind`: `structure`, `reference` 등
- **필수 헤더 (클라이언트)**: `x-ms-blob-type: BlockBlob` (Azure Blob 업로드)

#### 3.1.3 스펙 렌더링 및 검증 (`/v1/boltz2/spec-*`)

| 메서드 | 경로 | 인증 | 요청 | 응답 | 설명 |
|--------|------|------|------|------|------|
| GET | `/v1/boltz2/spec-templates` | x-api-key | — | `ListSpecTemplatesResponse` (templates[]) | 사용 가능한 템플릿 목록 |
| POST | `/v1/boltz2/spec-templates/render` | x-api-key | `RenderSpecRequest` (template_name, target_asset_id, additional_sequences, constraints) | `RenderSpecResponse` (spec_id, canonical_yaml) | 템플릿으로부터 YAML spec 자동 생성 |
| POST | `/v1/boltz2/specs/validate` | x-api-key | `ValidateSpecRequest` (spec_id or raw_yaml, asset_ids) | `ValidateSpecResponse` (valid, spec_id, errors, warnings) | Spec YAML 검증 (CPU에서 실행) |

**주의**:
- Spec 검증은 **API 컨테이너의 CPU**에서 실행 (GPU worker 아님).
- `api.Dockerfile`에 `boltz==2.2.0` 설치되어 있음.
- 검증 시 `boltz predict --accelerator cpu --recycling_steps 1 --sampling_steps 1`로 dummy run.

#### 3.1.4 잡 제출 및 관리 (`/v1/boltz2/prediction-jobs`)

| 메서드 | 경로 | 인증 | 요청 | 응답 | 설명 |
|--------|------|------|------|------|------|
| POST | `/v1/boltz2/prediction-jobs` | x-api-key | `PredictionJobCreate` (spec_id, prediction_type, runtime_options, client_request_id) | `{"job_id": "...", "status": "queued", "idempotent_replay": bool}` | 잡 제출. Service Bus에 메시지 publish. |
| GET | `/v1/boltz2/prediction-jobs` | x-api-key | `status`, `limit`, `offset` (쿼리) | `PredictionJobListResponse` (jobs[], total) | 사용자의 잡 목록 조회 |
| GET | `/v1/boltz2/prediction-jobs/{job_id}` | x-api-key | — | `PredictionJobResponse` (전체 job 상세) | 특정 잡 상세 조회 |
| GET | `/v1/boltz2/prediction-jobs/{job_id}/status/public` | **없음** | — | `{"job_id", "status", "current_stage", "progress_percent", "status_message"}` | **공개 엔드포인트**. artifact UI에서 CORS 제약 없이 사용 |
| GET | `/v1/boltz2/prediction-jobs/{job_id}/logs/public` | **없음** | `tail` (쿼리, 기본 50) | `StreamingResponse` (text/plain) | **공개 엔드포인트**. ACA 로그 스트리밍 (SSE) |
| GET | `/v1/boltz2/prediction-jobs/{job_id}/logs/public/text` | **없음** | `tail` (쿼리) | `PlainTextResponse` + headers: `X-Live-Stage`, `X-Live-Progress` | **공개 엔드포인트**. 비-스트리밍 폴링 대안 |
| GET | `/v1/boltz2/prediction-jobs/{job_id}/artifacts` | x-api-key | — | `{"artifacts": {"filename": "sas_url", ...}}` | 완료된 잡의 아티팩트 SAS URL |
| POST | `/v1/boltz2/prediction-jobs/{job_id}:cancel` | x-api-key | — | `{"job_id": "...", "status": "canceled"}` | 잡 취소 |

**특이점**:
- `client_request_id`는 멱등성 키. 같은 키로 재제출 시 같은 `job_id` 반환.
- `status/public`, `logs/public` 엔드포인트는 인증 없음 (UUID job_id는 추측 불가능).
- 로그 스트리밍은 `AcaLogService`가 Azure Log Analytics API 호출.

#### 3.1.5 건강 확인 (`/health`, `/status`)

| 메서드 | 경로 | 인증 | 응답 | 설명 |
|--------|------|------|------|------|
| GET | `/health` | 없음 | `{"status": "ok"}` | 기본 상태 확인 |

#### 3.1.6 MCP 루트 엔드포인트 (RFC 9728)

| 메서드 | 경로 | 응답 | 설명 |
|--------|------|------|------|
| GET | `/.well-known/oauth-authorization-server` | OAuth 메타데이터 (issuer, authorization_endpoint, token_endpoint, code_challenge_methods_supported: ["S256"]) | Claude Code가 OAuth discovery |
| GET | `/.well-known/oauth-protected-resource` | MCP resource 메타데이터 (resource: `/mcp`, authorization_servers) | Claude Code가 보호된 리소스 발견 |
| GET | `/.well-known/openid-configuration` | OpenID Connect 호환 메타데이터 | — |

**중요**: 이 엔드포인트는 **FastAPI 루트 경로에서 직접 서빙**해야 함 (FastMCP가 아님). Claude Code의 RFC 9728 discovery가 루트에서만 작동.

#### 3.1.7 MCP Streamable HTTP

| 경로 | 설명 |
|------|------|
| `/mcp` | FastMCP의 Streamable HTTP mount point. POST 요청으로 도구 호출 |
| `/mcp/mcp` | MCP 프로토콜 엔드포인트 (HTTP로 MCP 메시지 교환) |

**FastAPI 마운트**:
```python
from fastapi import FastAPI
app = FastAPI(...)
mcp_starlette = mcp.streamable_http_app()
app.mount("/mcp", mcp_starlette)
```

---

### 3.2 MCP 도구 14개 (상세 명세)

모든 도구는 `@_mcp_error_handler` 데코레이터로 감싸져 있음. **raise하면 안 되고**, 항상 `{"error": "..."}` dict 반환.

#### 도구 0: `get_my_api_key`

```
시그니처: get_my_api_key(api_key: str = "") -> dict
```
- **목적**: MCP OAuth 로그인 후 API 키 반환 및 저장 힌트 제공.
- **파라미터**: `api_key` (선택, Bearer 토큰으로 fallback).
- **반환**: `{"api_key": "...", "email": "...", "profile_id": "...", "key_name": "...", "save_hint": "..."}`

#### 도구 1: `create_upload_url`

```
시그니처: create_upload_url(filename: str, api_key: str = "") -> dict
```
- **목적**: SAS URL 생성하여 클라이언트가 직접 업로드 가능하도록.
- **파라미터**: `filename` (e.g. `"target.cif"`), `api_key`.
- **반환**: `{"asset_id": "...", "upload_url": "...", "expires_at": "2026-04-17T...", "content_type": "...", "curl_hint": "..."}`
- **사용 흐름**: 
  1. `create_upload_url("target.cif")` → SAS URL 수령
  2. 클라이언트: `curl -X PUT -H "x-ms-blob-type: BlockBlob" -T target.cif <SAS URL>`
  3. asset_id를 `render_template` 등에 전달.

#### 도구 2: `upload_structure` (stdio/로컬 모드)

```
시그니처: upload_structure(file_path: str = "", file_content_base64: str = "", 
                         filename: str = "", api_key: str = "") -> dict
```
- **목적**: 로컬 파일 또는 base64 content를 직접 업로드 (stdio 모드에서 유용).
- **파라미터**: 
  - `file_path`: 로컬 경로 (stdio 모드)
  - `file_content_base64` + `filename`: base64 content (HTTP 모드)
- **반환**: `{"asset_id": "...", "filename": "..."}`

#### 도구 3: `validate_spec`

```
시그니처: validate_spec(raw_yaml: str, asset_ids: list[str], 
                       api_key: str = "") -> dict
```
- **목적**: 수동 작성한 YAML spec 검증.
- **파라미터**: `raw_yaml` (YAML 텍스트), `asset_ids` (구조 asset 목록).
- **반환**: `{"spec_id": "...", "warnings": [...], "valid": true}` 또는 `{"error": "...", "errors": [...]}`

#### 도구 4: `render_template`

```
시그니처: render_template(target_asset_id: str, additional_sequences: list[dict] = None,
                         constraints: list[dict] = None, api_key: str = "") -> dict
```
- **목적**: **권장 방법**. 템플릿으로부터 YAML spec 자동 생성.
- **파라미터**: 
  - `target_asset_id`: 구조 asset ID
  - `additional_sequences`: 추가 서열 (e.g. `[{"protein": {"id": "B", "sequence": "MKTL..."}}]`)
  - `constraints`: Boltz-2 constraints 블록
- **반환**: `{"spec_id": "...", "canonical_yaml": "..."}`

#### 도구 5: `submit_job`

```
시그니처: submit_job(spec_id: str, prediction_type: str = "structure",
                    diffusion_samples: int = 1, sampling_steps: int = 200,
                    recycling_steps: int = 3, step_scale: float = None,
                    max_parallel_samples: int = 5, output_format: str = "mmcif",
                    use_potentials: bool = False, use_msa_server: bool = True,
                    seed: int = None, write_full_pae: bool = False,
                    client_request_id: str = None, api_key: str = "") -> dict
```
- **목적**: 검증된 spec으로부터 job 제출, Service Bus에 메시지 publish.
- **파라미터**: 모두 optional, 기본값 제공.
  - `diffusion_samples`: 1-1000 (기본 1, 앙상블은 100 추천)
  - `sampling_steps`: 50-1000
  - `recycling_steps`: 1-10
  - `max_parallel_samples`: OOM 시 낮춤
- **반환**: `{"job_id": "...", "status": "queued", "idempotent_replay": bool}`
- **멱등성**: `client_request_id` 같으면 재제출 시에도 같은 job_id.

#### 도구 6: `get_job`

```
시그니처: get_job(job_id: str, api_key: str = "") -> dict
```
- **목적**: 잡의 현재 상태 조회.
- **반환**: `{"id": "...", "status": "...", "current_stage": "...", "progress_percent": ..., "prediction_type": "...", "runtime_options": {...}, "timestamps": {...}, "failure_message": "..."}`

#### 도구 7: `list_jobs`

```
시그니처: list_jobs(status: str = None, limit: int = 20, 
                  offset: int = 0, api_key: str = "") -> dict
```
- **목적**: 사용자의 잡 목록 조회 (필터링 가능).
- **파라미터**: `status` in ["queued", "running", "succeeded", "failed", "canceled"]
- **반환**: `{"jobs": [...], "total": ...}`

#### 도구 8: `cancel_job`

```
시그니처: cancel_job(job_id: str, api_key: str = "") -> dict
```
- **목적**: 실행 중인 또는 대기 중인 job 취소.
- **반환**: `{"job_id": "...", "status": "canceled"}`
- **동작**: DB status=canceled 설정 → Worker watch thread가 5초 polling으로 감지 → subprocess kill.

#### 도구 9: `get_logs`

```
시그니처: get_logs(job_id: str, tail: int = 50, api_key: str = "") -> dict
```
- **목적**: 잡의 진행률 및 ACA 로그 라인 조회.
- **파라미터**: `tail` (최근 로그 라인 수).
- **반환**: `{"job_id": "...", "status": "...", "current_stage": "...", "progress_percent": ..., "log_tail": [...], "live_stage": "...", "live_progress": ...}`
- **특이점**: DB 진행률 + ACA Log Analytics 쿼리 병행. ACA 실패 시 `log_error` 포함.

#### 도구 10: `get_artifacts`

```
시그니처: get_artifacts(job_id: str, api_key: str = "") -> dict
```
- **목적**: 완료된 잡의 아티팩트 다운로드 URL 조회.
- **조건**: job status = "succeeded"일 때만 가능.
- **반환**: `{"artifacts": {"results.zip": "sas_url", "input_spec.yaml": "sas_url", ...}}`
- **SAS URL**: 1시간 만료.

#### 도구 11: `list_templates`

```
시그니처: list_templates(api_key: str = "") -> dict
```
- **목적**: 사용 가능한 spec 템플릿 목록.
- **반환**: `{"templates": [{"name": "boltz2_structure_prediction", "description": "...", "parameters": {...}}]}`

#### 도구 12: `list_workers`

```
시그니처: list_workers(api_key: str = "", limit: int = 10) -> dict
```
- **목적**: ACA Job의 최근 실행 목록 (실구현: 최근 추가).
- **동작**: Azure ARM API로 worker job executions 조회.
- **필요 권한**: API의 Managed Identity가 worker job scope에 Reader.
- **반환**: `{"workers": [{"execution_name": "...", "status": "...", "start_time": "...", "end_time": "..."}], "total": ...}`
- **폴백**: ACA 설정이 없으면 `{"workers": [], "total": 0, "message": "..."}` 반환.

#### 도구 13: `submit_nanobody_structure_prediction` (cross-model workflow)

```
시그니처: submit_nanobody_structure_prediction(
  nanobody_sequence: str, target_asset_id: str,
  nanobody_chain_id: str = "N", prediction_type: str = "structure",
  diffusion_samples: int = 1, client_request_id: str = None,
  api_key: str = "") -> dict
```
- **목적**: `boltzgen` (나노바디 디자인)의 출력을 Boltz-2로 직결. 1-step workflow.
- **동작**:
  1. 나노바디 서열 + 타겟 구조로부터 YAML spec 자동 생성
  2. spec 검증
  3. job 제출 후 job_id 반환
- **반환**: `{"job_id": "...", "spec_id": "...", "status": "queued", "spec_yaml": "...", "workflow": "boltzgen -> boltz2 structure prediction"}`
- **검증**: 서열의 유효한 AA 문자만 허용, 최소 10 residues.

---

### 3.5 Worker 복제

Worker는 gateway repo 안에 **독립적 패키지**로 존재한다 (gateway API/MCP와 같은 저장소, 다른 Dockerfile, 다른 ACA Job).

**Gateway repo 예상 구조**:
```
gateway_repo/
├── src/
│   ├── gateway/                      # API + MCP (신규 또는 포팅)
│   ├── boltz2_worker/               # ← boltz2_MSA/src/boltz2_service/worker/ 복사
│   │   ├── __init__.py
│   │   ├── app.py                   # 메인 엔트리 (main() — one-shot 패턴)
│   │   ├── job_processor.py         # JobProcessor 오케스트레이터
│   │   ├── boltz2_runner.py         # boltz CLI subprocess + 취소
│   │   ├── queue_consumer.py        # Service Bus 소비자 (AutoLockRenewer)
│   │   └── artifact_bundle.py       # 결과 번들링 (results.zip)
│   ├── boltzgen_worker/             # ← boltzgen_MSA 에서 복사 (별도 에이전트)
│   └── platform_core/               # ← 공유 라이브러리 (submodule 또는 PyPI)
├── worker/boltz2/Dockerfile         # ← worker.Dockerfile 복사 (아래 상세 참조)
├── worker/boltzgen/Dockerfile
├── gateway.Dockerfile               # Gateway API+MCP 이미지
└── scripts/deploy.sh                # gateway + 다중 worker 통합 배포
```

**복제해야 할 파일 목록**:

| 원본 경로 (boltz2_MSA) | Gateway 대상 경로 | 용도 |
|----------------------|-----------------|------|
| `src/boltz2_service/worker/` | `src/boltz2_worker/` | Worker 메인 코드 |
| `src/boltz2_service/models.py` | 동일 위치 (공유 DB) | Boltz2Job 등 ORM |
| `src/boltz2_service/enums.py` | 동일 위치 | JobStatus 등 |
| `src/boltz2_service/config.py` | 동일 위치 | Boltz2Settings |
| `src/boltz2_service/services/jobs.py` | 동일 위치 | JobService (상태 전이) |
| `src/boltz2_service/repositories.py` | 동일 위치 | DB 리포지터리 |
| `src/boltz2_service/schemas/jobs.py` | 동일 위치 | RuntimeOptions Pydantic |
| `src/platform_core/` | `src/platform_core/` | 공유 인프라 라이브러리 |
| `worker.Dockerfile` | `worker/boltz2/Dockerfile` | CUDA 이미지 빌드 |
| `scripts/aca_deploy.sh` (worker 블록) | `scripts/deploy.sh` 내 병합 | ACA Job 배포 |

---

### 3.3 인증 3-way (매우 중요)

게이트웨이는 **정확히 같은 인증 동작**을 유지해야 함. 최종 인증 매체는 모두 `x-api-key` (또는 Bearer 토큰으로 포장).

#### 3.3.1 Supabase Google OAuth

```
[사용자] → /auth/login 
  ↓ (Supabase authorize URL 반환)
[브라우저] → Supabase Google OAuth 팝업 
  ↓ (사용자 로그인)
[Supabase] → /auth/callback?code=... 리다이렉트
  ↓ (auth code → JWT access_token 교환)
[API] → verify_supabase_jwt(access_token)
  ↓ (JWT 서명 검증, ES256 또는 HS256)
[DB] → Profile upsert (user_id, email, display_name)
  ↓ (도메인 룰 체크: shaperon.com 자동 승인)
[DB] → ApiKey 자동 발급 (또는 기존 key 반환)
  ↓
[API] → /auth/callback 응답: api_key, user_id, email, is_approved
```

**파일**: `src/platform_core/auth/supabase_auth.py` (verify_supabase_jwt)

**알고리즘 지원**:
- **ES256** (현재 Supabase 기본): JWKS에서 공개 키 fetch, kid 매칭, JWT 검증
- **HS256** (레거시): SUPABASE_JWT_SECRET로 검증

**도메인 자동 승인**: `AUTO_APPROVE_DOMAINS` 환경변수 (JSON) — e.g. `'["shaperon.com"]'`

#### 3.3.2 API Key 인증

```
Format: b2_<random-token> (prefix + underscore + token)
Storage: SHA256(token) → key_hash (DB)
Lookup: Authorization: x-api-key or header "x-api-key"
```

**파일**: `src/platform_core/auth/api_key_auth.py`, `src/platform_core/security.py`

**테이블**: `api_keys`
- `profile_id`: 소유자
- `service`: "boltz2" (또는 다른 서비스)
- `key_hash`: SHA256(token)
- `is_active`: boolean
- **Unique constraint**: `(profile_id, service)` — 프로필당 서비스별 1개 key

**Rate Limit**: `ApiKeyAuthService.assert_can_submit(key, Boltz2Job)` 체크
- `daily_job_limit`: 일일 제출 제한 (기본값은 설정에 따름)
- `max_concurrent_jobs`: 동시 실행 제한

#### 3.3.3 Device Authorization Flow (MCP 클라이언트용)

```
[MCP Client] → POST /auth/device-code
  ↓ (응답: device_code, user_code, verification_url)
[User] → 브라우저에서 verification_url 방문 (Bearer JWT 인증 필요)
  ↓
[API] → GET /auth/device-verify?user_code=... (Bearer JWT + 사용자 승인)
  ↓ (api_key 생성 또는 기존 key 반환, _device_plaintext_keys[device_code] = api_key)
[MCP Client] → POST /auth/device-token with device_code (polling, 202 Accepted → 200)
  ↓
[API] → return api_key
```

**특이점**:
- `user_code`: 8자 (ABCD-EFGH 형식), 15분 TTL
- `device_code`: UUID, 15분 TTL
- 평문 API key는 `TTLCache` 메모리 보관 — **중요**: dev 환경에서만 가능. prod는 보안 저장소 권장.
- `device_code` 재사용 불가 (status 추적).

#### 3.3.4 MCP OAuth 2.1 (Claude Code Streamable HTTP)

```
[Claude Code] → GET /.well-known/oauth-authorization-server (RFC 9728 discovery)
  ↓ (issuer, authorization_endpoint, code_challenge_methods_supported: ["S256"])
[Claude Code] → POST /authorize (client_id, redirect_uri, code_challenge, scope)
  ↓
[API] → Boltz2OAuthProvider.authorize()
  ↓ (session 생성, Supabase authorize URL 반환)
[Claude Code] → /authorize 리다이렉트 → Supabase Google OAuth
  ↓ (사용자 로그인)
[Supabase] → /oauth/callback/{session_id} 리다이렉트 (hash fragment)
  ↓ (2-Phase callback: GET → HTML, POST → token 검증)
[API] → Boltz2OAuthProvider.handle_oauth_callback()
  ↓ (verify_supabase_jwt, Profile upsert, auth_code 발급, Claude Code redirect_uri로 302)
[Claude Code] → POST /token (code, code_verifier)
  ↓
[API] → access_token = api_key (평문)
```

**파일**: `src/boltz2_service/mcp/oauth_provider.py`

**Supabase hash fragment 2-phase 특이점**:
- Supabase가 access_token을 URL hash fragment로만 반환 (`#access_token=...`). 
- 서버는 hash를 읽을 수 없으므로 클라이언트 JS가 hash → form POST로 재전송.
- `handle_oauth_callback` 메서드가 GET (HTML 서빙) / POST (검증) 동시 처리.

**session_id 경로 저장**:
- redirect_uri: `{issuer}/oauth/callback/{session_id}`
- Supabase 대시보드에서 prefix만 등록 (`{issuer}/oauth/callback/`) — 경로 파라미터가 매칭됨.

**Claude Code (public client)**:
- `client_secret` 강제하지 않음 (MCP auth 설정에서).
- PKCE 필수 (`code_challenge_method=S256`).

---

### 3.4 FastAPI 설정 및 라우터 마운트

**파일**: `src/boltz2_service/api/app.py`

**Factory 패턴**:
```python
def create_app() -> FastAPI:
    # 1. Boltz-2 설정 등록 (platform_core가 접근 가능하도록)
    register_settings(get_boltz2_settings())
    
    # 2. Lifespan: DB init + MCP session manager
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(model_modules=["boltz2_service.models"])
        async with mcp.session_manager.run():
            yield
    
    # 3. FastAPI 생성
    app = FastAPI(title="Bio AI Platform — Boltz-2 Service", ...)
    
    # 4. CORS (모든 origin 허용 — 게이트웨이에서는 재검토)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], ...)
    
    # 5. 라우터 마운트 (순서 중요)
    app.include_router(health_router, tags=["health"])
    app.include_router(auth_router)  # /auth
    app.include_router(uploads_router)  # /v1/boltz2/uploads
    app.include_router(specs_router)  # /v1/boltz2/spec-*
    app.include_router(jobs_router)  # /v1/boltz2/prediction-jobs
    
    # 6. RFC 9728 .well-known 엔드포인트 (루트에서)
    @app.get("/.well-known/oauth-authorization-server")
    async def oauth_auth_server(...):
        return JSONResponse({...})
    
    @app.get("/.well-known/oauth-protected-resource")
    async def oauth_protected_resource(...):
        return JSONResponse({...})
    
    @app.get("/.well-known/openid-configuration")
    async def openid_config(...):
        return JSONResponse({...})
    
    # 7. MCP Streamable HTTP 마운트
    app.mount("/mcp", mcp.streamable_http_app())
    
    return app
```

**uvicorn 실행**:
```bash
uvicorn boltz2_service.api.app:create_app --factory --port 8001 --reload
```

**게이트웨이 구현 시**: 모든 라우터를 같은 패턴으로 마운트하되, 서비스별 prefix 구분 (e.g. `/v1/boltzgen/...`, `/v1/boltz2/...`).

---

## 4. Worker 복제 대상 (Gateway repo로 복사)

**이 저장소의 Worker는 gateway repo로 복제된다**. "이 저장소에 Worker를 남겨두고 gateway가 참조한다"는 방식이 아니라, gateway repo가 **자체 Worker ACA Job을 배포하며 직접 소유**한다. 복제 완료 후 이 저장소의 Worker는 deprecate 예정.

### 4.1 Worker Docker Image

**복사 원본**: `worker.Dockerfile` → gateway: `worker/boltz2/Dockerfile`

```dockerfile
FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/cache
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System deps + Python 3.11
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common && \
    add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev \
        build-essential cmake git \
        libffi-dev libssl-dev libhdf5-dev \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python3.11 -m ensurepip --upgrade && \
    python3.11 -m pip install --no-cache-dir --upgrade pip setuptools wheel && \
    ln -sf /usr/bin/python3.11 /usr/local/bin/python && \
    ln -sf /usr/bin/python3.11 /usr/local/bin/python3

# Install PyTorch for CUDA 12.4 driver compatibility, then boltz2
RUN pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
RUN pip install --no-cache-dir "boltz==2.2.0"

# cuEquivariance kernels — accelerates triangular attention/multiplication on A100
# best-effort: 실패해도 --no_kernels fallback으로 계속 실행
RUN pip install --no-cache-dir \
    "cuequivariance_torch>=0.5.0" \
    "cuequivariance_ops_torch_cu12>=0.5.0" \
    "cuequivariance_ops_cu12>=0.5.0" \
    || echo "WARN: cuequivariance install failed, will use --no_kernels fallback"

# 캐시 디렉터리 생성 (가중치는 Azure Files mount 후 첫 실행 시 다운로드)
RUN mkdir -p /cache && boltz predict --help || true

# Install the platform package
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

RUN useradd -m -u 1000 worker && \
    chown -R worker:worker /cache
USER worker

CMD ["python", "-m", "boltz2_service.worker.app"]
```

**ACR 이미지 태그 컨벤션**:
- 현재: `shaperon.azurecr.io/boltz2-worker:sha-<git-commit>`
- Gateway에서도 같은 컨벤션 사용 권장 (ACR 위치는 달라도 됨)

### 4.2 ACA Job 배포 설정 (az containerapp job create/update)

**복사 원본**: `scripts/aca_deploy.sh` worker 블록 → gateway: `scripts/deploy.sh` 내 병합

**create (신규)**:
```bash
az containerapp job create \
  -g "${RESOURCE_GROUP}" \
  -n "${WORKER_JOB_NAME}" \
  --environment "${CONTAINERAPPS_ENV}" \
  --trigger-type Event \
  --image "${WORKER_IMAGE}" \
  --registry-server "${ACR_LOGIN_SERVER}" \
  --registry-username "${ACR_USERNAME}" \
  --registry-password "${ACR_PASSWORD}" \
  --cpu "${WORKER_CPU:-8.0}" \
  --memory "${WORKER_MEMORY:-32Gi}" \
  --workload-profile-name "${WORKER_WORKLOAD_PROFILE:-ConsumptionA100}" \
  --parallelism 10 \
  --replica-completion-count 1 \
  --replica-retry-limit "${WORKER_REPLICA_RETRY_LIMIT:-0}" \
  --replica-timeout "${WORKER_REPLICA_TIMEOUT:-86400}" \
  --polling-interval "${WORKER_POLLING_INTERVAL:-15}" \
  --min-executions "${WORKER_MIN_EXECUTIONS:-0}" \
  --max-executions "${WORKER_MAX_EXECUTIONS:-10}" \
  --scale-rule-name servicebus-queue \
  --scale-rule-type azure-servicebus \
  --scale-rule-metadata \
    "queueName=${SERVICE_BUS_QUEUE_NAME}" \
    "namespace=${SERVICE_BUS_NAMESPACE}" \
    "messageCount=1" \
  --scale-rule-auth "connection=sbconn" \
  --secrets "${secrets_args[@]}" \
  --env-vars "${worker_env_vars[@]}"
```

**update (기존 존재 시)**:
```bash
az containerapp job update \
  -g "${RESOURCE_GROUP}" \
  -n "${WORKER_JOB_NAME}" \
  --image "${WORKER_IMAGE}" \
  --cpu "${WORKER_CPU:-8.0}" \
  --memory "${WORKER_MEMORY:-32Gi}" \
  --workload-profile-name "${WORKER_WORKLOAD_PROFILE:-ConsumptionA100}" \
  --parallelism 10 \
  --replica-completion-count 1 \
  --replica-retry-limit "${WORKER_REPLICA_RETRY_LIMIT:-0}" \
  --replica-timeout "${WORKER_REPLICA_TIMEOUT:-86400}" \
  --polling-interval "${WORKER_POLLING_INTERVAL:-15}" \
  --min-executions "${WORKER_MIN_EXECUTIONS:-0}" \
  --max-executions "${WORKER_MAX_EXECUTIONS:-10}" \
  --scale-rule-name servicebus-queue \
  --scale-rule-type azure-servicebus \
  --scale-rule-metadata \
    "queueName=${SERVICE_BUS_QUEUE_NAME}" \
    "namespace=${SERVICE_BUS_NAMESPACE}" \
    "messageCount=1" \
  --scale-rule-auth "connection=sbconn" \
  --replace-env-vars "${worker_env_vars[@]}"
```

### 4.3 Secrets & Env Vars

**`secrets_args`** (ACA secret store — 평문값 노출 방지):
```bash
secrets_args=(
  "supurl=${SUPABASE_URL}"
  "supanon=${SUPABASE_ANON_KEY}"
  "supjwt=${SUPABASE_JWT_SECRET}"
  "dburl=${DATABASE_URL}"
  "sturl=${AZURE_STORAGE_ACCOUNT_URL}"
  "stname=${AZURE_STORAGE_ACCOUNT_NAME}"
  "stkey=${AZURE_STORAGE_ACCOUNT_KEY}"
  "sbconn=${SERVICE_BUS_CONNECTION_STRING}"
  "smtpuser=${SMTP_USERNAME:-}"
  "smtppwd=${SMTP_PASSWORD:-}"
  "smtpfrom=${SMTP_FROM_EMAIL:-}"
)
```

**`worker_env_vars`** (컨테이너 환경변수 — 민감값은 `secretref:`로 참조):
```bash
worker_env_vars=(
  "APP_ENV=production"
  "BLOB_BACKEND=azure"
  "QUEUE_BACKEND=azure"
  "AZURE_INPUT_CONTAINER=boltz2-inputs"
  "AZURE_RESULTS_CONTAINER=boltz2-results"
  "SERVICE_BUS_QUEUE_NAME=${SERVICE_BUS_QUEUE_NAME}"
  "BOLTZ2_BIN=boltz"
  "BOLTZ2_CACHE_DIR=/cache"
  "HF_HOME=/cache"
  "BOLTZ2_RUN_TIMEOUT_SECONDS=${BOLTZ2_RUN_TIMEOUT_SECONDS:-0}"
  "BOLTZ2_DEVICES=${BOLTZ2_DEVICES:-1}"
  "MSA_SERVER_URL=${MSA_SERVER_URL:-https://api.colabfold.com}"
  "SUPABASE_URL=secretref:supurl"
  "SUPABASE_ANON_KEY=secretref:supanon"
  "SUPABASE_JWT_SECRET=secretref:supjwt"
  "DATABASE_URL=secretref:dburl"
  "AZURE_STORAGE_ACCOUNT_URL=secretref:sturl"
  "AZURE_STORAGE_ACCOUNT_NAME=secretref:stname"
  "AZURE_STORAGE_ACCOUNT_KEY=secretref:stkey"
  "SERVICE_BUS_CONNECTION_STRING=secretref:sbconn"
  "SMTP_ENABLED=${SMTP_ENABLED:-false}"
  "SMTP_HOST=${SMTP_HOST:-}"
  "SMTP_PORT=${SMTP_PORT:-587}"
  "SMTP_USERNAME=secretref:smtpuser"
  "SMTP_PASSWORD=secretref:smtppwd"
  "SMTP_FROM_EMAIL=secretref:smtpfrom"
)
```

### 4.4 Azure Files `/cache` 마운트 (필수)

**목적**: Worker replica마다 7.6GB boltz 모델 가중치를 재다운로드하지 않도록 Azure Files share를 `/cache`에 영구 마운트.

**Share 이름**: `boltz2cache` (gateway repo는 자체 이름 사용 가능)

**순서**:
1. Azure Files share 생성 (`az storage share-rm create`)
2. ACA env에 storage 등록 (`az containerapp env storage set`)
3. Worker Job에 volumeMount 주입 (아래 `_apply_volume_to_job` 함수)

**`_apply_volume_to_job` 함수** (`scripts/aca_deploy.sh` L179-208):
```bash
_apply_volume_to_job() {
  local rg="$1" name="$2" storage_name="$3" mount_path="$4"
  local tmp_spec
  tmp_spec="$(mktemp -t aca-job-spec-XXXXXX.yaml)"

  az containerapp job show -g "${rg}" -n "${name}" -o yaml > "${tmp_spec}"

  python3 - "${tmp_spec}" "${storage_name}" "${mount_path}" <<'PY'
import sys, yaml
path, storage_name, mount_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    doc = yaml.safe_load(f)
tmpl = doc.setdefault("properties", {}).setdefault("template", {})
tmpl["volumes"] = [{
    "name": storage_name,
    "storageName": storage_name,
    "storageType": "AzureFile",
}]
for c in tmpl.get("containers", []):
    vm = c.get("volumeMounts") or []
    vm = [m for m in vm if m.get("volumeName") != storage_name]
    vm.append({"volumeName": storage_name, "mountPath": mount_path})
    c["volumeMounts"] = vm
with open(path, "w") as f:
    yaml.safe_dump(doc, f, sort_keys=False)
PY

  az containerapp job update -g "${rg}" -n "${name}" --yaml "${tmp_spec}" >/dev/null
  rm -f "${tmp_spec}"
}

# 호출 예시
_apply_volume_to_job "${RESOURCE_GROUP}" "${WORKER_JOB_NAME}" "${CACHE_STORAGE_NAME}" "${CACHE_MOUNT_PATH}"
```

**주의**: Standard Azure Files는 throughput이 60 MB/s로 제한됨. 대량 small-file read가 빈번하면 Premium Files 전환 검토.

### 4.5 KEDA Scale Rule 인증

Scale rule이 Service Bus connection string을 참조하려면 `--scale-rule-auth connection=sbconn` 필요.
`sbconn`은 위 `secrets_args`에 등록된 secret name — 별도 설정 없이 자동으로 scale rule auth로 사용됨.

### 4.6 Service Bus Queue Message Contract

**Queue**: `boltz2-predict-jobs`

**메시지 형식** (게이트웨이가 publish할 때):
```json
{
  "job_id": "<uuid>"
}
```

**설정** (scripts/aca_deploy.sh에서 생성):
- `--default-message-ttl`: (기본)
- `--lock-duration PT5M` (5분)
- `--max-delivery-count 3`
- Worker의 `AutoLockRenewer` 가 max 24시간까지 자동 갱신

**Worker 처리** (복제 후 gateway: `src/boltz2_worker/app.py`):
1. `QueueConsumer.receive_one()` — 메시지 1건 pull
2. JobProcessor 시작
3. 완료 → `consumer.ack()`
4. **one-shot**: 리플리카 종료, KEDA가 다음 메시지를 위해 새 리플리카 생성

**게이트웨이 책임**:
- `POST /v1/boltz2/prediction-jobs` 처리 시 Service Bus에 정확히 같은 형식 publish
- `ServiceBusQueueClient.send_messages(ServiceBusMessage(json.dumps({"job_id": job.id})))`

### 4.7 Blob Storage Path Convention

**Container**: `boltz2-inputs`, `boltz2-results`

**`boltz2-inputs` (업로드)**:
```
uploads/{asset_id}/{filename}
예: uploads/abc-123-def/target.cif
```

**`boltz2-results` (artifacts)**:
```
jobs/{job_id}/{filename}
예: jobs/xyz-789-uvw/results.zip
   jobs/xyz-789-uvw/input_spec.yaml
   jobs/xyz-789-uvw/run_manifest.json
   jobs/xyz-789-uvw/boltz_results_spec/predictions/...
```

**파일**: `src/platform_core/services/blob_storage.py`

**메서드**:
- `create_upload_target(blob_path, content_type)` → (upload_url, expires_at)
- `upload_bytes(container, blob_path, data)`
- `generate_sas(blob_path)` → sas_url (1시간 만료)

**SAS URL 업로드 헤더** (클라이언트가 수행):
```
PUT <upload_url>
x-ms-blob-type: BlockBlob
Content-Type: chemical/x-cif
```

### 4.8 Database Schema

**Supabase PostgreSQL** (Tokyo, Session Pooler IPv4)

**공유 테이블** (`src/platform_core/models/`):
- `profiles` — user_id (Supabase sub), email, display_name, is_approved, auto_approved, created_at, updated_at
- `api_keys` — id, profile_id (FK), service (boltz2 등), name, key_hash, is_active, created_at
- `device_codes` — id, device_code, user_code, status, profile_id, api_key_id, expires_at

**Boltz-2 테이블** (`src/boltz2_service/models.py` → 복제 후 gateway 동일 경로):
- `boltz2_assets` — id (PK), created_by_api_key_id (FK), filename, relative_path, content_type, kind (structure 등), blob_path (unique), created_at
- `boltz2_specs` — id (PK), created_by_api_key_id (FK), source_type (raw_yaml 또는 template), template_name, rendered_yaml (text), normalized_json, validation_status, validation_errors (JSON), validation_warnings (JSON), created_at
- `boltz2_spec_assets` — id (PK), spec_id (FK), asset_id (FK), unique(spec_id, asset_id)
- `boltz2_jobs` — id (PK), created_by_api_key_id (FK), spec_id (FK), prediction_type, status (queued/running/succeeded/failed/canceled), client_request_id, submitted_payload_hash, runtime_options (JSON), queue_message_id, worker_pod_name, worker_job_name, current_stage, progress_percent, status_message, artifact_manifest (JSON), failure_code, failure_message, created_at, updated_at, started_at, finished_at

**게이트웨이의 책임**:
- **테이블 변경 금지**. 마이그레이션은 현재 `boltz2_MSA`에서만 수행.
- 같은 `api_keys` 테이블 사용 (profile당 service별 1개 key).
- Worker가 보는 DB 테이블은 gateway API와 **공유** — gateway에서 ORM 변경 시 Worker에도 반영 필요.

### 4.9 Azure Files Cache Volume (`boltz2cache`)

**리소스**: `boltz2cache` Azure Files share (gateway는 자체 이름 사용 가능)

**마운트**: gateway Worker ACA Job에 `/cache`로 마운트 (위 4.4 `_apply_volume_to_job` 참조)

**용도**: Boltz-2 모델 가중치 + Hugging Face 캐시 영구 보존

**환경변수**:
- `BOLTZ2_CACHE_DIR=/cache`
- `HF_HOME=/cache`

spec validation 시에도 같은 `/cache` 경로 사용하도록 환경변수 세팅 (API 컨테이너도 같은 volume 마운트 가능하면).

---

### 4.10 전환 전략 및 영구 공존 운영

Gateway가 Worker 코드를 복제해서 자체 ACA Job을 배포할 때, 현행 `boltz2-worker` ACA Job과의 관계를 어떻게 설계할지 3가지 선택지가 있다.

#### 4.10.1 세 가지 전환 옵션

**옵션 A — 하드 컷오버 (권장, 가장 깔끔)**

```bash
1. Gateway worker 배포 (테스트 queue로 먼저 검증)
2. 검증 완료 후:
   az containerapp job update -n boltz2-worker -g <현 RG> \
     --max-executions 0    # 현 워커 KEDA 비활성화
3. Gateway worker를 boltz2-predict-jobs queue로 전환
4. 충분히 안정되면 현 워커 ACA Job + 이미지 삭제
```

장점: 운영 단순화, 중복 비용 없음, 추적 명확.  
단점: 컷오버 시점에 순간적 불가용 가능, 롤백 시 스크립트 재실행.

**옵션 B — 영구 공존 (queue 분리)**

```
- 현행: boltz2-predict-jobs + boltz2-worker (유지)
- Gateway: boltz2-predict-jobs-v2 + boltz2-worker-gw (신규)
- DB, Blob, Azure Files 캐시, Supabase Auth는 모두 공유
```

장점: 두 클라이언트 그룹(기존 MCP 사용자 vs 신규 Gateway 사용자) 동시 지원, 점진적 이전 가능, 롤백 부담 없음.  
단점: ACA Job 2개 유지 비용, DB 마이그레이션 시 양쪽 조정 필요.

**옵션 C — 단기 공존 후 컷오버 (옵션 A와 B의 절충)**

```
1. 옵션 B 구성으로 2~4주 병행 실행
2. Gateway 안정성 검증
3. 신규 트래픽을 Gateway로 유도 (DNS / 클라이언트 가이드)
4. 기존 queue가 완전히 drain되면 현행 deprecate
```

장점: 안전한 검증 기간 확보, 최종적으로 시스템 단일화.  
단점: 검증 기간만큼 중복 비용.

---

#### 4.10.2 영구 공존(옵션 B) 가능성 분석

두 시스템을 영구히 공존 운영할 경우, 각 리소스별 공유 안전 여부:

| 공유 리소스 | 공존 안전 여부 | 비고 |
|------------|----------------|------|
| **Service Bus queue** | ✅ 분리 필수 | `boltz2-predict-jobs` (현행) vs `boltz2-predict-jobs-v2` (gateway). Queue가 다르면 lock 충돌 없음 |
| **PostgreSQL DB** | ✅ 공유 OK | Job ID가 UUID — collision 없음. 같은 사용자가 API key 하나로 양쪽 엔드포인트 모두 접근 |
| **Blob storage** | ✅ 공유 OK | 경로 컨벤션 `uploads/{asset_id}/`, `jobs/{job_id}/` 전부 UUID — collision 없음 |
| **Azure Files 캐시 (`boltz2cache`)** | ⚠️ 공유 가능(조건부) | 두 worker가 같은 `/cache` 마운트 — read-only 공유 안전. 단 **boltz 버전 동일 필수**. 별도 share 사용(`boltz2-weights-gw`)도 대안 |
| **ACA env (`nanobody-aca-897d0b-env`)** | ✅ 같은 env에서 둘 다 배포 가능 | 리소스 이름만 달라야 함: `boltz2-worker` vs `boltz2-worker-gw` |
| **Supabase Auth** | ✅ 공유 | 같은 Supabase project → 같은 API key로 양쪽 엔드포인트 접근 가능 |
| **ACR (`shaperon.azurecr.io`)** | ⚠️ 가능하지만 분리 권장 | 이미지 태그 네임스페이스 분리: `boltz2-worker:<sha>` vs `gateway-boltz2-worker:<sha>` — CI가 같은 태그로 덮어쓰지 않도록 |

---

#### 4.10.3 Service Bus Competing Consumers 보장

중요한 질문: "두 worker가 같은 queue에 subscribe하면 메시지 한 개가 두 번 처리되지 않나?"

**답: 절대 아니다.** Service Bus는 Competing Consumers 패턴을 따른다.

- **PeekLock** 모드 (현재 구현): 한 consumer가 `receive_one()`으로 메시지를 가져가면 해당 메시지에 **lock**이 걸린다 (lockDuration PT5M). lock이 풀릴 때까지 다른 consumer는 같은 메시지를 볼 수 없다.
- 처리 완료 후 `complete()` 호출 → 메시지 삭제. 실패하면 lock 타임아웃 후 `max-delivery-count`만큼 재시도.
- 즉, **각 메시지는 정확히 하나의 worker replica에서만 처리된다.**

단, 같은 queue에 양쪽 worker가 붙으면 **KEDA scale이 양쪽 모두 triggering**되어 불필요한 replica가 기동될 수 있다 (빈손 replica는 `receive_one()` → `None` → 즉시 exit). 이를 피하려면 **queue를 분리**하는 것이 옵션 B의 전제.

---

#### 4.10.4 영구 공존 시 주의사항

**A. boltz 버전 동기화**
- 두 worker가 `/cache` 공유 시 boltz 버전이 다르면 `mols/` 스키마 / checkpoint 포맷 충돌 가능.
- 한 쪽만 boltz 업그레이드하지 말 것 — 동시 배포하거나, 별도 Azure Files share로 캐시 분리.

**B. DB 마이그레이션 조정**
- 공유 테이블(`boltz2_jobs`, `boltz2_specs`, `boltz2_assets`, `profiles`, `api_keys` 등)에 컬럼 추가 시 양쪽 코드 배포 동기화 필요.
- 순서: (1) backward-compatible migration (nullable 컬럼 추가) → (2) 양쪽 코드 배포 → (3) NOT NULL 제약 추가 migration (필요 시).

**C. 모니터링 구분**
- `boltz2_jobs.worker_job_name` 필드에 ACA Job 이름이 기록됨 — `boltz2-worker` vs `boltz2-worker-gw`로 어느 쪽 worker가 처리했는지 추적 가능.
- 대시보드에서 이 필드를 필터로 두 시스템 분리 모니터링 권장.

**D. API / MCP endpoint 라우팅**
- 사용자는 `boltz2-api.politebay-...azurecontainerapps.io` (현행) 또는 `<gateway-domain>` (신규) 중 선택.
- DNS / Application Gateway로 경로 기반 분배 가능: `/boltz2/v1/*` → 현행, `/gateway/v1/*` → 신규. 다만 이건 외부 인프라 필요.

**E. 비용 고려**
- ACA Job은 이벤트 트리거형이라 idle 시 0 replica → **구동 비용 없음**.
- 메시지가 양쪽 queue로 라우팅되므로 GPU 총 사용 시간은 합산 기준 유지됨 (단, 빈손 replica 스핀업 비용은 옵션 B에서 0 — queue가 달라서).
- API Container App이 2개 running 상태면 idle cost만 추가됨 (1 CPU / 2Gi 기준 대략 소액).

---

#### 4.10.5 권장 전환 단계 (옵션 C 절충 경로)

```
Phase 1 (Week 1~2): 병행 실행
  - Gateway repo에 worker 코드 복제 + 신규 queue + 신규 ACA Job 배포
  - 기존 boltz2_MSA는 그대로 유지 (유입 트래픽 처리 중단 없음)
  - 신규 Gateway endpoint는 소수 내부 사용자로 smoke test

Phase 2 (Week 3~4): 단계적 이전
  - MCP 클라이언트 가이드에 신규 endpoint 업데이트 (Claude Code mcp add ...)
  - 기존 API 호출자는 그대로 유지 (breaking change 없음)
  - 양쪽 잡 처리량 monitoring

Phase 3 (Week 5+): Deprecate 결정
  - 기존 queue depth가 꾸준히 0에 근접하면 deprecate 시작
  - 또는 영구 공존 결정 → Phase 3 생략하고 유지

Deprecate 절차 (Phase 3):
  1. 기존 API / MCP endpoint에 deprecation warning (응답 헤더 `Deprecation`, `Sunset`)
  2. 2~4주 후 KEDA 비활성화: --max-executions 0
  3. ACA Job 삭제, API Container App 삭제
  4. ACR 이미지 정리
```

---

#### 4.10.6 선택 기준 요약

| 상황 | 권장 옵션 |
|------|----------|
| Gateway가 완전히 기능 대등하고 신속한 단일화 목표 | **옵션 A** |
| 두 시스템을 장기 병행 운영해야 할 사업적 이유 (예: 기존 클라이언트 lock-in 해소 전까지) | **옵션 B** |
| 신중한 검증 후 통합 | **옵션 C** |

**Gateway 에이전트가 할 일**: 통합 범위와 운영 방침을 결정 후 옵션 하나 선택. 기본은 **옵션 C**가 안전하다.

---

## 5. 데이터/제어 흐름 Contracts

### 5.1 Job 생애주기 타임라인

Gateway가 API + Worker를 모두 소유하는 구조:

```
[Client] → POST /v1/boltz2/prediction-jobs (Gateway API)
   │ (API Key 인증)
   │ (ApiKeyAuthService.assert_can_submit — rate limit 체크)
   │ (spec validation_status 확인)
   ├─ Boltz2Job row 생성 (client_request_id 멱등성)
   ├─ Service Bus publish {"job_id": "<uuid>"}
   └─ 응답: {"job_id": "...", "status": "queued"}
      │
      │ [KEDA servicebus-queue 스케일 룰]
      │ (messageCount=1마다 Gateway Worker replica 기동)
      ↓
[Gateway Worker ACA Job replica] (one-shot, boltz2_MSA에서 복제)
   ├─ QueueConsumer.receive_one() → 메시지 pull
   ├─ AutoLockRenewer 등록 (5분 lock → 24시간까지 자동 연장)
   ├─ JobProcessor.process(job_id)
   │    ├─ DB: status=running, stage=preparing
   │    ├─ Blob download (boltz2-inputs/{asset_id}/...) → TemporaryDirectory
   │    ├─ Boltz2Runner.run(spec.yaml, output_dir)
   │    │    ├─ subprocess: boltz predict ... (GPU, /cache 가중치 사용)
   │    │    ├─ stdout 파싱 (STEP_PATTERN) → progress_percent 업데이트 (heartbeat throttle)
   │    │    └─ 별도 thread: _watch_for_cancel (5초 polling, DB status=canceled면 kill)
   │    ├─ bundle_output() → results.zip + 개별 .cif/.pdb/.json
   │    ├─ Blob upload (boltz2-results/jobs/{job_id}/...)
   │    └─ DB: status=succeeded, artifact_manifest 저장
   ├─ consumer.ack() (Service Bus complete)
   └─ main() return → 리플리카 종료 (one-shot, 루프 없음)
      │
      │ (SIGTERM graceful shutdown)
      │ (in-flight 메시지 ack 후 exit)
      ↓
[Client] → GET /v1/boltz2/prediction-jobs/{job_id} (Gateway API)
   └─ 응답: 최종 status, artifact_manifest, ...
```

**멱등성**: 같은 `client_request_id`로 재제출 시 같은 job_id 반환 (unique constraint on (api_key_id, client_request_id)).

**취소 흐름**: `POST /v1/boltz2/prediction-jobs/{job_id}:cancel` → DB status=canceled → Worker watch thread 감지 → subprocess kill.

### 5.2 로그 스트리밍

**엔드포인트** (공개, 인증 없음):
- `GET /v1/boltz2/prediction-jobs/{job_id}/logs/public` — **스트리밍 (SSE)**
- `GET /v1/boltz2/prediction-jobs/{job_id}/logs/public/text` — **비-스트리밍 (polling)**

**기술**:
1. `AcaLogService(settings).stream_async(job.worker_job_name, tail=tail)` — Azure Log Analytics API 호출
2. KQL 쿼리: 최근 로그 라인 tail개 조회
3. **live tqdm 진행률 파싱**: `X-Live-Stage`, `X-Live-Progress` 헤더로 반환

**필요 권한**: API의 Managed Identity → worker job scope에 `Reader` (이미 부여됨).

### 5.3 Artifact 다운로드 & SAS URL 갱신

**엔드포인트**:
- `GET /v1/boltz2/prediction-jobs/{job_id}/artifacts` (인증) — SAS URL dict 반환
- **공개 엔드포인트** `/v1/boltz2/prediction-jobs/{job_id}/status/public` — artifact UI에서 사용

**흐름**:
1. Job 완료 (status=succeeded)
2. artifact_manifest에 파일명 저장
3. Client: `list_artifacts` → SAS URL dict 수령 (1시간 만료)
4. SAS URL로 직접 다운로드 (Blob 서명된 URL)

**만료**: 1시간 후 다시 `list_artifacts` 호출하여 새 SAS URL 수령.

---

## 6. `platform_core` 공유 라이브러리

**위치**: `src/platform_core/` (이 저장소 내)

**독립 pyproject.toml**: `bioai-platform-core` 패키지로 정의 가능.

**게이트웨이 통합 방식**:
1. **서브모듈**: `gateway` repo에서 `src/platform_core`를 git submodule로 추가
2. **또는 PyPI**: `platform_core`를 별도 패키지로 publish, `gateway`에서 의존

**제공 기능**:

| 모듈 | 역할 |
|------|------|
| `auth/supabase_auth.py` | JWT ES256/HS256 검증 |
| `auth/api_key_auth.py` | API Key 인증 + rate limit |
| `auth/domain_rules.py` | 도메인 자동 승인 규칙 |
| `security.py` | API Key 쌍 생성 (prefix + hash) |
| `models/profile.py` | Profile ORM |
| `models/api_key.py` | ApiKey ORM |
| `models/device_code.py` | DeviceCode ORM |
| `models/base.py` | SQLAlchemy Base |
| `services/blob_storage.py` | Blob 추상화 (local ↔ azure) |
| `services/queue.py` | Queue 추상화 (local ↔ azure) |
| `db.py` | SQLAlchemy 엔진 초기화 (pooler 호환) |
| `config.py` | PlatformSettings 기본 클래스 |
| `time_utils.py` | UTC now 헬퍼 |

**게이트웨이 구현 시**:
```python
# 1. platform_core 임포트
from platform_core.auth.supabase_auth import verify_supabase_jwt
from platform_core.auth.api_key_auth import ApiKeyAuthService
from platform_core.services.blob_storage import BlobStorageService
from platform_core.services.queue import QueueService
from platform_core.db import init_db, SessionLocal

# 2. 서비스별 설정 주입
from platform_core.config import register_settings
from gateway.config import get_gateway_settings

register_settings(get_gateway_settings())

# 3. DB 초기화 (모든 model_modules 포함)
init_db(model_modules=[
    "boltz2_service.models",
    "boltzgen_service.models",  # 다른 서비스
])
```

**주의**: `platform_core`는 서비스별 모델/설정을 import하지 않음. 단방향 의존.

---

## 7. 환경 변수 매핑

### 7.1 런타임 변수 (앱 실행 시)

**공용** (API, Worker):
```
APP_ENV=production
AUTO_APPROVE_DOMAINS='["shaperon.com"]'
```

**Supabase/DB**:
```
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_JWT_SECRET=<secret>
DATABASE_URL=postgresql+psycopg://...  # (자동 정규화)
```

**Blob 백엔드**:
```
BLOB_BACKEND=azure  # (또는 local)
AZURE_STORAGE_ACCOUNT_URL=https://nanomapstorage.blob.core.windows.net
AZURE_STORAGE_ACCOUNT_NAME=nanomapstorage
AZURE_STORAGE_ACCOUNT_KEY=...
AZURE_INPUT_CONTAINER=boltz2-inputs
AZURE_RESULTS_CONTAINER=boltz2-results
```

**Queue 백엔드**:
```
QUEUE_BACKEND=azure  # (또는 local)
SERVICE_BUS_CONNECTION_STRING='Endpoint=sb://...;...'
SERVICE_BUS_QUEUE_NAME=boltz2-predict-jobs
```

**ACA 로그 스트리밍** (API만):
```
ACA_SUBSCRIPTION_ID=e80f86e3-b865-4248-92a5-90eb190f8bb7
ACA_RESOURCE_GROUP=nanobody-designer-897d0b-rg
ACA_WORKER_JOB_NAME=boltz2-worker
```

**Boltz-2 Worker**:
```
BOLTZ2_BIN=boltz
BOLTZ2_CACHE_DIR=/cache
BOLTZ2_RUN_TIMEOUT_SECONDS=0
BOLTZ2_DEVICES=1
MSA_SERVER_URL=https://api.colabfold.com
```

**SMTP** (선택):
```
SMTP_ENABLED=false
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_FROM_EMAIL=...
```

### 7.2 배포 변수 (scripts/aca_deploy.sh)

```bash
# 필수
RESOURCE_GROUP=nanobody-designer-897d0b-rg
CONTAINERAPPS_ENV=nanobody-aca-897d0b-env
ACR_LOGIN_SERVER=shaperon.azurecr.io
ACR_USERNAME=...
ACR_PASSWORD=...
AZURE_SUBSCRIPTION_ID=...

SERVICE_BUS_NAMESPACE=nanobodydsb897d0b
SERVICE_BUS_QUEUE_NAME=boltz2-predict-jobs

# API 컨테이너 리소스
API_CPU=1.0
API_MEMORY=2.0Gi
API_MIN_REPLICAS=1
API_MAX_REPLICAS=3

# Worker Job 리소스
WORKER_CPU=8.0
WORKER_MEMORY=32Gi
WORKER_WORKLOAD_PROFILE=ConsumptionA100
WORKER_MIN_EXECUTIONS=0
WORKER_MAX_EXECUTIONS=10
WORKER_REPLICA_TIMEOUT=86400

# Azure Files 캐시
CACHE_STORAGE_NAME=boltz2cache
CACHE_MOUNT_PATH=/cache
```

---

## 8. 게이트웨이 통합 시 주의사항

### 8.1 Supabase OAuth Redirect URL

**등록**: Supabase Dashboard → Authentication → Redirect URLs
```
https://gateway-api.example.com/auth/callback
https://gateway-api.example.com/oauth/callback/
```

**이유**: `/oauth/callback/{session_id}` (MCP OAuth 2.1) 경로 파라미터를 매칭하려면 prefix만 등록.

**실제 콜백**:
- REST OAuth: `/auth/callback?code=...`
- MCP OAuth: `/oauth/callback/{session_id}` (POST, 2-phase)

### 8.2 MCP OAuth Client Secret 정책

**Claude Code는 public client**. `.well-known/oauth-authorization-server` 응답에서 `client_secret` 요구하지 말 것.

**FastMCP 설정**:
```python
auth=AuthSettings(
    issuer_url=...,
    client_registration_options=ClientRegistrationOptions(
        enabled=True,
        valid_scopes=["boltz2"],
    ),
    # client_secret 강제하지 않음
)
```

### 8.3 RFC 9728 `.well-known` 엔드포인트

**위치**: FastAPI 루트 경로 (FastMCP mount 전)

```python
@app.get("/.well-known/oauth-authorization-server")
async def oauth_auth_server(...):
    return JSONResponse({
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        ...
    })
```

**이동/삭제 금지**. Claude Code의 OAuth discovery가 루트에서만 작동.

### 8.4 `Boltz2Settings` 필드 추가 시 3곳 갱신

1. `src/boltz2_service/config.py` — 필드 정의
2. `src/.env.example` — 사용 예제
3. `scripts/aca_deploy.sh` — `api_env_vars` / `worker_env_vars` / `secrets_args` 배열

### 8.5 Worker is One-Shot

```python
# Worker main loop — **NEVER do this:**
while True:
    msg = consumer.receive_one()  # ❌ 루프 금지
    process(msg)
```

**정확함**:
```python
# Worker main loop
msg = consumer.receive_one()
if msg is None:
    return  # 조용히 종료
process(msg)
# replica 종료, KEDA가 다음 메시지를 위해 새 replica 생성
```

### 8.6 MCP Tool Error Handling

**raise 금지**. `@_mcp_error_handler`가 모든 exception을 `{"error": "..."}` dict으로 변환.

```python
@mcp.tool()
@_mcp_error_handler
def my_tool(...):
    if error:
        return {"error": "message"}  # ✅
    # NOT: raise ValueError("message")  # ❌
```

### 8.7 Spec Validation은 API (CPU) 에서

**게이트웨이 API 컨테이너**에도 `boltz==2.2.0` 설치 필수.

```dockerfile
# api.Dockerfile
RUN pip install boltz==2.2.0
```

**Validation 커맨드**:
```bash
boltz predict \
  --input_pdb spec.yaml \
  --output_dir /tmp \
  --accelerator cpu \
  --recycling_steps 1 \
  --sampling_steps 1
```

### 8.8 가중치 캐시 (`/cache`)

**API Container App도 같은 volume 마운트**하면 좋음 (optional).

**환경변수** (app init):
```
BOLTZ2_CACHE_DIR=/cache
HF_HOME=/cache
```

### 8.9 Spec Validation에서 Cache Directory

**게이트웨이의 spec validation**에도 같은 cache 환경변수 필요.

```python
import os
cache_dir = os.getenv("BOLTZ2_CACHE_DIR", "/tmp")  # 기본값
# boltz 실행할 때 cache 경로 전달
```

### 8.10 BOLTZ2_CACHE_DIR 기본값

**로컬 dev**: `/tmp` (무관)
**ACA**: `/cache` (Azure Files mount)

### 8.11 cuEquivariance (best-effort)

**worker.Dockerfile**:
```dockerfile
RUN pip install cuEquivariance 2>/dev/null || true
```

**실패해도 `--no_kernels` fallback으로 계속 실행**. hard failure로 만들지 말 것.

### 8.12 Worker는 One-Shot — 루프 금지

```python
# ❌ 절대 금지
while True:
    msg = consumer.receive_one()
    process(msg)

# ✅ 올바름
msg = consumer.receive_one()
if msg is None:
    return  # 조용히 종료
process(msg)
# main() return → replica 종료, KEDA가 다음 메시지를 위해 새 replica 생성
```

루프를 돌면 KEDA가 replica를 추가로 생성하지 못하고 message lock이 충돌한다.

### 8.13 `--replica-retry-limit 0` 필수

재시도 시 `Boltz2Job.status ∈ {succeeded, failed, canceled}` 체크로 스킵되므로 멱등성은 보장되지만, 불필요한 재실행은 GPU 리소스 낭비. `--replica-retry-limit 0`으로 재시도 비활성화.

### 8.14 AutoLockRenewer와 replica-timeout 일치

- `AutoLockRenewer` 최대 갱신 시간: 86400초 (24시간)
- `--replica-timeout` 도 86400 — 두 값이 다르면 lock 만료 또는 조기 종료 발생

### 8.15 A100 텐서코어 precision 설정

Worker 코드에 `torch.set_float32_matmul_precision('high')` 설정됨 (commit `a515e1e`). 복제 후 `'highest'`로 변경하면 성능 저하 — **변경 금지**.

### 8.16 Worker가 보는 DB 테이블은 Gateway API와 공유

gateway repo에서 ORM 모델 (`boltz2_jobs`, `boltz2_specs`, `boltz2_assets`) 변경 시 Worker 코드에도 반영 필요. 두 컴포넌트가 같은 Supabase 테이블을 직접 접근하므로 스키마 불일치 시 runtime 오류.

---

## 9. 이관 체크리스트 (게이트웨이 에이전트용)

게이트웨이 개발 시 확인:

**API/MCP 복제**:
- [ ] 14개 MCP 도구 전수 포팅 또는 재구현
- [ ] 30+ REST endpoint 전수 포팅
- [ ] 3-way 인증 (Supabase OAuth + Device Flow + MCP OAuth 2.1) 동작 확인
- [ ] Service Bus publish가 `{"job_id": "..."}` 정확히 같은 형식
- [ ] Blob path 규약 준수 (`uploads/{asset_id}/...`, `jobs/{job_id}/...`)
- [ ] DB 연결 (같은 Supabase, 같은 테이블)
- [ ] 새 migration 금지 (schema 변경 불가)
- [ ] `AcaLogService` 재사용 또는 포팅 (`src/boltz2_service/services/aca_logs.py`)
- [ ] `/.well-known/...` endpoint를 FastAPI 루트에서 노출
- [ ] MCP Streamable HTTP mount 설정
- [ ] CORS 설정 (게이트웨이는 다른 origin 관리)
- [ ] Supabase redirect URL 등록 (prefix)

**Worker 복제**:
- [ ] `src/boltz2_service/worker/` 디렉터리 전체를 gateway repo로 복사 (`src/boltz2_worker/` 또는 유사 경로)
- [ ] Worker가 의존하는 서비스/모델 복사: `models.py`, `enums.py`, `config.py`, `services/jobs.py`, `repositories.py`, `schemas/jobs.py`
- [ ] `worker.Dockerfile` 복사 → `worker/boltz2/Dockerfile` (동일 base image `nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04` + deps 유지)
- [ ] `scripts/aca_deploy.sh`의 worker 배포 블록 (az containerapp job create/update + volume mount) gateway 배포 스크립트에 병합
- [ ] Azure Files share 생성 (`boltz2-weights` 유사 이름) + ACA env storage 등록 (`az containerapp env storage set`)
- [ ] `_apply_volume_to_job` 함수로 Worker Job에 `/cache` volume mount 적용
- [ ] Service Bus queue 생성 (이름 `boltz2-predict-jobs` 유지 또는 gateway 규칙 적용)
- [ ] KEDA scale rule + auth secret 구성 (`--scale-rule-auth connection=sbconn`)
- [ ] ACR에 `boltz2-worker` 이미지 빌드/푸시 파이프라인 구성
- [ ] ACA API Container App의 Managed Identity에 worker job scope `Contributor` role 할당 (gateway API가 worker log 조회/관리 시 필요)
- [ ] `torch.set_float32_matmul_precision('high')` 설정 유지 확인 (A100 텐서코어 활용)
- [ ] Worker one-shot 패턴 유지 (`main()` 이 메시지 1건 처리 후 `sys.exit(0)`)
- [ ] `--replica-retry-limit 0`, `--replica-timeout 86400` 설정 확인
- [ ] API/Worker Dockerfile 이미지 빌드 파이프라인 (CI/CD)
- [ ] `scripts/aca_deploy.sh` 또는 동등한 배포 스크립트 (또는 IaC)

**전환 전략 (§4.10 참조)**:
- [ ] 전환 전략 결정 (옵션 A / B / C 중 하나, §4.10 참조)
- [ ] 옵션 B 또는 C 선택 시: 신규 queue (`boltz2-predict-jobs-v2`) 생성
- [ ] 옵션 A 또는 C 선택 시: 기존 `boltz2-worker`의 `--max-executions 0` 비활성화 시점 계획
- [ ] boltz 버전 동기화 정책 결정 (동일 버전 유지 vs 캐시 분리)
- [ ] DB 마이그레이션 조정 프로세스 합의 (양쪽 repo 담당자 간)
- [ ] 모니터링 대시보드에서 `worker_job_name`으로 두 시스템 구분 가능하도록 구성

---

## 10. 참고 자료 및 관련 문서

**이 저장소 내 (복제 원본)**:
- `CLAUDE.md` — 프로젝트 전반 가이드
- `src/boltz2_service/mcp/server.py` — MCP 도구 전체 구현
- `src/boltz2_service/api/app.py` — FastAPI factory
- `src/boltz2_service/worker/` — Worker 코드 전체 (복제 대상) → gateway: `src/boltz2_worker/`
- `src/platform_core/` — 공유 라이브러리
- `worker.Dockerfile` — Worker Docker 이미지 (복제 대상) → gateway: `worker/boltz2/Dockerfile`
- `scripts/aca_deploy.sh` — ACA 배포 스크립트 (idempotent), worker 블록 → gateway: `scripts/deploy.sh` 내 병합
- `docs/testing-strategy.md` — 테스트 가치 있는 영역
- `docs/cleanup-report.md` — 드리프트/미사용 항목

**Gateway repo 복제 후 대응 경로**:
- `src/boltz2_service/worker/*` → `src/boltz2_worker/*`
- `worker.Dockerfile` → `worker/boltz2/Dockerfile`
- `scripts/aca_deploy.sh` worker 블록 → `scripts/deploy.sh` 내 함수

**외부 문서**:
- FastAPI: https://fastapi.tiangolo.com/
- FastMCP: https://github.com/jlopp/fastmcp
- Azure Container Apps: https://learn.microsoft.com/en-us/azure/container-apps/
- Azure Container Apps Jobs: https://learn.microsoft.com/en-us/azure/container-apps/jobs
- Supabase Auth: https://supabase.com/docs/guides/auth
- Service Bus: https://learn.microsoft.com/en-us/azure/service-bus-messaging/
- KEDA Azure Service Bus scaler: https://keda.sh/docs/scalers/azure-service-bus/

---

## 11. 게이트웨이 에이전트가 할 일 (요약)

1. **API 코드 복제**: 30+ endpoint를 gateway repo로 복사 또는 재구현
2. **MCP 도구 복제**: 14개 도구를 gateway MCP 서버에 추가
3. **인증 흐름 검증**: 3-way 인증이 기존과 정확히 같게 동작하는지 확인
4. **Service Bus 연결**: Queue publish가 정확히 같은 메시지 형식
5. **DB 스키마 공유**: 같은 Supabase, 같은 테이블 (변경 금지)
6. **Worker 코드 복제**: `src/boltz2_service/worker/` → gateway repo `src/boltz2_worker/` (Section 3.5, 4.1~4.5 참조)
7. **Worker 배포 구성 복제**: `worker.Dockerfile`, ACA Job 설정, Azure Files mount, KEDA scale rule (Section 4.2~4.5 참조)
8. **Managed Identity 설정**: gateway API → gateway worker job scope `Contributor` role
9. **문서화**: gateway repo에 이에 준하는 가이드 문서 작성
10. **테스트**: OAuth flow, job submission, worker GPU 실행, log streaming 등 end-to-end 검증

---

**작성 일시**: 2026-04-17  
**담당**: Boltz-2 MSA 이관 에이전트  
**최종 검토**: —
