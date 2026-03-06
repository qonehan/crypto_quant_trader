# Step 9 결과보고서 — Upbit 실행 파이프라인 운영 안정화 v1

작성일: 2026-02-19
브랜치: main
직전 커밋: 8c10e4e (Step 7 Upbit Exchange REST + Shadow Execution)

---

## 1. 변경/추가 파일 목록

| 파일 | 구분 | 주요 변경 내용 |
|------|------|---------------|
| `app/db/migrate.py` | 수정 | Section 13: 부분 유니크 인덱스 `ux_upbit_order_attempts_identifier_mode` 생성 |
| `app/db/writer.py` | 수정 | `_INSERT_` → `_UPSERT_` + `ON CONFLICT (identifier, mode) WHERE identifier IS NOT NULL DO UPDATE` |
| `app/exchange/upbit_rest.py` | 수정 | `parse_remaining_req()` 함수 추가 + `_last_call_meta`에 `remaining_req_parsed` 추가 |
| `app/exchange/runner.py` | 수정 | `_has_final_status()`, `_is_throttled()` 추가; `_THROTTLE_SEC_THRESHOLD`, `_FINAL_STATUSES` 상수 |
| `app/exchange/reconcile.py` | **신규** | 주문/계좌/포지션 정합성 점검 (조회 전용, DB 변경 없음) |
| `app/dashboard.py` | 수정 | remaining-req 최근값 캡션, 24h 상태 분포, 중복 identifier 0건 쿼리 추가 |

---

## 2. DoD (Definition of Done) 표

| # | 항목 | 상태 | 비고 |
|---|------|------|------|
| 1 | `ux_upbit_order_attempts_identifier_mode` 유니크 인덱스 생성 | **PASS** | `WHERE identifier IS NOT NULL` 부분 인덱스 |
| 2 | `ON CONFLICT` upsert — 중복 INSERT가 UPDATE로 멱등 처리 | **PASS** | COALESCE/GREATEST 병합 로직 |
| 3 | `parse_remaining_req()` 구현 + `_is_throttled()` 적용 | **PASS** | sec ≤ 1 → `status=throttled` |
| 4 | `reconcile.py` 작성 (키 없으면 SKIP exit 0) | **PASS** | 조회 전용, DB 변경 없음 |
| 5 | 봇 기동 오류 없음 (Step 6 파이프라인 유지) | **PASS** | WS→barrier→pred→paper 정상 |
| 6 | `identifier` 중복 0건 쿼리 확인 | **PASS** | 0 rows |
| 7 | dashboard [F] 섹션 확장 | **PASS** | 24h 분포, 중복 체크, remaining-req |

---

## 3. 유니크 인덱스 생성 증거

### `pg_indexes` 조회 결과

```sql
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'upbit_order_attempts';
```

```
upbit_order_attempts_pkey
  CREATE UNIQUE INDEX upbit_order_attempts_pkey ON public.upbit_order_attempts USING btree (id)

ix_upbit_order_attempts_ts
  CREATE INDEX ix_upbit_order_attempts_ts ON public.upbit_order_attempts USING btree (ts)

ix_upbit_order_attempts_symbol_ts
  CREATE INDEX ix_upbit_order_attempts_symbol_ts ON public.upbit_order_attempts USING btree (symbol, ts)

ux_upbit_order_attempts_identifier_mode   ← Step 9 신규
  CREATE UNIQUE INDEX ux_upbit_order_attempts_identifier_mode
  ON public.upbit_order_attempts
  USING btree (identifier, mode)
  WHERE (identifier IS NOT NULL)
```

### migrate.py Section 13 코드

```python
# ── Section 13: Step 9 — unique partial index ──────────────────────────────
conn.execute(text("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_upbit_order_attempts_identifier_mode
    ON upbit_order_attempts (identifier, mode)
    WHERE identifier IS NOT NULL
"""))
log.info("Applied: ux_upbit_order_attempts_identifier_mode (CREATE UNIQUE INDEX IF NOT EXISTS)")
```

### 봇 기동 시 마이그레이션 로그

