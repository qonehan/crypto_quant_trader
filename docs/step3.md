# Step 3 결과 보고서 — Barrier Controller (변동성 기반 r_t 계산) + barrier_state DB 적재

## 1. 추가/수정한 파일 목록

| # | 파일 경로 | 설명 |
|---|----------|------|
| 1 | `app/config.py` | R_MAX 필드 추가 (기본값 0.03) |
| 2 | `app/db/models.py` | BarrierState 테이블 모델 추가 (PK: symbol+ts, 인덱스 포함) |
| 3 | `app/db/writer.py` | upsert_barrier_state() — INSERT ON CONFLICT DO UPDATE |
| 4 | `app/barrier/__init__.py` | 패키지 초기화 |
| 5 | `app/barrier/controller.py` | BarrierController — DECISION_INTERVAL_SEC 주기 tick loop, σ 계산, r_t 산출, DB upsert |
| 6 | `app/bot.py` | BarrierController import 및 asyncio task 추가 |
| 7 | `app/dashboard.py` | Barrier State 섹션 추가 (최신 1행 메트릭, r_t/sigma_h 차트, 최근 20행 테이블) |

## 2. bot 30초 로그 (Barrier 관련 요약 포함)

```
14:17:43 [INFO] __main__: DB schema ensured (market_1s, barrier_state)
14:17:43 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
14:17:44 [INFO] __main__: mid=101,523,500 spread=11,000 trade=101,530,000/0.0001/ASK imb=-0.863 T=8 Tr=6 OB=7 err=0 reconn=0
14:17:45 [INFO] app.barrier.controller: Barrier: r_t=0.001282 sigma_1s=0.00011698 sigma_h=0.00128150 status=OK sample_n=583
14:17:45 [INFO] __main__: mid=101,506,000 spread=2,000 trade=101,507,000/0.0001/ASK imb=-0.617 T=19 Tr=16 OB=16 err=0 reconn=0
14:17:46 [INFO] __main__: mid=101,490,000 spread=44,000 trade=101,468,000/0.0000/ASK imb=-0.964 T=40 Tr=45 OB=25 err=0 reconn=0
14:17:47 [INFO] __main__: mid=101,455,000 spread=14,000 trade=101,448,000/0.0070/ASK imb=-0.954 T=56 Tr=61 OB=35 err=0 reconn=0
14:17:48 [INFO] __main__: mid=101,482,500 spread=69,000 trade=101,448,000/0.0019/ASK imb=-0.984 T=58 Tr=62 OB=44 err=0 reconn=0
14:17:49 [INFO] __main__: mid=101,475,000 spread=60,000 trade=101,445,000/0.0014/ASK imb=-0.798 T=64 Tr=66 OB=54 err=0 reconn=0
14:17:50 [INFO] app.barrier.controller: Barrier: r_t=0.001303 sigma_1s=0.00011896 sigma_h=0.00130317 status=OK sample_n=588
14:17:50 [INFO] __main__: mid=101,475,000 spread=60,000 trade=101,445,000/0.0014/ASK imb=-0.881 T=64 Tr=66 OB=64 err=0 reconn=0
14:17:55 [INFO] app.barrier.controller: Barrier: r_t=0.001301 sigma_1s=0.00011878 sigma_h=0.00130113 status=OK sample_n=592
14:18:00 [INFO] app.barrier.controller: Barrier: r_t=0.001318 sigma_1s=0.00012035 sigma_h=0.00131835 status=OK sample_n=592
14:18:05 [INFO] app.barrier.controller: Barrier: r_t=0.001318 sigma_1s=0.00012035 sigma_h=0.00131836 status=OK sample_n=592
14:18:10 [INFO] app.barrier.controller: Barrier: r_t=0.001318 sigma_1s=0.00012035 sigma_h=0.00131836 status=OK sample_n=592
```

## 3. barrier_state 최근 10행 쿼리 결과

```
ts                          | symbol  | r_t        | sigma_1s     | sigma_h      | status | sample_n | h_sec | vol_window_sec
2026-02-16 14:18:30 UTC     | KRW-BTC | 0.001356   | 0.00012377   | 0.00135585   | OK     | 592      | 120   | 600
2026-02-16 14:18:25 UTC     | KRW-BTC | 0.001356   | 0.00012379   | 0.00135600   | OK     | 592      | 120   | 600
2026-02-16 14:18:20 UTC     | KRW-BTC | 0.001322   | 0.00012070   | 0.00132225   | OK     | 592      | 120   | 600
2026-02-16 14:18:15 UTC     | KRW-BTC | 0.001319   | 0.00012045   | 0.00131948   | OK     | 592      | 120   | 600
2026-02-16 14:18:10 UTC     | KRW-BTC | 0.001318   | 0.00012035   | 0.00131836   | OK     | 592      | 120   | 600
2026-02-16 14:18:05 UTC     | KRW-BTC | 0.001318   | 0.00012035   | 0.00131836   | OK     | 592      | 120   | 600
2026-02-16 14:18:00 UTC     | KRW-BTC | 0.001318   | 0.00012035   | 0.00131835   | OK     | 592      | 120   | 600
2026-02-16 14:17:55 UTC     | KRW-BTC | 0.001301   | 0.00011878   | 0.00130113   | OK     | 592      | 120   | 600
2026-02-16 14:17:50 UTC     | KRW-BTC | 0.001303   | 0.00011896   | 0.00130317   | OK     | 588      | 120   | 600
2026-02-16 14:17:45 UTC     | KRW-BTC | 0.001282   | 0.00011698   | 0.00128150   | OK     | 583      | 120   | 600
```

## 4. 검증 결과 (DoD 체크)

| 기준 | 결과 |
|------|------|
| barrier_state가 DECISION_INTERVAL_SEC(5초) 주기로 쌓임 | ✅ 5초마다 1행씩 증가 |
| status가 WARMUP → OK 전환 | ✅ 이전 market_1s 데이터가 충분하여 첫 tick부터 OK (sample_n=583 > threshold=180) |
| r_t가 NaN 없이 기록, R_MIN(0.001) 이상 유지 | ✅ r_t ≈ 0.0013, 항상 R_MIN 이상 |
| 대시보드에서 최신 r_t/sigma/lag 표시 | ✅ http://localhost:8502 에서 Barrier State 섹션 확인 가능 |

## 5. 핵심 설계 요약

- **σ̂₁ₛ 계산**: market_1s에서 최근 600초(VOL_WINDOW_SEC) mid 값 조회 → log-return → sample std
- **r_t 산출**: `r_t = max(R_MIN, K_VOL × σ₁ₛ × √H_SEC)` = max(0.001, 1.0 × σ₁ₛ × √120)
- **Warmup 판정**: sample_n < max(30, VOL_WINDOW_SEC × 0.3) = 180이면 WARMUP, r_t = R_MIN 고정
- **비동기 처리**: DB 쿼리/upsert는 `asyncio.to_thread()`로 event loop 비차단
