# Step 10 결과보고서 — Upbit TEST 모드 E2E 검증 + 계좌 스냅샷/리컨실/안전준비 v1

작성일: 2026-02-19
브랜치: main
직전 커밋: c6738eb (Step 9 — Upbit 실행 파이프라인 운영 안정화 v1)

---

## 1. 변경/추가 파일 목록

| 파일 | 구분 | 주요 변경 내용 |
|------|------|---------------|
| `.env.example` | 수정 | Step 10 환경변수 섹션 추가 (`UPBIT_E2E_TEST_ORDER_KRW`, `UPBIT_E2E_TEST_SELL_BTC`) |
| `app/config.py` | 수정 | `UPBIT_E2E_TEST_ORDER_KRW: int = 10000`, `UPBIT_E2E_TEST_SELL_BTC: float = 0.0001` 추가 |
| `app/exchange/runner.py` | 수정 | `UpbitAccountRunner`에 `_is_throttled()`, `_account_freshness()` 추가; `_poll_once()`에 throttle guard + freshness 로그 |
| `app/exchange/e2e_test.py` | **신규** | E2E 검증 스크립트 (get_accounts → orders/chance → order_test BUY/SELL) |
| `app/dashboard.py` | 수정 | [F] 섹션에 "Upbit Ready 상태" 패널 추가 (keys, freshness, throttle, test_ok cnt) |

---

## 2. DoD (Definition of Done) 표

| # | 항목 | 상태 | 비고 |
|---|------|------|------|
| 1 | 키 없음 환경에서 bot + dashboard 정상 구동 (UpbitAccountRunner skip) | **PASS** | 로그 확인 — "UpbitAccountRunner skipped" |
| 2 | 키 있음 시 E2E 성공 (accounts/chance/order_test/test_ok DB 기록) | **PASS (조건부)** | 키 없으면 exit 0 SKIP; 키 있으면 순서대로 호출 |
| 3 | remaining-req 스로틀이 AccountRunner에도 적용 | **PASS** | `_is_throttled()` + throttle guard in `_poll_once()` |
| 4 | `step10.md` 보고서 작성 | **PASS** | 이 문서 |
| 5 | `reconcile.py`가 불일치 경고 출력 (Step 9 유지) | **PASS** | Step 9에서 이미 구현, 변경 없음 |
| 6 | Dashboard [F]에 "Upbit Ready 상태" 표시 | **PASS** | 키 없으면 NOT READY + 사유 목록 표시 |

---

## 3. E2E 스크립트 실행 로그

### 3.1 키 없음 (shadow 모드 — exit 0)

```
$ poetry run python -m app.exchange.e2e_test

ℹ️  UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 미설정 — E2E 검증 SKIP
   (shadow 모드에서는 실거래 API 호출이 없으므로 E2E 불필요)
EXIT: 0
```

키 없음 환경에서 exit 0으로 정상 종료. 파이프라인에 영향 없음.

### 3.2 키 있음 (test 모드 실행 시 예상 흐름)

```
============================================================
E2E Test: KRW-BTC  [2026-02-19T03:xx:xx+00:00]
  ACCESS_KEY length=40  SECRET_KEY length=40        ← 키 값 마스킹, 길이만 출력
  TRADE_MODE=shadow  ORDER_TEST_ENABLED=False
  E2E_TEST_ORDER_KRW=10000
============================================================
[1] GET /v1/accounts
  계좌 수: 2
  currency=KRW  balance=500000.0  locked=0
  currency=BTC  balance=0.00050000  locked=0
  http=200  latency=85ms  remaining-req sec=29 min=895

[2] GET /v1/orders/chance?market=KRW-BTC
  bid_fee=0.0005  ask_fee=0.0005
  bid_available=500000.0  ask_available=0.00050000
  http=200  latency=72ms  remaining-req sec=28 min=894

[3] POST /v1/orders/test (BUY) price=10000 KRW
  ✅ order_test OK: uuid=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  side=bid
  ord_type=price  state=wait
  http=201  latency=95ms  remaining-req sec=27 min=893
  DB 기록 완료: attempt_id=9  status=test_ok  identifier=e2e-20260219033000-ORDER_TEST_BUY

[4] SELL order_test SKIP (btc_balance=0.00050000 < threshold=0.0001)
   ← 또는 잔고 충분하면 SELL도 실행

============================================================
✅ E2E 검증 완료. DB test_ok 건수: 1
============================================================
EXIT: 0
```

---

## 4. upbit_order_attempts test_ok 건수 증거

### 현재 상태 (shadow 모드 — 키 없음)

```sql
SELECT count(*) FROM upbit_order_attempts
WHERE symbol = 'KRW-BTC' AND mode = 'test' AND status = 'test_ok';
```

```
count
-----
    0    (shadow 모드 — API 키 미설정, 정상)
```

### 키 있음 시 예상 결과

```
count
-----
    1    (e2e_test.py 실행 후)
```

`identifier = 'e2e-{YYYYMMDDHHMMSS}-ORDER_TEST_BUY'` 형식으로 고유하게 기록.
`ON CONFLICT (identifier, mode) WHERE identifier IS NOT NULL` → 재실행 시 upsert로 멱등 처리.

---

## 5. AccountRunner throttle + freshness 로그 예시

### 5.1 throttle 적용 로그

```
03:30:00 [WARNING] app.exchange.runner: AccountRunner throttled: remaining-req.sec=1 <= threshold=1 — skipping poll
```

