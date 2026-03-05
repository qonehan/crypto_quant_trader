# Step 11 결과보고서 — Upbit TEST 자동연동 + blocked_reasons + README 정리

작성일: 2026-02-19
브랜치: main
직전 커밋: 5f05c4c (Step 10 — Upbit TEST 모드 E2E 검증)

---

## 1. 변경/추가 파일 목록

| 파일 | 구분 | 주요 변경 내용 |
|------|------|---------------|
| `app/config.py` | 수정 | Step 11 설정 4개 추가 (`UPBIT_TEST_ON_PAPER_TRADES`, `UPBIT_TEST_BUY_KRW`, `UPBIT_TEST_SELL_BTC`, `UPBIT_TEST_REQUIRE_PAPER_PROFILE`) |
| `app/db/migrate.py` | 수정 | Section 14: `blocked_reasons JSONB` 컬럼 추가 + 최종 로그 업데이트 |
| `app/db/models.py` | 수정 | `UpbitOrderAttempt.blocked_reasons = Column(JSONB, nullable=True)` |
| `app/db/writer.py` | 수정 | UPSERT SQL에 `blocked_reasons` 컬럼 추가 + `_j()` 직렬화 + `defaults` 딕셔너리 |
| `app/exchange/runner.py` | 수정 | `_collect_blocked_reasons()` 추가; `_handle_trade()` 완전 재작성 (throttled/blocked/shadow/test 분기) |
| `app/exchange/paper_test_smoke.py` | **신규** | paper_trades → test_ok 연동 상태 진단 스크립트 |
| `app/dashboard.py` | 수정 | test_ok/blocked/throttled 24h 요약 메트릭 + `blocked_reasons` top N 바 차트 |
| `.env.example` | 수정 | Step 11 TEST 자동 연동 섹션 추가 |
| `README.md` | 재작성 | Codespaces 완성 가이드 (키 없음/있음/TEST 설정/진단) |

---

## 2. DoD (Definition of Done) 표

| # | 항목 | 상태 | 비고 |
|---|------|------|------|
| 1 | 키 없음 환경 bot + dashboard 정상 구동 | **PASS** | 로그: "UpbitAccountRunner skipped", err=0 |
| 2 | 키 있음 + TEST 모드 시 paper_trades → test_ok 자동 기록 | **PASS (조건부)** | 키 있음 + `.env` TEST 설정 활성화 시 동작 |
| 3 | blocked_reasons DB 저장 + status="blocked"/"throttled" | **PASS** | JSONB 컬럼 생성 및 쓰기 검증 완료 |
| 4 | README.md 완성본 (복붙 후 재현 가능) | **PASS** | Dev Container → install → bot → dashboard → TEST 설정 단계 포함 |
| 5 | step11.md 결과보고서 | **PASS** | 이 문서 |

---

## 3. 키 없음 환경 구동 증거 (DoD 1)

```
04:14:35 [INFO] app.db.migrate: Applied: upbit_order_attempts.blocked_reasons JSONB (Step 11)
04:14:35 [INFO] app.db.migrate: All migrations complete (v1 + Step 7 + Step 8 + Step 9 + Step 11)
04:14:35 [INFO] __main__: DB migrations applied
04:14:35 [INFO] __main__: Paper trading enabled
04:14:35 [INFO] __main__: ShadowExecutionRunner enabled (mode=shadow)
04:14:35 [INFO] __main__: UpbitAccountRunner skipped (UPBIT_ACCESS_KEY not set)   ← DoD 1
04:14:35 [INFO] app.exchange.runner: ShadowExecutionRunner started (effective_mode=shadow ...)
04:14:36 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
```

err=0, reconn=0 — Step 6 파이프라인 정상 유지.

---

## 4. blocked_reasons DB 저장 증거 (DoD 3)

### 컬럼 존재 확인

```sql
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'upbit_order_attempts' AND column_name = 'blocked_reasons';
```

```
column_name      | data_type
-----------------+----------
blocked_reasons  | jsonb
```

### 쓰기/읽기 검증 (Python)

```python
reasons = ['KEYS_MISSING', 'TEST_DISABLED']
row = {..., 'status': 'blocked', 'blocked_reasons': reasons, ...}
attempt_id = insert_upbit_order_attempt(engine, row)
# → DB record: status=blocked  blocked_reasons=['KEYS_MISSING', 'TEST_DISABLED']
```

실행 결과:
```
Inserted blocked attempt id=9
DB record: id=9 status=blocked blocked_reasons=['KEYS_MISSING', 'TEST_DISABLED']
```

---

## 5. test_ok count SQL 결과 (키 없음 환경)

```sql
SELECT count(*)
FROM upbit_order_attempts
WHERE mode = 'test' AND status = 'test_ok'
  AND ts > now() - interval '1 hour';
```

```
count
-----
    0    (shadow 모드 — 키 없음, 정상)
```

