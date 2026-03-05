# STEP 5.1 결과 보고서 — DB 스키마 확장 + 경량 마이그레이션

## 목표
v1 라벨/정산(ask entry + bid exit + bid_high/bid_low)과 피드백 컨트롤을 위해 필요한 컬럼/테이블을 추가하고,
Alembic 없이 기존 DB를 안전하게 확장하는 "경량 마이그레이션(ALTER TABLE ... IF NOT EXISTS)"을 도입한다.

---

## 추가/수정 파일 목록

| 파일 | 변경 유형 | 설명 |
|------|-----------|------|
| `app/db/migrate.py` | **신규** | `apply_migrations(engine)` — 멱등 ALTER/CREATE 실행 |
| `app/db/models.py` | **수정** | 4개 테이블에 v1 컬럼 추가 + `BarrierParams` 모델 추가 |
| `app/bot.py` | **수정** | 시작 시 `apply_migrations()` 호출 추가 |

---

## apply_migrations 핵심 SQL 목록

### (1) market_1s — bid/ask OHLC + 기타 (11 columns)
```sql
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS bid_open_1s DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS bid_high_1s DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS bid_low_1s DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS bid_close_1s DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS ask_open_1s DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS ask_high_1s DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS ask_low_1s DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS ask_close_1s DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS spread_bps DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS imb_notional_top5 DOUBLE PRECISION;
ALTER TABLE market_1s ADD COLUMN IF NOT EXISTS mid_close_1s DOUBLE PRECISION;
```

### (2) barrier_state — 피드백/상태 추적 (6 columns)
```sql
ALTER TABLE barrier_state ADD COLUMN IF NOT EXISTS k_vol_eff DOUBLE PRECISION;
ALTER TABLE barrier_state ADD COLUMN IF NOT EXISTS none_ewma DOUBLE PRECISION;
ALTER TABLE barrier_state ADD COLUMN IF NOT EXISTS target_none DOUBLE PRECISION;
ALTER TABLE barrier_state ADD COLUMN IF NOT EXISTS ewma_alpha DOUBLE PRECISION;
ALTER TABLE barrier_state ADD COLUMN IF NOT EXISTS ewma_eta DOUBLE PRECISION;
ALTER TABLE barrier_state ADD COLUMN IF NOT EXISTS vol_dt_sec INTEGER;
```

### (3) predictions — v1 확률/EV (10 columns)
```sql
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS z_barrier DOUBLE PRECISION;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS p_hit_base DOUBLE PRECISION;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS ev_rate DOUBLE PRECISION;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS r_none_pred DOUBLE PRECISION;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS t_up_cond_pred DOUBLE PRECISION;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS t_down_cond_pred DOUBLE PRECISION;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS spread_bps DOUBLE PRECISION;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS mom_z DOUBLE PRECISION;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS imb_notional_top5 DOUBLE PRECISION;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS action_hat TEXT;
```

### (4) evaluation_results — exec_v1 라벨/확률 (11 columns)
```sql
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS label_version TEXT;
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS entry_price DOUBLE PRECISION;
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS u_exec DOUBLE PRECISION;
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS d_exec DOUBLE PRECISION;
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS ambig_touch BOOLEAN;
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS r_h DOUBLE PRECISION;
-- p_up, p_down, p_none: 이미 존재 → IF NOT EXISTS로 skip
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS p_up DOUBLE PRECISION;
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS p_down DOUBLE PRECISION;
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS p_none DOUBLE PRECISION;
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS brier DOUBLE PRECISION;
ALTER TABLE evaluation_results ADD COLUMN IF NOT EXISTS logloss DOUBLE PRECISION;
```

### (5) barrier_params 테이블 생성
```sql
CREATE TABLE IF NOT EXISTS barrier_params (
    symbol         TEXT PRIMARY KEY,
    k_vol_eff      DOUBLE PRECISION NOT NULL,
    none_ewma      DOUBLE PRECISION NOT NULL,
    target_none    DOUBLE PRECISION NOT NULL,
    ewma_alpha     DOUBLE PRECISION NOT NULL,
    ewma_eta       DOUBLE PRECISION NOT NULL,
    last_eval_t0   TIMESTAMPTZ NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 검증 커맨드 출력 전문

```
market_1s new cols: [('ask_open_1s',), ('bid_high_1s',), ('bid_low_1s',), ('imb_notional_top5',), ('spread_bps',)]
barrier_state new cols: [('ewma_alpha',), ('ewma_eta',), ('k_vol_eff',), ('none_ewma',), ('target_none',), ('vol_dt_sec',)]
predictions new cols: [('action_hat',), ('ev_rate',), ('p_hit_base',), ('r_none_pred',), ('t_down_cond_pred',), ('t_up_cond_pred',), ('z_barrier',)]
evaluation_results new cols: [('ambig_touch',), ('brier',), ('d_exec',), ('entry_price',), ('label_version',), ('logloss',), ('r_h',), ('u_exec',)]
tables: [('barrier_params',)]
OK
```

---

## Bot 실행 로그 (시작 시퀀스)

```
Boot OK
DB OK
11:16:03 [INFO] app.bot: DB schema ensured (market_1s, barrier_state, predictions, evaluation_results)
11:16:03 [INFO] app.db.migrate: Applied: market_1s bid/ask OHLC + extras (11 columns)
11:16:03 [INFO] app.db.migrate: Applied: barrier_state feedback cols (6 columns)
11:16:03 [INFO] app.db.migrate: Applied: predictions v1 cols (10 columns)
11:16:03 [INFO] app.db.migrate: Applied: evaluation_results exec_v1 cols (11 columns)
11:16:03 [INFO] app.db.migrate: Applied: barrier_params table (CREATE IF NOT EXISTS)
11:16:03 [INFO] app.db.migrate: All v1 migrations complete
11:16:03 [INFO] app.bot: DB migrations applied
```

---

## DoD 체크리스트

- [x] `apply_migrations`가 에러 없이 실행됨
- [x] 검증 출력에서 각 테이블의 신규 컬럼들이 조회됨
- [x] `barrier_params` 테이블이 존재함
- [x] bot.py에서 "DB migrations applied" 로그가 출력됨
- [x] 멱등성 확인 — 재실행 시에도 에러 없음
- [x] 기존 테이블/컬럼/PK/인덱스 변경 없음 (Backwards compatible)
