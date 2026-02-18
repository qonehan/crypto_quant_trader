# Step 6-4 실시간 데이터 파이프라인 점검 보고서

**점검일시:** 2026-02-18 12:20 UTC
**심볼:** KRW-BTC
**환경:** GitHub Codespaces / Dev Container (Python 3.11, Poetry 2.3.2)
**점검 도구:** `app/diagnostics/realtime_check.py` (신규 추가)
**점검 윈도우:** 300초 (5분)

---

## 1. 점검 개요

Upbit WebSocket → market_1s 적재 → barrier_state / predictions / paper_decisions 생성 → exec_v1 evaluator 정산 → Streamlit 대시보드 렌더링까지 전 파이프라인이 끊기지 않고 실시간으로 동작하는지 DB 기반 수치로 검증하였다.

---

## 2. 진단 스크립트 실행 결과 (전문)

```
============================================================
  실시간 데이터 파이프라인 점검
  now_utc  = 2026-02-18T12:20:06.493361+00:00
  symbol   = KRW-BTC
  window   = 300s  interval=5s  h_sec=120s
============================================================

[market_1s]
  max(ts)   = 2026-02-18 12:20:06+00:00
  lag_sec   = 0.5s ✅
  count(window=300s) = 300 / expected=300
  fill_rate = 100.0% ✅
  last 3 rows:
    ts=2026-02-18 12:20:06+00:00, bid_close_1s=100047000.0, ask_close_1s=100098000.0, spread_bps=5.10, imb_notional_top5=-0.272
    ts=2026-02-18 12:20:05+00:00, bid_close_1s=100047000.0, ask_close_1s=100097000.0, spread_bps=5.00, imb_notional_top5=-0.256
    ts=2026-02-18 12:20:04+00:00, bid_close_1s=100047000.0, ask_close_1s=100098000.0, spread_bps=5.10, imb_notional_top5=-0.350
  → PASS ✅

[barrier_state]
  max(ts)   = 2026-02-18 12:20:05+00:00
  lag_sec   = 1.5s ✅
  count(window=300s) = 60 / expected≈60
  fill_rate = 100.0% ✅
  last 3 rows:
    ts=12:20:05, r_t=0.002089, r_min_eff=0.002089, cost=0.001900, status=OK, k_vol_eff=0.5, none_ewma=0.9617
    ts=12:20:00, r_t=0.002090, r_min_eff=0.002090, cost=0.001900, status=OK, k_vol_eff=0.5, none_ewma=0.9610
    ts=12:19:55, r_t=0.002090, r_min_eff=0.002090, cost=0.001900, status=OK, k_vol_eff=0.5, none_ewma=0.9602
  → PASS ✅

[predictions]
  max(t0)   = 2026-02-18 12:20:05+00:00
  lag_sec   = 1.5s ✅
  count(window=300s) = 60 / expected≈60
  fill_rate = 100.0% ✅
  last 3 rows:
    t0=12:20:05, p_up=0.0119, p_down=0.0264, p_none=0.9617, ev=-0.001949, ev_rate=-1.628e-05, action_hat=STAY_FLAT, model=baseline_v1_exec
    t0=12:20:00, p_up=0.0122, p_down=0.0278, p_none=0.9601, ev=-0.001942, ev_rate=-1.622e-05, action_hat=STAY_FLAT, model=baseline_v1_exec
    t0=12:19:55, p_up=0.0130, p_down=0.0286, p_none=0.9584, ev=-0.001942, ev_rate=-1.622e-05, action_hat=STAY_FLAT, model=baseline_v1_exec
  → PASS ✅

[evaluation_results (exec_v1)]
  ⚠️  evaluator는 horizon(120s) 경과 후 정산 → 초반엔 행이 적을 수 있음
  count(last 10min) = 58
  → PASS ✅

[paper_decisions]
  max(ts)   = 2026-02-18 12:20:05+00:00
  lag_sec   = 1.5s ✅
  count(window=300s) = 60 / expected≈60
  fill_rate = 100.0% ✅
  last 3 rows:
    ts=12:20:05, pos_status=FLAT, action=STAY_FLAT, reason=RATE_LIMIT, equity=998921, dd=-0.108%, profile=test
    ts=12:20:00, pos_status=FLAT, action=STAY_FLAT, reason=RATE_LIMIT, equity=998921, dd=-0.108%, profile=test
    ts=12:19:55, pos_status=FLAT, action=STAY_FLAT, reason=RATE_LIMIT, equity=998921, dd=-0.108%, profile=test
  → PASS ✅

[paper_trades]
  count(last 24h) = 8
  last 3 rows:
    t=12:18:10, action=EXIT_LONG,  reason=EV_BAD, price=100088978, qty=0.001995, fee=99.85, pnl=-237.70 KRW
    t=12:16:24, action=ENTER_LONG, reason=SIGNAL, price=100108018, qty=0.001995, fee=99.87
    t=12:15:24, action=EXIT_LONG,  reason=TIME,   price=100027990, qty=0.001997, fee=99.86, pnl=-269.67 KRW

[dashboard (http://localhost:8501)]
  /healthz → HTTP 200 ✅

============================================================
  개별 결과:
    market_1s               : PASS ✅
    barrier_state           : PASS ✅
    predictions             : PASS ✅
    paper_decisions         : PASS ✅
    evaluation_results      : OPTIONAL (OVERALL 미포함)
    dashboard               : PASS ✅

  OVERALL: PASS ✅
============================================================
```

