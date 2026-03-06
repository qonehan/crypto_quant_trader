# step_2_14 결과보고: `cost_roundtrip_est` 피처 추가

## 변경 대상
`scripts/build_historical_dataset.py`

---

## 에러 원인
`scripts/train_and_trade_econ_gate.py`가 학습 데이터셋에서 `cost_roundtrip_est` 컬럼을 요구하나,
`build_historical_dataset.py`가 해당 컬럼을 생성하지 않아 `cost_roundtrip_est missing` 에러 발생.

---

## 수정 내역

### 1. `resample_1s()` — 계산 및 집계 추가

**계산식 추가** (tick 단계, `set_index` 이전):
```python
raw["cost_roundtrip_est"] = (
    raw["spread_raw"] / raw["mid"].where(raw["mid"] > 0, other=_EPS)
) + 0.001
```
- `spread_raw / mid`: 현재 스프레드 비율 (bid-ask spread / 중간가)
- `+ 0.001`: 업비트 시장가 왕복 수수료 0.1% (매수 0.05% + 매도 0.05%)

**resample agg 추가**:
```python
agg = raw[["mid", "spread_bps", "imb_notional_top5", "cost_roundtrip_est"]].resample("1s").agg({
    "mid": "last",
    "spread_bps": "mean",
    "imb_notional_top5": "mean",
    "cost_roundtrip_est": "mean",   # 추가
})
```
1초 구간 내 여러 tick의 평균값으로 집계 후 `ffill()`로 빈 구간 보간.

---

### 2. `run_simulation()` — 캔들에서 읽어 features 행에 포함

**candle row에서 읽기**:
```python
cost_rt: float = float(row["cost_roundtrip_est"]) if pd.notna(row["cost_roundtrip_est"]) else 0.001
```
- NaN인 경우 fallback: 수수료만 적용한 최소값 `0.001`

**rows.append dict에 추가**:
```python
rows.append({
    ...
    "cost_roundtrip_est": cost_rt,   # 추가
    "action_hat": output.action_hat,
    ...
})
```

---

## 데이터 흐름 요약

```
load_orderbook()
  → spread_raw, mid 컬럼 포함 raw tick DataFrame
       ↓
resample_1s()
  → cost_roundtrip_est = (spread_raw / mid) + 0.001  [tick 단계]
  → resample "1s" mean 집계
  → candles DataFrame (cost_roundtrip_est 포함)
       ↓
run_simulation()
  → 각 candle row에서 cost_roundtrip_est 읽기
  → features DataFrame rows에 포함
       ↓
generate_labels()
  → features와 prices merge → dataset
       ↓
dataset.to_parquet()  ← cost_roundtrip_est 컬럼 포함 출력
```

---

## 컬럼 값 범위 (참고)

| 항목 | 전형적 값 |
|---|---|
| 업비트 BTC 스프레드 비율 | 0.00005 ~ 0.0003 (0.005% ~ 0.03%) |
| 왕복 수수료 고정값 | 0.001 (0.1%) |
| `cost_roundtrip_est` 범위 | 약 0.00105 ~ 0.0013 |
