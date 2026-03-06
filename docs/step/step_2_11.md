# Step 2-11: GCP DB Level 1 전용 스키마 반영 결과 보고

## 배경

`build_historical_dataset.py` 실행 시 아래 에러 발생:
```
column "level_2_bid_price" does not exist
```

GCP `upbit_orderbook` 테이블은 용량 최적화로 **Level 1 호가 + `orderbook_imbalance`** 만 저장.
Level 2~5 컬럼은 존재하지 않음.

---

## 수정 내용

### 1. `_LOAD_ORDERBOOK_SQL` — SELECT 절 단순화

**변경 전 (level 1~5 전부 요청)**
```sql
SELECT
    timestamp AS ts,
    (level_1_bid_price + level_1_ask_price) / 2.0 AS mid,
    level_1_ask_price - level_1_bid_price          AS spread_raw,
    level_1_bid_price, level_1_ask_price,
    level_1_bid_size,  level_1_ask_size,
    level_2_bid_price, level_2_ask_price,   -- ❌ 존재하지 않음
    level_2_bid_size,  level_2_ask_size,
    ...
    level_5_bid_price, level_5_ask_price,
    level_5_bid_size,  level_5_ask_size
FROM upbit_orderbook ...
```

**변경 후 (Level 1 + orderbook_imbalance 전용)**
```sql
SELECT
    timestamp AS ts,
    (level_1_bid_price + level_1_ask_price) / 2.0 AS mid,
    level_1_ask_price - level_1_bid_price          AS spread_raw,
    orderbook_imbalance
FROM upbit_orderbook
WHERE level_1_bid_price IS NOT NULL
  AND level_1_ask_price IS NOT NULL
  AND timestamp >= :t_min
  AND timestamp <= :t_max
ORDER BY timestamp
```

---

### 2. `_compute_imb_notional_top5()` 함수 삭제

Level 2~5 bid/ask size × price 기반 복잡한 계산 로직 전체 제거.

---

### 3. `resample_1s()` — imbalance 매핑 단순화

**변경 전**
```python
raw["imb_notional_top5"] = _compute_imb_notional_top5(raw)  # level 1~5 계산
```

**변경 후**
```python
raw["imb_notional_top5"] = raw["orderbook_imbalance"].fillna(0.0)  # 직접 매핑
```

이후 리샘플링 및 시뮬레이션 루프 / 모델 입력은 변경 없음. `imb_notional_top5` 컬럼명이 그대로 유지되므로 `BaselineModelV1.predict()` 호환성 보장.

---

## 최종 데이터 흐름 (수정 후)

```
upbit_orderbook (Level 1 전용)
  ├─ timestamp                        → ts
  ├─ (level_1_bid + level_1_ask) / 2 → mid
  ├─ level_1_ask - level_1_bid        → spread_raw → spread_bps
  └─ orderbook_imbalance              → imb_notional_top5
```

## 상태

- [x] SQL 쿼리 Level 2~5 컬럼 제거
- [x] `orderbook_imbalance` → `imb_notional_top5` 직접 매핑
- [x] `_compute_imb_notional_top5()` 함수 삭제
- [ ] GCP DB 연결 후 실행 검증 필요