---

## 3. PASS/FAIL 판정 상세

| 테이블 | lag_sec | fill_rate | 판정 |
|---|---|---|---|
| `market_1s` | **0.5s** ✅ (≤3s) | **100.0%** ✅ (≥90%) | **PASS** |
| `barrier_state` | **1.5s** ✅ (≤10s) | **100.0%** ✅ (≥90%) | **PASS** |
| `predictions` | **1.5s** ✅ (≤10s) | **100.0%** ✅ (≥90%) | **PASS** |
| `paper_decisions` | **1.5s** ✅ (≤10s) | **100.0%** ✅ (≥90%) | **PASS** |
| `evaluation_results` | — | 58건/10분 | **PASS** (optional) |
| `dashboard /healthz` | — | HTTP 200 | **PASS** |

**OVERALL: PASS ✅**

---

## 4. 봇 실행 로그 핵심 라인

```
12:13:14 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
12:13:15 [INFO] app.barrier.controller: Barrier: r_t=0.001694 ... status=WARMUP n=0
12:13:15 [INFO] app.predictor.runner: Pred(v1): t0=12:13:15 p_none=1.0000 p_up=0.0000 p_down=0.0000
12:13:19 [INFO] app.trading.runner: Paper: pos=FLAT action=STAY_FLAT reason=PNONE_HIGH
12:13:24 [INFO] app.trading.runner: PaperTrade ENTER: price=100063009 qty=0.00199660 fee=99.89 cash=799543 u_exec=100232510 d_exec=99893507 h=120s
12:13:24 [INFO] app.trading.runner: Paper: pos=LONG action=ENTER_LONG reason=OK equity=999221 dd=-0.0779%
```

- WS 연결 → 5초 이내 Barrier/Prediction 첫 출력
- WARMUP 해제 후 즉시 ENTER_LONG 발생 (test 프로필)
- paper_trades 24h 누적 8건 확인

---

## 5. 파이프라인 데이터 흐름 요약

```
Upbit WS → market_1s(1초 fill_rate=100%) → barrier_state(5초 주기, fill=100%)
         → predictions(5초 주기, fill=100%) → evaluation_results(120s 후 정산, 58건/10min)
         → paper_decisions(5초 주기, fill=100%) → paper_trades(24h 8건)
         → Streamlit dashboard(HTTP 200)
```

전 단계 끊김 없음. 정상 파이프라인 확인.

---

## 6. 진단 스크립트 트러블슈팅 기록

**발생 이슈:** 1차 실행 시 `paper_decisions`, `paper_trades` 쿼리가
`psycopg.errors.InFailedSqlTransaction` 오류로 SKIP됨

**원인:** SQLAlchemy + psycopg3 환경에서, 이전 쿼리가 예외를 발생시키면 해당 커넥션의 트랜잭션이 abort 상태가 되어 이후 모든 쿼리가 실패함

**해결:** `_safe_query()` 함수에 `conn.rollback()` 호출 추가 → 2차 실행에서 OVERALL PASS

---

## 7. 진단 스크립트 실행 방법

```bash
# 기본 (window=300s)
poetry run python -m app.diagnostics.realtime_check

# 윈도우 지정 (600s)
poetry run python -m app.diagnostics.realtime_check --window 600
```

종료 코드: `0`=OVERALL PASS, `1`=OVERALL FAIL

---

## 8. 관찰 사항

| 항목 | 값 | 해석 |
|---|---|---|
| BTC/KRW mid price | ~100,047,000~100,098,000 KRW | 정상 시장 데이터 |
| spread_bps | 4.9~5.1 bps | SPREAD_WIDE 미발동 (기준 20bps) |
| barrier status | OK | WARMUP 해제, 정상 동작 |
| p_none (최근) | 0.954~0.962 | 높음 → PNONE_HIGH 조건 자주 발동 |
| ev_rate (최근) | ~-1.6e-05 | 음수 → EV_RATE_LOW 조건 |
| paper equity | 998,921 KRW | 초기 1,000,000에서 소폭 감소 (정상) |
| paper dd | -0.108% | MAX_DRAWDOWN_PCT(5%) 훨씬 미만 |
| paper_trades 24h | 8건 (test 프로필) | ENTER/EXIT 정상 사이클 확인 |
| policy_profile | test | 운영 전 strict 복귀 필요 |
