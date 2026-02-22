# Step ALT-2 개선 결과 보고서

**작성일**: 2026-02-22
**작업 범위**: Alt Data 파이프라인 품질 강화 — 데이터 누수 방지 · Coinglass 필수화 · Export 도구

---

## 1. 배경

Step ALT-1에서 `feature_snapshots` 파이프라인과 기본 진단 도구를 구축했다.
Step ALT-2는 **데이터 무결성(leakage 방지)**, **Coinglass 수집 의무화 체계**, **Dataset 내보내기** 세 축을 목표로 한다.

---

## 2. 구현 내용

### 2.1 데이터 누수 방지 (Feature Join Integrity)

#### 문제
기존 `_fetch_liq_aggregate()` 쿼리가 `window_sec` 파라미터를 문자열 포매팅으로 삽입해
PostgreSQL interval 계산 오류 가능성이 존재했고,
AltData 조인 시 미래값 사용 여부를 추적할 방법이 없었다.

#### 해결

**`app/predictor/runner.py`**
```python
# Before: interval '{window_sec} seconds' — 파라미터 바인딩 불가
# After: 고정 'interval 5 minutes' + 명시적 경계
text("""
    SELECT
        COALESCE(SUM(notional), 0.0) AS liq_5m_notional,
        COALESCE(COUNT(*), 0)        AS liq_5m_count,
        MAX(ts)                      AS liq_last_ts
    FROM binance_force_orders
    WHERE symbol = :sym
      AND ts >  (:t0 - interval '5 minutes')   -- 하한 exclusive
      AND ts <= :t0                              -- 상한 inclusive (미래 데이터 차단)
""")
```

`_fetch_binance_mark_near()` → `ts AS mark_ts` 반환
`_fetch_binance_metrics_near()` → `ts_dict` 딕셔너리로 per-metric timestamp 반환

#### Source Timestamp 컬럼 추가

`feature_snapshots` 테이블에 3개 컬럼 추가 (`ALTER TABLE ADD COLUMN IF NOT EXISTS`):

| 컬럼 | 설명 |
|------|------|
| `bin_mark_ts` | Binance mark price 데이터의 실제 ts |
| `oi_ts` | Binance open_interest 데이터의 실제 ts |
| `liq_last_ts` | 최후 청산 이벤트의 ts (해당 window에 청산 없으면 NULL) |

**검증 원칙**: 세 컬럼 모두 반드시 `<= snapshot ts` 이어야 함.
위반 시 `feature_leak_check` FAIL.

---

### 2.2 Coinglass 의무화 체계

#### 변경 사항

**`app/config.py`**
```python
COINGLASS_ENABLED: bool = False   # 명시적 opt-in
```

**`app/altdata/coinglass_rest.py`**
- `_poll_liq_history()` → `(ok: bool, http_status, error_msg)` 반환
- `run()` → 각 poll 후 `insert_coinglass_call_status()` 호출
- `COINGLASS_ENABLED=True` + 잘못된 키 → `log.error()` (기존 warning 수준 → error 승격)

**`app/altdata/writer.py`** — `insert_coinglass_call_status()` 신설

**`coinglass_call_status` 테이블 신설** (DB migration):
```sql
CREATE TABLE IF NOT EXISTS coinglass_call_status (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok          BOOLEAN     NOT NULL,
    http_status INT,
    error_msg   TEXT,
    latency_ms  INT,
    poll_count  BIGINT
);
```

#### 판정 로직

| `COINGLASS_ENABLED` | API 키 상태 | 결과 |
|---|---|---|
| False | 무관 | SKIP PASS |
| True | 미설정/플레이스홀더 | **FAIL** |
| True | 유효 (길이≥12) | 실제 데이터 lag·count 체크 |

---

### 2.3 신규 진단 도구

#### `app/diagnostics/feature_leak_check.py`

source_ts가 snapshot_ts를 초과하는 row를 탐색 (미래값 사용 = 데이터 누수).

```
[feature_leak_check — source_ts <= snapshot_ts (window=600s)]
  bin_mark_ts > ts violations: 0
  oi_ts > ts violations: 0
  liq_last_ts > ts violations: 0
  ✅ 미래값 사용 위반 없음
  → PASS ✅

[feature_leak_check — source_ts coverage (window=600s)]
  total rows (window): 120
  bin_mark_ts coverage: 120/120 (100.0%)
  oi_ts coverage:       120/120 (100.0%)
  → PASS ✅ (coverage 체크는 정보성)
```

#### `app/diagnostics/coinglass_check.py`

Coinglass 수집 상태를 단독 점검. `COINGLASS_ENABLED=False`이면 SKIP PASS.

```
[coinglass_collection]
  COINGLASS_ENABLED=False → SKIP ✅

[coinglass_call_status]
  no call records yet
  → SKIP

OVERALL: PASS ✅
※ COINGLASS_ENABLED=False → SKIP PASS (수집 비활성 상태)
```

---

### 2.4 Dataset Export (`app/features/export_dataset.py`)

학습·백테스트용 Parquet/CSV 내보내기 도구.

**특징**:
- **누수 방지**: 피처는 `ts <= end` DB 쿼리로 추출, 라벨만 `t0 + horizon` 미래값 사용
- **라벨 생성**: `pandas.merge_asof(direction='nearest', tolerance=5s)` — 미래 mid_krw 매칭
- **라벨 타입**: `direction` (UP/DOWN/NONE vs ±r_t), `binary` (>0→1), `continuous` (float)
- **CLI**:
  ```bash
  poetry run python -m app.features.export_dataset \
    --symbol KRW-BTC \
    --start "2026-02-22T05:50:00Z" \
    --end   "2026-02-22T06:00:00Z" \
    --horizon-sec 120 \
    --out ./data/datasets/krw_btc_test.parquet \
    --format parquet \
    --label-type direction
  ```

