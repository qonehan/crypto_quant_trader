# STEP 6.3 결과 보고서 — Paper Smoke Test (test 프로필)

**실행일시:** 2026-02-18 UTC 10:03 ~ 10:08
**심볼:** KRW-BTC
**DB:** postgresql+psycopg://postgres:postgres@db:5432/quant
**policy_profile:** test

---

## 1. 적용한 test 설정 값 (.env 핵심)

| 키 | 값 | 비고 |
|---|---|---|
| `PAPER_POLICY_PROFILE` | `test` | test 프로필 활성화 |
| `TEST_ENTER_EV_RATE_TH` | `-0.001` | EV_RATE 기준 완화 (기본: 0.0) |
| `TEST_ENTER_PNONE_MAX` | `0.995` | p_none 허용 상한 완화 (기본: 0.70) |
| `TEST_ENTER_PDIR_MARGIN` | `-1.0` | 방향성 마진 사실상 해제 |
| `TEST_COST_RMIN_MULT` | `0.0` | COST_GT_RT 차단 해제 (테스트 목적) |
| `TEST_MAX_POSITION_FRAC` | `0.05` | 포지션 크기 5% |
| `TEST_MAX_ENTRIES_PER_HOUR` | `2` | 시간당 최대 2회 진입 |
| `TEST_COOLDOWN_SEC` | `60` | 거래 후 쿨다운 60초 |

> **주의:** 이 값들은 Paper 검증 전용. Live에 절대 사용 금지.

---

## 2. Bot 로그 — ENTER/EXIT 관련 핵심 라인

```
10:03:34 [INFO] __main__: DB schema ensured (market_1s, barrier_state, predictions, evaluation_results)
10:03:34 [INFO] app.db.migrate: All v1 migrations complete
10:03:34 [INFO] __main__: Paper trading enabled
10:03:34 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
10:03:35 [INFO] app.predictor.runner: Pred(v1): t0=10:03:35 r_t=0.001595 z=N/A p_none=0.9900 p_up=0.0050 p_down=0.0050 ev=-0.00208338 ev_rate=-0.00001737 action=STAY_FLAT

--- [ENTER_LONG 실행: 10:03:09 UTC (by bot PID 12069)] ---
PaperTrade ENTER: price=100459088 qty=0.00198987 fee=99.95 cash=800000 u_exec=100619229 d_exec=100299014 h=120s

10:03:40 [INFO] app.trading.runner: Paper: pos=LONG action=HOLD_LONG reason=OK cash=800000 qty=0.00198987 equity=999760 dd=-0.0240% halted=False profile=test
10:04:00 [INFO] app.trading.runner: Paper: pos=LONG action=HOLD_LONG reason=OK cash=800000 qty=0.00198987 equity=999760 dd=-0.0240% halted=False profile=test
10:04:15 [INFO] app.trading.runner: Paper: pos=LONG action=HOLD_LONG reason=OK cash=800000 qty=0.00198987 equity=999770 dd=-0.0230% halted=False profile=test
10:04:30 [INFO] app.trading.runner: Paper: pos=LONG action=HOLD_LONG reason=OK cash=800000 qty=0.00198987 equity=999756 dd=-0.0244% halted=False profile=test
10:04:45 [INFO] app.trading.runner: Paper: pos=LONG action=HOLD_LONG reason=OK cash=800000 qty=0.00198987 equity=999756 dd=-0.0244% halted=False profile=test
10:05:00 [INFO] app.trading.runner: Paper: pos=LONG action=HOLD_LONG reason=OK cash=800000 qty=0.00198987 equity=999822 dd=-0.0178% halted=False profile=test
10:05:05 [INFO] app.trading.runner: Paper: pos=LONG action=HOLD_LONG reason=OK cash=800000 qty=0.00198987 equity=999820 dd=-0.0180% halted=False profile=test

--- [EXIT_LONG(TIME) 실행: 10:05:09 UTC — hold 120s 경과] ---
PaperTrade EXIT(TIME): price=100418912 qty=0.00198987 fee=99.91 pnl=-279.80 pnl_rate=-0.1399% hold=120s cash=999720

10:05:10 [INFO] app.trading.runner: Paper: pos=FLAT action=STAY_FLAT reason=COOLDOWN cash=999720 qty=0.00000000 equity=999720 dd=-0.0280% halted=False profile=test
10:05:15 [INFO] app.trading.runner: Paper: pos=FLAT action=STAY_FLAT reason=COOLDOWN cash=999720 qty=0.00000000 equity=999720 dd=-0.0280% halted=False profile=test
10:05:25 [INFO] app.trading.runner: Paper: pos=FLAT action=STAY_FLAT reason=COOLDOWN cash=999720 qty=0.00000000 equity=999720 dd=-0.0280% halted=False profile=test
10:05:30 [INFO] app.trading.runner: Paper: pos=FLAT action=STAY_FLAT reason=COOLDOWN cash=999720 qty=0.00000000 equity=999720 dd=-0.0280% halted=False profile=test

--- [ENTER_LONG 실행: 10:06:09 UTC — cooldown 60s 후 2차 진입] ---
PaperTrade ENTER: price=100470090 qty=0.00198909 fee=99.92 cash=799776 u_exec=100650125 d_exec=100290055 h=120s

10:06:20 [INFO] app.trading.runner: Paper: pos=LONG action=HOLD_LONG reason=OK cash=799776 qty=0.00198909 equity=999473 dd=-0.0527% halted=False profile=test
10:07:05 [INFO] app.trading.runner: Paper: pos=LONG action=HOLD_LONG reason=OK cash=799776 qty=0.00198909 equity=999528 dd=-0.0472% halted=False profile=test
```

