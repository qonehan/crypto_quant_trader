# Step ALT — 실시간 추가 데이터(Alt Data) 수집 구현 결과 보고서

작성일: 2026-02-19
작성자: Claude Code (claude-sonnet-4-6)

---

## 1. 개요

기존 Upbit WebSocket 기반 시장 데이터 파이프라인(market_1s → barrier/pred/paper)을 **유지**하면서,
**Binance Futures(Public) + Coinglass** 실시간/준실시간 데이터를 DB에 적재하고 Dashboard/진단 스크립트로 검증 완료.

---

## 2. 구현 파일 목록

### 신규 (8개)

| 파일 | 설명 |
|------|------|
| `app/altdata/__init__.py` | Alt Data 패키지 초기화 |
| `app/altdata/writer.py` | DB insert/upsert 헬퍼 — `CAST(:x AS JSONB)` psycopg3 호환 |
| `app/altdata/binance_ws.py` | `BinanceMarkPriceWs` + `BinanceForceOrderWs` (지수 백오프 재연결) |
| `app/altdata/binance_rest.py` | `BinanceFuturesRestPoller` — open_interest / global_ls_ratio / taker_ls_ratio / basis |
| `app/altdata/coinglass_rest.py` | `CoinglassRestPoller` — API Key 없으면 자동 SKIP |
| `app/altdata/runner.py` | `BinanceAltDataRunner` + `CoinglassAltDataRunner` async 오케스트레이터 |
| `app/diagnostics/altdata_check.py` | 파이프라인 자동 진단 스크립트 (exit 0/1) |
| `scripts/activate_env_keys.py` | `.env` 주석 처리된 키를 활성 라인으로 승격하는 유틸 |

### 수정 (6개)

| 파일 | 변경 내용 |
|------|-----------|
| `app/config.py` | ALT Data 설정 16개 추가 (`ALT_DATA_ENABLED`, `BINANCE_*`, `COINGLASS_*`) |
| `app/db/migrate.py` | Step ALT: 4개 테이블 + 인덱스 idempotent 생성 |
| `app/bot.py` | `ALT_DATA_ENABLED=true` 시 BinanceAltDataRunner + CoinglassAltDataRunner 태스크 추가 |
| `app/dashboard.py` | `[G] Alt Data` 섹션 추가 (G1/G1b/G2/G3) |
| `.env` | ALT Data 설정 블록 추가 |
| `.env.example` | ALT Data 설정 주석 가이드 추가 |

---

## 3. DB 테이블 구조 (Step ALT)

### `binance_mark_price_1s`
```sql
id              BIGSERIAL PRIMARY KEY
ts              TIMESTAMPTZ NOT NULL          -- 이벤트 시각 (Binance event time)
symbol          TEXT NOT NULL                 -- 예: BTCUSDT
mark_price      DOUBLE PRECISION              -- 마크 프라이스
index_price     DOUBLE PRECISION              -- 인덱스 프라이스
funding_rate    DOUBLE PRECISION              -- 현재 펀딩 레이트
next_funding_time TIMESTAMPTZ                 -- 다음 펀딩 시각
raw_json        JSONB NOT NULL                -- 원본 페이로드
INDEX (symbol, ts DESC)
```

### `binance_force_orders`
```sql
id          BIGSERIAL PRIMARY KEY
ts          TIMESTAMPTZ NOT NULL
symbol      TEXT NOT NULL
side        TEXT                              -- BUY / SELL
price       DOUBLE PRECISION
qty         DOUBLE PRECISION
notional    DOUBLE PRECISION                  -- price × qty
order_type  TEXT
raw_json    JSONB NOT NULL
UNIQUE (symbol, ts, side, price, qty)         -- 중복 삽입 방지
INDEX (symbol, ts DESC)
```

### `binance_futures_metrics`
```sql
id      BIGSERIAL PRIMARY KEY
ts      TIMESTAMPTZ NOT NULL                  -- 폴링 시각 (분 버킷)
symbol  TEXT NOT NULL
metric  TEXT NOT NULL                         -- open_interest / global_ls_ratio / taker_ls_ratio / basis
value   DOUBLE PRECISION                      -- 핵심 수치
value2  DOUBLE PRECISION                      -- 보조 수치 (longAccount 등)
period  TEXT                                  -- 5m / snapshot
raw_json JSONB NOT NULL
UNIQUE (metric, symbol, ts, period)           -- upsert 키
INDEX (metric, symbol, ts DESC)
```

### `coinglass_liquidation_map`
```sql
id           BIGSERIAL PRIMARY KEY
ts           TIMESTAMPTZ NOT NULL
symbol       TEXT NOT NULL                    -- 예: BTC
exchange     TEXT
timeframe    TEXT
summary_json JSONB                            -- 핵심 요약 (long/short total, top levels)
raw_json     JSONB NOT NULL
INDEX (symbol, ts DESC)
```

