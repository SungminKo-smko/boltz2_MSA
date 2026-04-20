# 배포 테스트 계획 — `.env` / `.env.example` 변경 검증

**목표**: 방금 수정한 `.env`와 (선택) 스크립트 변경이 프로덕션에서 정상 동작하는지 검증. 이미지는 재빌드 없이 **현재 프로덕션 태그 `sha-2b12c68ce1fd944dac3fa7a0cffb96eb433516b6`에 pinning**.

**비목표**: 코드 변경 검증 (이미지가 같으므로 앱 동작은 변하지 않음). 이번 테스트는 **배포 구성/크리덴셜/스크립트**만 확인.

---

## 전제 / 고정값

| 항목 | 값 |
|------|-----|
| Pinned API 이미지 | `shaperon.azurecr.io/boltz2-api:sha-2b12c68ce1fd944dac3fa7a0cffb96eb433516b6` |
| Pinned Worker 이미지 | `shaperon.azurecr.io/boltz2-worker:sha-2b12c68ce1fd944dac3fa7a0cffb96eb433516b6` |
| Resource Group | `nanobody-designer-897d0b-rg` |
| ACA Managed Env | `nanobody-aca-897d0b-env` |
| Service Bus Namespace | `nanobodydsb897d0b` |
| Storage Account | `nanomapstorage` |
| Supabase DB | 공유 (읽기/쓰기 모두 production과 동일 DB) |

---

## 격리 전략 (공유 ↔ 분리 매트릭스)

| 리소스 | 프로덕션 | 테스트 | 이유 |
|--------|----------|--------|------|
| API Container App | `boltz2-api` | **`boltz2-api-staging`** | 트래픽 완전 분리 |
| Worker ACA Job | `boltz2-worker` | **`boltz2-worker-staging`** | KEDA 스케일 룰도 별도 |
| Service Bus Queue | `boltz2-predict-jobs` | **`boltz2-predict-jobs-staging`** | 프로덕션 Worker가 테스트 잡 집지 않도록 |
| Managed Identity | 기존 MI | **신규 MI 자동 생성** | staging 앱 전용 |
| Blob Storage | 공유 (`boltz2-inputs`, `boltz2-results`) | 동일 | 파일 경로는 UUID 키잉 → 충돌 없음. 정리 시 `test-*` prefix 권장 |
| Supabase DB | 공유 | 동일 | 스키마 동일(이미지 같음). 테스트 데이터는 `test-` prefix api_key로 격리 |
| ACR | 공유 | 동일 | pull만, 양방향 영향 없음 |
| ACA Managed Env | 공유 | 동일 | 같은 VNet/프로파일 활용 |

**핵심 안전장치**:
- 스테이징 Worker가 **자기 큐만** 바라보므로 프로덕션 큐에 들어온 실제 잡에 영향 없음
- 프로덕션 Worker가 스테이징 큐를 모르므로 테스트 잡이 프로덕션 Worker로 흘러가지 않음
- DB는 공유지만 row-level이라 `client_request_id='staging-smoke-<ts>'` 같은 tagging으로 구분 가능

---

## 위험 요소 & 대응

| 위험 | 영향 | 대응 |
|------|------|------|
| `aca_deploy.sh`가 프로덕션 API를 `latest` 태그로 덮어씀 | 운영 중단 | **`API_IMAGE_TAG` / `WORKER_IMAGE_TAG`를 현재 prod SHA로 명시 고정** (아래 Phase 2 참조) |
| `API_APP_NAME`/`WORKER_JOB_NAME`을 덮어쓰지 않고 실행 | prod 환경변수 replace (downtime) | 반드시 **staging 접미사**로 export 후 실행 |
| 스테이징이 프로덕션 큐를 구독 | 프로덕션 잡이 스테이징 워커로 | `SERVICE_BUS_QUEUE_NAME=...-staging` 필수 |
| A100 워커 비용 | 테스트 1잡 ≈ 수분 실행, 수 USD | 최소 샘플링(`diffusion_samples=1`), 테스트 후 즉시 teardown |
| DB에 테스트 row 잔존 | 장기적으로 통계 오염 | teardown 단계에서 `client_request_id LIKE 'staging-%'` 삭제 |
| Supabase schema 충돌 | 동시 `init_db` 호출 시 `CREATE TABLE IF NOT EXISTS`로 안전 | 이미지 SHA 동일 → 스키마 동일, 충돌 없음 |

---

## Phase 0 — Dry-run & 사전 점검 (Azure 변경 없음)

