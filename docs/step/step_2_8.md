# Step 2-8: GCP DB 테이블 스키마 불일치 수정 결과 보고

## 배경

Step 2-7에서 `upbit_orderbook` 쿼리를 적용했으나, GCP 수집기가 시간 컬럼을 `ts`가 아닌 `timestamp`로 정의하여 실행 시 아래 에러 발생.

```
column "ts" does not exist
```

---

## 수정 내용 (`scripts/export_dataset.py` — `load_prices` SQL 쿼리)

### 변경 전

```sql
SELECT ts, (bid_price_0 + ask_price_0) / 2.0 AS mid
FROM upbit_orderbook
WHERE symbol = :sym
  AND bid_price_0 IS NOT NULL
  AND ask_price_0 IS NOT NULL
  AND ts >= :t_min
  AND ts <= :t_max
ORDER BY ts
```

### 변경 후

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

---

## 변경 포인트 요약

| 위치 | 변경 전 | 변경 후 |
|---|---|---|
| SELECT | `ts` | `timestamp AS ts` |
| WHERE (하한) | `ts >= :t_min` | `timestamp >= :t_min` |
| WHERE (상한) | `ts <= :t_max` | `timestamp <= :t_max` |
| ORDER BY | `ORDER BY ts` | `ORDER BY timestamp` |

- `timestamp AS ts` 별칭 처리로 Pandas DataFrame 및 이후 파이프라인(`resample`, `merge_asof`)은 기존과 동일하게 `ts` 컬럼명 그대로 사용.
- 범위 쿼리(`t_min` / `t_max` 없음) 분기도 동일하게 적용.

## 상태

- [x] `load_prices` SQL 컬럼명 `ts` → `timestamp` 수정 완료
- [ ] GCP DB 연결 후 실행 검증 필요
