# boltz2_MSA — Bio AI Multi-Model Platform

## Overview

Boltz-2 단백질 구조 예측 서비스. FastAPI REST API + MCP(Model Context Protocol) 서버 + GPU Worker로 구성.
boltzgen(나노바디 디자인) → Boltz-2(구조 예측) cross-model workflow 지원.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Clients                           │
│  Claude Code (MCP) │ REST API │ Swagger UI           │
└──────────┬─────────┴──────────┴─────────────────────┘
           │
┌──────────▼──────────────────────────────────────────┐
│              API Server (FastAPI)                     │
│  OAuth 2.1 (MCP) │ Google OAuth │ API Key Auth       │
│  21 REST endpoints │ 13 MCP tools │ Device Auth      │
│  Spec validation (boltz CPU) │ Job submission        │
│  Port 8001 │ Azure Container Apps                    │
└──────┬─────────────┬───────────────┬────────────────┘
       │             │               │
┌──────▼──────┐ ┌────▼────┐  ┌──────▼──────────────┐
│  Supabase   │ │  Azure  │  │   Azure Service Bus  │
│  PostgreSQL │ │  Blob   │  │  (boltz2-predict-    │
│  (Tokyo)    │ │ Storage │  │   jobs queue)        │
│  profiles   │ │ inputs  │  └──────┬───────────────┘
│  api_keys   │ │ results │         │ auto-scale
│  specs/jobs │ └─────────┘  ┌──────▼───────────────┐
│  device_codes│              │   GPU Worker (A100)  │
└─────────────┘              │  boltz predict CLI   │
                             │  Job processing      │
                             │  Artifact upload      │
                             │  Azure Container Apps │
                             │  Event-triggered      │
                             │  0→10 auto-scale     │
                             └──────────────────────┘
```

## Features

- **13 MCP Tools**: create_upload_url, upload_structure, validate_spec, render_template, submit_job, get_job, list_jobs, cancel_job, get_logs, get_artifacts, list_templates, list_workers, submit_nanobody_structure_prediction
- **21+ REST API Endpoints**: Full CRUD for uploads, specs, jobs, auth, device auth, health + 공개 로그 스트리밍
- **OAuth 2.1**: Claude Code HTTP MCP transport with Google login via Supabase
- **Device Auth Flow**: MCP clients without browser access
- **Cross-Model Workflow**: boltzgen nanobody design → Boltz-2 structure prediction (one-step MCP tool)
- **GPU Auto-Scaling**: Worker scales 0→10 based on Service Bus queue depth
- **Spec Validation**: CPU-based validation on API server (no GPU needed)
- **Gmail SMTP 알림**: 작업 상태/단계 변경 시 이메일 알림 발송
- **Worker 병렬 처리**: 동시 실행 10, 동시 작업 제한 5
- **Worker 무한 대기**: timeout=0 (무한 대기), 메시지 lock 24시간 자동 갱신
- **SIGTERM Graceful Shutdown**: Worker 종료 시 진행 중 작업 안전 완료
- **공개 로그 스트리밍**: 인증 없이 작업 로그 및 상태 조회 가능
- **Worker 멱등성**: 터미널 상태 작업 자동 건너뛰기 (중복 처리 방지)

## Azure Infrastructure

| Resource | Name | Details |
|----------|------|---------|
| API | boltz2-api | West US 3, 1 CPU, 2Gi, Container Apps |
| Worker | boltz2-worker | A100 GPU, 8 CPU, 32Gi, Event-triggered |
| Storage | nanomapstorage | boltz2-inputs / boltz2-results containers |
| Queue | boltz2-predict-jobs | Service Bus, lock-duration 5분, max-delivery-count 3 |
| DB | Supabase (nanomapAIDEN) | PostgreSQL, Tokyo, session pooler (IPv4) |
| ACR | shaperon.azurecr.io | boltz2-api / boltz2-worker images |

## API URL

```
https://boltz2-api.politebay-55ff119b.westus3.azurecontainerapps.io
```

- Swagger UI: `/docs`
- Health: `/healthz`
- MCP: `/mcp/mcp`
- OAuth metadata: `/.well-known/oauth-authorization-server`

### 공개 엔드포인트 (인증 불필요)

```
GET /v1/boltz2/prediction-jobs/{job_id}/logs/public?tail=200
GET /v1/boltz2/prediction-jobs/{job_id}/logs/public/text?tail=50
GET /v1/boltz2/prediction-jobs/{job_id}/status/public
```

## Quick Start

### Local Development

```bash
# Clone with submodule
git clone --recursive https://github.com/SungminKo-smko/boltz2_MSA.git
cd boltz2_MSA

