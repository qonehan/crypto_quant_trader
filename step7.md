# STEP 7 결과 보고서 — Upbit Exchange REST 연동 (Shadow 기본)

**완료일시:** 2026-02-18 13:10 UTC
**담당:** Claude Sonnet 4.6

---

## DoD 달성 여부

| DoD 항목 | 결과 |
|---|---|
| `UPBIT_ACCESS_KEY` 미설정 시 shadow 모드로 bot 정상 동작 | ✅ |
| `UpbitAccountRunner`: API key 없으면 SKIP, 있으면 polling | ✅ |
| `ShadowExecutionRunner`: paper_trades 감지 → upbit_order_attempts 로깅 | ✅ |
| `upbit_account_snapshots` 테이블 생성 (idempotent migration) | ✅ |
| `upbit_order_attempts` 테이블 생성 (idempotent migration) | ✅ |
| 3중 안전장치 구현 (LIVE_TRADING_ENABLED + UPBIT_TRADE_MODE + LIVE_GUARD_PHRASE) | ✅ |
| Dashboard [F] Upbit 섹션 추가 | ✅ |
| smoke test 스크립트 (`app.exchange.smoke`) 작성 | ✅ |
| step7.md 결과 보고서 작성 | ✅ |

**OVERALL: PASS ✅**

---

## 작업 내용

### 신규 파일

| 파일 | 내용 |
|---|---|
| `app/exchange/__init__.py` | exchange 패키지 초기화 |
| `app/exchange/upbit_auth.py` | JWT HS256 + SHA512 query_hash 인증 헬퍼 |
| `app/exchange/upbit_rest.py` | Upbit v1 REST 클라이언트 (get_accounts, get_orders_chance, order_test, create_order, get_order, list_open_orders) |
| `app/exchange/runner.py` | UpbitAccountRunner + ShadowExecutionRunner |
| `app/exchange/smoke.py` | API key 설정 확인 + 계좌/주문가능정보 점검 스크립트 |
| `step7.md` | 이 파일 |

### 수정 파일

| 파일 | 변경 내용 |
|---|---|
| `app/config.py` | Upbit REST 설정 11개 추가 (ACCESS_KEY, SECRET_KEY, API_BASE, POLL_SEC, TIMEOUT, MAX_RETRY, SHADOW_ENABLED, ORDER_TEST_ENABLED, LIVE_TRADING_ENABLED, TRADE_MODE, LIVE_GUARD_PHRASE) |
| `.env.example` | 신규 Upbit REST 환경변수 예시 추가 (주석 처리) |
| `app/db/models.py` | `UpbitAccountSnapshot`, `UpbitOrderAttempt` 모델 추가 |
| `app/db/migrate.py` | upbit_account_snapshots + upbit_order_attempts CREATE TABLE IF NOT EXISTS 추가 |
| `app/db/writer.py` | `insert_upbit_account_snapshot`, `insert_upbit_order_attempt` 함수 추가 |
| `app/bot.py` | `ShadowExecutionRunner` 무조건 등록, `UpbitAccountRunner` 키 있을 때 등록 |
| `app/dashboard.py` | `[F] Upbit Exchange` 섹션 추가 (계좌 잔액 + 주문 시도 로그) |
| `pyproject.toml` | `httpx ^0.28.1`, `pyjwt ^2.11.0` 의존성 추가 |

---

## 실행 로그 핵심 라인

```
13:04:25 [INFO] app.db.migrate: Applied: upbit_account_snapshots table (CREATE IF NOT EXISTS)
13:04:25 [INFO] app.db.migrate: Applied: upbit_order_attempts table (CREATE IF NOT EXISTS)
13:04:25 [INFO] app.db.migrate: All migrations complete (v1 + Step 7)
13:04:25 [INFO] __main__: Paper trading enabled
13:04:25 [INFO] __main__: ShadowExecutionRunner enabled (mode=shadow)
13:04:25 [INFO] __main__: UpbitAccountRunner skipped (UPBIT_ACCESS_KEY not set)
13:04:25 [INFO] app.exchange.runner: ShadowExecutionRunner started (mode=shadow live=False)
13:04:25 [INFO] app.exchange.runner: ShadowExecutionRunner cursor init: last_id=8
```

---

## DB 스키마 (신규 2개)

