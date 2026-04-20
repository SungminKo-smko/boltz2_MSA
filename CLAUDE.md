# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 저장소 개요

Bio AI Platform — **Boltz-2 단백질 구조 예측 서비스**. 단일 파이썬 패키지 안에 세 개의 실행 단위가 공존하며, Azure Container Apps 위에서 서로 다른 리소스 타입으로 배포된다.

- **API 서버** (`boltz2_service.api.app:create_app`) — FastAPI. 인증, 업로드/spec/잡 CRUD, spec 검증(CPU), artifact SAS URL 발급. 포트 8001. **ACA Container App** (min 1 / max 3 replica, 1 CPU / 2Gi).
- **MCP 서버** (`boltz2_service.mcp.server:mcp`) — FastMCP 1.26+. API와 동일 프로세스에 Streamable HTTP로 `/mcp` 경로에 mount되거나, stdio로 로컬 실행. 서비스 레이어를 직접 호출(HTTP 왕복 없음). **현재 등록된 도구 14개.**
- **GPU Worker** (`boltz2_service.worker.app:main`) — **ACA Container App *Job*** (Event-triggered). A100 워크로드 프로파일(`ConsumptionA100`), 8 CPU / 32Gi, KEDA `azure-servicebus` 스케일 룰로 `messageCount=1`마다 replica 생성. min 0 / max 10, replica-timeout 24시간.

`boltz/`는 `jwohlwend/boltz`의 **git submodule**이다. 반드시 `--recursive`로 clone.

## 실제 배포 상태 (2026-04-17 기준, az CLI 확인)

| 리소스 | 이름 / 값 |
|--------|----------|
| Subscription | `Azure subscription 1` (`e80f86e3-b865-4248-92a5-90eb190f8bb7`) |
| Resource Group | `nanobody-designer-897d0b-rg` |
| Region | `westus3` |
| ACA Managed Env | `nanobody-aca-897d0b-env` |
| API Container App | `boltz2-api` — `boltz2-api.politebay-55ff119b.westus3.azurecontainerapps.io`, target-port 8001, transport `Auto`, revisionsMode `Single` |
| API 현재 revision | `boltz2-api--0000033` (Healthy) |
| Worker ACA Job | `boltz2-worker` — triggerType `Event`, parallelism 10, maxExecutions 10, minExecutions 0, replicaTimeout 86400, workloadProfile `ConsumptionA100` |
| Service Bus | `nanobodydsb897d0b` (Standard), queue `boltz2-predict-jobs` (lockDuration PT5M, maxDeliveryCount 3) |
| Storage Account | `nanomapstorage` (StorageV2) — containers: `boltz2-inputs`, `boltz2-results`, `superimpose`(타 서비스용) |
| ACR | `shaperon.azurecr.io` — 이미지: `boltz2-api:sha-<commit>`, `boltz2-worker:sha-<commit>` (현재 태그 = 최신 커밋 `2b12c68...`) |
| API Managed Identity | SystemAssigned, principalId `3756a04f-2634-42a7-867d-170e548d7c12` |
| MI Role Assignments | `Reader` + `Contributor` @ RG scope, `Contributor` @ worker job scope |
| ACR pull 시크릿 | `shaperonazurecrio-shaperon` (ACA가 `--registry-username/--registry-password`로부터 자동 생성) |
| CI/CD | `.github/workflows/`의 `build-deploy.yml`, `ci.yml`, `deploy-only.yml` |

**주의**: `aca_deploy.sh`는 worker job scope에만 `Contributor`를 부여하는데, 실제 MI는 RG scope의 `Reader`+`Contributor`까지 보유 중. **스크립트 밖에서 수동 부여된 권한**이 있다 — 재배포 시 스크립트만으로는 상태가 재현되지 않는다.

## 자주 쓰는 커맨드

```bash
# 설치 (dev + MCP extras)
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]" "mcp[cli]>=1.0"

# API + MCP 실행 (FastAPI factory, MCP는 /mcp/mcp에 mount)
uvicorn boltz2_service.api.app:create_app --factory --port 8001 --reload

# MCP stdio 모드 (로컬 Claude Code)
python -m boltz2_service.mcp.stdio          # 또는: boltz2-mcp

# Worker 실행 (GPU + BOLTZ2_BIN 필요. 메시지 1건 처리 후 exit — one-shot)
python -m boltz2_service.worker.app

# Lint (ruff, py311, E+F만, line-length 100, E501 무시)
ruff check src

# Test (pytest + pytest-asyncio. tests/ 디렉터리 미존재 — 추가 시 tests/ 아래에)
pytest
pytest tests/path/to/test_file.py::test_name

# 이미지 빌드
docker build -f api.Dockerfile -t boltz2-api .           # Python 3.11-slim
docker build -f worker.Dockerfile -t boltz2-worker .     # CUDA 12.4.1-cudnn-devel-ubuntu22.04

# Azure Container Apps 배포 (env 필요. scripts/aca_deploy.sh 상단 required_vars 참조)
./scripts/aca_deploy.sh
```