# Setup
cp .env.example .env  # Edit with your Supabase credentials
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]" "mcp[cli]>=1.0"

# Run API server
uvicorn boltz2_service.api.app:create_app --factory --port 8001 --reload

# Run MCP server (stdio mode)
python -m boltz2_service.mcp.stdio
```

### Docker Build

```bash
# API image
docker build -f api.Dockerfile -t boltz2-api .

# Worker image (requires NVIDIA CUDA)
docker build -f worker.Dockerfile -t boltz2-worker .
```

### Azure Deployment

```bash
# Set required env vars (see .env.example for full list)
export RESOURCE_GROUP=... CONTAINERAPPS_ENV=... ACR_LOGIN_SERVER=...
# ... other required vars

# Deploy
./scripts/aca_deploy.sh
```

## MCP Integration (Claude Code)

```bash
# Install skill
git clone https://github.com/SungminKo-smko/boltz2_skill ~/.claude/skills/boltz2-predict

# Register MCP server (HTTP with OAuth)
claude mcp add --transport http --callback-port 9999 boltz2 \
  https://boltz2-api.politebay-55ff119b.westus3.azurecontainerapps.io/mcp/mcp

# Or local stdio mode
claude mcp add boltz2 python3 -m boltz2_service.mcp.stdio
```

## Project Structure

```
boltz2_MSA/
├── boltz/                      # git submodule (jwohlwend/boltz)
├── src/
│   ├── boltz2_service/         # Boltz-2 API + MCP + Worker
│   │   ├── api/                # FastAPI app + routes (auth, jobs, specs, uploads)
│   │   ├── mcp/                # MCP server (13 tools) + OAuth provider
│   │   ├── worker/             # GPU job processor + queue consumer
│   │   ├── services/           # Business logic (jobs, spec renderer/validator)
│   │   ├── schemas/            # Pydantic request/response models
│   │   ├── models.py           # SQLAlchemy ORM (Asset, Spec, Job)
│   │   ├── repositories.py    # Data access layer
│   │   ├── config.py          # Boltz2Settings
│   │   └── enums.py           # StrEnum definitions
│   └── platform_core/          # Shared platform infrastructure
│       ├── auth/               # Supabase JWT (ES256/HS256) + API Key + domain rules
│       ├── models/             # Profile, ApiKey, DeviceCode
│       ├── services/           # Blob storage + queue abstractions
│       ├── config.py           # PlatformSettings
│       ├── db.py               # SQLAlchemy + Supabase pooler (psycopg 자동 정규화)
│       ├── notifications.py    # Gmail SMTP 이메일 알림
│       └── security.py         # API key generation/hashing
├── scripts/
│   ├── aca_deploy.sh           # Azure Container Apps deployment
│   └── setup_trigger.sql       # Supabase profile auto-creation trigger
├── api.Dockerfile              # API image (Python 3.11 + boltz CPU)
├── worker.Dockerfile           # Worker image (CUDA 12.2 + boltz GPU)
├── pyproject.toml
└── .env.example
```

## Tech Stack

- **API**: FastAPI, SQLAlchemy 2.0, Pydantic 2.0
- **MCP**: FastMCP 1.26+ (Streamable HTTP + stdio)
- **Auth**: Supabase Google OAuth, JWT (ES256/HS256), API Key
- **DB**: PostgreSQL (Supabase, session pooler IPv4, psycopg 드라이버 자동 정규화)
- **Storage**: Azure Blob Storage
- **Queue**: Azure Service Bus
- **Compute**: Azure Container Apps (API) + Container Apps Jobs (GPU Worker)
- **Notification**: Gmail SMTP (작업 상태/단계 변경 알림)
- **Logging**: ACA Log Streaming via Managed Identity
- **CI/CD**: GitHub Actions (API + Worker 이미지 빌드)
- **ML**: Boltz-2 2.2.x (structure prediction)

## Related Repositories

- [boltz2_skill](https://github.com/SungminKo-smko/boltz2_skill) — Claude Code skill for this platform
- [boltzgen_MSA](https://github.com/SungminKo-smko/boltzgen_MSA) — Nanobody design service (upstream)
- [boltz](https://github.com/jwohlwend/boltz) — Boltz-2 open source (submodule)
