# Step 8 결과 보고서 — Upbit 주문 실행 파이프라인 v1

작성일: 2026-02-19
브랜치: main
구현자: Claude Code (claude-sonnet-4-6)

---

## 1. 변경/추가 파일 목록

| 파일 | 변경 유형 | 주요 내용 |
|------|-----------|-----------|
| `app/config.py` | 수정 | `LIVE_ORDER_POLL_INTERVAL_SEC=5`, `LIVE_ORDER_MAX_POLLS=24` 추가 |
| `app/db/migrate.py` | 수정 | Step 8 마이그레이션 3개 추가 (섹션 10~12) |
| `app/db/models.py` | 수정 | `UpbitOrderAttempt` 컬럼 확장, `UpbitOrderSnapshot`, `LivePosition` 모델 추가 |
| `app/db/writer.py` | 수정 | `_j()` JSONB 직렬화 헬퍼, `insert_upbit_order_attempt()` RETURNING id, `insert_upbit_order_snapshot()`, `update_upbit_order_attempt_final()`, `upsert_live_position()` 추가 |
| `app/exchange/upbit_rest.py` | 수정 | `UpbitApiError`, `_last_call_meta`, retry/backoff, `/v1/orders/test` 직접 호출, `identifier` 파라미터 지원 |
| `app/exchange/runner.py` | 수정 | Idempotency 체크, identifier, request_json, 4중 live 가드, live 폴링, UpbitAccountRunner backoff, `live_positions` 갱신 |
| `app/dashboard.py` | 수정 | `[F]` 섹션 확장 (모드/가드 상태, 계좌 요약, 50행 attempts, snapshots 섹션) |
| `app/exchange/smoke.py` | 수정 | `/v1/orders/test` 직접 호출, `remaining-req` 출력, `UpbitApiError` 처리 |
| `.env.example` | 수정 | Step 8 설정 항목 주석 추가 |
| `step8.md` | 신규 | 본 보고서 |

---

## 2. DoD 체크 결과

### 필수 PASS

| 번호 | 기준 | 결과 | 증거 |
|------|------|------|------|
| 1 | 키 없음: UpbitAccountRunner skip | ✅ PASS | `UpbitAccountRunner skipped (UPBIT_ACCESS_KEY not set)` |
| 1 | 키 없음: ShadowExecutionRunner shadow 동작 | ✅ PASS | `ShadowExecutionRunner started (effective_mode=shadow)` |
| 1 | 전체 파이프라인 크래시 없음 | ✅ PASS | 30초 이상 정상 구동 확인 |
| 2 | `upbit_order_attempts` 정상 기록 + 신규 컬럼 | ✅ PASS | `identifier`, `request_json` 포함 행 확인 (아래 참조) |
| 3 | Dashboard `/healthz` HTTP 200 | ✅ PASS | `curl -w "HTTP %{http_code}"` → `HTTP 200` |
| 3 | `[F]` 섹션 렌더 (attempts 표시) | ✅ PASS | upbit_order_attempts 쿼리 정상 실행 |
| 4 | `step8.md` 보고서 완료 | ✅ PASS | 본 문서 |

### 선택 PASS (키 없으므로 SKIP)

| 번호 | 기준 | 결과 | 비고 |
|------|------|------|------|
| 5 | test 모드 order_test → status="test_ok" | ⏭ SKIP | API 키 미설정 |
| 6 | live 모드 create_order + snapshots | ⏭ SKIP | API 키 미설정 + live 가드 기본 OFF |

---

## 3. 실행 로그 발췌

### Shadow 모드 동작 로그

