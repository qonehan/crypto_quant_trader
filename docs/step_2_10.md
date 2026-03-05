# Step 2-10: GCP 원천 데이터 기반 일괄 데이터셋 생성기 작성 결과 보고

## 배경

로컬(Codespaces) 환경은 24시간 가동 불가 → `predictions` 테이블에 피처가 쌓이지 않음.
GCP는 `upbit_orderbook` 원천 데이터를 24시간 수집 중.
→ GCP 과거 데이터만으로 학습 가능한 완전한 데이터셋을 오프라인 일괄 생성.

---

## 신규 파일

`scripts/build_historical_dataset.py`

---

## 처리 파이프라인

```
GCP upbit_orderbook (raw tick)
    │
    ▼ [1] load_orderbook()
    │  timestamp AS ts, (level_1_bid + level_1_ask) / 2 AS mid
    │  + level_1~5 bid/ask price/size
    │
    ▼ [2] resample_1s()
    │  resample('1s').agg(mid=last, spread_bps=mean, imb_notional_top5=mean)
    │  + ffill() 공백 보간
    │
    ▼ [3] run_simulation()
    │  매 1초 캔들 순회
    │    ├─ market_window deque (최근 MODEL_LOOKBACK_SEC=120s)
    │    ├─ vol_window deque (최근 VOL_WINDOW_SEC=600s)
    │    └─ predict_interval_sec(=5)마다:
    │         sigma_1s = rolling std of log-returns (vol_window)
    │         barrier_row = synthetic dict (DB 없이 인라인 계산)
    │         BaselineModelV1.predict() → p_up, ev, mom_z, action_hat, ...
    │
    ▼ [4] generate_labels()
    │  merge_asof(direction='forward', left_on=ts+horizon, right_on=ts)
    │  → label_return = (future_mid - entry_mid) / entry_mid
    │  → label_lag_sec 범위 검증 + Hard FAIL
    │
    ▼ [5] 저장
       data/datasets/historical_dataset.parquet
```

---

## 주요 함수 역할

| 함수 | 설명 |
|---|---|
| `load_orderbook(engine, t_min, t_max)` | GCP `upbit_orderbook` → raw tick DataFrame |
| `resample_1s(raw)` | tick → 1초 캔들 (mid, spread_bps, imb_notional_top5) |
| `_compute_imb_notional_top5(df)` | level 1~5 bid/ask notional 불균형 계산, 컬럼 없으면 0 |
| `_build_barrier_row(...)` | DB 없이 sigma_1s로 r_t, sigma_h 계산 (WARMUP/OK 판정) |
| `run_simulation(candles, settings, interval)` | 시뮬레이션 루프 → feature rows |
| `generate_labels(features, prices, ...)` | merge_asof forward + lag guard + label_return |

---

## argparse 인자

| 인자 | 기본값 | 설명 |
|---|---|---|
| `--hours` | 24 | GCP에서 가져올 과거 데이터 기간 |
| `--horizon` | settings.H_SEC | 라벨 horizon (초) |
| `--predict-interval` | 5 | 예측 수행 간격 (초) |
| `--max-label-lag-mult` | 2.0 | max_label_lag = horizon × mult |
| `--output` | `data/datasets/historical_dataset.parquet` | 출력 경로 |

---

## 사용 예시

```bash
# 최근 48시간 데이터로 데이터셋 생성
poetry run python scripts/build_historical_dataset.py --hours 48

# horizon 60초, 예측 간격 10초
poetry run python scripts/build_historical_dataset.py --hours 24 --horizon 60 --predict-interval 10

# CSV 출력
poetry run python scripts/build_historical_dataset.py --hours 24 --output data/datasets/hist.csv
```

---

## export_dataset.py와의 차이

| 항목 | export_dataset.py | build_historical_dataset.py |
|---|---|---|
| 피처 소스 | 로컬 `predictions` 테이블 | GCP `upbit_orderbook` → 시뮬레이션 |
| 가격 소스 | GCP `upbit_orderbook` | 동일 resample 결과 재활용 |
| 실시간 봇 필요 여부 | 봇이 predictions를 쌓아야 함 | **불필요** (완전 오프라인) |
| 용도 | 실제 봇 로그 기반 학습 | **백테스트형 일괄 생성** |

---

## 상태

- [x] `scripts/build_historical_dataset.py` 작성 완료
- [ ] GCP DB 연결 후 실행 검증 필요 (`--hours 1` 으로 소규모 테스트 권장)
