# Step 5 결과 보고서 — Evaluator: 예측 vs 실제 배리어 터치 정산

## 1. 추가/수정한 파일 목록

| # | 파일 경로 | 설명 |
|---|----------|------|
| 1 | `app/db/models.py` | EvaluationResult 테이블 모델 추가 (UNIQUE(symbol, t0), 인덱스 포함) |
| 2 | `app/db/writer.py` | upsert_evaluation_result() — INSERT ON CONFLICT DO UPDATE |
| 3 | `app/evaluator/__init__.py` | 패키지 초기화 |
| 4 | `app/evaluator/evaluator.py` | Evaluator — PENDING 예측 정산, 배리어 터치 판정, 집계 메트릭 계산 |
| 5 | `app/bot.py` | Evaluator import 및 asyncio task 추가 |
| 6 | `app/dashboard.py` | Evaluation Results 섹션 추가 (accuracy/hit_rate 메트릭, 최근 20행 테이블) |

## 2. bot 30초 로그 (Barrier + Pred + Eval 라인 포함)

```
14:41:32 [INFO] __main__: DB schema ensured (market_1s, barrier_state, predictions, evaluation_results)
14:41:32 [INFO] app.evaluator.evaluator: Evaluator: waiting 125s for first horizon to expire...
14:41:33 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
14:41:35 [INFO] app.barrier.controller: Barrier: r_t=0.001000 sigma_1s=0.00009131 sigma_h=0.00100020 status=OK sample_n=585
14:41:35 [INFO] app.predictor.runner: Pred: t0=14:41:35 r_t=0.001000 p_up=0.2961 p_down=0.1833 p_none=0.5206 t_up=108.5 t_down=131.5 slope=0.00000134 ev=-0.00178553 hat=NONE
14:41:40 [INFO] app.barrier.controller: Barrier: r_t=0.001000 sigma_1s=0.00009131 sigma_h=0.00100021 status=OK sample_n=585
14:41:40 [INFO] app.predictor.runner: Pred: t0=14:41:40 r_t=0.001000 p_up=0.0761 p_down=0.0898 p_none=0.8341 t_up=124.0 t_down=116.0 slope=-0.00000016 ev=-0.00227573 hat=NONE
...
14:43:38 [INFO] app.evaluator.evaluator: Eval: settled=50 total=50 accuracy=0.560 hit_rate=0.440 avg_error=-0.587348
14:43:43 [INFO] app.evaluator.evaluator: Eval: settled=32 total=82 accuracy=0.573 hit_rate=0.427 avg_error=-0.609895
14:43:48 [INFO] app.evaluator.evaluator: Eval: settled=1 total=83 accuracy=0.566 hit_rate=0.434 avg_error=-0.611296
14:43:53 [INFO] app.evaluator.evaluator: Eval: settled=1 total=84 accuracy=0.571 hit_rate=0.429 avg_error=-0.615589
14:43:58 [INFO] app.evaluator.evaluator: Eval: settled=1 total=85 accuracy=0.576 hit_rate=0.424 avg_error=-0.619133
14:44:03 [INFO] app.evaluator.evaluator: Eval: settled=1 total=86 accuracy=0.581 hit_rate=0.419 avg_error=-0.622701
14:44:08 [INFO] app.evaluator.evaluator: Eval: settled=1 total=87 accuracy=0.586 hit_rate=0.414 avg_error=-0.620270
14:44:13 [INFO] app.evaluator.evaluator: Eval: settled=1 total=88 accuracy=0.591 hit_rate=0.409 avg_error=-0.620222
```

## 3. evaluation_results 최근 10행 쿼리 결과