```
02:30:38 [INFO] app.db.migrate: Applied: upbit_order_attempts Step 8 cols (11 columns)
02:30:38 [INFO] app.db.migrate: Applied: upbit_order_snapshots table (CREATE IF NOT EXISTS)
02:30:38 [INFO] app.db.migrate: Applied: live_positions table (CREATE IF NOT EXISTS)
02:30:38 [INFO] app.db.migrate: All migrations complete (v1 + Step 7 + Step 8)
02:30:38 [INFO] __main__: ShadowExecutionRunner enabled (mode=shadow)
02:30:38 [INFO] __main__: UpbitAccountRunner skipped (UPBIT_ACCESS_KEY not set)
02:30:38 [INFO] app.exchange.runner: ShadowExecutionRunner started (effective_mode=shadow live_enabled=False trade_mode=shadow)
02:30:38 [INFO] app.exchange.runner: ShadowExecutionRunner cursor init: last_id=16
02:30:59 [INFO] app.trading.runner: PaperTrade EXIT(EV_BAD): price=99203155 qty=0.00200866 fee=99.63 pnl=-431.79 hold=81s
02:31:03 [INFO] app.exchange.runner: Shadow [EXIT_LONG]: side=ask ord_type=market volume=0.002008... (no API call)
```

### Bot 정상 실행 확인 (키 없음 → shadow 모드)

```
02:23:54 [INFO] Boot OK
02:23:54 [INFO] DB OK
→ UpbitAccountRunner skipped (UPBIT_ACCESS_KEY not set)
→ ShadowExecutionRunner started (effective_mode=shadow)
```

---

## 4. DB 테이블 스키마 / 샘플 rows

### 4.1 upbit_order_attempts (Step 8 확장 후 전체 컬럼)

```sql
\d upbit_order_attempts
  id              BIGSERIAL PRIMARY KEY
  ts              TIMESTAMPTZ NOT NULL DEFAULT now()
  symbol          TEXT NOT NULL
  action          TEXT NOT NULL      -- ENTER_LONG | EXIT_LONG
  mode            TEXT NOT NULL      -- shadow | test | live
  side            TEXT NOT NULL      -- bid | ask
  ord_type        TEXT NOT NULL      -- price | market
  price           DOUBLE PRECISION
  volume          DOUBLE PRECISION
  paper_trade_id  BIGINT
  response_json   JSONB
  status          TEXT NOT NULL      -- logged | test_ok | submitted | error
  error_msg       TEXT
  -- Step 8 신규 컬럼
  uuid            TEXT               -- live 주문 uuid
  identifier      TEXT               -- paper-{id}-{action}
  request_json    JSONB              -- 요청 파라미터 (인증 제외)
  http_status     INTEGER
  latency_ms      INTEGER
  remaining_req   TEXT               -- remaining-req 헤더 raw
  retry_count     INTEGER DEFAULT 0
  final_state     TEXT               -- done | cancel
  executed_volume DOUBLE PRECISION
  paid_fee        DOUBLE PRECISION
  avg_price       DOUBLE PRECISION
```

**샘플 rows (최신 8건):**

```
id=8 | 2026-02-19 02:31:03 | EXIT_LONG  | shadow | logged | paper-17-EXIT_LONG
id=7 | 2026-02-19 02:29:42 | ENTER_LONG | shadow | logged | paper-16-ENTER_LONG
id=6 | 2026-02-19 02:23:41 | EXIT_LONG  | shadow | logged | paper-15-EXIT_LONG
id=5 | 2026-02-18 13:18:30 | EXIT_LONG  | shadow | logged | (Step7 이전 행)
id=4 | 2026-02-18 13:16:30 | ENTER_LONG | shadow | logged | (Step7 이전 행)
```

**request_json 샘플 (Step 8 신규, 인증정보 없음):**
```json
{
  "side": "ask",
  "market": "KRW-BTC",
  "volume": "0.002008657857101298",
  "ord_type": "market",
  "identifier": "paper-17-EXIT_LONG"
}
```

### 4.2 upbit_order_snapshots (신규)

