# Step ALT-1 보강 결과보고서

작성일: 2026-02-22
작성자: Claude Code (claude-sonnet-4-6)

---

## 1. 개요

Step ALT 결과보고서에서 드러난 진단 표기 모순을 제거하고, Alt Data를 학습/피처로 바로 쓸 수 있는 형태(`feature_snapshots`)로 정렬 적재하여 다음 단계(모델 개선/백테스트)로 넘어갈 수 있게 만든 보강 작업.

---

## 2. 수정/신규 파일 목록

### 신규 (5개)

| 파일 | 설명 |
|------|------|
| `app/features/__init__.py` | Feature Snapshot 패키지 초기화 |
| `app/features/writer.py` | `upsert_feature_snapshot()` — JSONB CAST + ON CONFLICT UPSERT |
| `app/diagnostics/feature_check.py` | feature_snapshots 파이프라인 자동 진단 (exit 0/1) |
| `app/diagnostics/prune_altdata.py` | Alt Data 테이블 retention 정리 (`--days 7`, `--dry-run`) |
| `step-alt-1-improvements.md` | 본 결과보고서 |

### 수정 (8개)

| 파일 | 변경 내용 |
|------|-----------|
| `app/config.py` | `is_real_key()` 헬퍼 추가 + `BINANCE_METRICS_FRESH_SEC=180` 설정 |
| `app/diagnostics/altdata_check.py` | lag clamp (max 0.0) + `is_real_key()` 적용 + `check_coinglass` 모순 제거 |
| `app/altdata/coinglass_rest.py` | `is_real_key()` 적용 + 실패 원인 명확 로깅 + anti-spam |
| `app/altdata/runner.py` | `is_real_key()` 적용 |
| `app/bot.py` | `is_real_key()` 적용 |
| `app/db/migrate.py` | Step ALT-1: `feature_snapshots` 테이블 + 인덱스 idempotent 생성 |
| `app/predictor/runner.py` | `_FETCH_BARRIER_SQL`에 `k_vol_eff/r_min_eff/cost_roundtrip_est` 추가 + `_save_feature_snapshot()` 구현 |
| `app/dashboard.py` | `[G4] Feature Snapshots` 섹션 추가 (metrics/charts/table) + `is_real_key()` 적용 |
| `scripts/activate_env_keys.py` | placeholder 감지 로직 추가 (빈값/짧은값/`your_key_here` 등 → `__PUT_REAL_KEY_HERE__`) |

---

## 3. DoD 달성 현황

### (A) 진단/표기 보강

| 항목 | 결과 |
|------|------|
| COINGLASS_KEY_SET 표기 모순 | ✅ `is_real_key()` 판정으로 단일화 — `COINGLASS_KEY_SET = False (is_real_key 판정)` |
| lag_sec 음수 출력 | ✅ `_lag()` 함수에 `max(0.0, raw)` clamp 적용 |
| Coinglass 실패 원인 명확화 | ✅ `CoinglassRestPoller`: 빈값/placeholder 구분하여 정확한 reason 로깅 |

### (B) Feature Snapshot

| 항목 | 결과 |
|------|------|
| `feature_snapshots` 테이블 생성 | ✅ 30컬럼, PRIMARY KEY (ts, symbol), 멱등 마이그레이션 |
| fill_rate ≥ 90% (5분) | ✅ **100.0%** (60/60 rows in 300s window) |
| `feature_check.py` OVERALL PASS | ✅ exit 0 |

### (C) Dashboard

| 항목 | 결과 |
|------|------|
| G4 Feature Snapshots 섹션 | ✅ metrics(lag/fill_rate/null_rates) + charts(4종) + table(50행) |

### (D) 결과물

| 항목 | 결과 |
|------|------|
| `step-alt-1-improvements.md` 작성 | ✅ (본 파일) |

---

## 4. `altdata_check --window 300` 실행 결과 전문