```bash
cd /Users/kosungmin/workspace/boltz2_MSA

# 0.1 .env 로드 검증 (required_vars 15개 + SERVICE_BUS 완전값)
bash -c 'set -a; source .env; set +a; for v in RESOURCE_GROUP CONTAINERAPPS_ENV ACR_LOGIN_SERVER ACR_USERNAME ACR_PASSWORD DATABASE_URL SUPABASE_URL SUPABASE_ANON_KEY SUPABASE_JWT_SECRET AZURE_STORAGE_ACCOUNT_URL AZURE_STORAGE_ACCOUNT_NAME AZURE_STORAGE_ACCOUNT_KEY SERVICE_BUS_NAMESPACE SERVICE_BUS_QUEUE_NAME SERVICE_BUS_CONNECTION_STRING; do [[ -z "${!v}" ]] && echo "MISSING: $v"; done; echo "SERVICE_BUS len: ${#SERVICE_BUS_CONNECTION_STRING}"'
# 기대: 누락 없음 + SERVICE_BUS len ≥ 160

# 0.2 ACR 이미지 존재 확인
az acr repository show --name shaperon --image boltz2-api:sha-2b12c68ce1fd944dac3fa7a0cffb96eb433516b6 -o table
az acr repository show --name shaperon --image boltz2-worker:sha-2b12c68ce1fd944dac3fa7a0cffb96eb433516b6 -o table

# 0.3 현재 prod 상태 스냅샷 (롤백 비교 기준)
az containerapp show -g nanobody-designer-897d0b-rg -n boltz2-api --query "{image:properties.template.containers[0].image, revision:properties.latestRevisionName}" -o json > /tmp/prod-snapshot-api.json
az containerapp job show -g nanobody-designer-897d0b-rg -n boltz2-worker --query "{image:properties.template.containers[0].image, scaleRules:properties.configuration.eventTriggerConfig.scale.rules}" -o json > /tmp/prod-snapshot-worker.json
cat /tmp/prod-snapshot-api.json /tmp/prod-snapshot-worker.json

# 0.4 Azure CLI 로그인/구독 확인
az account show --query "{name:name, id:id}" -o table
# 기대: subscription id = e80f86e3-b865-4248-92a5-90eb190f8bb7
```

**Gate**: 위 4개 모두 문제 없으면 Phase 1로.

---

## Phase 1 — 격리 리소스 프로비저닝

```bash
# 1.1 테스트용 Service Bus 큐 생성 (프로덕션과 동일 설정)
az servicebus queue create \
  -g nanobody-designer-897d0b-rg \
  --namespace-name nanobodydsb897d0b \
  -n boltz2-predict-jobs-staging \
  --lock-duration PT5M \
  --max-delivery-count 3

# 1.2 큐 존재 확인
az servicebus queue show -g nanobody-designer-897d0b-rg --namespace-name nanobodydsb897d0b -n boltz2-predict-jobs-staging --query "{status:status, lock:lockDuration}" -o table
```

**롤백**: `az servicebus queue delete -g ... -n boltz2-predict-jobs-staging`

---

## Phase 2 — 스테이징 스택 배포 (`aca_deploy.sh` 재사용)

**핵심**: 기존 스크립트를 수정 없이 환경변수 오버라이드만으로 staging 배포.

```bash
# 2.1 staging overlay 로드
set -a; source .env; set +a

# 2.2 staging 전용 오버라이드
export API_APP_NAME=boltz2-api-staging
export WORKER_JOB_NAME=boltz2-worker-staging
export API_IMAGE_TAG=sha-2b12c68ce1fd944dac3fa7a0cffb96eb433516b6
export WORKER_IMAGE_TAG=sha-2b12c68ce1fd944dac3fa7a0cffb96eb433516b6
export SERVICE_BUS_QUEUE_NAME=boltz2-predict-jobs-staging

# 2.3 최종 확인 — 반드시 "-staging" 접미사 확인 후 진행
echo "API_APP_NAME=$API_APP_NAME"
echo "WORKER_JOB_NAME=$WORKER_JOB_NAME"
echo "API_IMAGE_TAG=$API_IMAGE_TAG"
echo "WORKER_IMAGE_TAG=$WORKER_IMAGE_TAG"
echo "SERVICE_BUS_QUEUE_NAME=$SERVICE_BUS_QUEUE_NAME"

# 2.4 배포 실행 (약 2-4분 소요)
./scripts/aca_deploy.sh

# 2.5 FQDN 확인
STAGING_FQDN=$(az containerapp show -g nanobody-designer-897d0b-rg -n boltz2-api-staging --query properties.configuration.ingress.fqdn -o tsv)
echo "Staging API: https://$STAGING_FQDN"
```

**Gate**: 스크립트가 `Deploy complete!` 출력 + staging FQDN 응답.

---

## Phase 3 — E2E Smoke Test