```sql
CREATE TABLE upbit_order_snapshots (
    id               BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL,
    symbol           TEXT NOT NULL,
    uuid             TEXT NOT NULL,
    state            TEXT,
    side             TEXT,
    ord_type         TEXT,
    price            DOUBLE PRECISION,
    volume           DOUBLE PRECISION,
    remaining_volume DOUBLE PRECISION,
    executed_volume  DOUBLE PRECISION,
    paid_fee         DOUBLE PRECISION,
    raw_json         JSONB NOT NULL,
    UNIQUE (uuid, ts)
)
-- INDEX: (symbol, ts DESC), (uuid, ts DESC)
```
현재 데이터: 0건 (live 모드에서 실주문 시에만 생성)

### 4.3 live_positions (신규)

```sql
CREATE TABLE live_positions (
    symbol               TEXT PRIMARY KEY,
    ts                   TIMESTAMPTZ NOT NULL,
    krw_balance          DOUBLE PRECISION,
    btc_balance          DOUBLE PRECISION,
    btc_avg_buy_price    DOUBLE PRECISION,
    position_status      TEXT,     -- FLAT | LONG
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
)
```
현재 데이터: 0건 (API 키 설정 시 UpbitAccountRunner가 자동 갱신)

### 4.4 upbit_account_snapshots

현재 데이터: 0건 (API 키 미설정)

---

## 5. Dashboard 증거

### /healthz HTTP 200

```bash
$ curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8501/healthz
HTTP 200
```

### [F] Upbit Exchange 섹션 — DB 쿼리 정상 실행

대시보드 `[F]` 섹션은 다음 4개 서브섹션으로 구성:

1. **모드/가드 상태** — 5개 지표 + live guard 상태 표시
   - `LIVE_TRADING_ENABLED=False` / `UPBIT_TRADE_MODE=shadow` / `SHADOW_ENABLED=True`
   - `API Keys: ❌ not set` / `Live Guard: 🟢 SAFE (no live)`

2. **계좌 잔액** — `upbit_account_snapshots` + `live_positions` 쿼리
   - API 키 미설정 시 안내 메시지 표시

3. **주문 시도 로그** — `upbit_order_attempts` 최근 50건
   - Step 8 신규 컬럼(uuid, identifier, http_status, latency_ms, remaining_req, retry_count, final_state) 포함

4. **주문 상태 스냅샷** — `upbit_order_snapshots` 최근 50건
   - live 모드 전용, 현재 비어있음

---

## 6. Live 모드가 기본적으로 켜지지 않음 증명

### 4중 가드 조건 (모두 true여야 live 허용)

```python
# app/exchange/runner.py _determine_mode()
if (
    s.LIVE_TRADING_ENABLED          # False (기본값)
    and s.UPBIT_TRADE_MODE == "live" # "shadow" (기본값)
    and s.LIVE_GUARD_PHRASE == "I_CONFIRM_LIVE_TRADING"  # "" (기본값)
    and s.PAPER_POLICY_PROFILE != "test"  # 추가 안전: test 프로필에서 live 금지
):
    if s.UPBIT_ACCESS_KEY and s.UPBIT_SECRET_KEY:
        return "live"
```

### 현재 설정값

| 조건 | 기본값 | live 허용 여부 |
|------|--------|----------------|
| `LIVE_TRADING_ENABLED` | `False` | ❌ BLOCKED |
| `UPBIT_TRADE_MODE` | `"shadow"` | ❌ BLOCKED |
| `LIVE_GUARD_PHRASE` | `""` | ❌ BLOCKED |
| `PAPER_POLICY_PROFILE` | `"test"` | ❌ BLOCKED (test 프로필) |
| API 키 존재 | `False` | ❌ BLOCKED |

→ **기본 상태에서 live 주문은 절대 발생하지 않음**. 5가지 조건 중 단 하나라도 미충족이면 shadow로 downgrade.

---

## 7. 핵심 구현 요약

### Idempotency (중복 주문 방지)

```python
# identifier = f"paper-{paper_trade_id}-{action}"
# 동일 paper_trade_id + action 조합 존재 시:
#   - status in (submitted, done, test_ok, logged) → skip
#   - status = error and retry_count < MAX_RETRY   → retry 허용
#   - 그 외                                        → skip
```