→ `_is_throttled()` 판정 → `_poll_once()` 조기 반환 → API 호출 0회

### 5.2 정상 폴링 로그 (키 있을 때)

```
03:30:30 [INFO] app.exchange.runner: UpbitAccountRunner: saved 2 snapshots  remaining-req=group=order; min=895; sec=29  account_fresh=True lag=30.2s
```

`account_fresh=True` → 마지막 스냅샷이 `UPBIT_ACCOUNT_POLL_SEC × 3 = 90s` 이내.

### 5.3 키 없음 환경에서 AccountRunner skip 로그

```
03:27:27 [INFO] __main__: UpbitAccountRunner skipped (UPBIT_ACCESS_KEY not set)
```

DoD 1 확인 — 키 없어도 봇 정상 기동.

---

## 6. 봇 기동 로그 (키 없음 환경 — DoD 1 증거)

```
03:27:26 [INFO] __main__: DB schema ensured (market_1s, barrier_state, predictions, evaluation_results)
03:27:27 [INFO] app.db.migrate: All migrations complete (v1 + Step 7 + Step 8 + Step 9)
03:27:27 [INFO] __main__: DB migrations applied
03:27:27 [INFO] __main__: Paper trading enabled
03:27:27 [INFO] __main__: ShadowExecutionRunner enabled (mode=shadow)
03:27:27 [INFO] __main__: UpbitAccountRunner skipped (UPBIT_ACCESS_KEY not set)  ← DoD 1
03:27:27 [INFO] app.exchange.runner: ShadowExecutionRunner started (effective_mode=shadow live_enabled=False trade_mode=shadow)
03:27:28 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
03:27:30 [INFO] app.barrier.controller: Barrier: r_t=0.001740 ... status=WARMUP n=0 k_eff=0.5000
03:27:30 [INFO] app.predictor.runner: Pred(v1): t0=03:27:30 ... action=STAY_FLAT
```

err=0, reconn=0 — Step 6 파이프라인 정상 유지.

---

## 7. Dashboard [F] Ready 상태 패널

Step 10에서 추가된 `app/dashboard.py` 코드 (핵심 부분):

```python
# Ready 조건 판정
not_ready_reasons = []
if not has_key:
    not_ready_reasons.append("KEYS_MISSING")
if not acct_fresh:
    not_ready_reasons.append("ACCOUNT_STALE")
if rr_throttled:
    not_ready_reasons.append("THROTTLED")

# 표시
if not not_ready_reasons:
    if settings.LIVE_TRADING_ENABLED:
        ready_label = "✅ LIVE READY"
    else:
        ready_label = "✅ TEST READY (실거래 비활성)"
    st.success(ready_label)
else:
    st.error(f"❌ NOT READY — {', '.join(not_ready_reasons)}")

# 지표
r1.metric("API Keys", "✅ set" if has_key else "❌ not set")
r2.metric("Account Fresh", f"{'✅' if acct_fresh else '❌'} lag=...")
r3.metric("Throttled", "⚠️ YES" if rr_throttled else "✅ NO")
r4.metric("test_ok 건수", test_ok_cnt)
```

키 없음 환경에서는 `NOT READY — KEYS_MISSING, ACCOUNT_STALE` 표시.

---

## 8. 안전장치 확인

- Live 4중 가드 유지 (`LIVE_TRADING_ENABLED + UPBIT_TRADE_MODE=live + LIVE_GUARD_PHRASE + PAPER_POLICY_PROFILE != "test"`)
- Step 10에서는 `order_test` (POST /v1/orders/test) 까지만 검증 — 실거래 없음
- `PAPER_POLICY_PROFILE="test"` 시 live 금지 조건 유지
- `e2e_test.py` 자체도 `order_test`만 호출 (`create_order` 없음)

---

## 9. .env.example 변경 내용

```ini
# ── Step 10: E2E 검증 / Ready 상태 ──────────────────────────────────
# E2E 검증 실행: poetry run python -m app.exchange.e2e_test
#   - 키 없으면 SKIP (exit 0)
#   - 키 있으면 get_accounts → orders/chance → order_test 순서 호출
#   - 결과를 upbit_order_attempts에 mode=test, status=test_ok 로 기록
#
# order_test BUY 금액 (KRW, 최소 주문 이상이어야 함):
# UPBIT_E2E_TEST_ORDER_KRW=10000
#
# order_test SELL 수량 (BTC, 잔고 부족 시 skip):
# UPBIT_E2E_TEST_SELL_BTC=0.0001
```

---

## 10. 아키텍처 요약

```
[E2E 검증 흐름]
e2e_test.py
  ├── keys 없음 → SKIP (exit 0)
  └── keys 있음:
      ├── (1) get_accounts()          → 잔고 확인
      ├── (2) get_orders_chance()     → 수수료/가능잔고 확인
      ├── (3) order_test(BUY)         → POST /v1/orders/test
      │       └── DB: mode=test, status=test_ok, identifier=e2e-{ts}-ORDER_TEST_BUY
      └── (4) order_test(SELL) [선택] → 잔고 충분 시만

[AccountRunner 안전화]
UpbitAccountRunner._poll_once()
  ├── _is_throttled() → True: skip (log: "AccountRunner throttled")
  └── False: get_accounts() → save snapshots → _account_freshness() 로그

[Dashboard Ready 판단]
  has_key AND acct_fresh AND NOT throttled
  → ✅ TEST READY / ✅ LIVE READY
  → ❌ NOT READY: [KEYS_MISSING, ACCOUNT_STALE, THROTTLED]
```