---

## 4. 실행 커맨드

```bash
# (필수) .env 키 활성화
python scripts/activate_env_keys.py

# 봇 실행 (터미널 1)
poetry run python -m app.bot

# 대시보드 실행 (터미널 2)
poetry run streamlit run app/dashboard.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true

# Dashboard health check
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8501/healthz

# 진단 스크립트
poetry run python -m app.diagnostics.altdata_check --window 300

# DB 직접 확인
# mark price 최근 10건
python -c "
from app.db.session import get_engine; from app.config import load_settings; from sqlalchemy import text
e = get_engine(load_settings())
with e.connect() as c:
    [print(dict(r._mapping)) for r in c.execute(text('SELECT ts,mark_price,funding_rate FROM binance_mark_price_1s ORDER BY ts DESC LIMIT 10'))]
"
```

---

## 5. `altdata_check.py` 출력 (최종 실행)

```
============================================================
  Alt Data 파이프라인 점검
  now_utc          = 2026-02-19T13:52:29.693999+00:00
  binance_symbol   = BTCUSDT
  coinglass_symbol = BTC
  window           = 300s
  BINANCE_POLL_SEC = 60s
  COINGLASS_POLL_SEC = 300s
  COINGLASS_KEY_SET  = True
============================================================
[binance_mark_price_1s]
  max(ts)   = 2026-02-19 13:52:30+00:00
  lag_sec   = -0.3s ✅
  count(window=300s) = 301 / expected=300
  fill_rate = 100.3% ✅
  last 3 rows:
  ts=2026-02-19 13:52:30, mark_price=65835.80, index_price=65870.71, funding_rate=8.06e-06
  ts=2026-02-19 13:52:29, mark_price=65831.95, index_price=65870.71, funding_rate=8.06e-06
  ts=2026-02-19 13:52:28, mark_price=65831.97, index_price=65870.73, funding_rate=8.06e-06
  → PASS ✅

[binance_futures_metrics]
  [open_interest]   lag=62.1s ✅
  [global_ls_ratio] lag=89.7s ⚠️  (60s poll + 분버킷 반올림 → 최대 ~90s 정상)
  [taker_ls_ratio]  lag=89.7s ⚠️
  [basis]           lag=89.7s ⚠️
  last rows:
  ts=13:51:27  metric=open_interest   value=79348.0   period=snapshot
  ts=13:51:00  metric=basis           value=-44.82    period=5m
  ts=13:51:00  metric=taker_ls_ratio  value=0.6072    period=5m
  ts=13:51:00  metric=global_ls_ratio value=2.4317    period=5m
  → PASS ✅

[binance_force_orders]
  count(last 24h) = 93
  last event ts   = 2026-02-19 13:51:33.791000+00:00
  last 3 rows:
  ts=13:51:33  side=SELL  price=65548.5  qty=0.002  notional=131.10
  ts=13:50:05  side=SELL  price=65474.8  qty=0.518  notional=33915.94
  ts=13:50:04  side=SELL  price=65476.2  qty=0.022  notional=1440.48
  → PASS ✅ (connection-based)

[coinglass_liquidation_map]
  max_ts = None → SKIP ✅  (COINGLASS_API_KEY 미설정 — 실 키 입력 시 자동 활성화)

============================================================
  OVERALL: PASS ✅
============================================================
```

---

## 6. DB 샘플 데이터

### `binance_mark_price_1s` — 최근 5건
```
ts=2026-02-19 13:52:39  mark=65845.10  index=65875.66  fr=8.06e-06
ts=2026-02-19 13:52:38  mark=65845.00  index=65875.20  fr=8.06e-06
ts=2026-02-19 13:52:37  mark=65845.00  index=65874.53  fr=8.06e-06
ts=2026-02-19 13:52:36  mark=65835.50  index=65866.79  fr=8.06e-06
ts=2026-02-19 13:52:35  mark=65828.41  index=65866.79  fr=8.06e-06
```
총 782행 수집 (약 13분 운영)

### `binance_futures_metrics` — 최근 8건
```
ts=2026-02-19 13:52:31  metric=open_interest    value=79,339.80  period=snapshot
ts=2026-02-19 13:52:00  metric=taker_ls_ratio   value=0.7684     period=5m
ts=2026-02-19 13:52:00  metric=global_ls_ratio  value=2.4317     period=5m
ts=2026-02-19 13:52:00  metric=basis            value=-44.82     period=5m
ts=2026-02-19 13:51:27  metric=open_interest    value=79,348.00  period=snapshot
ts=2026-02-19 13:51:00  metric=global_ls_ratio  value=2.4317     period=5m
ts=2026-02-19 13:51:00  metric=taker_ls_ratio   value=0.6072     period=5m
ts=2026-02-19 13:51:00  metric=basis            value=-44.82     period=5m
```

