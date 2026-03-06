# Step 2-9: GCP DB 실제 스키마 완벽 반영 결과 보고

## 배경

SQLAlchemy Inspector로 GCP의 `upbit_orderbook` 실제 스키마를 확인한 결과, 로컬 스키마와 컬럼명이 완전히 달랐습니다.

| 항목 | 로컬(가정) | GCP(실제) |
|---|---|---|
| 시간 컬럼 | `ts` | `timestamp` |
| 호가 컬럼 | `bid_price_0`, `ask_price_0` | `level_1_bid_price`, `level_1_ask_price` |
| symbol 컬럼 | 존재 | **존재하지 않음** |

---

## 수정 내용 (`scripts/export_dataset.py` — `load_prices`)

### 변경 전

```sql
SELECT timestamp AS ts, (bid_price_0 + ask_price_0) / 2.0 AS mid
FROM upbit_orderbook
WHERE symbol = :sym
  AND bid_price_0 IS NOT NULL
  AND ask_price_0 IS NOT NULL
  AND timestamp >= :t_min
  AND timestamp <= :t_max
ORDER BY timestamp
```
- `params = {"sym": symbol, "t_min": t_min, "t_max": t_max}`

### 변경 후

```sql
SELECT timestamp AS ts,
       (level_1_bid_price + level_1_ask_price) / 2.0 AS mid
FROM upbit_orderbook
WHERE level_1_bid_price IS NOT NULL
  AND level_1_ask_price IS NOT NULL
  AND timestamp >= :t_min
  AND timestamp <= :t_max
ORDER BY timestamp
```
- `params = {"t_min": t_min, "t_max": t_max}`

---

## 변경 포인트 요약

| 항목 | 변경 전 | 변경 후 |
|---|---|---|
| 가격 컬럼 | `bid_price_0`, `ask_price_0` | `level_1_bid_price`, `level_1_ask_price` |
| symbol 필터 | `WHERE symbol = :sym` | **제거** |
| NULL 필터 | `bid_price_0 IS NOT NULL ...` | `level_1_bid_price IS NOT NULL ...` |
| params (bounded) | `{"sym": symbol, "t_min": ..., "t_max": ...}` | `{"t_min": ..., "t_max": ...}` |
| params (unbounded) | `{"sym": symbol}` | `{}` |

- `timestamp AS ts` 별칭은 유지 → 이후 `pd.to_datetime`, `resample`, `merge_asof` 파이프라인 변경 없음.
- `symbol` 인자는 함수 시그니처에 유지 (로컬 엔진 폴백 호환성), 쿼리에만 미사용.

## 상태

- [x] GCP 실제 스키마 기준 컬럼명 수정 (`level_1_bid_price`, `level_1_ask_price`)
- [x] `symbol` 필터 및 `:sym` 파라미터 바인딩 제거
- [ ] GCP DB 연결 후 실행 검증 필요