```
============================================================
  Alt Data 파이프라인 점검
  now_utc          = 2026-02-22T05:12:03.304887+00:00
  binance_symbol   = BTCUSDT
  coinglass_symbol = BTC
  window           = 300s
  BINANCE_POLL_SEC = 60s
  COINGLASS_POLL_SEC = 300s
  COINGLASS_KEY_SET  = False (is_real_key 판정)
============================================================
[binance_mark_price_1s]
  max(ts)   = 2026-02-22 05:12:03.005000+00:00
  lag_sec   = 0.3s ✅
  count(window=300s) = 300 / expected=300
  fill_rate = 100.0% ✅
  last 3 rows:
  ts=2026-02-22 05:12:03.005000+00:00, mark_price=67859.8, index_price=67890.868, funding_rate=4.26e-06
  ts=2026-02-22 05:12:02+00:00, mark_price=67859.9, index_price=67887.612, funding_rate=4.26e-06
  ts=2026-02-22 05:12:01+00:00, mark_price=67857.2, index_price=67888.136, funding_rate=4.26e-06
  → PASS ✅

[binance_futures_metrics]
  [open_interest] lag=60.7s ✅
  [global_ls_ratio] lag=63.3s ✅
  [taker_ls_ratio] lag=63.3s ✅
  [basis] lag=63.3s ✅
  → PASS ✅

[binance_force_orders]
  count(last 24h)  = 1
  last event ts    = 2026-02-22 05:07:20.662000+00:00
  → PASS ✅ (connection-based check)

[coinglass_liquidation_map]
  max_ts = None (COINGLASS_API_KEY 미설정/비정상 → 수집 SKIP)
  → SKIP ✅ (키 없음 — 정상)

============================================================
  개별 결과:
    binance_mark_price_1s              : PASS ✅
    binance_futures_metrics            : PASS ✅
    binance_force_orders               : PASS ✅
    coinglass_liquidation_map          : PASS ✅

  OVERALL: PASS ✅
============================================================
```

---

## 5. `feature_check --window 300` 실행 결과 전문

```
============================================================
  feature_snapshots 파이프라인 점검
  now_utc            = 2026-02-22T05:12:07.454904+00:00
  symbol             = KRW-BTC
  window             = 300s
  interval_sec       = 5s
  expected_rows      ≈ 60
============================================================
[feature_snapshots — lag]
  max(ts)  = 2026-02-22 05:12:05+00:00
  lag_sec  = 2.5s ✅
  → PASS ✅

[feature_snapshots — fill_rate (window=300s)]
  count = 60  expected ≈ 60  fill_rate = 100.0% ✅
  → PASS ✅

[feature_snapshots — null_rates (window=300s)]
  mid_krw                  : null_rate=0.0% ✅  (0/60)
  spread_bps               : null_rate=0.0% ✅  (0/60)
  p_none                   : null_rate=0.0% ✅  (0/60)
  ev_rate                  : null_rate=0.0% ✅  (0/60)
  bin_funding_rate         : null_rate=0.0% ✅  (0/60)
  oi_value                 : null_rate=0.0% ✅  (0/60)
  liq_5m_notional          : null_rate=0.0% ✅  (0/60)
  → PASS ✅

[feature_snapshots — liq_5m_notional null check]
  liq_5m_notional null_rate = 0.0% (0/60)
  ※ 값이 0이면 정상 (청산 이벤트 없음), null은 파이프라인 오류
  → PASS ✅

[feature_snapshots — 최근 5행 샘플]
  (2026-02-22 05:12:05, 0.99, -1.97e-05, 4.26e-06, 80629.98, 22175.82)
  (2026-02-22 05:12:00, 0.99, -1.84e-05, 4.26e-06, 80629.98, 22175.82)
  (2026-02-22 05:11:55, 0.99, -1.62e-05, 4.30e-06, 80629.98, 22175.82)
  (2026-02-22 05:11:50, 0.99, -1.50e-05, 4.30e-06, 80629.98, 22175.82)
  (2026-02-22 05:11:45, 0.99, -1.50e-05, 4.30e-06, 80629.98, 22175.82)

============================================================
  개별 결과:
    lag                 : PASS ✅
    fill_rate           : PASS ✅
    null_rates          : PASS ✅
    liq_not_null        : PASS ✅

  OVERALL: PASS ✅
============================================================
```