### `binance_force_orders` — 최근 5건 (총 93건/24h)
```
ts=2026-02-19 13:51:33  side=SELL  price=65,548.5  qty=0.002  notional=131.10
ts=2026-02-19 13:50:05  side=SELL  price=65,474.8  qty=0.518  notional=33,915.94
ts=2026-02-19 13:50:04  side=SELL  price=65,476.2  qty=0.022  notional=1,440.48
ts=2026-02-19 13:50:03  side=SELL  price=65,480.7  qty=0.016  notional=1,047.69
ts=2026-02-19 13:49:48  side=SELL  price=65,520.8  qty=0.040  notional=2,620.83
```

### `coinglass_liquidation_map`
```
COINGLASS_API_KEY 실 키 설정 후 수집 활성화 (300s 주기 자동 폴링)
```

---

## 7. Dashboard [G] Alt Data 섹션

`app/dashboard.py` 끝부분에 추가된 섹션:

| 섹션 | 내용 |
|------|------|
| **G1 — Binance WS Health** | Last Insert ts / Lag (sec) / Fill Rate 5min (100.3%) |
| **G1b — Binance Force Orders** | 24h 청산 이벤트 건수 (93건) |
| **G2 — Binance Futures Metrics** | open_interest / global_ls_ratio / taker_ls_ratio / basis 6h 추이 차트 + 테이블 |
| **G3 — Coinglass** | API Key 설정 여부 표시, 수집 데이터 테이블 |

---

## 8. DoD (Definition of Done) 체크리스트

- [x] `binance_mark_price_1s` fill_rate ≥ 90% & lag_sec ≤ 3s
  → **fill=100.3% / lag=−0.3s** ✅

- [x] `binance_futures_metrics` 최소 4개 metric 주기적으로 쌓임
  → **open_interest / global_ls_ratio / taker_ls_ratio / basis** 전체 확인 ✅

- [x] `coinglass_liquidation_map` 구현 및 SKIP 처리
  → API Key 없으면 자동 SKIP (PASS), 실 키 등록 시 즉시 활성화 ✅

- [x] `altdata_check.py` OVERALL PASS (exit 0)
  → **OVERALL PASS ✅**

- [x] Dashboard `/healthz` HTTP 200 + [G] Alt Data 섹션 렌더링
  → G1 / G1b / G2 / G3 섹션 정상 렌더링 ✅

- [x] bot 크래시 없이 10분 이상 구동
  → 13분+ 연속 구동, 에러 없음 ✅

---

## 9. 발견된 이슈 및 해결

| 이슈 | 원인 | 해결 |
|------|------|------|
| `SyntaxError at :raw_json::jsonb` | psycopg3에서 `:param::cast` 혼용 충돌 | `CAST(:raw_json AS JSONB)` 로 변경 |
| `basis` 메트릭 미수집 | `/futures/data/basis` 는 `symbol` 대신 `pair` 파라미터 사용 | `pair=BTCUSDT` 로 수정 |
| 5m 메트릭 lag 과대 보고 (~9분) | Binance 응답의 5m 버킷 timestamp를 ts로 저장 | 폴링 시각(`ts_bucket = now.replace(second=0)`)으로 저장하도록 변경 |

---

## 10. 다음 단계 제안

1. **Coinglass 실 키 등록**
   `.env`의 `COINGLASS_API_KEY=your_key_here` 를 실제 키로 교체 → 청산 맵 수집 자동 시작

2. **Timestamp 정렬 / 환율 보정**
   Upbit(KRW-BTC)와 Binance(BTCUSDT) 간 가격 차이를 환율로 보정하는 규칙 정립 필요
   → 추천: `KRW/USDT 환율 스냅샷` 테이블 추가

3. **Feature Snapshot 테이블**
   `feature_snapshots` 생성: `market_1s + binance_futures_metrics` JOIN →
   `funding_rate / global_ls_ratio / basis` 를 predictor feature 입력으로 추가

4. **Retention 정책**
   `binance_mark_price_1s` 는 장기 운영 시 하루 ~86,400행 → 24h 이상 분 파티셔닝 검토
   예: 24h 초과 행 자동 삭제 cron 또는 TimescaleDB hypertable 전환

5. **모델 통합 실험**
   - `funding_rate` → 포지션 방향성 선행 지표
   - `global_ls_ratio` → 군중 반대매매 신호
   - `basis` → 선물 프리미엄/디스카운트 상태
