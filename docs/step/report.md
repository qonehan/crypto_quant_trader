  아키텍처 진단 리포트                                                                                                                                                          
                                                                                                                                                                                
  분석 기준일: 2026-03-05 | 브랜치: copilot/review-alt-3-changes                                                                                                                
                  
  ---
  항목 1. 중복된 데이터 수집/저장 로직

  1-A. 제거 대상 파일 및 함수 (GCP가 대신 수행)

  현재 로컬에서 장기 수집/저장을 담당하는 코드가 두 계층으로 존재합니다.

  [계층 A] Alt Data 수집 전체 — 완전 제거 대상

  ┌───────────────────────────────┬─────────────────────────────────────────────────────────────────────────────────────┬───────────────────────────────────────────────────┐
  │             파일              │                                    클래스 / 함수                                    │                      하는 일                      │
  ├───────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ app/altdata/binance_ws.py     │ BinanceMarkPriceWs, BinanceForceOrderWs                                             │ Binance WS → binance_mark_price_1s,               │
  │                               │                                                                                     │ binance_force_orders INSERT                       │
  ├───────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ app/altdata/binance_rest.py   │ BinanceFuturesRestPoller                                                            │ Binance REST → binance_futures_metrics UPSERT     │
  ├───────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ app/altdata/coinglass_rest.py │ CoinglassRestPoller                                                                 │ Coinglass REST → coinglass_liquidation_map INSERT │
  ├───────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ app/altdata/runner.py         │ BinanceAltDataRunner, CoinglassAltDataRunner                                        │ 위 수집기들을 asyncio 태스크로 실행               │
  ├───────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ app/altdata/writer.py         │ insert_mark_price, insert_force_order, upsert_futures_metric,                       │ DB INSERT/UPSERT 헬퍼 전부                        │
  │                               │ insert_coinglass_liq_map, insert_coinglass_call_status                              │                                                   │
  ├───────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────┼───────────────────────────────────────────────────┤
  │ app/bot.py (L142~153)         │ ALT_DATA_ENABLED 블록                                                               │ 위 Runner들을 태스크에 등록                       │
  └───────────────────────────────┴─────────────────────────────────────────────────────────────────────────────────────┴───────────────────────────────────────────────────┘

  [계층 B] 업비트 실시간 마켓 데이터 저장 — 부분 제거 대상

  ┌─────────────────────────────┬──────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┐
  │            파일             │                       함수                       │                          하는 일                          │
  ├─────────────────────────────┼──────────────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
  │ app/marketdata/resampler.py │ MarketResampler.run() 내 upsert_market_1s() 호출 │ 매 1초 Upbit WebSocket 데이터를 market_1s 테이블에 INSERT │
  ├─────────────────────────────┼──────────────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
  │ app/db/writer.py            │ upsert_market_1s()                               │ market_1s UPSERT SQL                                      │
  └─────────────────────────────┴──────────────────────────────────────────────────┴───────────────────────────────────────────────────────────┘

  주의: UpbitWsClient 자체(app/marketdata/upbit_ws.py)는 실시간 거래용으로 계속 유지 필요합니다. 제거 대상은 그 데이터를 DB에 저장하는 upsert_market_1s() 호출 부분입니다.

  DB 스키마/마이그레이션:
  app/db/init_db.py, app/db/migrate.py, app/db/models.py — GCP가 스키마를 관리한다면 로컬에서는 제거 또는 read-only 클라이언트로 교체 필요.

  ---
  1-B. GCP DB에서 "읽기만" 하도록 개편 시 수정 포인트

  현재 로컬 DB를 읽는 핵심 경로는 다음과 같습니다:

  GCP DB (postgresql://gcp-host/..)
          ↓  SELECT만
  app/predictor/runner.py    → market_1s WHERE ts >= now()-120s  (마켓 윈도우)
  app/barrier/controller.py  → market_1s (변동성 계산용)
  app/exchange/runner.py     → market_1s (DATA_LAG 판단)
  scripts/export_dataset.py  → predictions + market_1s (학습 데이터 export)
  app/dashboard.py           → 모든 테이블 (시각화)

  수정 포인트:

  1. app/config.py DB_URL 분리: 현재 단일 DB_URL = "postgresql://...@db:5432/quant" → GCP_DB_URL (read, 학습/UI용)과 LOCAL_DB_URL (write, paper trading 결과 저장용)으로 분리
  권장
  2. scripts/export_dataset.py: create_engine(s.DB_URL) → create_engine(s.GCP_DB_URL) 으로 교체
  3. app/dashboard.py: get_engine(settings) → GCP DB URL 사용
  4. app/predictor/runner.py, app/barrier/controller.py: market window 쿼리를 GCP DB에서 읽도록 engine 교체

  ---
  항목 2. AI 학습(Training) 파이프라인 상태

  2-A. 학습 데이터 파이프라인 (현재 상태)

  로컬 PostgreSQL (docker db)
          ↓  SELECT
  scripts/export_dataset.py
    ├── load_features() → predictions 테이블 (t0, p_up, p_down, ev, ev_rate, r_t, ...)
    └── load_prices()   → market_1s 테이블 (ts, mid_close_1s)
          ↓  .parquet / .csv 출력
  scripts/train_regression_baseline.py  (--input dataset.parquet)
  scripts/walkforward_ridge.py
  scripts/train_and_trade_econ_gate.py

  현재 문제: 로컬 Docker DB(@db:5432/quant)에서 데이터를 읽기 때문에, GCP DB로 연결만 바꾸면 동일 파이프라인 재사용 가능합니다.

  2-B. 현재 구현된 알고리즘

  ┌─────────────────────┬──────────────────────────────────────────────────────────────┐
  │        구분         │                             내용                             │
  ├─────────────────────┼──────────────────────────────────────────────────────────────┤
  │ 알고리즘            │ Ridge Regression + StandardScaler (sklearn Pipeline)         │
  ├─────────────────────┼──────────────────────────────────────────────────────────────┤
  │ 하이퍼파라미터 탐색 │ alpha ∈ {0.1, 1.0, 10.0, 50.0} — validation RMSE 최소 선택   │
  ├─────────────────────┼──────────────────────────────────────────────────────────────┤
  │ 시계열 분할         │ Train 70% / Valid 15% / Test 15% (시간 순 정렬 후 고정 분할) │
  ├─────────────────────┼──────────────────────────────────────────────────────────────┤
  │ Walk-forward        │ scripts/walkforward_ridge.py 별도 구현                       │
  └─────────────────────┴──────────────────────────────────────────────────────────────┘

  2-C. 피처(Feature) 및 라벨(Label) 정의

  피처 (export_dataset → train 시 자동 감지):

  ┌──────────────────────┬────────────────┬───────────────────────────────────────┐
  │        피처명        │      출처      │                 의미                  │
  ├──────────────────────┼────────────────┼───────────────────────────────────────┤
  │ p_up, p_down, p_none │ predictions    │ 모델 예측 확률                        │
  ├──────────────────────┼────────────────┼───────────────────────────────────────┤
  │ ev, ev_rate          │ predictions    │ 기댓값, 기댓값/기대시간               │
  ├──────────────────────┼────────────────┼───────────────────────────────────────┤
  │ r_t                  │ predictions    │ 배리어 크기                           │
  ├──────────────────────┼────────────────┼───────────────────────────────────────┤
  │ z_barrier            │ predictions    │ 정규화된 배리어                       │
  ├──────────────────────┼────────────────┼───────────────────────────────────────┤
  │ spread_bps           │ predictions    │ 호가 스프레드 (bps)                   │
  ├──────────────────────┼────────────────┼───────────────────────────────────────┤
  │ mom_z                │ predictions    │ 변동성 표준화 모멘텀                  │
  ├──────────────────────┼────────────────┼───────────────────────────────────────┤
  │ imb_notional_top5    │ predictions    │ 호가창 상위 5레벨 잔량 불균형         │
  ├──────────────────────┼────────────────┼───────────────────────────────────────┤
  │ cost_roundtrip_est   │ export 시 계산 │ 왕복 비용 추정 (fee_bps + spread_bps) │
  └──────────────────────┴────────────────┴───────────────────────────────────────┘

  라벨 (Target):

  label_return = (future_mid - entry_mid) / entry_mid

  - future_mid: market_1s.mid_close_1s at t0 + horizon_sec (default 120초)
  - entry_mid: market_1s.mid_close_1s at t0
  - 라벨 누수 방지: merge_asof(direction='forward') + label_lag_sec 구간 검증

  중요 갭: 학습된 Ridge 모델(artifacts/ml1/ridge_model.joblib)이 실시간 거래 루프에 연결되지 않았습니다. 현재 실전 거래는 학습 없는 규칙 기반 모델(BaselineModelV1)을
  사용합니다.

  ---
  항목 3. AI 기반 거래(Trading) 알고리즘

  3-A. 잔고 확인 및 주문 체결 로직

  app/exchange/upbit_rest.py      ← UpbitRestClient
    ├── get_accounts()            → GET /v1/accounts (잔고 조회)
    ├── create_order()            → POST /v1/orders (실주문)
    ├── order_test()              → POST /v1/orders/test (테스트 주문)
    └── get_order(uuid)           → GET /v1/orders/{uuid} (주문 상태 폴링)

  app/exchange/runner.py
    ├── UpbitAccountRunner        → 30초마다 잔고 polling → upbit_account_snapshots 저장
    └── ShadowExecutionRunner     → paper_trades 감지 → 3단계 모드로 Upbit API 호출
          ├── shadow 모드: DB 기록만 (API 호출 없음)
          ├── test 모드: POST /v1/orders/test 호출
          └── live 모드: POST /v1/orders 실호출 (4중 안전장치)

  4중 안전장치 (모두 참이어야 live 허용):
  1. LIVE_TRADING_ENABLED=true
  2. UPBIT_TRADE_MODE=live
  3. LIVE_GUARD_PHRASE="I_CONFIRM_LIVE_TRADING"
  4. PAPER_POLICY_PROFILE != "test"

  3-B. 모델 예측 → 진입/청산 결정 로직

  [매 5초]
  PredictionRunner.run()
    1. market_1s 최근 120초 윈도우 SELECT (로컬 DB)
    2. barrier_state 최신 행 SELECT
    3. BaselineModelV1.predict() 실행
       │
       ├── mom_z = 변동성 표준화 모멘텀 (ret_10s, ret_60s / sigma_1s)
       ├── imb_notional_top5 = 호가창 잔량 불균형
       ├── spread_bps = 호가 스프레드
       ├── score = A*mom_z + B*imb - C*spread_term
       ├── p_dir = sigmoid(score)
       ├── z_barrier = r_t / sigma_h
       ├── p_none = 1 - exp(-C_z * z_barrier²)
       └── ev = p_up*r_t + p_down*(-r_t) + p_none*r_none_pred - cost
    4. predictions 테이블 UPSERT

  PaperTradingRunner._run_tick()
    1. predictions 최신 행 SELECT
    2. MarketState 스냅샷 (in-memory best_bid/ask, lag_sec)
    3. decide_action() 호출

    FLAT → ENTER_LONG 조건 (strict 프로필):
      ev_rate ≥ ENTER_EV_RATE_TH (0.0)
      p_none ≤ ENTER_PNONE_MAX (0.70)
      p_up ≥ p_down + ENTER_PDIR_MARGIN (0.05)
      spread_bps ≤ ENTER_SPREAD_BPS_MAX (20.0)
      lag_sec ≤ DATA_LAG_SEC_MAX (5.0)

    LONG → EXIT_LONG 조건:
      TP: bid ≥ u_exec (entry*(1+r_t))
      SL: bid ≤ d_exec (entry*(1-r_t))
      TIME: now ≥ entry_time + h_sec (120s)
      EV_BAD: ev_rate ≤ EXIT_EV_RATE_TH (-0.00002)

  현재 상태 요약: 경제적 엣지(수수료 0.05% × 2 + 슬리피지 + 스프레드) 계산 로직은 구현 완료. 단, 실제 ML 모델 가중치가 없는 규칙 기반 점수로 예측하고 있어 학습된 모델과의
  연결이 미완성 상태입니다.

  ---
  항목 4. UI 및 시각화 상태

  4-A. 현재 구현

  프레임워크: Streamlit (app/dashboard.py, 1,053 라인)

  현재 7개 섹션 구성:

  ┌───────────────────────────┬─────────────────────────────────────────────────────────────────────┐
  │           패널            │                             시각화 내용                             │
  ├───────────────────────────┼─────────────────────────────────────────────────────────────────────┤
  │ 메인                      │ market_1s 최근 60행 테이블 + 5분 mid 가격 차트                      │
  ├───────────────────────────┼─────────────────────────────────────────────────────────────────────┤
  │ [A] Barrier Feedback      │ r_t / r_min_eff / cost_roundtrip 시계열, k_vol_eff / none_ewma 차트 │
  ├───────────────────────────┼─────────────────────────────────────────────────────────────────────┤
  │ [B] Probabilistic Metrics │ Brier score, LogLoss, Accuracy, Hit Rate (최근 N건)                 │
  ├───────────────────────────┼─────────────────────────────────────────────────────────────────────┤
  │ [C] Calibration Tables    │ UP/DOWN/NONE 클래스별 calibration (ECE 포함)                        │
  ├───────────────────────────┼─────────────────────────────────────────────────────────────────────┤
  │ [D] EV/Cost Diagnostic    │ EV mean/median, ev_rate, p_none 분포, action_hat 바차트             │
  ├───────────────────────────┼─────────────────────────────────────────────────────────────────────┤
  │ [E] Paper Trading         │ 포지션 현황, 에퀴티 커브(6h), 드로우다운, 거래 통계, 청산 이유 분포 │
  ├───────────────────────────┼─────────────────────────────────────────────────────────────────────┤
  │ [F] Upbit Exchange        │ 계좌 잔액, 주문 시도 로그, 모드/Ready 상태, throttle 진단           │
  ├───────────────────────────┼─────────────────────────────────────────────────────────────────────┤
  │ [G] Alt Data              │ Binance mark price 수신 지연, 선물 메트릭, Coinglass 청산맵         │
  └───────────────────────────┴─────────────────────────────────────────────────────────────────────┘

  데이터 소스: 현재 전부 로컬 DB (settings.DB_URL = postgresql://...@db:5432/quant).
  모든 쿼리는 SELECT 전용 — 쓰기 없음.

  4-B. GCP DB 연동 시 필요한 구조적 변경

  변경 난이도: 낮음 — 단일 연결 문자열 교체로 대부분 해결 가능합니다.

  # app/config.py 에 추가
  GCP_DB_URL: str = "postgresql+psycopg://user:pass@<gcp-host>:5432/quant"

  # app/dashboard.py 수정 (현재)
  engine = get_engine(settings)          # DB_URL 사용

  # 변경 후
  from sqlalchemy import create_engine
  engine = create_engine(settings.GCP_DB_URL)  # GCP DB로 직접 연결

  추가로 권장하는 구조적 변경:

  ┌──────────────────────┬────────────────┬───────────────────────────────────────────────────┐
  │         항목         │      현재      │                      변경 후                      │
  ├──────────────────────┼────────────────┼───────────────────────────────────────────────────┤
  │ 학습 결과 시각화     │ 없음           │ artifacts/ 디렉토리의 metrics.json 읽기 패널 추가 │
  ├──────────────────────┼────────────────┼───────────────────────────────────────────────────┤
  │ 실시간 GCP 연결 상태 │ 없음           │ GCP DB lag 모니터링 지표 추가                     │
  ├──────────────────────┼────────────────┼───────────────────────────────────────────────────┤
  │ 훈련된 모델 성능     │ 없음           │ IC, Win Rate, RMSE 표시 패널 추가                 │
  ├──────────────────────┼────────────────┼───────────────────────────────────────────────────┤
  │ [G] Alt Data 패널    │ 로컬 수집 상태 │ GCP에서 수집된 데이터 조회로 교체                 │
  └──────────────────────┴────────────────┴───────────────────────────────────────────────────┘

  ---
  종합 현황 요약

  ┌─────────────────────────────────────────────────────────┐
  │            현재 로컬 시스템 상태 요약                    │
  ├──────────────────────┬──────────────────────────────────┤
  │ 영역                 │ 상태                             │
  ├──────────────────────┼──────────────────────────────────┤
  │ 데이터 수집 (Alt)    │ ⚠️   GCP와 중복 — 제거 필요      │
  │ 데이터 수집 (Upbit)  │ ⚠️   WS 필요, DB 저장은 제거 가능 │
  │ 학습 파이프라인      │ ✅  구조 완비, DB URL만 교체 필요 │
  │ 학습 알고리즘        │ ✅  Ridge 완성 (walkforward 포함) │
  │ 모델→거래 연결       │ ❌  Ridge 모델 미연결 (규칙 기반) │
  │ Paper Trading 로직   │ ✅  EV 게이트, TP/SL/TIME 완성   │
  │ 실거래 체결 (Upbit)  │ ✅  4중 안전장치 포함            │
  │ Streamlit 대시보드   │ ✅  7개 패널 운영 중             │
  │ GCP DB 연동          │ ❌  DB_URL 교체만으로 전환 가능  │
  └──────────────────────┴──────────────────────────────────┘

  가장 시급한 3가지 작업

  1. ALT_DATA_ENABLED=false — bot.py에서 Binance/Coinglass 수집 즉시 비활성화 (GCP 중복 제거)
  2. GCP_DB_URL 추가 — config.py에 GCP 읽기 전용 연결 추가, export_dataset.py 및 dashboard.py 연결 교체
  3. Ridge 모델 → PredictionRunner 연결 — 학습된 ridge_model.joblib을 BaselineModelV1 대신 BaseModel 인터페이스로 래핑하여 실시간 추론에 투입