### 3.1 기본 헬스체크 (인증 불필요)

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" https://$STAGING_FQDN/healthz
# 기대: HTTP 200

curl -s https://$STAGING_FQDN/.well-known/oauth-authorization-server | python3 -m json.tool | head -20
# 기대: issuer URL이 prod URL로 반환됨 (MCP_ISSUER_URL 미설정이므로 config.py default 사용)
# → 이건 운영 OAuth 경로로 유도됨. 의도 테스트라면 별도 처리 필요 (아래 noted issue).
```

### 3.2 API Key 획득

테스트 사용자의 DB row는 프로덕션과 공유이므로, **기존 API Key 재사용**이 가장 간편:

```bash
# 본인 API Key를 이미 보유 중이라면 재사용
export API_KEY="<본인의 b2_ 로 시작하는 키>"

# 없으면 프로덕션 /mcp 로그인 플로우로 발급 받아와서 사용
```

### 3.3 업로드 → 검증 → 제출 → 상태 체크 (E2E)

```bash
# 테스트용 작은 타겟 파일 준비 (예: 아무 PDB 아미노 30~50개)
echo "준비: /tmp/test-target.pdb"

# (A) upload URL 생성
resp=$(curl -s -X POST "https://$STAGING_FQDN/v1/boltz2/uploads" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"filename": "test-target-staging.pdb"}')
echo "$resp" | python3 -m json.tool
ASSET_ID=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['asset_id'])")
UPLOAD_URL=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['upload_url'])")

# (B) blob 업로드
curl -s -X PUT -T /tmp/test-target.pdb \
  -H "x-ms-blob-type: BlockBlob" \
  -H "Content-Type: chemical/x-pdb" \
  "$UPLOAD_URL"

# (C) spec 렌더 (템플릿 기반)
spec_resp=$(curl -s -X POST "https://$STAGING_FQDN/v1/boltz2/specs:render" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"template_name\": \"boltz2_structure_prediction\", \"target_asset_id\": \"$ASSET_ID\"}")
SPEC_ID=$(echo "$spec_resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['spec_id'])")

