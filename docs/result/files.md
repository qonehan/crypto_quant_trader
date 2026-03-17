# 프로젝트 전체 파일 역할 목록

> 생성일: 2026-03-17

---

## 프로젝트 루트

| 파일 | 역할 |
|------|------|
| `README.md` | 프로젝트 개요, 퀵스타트 가이드, 아키텍처 설명, 트러블슈팅 |
| `pyproject.toml` | Poetry 프로젝트 설정 및 의존성 (pydantic, sqlalchemy, streamlit, torch 등) |
| `poetry.lock` | 재현 가능한 빌드를 위한 의존성 버전 고정 파일 |
| `.env.example` | 환경 변수 템플릿 |
| `.env` | 실제 환경 변수 설정 (git 제외) |
| `.gitignore` | git 제외 파일 규칙 |

---

## app/ — 핵심 애플리케이션

| 파일 | 역할 |
|------|------|
| `app/__init__.py` | 패키지 초기화 마커 |
| `app/__main__.py` | 엔트리포인트 — `app.bot.main()` 호출 |
| `app/bot.py` | 메인 봇 오케스트레이터. DB, WS 클라이언트, BarrierController, Predictor, 페이퍼 트레이딩, 거래소 Runner, 대체데이터 Runner를 초기화 및 실행 |
| `app/config.py` | Pydantic Settings 기반 전체 설정 클래스. `.env` 읽기, `ACTIVE_MODEL`로 `H_SEC`·`MODEL_LOOKBACK_SEC` 자동 동기화, 거래 파라미터 정의 |
| `app/dashboard.py` | Streamlit 대시보드. 탭 구성: 직관적 요약 / 시장 데이터 / 배리어 상태 / 예측 지표 / 페이퍼 트레이딩 / 업비트 거래소 상태 |

---

## app/barrier/ — 변동성 배리어

| 파일 | 역할 |
|------|------|
| `app/barrier/__init__.py` | 패키지 마커 |
| `app/barrier/controller.py` | `BarrierController`: 1s 가격 데이터로 동적 변동성 배리어(r_t) 계산. EWMA 피드백 루프로 k_vol 스케일 적응형 조정 |

---

## app/db/ — 데이터베이스 레이어

| 파일 | 역할 |
|------|------|
| `app/db/__init__.py` | 패키지 마커 |
| `app/db/models.py` | SQLAlchemy ORM 모델. 테이블: `market_1s`, `barrier_state`, `prediction`, `paper_position`, `paper_trade`, `upbit_account`, `upbit_order`, `upbit_order_attempt` 등 |
| `app/db/session.py` | DB 엔진 팩토리 및 세션 관리 |
| `app/db/init_db.py` | 스키마 초기화 및 테이블 생성 |
| `app/db/migrate.py` | 버전 관리 DB 마이그레이션 실행기 |
| `app/db/writer.py` | 시장 데이터, 배리어, 예측, 페이퍼 트레이딩, 업비트 테이블 벌크 insert/upsert 함수 |

---

## app/marketdata/ — 시장 데이터 수집

| 파일 | 역할 |
|------|------|
| `app/marketdata/__init__.py` | 패키지 마커 |
| `app/marketdata/upbit_ws.py` | `UpbitWsClient`: 업비트 실시간 호가·체결 WebSocket 클라이언트. 자동 재연결 및 지수 백오프 포함 |
| `app/marketdata/state.py` | `MarketState`: 최우선 매수/매도호가, 스프레드, 불균형 지표 인메모리 스냅샷 |
| `app/marketdata/resampler.py` | `MarketResampler`: WebSocket 스트림에서 1s 시장 스냅샷 집계 (bid/ask OHLC, 체결 볼륨, 스프레드, 불균형) |

---

## app/predictor/ — ML 예측 모듈

| 파일 | 역할 |
|------|------|
| `app/predictor/__init__.py` | 패키지 마커 |
| `app/predictor/ml_model.py` | `ModelFactory` + `ModelSpec` 레지스트리. 플러그앤플레이 모델 선택 (ridge_h3600/h600/h120, hgbr_h600/h120, baseline_v1). 아티팩트 경로 해석 |
| `app/predictor/runner.py` | `PredictionRunner`: GCP DB에서 시장 윈도우 + 대체 데이터 조회, 피처 계산, 모델 예측 실행, 예측 결과 DB 저장 |
| `app/predictor/dl_model.py` | PyTorch 모델 정의: `LSTMClassifier` (1D-CNN + Stacked LSTM), `TCNClassifier`, `CryptoMambaClassifier` (DWT + Selective SSM + KAN layers). 15분 수익률 방향 예측용 |
| `app/predictor/dl_dataset.py` | DL 모델용 데이터셋 로딩 및 전처리 (1분 OHLC 윈도잉, 피처 스케일링, 타겟 엔지니어링) |
| `app/predictor/losses.py` | `GMADLoss` (Generalized Mean Absolute Directional Loss): 고변동성 구간 방향성 가중 손실 함수 |

