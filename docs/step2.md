# Step 2 결과 보고서 — 1초 리샘플러 + market_1s DB 적재 파이프라인

## 1. 추가/수정한 파일 목록

| # | 파일 경로 | 설명 |
|---|----------|------|
| 1 | `app/db/models.py` | Market1s 테이블 모델 정의 (PK: symbol+ts, 인덱스 포함) |
| 2 | `app/db/init_db.py` | ensure_schema() — Base.metadata.create_all로 테이블 자동 생성 |
| 3 | `app/db/writer.py` | upsert_market_1s() — INSERT ON CONFLICT DO UPDATE |
| 4 | `app/marketdata/resampler.py` | MarketResampler — 1초 tick loop, trade 누적, DB upsert |
| 5 | `app/bot.py` | ensure_schema 호출, resampler task 추가, consumer→resampler.on_trade 연결 |
| 6 | `app/dashboard.py` | market_1s 최근 60행 테이블, lag(초) 표시, mid 5분 라인 차트 추가 |

## 2. bot 30초 로그 (요약 발췌)

```
13:54:20 [INFO] __main__: DB schema ensured (market_1s)
13:54:21 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
13:54:21 [INFO] __main__: mid=101,435,000 spread=42,000 trade=101,456,000/0.0024/BID imb=-0.701 T=1 Tr=1 OB=4 err=0 reconn=0
13:54:26 [INFO] __main__: mid=101,437,000 spread=46,000 trade=101,414,000/0.0010/ASK imb=-0.635 T=15 Tr=9 OB=51 err=0 reconn=0
13:54:36 [INFO] __main__: mid=101,437,500 spread=7,000 trade=101,434,000/0.0002/ASK imb=+0.114 T=56 Tr=32 OB=134 err=0 reconn=0
13:54:46 [INFO] __main__: mid=101,331,500 spread=29,000 trade=101,317,000/0.0010/ASK imb=-0.266 T=164 Tr=135 OB=223 err=0 reconn=0
13:54:56 [INFO] __main__: mid=101,377,000 spread=16,000 trade=101,369,000/0.0007/ASK imb=-0.502 T=233 Tr=186 OB=307 err=0 reconn=0
13:55:20 [INFO] __main__: mid=101,371,000 spread=4,000 trade=101,369,000/0.0136/ASK imb=+0.752 T=326 Tr=243 OB=507 err=0 reconn=0
13:55:40 [INFO] __main__: mid=101,401,500 spread=65,000 trade=101,434,000/0.0203/BID imb=-0.128 T=435 Tr=322 OB=680 err=0 reconn=0
13:56:00 [INFO] __main__: mid=101,416,000 spread=88,000 trade=101,460,000/0.0025/BID imb=-0.536 T=559 Tr=580 OB=835 err=0 reconn=0
13:56:20 [INFO] __main__: mid=101,411,000 spread=78,000 trade=101,396,000/0.0032/BID imb=-0.037 T=621 Tr=619 OB=990 err=0 reconn=0
13:56:28 [INFO] __main__: mid=101,413,000 spread=74,000 trade=101,450,000/0.0001/BID imb=-0.065 T=630 Tr=621 OB=1052 err=0 reconn=0
```

## 3. market_1s 최근 10행 쿼리 결과

```
ts                          mid          bid          ask          spread  trade_cnt  trade_vol     imb_top5   last_tp       side
2026-02-16 13:56:29 UTC    101413000   101376000   101450000   74000      1          0.0014        -0.061     101450000     BID
2026-02-16 13:56:28 UTC    101413000   101376000   101450000   74000      0          0.0000        +0.452     101450000     BID
2026-02-16 13:56:27 UTC    101411500   101373000   101450000   77000      1          0.0001        +0.459     101450000     BID
2026-02-16 13:56:26 UTC    101411500   101373000   101450000   77000      0          0.0000        +0.459     101450000     BID
2026-02-16 13:56:25 UTC    101411500   101373000   101450000   77000      0          0.0000        +0.311     101450000     BID
2026-02-16 13:56:24 UTC    101411500   101373000   101450000   77000      0          0.0000        +0.379     101450000     BID
2026-02-16 13:56:23 UTC    101411000   101372000   101450000   78000      1          0.0001        +0.284     101450000     BID
2026-02-16 13:56:22 UTC    101411000   101372000   101450000   78000      0          0.0000        +0.260     101396000     BID
2026-02-16 13:56:21 UTC    101410500   101372000   101449000   77000      0          0.0000        +0.272     101396000     BID
2026-02-16 13:56:20 UTC    101421500   101396000   101447000   51000      5          0.1479        +0.397     101396000     BID
```

## 4. 2분 실행 후 count 및 last row

```
count: 129
last: (2026-02-16 13:56:29 UTC, 'KRW-BTC', mid=101413000, spread=74000, trade_count_1s=1)
```

- **129 rows in ~130 seconds = 99.2% 적재율** (거의 1초당 1행)
- PK (symbol, ts) 중복 없음
- mid/spread 값이 실시간 로그와 일치

## 5. 성공 기준 충족 여부

| 기준 | 결과 |
|------|------|
| bot 2분 실행 후 market_1s row 100~140행 | **PASS** — 129행 |
| (symbol, ts) 중복 없음 | **PASS** — PK 제약 + upsert |
| last row의 mid/spread가 실시간 로그와 일치 | **PASS** — mid=101,413,000 / spread=74,000 |
| 대시보드에서 market_1s 최근 행 표시 | **PASS** — 60행 테이블 + lag 표시 + mid 차트 구현 |

## 6. 아키텍처 요약

```
┌──────────────┐   Queue    ┌──────────┐
│UpbitWsClient │ ────────> │ Consumer │
│  (ticker/    │            │          │─── state.update_*()
│   trade/     │            │          │─── resampler.on_trade(vol)
│   orderbook) │            └──────────┘
└──────────────┘
                            ┌──────────────────┐
                            │ MarketResampler   │
                            │  1s tick loop     │
                            │  snapshot state   │──> upsert_market_1s()
                            │  reset deltas     │       ↓
                            └──────────────────┘    PostgreSQL
                                                    market_1s table

Dashboard (read-only):
  - 최근 60행 테이블
  - lag(sec) 메트릭
  - mid 5분 라인 차트
```

### 핵심 설계 포인트

- **1초 경계 정렬**: `next_ts = int(now) + 1` → microsecond=0으로 고정, 드리프트 최소화
- **trade 누적 버퍼**: `on_trade()` → `snapshot_and_reset()` (thread-safe lock)
- **항상 1행 생성**: 해당 초에 trade가 없어도 마지막 관측값 스냅샷으로 저장
- **upsert**: `INSERT ON CONFLICT DO UPDATE` — 재시작 시 중복 방지
- **비동기 DB 쓰기**: `asyncio.to_thread(upsert_market_1s)` — 이벤트루프 미차단
