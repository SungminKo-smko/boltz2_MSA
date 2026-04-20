# 코드 찌꺼기 보고 (2026-04-17)

실제 Azure 배포 상태와 대조해 발견한 미사용·stale·드리프트 항목.

## 우선순위 높음 (삭제/배선만으로 정리 가능)

### 1. MCP `list_workers` — 빈 stub
파일: `src/boltz2_service/mcp/server.py:640`
하드코딩된 `{"workers": [], "total": 0, "message": "ACA worker management not yet configured..."}` 반환.
README는 이 도구를 "기능"으로 홍보하지만 실제로는 미구현. **삭제 또는 실제 구현 필요**.

### 2. MCP `get_logs` — 구현 연결 누락
파일: `src/boltz2_service/mcp/server.py:553`
DB의 `job.progress_percent`, `status_message`만 반환하고 끝. `AcaLogService`를 전혀 호출하지 않음. docstring의 `tail: Reserved for future ACA log streaming`은 거짓말 — 이미 `AcaLogService`가 `api/routes/jobs.py`에서 REST 공개 엔드포인트용으로 **완전 구현**되어 있음. MCP 쪽도 같은 서비스 호출로 이어주는 배선만 하면 됨.

### 3. `Boltz2Settings` 미사용 필드
파일: `src/boltz2_service/config.py`
`grep -rn`으로 `src/` 전체 확인 결과 **참조 0건**:
- `msa_server_username` (line 40)
- `msa_server_password` (line 41)
- `default_max_diffusion_samples` (line 53)

환경변수로만 존재할 뿐 서비스 로직/runner에 주입되지 않음. 실제 MSA 서버 호출은 `msa_server_url`만 사용. **필드 제거 권장.**

### 4. MCP 도구 개수 표기 드리프트
- `src/boltz2_service/mcp/server.py:4` 모듈 docstring: **"13개 tool"**
- `README.md:44`: **"13 MCP Tools"**
- 실제 `@mcp.tool` 등록 수: **14** (`get_my_api_key` 추가 후 카운트 갱신 누락)

모듈 docstring은 `submit_nanobody_structure_prediction`과 `get_my_api_key`가 모두 빠진 옛 리스트를 담고 있음.

## 우선순위 중간

### 5. `default_max_concurrent_jobs` 중복 선언
- `platform_core/config.py:36` default=2
- `boltz2_service/config.py:52` default=5 (오버라이드)

의도적 override일 수 있으나 실제 사용처는 한 쪽(`api_key_auth.py`)뿐. 의도 없으면 공통 base에서만 정의하고 `boltz2_service` 쪽 제거.

### 6. 로컬 SQLite 파일 잔존
`bioai_platform.db` (106KB)가 레포 루트에 존재.
`.gitignore`에 `*.db`가 있어 **tracked는 아님**(`git ls-files` 비어있음)이지만, 로컬 디스크 잔존물. `rm bioai_platform.db` 해도 무해.

## 우선순위 낮음 (인프라/문서 드리프트)

### 7. 인프라 드리프트 — `aca_deploy.sh`로 재현 불가
실제 API Managed Identity 권한:
```
Reader      @ /subscriptions/.../resourceGroups/nanobody-designer-897d0b-rg
Contributor @ /subscriptions/.../resourceGroups/nanobody-designer-897d0b-rg
Contributor @ .../providers/Microsoft.App/jobs/boltz2-worker
```
그러나 `scripts/aca_deploy.sh:290-293`은 **worker job scope의 Contributor만** 할당. 나머지 RG-scope 권한은 out-of-band 상태. 스크립트에 포함시키거나, 필요 여부를 재검토해 제거.

### 8. 배포된 Worker Job 일부 필드가 null
`scripts/aca_deploy.sh:222-223`가 `--polling-interval` / `--replica-retry-limit`을 지정하지만, 실제 상태:
```
pollingInterval:        null   (기본 30s)
replicaCompletionCount: null   (기본 1)
replicaRetryLimit:      null   (기본 0)
```
`0` 값이 `null`로 저장됐을 가능성. 재배포 시 영향 없음을 확인할 필요. 이상하면 스크립트에서 해당 인자 제거해도 동일 결과.

### 9. Storage container `superimpose`
같은 스토리지 계정에 `superimpose` 컨테이너가 있으나 이 레포 어디에서도 참조하지 않음 — 별도 `protein-superimpose-mcp` 서비스 자산. CLAUDE.md에 "타 서비스용"으로 명시해 혼동 방지.

## 정리 권장 순서

1. **즉시**: 4번(도구 개수 표기) — 문서 숫자만 수정
2. **즉시**: 3번(미사용 settings 필드) — 삭제 시 영향 없음
3. **단기**: 1, 2번 — `list_workers` 삭제 or 구현, `get_logs`를 `AcaLogService`에 배선
4. **중기**: 7번 — 스크립트에 RG-scope 권한 추가 or 제거 결정
5. **선택**: 5, 6, 8, 9번 — 기능적 영향 없음