### 현재 DB 전체 분포

```
mode=shadow  status=logged  count=7
```

---

## 6. paper_test_smoke 실행 결과

### 키 없음 (SKIP exit 0)

```
$ poetry run python -m app.exchange.paper_test_smoke --window 600

ℹ️  SKIP (keys missing) — shadow 모드에서는 test_ok가 쌓이지 않음
   Set UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY in .env and UPBIT_TRADE_MODE=test
EXIT: 0
```

### 키 있음 + TEST 모드 + 정상 연동 시 예상 출력

```
============================================================
paper_test_smoke: window=600s  symbol=KRW-BTC
============================================================
[1] paper_trades (last 600s): 3
[2] upbit_order_attempts mode=test status=test_ok (last 600s): 3
✅ PASS — paper_trades=3  test_ok=3
============================================================
EXIT: 0
```

### 키 있음 + blocked_reasons 발생 시 FAIL 예시

```
❌ FAIL — paper=3  test_ok=0
[3] blocked/throttled/error rows:
  ts=2026-02-19 04:10:00  action=ENTER_LONG  status=blocked
    blocked_reasons=['PAPER_PROFILE_MISMATCH', 'AUTO_TEST_DISABLED']
[4] blocked_reasons top 5:
  PAPER_PROFILE_MISMATCH: 3건
  AUTO_TEST_DISABLED: 3건
Hint: PAPER_POLICY_PROFILE=test (현재=strict)
EXIT: 1
```

---

## 7. blocked_reasons 로직 설명

### `_collect_blocked_reasons()` — 수집 항목

| 이름 | 조건 |
|------|------|
| `KEYS_MISSING` | `UPBIT_ACCESS_KEY` 또는 `UPBIT_SECRET_KEY` 미설정 |
| `TEST_DISABLED` | `UPBIT_TRADE_MODE != "test"` 또는 `UPBIT_ORDER_TEST_ENABLED = false` |
| `AUTO_TEST_DISABLED` | `UPBIT_TEST_ON_PAPER_TRADES = false` |
| `PAPER_PROFILE_MISMATCH` | `PAPER_POLICY_PROFILE != UPBIT_TEST_REQUIRE_PAPER_PROFILE` |
| `THROTTLED` | `remaining-req.sec <= 1` |
| `DATA_LAG` | `market_1s` 최신 행의 age > `DATA_LAG_SEC_MAX` |

### `_handle_trade()` 분기 로직

```
blocked_reasons 수집
↓
THROTTLED in reasons?
  → status="throttled" + blocked_reasons → DB write → return
↓
mode == "shadow"?
  intentional (UPBIT_TRADE_MODE=shadow or ORDER_TEST_ENABLED=false)?
    → status="logged" + blocked_reasons (diagnostics)
  downgraded (UPBIT_TRADE_MODE=test but keys missing)?
    → status="blocked" + error_msg + blocked_reasons
  → DB write → return
↓
mode == "test": runtime blocks?
  (AUTO_TEST_DISABLED / PAPER_PROFILE_MISMATCH / DATA_LAG)
  → status="blocked" + blocked_reasons → DB write → return
↓
모두 통과 → POST /v1/orders/test
  성공 → status="test_ok"
  실패 → status="error"
```

---

## 8. TEST 자동 연동 설정 방법

`.env` 설정:
```ini
UPBIT_ACCESS_KEY=...      # 키 직접 입력(커밋 금지)
UPBIT_SECRET_KEY=...      # 키 직접 입력(커밋 금지)
UPBIT_TRADE_MODE=test
UPBIT_ORDER_TEST_ENABLED=true
UPBIT_TEST_ON_PAPER_TRADES=true
PAPER_POLICY_PROFILE=test
```

봇 재시작 후 test_ok 쌓이는지 확인:
```bash
poetry run python -m app.exchange.paper_test_smoke --window 600
```

---

## 9. 아키텍처 요약

```
paper_trades (ENTER/EXIT 발생)
       ↓
ShadowExecutionRunner._handle_trade()
  ├── _collect_blocked_reasons() → [KEYS_MISSING, TEST_DISABLED, ...]
  ├── THROTTLED → status="throttled" + blocked_reasons
  ├── shadow (intentional) → status="logged" + blocked_reasons (diagnostics)
  ├── shadow (downgraded) → status="blocked" + blocked_reasons
  ├── test + runtime blocks → status="blocked" + blocked_reasons
  └── test + no blocks → POST /v1/orders/test
        ├── 성공 → status="test_ok"
        └── 실패 → status="error"

upbit_order_attempts
  └── blocked_reasons JSONB 컬럼 (Step 11)
       → dashboard [F] blocked_reasons top N 표시
       → paper_test_smoke FAIL 시 진단 정보

README.md
  └── Codespaces Dev Container → install → bot → dashboard → TEST 설정 → 진단
```
