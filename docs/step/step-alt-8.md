# Step ALT-8 — Codespaces 런타임 검증 결과보고서

작성일: 2026-02-22
브랜치: copilot/review-alt-3-changes
환경: Codespaces/DevContainer (Python 3.11.13, PostgreSQL @ db:5432)

---

## 1) 최종 판정

| 항목 | 결과 |
|------|------|
| DB_CONNECT_OK | ✅ |
| run_pipeline_checks --window 600 | EXIT_CODE=1 (FAIL) |
| coinglass_call_status (last30): ok_true | 0 (API 키 미설정) |
| coinglass_call_status (last30): http_2xx | 0 (API 키 미설정) |
| Export 1h: ./data/datasets/btc_1h.parquet | 31K / 220 rows (PARQUET_1H_OK ✅) |
| 라벨 후보 컬럼 | `label_return` (연속값) |
| 라벨 분포(1h, horizon=120s) | UP=0(0%), DOWN=0(0%), NONE=220(100%) |
| **다음 단계** | **ALT-9 즉시** (UP+DOWN < 1%, r_t/horizon 튜닝 필요) |

---

## 2) 실행 증거

### 2-1 run_pipeline_checks 출력 요약

```
Pipeline Checks — 운영 점검 원클릭 (2026-02-22T12:07:00Z)

[1/4] altdata_check     → FAIL ❌
  - binance_mark_price_1s : PASS ✅  lag=0.4s fill=200%
  - binance_futures_metrics: FAIL ❌  [basis] lag=181.4s > threshold 150s
  - binance_force_orders   : PASS ✅  count_24h=13
  - coinglass_liquidation_map: PASS ✅ (SKIP, key 미설정)

[2/4] feature_check     → PASS ✅
  - predictions_quality : p_up/p_down/p_none/ev/ev_rate/r_t/mom_z/spread_bps 모두 100% (120rows)
  - market_features     : mid/spread_bps/imb/bid/ask 모두 ≥91% (600rows)

[3/4] feature_leak_check → PASS ✅
  - prediction_ts_order  : violations=0 (120 preds)
  - evaluation_horizon   : early_evals=0 (96 evals)
  - feature_market_align : avg_latency=0.51s max=0.51s

[4/4] coinglass_check   → FAIL ❌
  COINGLASS_ENABLED=false → FAIL (SKIP PASS 금지)

PIPELINE OVERALL: FAIL ❌  EXIT_CODE=1
```

### 2-2 Coinglass DB 증거 (last30)

- `coinglass_call_status` rows: **0건**
- `COINGLASS_API_KEY not set, skipping` → Coinglass poller 즉시 종료
- ok_true_count_in_last30 = 0
- http_2xx_count_in_last30 = 0

### 2-3 Export 로그 요약

```
Dataset Export (라벨 누수 방지)
  symbol      = KRW-BTC
  horizon_sec = 120
  output      = ./data/datasets/btc_1h.parquet

[1] features: 244 rows
[2] prices: 1219 rows
[3] label generation:
    ⚠️  Dropped 24 rows: no future price found for label
    244 → 220 rows (dropped 24)
    ✅ All 220 rows pass label_ts >= t0 + horizon_sec

EXPORT COMPLETE ✅  (31K, 220 rows)
```

**라벨 분포 상세**:
```
threshold (r_t mean) = 0.001930
UP   (>= +threshold): 0   (0.0%)
DOWN (<= -threshold): 0   (0.0%)
NONE (within ±thr):  220 (100.0%)

range: [-0.001568, +0.000484]
mean = -0.000144   std = 0.000424
```

→ 실제 120초 수익률 범위가 r_t 임계값(~0.00193)보다 작아 UP/DOWN 라벨 전혀 생성 안 됨.

### 2-4 DB 데이터 수집 현황 (약 21분 구동 후)

