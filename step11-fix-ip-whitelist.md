# Step 11 IP 화이트리스트 조치 보고서

작성일시: 2026-02-19
환경: Codespaces (Azure, linux)

---

## 최종 판정

| 항목 | 결과 |
|------|------|
| IP 화이트리스트 등록 | ✅ 완료 — 401 no_authorization_ip 소멸 |
| GET /v1/accounts | ✅ HTTP 200 |
| GET /v1/orders/chance | ✅ HTTP 200 |
| POST /v1/orders/test | ❌ HTTP 400 bank_account_required |
| test_ok_1h | 0 |
| Step 11 PASS 기준 | ❌ FAIL |

**현재 차단 원인**: `bank_account_required` — 해당 API 키의 Upbit 계좌에 실명확인 입출금 계좌가 미등록됨.

---

## 1) 외부 IP 확인

```
curl -s https://api.ipify.org  →  23.97.62.134
curl -s ifconfig.me            →  23.97.62.134
```

- 등록한 허용 IP: **23.97.62.134**

---

## 2) Upbit 허용 IP 등록 결과

- 등록 전: `HTTP 401 no_authorization_ip`
- 등록 후: `HTTP 400 bank_account_required` (401 완전 소멸)
- **IP 화이트리스트 조치: ✅ 성공**

---

## 3) e2e_test 결과 (IP 등록 후)

```
[1] GET /v1/accounts
  계좌 수: 0
  http=200  latency=916ms  remaining-req sec=29 min=1800   ← ✅

[2] GET /v1/orders/chance?market=KRW-BTC
  bid_fee=0.0005  ask_fee=0.0005
  bid_available=0  ask_available=0
  http=200  latency=260ms  remaining-req sec=28 min=1800   ← ✅

[3] POST /v1/orders/test (BUY) price=10000 KRW
  ❌ HTTP 400: 실명확인 입출금 계좌 등록 후 이용가능합니다. (bank_account_required)
```

---

## 4) Bot 실행 — paper→test 자동연동 로그

```
[INFO] ShadowExecutionRunner started (effective_mode=test live_enabled=False trade_mode=test)

[INFO] app.trading.runner: PaperTrade ENTER: price=99222841 qty=0.00200850 ...
[INFO] app.exchange.runner: Test [ENTER_LONG]: POST /v1/orders/test side=bid ord_type=price price=10000.0
[INFO] httpx: HTTP Request: POST https://api.upbit.com/v1/orders/test "HTTP/1.1 400 Bad Request"
[ERROR] ShadowExecutionRunner API error [ENTER_LONG]: HTTP 400: bank_account_required

[INFO] app.trading.runner: PaperTrade EXIT(TIME): price=99144167 qty=0.00200850 ...
[INFO] app.exchange.runner: Test [EXIT_LONG]: POST /v1/orders/test side=ask ord_type=market ...
[INFO] httpx: HTTP Request: POST https://api.upbit.com/v1/orders/test "HTTP/1.1 400 Bad Request"
[ERROR] ShadowExecutionRunner API error [EXIT_LONG]: HTTP 400: bank_account_required
```

**중요**: 401 no_authorization_ip가 완전히 사라졌고, `/v1/orders/test` 호출이 정상적으로 Upbit 서버까지 도달함. Step 11 연동 자체는 완전히 정상 동작.

---

## 5) DB 최근 attempts (1h)

| ts | action | mode | status | http | error |
|----|--------|------|--------|------|-------|
| 2026-02-19 06:42:12 | EXIT_LONG | test | error | **400** | bank_account_required |
| 2026-02-19 06:40:11 | ENTER_LONG | test | error | **400** | bank_account_required |
| 2026-02-19 06:39:17 | E2E_ORDER_TEST_BUY | test | error | **400** | bank_account_required |
| 2026-02-19 06:08:57 | EXIT_LONG | test | error | ~~401~~ | ~~no_authorization_ip~~ |
| 2026-02-19 06:06:56 | ENTER_LONG | test | error | ~~401~~ | ~~no_authorization_ip~~ |

- `blocked_reasons = NULL` (사전 차단 없음 — 모든 조건 통과 후 API 호출)
- test_ok_1h = **0**

---

## 6) 잔여 조치 사항

현재 API 키로는 `bank_account_required` 오류가 발생함.
Upbit `/v1/orders/test` 엔드포인트는 실제 주문과 동일한 계좌 요건을 요구함.

### 필요 조치

**옵션 A (권장)**: 실명확인 입출금 계좌 등록
- Upbit 앱/웹 → 입출금 → 계좌 인증 완료
- 완료 후 즉시 `poetry run python -m app.exchange.e2e_test` 재실행

**옵션 B**: 계좌 인증이 완료된 별도 API 키 사용
- `.env`의 `UPBIT_ACCESS_KEY` / `UPBIT_SECRET_KEY` 교체

---

## 7) 코드/설정 상태

```
UPBIT_TRADE_MODE = test        ✅
UPBIT_ORDER_TEST_ENABLED = True  ✅
UPBIT_TEST_ON_PAPER_TRADES = True ✅
PAPER_POLICY_PROFILE = test    ✅
UPBIT_TEST_REQUIRE_PAPER_PROFILE = test ✅
LIVE_TRADING_ENABLED = False   ✅
ACCESS_KEY len = 40            ✅
SECRET_KEY len = 40            ✅
blocked_reasons (last 1h) = NULL ✅
```

Step 11 코드 구현: **완전 정상**. 문제는 Upbit 계좌 설정(실명확인)뿐.

---

## 최종 PASS 조건 달성 경로

1. Upbit 계좌 실명확인 입출금 등록 완료
2. `poetry run python -m app.exchange.e2e_test` → POST /v1/orders/test HTTP 201 확인
3. `poetry run python -m app.bot` 실행 (paper_trade 1회 이상 발생 대기)
4. `poetry run python -m app.exchange.paper_test_smoke --window 600` → EXIT_CODE=0 확인
5. test_ok_1h ≥ 1 → **PASS**