### upbit_account_snapshots

| 컬럼 | 타입 | 설명 |
|---|---|---|
| id | BIGSERIAL PK | 자동 증가 |
| ts | TIMESTAMPTZ | 스냅샷 시각 |
| symbol | TEXT | 심볼 (예: KRW-BTC) |
| currency | TEXT | 통화 (예: KRW, BTC) |
| balance | DOUBLE PRECISION | 사용 가능 잔액 |
| locked | DOUBLE PRECISION | 거래 중 잠금 잔액 |
| avg_buy_price | DOUBLE PRECISION | 평균 매수가 |
| avg_buy_price_modified | BOOLEAN | 평단가 수정 여부 |
| unit_currency | TEXT | 기준 화폐 |
| raw_json | JSONB | Upbit API 원본 응답 |

### upbit_order_attempts

| 컬럼 | 타입 | 설명 |
|---|---|---|
| id | BIGSERIAL PK | 자동 증가 |
| ts | TIMESTAMPTZ | 시도 시각 |
| symbol | TEXT | 심볼 |
| action | TEXT | ENTER_LONG \| EXIT_LONG |
| mode | TEXT | shadow \| test \| live |
| side | TEXT | bid \| ask |
| ord_type | TEXT | market \| limit |
| price | DOUBLE PRECISION | 주문가 |
| volume | DOUBLE PRECISION | 주문량 |
| paper_trade_id | BIGINT | 연결된 paper_trades.id |
| response_json | JSONB | API 응답 (mode=test/live) |
| status | TEXT | logged \| test_ok \| submitted \| error |
| error_msg | TEXT | 오류 메시지 |

---

## 3중 안전장치

```python
# _determine_mode() in ShadowExecutionRunner
if (
    s.LIVE_TRADING_ENABLED           # 1️⃣ 명시적 활성화
    and s.UPBIT_TRADE_MODE == "live" # 2️⃣ 모드 지정
    and s.LIVE_GUARD_PHRASE == "I_CONFIRM_LIVE_TRADING"  # 3️⃣ 확인 문구
):
    return "live"
if s.UPBIT_ORDER_TEST_ENABLED:
    return "test"
return "shadow"  # 기본값: shadow (API 호출 없음)
```

기본값 (.env 미설정 시):
- `LIVE_TRADING_ENABLED=false`
- `UPBIT_TRADE_MODE=shadow`
- `LIVE_GUARD_PHRASE=""`

→ 3중 조건 모두 충족되어야 실거래. 기본은 shadow로 안전.

---

## Shadow 실행 검증

수동 테스트 결과:

```
paper_trades 신규 ENTER_LONG (id=9) 삽입
→ ShadowExecutionRunner._process_new_trades() 호출
→ upbit_order_attempts 행 생성:
    action=ENTER_LONG  mode=shadow  side=bid  status=logged  err=None
→ PASS ✅
```

---

## Smoke Test 결과

```bash
poetry run python -m app.exchange.smoke
```

```
❌ UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 미설정
   .env 파일에 키를 설정하거나, Shadow 모드로 bot을 실행하세요.
   (UPBIT_SHADOW_ENABLED=true 이면 API key 없이도 bot은 동작 가능)
```

API key 미설정 → 정상 종료 (exit=1). API key 설정 시 계좌조회 + 주문가능정보 조회 수행.

---

## 트러블슈팅

**발생 이슈:** `response_json::jsonb` cast를 SQLAlchemy `text()` 안에서 사용 시
`psycopg.errors.SyntaxError: syntax error at or near ":"` 발생

**원인:** SQLAlchemy 네임드 파라미터 (`:name`) 문법과 PostgreSQL 타입 캐스트 (`::jsonb`) 충돌.
psycopg3가 `$N` 위치 바인딩으로 변환하는 과정에서 `::` 앞의 `:` 를 파라미터로 해석.

**해결:** `::jsonb` 제거 → `:response_json` 만 사용.
SQLAlchemy + psycopg3 조합에서 Python `dict` → JSONB 자동 변환 지원.

---

## 다음 단계

- Step 8로 진행
- Upbit API key 취득 시 smoke test 재실행하여 계좌 조회 확인
- 운영 전 `PAPER_POLICY_PROFILE=strict` 복귀 권장
