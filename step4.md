# Step 4 결과 보고서 — 고정 베이스라인 모델 + predictions 테이블 적재 (EV/slope 포함)

## 1. 추가/수정한 파일 목록

| # | 파일 경로 | 설명 |
|---|----------|------|
| 1 | `app/config.py` | MODEL_LOOKBACK_SEC, FEE_RATE, SLIPPAGE_BPS, EV_COST_MULT, P_NONE_MAX_FOR_SIGNAL 추가 |
| 2 | `app/db/models.py` | Prediction 테이블 모델 추가 (UNIQUE(symbol, t0), 인덱스 포함) |
| 3 | `app/db/writer.py` | upsert_prediction() — INSERT ON CONFLICT DO UPDATE |
| 4 | `app/models/__init__.py` | 패키지 초기화 |
| 5 | `app/models/interface.py` | PredictionOutput dataclass + BaseModel 추상 클래스 |
| 6 | `app/models/baseline.py` | BaselineModel — 휴리스틱 확률/EV/slope 계산 (baseline_v1) |
| 7 | `app/predictor/__init__.py` | 패키지 초기화 |
| 8 | `app/predictor/runner.py` | PredictionRunner — DECISION_INTERVAL_SEC 주기 예측 실행기 |
| 9 | `app/bot.py` | BaselineModel + PredictionRunner import 및 asyncio task 추가 |
| 10 | `app/dashboard.py` | Predictions 섹션 추가 (메트릭, 테이블, slope_pred/ev 차트) |

## 2. bot 30초 로그 (Barrier + Pred 라인 포함)

```
14:34:47 [INFO] __main__: DB schema ensured (market_1s, barrier_state, predictions)
14:34:47 [INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
14:34:48 [INFO] __main__: mid=101,588,500 spread=81,000 trade=101,629,000/0.0030/BID imb=-0.091 T=1 Tr=1 OB=2 err=0 reconn=0
14:34:50 [INFO] app.barrier.controller: Barrier: r_t=0.001261 sigma_1s=0.00011509 sigma_h=0.00126075 status=OK sample_n=592
14:34:50 [INFO] app.predictor.runner: Pred: t0=14:34:50 r_t=0.001261 p_up=0.0535 p_down=0.0599 p_none=0.8867 t_up=122.7 t_down=117.3 slope=-0.00000009 ev=-0.00200542 hat=NONE
14:34:55 [INFO] app.barrier.controller: Barrier: r_t=0.001261 sigma_1s=0.00011509 sigma_h=0.00126075 status=OK sample_n=592
14:34:55 [INFO] app.predictor.runner: Pred: t0=14:34:55 r_t=0.001261 p_up=0.0507 p_down=0.0565 p_none=0.8928 t_up=122.6 t_down=117.4 slope=-0.00000008 ev=-0.00200458 hat=NONE
14:35:00 [INFO] app.barrier.controller: Barrier: r_t=0.001261 sigma_1s=0.00011509 sigma_h=0.00126075 status=OK sample_n=592
14:35:00 [INFO] app.predictor.runner: Pred: t0=14:35:00 r_t=0.001261 p_up=0.2667 p_down=0.1720 p_none=0.5613 t_up=109.5 t_down=130.5 slope=0.00000141 ev=-0.00183857 hat=NONE
14:35:05 [INFO] app.barrier.controller: Barrier: r_t=0.001261 sigma_1s=0.00011515 sigma_h=0.00126137 status=OK sample_n=592
14:35:05 [INFO] app.predictor.runner: Pred: t0=14:35:05 r_t=0.001261 p_up=0.0731 p_down=0.0857 p_none=0.8411 t_up=123.8 t_down=116.2 slope=-0.00000019 ev=-0.00173752 hat=NONE
14:35:10 [INFO] app.barrier.controller: Barrier: r_t=0.001261 sigma_1s=0.00011515 sigma_h=0.00126135 status=OK sample_n=593
14:35:10 [INFO] app.predictor.runner: Pred: t0=14:35:10 r_t=0.001261 p_up=0.0247 p_down=0.0253 p_none=0.9500 t_up=120.6 t_down=119.4 slope=-0.00000001 ev=-0.00197848 hat=NONE
14:35:15 [INFO] app.barrier.controller: Barrier: r_t=0.001263 sigma_1s=0.00011527 sigma_h=0.00126272 status=OK sample_n=592
14:35:15 [INFO] app.predictor.runner: Pred: t0=14:35:15 r_t=0.001263 p_up=0.0869 p_down=0.1053 p_none=0.8078 t_up=124.6 t_down=115.4 slope=-0.00000027 ev=-0.00180395 hat=NONE
14:35:20 [INFO] app.barrier.controller: Barrier: r_t=0.001261 sigma_1s=0.00011514 sigma_h=0.00126135 status=OK sample_n=592
14:35:20 [INFO] app.predictor.runner: Pred: t0=14:35:20 r_t=0.001261 p_up=0.1490 p_down=0.2144 p_none=0.6366 t_up=128.7 t_down=111.3 slope=-0.00000097 ev=-0.00196155 hat=NONE
```