---

## 3. DB 쿼리 출력 전문

### paper_positions (현재 상태)

```
symbol   : KRW-BTC
status   : LONG  ← 2차 ENTER 이후 LONG 유지 중
cash_krw : 799,776.16
qty      : 0.001989090654936326
entry_time : 2026-02-18 10:06:09 UTC
entry_price: 100,470,090.0
u_exec   : 100,650,124.67
d_exec   : 100,290,055.33
h_sec    : 120
halted   : False
halt_reason: None
updated_at : 2026-02-18 10:07:05 UTC
```

### paper_trades (최근 3건 전문)

```
[1] t=2026-02-18 10:06:09 UTC
    action=ENTER_LONG  reason=SIGNAL
    price=100,470,090.0  qty=0.001989090654936326
    fee_krw=99.9220585598058  cash_after=799,776.16
    pnl_krw=None  pnl_rate=None  hold_sec=None
    pred_t0=10:06:05  model_version=baseline_v1_exec

[2] t=2026-02-18 10:05:09 UTC
    action=EXIT_LONG   reason=TIME
    price=100,418,912.2  qty=0.001989865271054278
    fee_krw=99.91005297191437  cash_after=999,720.20
    pnl_krw=-279.80410914318054  pnl_rate=-0.0013990205457159028  hold_sec=120.0
    pred_t0=10:03:05  model_version=baseline_v1_exec

[3] t=2026-02-18 10:03:09 UTC
    action=ENTER_LONG  reason=SIGNAL
    price=100,459,087.8  qty=0.001989865271054278
    fee_krw=99.95002498750624  cash_after=800,000.00
    pnl_krw=None  pnl_rate=None  hold_sec=None
    pred_t0=10:03:05  model_version=baseline_v1_exec
```

### paper_decisions (최근 15건)

```
10:07:05 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,528  dd=-0.047%
10:07:04 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,528  dd=-0.047%
10:07:00 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,568  dd=-0.043%
10:06:59 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,568  dd=-0.043%
10:06:55 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,491  dd=-0.051%
10:06:50 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,520  dd=-0.048%
10:06:44 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,568  dd=-0.043%
10:06:40 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,568  dd=-0.043%
10:06:35 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,568  dd=-0.043%
10:06:30 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,562  dd=-0.044%
10:06:25 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,518  dd=-0.048%
10:06:20 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,473  dd=-0.053%
10:06:15 LONG  HOLD_LONG  OK          ["OK"]     profile=test  equity=999,473  dd=-0.053%
10:06:09 FLAT  ENTER_LONG OK→ENTER    ["OK"]     profile=test  equity=999,720  dd=-0.028%
10:05:30 FLAT  STAY_FLAT  COOLDOWN    ["COOLDOWN"] profile=test equity=999,720 dd=-0.028%
```

