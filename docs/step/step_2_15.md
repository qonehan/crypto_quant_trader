# step_2_15 결과보고: 4대 원천 데이터 하이브리드 병합

## 변경 대상
`scripts/build_historical_dataset.py` 전면 재작성

---

## 아키텍처 요약

```
[로컬 백업 Parquet]           [GCP 라이브 DB]
  upbit_orderbook.parquet  →  upbit_orderbook 테이블
  upbit_tick.parquet       →  upbit_tick 테이블
  binance_futures_metrics.parquet  →  binance_futures_metrics (pivot)
  binance_mark_price_1s.parquet    →  binance_mark_price_1s (funding_rate)
  macro.parquet            →  macro_data 테이블
        ↓ concat + dedup         ↓ try/except 안전 처리
        └──────────── load_*() 함수 ────────────┘
                           ↓
                    resample_1s()  ← 4소스 Left-join
                           ↓
                    run_simulation()  ← 신규 컬럼 포함 출력
                           ↓
                    generate_labels()
                           ↓
                    dataset.to_parquet()
```

---

## 1. 공통 헬퍼 함수 추가

| 함수 | 역할 |
|---|---|
| `_date_range(t_min, t_max)` | t_min~t_max 범위의 UTC date 리스트 생성 |
| `_utc(dt)` | datetime을 UTC-aware로 변환 |
| `_read_parquet_days(backup_root, file_name, ...)` | 날짜별 백업 Parquet 일괄 읽기 + 기간 자르기 |
| `_merge_and_dedup(frames, ts_col)` | concat → sort → drop_duplicates 반환 |

---

## 2. 3개 로드 함수 추가

### `load_tick(engine, t_min, t_max, backup_dir)`
- 백업: `{backup_dir}/{date}/upbit_tick.parquet`
- DB: `upbit_tick` 테이블 (`timestamp`, `price`, `volume`, `ask_bid`)
- tick은 동일 초 복수 체결 허용 → `drop_duplicates` 미적용
- 테이블/파일 없으면 빈 DataFrame 반환 (WARN 출력)

### `load_binance(engine, t_min, t_max, backup_dir)`
- 백업: `binance_futures_metrics.parquet` (long/wide 자동 감지) + `binance_mark_price_1s.parquet`
- DB: `binance_futures_metrics` (pivot: `open_interest`, `global_ls_ratio→long_short_ratio`) + `binance_mark_price_1s` (funding_rate)
- 두 소스를 `merge_asof(direction='nearest')`로 합산
- 반환 컬럼: `ts`, `funding_rate`, `long_short_ratio`, `open_interest`

### `load_macro(engine, t_min, t_max, backup_dir)`
- 백업: `{backup_dir}/{date}/macro.parquet`
- DB: `macro_data` 테이블 (`timestamp`, `dxy_index`, `fear_greed_index`)
- 테이블/파일 없으면 빈 DataFrame 반환 (WARN 출력)

---

## 3. 리샘플링 헬퍼 함수 추가

### `_resample_tick_1s(raw_tick)`
```python
# ask_bid == 'ASK' → 매수 체결 (업비트 컨벤션)
df["is_buy"] = (df["ask_bid"].str.upper() == "ASK")
agg["total_volume"]    = sum(volume)
agg["buy_volume"]      = sum(volume where is_buy)
agg["sell_volume"]     = total_volume - buy_volume
agg["buy_volume_ratio"] = buy_volume / total_volume  # 0 나누기 → NaN → fillna(0)
```
빈 1초 구간: 모든 컬럼 `0.0` 채움

### `_resample_binance_1s(raw_binance, index_ts)`
- `resample("1s").last().ffill()` 적용
- 반환: `ts`, `funding_rate`, `long_short_ratio`, `open_interest`

### `_resample_macro_1s(raw_macro)`
- `resample("1s").last().ffill()` 적용
- 반환: `ts`, `dxy_index`, `fear_greed_index`

---

## 4. `resample_1s()` 시그니처 확장 및 병합

```python
def resample_1s(
    raw: pd.DataFrame,
    raw_tick: pd.DataFrame | None = None,
    raw_binance: pd.DataFrame | None = None,
    raw_macro: pd.DataFrame | None = None,
) -> pd.DataFrame:
```

병합 전략:
1. Orderbook 1초 캔들 → **주축(Left)**
2. Tick, Binance, Macro 각각 1초 리샘플 후 `pd.merge_asof(direction='backward')`
3. 초반 결측치: `bfill()` → 나머지: `fillna(0.0)`

최종 컬럼:

| 그룹 | 컬럼 |
|---|---|
| Orderbook | `ts`, `mid`, `spread_bps`, `imb_notional_top5`, `cost_roundtrip_est` |
| Tick | `total_volume`, `buy_volume`, `sell_volume`, `buy_volume_ratio` |
| Binance | `funding_rate`, `long_short_ratio`, `open_interest` |
| Macro | `dxy_index`, `fear_greed_index` |

---

## 5. `run_simulation()` — 신규 컬럼 전파

각 candle row에서 새 피처를 읽어 `rows.append()` dict에 포함:

```python
"buy_volume_ratio":  buy_vol_ratio,
"funding_rate":      funding_rate,
"long_short_ratio":  long_short_ratio,
"open_interest":     open_interest,
"dxy_index":         dxy_index,
"fear_greed_index":  fear_greed_index,
```

컬럼 없음/NaN → safe getter `_fval(row, col, default)` 로 기본값 처리:
- tick: `0.0`, binance: `0.0`, macro: `0.0`

---

## 6. `main()` — Step 1 4분할 로드

```
[1a] load_orderbook()  → 필수 (없으면 ERROR exit 1)
[1b] load_tick()       → 선택 (없으면 WARN, buy_volume_ratio=0.0)
[1c] load_binance()    → 선택 (없으면 WARN, binance 컬럼 0.0)
[1d] load_macro()      → 선택 (없으면 WARN, macro 컬럼 0.0)
     ↓
resample_1s(raw_ob, raw_tick, raw_binance, raw_macro)
```

---

## 7. 내결함성(Fault Tolerance) 설계

| 상황 | 동작 |
|---|---|
| 백업 Parquet 없음 | 해당 날짜 건너뜀, DB 시도 |
| DB 테이블 없음 (tick, macro) | WARNING 출력 후 빈 DataFrame |
| tick/binance/macro 전체 없음 | 기본값(0.0)으로 컬럼 생성, 학습 계속 |
| Orderbook 없음 | ERROR, exit 1 |

---

## 8. 최종 출력 컬럼 (Parquet)

orderbook 기반 피처 + tick/binance/macro 피처 + 라벨:

```
ts, r_t, sigma_1s, sigma_h, barrier_status,
p_up, p_down, p_none, ev, ev_rate, z_barrier, mom_z,
spread_bps, imb_notional_top5, cost_roundtrip_est,
action_hat, model_version,
buy_volume_ratio,
funding_rate, long_short_ratio, open_interest,
dxy_index, fear_greed_index,
label_ts, future_mid, entry_mid, label_lag_sec, label_return
```