---

## app/trading/ — 페이퍼 트레이딩 & 정책

| 파일 | 역할 |
|------|------|
| `app/trading/__init__.py` | 패키지 마커 |
| `app/trading/policy.py` | `decide_action()`: 결정론적 거래 정책 게이트. 우선순위: `DATA_LAG > SPREAD_WIDE > NO_PRED > COOLDOWN > RATE_LIMIT > COST_GT_RT > MODEL_FLAT > PNONE_HIGH > PDIR_WEAK > EV_RATE_LOW`. 비용 추정 포함 |
| `app/trading/runner.py` | `PaperTradingRunner`: 페이퍼 트레이딩 상태 머신. ENTER_LONG/EXIT_LONG/STAY_FLAT 결정, 포지션·P&L(수수료·슬리피지 차감) 추적, DB 저장 |
| `app/trading/paper.py` | `execute_enter_long()` / `execute_exit_long()`: 시뮬레이션 주문 실행 — 포지션 사이징, 수수료 계산, 슬리피지 적용 |

---

## app/exchange/ — 업비트 거래소 연동

| 파일 | 역할 |
|------|------|
| `app/exchange/__init__.py` | 패키지 마커 |
| `app/exchange/upbit_rest.py` | `UpbitRestClient`: 계좌 정보, 주문, 주문 이력 REST API 클라이언트. JWT 인증, 레이트 리밋, 테스트/라이브 모드 분기 |
| `app/exchange/upbit_auth.py` | 업비트 REST 요청 JWT 인증 서명 |
| `app/exchange/runner.py` | `ShadowExecutionRunner`: `/v1/orders/test`로 페이퍼 트레이드 파라미터 검증. `UpbitAccountRunner`: 주기적 계좌 잔고·주문 상태 폴링 |
| `app/exchange/reconcile.py` | 주문/계좌 상태 일관성 검증 진단 도구 |
| `app/exchange/smoke.py` | 거래소 연동 스모크 테스트 |
| `app/exchange/paper_test_smoke.py` | 페이퍼 테스트 연동 스모크 테스트 |
| `app/exchange/e2e_test.py` | 거래소 E2E 테스트 하네스 |

---

## app/altdata/ — 대체 데이터 수집

| 파일 | 역할 |
|------|------|
| `app/altdata/__init__.py` | 패키지 마커 |
| `app/altdata/runner.py` | `BinanceAltDataRunner`: 바이낸스 마크프라이스 WS, 강제청산 WS, REST 폴링 오케스트레이션. `CoinglassAltDataRunner`: 센티멘트 데이터 수집 |
| `app/altdata/binance_ws.py` | `BinanceMarkPriceWs`, `BinanceForceOrderWs`: 바이낸스 펀딩비·강제청산 데이터 WebSocket 클라이언트 |
| `app/altdata/binance_rest.py` | `BinanceFuturesRestPoller`: 미결제약정, 롱숏비율, 펀딩비 REST 폴러 |
| `app/altdata/coinglass_rest.py` | `CoinglassRestPoller`: 공포탐욕지수 등 센티멘트 지표 REST 폴러 |
| `app/altdata/writer.py` | 대체 데이터 테이블 벌크 라이터 (`binance_derivatives`, `macro_and_sentiment`) |

---

## app/evaluator/ — 예측 평가

| 파일 | 역할 |
|------|------|
| `app/evaluator/__init__.py` | 패키지 마커 |
| `app/evaluator/evaluator.py` | `Evaluator`: 예측 결과 추적 (PENDING→FINAL 상태 전이), 터치/돌파 감지, Brier Score·Log Loss 계산, 배리어 파라미터 EWMA 업데이트 |

---

## app/diagnostics/ — 모니터링 & 진단

| 파일 | 역할 |
|------|------|
| `app/diagnostics/__init__.py` | 패키지 마커 |
| `app/diagnostics/realtime_check.py` | 실시간 헬스 체크: 시장 데이터 지연, WS 연결, 대체 데이터 신선도 |
| `app/diagnostics/altdata_check.py` | 대체 데이터 테이블 진단 (행 수, 타임스탬프 신선도) |
| `app/diagnostics/coinglass_check.py` | Coinglass 데이터 유효성 검증 |
| `app/diagnostics/feature_check.py` | 피처 엔지니어링 유효성 검증 및 행별 진단 |
| `app/diagnostics/feature_leak_check.py` | 룩어헤드 바이어스·피처 누수 탐지 |
| `app/diagnostics/continuity_check.py` | 시장 데이터 시계열 연속성 체크 |