### 모드별 동작

| 모드 | API 호출 | DB 기록 | 비고 |
|------|---------|---------|------|
| shadow | 없음 | `status="logged"` | **기본 모드** |
| test | `POST /v1/orders/test` | `status="test_ok"` or `"error"` | 키 필요, 실 체결 없음 |
| live | `POST /v1/orders` + 폴링 | `status="submitted"` → `final_state` | 4중 가드 + 키 필요 |

### UpbitRestClient Step 8 신기능

- `_last_call_meta`: `{http_status, remaining_req, latency_ms}` → 매 요청 후 갱신
- retry: exponential backoff (2^attempt + jitter) + max_retry=3
- `UpbitApiError`: `http_status`, `remaining_req` 포함
- `order_test()`: `POST /v1/orders/test` 직접 호출 (이전: orders/chance dry-run)
- `create_order()` / `order_test()`: `identifier` 파라미터 지원

---

## 8. Smoke Test 사용법

```bash
# 키 없음 → 종료코드 1
poetry run python -m app.exchange.smoke
# ❌ UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 미설정

# 키 있음 + UPBIT_ORDER_TEST_ENABLED=true → /v1/orders/test 호출
UPBIT_ACCESS_KEY=... UPBIT_SECRET_KEY=... UPBIT_ORDER_TEST_ENABLED=true \
  poetry run python -m app.exchange.smoke
```

---

## 9. 검증 커맨드

```bash
# bot 실행 (migration 자동 적용)
poetry run python -m app.bot

# Dashboard
poetry run streamlit run app/dashboard.py \
  --server.address 0.0.0.0 --server.port 8501 --server.headless true

# /healthz 확인
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8501/healthz
# → HTTP 200

# DB 확인 SQL
# upbit_order_attempts last 10
psql $DB_URL -c "SELECT id, ts, action, mode, status, identifier, http_status, latency_ms, retry_count FROM upbit_order_attempts ORDER BY ts DESC LIMIT 10;"

# upbit_order_snapshots last 10 (live 모드 전용)
psql $DB_URL -c "SELECT ts, uuid, state, executed_volume, paid_fee FROM upbit_order_snapshots ORDER BY ts DESC LIMIT 10;"

# upbit_account_snapshots last 5
psql $DB_URL -c "SELECT ts, currency, balance, locked, avg_buy_price FROM upbit_account_snapshots ORDER BY ts DESC LIMIT 5;"
```

---

## 10. 트러블슈팅 기록

### Issue 1: psycopg3 JSONB dict serialization error

**증상:** `cannot adapt type 'dict' using placeholder '%s' (format: AUTO)`
**원인:** psycopg3에서 `text()` 쿼리 사용 시 Python dict를 JSONB로 자동 변환하지 않음
**해결:** `writer.py`에 `_j()` 헬퍼 추가 → dict/list를 `json.dumps()`로 직렬화
```python
def _j(val):
    if val is None: return None
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return val
```

### Issue 2: insert_upbit_order_attempt RETURNING id 필요

**증상:** live 모드에서 attempt_id로 `upbit_order_snapshots` 연결 필요
**해결:** INSERT에 `RETURNING id` 추가, 함수 반환형을 `int | None`으로 변경

---

## 11. Step 6 실시간 파이프라인 유지 확인

Step 8 구현 후에도 Step 6 파이프라인(WS → market_1s → barrier → pred → evaluator → paper)은 정상 동작:

```
02:30:59 [INFO] app.trading.runner: PaperTrade EXIT(EV_BAD): price=99203155 qty=0.00200866 fee=99.63 pnl=-431.79
02:31:03 [INFO] app.exchange.runner: Shadow [EXIT_LONG]: side=ask ord_type=market volume=... (no API call)
```

→ paper_trades → ShadowExecutionRunner → upbit_order_attempts 전체 흐름 정상.
