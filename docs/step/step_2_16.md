# step_2_16 결과보고: SQL 쿼리 및 백업 파일명 수정

## 변경 대상
`scripts/build_historical_dataset.py`

---

## 에러 원인
`UndefinedTable` — 스크립트에 작성된 테이블명/파일명이 실제 DB 스키마(`src/database.py`)와 불일치.

---

## 수정 내역

### 1. SQL 상수 — Binance

**Before** (2개 쿼리, 구 테이블):
```python
# binance_futures_metrics 피벗 쿼리
_LOAD_BINANCE_METRICS_SQL = text("""
    SELECT ts,
        MAX(CASE WHEN metric='open_interest' ...) AS open_interest,
        MAX(CASE WHEN metric='global_ls_ratio' ...) AS long_short_ratio
    FROM binance_futures_metrics ...
""")

# binance_mark_price_1s에서 funding_rate
_LOAD_BINANCE_FUNDING_SQL = text("""
    SELECT ts, funding_rate FROM binance_mark_price_1s ...
""")
```

**After** (1개 쿼리, 실제 테이블):
```python
_LOAD_BINANCE_SQL = text("""
    SELECT
        timestamp AS ts,
        open_interest,
        long_short_ratio,
        funding_rate
    FROM binance_derivatives
    WHERE timestamp >= :t_min AND timestamp <= :t_max
    ORDER BY timestamp
""")
```

---

### 2. SQL 상수 — Macro

**Before**: `FROM macro_data`
**After**: `FROM macro_and_sentiment`

```python
_LOAD_MACRO_SQL = text("""
    SELECT timestamp AS ts, dxy_index, fear_greed_index
    FROM macro_and_sentiment
    WHERE timestamp >= :t_min AND timestamp <= :t_max
    ORDER BY timestamp
""")
```

---

### 3. `load_binance()` 함수 본체 단순화

| 항목 | Before | After |
|---|---|---|
| 백업 파일 | `binance_futures_metrics.parquet` + `binance_mark_price_1s.parquet` (2개) | `binance_derivatives.parquet` (1개) |
| pivot 로직 | long→wide pivot 포함 | 제거 |
| DB 쿼리 | 2회 (`_METRICS_SQL` + `_FUNDING_SQL`) | 1회 (`_LOAD_BINANCE_SQL`) |
| merge_asof | metrics + funding 별도 병합 | 불필요, 단일 소스 |

---

### 4. `load_macro()` 백업 파일명

**Before**: `"macro.parquet"`
**After**: `"macro_and_sentiment.parquet"`

---

## 최종 테이블·파일 매핑

| 소스 | DB 테이블 | 백업 Parquet 파일명 |
|---|---|---|
| Orderbook | `upbit_orderbook` | `upbit_orderbook.parquet` |
| Tick | `upbit_tick` | `upbit_tick.parquet` |
| Binance | `binance_derivatives` | `binance_derivatives.parquet` |
| Macro | `macro_and_sentiment` | `macro_and_sentiment.parquet` |