| 테이블 | 집계 | 결과 |
|--------|------|------|
| market_1s (last 10min) | 600 rows | ✅ |
| predictions (last 60min) | 249 rows | ✅ |
| binance_mark_price_1s (last 10min) | 1200 rows | ✅ |
| binance_futures_metrics (last 60min) | 95 rows | ✅ |
| binance_force_orders (last 24h) | 14 rows | ✅ |
| coinglass_liquidation_map (last 24h) | 0 rows | API 키 없음 |

### 2-5 봇 부팅 로그 (핵심)

```
11:48:48 DB schema ensured → DB migrations applied
11:48:48 BinanceAltDataRunner enabled (symbol=BTCUSDT)
11:48:48 CoinglassAltDataRunner enabled (key_set=False)
11:48:48 CoinglassRestPoller: COINGLASS_API_KEY not set, skipping
11:48:49 WS connected to wss://api.upbit.com/websocket/v1
11:48:49 Binance forceOrder WS connected
11:48:49 Binance markPrice WS connected
```

---

## 3) 이슈/조치

### 이슈 1: binance_futures_metrics `basis` 간헐적 폴링 누락

- **이슈**: `basis` 메트릭 lag=181.4s, altdata_check 임계값 150s 초과 → FAIL
- **원인**: 네트워크/API — Binance `/futures/data/basis` 엔드포인트가 특정 폴 사이클에서 응답을 건너뜀(빈 결과 반환). 코드에 basis 폴 실패 시 명시적 로그가 없어 무음 누락. DB에 12:01→12:02→12:04→12:07 형태의 간헐적 갭 관찰됨.
- **조치**: `binance_rest.py` `_poll_all()`에 basis API 실패 시 경고 로그 추가. altdata_check 임계값을 `POLL_SEC*3+30`(=210s)으로 완화하거나, basis 전용 재시도 로직 추가 권장.
- **재발 방지**: ALT-9 시작 전 basis 폴링 로그를 확인해 연속 누락 여부 모니터링.

### 이슈 2: COINGLASS_ENABLED=false (API 키 미설정)

- **이슈**: Coinglass poller 즉시 종료, coinglass_check FAIL, coinglass_call_status 0건
- **원인**: 설정 — `.env` 파일 없음, `COINGLASS_API_KEY` 환경변수 미설정 (기본값 `""`)
- **조치**: `.env` 파일 생성 또는 Codespaces Secret에 `COINGLASS_API_KEY`, `COINGLASS_ENABLED=true` 추가 필요.
- **재발 방지**: `.env.example`에 COINGLASS 설정 주석 해제 및 실제 키 입력 가이드 명시.

### 이슈 3: UP+DOWN 라벨 0% → 학습 불가

- **이슈**: label_return 범위 [-0.0016, +0.0005]가 r_t 임계값(~0.00193) 미만 → 전체 220행 NONE
- **원인**: 데이터 부족 — 21분 수집 데이터(1초 bar 기준 약 1200행)로 120초 수익률이 매우 작음. 또한 r_t가 변동성에 비해 높게 설정됨 (k_vol_eff=0.5까지 내려감).
- **조치**: **ALT-9 즉시 진행** — r_t/horizon 튜닝 실험 루프 (예: horizon=300~600s, 또는 r_t 비율 임계값 조정).
- **재발 방지**: 더 긴 수집 기간(6h+) 후 Export 재시도, 또는 히스토리컬 데이터 백필 검토.

---

## 4) 봇 상태 (보고서 작성 시점)

- Upbit WS: 연결 유지 (T=2532 Tr=3179 OB=4006, err=0 reconn=0)
- Barrier: status=OK, n=120, k_vol_eff=0.5000 (최솟값 도달)
- EvalMetrics: N=226, acc=1.000, hit=0.000, none=1.000, brier=0.0008
- Paper: FLAT (no entries, PNONE_HIGH / COST_GT_RT)
- Binance WS: markPrice + forceOrder 정상 연결