# (D) job 제출 (최소 파라미터 — A100 시간 절약)
job_resp=$(curl -s -X POST "https://$STAGING_FQDN/v1/boltz2/prediction-jobs" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"spec_id\": \"$SPEC_ID\", \"prediction_type\": \"structure\", \"runtime_options\": {\"diffusion_samples\": 1, \"sampling_steps\": 50, \"recycling_steps\": 1}, \"client_request_id\": \"staging-smoke-$(date +%s)\"}")
JOB_ID=$(echo "$job_resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
echo "Job: $JOB_ID"

# (E) KEDA가 staging 큐 메시지를 감지해 worker 스케일업 하는지 관찰
watch -n 10 "az containerapp job execution list -g nanobody-designer-897d0b-rg -n boltz2-worker-staging --query '[:3].{name:name, status:properties.status, start:properties.startTime}' -o table"
# 기대: 30-60초 내 replica 1개 Running, 이후 Succeeded

# (F) job 상태 폴링
while true; do
  status=$(curl -s "https://$STAGING_FQDN/v1/boltz2/prediction-jobs/$JOB_ID" -H "x-api-key: $API_KEY" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'],d.get('current_stage'),d.get('progress_percent'))")
  echo "$(date +%H:%M:%S) $status"
  [[ "$status" == succeeded* || "$status" == failed* ]] && break
  sleep 30
done

# (G) artifact 다운로드 URL 생성
curl -s "https://$STAGING_FQDN/v1/boltz2/prediction-jobs/$JOB_ID/artifacts" -H "x-api-key: $API_KEY" | python3 -m json.tool
```

### 3.4 Verification Checklist

| 항목 | 기대 결과 | 실패 시 원인 후보 |
|------|----------|------------------|
| `/healthz` | 200 | API 기동 실패, DB 접속 실패 |
| API Key 인증 | 토큰 유효 | DATABASE_URL 오타/JWT_SECRET 불일치 |
| Upload URL 생성 | `upload_url` 반환 | `AZURE_STORAGE_ACCOUNT_*` 오류, SAS 권한 |
| Blob PUT 성공 | HTTP 201 | storage key 잘림, CORS 문제 |
| Spec render | `spec_id` 반환 | `boltz_service` CPU 검증 실패 |
| Job 제출 | `job_id` 반환 + queued | Service Bus connection string 잘림(⚠ `;` quoting 체크) |
| KEDA 스케일업 | 60초 내 replica 기동 | `SERVICE_BUS_NAMESPACE` 오타, queue 이름 불일치 |
| Worker execution | Succeeded | A100 워크로드 프로파일 이슈, GPU 할당 실패 |
| Artifacts | `results.zip` URL | 결과 blob 업로드 실패 |

---

## Phase 4 — 정리 (teardown)

```bash
# 4.1 staging 리소스 삭제
az containerapp delete -g nanobody-designer-897d0b-rg -n boltz2-api-staging --yes
az containerapp job delete -g nanobody-designer-897d0b-rg -n boltz2-worker-staging --yes
az servicebus queue delete -g nanobody-designer-897d0b-rg --namespace-name nanobodydsb897d0b -n boltz2-predict-jobs-staging

# 4.2 테스트 Blob 정리 (선택 — 비용 미미)
# azcopy / az storage blob delete-batch 로 prefix 삭제
az storage blob delete-batch --account-name nanomapstorage --account-key "$AZURE_STORAGE_ACCOUNT_KEY" --source boltz2-inputs --pattern "targets/test-target-staging*"

# 4.3 DB row 정리 (선택 — staging- prefix client_request_id)
# Supabase SQL editor 에서:
# DELETE FROM boltz2_jobs WHERE client_request_id LIKE 'staging-smoke-%';
# DELETE FROM boltz2_assets WHERE filename LIKE 'test-target-staging%';

# 4.4 프로덕션 상태 재확인 (Phase 0 스냅샷과 비교)
az containerapp show -g nanobody-designer-897d0b-rg -n boltz2-api --query "{image:properties.template.containers[0].image, revision:properties.latestRevisionName}" -o json
diff /tmp/prod-snapshot-api.json <(az containerapp show -g nanobody-designer-897d0b-rg -n boltz2-api --query "{image:properties.template.containers[0].image, revision:properties.latestRevisionName}" -o json)
# 기대: diff 없음 (프로덕션 완전 무영향)
```

---

## 결정 기준 — 프로덕션 재배포 여부

Phase 3 전부 통과하면:
- **변경점이 `.env`만**: 프로덕션 재배포 불필요. GitHub Secret들이 동일하다면 CI가 기존대로 동작하고, 로컬 `.env`는 로컬 배포 수단일 뿐.
- **GitHub Secret 갱신/추가가 있었다면**: 갱신 후 `workflow_dispatch`로 CI 재배포 or `./scripts/aca_deploy.sh`를 prod 타겟으로 실행 (`API_APP_NAME=boltz2-api`, SHA 고정).

---

## Known Issues / 대비

1. **MCP OAuth 스테이징 테스트**: `mcp_issuer_url` 기본값이 prod URL이라 staging에서 OAuth 플로우를 끝까지 돌면 prod로 리다이렉트됨. 필요 시 스테이징 API의 env에 `MCP_ISSUER_URL=https://$STAGING_FQDN/mcp` 추가 배포.
2. **Supabase row 공유**: 테스트 잡/asset이 프로덕션 DB에 기록됨. `client_request_id` prefix로 구분하면 쉬움.
3. **Managed Identity role 상속**: staging MI는 worker-staging job에만 Contributor — prod worker 로그는 못 읽음. 의도됨.
4. **aca_deploy.sh가 `--replace-env-vars` 사용**: staging에만 영향. prod 앱은 건드리지 않음.

---

## 대안 (더 가볍게)

### 대안 A — 로컬 API + 스테이징 큐 (워커 없음)

Worker GPU가 필요 없는 API/MCP 경로만 검증할 때:

```bash
set -a; source .env; set +a
export SERVICE_BUS_QUEUE_NAME=boltz2-predict-jobs-staging
uvicorn boltz2_service.api.app:create_app --factory --port 8001
# 제출된 잡은 큐에 쌓이지만 처리기가 없으므로 종국엔 dead-letter
# DB/Blob/Queue 통합만 점검
```

**장점**: 무료, 빠름. **단점**: Worker/KEDA/A100 경로 미검증.

### 대안 B — prod에 신규 revision 배포 후 0% 트래픽

`boltz2-api`를 multi-revision 모드로 전환 후 새 revision 추가, 트래픽 0%로 고정하고 revision 직접 FQDN으로 테스트. **Worker에는 적용 불가** (ACA Jobs는 revision 개념 없음). 부분 검증만 가능하므로 비추천.

---

## 실행 순서 요약

1. **Phase 0**: .env 파싱/ACR 이미지/prod snapshot 확인 (5분)
2. **Phase 1**: staging 큐 생성 (1분)
3. **Phase 2**: `./scripts/aca_deploy.sh` with staging 오버라이드 (3-5분)
4. **Phase 3**: E2E 스모크 테스트 + Worker 잡 1건 실행 (10-20분)
5. **Phase 4**: teardown + prod 무영향 확인 (2분)

**총 소요**: ~30분, **총 비용**: ~$5 (A100 1회 short run)