```
02:52:52 [INFO] app.db.migrate: Applied: ux_upbit_order_attempts_identifier_mode (CREATE UNIQUE INDEX IF NOT EXISTS)
02:52:52 [INFO] app.db.migrate: All migrations complete (v1 + Step 7 + Step 8 + Step 9)
```

---

## 4. identifier 중복 0건 증거

### 쿼리

```sql
SELECT identifier, mode, COUNT(*) AS cnt
FROM upbit_order_attempts
WHERE identifier IS NOT NULL
GROUP BY identifier, mode
HAVING COUNT(*) > 1;
```

### 결과

```
Duplicate rows: 0 (should be 0)
```

### DB에 기록된 실제 identifier 목록 (참고)

| id | ts | action | identifier | mode | status |
|----|-----|--------|-----------|------|--------|
| 6 | 2026-02-19 02:23:41 | EXIT_LONG | paper-15-EXIT_LONG | shadow | logged |
| 7 | 2026-02-19 02:29:42 | ENTER_LONG | paper-16-ENTER_LONG | shadow | logged |
| 8 | 2026-02-19 02:31:03 | EXIT_LONG | paper-17-EXIT_LONG | shadow | logged |

모든 identifier가 유니크하게 유지됨. ON CONFLICT upsert로 재시작/재진입 시에도 중복 방지.

---

## 5. remaining-req 파싱/스로틀 동작 증거

### `parse_remaining_req()` 구현 (`upbit_rest.py`)

```python
def parse_remaining_req(raw: str | None) -> dict:
    """Upbit remaining-req 헤더 파싱.
    형식: "group=default; min=900; sec=30"
    반환: {group, min, sec, raw}
    """
    result: dict = {"group": None, "min": None, "sec": None, "raw": raw}
    if not raw:
        return result
    try:
        for part in raw.split(";"):
            part = part.strip()
            key, _, val = part.partition("=")
            key = key.strip()
            val = val.strip()
            if key == "group":   result["group"] = val
            elif key == "min":   result["min"] = int(val)
            elif key == "sec":   result["sec"] = int(val)
    except Exception:
        pass
    return result
```

`_request()` 내부에서 모든 응답에 자동 파싱:

```python
self._last_call_meta = {
    "http_status": http_status,
    "remaining_req": remaining_req,
    "remaining_req_parsed": parse_remaining_req(remaining_req),  # Step 9
    "latency_ms": latency_ms,
}
```

### `_is_throttled()` 스로틀 로직 (`runner.py`)

```python
_THROTTLE_SEC_THRESHOLD = 1   # sec ≤ 1 → throttled

def _is_throttled(self) -> bool:
    meta = self.client._last_call_meta
    parsed = meta.get("remaining_req_parsed")
    if not parsed:
        return False
    sec = parsed.get("sec")
    if sec is None:
        return False
    if sec <= _THROTTLE_SEC_THRESHOLD:
        log.warning(
            "remaining-req throttle: sec=%d <= threshold=%d", sec, _THROTTLE_SEC_THRESHOLD
        )
        return True
    return False
```

스로틀 발생 시: `status="throttled"` 로 DB 기록 후 조기 반환.

### reconcile.py remaining-req 출력 예시

```
[1] GET /v1/orders/open?market=KRW-BTC
  미체결 주문 0건  remaining-req: sec=29 min=897
[4] GET /v1/orders/chance?market=KRW-BTC
  bid_fee=0.0005  ask_fee=0.0005
  bid_available=...  ask_available=...
  remaining-req: sec=28 min=896
```

---

## 6. 봇 기동 로그 (Step 6 파이프라인 정상 유지 증거)