## 아키텍처

### `src/` 내 2-패키지 구조

- **`platform_core/`** — 서비스 간 공유 인프라. Supabase JWT 검증(ES256/HS256), API Key 인증, 도메인 자동승인 룰(`AUTO_APPROVE_DOMAINS`), blob/queue 백엔드 추상화(`local` ↔ `azure`), SQLAlchemy 초기화, Gmail SMTP 알림.
- **`boltz2_service/`** — Boltz-2 전용. API routes, MCP tools, worker, job/spec 서비스, ORM 모델(`Boltz2Asset`, `Boltz2Spec`, `Boltz2Job`).

`platform_core`는 `boltz2_service`를 import하면 안 된다. 서비스별 설정은 앱 시작 시 `register_settings(get_boltz2_settings())`로 등록하여 `platform_core.config.get_settings()`가 서브클래스(`Boltz2Settings`)를 반환하도록 한다.

### 인증 — 3개 경로가 모두 하나의 `ApiKey`로 수렴

모든 최종 인증 매체는 `x-api-key` (또는 OAuth Bearer로 포장된 동일 값)이다.

1. **웹 로그인 (Supabase Google OAuth)** — `/auth/login` → Supabase `authorize` → `/auth/callback`에서 `auth_code`를 JWT로 교환, `verify_supabase_jwt`로 검증, `Profile` upsert, `on_user_authenticated` 훅이 도메인 룰(`shaperon.com` 자동승인)에 따라 API Key 자동 발급.
2. **Device Authorization Flow** — 브라우저 없는 MCP 클라이언트용. `/auth/device-code`로 `user_code`/`device_code` 쌍 발급(TTL 15분) → 사용자가 브라우저에서 `/auth/device-verify?user_code=...` 승인 → 클라이언트는 `/auth/device-token`을 polling하여 `api_key` 수령. 평문 키는 `TTLCache`에 메모리 보관.
3. **MCP OAuth 2.1** (`Boltz2OAuthProvider`) — Claude Code Streamable HTTP 플로우. FastAPI 루트에서 RFC 9728 `.well-known/oauth-authorization-server` / `.well-known/oauth-protected-resource`를 직접 서빙 (FastMCP가 아님). OAuth `access_token` 자체가 API Key 평문.

#### MCP OAuth의 Supabase 콜백 특이점

- Supabase는 access_token을 **URL hash fragment**(`#access_token=...`)로만 반환한다. 서버는 hash를 읽을 수 없으므로 `handle_oauth_callback`는 2-phase로 동작한다:
  - **Phase 1 (GET)**: HTML을 서빙. 클라이언트 JS가 `window.location.hash`에서 토큰을 추출하여 form POST로 재전송.
  - **Phase 2 (POST)**: 서버가 토큰을 `verify_supabase_jwt`로 검증 → Profile upsert → MCP auth code 발급 → Claude Code redirect_uri로 302.
- `session_id`는 **URL path**에 담는다 (`/oauth/callback/{session_id}`). Supabase 대시보드의 redirect URL 등록은 query string 파라미터를 매칭하지 못하기 때문.
- Claude Code는 public client(PKCE) — `register_client`에서 `client_secret`을 강제하지 않는다.

모든 MCP 도구는 `with mcp_auth(api_key) as (db, key):` 패턴을 사용한다. `api_key`가 비어 있으면 `get_access_token()`의 Bearer로 fallback. 도구별 에러는 `@_mcp_error_handler`가 `{"error": ...}`로 변환한다(raise 안 함).

### Job 생애주기 (Azure)