```
t0                      | symbol  | r_t      | hat  | actual | actual_r_t | touch_sec | ev         | slope_pred   | error     | status
2026-02-16 14:42:25 UTC | KRW-BTC | 0.001000 | NONE | NONE   | 0.000054   | None      | -0.001486  | -0.00000081  | -0.374272 | COMPLETED
2026-02-16 14:42:20 UTC | KRW-BTC | 0.001000 | NONE | NONE   | 0.000148   | None      | -0.001434  | -0.00000020  | -0.185812 | COMPLETED
2026-02-16 14:42:15 UTC | KRW-BTC | 0.001000 | NONE | NONE   | 0.000098   | None      | -0.001467  | -0.00000059  | -0.318064 | COMPLETED
2026-02-16 14:42:10 UTC | KRW-BTC | 0.001000 | NONE | NONE   | 0.000295   | None      | -0.001561  | -0.00000220  | -0.616010 | COMPLETED
2026-02-16 14:42:05 UTC | KRW-BTC | 0.001000 | NONE | NONE   | 0.000123   | None      | -0.001490  | -0.00000098  | -0.411245 | COMPLETED
2026-02-16 14:42:00 UTC | KRW-BTC | 0.001000 | NONE | NONE   | 0.000158   | None      | -0.001817  | -0.00000494  | -0.925949 | COMPLETED
2026-02-16 14:41:55 UTC | KRW-BTC | 0.001029 | NONE | NONE   | 0.000202   | None      | -0.001772  | -0.00000498  | -0.916829 | COMPLETED
2026-02-16 14:41:50 UTC | KRW-BTC | 0.001022 | NONE | DOWN   | 0.000600   | None      | -0.002130  | -0.00000555  | -0.971917 | COMPLETED
2026-02-16 14:41:45 UTC | KRW-BTC | 0.001008 | NONE | DOWN   | 0.001205   | 46.0s     | -0.001762  | -0.00000118  | -0.726142 | COMPLETED
2026-02-16 14:41:40 UTC | KRW-BTC | 0.001000 | NONE | DOWN   | 0.001018   | 14.0s     | -0.002276  | -0.00000016  | -0.910188 | COMPLETED
```

## 4. 검증 결과 (DoD 체크)

| 기준 | 결과 |
|------|------|
| evaluation_results가 DECISION_INTERVAL_SEC 주기로 쌓임 | ✅ 91건 정산 완료, 5초마다 1건씩 추가 |
| direction_hat과 actual_direction 비교 (accuracy) | ✅ accuracy=0.591 (59.1%) — NONE 예측이 대부분 맞음 |
| hit_rate (배리어 터치 비율) | ✅ hit_rate=0.409 (40.9%) — H_SEC=120초 내 터치 |
| error_rate | ✅ avg_error=-0.620 — 개선 여지 있음 (정상 기록) |
| predictions.status가 SETTLED로 전환 | ✅ 정산 완료된 예측은 SETTLED 상태 |
| 대시보드에서 Evaluation Results 섹션 실시간 갱신 | ✅ http://localhost:8502 에서 확인 가능 |

## 5. 핵심 설계 요약

### Evaluator 동작 흐름
1. **대기**: bot 시작 후 H_SEC+5초(=125초) 대기 → 첫 번째 horizon 만료까지 기다림
2. **정산**: DECISION_INTERVAL_SEC(5초) 주기로 `predictions` 테이블의 `status='PENDING'` AND `t0 + h_sec <= now` 인 행을 배치 조회
3. **배리어 판정**: market_1s에서 [t0, t0+h_sec] 구간의 mid 데이터를 조회
   - mid ≥ mid_t0 × (1 + r_t) → `actual_direction = "UP"`, touch_time_sec 기록
   - mid ≤ mid_t0 × (1 - r_t) → `actual_direction = "DOWN"`, touch_time_sec 기록
   - 둘 다 아님 → `actual_direction = "NONE"`
4. **오차 계산**: `error = p_predicted_actual - 1.0` (해당 방향의 예측 확률과 실제의 차이)
5. **DB 기록**: `evaluation_results`에 upsert, `predictions.status`를 `SETTLED`로 변경
6. **집계**: 최근 100건 기준 accuracy, hit_rate, avg_error 계산 → 로그 출력

### 집계 메트릭 해석
- **accuracy**: direction_hat == actual_direction 비율 (59% — baseline에서 대부분 NONE 예측)
- **hit_rate**: 배리어가 실제로 터치된 비율 (41% — 120초 내 ±0.1% 이동)
- **avg_error**: 예측 확률과 실제 결과 간 오차 평균 (개선 여지 있음)