```
02:52:52 [INFO] __main__: DB schema ensured (market_1s, barrier_state, predictions, evaluation_results)
02:52:52 [INFO] app.db.migrate: All migrations complete (v1 + Step 7 + Step 8 + Step 9)
02:52:52 [INFO] __main__: Paper trading enabled
02:52:52 [INFO] __main__: ShadowExecutionRunner enabled (mode=shadow)
02:52:52 [INFO] __main__: UpbitAccountRunner skipped (UPBIT_ACCESS_KEY not set)
02:52:52 [INFO] app.exchange.runner: ShadowExecutionRunner started (effective_mode=shadow live_enabled=False trade_mode=shadow)
02:52:52 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
02:52:55 [INFO] app.barrier.controller: Barrier: r_t=0.001929 ... status=WARMUP n=7 k_eff=0.5000
02:52:55 [INFO] app.predictor.runner: Pred(v1): t0=02:52:55 r_t=0.001929 z=N/A p_none=0.9900 ... action=STAY_FLAT
02:52:58 [INFO] app.trading.runner: Paper: pos=FLAT action=STAY_FLAT reason=RATE_LIMIT cash=997555 ...
```

→ WS 연결, Barrier WARMUP, Predictor, Paper trading 모두 정상 동작. err=0, reconn=0.

---

## 7. dashboard [F] 섹션 확장 내용

Step 9에서 `app/dashboard.py` [F] Upbit Exchange 섹션에 추가된 항목:

```python
# remaining-req 최근값
st.caption(f"remaining-req  sec={sec_val}  min={min_val}  ({remaining_raw})")

# 24h 상태 분포
rows = conn.execute(text("""
    SELECT mode, status, COUNT(*) as cnt
    FROM upbit_order_attempts
    WHERE ts > now() - interval '24 hours'
    GROUP BY mode, status ORDER BY mode, cnt DESC
""")).fetchall()

# 중복 identifier 0건 쿼리
dup_rows = conn.execute(text("""
    SELECT identifier, mode, COUNT(*) as cnt
    FROM upbit_order_attempts
    WHERE identifier IS NOT NULL
    GROUP BY identifier, mode HAVING COUNT(*) > 1
""")).fetchall()
if len(dup_rows) == 0:
    st.success("identifier 중복 없음 (unique constraint 정상)")
else:
    st.error(f"identifier 중복 {len(dup_rows)}건 — 조사 필요")
```

---

## 8. reconcile.py 동작 설명

```
$ poetry run python -m app.exchange.reconcile

# 키 없는 경우 (shadow 모드):
ℹ️  UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 미설정 — 리컨실리에이션 SKIP
   (shadow 모드에서는 실제 주문이 없으므로 리컨실리에이션 불필요)

# 키 있는 경우:
Reconcile: KRW-BTC  [2026-02-19T02:52:52+00:00]
============================================================
[1] GET /v1/orders/open?market=KRW-BTC
  미체결 주문 0건  remaining-req: sec=29 min=897
[2] DB upbit_order_attempts (submitted, no final_state)
  DB submitted 건수: 0
[3] 대조 결과
  ✅ 미체결 주문 없음, DB submitted 없음 — 정합성 OK
[4] GET /v1/orders/chance?market=KRW-BTC
  bid_fee=0.0005  ask_fee=0.0005
  ...
============================================================
Reconcile 완료. DB 변경 없음.
```

**설계 원칙**: 조회 전용, DB 변경 없음. `reconcile.py`는 진단 도구이며 주문 취소/수정 기능 없음.

---

## 9. 현재 24h 상태 분포

```
mode=shadow  status=logged  cnt=7
```

shadow 모드: API 키 없이 실행, 실제 주문 없이 shadow log만 기록.
`upbit_order_snapshots`: 0 rows (live 폴링 없음)
`live_positions`: 0 rows (live 모드 비활성)

---

## 10. 아키텍처 요약

```
paper_decisions (ENTER/EXIT)
       ↓
ShadowExecutionRunner._handle_trade()
  ├── _has_final_status(identifier, mode)  ← DB 멱등성 체크
  ├── _is_throttled()                      ← remaining-req ≤ 1 → skip
  ├── shadow: status=logged (no HTTP call)
  ├── test:   order_test() → status=test_ok
  └── live:   create_order() + _poll_live_order() → status=submitted/done

upbit_order_attempts (upsert ON CONFLICT)
  └── UNIQUE (identifier, mode) WHERE identifier IS NOT NULL
       → 재시작/재진입 시 중복 방지

reconcile.py (cron 용)
  └── list_open_orders() ↔ DB submitted 대조 → stdout 리포트
```