```
Client → API (POST /v1/boltz2/prediction-jobs)
         ├─ Boltz2Spec(rendered_yaml) + Boltz2Job row 생성
         └─ Service Bus(boltz2-predict-jobs)에 {"job_id": ...} publish
         │
         ▼
KEDA servicebus-queue 스케일 룰 (messageCount=1) 감지
         │
         ▼
ACA Job Worker replica 기동 (A100 워크로드 프로파일)
         │
         ├─ QueueConsumer.receive_one() — 메시지 1건 pull
         ├─ AutoLockRenewer 등록 (boltz2_run_timeout_seconds or 86400초)
         ├─ JobProcessor.process(job_id)
         │    ├─ _mark_running: status=running, stage=preparing
         │    ├─ Blob download (boltz2-inputs) → TemporaryDirectory
         │    ├─ Boltz2Runner.run(spec.yaml, output_dir) — boltz CLI subprocess
         │    │   ├─ stdout line handler — STEP_PATTERN 파싱해 progress_percent 업데이트
         │    │   │   (job_heartbeat_interval_seconds throttle)
         │    │   └─ 별도 thread `_watch_for_cancel` — 5초마다 DB 조회해 canceled면 JobCanceledException
         │    ├─ bundle_output → results.zip + 개별 *.cif/*.pdb/*.json
         │    ├─ Blob upload (boltz2-results)
         │    └─ _mark_succeeded: artifact_manifest 저장
         ├─ consumer.ack() — Service Bus complete
         └─ main() return → replica 종료 (one-shot)
```

- **멱등성**: Worker는 `job.status ∈ {succeeded, failed, canceled}`면 스킵. `submit_job`은 `client_request_id`(idempotency key)로 중복 제출 방지.
- **SIGTERM Graceful Shutdown**: ACA가 replica 종료 시 `_sigterm_handler`가 in-flight 메시지를 ack 후 `sys.exit(0)`. 절대 재처리되지 않도록.
- **24시간 lock 연장**: `AutoLockRenewer`가 `max_lock_renewal_duration=86400`까지 Service Bus lock을 자동 갱신 (기본 lock-duration 5분).
- **취소**: `cancel_job` API가 `Boltz2Job.status=canceled` 설정 → worker thread가 감지 → 현재 subprocess kill + `_upload_artifact` skip.
- **로그 스트리밍**: `aca_logs.py`가 Managed Identity로 Azure Log Analytics를 조회. `aca_deploy.sh`는 API의 system-assigned identity에 worker job scope `Contributor` role을 할당한다.

### 데이터 스토리지

| 리소스 | 역할 |
|--------|------|
| Supabase PostgreSQL (Tokyo, session pooler IPv4) | `Profile`, `ApiKey`, `DeviceCode`, `Boltz2Asset`, `Boltz2Spec`, `Boltz2Job` |
| Azure Blob `boltz2-inputs` | 사용자 업로드 `.cif`/`.pdb`, spec YAML |
| Azure Blob `boltz2-results` | 잡 artifacts — `results.zip`, `input_spec.yaml`, `run_manifest.json`, 개별 `*.cif`/`*.pdb`/`*.json` |
| Azure Service Bus `boltz2-predict-jobs` | 잡 큐. lock-duration PT5M, max-delivery-count 3 |
| Azure Container Registry `shaperon.azurecr.io` | `boltz2-api`, `boltz2-worker` 이미지 |

- `BLOB_BACKEND=local|azure` / `QUEUE_BACKEND=local|azure` 로 dev/prod 전환. local 모드는 `.local-storage/queue/*.jsonl` 파일 + 로컬 디렉터리를 사용한다.
- 업로드 SAS URL은 `x-ms-blob-type: BlockBlob` 헤더 필수. `create_upload_url`이 반환하는 `curl_hint`에 포함되어 있다.

### 데이터베이스 드라이버 정규화

`platform_core.db.get_engine()`이 URL을 자동 재작성한다:

- `postgresql://` → `postgresql+psycopg://` (psycopg **v3**, psycopg2 아님)
- `postgresql+psycopg2://` → `postgresql+psycopg://`

Supabase Pooler(transaction mode) 호환을 위해 `NullPool` + `prepare_threshold=0`이 강제된다. 배포 스크립트/`.env`에서 드라이버 prefix를 하드코딩하지 말 것 — 정규화 로직만 신뢰.

### MCP 도구 (14개)

`get_my_api_key`, `create_upload_url`, `upload_structure`, `validate_spec`, `render_template`, `submit_job`, `get_job`, `list_jobs`, `cancel_job`, `get_logs`, `get_artifacts`, `list_templates`, `list_workers`, `submit_nanobody_structure_prediction`.