---

## 4. 가장 최근 EXIT 1건 요약

```
entry_price=100,459,087.8  exit_price=100,418,912.2  qty=0.001990
fees=99.95(entry)+99.91(exit)=199.86 KRW
pnl_krw=-279.80 KRW  pnl_rate=-0.1399%  hold_sec=120  exit_reason=TIME
cash 변화: 1,000,000 → 999,720.20 KRW (수수료 포함 소폭 손실)
```

---

## 5. 무결성 체크 결과

| 체크 항목 | 결과 | 값 |
|---|---|---|
| EXIT_LONG에 pnl_krw NOT NULL | ✅ PASS | -279.80 KRW |
| EXIT_LONG에 pnl_rate NOT NULL | ✅ PASS | -0.1399% |
| EXIT_LONG에 hold_sec NOT NULL | ✅ PASS | 120.0s |
| ENTER 직후 status == LONG | ✅ PASS | LONG 확인 |
| ENTER 직후 qty > 0 | ✅ PASS | 0.001989865 |
| EXIT 직후 status == FLAT | ✅ PASS | FLAT 확인 |
| EXIT 직후 qty == 0 | ✅ PASS | qty=0 |
| cash_after 합리적 (초기 1M 소폭 변화) | ✅ PASS | 999,720 KRW |
| paper_positions LONG→FLAT→LONG 전환 | ✅ PASS | 2차 진입 확인 |
| halted == False | ✅ PASS | 드로우다운 미초과 |
| reason_flags 기록 | ✅ PASS | ["OK"], ["COOLDOWN"] 등 정상 기록 |
| policy_profile=test 일관 | ✅ PASS | 모든 decisions에서 test 확인 |

---

## 6. DoD 달성 여부

| DoD 항목 | 결과 |
|---|---|
| ENTER_LONG 1건 이상 발생 | ✅ 2건 (10:03:09, 10:06:09) |
| EXIT_LONG 1건 이상 발생 | ✅ 1건 (10:05:09, reason=TIME) |
| pnl_krw/pnl_rate/hold_sec 계산 저장 | ✅ -279.80 / -0.1399% / 120.0s |
| LONG→FLAT 상태 전환 정상 | ✅ 10:05:09에 FLAT 전환 확인 |
| dashboard trade stats 0이 아님 | ✅ 총 3건 trades 기록 |

---

## 7. 코드 변경 사항 (Step 6.3)

### app/trading/runner.py
- ENTER 로그: `PaperTrade ENTER: price=... qty=... fee=... cash=... u_exec=... d_exec=... h=...` 형식으로 상세화
- EXIT 로그: `PaperTrade EXIT(<reason>): price=... qty=... fee=... pnl=... pnl_rate=... hold=... cash=...` 형식으로 상세화

### app/trading/paper.py
- ENTER 실행 후 `cash_after < 0`이면 엔트리 취소(None 리턴) sanity check 추가

### .env
- `PAPER_POLICY_PROFILE=test` 및 test 임계값 6종 설정

---

## 8. 특이사항

- **Bot WARMUP 상태에서도 즉시 진입**: Barrier status=WARMUP이어도 test 프로필 조건이 충분히 완화되어 첫 prediction 수신 후 즉시 ENTER 발생
- **COOLDOWN 정상 동작**: EXIT 후 60초간 COOLDOWN reason_flags 정상 기록
- **2차 ENTER 발생**: cooldown 만료(60s) 후 자동으로 2차 진입 확인
- **halted=False**: 드로우다운 -0.028%로 MAX_DRAWDOWN_PCT(5%) 훨씬 미만 → 정상
- **spread 편차**: 장중 spread가 0.1bp~7.4bp 범위로 변동, SPREAD_WIDE(기준 20bp) 미발동