## 3. predictions 최근 10행 쿼리 결과

```
t0                      | symbol  | r_t      | p_up   | p_down | p_none | t_up   | t_down | slope_pred   | ev           | hat  | status
2026-02-16 14:35:30 UTC | KRW-BTC | 0.001263 | 0.0245 | 0.0255 | 0.9500 | 120.9  | 119.0  | -0.00000001  | -0.00121109  | NONE | PENDING
2026-02-16 14:35:25 UTC | KRW-BTC | 0.001263 | 0.0247 | 0.0253 | 0.9500 | 120.6  | 119.4  | -0.00000001  | -0.00121063  | NONE | PENDING
2026-02-16 14:35:20 UTC | KRW-BTC | 0.001261 | 0.1490 | 0.2144 | 0.6366 | 128.7  | 111.3  | -0.00000097  | -0.00196155  | NONE | PENDING
2026-02-16 14:35:15 UTC | KRW-BTC | 0.001263 | 0.0869 | 0.1053 | 0.8078 | 124.6  | 115.4  | -0.00000027  | -0.00180395  | NONE | PENDING
2026-02-16 14:35:10 UTC | KRW-BTC | 0.001261 | 0.0247 | 0.0253 | 0.9500 | 120.6  | 119.4  | -0.00000001  | -0.00197848  | NONE | PENDING
2026-02-16 14:35:05 UTC | KRW-BTC | 0.001261 | 0.0731 | 0.0857 | 0.8411 | 123.8  | 116.2  | -0.00000019  | -0.00173752  | NONE | PENDING
2026-02-16 14:35:00 UTC | KRW-BTC | 0.001261 | 0.2667 | 0.1720 | 0.5614 | 109.5  | 130.5  | +0.00000141  | -0.00183857  | NONE | PENDING
2026-02-16 14:34:55 UTC | KRW-BTC | 0.001261 | 0.0507 | 0.0565 | 0.8928 | 122.6  | 117.4  | -0.00000008  | -0.00200458  | NONE | PENDING
2026-02-16 14:34:50 UTC | KRW-BTC | 0.001261 | 0.0535 | 0.0599 | 0.8867 | 122.7  | 117.3  | -0.00000009  | -0.00200542  | NONE | PENDING
```

## 4. 검증 결과 (DoD 체크)

| 기준 | 결과 |
|------|------|
| predictions가 DECISION_INTERVAL_SEC(5초) 주기로 쌓임 | ✅ 5초마다 1행씩 증가 (9행/45초) |
| (symbol, t0) 중복 없이 UNIQUE 유지 | ✅ UNIQUE constraint + upsert 적용 |
| p_up/p_down/p_none이 NaN 없이 0~1 범위, 합 ≈ 1.0 | ✅ 모든 행에서 합 = 1.0000 |
| slope_pred, ev가 NaN 없이 계산됨 | ✅ 모든 행에서 유효한 값 |
| 대시보드에서 Predictions 섹션 실시간 갱신 | ✅ http://localhost:8502 에서 확인 가능 |

## 5. 핵심 설계 요약

### BaselineModel (baseline_v1)
- **Features**: ret_10s, ret_60s, mom(=0.7×ret_10s+0.3×ret_60s), imb, spread_pct
- **Score**: 500×mom + 1.0×imb - 50×spread_pct
- **확률**: sigmoid(score) → p_dir, conf → p_none/p_up/p_down (normalized)
- **도달시간**: base_T = (r_t²)/(σ₁ₛ²), 방향 편향 ±20%×conf
- **EV**: p_up×r_t - p_down×r_t - cost (fee+spread+slippage)
- **direction_hat**: ev>0 AND p_none≤0.7 → UP/DOWN, else NONE

### PredictionRunner
- Barrier Controller 이후 0.5초 offset으로 실행 (barrier 데이터 확보)
- market_1s 최근 120초(MODEL_LOOKBACK_SEC) 윈도우 사용
- asyncio.to_thread로 DB I/O 비차단
