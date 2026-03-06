# 2_13 결과보고: 백업 파일 + 실시간 DB 하이브리드 로드 구현

## 변경 대상
`scripts/build_historical_dataset.py`

---

## 구현 내용

### 1. `--backup-dir` 인자 추가

```
parser.add_argument("--backup-dir", type=str,
                    default="data/backups",
                    help="로컬 백업 Parquet 루트 디렉터리 (기본: data/backups)")
```

- 기본값: `data/backups`
- 사용 예시: `--backup-dir /mnt/storage/backups`

---

### 2. `_load_parquet_day()` 헬퍼 함수 신설

단일 날짜 백업 Parquet을 읽어 DB 스키마에 맞게 가공하는 함수.

| 처리 항목 | 내용 |
|---|---|
| `timestamp` → `ts` | 컬럼 rename + `pd.to_datetime(utc=True)` |
| `mid` | `(level_1_bid_price + level_1_ask_price) / 2.0` |
| `orderbook_imbalance` → `imb_notional_top5` | 컬럼 rename |
| `spread_raw` | `level_1_ask_price - level_1_bid_price` |
| 유효 행 필터 | `level_1_bid_price.notna() & level_1_ask_price.notna()` |
| 기간 자르기 | `ts >= t_min AND ts <= t_max` |
| 반환 컬럼 | `["ts", "mid", "spread_raw", "imb_notional_top5"]` |

---

### 3. `load_orderbook()` 전면 수정

시그니처 변경:
```python
def load_orderbook(engine, t_min, t_max, backup_dir="data/backups") -> pd.DataFrame
```

처리 흐름:

```
t_min ~ t_max 범위의 모든 UTC 날짜 산출
        ↓
각 날짜별 {backup_dir}/{YYYY-MM-DD}/upbit_orderbook.parquet 존재 확인
        ↓
존재 시 → _load_parquet_day()로 읽기 (경고만 하고 실패 무시)
        ↓
GCP 라이브 DB 쿼리 (동일 기간, try/except로 실패 무시)
        ↓
pd.concat([parquet_frames..., db_df])
        ↓
sort_values('ts') → drop_duplicates(subset=['ts']) → reset_index
        ↓
반환
```

**중복 제거 기준**: `ts` 컬럼 단위 exact match. 백업과 DB가 같은 timestamp를 갖는 경우 먼저 concat된 Parquet 행이 유지됨 (pandas `drop_duplicates` keep='first' 기본값).

---

### 4. SQL 쿼리 수정

DB에서도 `orderbook_imbalance AS imb_notional_top5`로 alias 통일:

```sql
SELECT
    timestamp AS ts,
    (level_1_bid_price + level_1_ask_price) / 2.0 AS mid,
    level_1_ask_price - level_1_bid_price          AS spread_raw,
    orderbook_imbalance                            AS imb_notional_top5
FROM upbit_orderbook
...
```

---

### 5. `resample_1s()` 수정

기존 코드가 `orderbook_imbalance` 컬럼을 직접 참조하던 부분을 `imb_notional_top5`로 수정 (load_orderbook에서 이미 rename 완료됨):

```python
# 변경 전
raw["imb_notional_top5"] = raw["orderbook_imbalance"].fillna(0.0)

# 변경 후
raw["imb_notional_top5"] = raw["imb_notional_top5"].fillna(0.0)
```

---

## 사용 예시

```bash
# 기본 (백업 없으면 DB만 사용)
poetry run python scripts/build_historical_dataset.py --hours 72

# 백업 디렉터리 명시
poetry run python scripts/build_historical_dataset.py --hours 72 --backup-dir data/backups

# 외부 마운트된 백업 사용
poetry run python scripts/build_historical_dataset.py --hours 168 --backup-dir /mnt/gcs/backups
```

---

## 하이브리드 동작 시나리오

| 상황 | 동작 |
|---|---|
| 백업 Parquet만 있음 (DB 없음) | Parquet 데이터만 반환 |
| DB만 있음 (백업 없음) | 기존과 동일하게 DB만 반환 |
| 백업 + DB 모두 있음 | concat → ts 정렬 → 중복 제거 후 반환 |
| 백업 Parquet 읽기 실패 | WARNING 출력 후 해당 날짜 건너뜀 |
| GCP DB 쿼리 실패 | WARNING 출력 후 Parquet만 반환 |
| 둘 다 실패 | 빈 DataFrame 반환 → main()에서 ERROR 처리 |