- `submit_nanobody_structure_prediction`은 **cross-model workflow** — 상위 `boltzgen_MSA` 서비스가 설계한 나노바디 서열 + 사전 업로드된 타겟 asset을 받아, Boltz-2 v1 YAML 생성 → 검증 → 제출을 한 번에 수행한다.
- ⚠️ `list_workers`는 현재 **stub** (하드코딩된 `{"workers": [], "total": 0, "message": "ACA worker management not yet configured."}` 반환). README/mcp 서버 모듈 docstring의 "13 tools" 표기와 실제 등록 수(14)가 맞지 않는 것과 같은 맥락의 미완성 흔적.
- ⚠️ MCP `get_logs` 도구는 `AcaLogService`를 호출하지 않고 DB 진행률만 반환한다. 실제 ACA 로그 스트리밍은 **REST 공개 엔드포인트(`/logs/public`, `/logs/public/text`)에서만** 동작한다. 도구 docstring의 "tail: Reserved for future ACA log streaming" 문구는 stale.

### Azure Container Apps 배포 (`scripts/aca_deploy.sh`)

- **API**: `az containerapp create/update`, ingress external, target-port 8001.
- **Worker**: `az containerapp job create/update --trigger-type Event`, parallelism 10, replica-retry-limit 0, polling-interval 15초, scale rule `servicebus-queue`(`messageCount=1`), scale-rule-auth secretref `sbconn`.
- **시크릿**: `--secrets supurl=... supanon=... supjwt=... dburl=... sturl=... stname=... stkey=... sbconn=... smtpuser=... smtppwd=... smtpfrom=...` (이름은 스크립트 상수). 환경 변수는 `secretref:<name>`으로만 참조.
- **Managed Identity**: API에 system-assigned identity 부여 → worker job scope에 `Contributor` 할당 → API에서 ACA 로그 스트리밍 가능.
- 스크립트는 idempotent — 리소스 존재 시 `update`, 없으면 `create`.

## 주의 사항

- **`boltz2_service` ↔ `platform_core` import 순환을 만들지 말 것.** 설정은 `register_settings` 한 방향으로만 흐른다.
- **`Boltz2Settings`에 필드 추가 시** — `.env.example`과 `aca_deploy.sh`의 `api_env_vars` / `worker_env_vars` 양쪽을 갱신. 민감값이면 `secrets_args`에도 추가.
- **MCP 루트 `.well-known/...`** 은 FastAPI 앱이 직접 서빙한다 (FastMCP 아님). Claude Code의 OAuth discovery(RFC 9728)가 루트 경로에서만 작동하기 때문. 이동/삭제 금지.
- **Supabase redirect URL 등록**: `mcp_issuer_url + "/oauth/callback/{session_id}"` 형태. Supabase 대시보드에는 prefix까지만 등록해야 path parameter가 매칭됨.
- **Worker는 one-shot**: `receive_one()`이 `None`이면 조용히 종료. 루프 돌려서 여러 잡을 처리하지 말 것 — KEDA 스케일 룰이 리플리카를 더 띄우는 책임을 진다.
- **A100 텐서 코어**: 워커는 float32 matmul precision을 `'high'`로 설정한다 (commit `a515e1e`). 성능 이유 없이 `'highest'`로 되돌리지 말 것.
- **cuEquivariance**: `worker.Dockerfile`에서 best-effort 설치. 실패해도 `--no_kernels` fallback — hard failure로 바꾸지 말 것.
- **Ruff 룰은 의도적으로 좁음** (E+F만, E501 무시). 확장하려면 사전 논의.
- **`bioai_platform.db`** — 루트에 커밋된 SQLite 파일은 로컬 dev용. Azure 경로에서는 `DATABASE_URL`이 Supabase를 가리키므로 무관.
- **스펙 검증은 API CPU에서 실행**. `api.Dockerfile`에 `boltz==2.2.0`이 설치되어 있는 이유 — validation만 CPU로 돌리고, GPU는 `boltz predict`에만 쓴다.
- **MCP 도구 에러는 raise 금지**. 모두 `@_mcp_error_handler`가 감싸므로 `ValueError`/`HTTPException`를 던지면 `{"error": ...}` dict로 변환되어 Claude Code에 표시된다.

## 관련 레포

- `boltz2_skill` — 이 플랫폼용 Claude Code 스킬 (별도 레포).
- `boltzgen_MSA` — 상위 나노바디 디자인 서비스. 출력이 `submit_nanobody_structure_prediction`으로 들어온다.
- `boltz/` — git submodule, upstream Boltz-2 모델 코드.
