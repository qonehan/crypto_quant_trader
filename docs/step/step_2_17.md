step_2_17 결과 보고

  변경된 파일 목록

  ┌───────────────────────────┬──────────────────────────────────────────────────────────────────────────────────────────────────────────┐
  │           파일            │                                                변경 내용                                                 │
  ├───────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ app/config.py             │ H_SEC=3600, MODEL_LOOKBACK_SEC=3600, PREDICTOR_TYPE="ridge", RIDGE_MODEL_PATH 설정, RIDGE_GAMMA=1.5 추가 │
  ├───────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ .env                      │ H_SEC=3600, MODEL_LOOKBACK_SEC=3600, RIDGE_MODEL_PATH, RIDGE_GAMMA=1.5 추가                              │
  ├───────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ app/predictor/ml_model.py │ MODEL_VERSION → ridge_h3600_v1, feat_map 11→19개, gamma 기반 action_hat 로직                             │
  ├───────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ app/predictor/runner.py   │ GCP DB 엔진 초기화 + fetch_alt_row() + _run_tick에서 alt 피처 병합                                       │
  ├───────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ app/trading/policy.py     │ MODEL_FLAT 플래그 추가 (ridge action_hat != ENTER_LONG 시 진입 차단)                                     │
  ├───────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ app/dashboard.py          │ altair 기반 진입 마커 차트, Net PnL 섹션, 모델 버전 표기                                                 │
  └───────────────────────────┴──────────────────────────────────────────────────────────────────────────────────────────────────────────┘

  ---
  1. 예측 엔진 변경 상세

  RidgePredictor.MODEL_VERSION → "ridge_h3600_v1"

  19-피처 feat_map (app/predictor/ml_model.py:118):
  - 기존 11개 → 19개 (학습 feature_cols.json과 완전 정렬)
  - sigma_1s, sigma_h → barrier_row에서 직접 추출
  - buy_volume_ratio, funding_rate, long_short_ratio, open_interest, dxy_index, fear_greed_index → PredictionRunner.fetch_alt_row()가 GCP에서 조회 후 barrier_row에 주입

  gamma=1.5 진입 문턱 (app/predictor/ml_model.py:150):
  threshold = gamma * (2×FEE_RATE + 2×SLIPPAGE_BPS/10000)
            = 1.5 × (0.001 + 0.0004) = 0.0021
  # ridge_return > 0.0021  → ENTER_LONG
  # ridge_return < -0.0021 → ENTER_SHORT
  # 그 외               → STAY_FLAT

  ---
  2. Paper Trading 연동

  policy.py에 MODEL_FLAT 게이트 추가:
  - action_hat != "ENTER_LONG" 이면 진입 차단 → reason_flags에 "MODEL_FLAT" 기록
  - PnL 계산: pnl_krw = proceeds - exit_fee - entry_cost (기존과 동일, 수수료 이미 차감됨)

  ---
  3. 대시보드 주요 변경

  - 상단 캡션: 모델: ridge_h3600_v1 (1시간 호흡 Ridge, sign_acc 82%, γ=1.5) 표시
  - 가격 차트 (Tab 2): st.line_chart → Altair 레이어 차트로 교체, ENTER_LONG(초록 ▲) / ENTER_SHORT(빨강 ▼) 마커 오버레이
  - Net PnL 섹션 (Tab 1): 총 순수익, 총 수수료, 청산 횟수 메트릭 추가
  - 자동 갱신: 5초 주기 st.rerun()

  ---
  실행 명령어

  # 봇 실행
  poetry run python -m app

  # Streamlit 대시보드 실행
  poetry run streamlit run app/dashboard.py --server.port 8501