# 테스트 전략 (Testing Strategy)

현재 이 레포는 **테스트가 하나도 없다**. CI는 `pytest || [ $? -eq 5 ]` 로
"no tests collected" (exit 5) 만 성공으로 취급하는 soft-fail 상태.

테스트 infra(`pytest`, `pytest-asyncio`)는 이미 `[project.optional-dependencies].dev`
에 들어있으므로 `pip install -e .[dev]` 한 뒤 `pytest tests/` 로 바로 실행 가능.

## 언제 테스트를 추가할 가치가 있는가

회귀 시 **보안 사고 · 데이터 손상 · 고객 영향**이 발생할 수 있는 영역부터.
커버리지 %를 채우는 게 목적이 아니다. 다음 기준에 해당하는 코드를 우선 타깃.

| 영역 | 이유 |
|------|------|
| `platform_core/auth/supabase_auth.py` | JWT ES256/HS256 검증, 만료, kid rotation — 보안 critical |
| `platform_core/auth/api_key_auth.py` | daily_job_limit, max_concurrent_jobs, 도메인 자동승인 — 과금/권한 |
| `platform_core/security.py` | API key hash/verify — 인증 근본 |
| `boltz2_service/services/jobs.py` | 상태 전이 (queued → running → succeeded/failed/canceled), client_request_id idempotency |
| `boltz2_service/services/spec_renderer.py` | YAML 템플릿 렌더링 — `{{ var }}` 치환, 잘못된 input 거부 |
| `boltz2_service/services/spec_validator.py` | subprocess 호출 결과 파싱 — mock으로 success/timeout/error 경로 |
| `boltz2_service/mcp/oauth_provider.py` | OAuth 2.1 + Supabase hash fragment 2-phase callback — 복잡하고 보안 |
| `platform_core/db.py` URL 재작성 | `postgresql://` → `postgresql+psycopg://` 자동 정규화 로직 |
| `boltz2_service/enums.py` 직렬화 | StrEnum 값이 API JSON에 그대로 노출되므로 변경 시 client breaking |

## 테스트를 굳이 안 써도 되는 영역

다음은 외부 서비스 mocking 비용 > 얻는 이득.
프로덕션 스모크(실제 job 제출/검증)로 충분히 회귀 감지 가능.

- `boltz2_service/worker/` GPU 경로 — `boltz predict` subprocess 자체의 정확성은 `jwohlwend/boltz` upstream 책임
- Azure Blob / Service Bus 실 호출 — `BLOB_BACKEND=local` / `QUEUE_BACKEND=local` 어댑터가 이미 통합 테스트 역할
- `platform_core/notifications.py` SMTP 발송 — dry-run 모드 이상의 테스트는 ROI 낮음

## 디렉터리 컨벤션

- `tests/` (레포 루트) 하위에 `src/` 레이아웃을 **미러링**:
  ```
  tests/
    platform_core/
      auth/
        test_supabase_auth.py
        test_api_key_auth.py
      test_security.py
      test_db.py
    boltz2_service/
      services/
        test_jobs.py
        test_spec_renderer.py
      mcp/
        test_oauth_provider.py
  ```
- 통합 테스트는 `tests/integration/` 로 분리 (실 DB 필요, 평상시 skip, `-m integration` 으로 opt-in)

## 실행

```bash
pytest                             # 전체
pytest tests/platform_core/        # 서브트리
pytest tests/path/to/test_x.py::test_y  # 단일
pytest -x                          # 첫 실패에서 중단
pytest -m integration              # integration 마커만
```

## 테스트 추가 시 CI 정책 갱신

`.github/workflows/ci.yml` 의 soft-fail (`|| [ $? -eq 5 ]`) 를 제거하고
pytest 실패를 그대로 CI fail 로 취급.
테스트 1개라도 있으면 exit 5 는 더 이상 발생하지 않음.