---

## app/models/ — 모델 인터페이스 & 베이스라인

| 파일 | 역할 |
|------|------|
| `app/models/__init__.py` | 패키지 마커 |
| `app/models/interface.py` | `BaseModel`: 모든 예측기 추상 인터페이스. `PredictionOutput`: 예측 결과 데이터클래스 (확률, 기대값, 피처, 임계값) |
| `app/models/baseline_v1.py` | `BaselineModelV1`: ML 모델 없는 룰 기반 베이스라인 예측기 |

---

## scripts/ — 학습·분석 스크립트

| 파일 | 역할 |
|------|------|
| `scripts/build_historical_dataset.py` | 역사 시장·대체 데이터에서 Ridge/HGBR 학습용 레이블 데이터셋 구축 |
| `scripts/walkforward_ridge.py` | Ridge 회귀 모델 워크포워드 백테스트 하네스 |
| `scripts/train_and_trade_econ_gate.py` | 경제 센티멘트 게이트 포함 Ridge 모델 학습 및 백테스트 |
| `scripts/train_regression_baseline.py` | 베이스라인 회귀 모델 학습 |
| `scripts/export_dataset.py` | 외부 분석용 데이터셋 내보내기 |
| `scripts/activate_env_keys.py` | 환경 변수 키 활성화 유틸리티 |

---

## scripts/dl/ — 딥러닝 학습 파이프라인

| 파일 | 역할 |
|------|------|
| `scripts/dl/step_dl_1_collect_data.py` | 거래소(바이낸스, 업비트)에서 1분 BTC 데이터 및 대체 데이터 수집 |
| `scripts/dl/step_dl_2_dataset.py` | 윈도잉·피처 엔지니어링: 240 스텝 × 52 피처 텐서 생성 (15분 수익률 타겟) |
| `scripts/dl/step_dl_3_train.py` | `LSTMClassifier` 학습 (모의 트레이딩 검증 루프 포함) |
| `scripts/dl/step_dl_4_mock_trade.py` | LSTM 예측 기반 모의 트레이딩 (수수료·슬리피지 반영 P&L) |
| `scripts/dl/step_dl_5_train_tcn.py` | `TCNClassifier` (Temporal Convolutional Network) 학습 |
| `scripts/dl/step_dl_6_feature_engineering.py` | 고급 피처 추출: 미세구조, 모멘텀, 변동성, 대체 데이터 지표 |
| `scripts/dl/step_dl_7_test_gmadl.py` | `GMADLoss` 구현 테스트 및 손실 랜드스케이프 분석 |
| `scripts/dl/step_dl_8_test_model.py` | `CryptoMambaClassifier` 아키텍처 테스트 (역전파 검증) |
| `scripts/dl/step_dl_9_train_mamba.py` | `CryptoMamba` (DWT + SSM + KAN) + `GMADLoss`로 15분 타겟 학습 |
| `scripts/dl/step_dl_10_mock_trade.py` | CryptoMamba 예측 기반 모의 트레이딩 |

---

## artifacts/ — 학습된 모델 & 백테스트 결과

### artifacts/ml_prod/ — 프로덕션 ML 모델
| 경로 | 역할 |
|------|------|
| `artifacts/ml_prod/h120/` | 120s 예측 지평 Ridge/HGBR 모델 (`ridge_model.joblib`, `hgbr_ridge_model.joblib`, `feature_cols.json`, `model_meta.json`, `metrics.json`, `test_trades.csv`) |
| `artifacts/ml_prod/h600/` | 600s 예측 지평 Ridge/HGBR 모델 |
| `artifacts/ml_prod/h3600/` | 3600s 예측 지평 Ridge/HGBR 모델 (현재 ACTIVE_MODEL) |
| `artifacts/ml_prod/h3600_v2/` | h3600 개선 실험 버전 |

### artifacts/dl_prod/ — 딥러닝 프로덕션 모델
| 파일 | 역할 |
|------|------|
| `artifacts/dl_prod/lstm_model.pt` | LSTM 모델 PyTorch 체크포인트 |
| `artifacts/dl_prod/tcn_model.pt` | TCN 모델 PyTorch 체크포인트 |
| `artifacts/dl_prod/cryptomamba_scaler.joblib` | CryptoMamba 피처 스케일러 |
| `artifacts/dl_prod/cryptomamba_model_meta.json` | CryptoMamba 모델 메타데이터 |

