# Step 2-7: GCP 원천 데이터 활용 — `load_prices` 수정 결과 보고

## 배경

GCP DB에는 가공된 `market_1s` 테이블이 존재하지 않고, 원천 테이블(`upbit_orderbook`, `upbit_tick`)만 저장되어 있어 기존 쿼리 실행 시 `UndefinedTable` 에러 발생.

---

## 수정 내용 (`scripts/export_dataset.py` — `load_prices`)

### 변경 전

```python
SELECT ts, mid_close_1s as mid
FROM market_1s
WHERE symbol = :sym AND mid_close_1s IS NOT NULL
  AND ts >= :t_min AND ts <= :t_max
ORDER BY ts
```
- 리샘플링 없음, 원시 행 그대로 반환

### 변경 후 (전체 함수)

```python
def load_prices(engine, symbol: str, t_min=None, t_max=None) -> pd.DataFrame:
    """Load price data from upbit_orderbook for label generation.

    mid = (bid_price_0 + ask_price_0) / 2 계산 후 1초 리샘플링(ffill) 반환.
    """
    if t_min is not None and t_max is not None:
        query = text("""
            SELECT ts, (bid_price_0 + ask_price_0) / 2.0 AS mid
            FROM upbit_orderbook
            WHERE symbol = :sym
              AND bid_price_0 IS NOT NULL
              AND ask_price_0 IS NOT NULL
              AND ts >= :t_min
              AND ts <= :t_max
            ORDER BY ts
        """)
        params = {"sym": symbol, "t_min": t_min, "t_max": t_max}
    else:
        query = text("""
            SELECT ts, (bid_price_0 + ask_price_0) / 2.0 AS mid
            FROM upbit_orderbook
            WHERE symbol = :sym
              AND bid_price_0 IS NOT NULL
              AND ask_price_0 IS NOT NULL
            ORDER BY ts
        """)
        params = {"sym": symbol}

    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params=params)

    if df.empty:
        return df

    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts")[["mid"]].resample("1s").last().ffill().reset_index()
    return df
```

---

## 변경 포인트 요약

| 항목 | 변경 전 | 변경 후 |
|---|---|---|
| 소스 테이블 | `market_1s` | `upbit_orderbook` |
| mid 컬럼 | `mid_close_1s` (기 가공) | `(bid_price_0 + ask_price_0) / 2.0` (직접 계산) |
| NULL 필터 | `mid_close_1s IS NOT NULL` | `bid_price_0 IS NOT NULL AND ask_price_0 IS NOT NULL` |
| 리샘플링 | 없음 | `resample('1s').last().ffill()` — 1초 캔들 + 빈 구간 ffill |
| 반환 형태 | 원시 tick 행 | 1초 단위 정규화 DataFrame (`ts`, `mid`) |

## 리샘플링 파이프라인 상세

```
upbit_orderbook (tick 단위)
  └─ (bid_price_0 + ask_price_0) / 2  →  mid 계산
  └─ set_index('ts')                  →  DatetimeIndex (UTC)
  └─ resample('1s').last()            →  1초 구간 마지막 호가 mid
  └─ ffill()                          →  누락 구간 직전 가격으로 연속성 확보
  └─ reset_index()                    →  ts 컬럼 복원 후 반환
```

- `resample('1s').last()` 선택 이유: 호가 갱신이 여러 번 발생하는 1초 구간에서 가장 최신 mid를 label 기준 가격으로 사용.
- `ffill()` 적용으로 체결 공백 구간에서도 `generate_labels`의 `merge_asof` 매칭 실패 방지.

## 상태

- [x] `load_prices` 함수 수정 완료
- [x] `engine_gcp`를 인자로 수신 (Step 2-6 듀얼 엔진 연동)
- [ ] 실제 GCP DB 연결 후 실행 검증 필요