---

### 2.5 Dashboard 개선

**G3 (Coinglass 패널)**:
- `COINGLASS_ENABLED=False` → `st.info()` 배너
- `COINGLASS_ENABLED=True` + 키 무효 → `st.error()` 배너
- `coinglass_call_status` 메트릭: 최근 ok/ts/latency, 24h 성공률, 에러 메시지

**G4 (Feature 품질)**:
- source_ts 누수 위반 건수 표시 (`st.error()` if violations > 0)
- bin_mark_age_sec, oi_age_sec 평균 표시 (데이터 신선도)

---

## 3. DB 마이그레이션

```sql
-- feature_snapshots source_ts 컬럼
ALTER TABLE feature_snapshots ADD COLUMN IF NOT EXISTS bin_mark_ts TIMESTAMPTZ;
ALTER TABLE feature_snapshots ADD COLUMN IF NOT EXISTS oi_ts TIMESTAMPTZ;
ALTER TABLE feature_snapshots ADD COLUMN IF NOT EXISTS liq_last_ts TIMESTAMPTZ;

-- coinglass_call_status 테이블
CREATE TABLE IF NOT EXISTS coinglass_call_status (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok BOOLEAN NOT NULL,
    http_status INT, error_msg TEXT, latency_ms INT, poll_count BIGINT
);
```

마이그레이션 성공 확인:
```
 Column Name   | Data Type
 bin_mark_ts   | timestamp with time zone
 oi_ts         | timestamp with time zone
 liq_last_ts   | timestamp with time zone

 Table: coinglass_call_status — created ✅
```

---

## 4. 완료 판정 (DoD)

| 항목 | 결과 |
|------|------|
| `altdata_check --window 600` | **OVERALL PASS ✅** |
| `feature_check --window 600` | **OVERALL PASS ✅** |
| `feature_leak_check --window 600` | **OVERALL PASS ✅** |
| `coinglass_check --window 600` | **OVERALL PASS ✅** |
| `export_dataset` 1회 성공 + 파일 생성 | **DONE ✅** |

### 진단 출력 요약

#### altdata_check --window 600
```
[binance_mark_price_1s]  fill_rate=100.0%  lag=0.2s   → PASS ✅
[binance_futures_metrics]                  lag≤107s    → PASS ✅
[binance_force_orders]   count_24h=4                   → PASS ✅
[coinglass_liquidation_map] COINGLASS_ENABLED=False    → SKIP ✅
OVERALL: PASS ✅
```

#### feature_check --window 600
```
[lag]        lag=3.4s                 → PASS ✅
[fill_rate]  120/120=100.0%           → PASS ✅
[null_rates] 7 columns 모두 0.0%      → PASS ✅
[liq_not_null] liq_5m_notional 0.0%   → PASS ✅
OVERALL: PASS ✅
```

#### feature_leak_check --window 600
```
bin_mark_ts > ts violations: 0
oi_ts > ts violations: 0
liq_last_ts > ts violations: 0
coverage: bin_mark_ts 100.0% / oi_ts 100.0%
OVERALL: PASS ✅
```

#### coinglass_check --window 600
```
COINGLASS_ENABLED=False → SKIP ✅
OVERALL: PASS ✅
```

#### export_dataset 실행 결과
```
symbol      = KRW-BTC
start       = 2026-02-22T05:50:00+00:00
end         = 2026-02-22T06:00:00+00:00
horizon_sec = 120s
label_type  = direction
format      = parquet

Loaded 121 feature rows
Loaded 132 rows for label matching
Label created: 109 rows (12 dropped — no future match)
Label distribution: {'NONE': 109}   ← 시장 횡보 (|r| < r_t ≈ 0.18%)
Written: 109 rows, 35 cols, 31.7 KB
DONE ✅
```

> 라벨 분포가 NONE 100%인 이유: 해당 10분 구간 BTC가 ±0.18%(r_t) 미만의 소폭 등락.
> 장기 학습 데이터 수집 후 UP/DOWN 분포 확인 예정.

---

## 5. 신규/변경 파일 목록

| 파일 | 변경 유형 |
|------|---------|
| `app/config.py` | 수정 — `COINGLASS_ENABLED` 추가 |
| `app/db/migrate.py` | 수정 — source_ts 컬럼 + coinglass_call_status 마이그레이션 |
| `app/altdata/writer.py` | 수정 — `insert_coinglass_call_status()` |
| `app/altdata/coinglass_rest.py` | 수정 — status tuple 반환, call_status 기록 |
| `app/features/writer.py` | 수정 — bin_mark_ts, oi_ts, liq_last_ts upsert |
| `app/predictor/runner.py` | 수정 — source_ts 반환, liq_5m SQL 수정 |
| `app/diagnostics/altdata_check.py` | 수정 — COINGLASS_ENABLED 체크 |
| `app/diagnostics/feature_leak_check.py` | **신규** |
| `app/diagnostics/coinglass_check.py` | **신규** |
| `app/features/export_dataset.py` | **신규** |
| `app/dashboard.py` | 수정 — G3 Coinglass 패널 + G4 source_ts 품질 |

---

## 6. 향후 과제

1. **Coinglass 실 키 등록**: `.env`에 `COINGLASS_API_KEY=<real_key>` + `COINGLASS_ENABLED=True` 설정 후 `coinglass_check` 재점검
2. **장기 데이터 수집**: 최소 24h 축적 후 `export_dataset`으로 UP/DOWN 라벨 분포 확인
3. **prune_altdata 스케줄링**: cron 또는 systemd timer로 `--days 7` 자동 실행
4. **feature_leak_check CI 통합**: Git push 시 자동 실행하여 누수 방지 상시화