### artifacts/ml1, ml2, ml3, ml_new/ — 실험 모델 변형
다양한 예측 지평(h120, h300, h600, h900)과 데이터셋 변형(btc_24h, btc_80m, btc_alt15/16, btc_seg)에 대한 과거 Ridge/HGBR 모델 실험 아티팩트.

---

## data/datasets/ — 역사 데이터 Parquet

| 파일 | 역할 |
|------|------|
| `historical_dataset.parquet` | 레이블 포함 마스터 통합 데이터셋 (수익률, 방향) |
| `btc_1h.parquet` | BTC 1시간봉 OHLC |
| `btc_1m_dl.parquet` | DL 학습용 1분봉 BTC 데이터 |
| `btc_1m_dl_2y.parquet` | 2년치 1분봉 DL 데이터 |
| `btc_24h_h900_maker.parquet` 외 | 예측 지평·수수료 타입별 다양한 데이터셋 변형 |
| `btc_80m_h*.parquet` | 80분 집계 윈도우 데이터셋 |
| `btc_alt15_h*.parquet`, `btc_alt16_h*.parquet` | 대체 데이터(바이낸스, 매크로) 포함 변형 |
| `local_h120_maker.parquet` | 로컬 수집 데이터 (120s 지평) |
| `btc_1m_hft_v2.parquet` | 고빈도 틱 데이터 변형 |
| `_sanity_h1800.parquet` | 데이터 품질 검증용 데이터셋 |

---

## data/backups/ — 백업 데이터

| 경로 | 역할 |
|------|------|
| `data/backups/2026-03-05/` | 대체 데이터 테이블 스냅샷 (`binance_derivatives`, `macro_and_sentiment`, `upbit_orderbook`, `upbit_tick`) Parquet 백업 |

---

## docs/step/ — 개발 단계별 문서 (~70개 마크다운)

| 카테고리 | 내용 |
|----------|------|
| Phase 0~2 (`step0.md` ~ `step11.md`) | 초기 개발 및 베이스라인 모델 실험 |
| 대체 데이터 (`step-alt-3` ~ `step-alt-16`) | 바이낸스·Coinglass 대체 데이터 통합 실험 |
| 딥러닝 (`step_dl_1` ~ `step_dl_10`) | CryptoMamba HFT 파이프라인 개발 (DWT, SSM, KAN, GMADLoss) |
| 모델 레지스트리 (`step_2_1` ~ `step_2_18`) | Ridge/HGBR 모델 학습·백테스트·레지스트리 진화 |
| 결과 보고 (`report.md`, `step-altdata-*.md`) | 결과 집계 및 최종 보고서 |

---

## docs/result/ — 결과 출력

| 파일 | 역할 |
|------|------|
| `result_2026-03-05.md` | 2026-03-05 기준 모델 성능, 백테스트 결과, 지표 요약 |
| `files.md` | 본 파일 — 프로젝트 전체 파일 역할 목록 |

---

## logs/ — 런타임 로그

| 파일 | 역할 |
|------|------|
| `logs/bot.log` | 메인 봇 애플리케이션 로그 |
| `logs/bot_supervisor.log` | Supervisor/데몬 로그 |
| `logs/keepalive.log` | 헬스체크·킵얼라이브 로그 |
| `logs/mock_trade_20260313.log` | 모의 트레이딩 세션 로그 |

---

## 설정 파일

| 파일 | 역할 |
|------|------|
| `.devcontainer/docker-compose.yml` | 개발 환경용 PostgreSQL 컨테이너 설정 |
| `.devcontainer/Dockerfile` | 개발 컨테이너 이미지 빌드 스펙 |
| `.streamlit/config.toml` | Streamlit 앱 설정 |

---

## 핵심 데이터 흐름 요약

```
시장 데이터:   업비트 WS → UpbitWsClient → MarketResampler → market_1s 테이블
대체 데이터:   바이낸스 WS/REST + Coinglass → BinanceAltDataRunner → alt 데이터 테이블
배리어:        market_1s → BarrierController → barrier_state 테이블
예측:          market_1s + barrier + alt-data → PredictionRunner + 모델 → predictions 테이블
평가:          predictions(PENDING) → Evaluator → 결과, 파라미터 업데이트
페이퍼 트레이딩: predictions + 정책 게이트 → PaperTradingRunner → paper_trades 테이블
거래소:        paper_trades → ShadowExecutionRunner/UpbitAccountRunner → upbit_* 테이블
대시보드:      전체 테이블 → Streamlit 대시보드 (실시간 지표 및 시각화)
```