---

## 6. `feature_snapshots` 최근 10행 (핵심 컬럼)

```
ts                   | symbol  | p_none | ev_rate    | bin_fr    | oi_value  | liq_5m_notional
---------------------------------------------------------------------------------------------
2026-02-22 05:12:15 | KRW-BTC | 0.9900 | -1.97e-05 | 4.26e-06 | 80608.06  | 22175.82
2026-02-22 05:12:10 | KRW-BTC | 0.9900 | -1.97e-05 | 4.26e-06 | 80608.06  | 22175.82
2026-02-22 05:12:05 | KRW-BTC | 0.9900 | -1.97e-05 | 4.26e-06 | 80629.98  | 22175.82
2026-02-22 05:12:00 | KRW-BTC | 0.9900 | -1.84e-05 | 4.26e-06 | 80629.98  | 22175.82
2026-02-22 05:11:55 | KRW-BTC | 0.9900 | -1.62e-05 | 4.30e-06 | 80629.98  | 22175.82
2026-02-22 05:11:50 | KRW-BTC | 0.9900 | -1.50e-05 | 4.30e-06 | 80629.98  | 22175.82
2026-02-22 05:11:45 | KRW-BTC | 0.9900 | -1.50e-05 | 4.30e-06 | 80629.98  | 22175.82
2026-02-22 05:11:40 | KRW-BTC | 0.9900 | -1.52e-05 | 4.30e-06 | 80629.98  | 22175.82
2026-02-22 05:11:35 | KRW-BTC | 0.9900 | -1.16e-05 | 4.30e-06 | 80629.98  | 22175.82
2026-02-22 05:11:30 | KRW-BTC | 0.9900 | -1.18e-05 | 4.30e-06 | 80629.98  | 22175.82
```

---

## 7. Dashboard G4 섹션 (헤드리스 환경)

Dashboard는 헤드리스 환경이므로 캡처 대신 DB에서 확인한 주요 G4 metric 값 기록:

| 항목 | 값 |
|------|-----|
| Last TS | 2026-02-22 05:12:15 UTC |
| Lag (sec) | 2.5s |
| Fill Rate 5min | 100.0% (60/60) |
| null mid_krw | 0.0% |
| null p_none | 0.0% |
| null bin_funding_rate | 0.0% |
| null oi_value | 0.0% |
| bin_funding_rate (latest) | 4.26e-06 |
| oi_value (latest) | 80,608 BTC |
| liq_5m_notional (latest) | 22,175 USDT |

---

## 8. 발견 이슈 / 해결

| 이슈 | 해결 |
|------|------|
| `COINGLASS_KEY_SET = True` 로 출력되지만 실제 키값이 `your_key_here` → 모순 | `is_real_key()` 헬퍼로 단일 판정. `COINGLASS_KEY_SET = False (is_real_key 판정)` 으로 출력 |
| lag_sec 음수 출력 가능성 | `_lag()` 함수에 `max(0.0, raw)` clamp 적용 |
| Coinglass 실패 시 "미설정" 같은 오해 로그 | `CoinglassRestPoller.run()` 초기화 시 빈값/placeholder 구분하여 정확한 reason 로깅 |
| barrier_row에서 `k_vol_eff, r_min_eff, cost_roundtrip_est` 누락 | `_FETCH_BARRIER_SQL`에 해당 컬럼 추가 |
| feature_snapshots 저장 중 예외 발생 시 predictor 전체 중단 가능성 | `try/except`로 non-fatal 처리 |

---

## 9. 완료 판정

- [x] `altdata_check --window 300` → **OVERALL PASS ✅**
- [x] `feature_check --window 300` → **OVERALL PASS ✅**
- [x] `step-alt-1-improvements.md` 제출 ← (본 파일)

**Step ALT 보강 완료.**
