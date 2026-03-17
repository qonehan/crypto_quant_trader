# Prediction Models 전체 정리

> 최종 업데이트: 2026-03-17

---

## 목차

1. [모델 구조 개요](#1-모델-구조-개요)
2. [ML 모델 (Ridge / HGBR)](#2-ml-모델-ridge--hgbr)
3. [DL 모델 (LSTM / TCN / CryptoMamba)](#3-dl-모델-lstm--tcn--cryptomamba)
4. [Baseline 모델 (Rule-based)](#4-baseline-모델-rule-based)
5. [학습 방법](#5-학습-방법)
   - [ML 모델 학습](#51-ml-모델-학습)
   - [DL 모델 학습](#52-dl-모델-학습)
6. [피처 엔지니어링](#6-피처-엔지니어링)
7. [아티팩트 구조](#7-아티팩트-구조)
8. [성능 요약](#8-성능-요약)

---

## 1. 모델 구조 개요

```
crypto_quant_trader
├── ML 모델 (scikit-learn)
│   ├── Ridge H120 / H600 / H3600       ← ACTIVE_MODEL 환경변수로 선택
│   ├── HGBR H120 / H600
│   └── Baseline V1 (Rule-based, fallback)
│
└── DL 모델 (PyTorch)
    ├── LSTM Classifier                  ← Archived
    ├── TCN Classifier                   ← Archived
    └── CryptoMamba Classifier           ← 현재 DL 활성 모델
```

### 모델 레지스트리 (`app/predictor/ml_model.py`)

| Model ID | Class | 예측 Horizon | Gamma | Version | Status |
|---|---|---|---|---|---|
| `ridge_h3600` | RidgePredictor | 3600s (1h) | 1.5 | ridge_h3600_v1 | **ACTIVE** |
| `ridge_h600` | RidgePredictor | 600s (10m) | 1.5 | ridge_h600_v1 | Ready |
| `ridge_h120` | RidgePredictor | 120s (2m) | 1.5 | ridge_h120_v1 | Ready |
| `hgbr_h600` | HGBRPredictor | 600s (10m) | 1.5 | hgbr_h600_v1 | Ready |
| `hgbr_h120` | HGBRPredictor | 120s (2m) | 1.5 | hgbr_h120_v1 | Ready |
| `baseline_v1` | BaselinePredictor | 120s | 0.0 | baseline_v1 | Fallback |

---

## 2. ML 모델 (Ridge / HGBR)

### 2-1. 공통 사항

**입력 피처 (19개)**:

| 피처명 | 설명 | 카테고리 |
|---|---|---|
| `r_t` | 직전 bar log-return | Microstructure |
| `sigma_1s` | 1초 단위 변동성 | Microstructure |
| `sigma_h` | 호라이즌 기준 변동성 | Microstructure |
| `p_up` | Baseline 상승 확률 | Probability |
| `p_down` | Baseline 하락 확률 | Probability |
| `p_none` | 포지션 없음 확률 | Probability |
| `ev` | Expected Value | Signal |
| `ev_rate` | EV / cost ratio | Signal |
| `z_barrier` | 배리어까지 z-score | Signal |
| `mom_z` | 모멘텀 Z-score | Momentum |
| `spread_bps` | Bid-Ask 스프레드 (bps) | Liquidity |
| `imb_notional_top5` | 오더북 상위5호가 불균형 | Liquidity |
| `buy_volume_ratio` | 60초 매수 비율 (Upbit tick) | Alt: Order Flow |
| `funding_rate` | Binance 선물 Funding Rate | Alt: Derivatives |
| `long_short_ratio` | Binance L/S 비율 | Alt: Sentiment |
| `open_interest` | Binance 미결제약정 | Alt: Derivatives |
| `dxy_index` | 달러 인덱스 | Macro |
| `fear_greed_index` | 공포탐욕 지수 | Macro |
| `entry_mid` | 진입 기준 Mid Price | Reference |

**출력**: 연속 log-return 예측값 → `_compute_action_hat()`으로 방향성 변환
- `gamma` 임계값 기준: `|pred| > gamma × sigma_h` 이면 방향성 신호 발생
- 최종 액션: `ENTER_LONG` / `ENTER_SHORT` / `STAY_FLAT`

---

### 2-2. RidgePredictor (`ridge_h*`)

- **알고리즘**: `Pipeline(StandardScaler → Ridge regression)`
- **정규화**: L2 (α 는 학습 시 GridSearchCV 최적화)
- **아티팩트 경로**: `artifacts/ml_prod/h{SEC}/historical_dataset_ridge/`
  - `ridge_model.joblib` — 학습된 파이프라인
  - `feature_cols.json` — 19개 피처 컬럼명
  - `model_meta.json` — 메타데이터
  - `metrics.json` — 테스트 성능
  - `test_trades.csv` — 백테스트 거래 내역

**현재 활성 모델 (ridge_h3600) 성능**:
```
RMSE      : 0.00343
Sign Acc  : 82.65%  (vs 베이스라인 0%)
IC (Pearson): 0.536
Test 거래  : 3건 (Short 100% 승률)
Net PnL   : +0.011
```

---

### 2-3. HGBRPredictor (`hgbr_h*`)

- **알고리즘**: `HistGradientBoostingRegressor` (sklearn)
- **특이점**: 결측치 내성 (NaN 직접 처리), Ridge 대비 비선형 모델
- **아티팩트 경로**: `artifacts/ml_prod/h{SEC}/historical_dataset_hgbr/`
  - `hgbr_model.joblib`
  - `feature_cols.json`
  - `model_meta.json` / `metrics.json`

---

## 3. DL 모델 (LSTM / TCN / CryptoMamba)

> 모든 DL 모델은 PyTorch 기반, **1분봉 60개 (=1시간) → 15분 수익률 예측**

### 3-1. LSTMClassifier (Archived — Step DL-3)

```
Input (B, T=60, F=52)
  ↓ 1D-CNN (conv1d, kernel=3)
  ↓ Stacked LSTM (2 layers, hidden=64)
  ↓ FC head (64→32→1)
Output: (B, 1) logit
```

- **파라미터 수**: ~11,000
- **태스크**: Binary Classification (15분 후 상승/하락)
- **아티팩트**: `artifacts/dl_prod/lstm_model.pt`
- **입력 피처**: 52개 (`artifacts/dl_prod/feature_cols.json`)

---

### 3-2. TCNClassifier (Archived — Step DL-5)

```
Input (B, T=60, F=52)
  ↓ 6개 Dilated Causal Conv Block [dilation: 1, 2, 4, 8, 16, 32]
  ↓ Residual connections
  ↓ FC head
Output: (B, 1) logit
```

- **Receptive Field**: ~253 timesteps (≈ 4.2시간)
- **파라미터 수**: ~7,000
- **태스크**: Binary Classification
- **아티팩트**: `artifacts/dl_prod/tcn_model.pt`

---

### 3-3. CryptoMambaClassifier (Active — Step DL-8/9)

현재 DL 계열 활성 모델. **Mamba SSM + Haar DWT + KAN** 하이브리드.

#### 아키텍처

```
Input (B, T=60, F=67)
  ↓
[_HaarDWT1D]  — 비학습, 고정 Haar 웨이블릿 분해 (1/√2 계수)
  ├── low  (B, 30, F)  — 저주파 (추세)
  └── high (B, 30, F)  — 고주파 (잡음)
  ↓
[InputProj: F→d_model]  — Linear embedding
  ↓
[_CryptoMambaBlock × n_low]  — 추세 스트림 (n_low=2)
[_CryptoMambaBlock × n_high] — 잡음 스트림 (n_high=1)
  ↓
[Last Timestep Extract]  — (B, 1, d_model)
  ↓
[_KANLayer × 2]  — EfficientKAN: W_base(x) + W_spline(SiLU(x))
  ↓
Output (B, 1)  — log-return 예측 (Regression)
```

#### 핵심 컴포넌트

**`_HaarDWT1D`** (비학습)
- 고정 Haar 웨이블릿 필터 (low: `[1/√2, 1/√2]`, high: `[1/√2, -1/√2]`)
- 시계열을 추세/잡음 2개 스트림으로 분리

**`_SelectiveScan`** (Mamba SSM)
- 입력 의존적 감쇠: `dt_t = sigmoid(W_dt @ x_t)`
- 선택적 망각 게이트로 장거리 의존성 포착

**`_CryptoMambaBlock`**
```
PreNorm → in_proj → depthwise_conv(d_conv=4) → SelectiveScan → z-gate → out_proj
```

**`_KANLayer`** (EfficientKAN 근사)
```
y = W_base(x) + W_spline(SiLU(x))
```

#### 하이퍼파라미터

```json
{
  "n_features": 67,
  "d_model": 64,
  "n_low": 2,
  "n_high": 1,
  "d_conv": 4,
  "dropout": 0.1
}
```

- **파라미터 수**: ~140,000 (목표 < 200k, 서브밀리초 추론)
- **태스크**: Regression (GMADLoss)
- **타겟**: `future_ret_15` (15분 log-return)

#### 성능

```
GMADLoss (test): 0.7104
MSE             : 7.51e-07
Dir Accuracy    : 53.19%  (|ret| ≥ 0.01% 구간만 평가)
학습 시간        : 7.9초 (GPU)
```

#### 아티팩트

```
artifacts/dl_prod/
├── cryptomamba_model.pt           (322 KB) — 학습된 가중치
├── cryptomamba_model_meta.json    — 아키텍처 하이퍼파라미터
├── cryptomamba_feature_cols.json  — 67개 피처 컬럼명
├── cryptomamba_scaler.joblib      — RobustScaler (학습 시 fit)
└── cryptomamba_train_log.json     — Epoch별 학습 히스토리
```

---

## 4. Baseline 모델 (Rule-based)

**파일**: `app/models/baseline_v1.py`

ML/DL 모델 실패 시 폴백으로 사용.

```python
# 스코어 기반 방향성 확률 계산
score = SCORE_A_MOMZ × mom_z + SCORE_B_IMB × imb - SCORE_C_SPREAD × spread
p_dir = sigmoid(score)
p_up   = (1 - p_none) × p_dir
p_down = (1 - p_none) × (1 - p_dir)
```

- **피처**: `mom_z`, `imb_notional_top5`, `spread_bps`, `z_barrier` (4개)
- **출력**: ML과 동일한 `PredictionOutput` 구조
- **학습 불필요** (규칙 기반, 하이퍼파라미터만 튜닝)

---

## 5. 학습 방법

### 5-1. ML 모델 학습

#### 사전 준비: 데이터셋 빌드

```bash
# 1. GCP DB에서 4가지 소스 데이터를 집계하여 historical_dataset.parquet 생성
poetry run python scripts/build_historical_dataset.py
```

집계 소스:
1. **Upbit Orderbook** — mid, spread_bps, imb_notional_top5
2. **Upbit Tick** (60초 집계) — buy_volume_ratio
3. **Binance Derivatives** (GCP DB) — funding_rate, long_short_ratio, open_interest
4. **Macro/Sentiment** (GCP DB) — dxy_index, fear_greed_index

#### Ridge / HGBR 학습

```bash
# H120 (2분 호라이즌) 모든 모델 학습
poetry run python scripts/train_regression_baseline.py --h_sec 120

# H600 (10분 호라이즌)
poetry run python scripts/train_regression_baseline.py --h_sec 600

# H3600 (1시간 호라이즌) — 현재 프로덕션 모델
poetry run python scripts/train_regression_baseline.py --h_sec 3600
```

**학습 설정**:
- 시간 순서 분할: Train 70% / Val 15% / Test 15% (셔플 없음)
- 스케일링: StandardScaler (train 기준 fit, 누수 방지)
- 비교 모델: Ridge + HGBR 동시 학습

**출력 아티팩트**:
```
artifacts/ml_prod/h{SEC}/historical_dataset_ridge/
├── ridge_model.joblib
├── feature_cols.json
├── model_meta.json
└── metrics.json
```

#### 모델 전환 방법

```bash
# .env 파일에서 ACTIVE_MODEL만 변경
ACTIVE_MODEL=ridge_h3600   # 또는 ridge_h600, ridge_h120, hgbr_h600, hgbr_h120
```

H_SEC, MODEL_LOOKBACK_SEC는 `model_validator`가 자동 동기화.

---

### 5-2. DL 모델 학습

#### 사전 준비: HFT 피처 데이터셋 빌드

```bash
# 1. 원시 1분봉 데이터 수집 (최초 1회)
poetry run python scripts/dl/step_dl_1_collect_data.py

# 2. HFT 피처 엔지니어링 (btc_1m_dl_2y.parquet → btc_1m_hft_v2.parquet)
poetry run python scripts/dl/step_dl_6_feature_engineering.py
```

**생성 피처 그룹**:
| 그룹 | 피처 | 수 |
|---|---|---|
| CVD (누적 거래량 차분) | cvd_20, cvd_60, cvd_slope_5, cvd_norm | 4 |
| Multi-Resolution EMA | ema15_dist, ema60_dist, ema_cross_15_60 | 3 |
| Bollinger Bands | bb_upper_break, bb_lower_break, bb_squeeze | 3 |
| Microstructure | order_imbalance, vwap_dev_20 | 2 |
| Volume | vol_ratio_15m | 1 |
| OBV | obv_slope_5 | 1 |
| 타겟 | future_ret_1, future_ret_15, target_1m | 3 |

**입력/출력**:
- 입력: `data/datasets/btc_1m_dl_2y.parquet` (294 MB, 54 피처)
- 출력: `data/datasets/btc_1m_hft_v2.parquet` (408 MB, 67 피처)

#### CryptoMamba 학습 (Step DL-9)

```bash
# 기본 학습 (CPU/자동 GPU 감지)
poetry run python scripts/dl/step_dl_9_train_mamba.py

# 하이퍼파라미터 커스터마이징
poetry run python scripts/dl/step_dl_9_train_mamba.py \
  --batch_size 1024 \
  --epochs 100 \
  --gamma 150 \
  --alpha 0.75

# 스모크 테스트 (빠른 검증, 3 epoch, 15K 샘플)
poetry run python scripts/dl/step_dl_9_train_mamba.py --smoke_test
```

**학습 설정**:

| 파라미터 | 값 | 설명 |
|---|---|---|
| Optimizer | AdamW | lr=0.0003, weight_decay=0.0001 |
| Scheduler | CosineAnnealingLR + ReduceLROnPlateau | — |
| Batch Size | 512 | GPU 기준 |
| Max Epochs | 100 | Early Stop patience=15 |
| Loss | GMADLoss | α=0.7, γ=500, τ=std(train_target) |
| Max Grad Norm | 1.0 | Gradient clipping |
| 데이터 분할 | 시간순 80/10/10 | 셔플 없음 |

**GMADLoss 구조** (`app/predictor/losses.py`):
```python
# 가중치: 큰 움직임에 높은 비중
w_i = |y_i|^β · exp(min(γ·|y_i|, 20))

# 방향 손실 (tanh 기반)
L_dir = 1 - tanh(ŷ·y / τ²)

# 크기 손실 (Smooth L1)
L_smooth = SmoothL1(ŷ, y)

# 결합
Loss = α·L_dir + (1-α)·L_smooth
```

**출력 아티팩트**:
```
artifacts/dl_prod/
├── cryptomamba_model.pt
├── cryptomamba_model_meta.json
├── cryptomamba_feature_cols.json
├── cryptomamba_scaler.joblib
└── cryptomamba_train_log.json
```

#### LSTM / TCN 학습 (Archived)

```bash
# LSTM (Step DL-3)
poetry run python scripts/dl/step_dl_3_train.py

# TCN (Step DL-5)
poetry run python scripts/dl/step_dl_5_train_tcn.py
```

> 현재 보관 상태. CryptoMamba로 대체됨.

#### 백테스트 (Step DL-10)

```bash
# DL 모델 Mock 트레이딩 백테스트
poetry run python scripts/dl/step_dl_10_mock_trade.py
```

---

## 6. 피처 엔지니어링

### 데이터셋 계층

```
Upbit API / Binance API
        ↓
btc_1m_dl_2y.parquet    (294 MB, 2년 1분봉, 54 피처)
        ↓ step_dl_6_feature_engineering.py
btc_1m_hft_v2.parquet   (408 MB, 67 피처, future_ret_15 포함)  ← DL 학습용
        ↓
historical_dataset.parquet  ← ML 학습용 (build_historical_dataset.py)
```

### 피처 소스별 분류 (DL, 67개)

| 소스 | 피처 수 | 예시 |
|---|---|---|
| OHLCV 기본 | ~20 | open, high, low, close, volume |
| 모멘텀/오실레이터 | ~10 | rsi, macd, mom_z |
| 변동성 | ~5 | atr, sigma_1s, bb_squeeze |
| 마이크로스트럭처 | ~8 | spread_bps, imb_notional_top5, vwap_dev_20 |
| CVD/Volume | ~7 | cvd_20, cvd_60, vol_ratio_15m, obv_slope_5 |
| EMA | ~5 | ema15_dist, ema60_dist, ema_cross_15_60 |
| Alt (선물/매크로) | ~7 | funding_rate, fear_greed_index, dxy_index |
| 타겟 제외 | — | future_ret_15 (타겟, 학습 시 Y로 분리) |

---

## 7. 아티팩트 구조

```
artifacts/
├── ml_prod/
│   ├── h120/
│   │   ├── historical_dataset_ridge/
│   │   │   ├── ridge_model.joblib
│   │   │   ├── feature_cols.json
│   │   │   ├── model_meta.json
│   │   │   ├── metrics.json
│   │   │   └── test_trades.csv
│   │   └── historical_dataset_hgbr/
│   │       └── (동일 구조)
│   ├── h600/
│   │   ├── historical_dataset_ridge/
│   │   └── historical_dataset_hgbr/
│   └── h3600/                          ← 현재 프로덕션
│       └── historical_dataset_ridge/
│           ├── ridge_model.joblib      ← 배포 모델
│           ├── feature_cols.json
│           ├── model_meta.json
│           └── metrics.json
│
└── dl_prod/
    ├── cryptomamba_model.pt            (322 KB) ← DL 배포 모델
    ├── cryptomamba_model_meta.json
    ├── cryptomamba_feature_cols.json
    ├── cryptomamba_scaler.joblib
    ├── cryptomamba_train_log.json
    ├── lstm_model.pt                   (Archived)
    ├── tcn_model.pt                    (Archived)
    ├── scaler.joblib                   (Legacy, 52 피처용)
    └── feature_cols.json               (Legacy, 52 피처)
```

---

## 8. 성능 요약

| 모델 | Horizon | 피처 수 | 알고리즘 | Sign Acc | MSE/RMSE | Status |
|---|---|---|---|---|---|---|
| **ridge_h3600** | 3600s (1h) | 19 | Ridge Regression | **82.65%** | RMSE 0.00343 | **ACTIVE** |
| ridge_h600 | 600s (10m) | 19 | Ridge Regression | — | — | Ready |
| ridge_h120 | 120s (2m) | 19 | Ridge Regression | — | — | Ready |
| hgbr_h600 | 600s | 19 | HistGBR | — | — | Ready |
| hgbr_h120 | 120s | 19 | HistGBR | — | — | Ready |
| baseline_v1 | 120s | 4 | Rule-based | ~50% | — | Fallback |
| LSTM | 15m (target) | 52 | CNN+LSTM | — | — | Archived |
| TCN | 15m (target) | 52 | Dilated Conv | — | — | Archived |
| **CryptoMamba** | 15m (target) | 67 | Mamba+KAN+DWT | 53.19% (dir) | MSE 7.51e-07 | **DL Active** |

---

## 관련 파일

| 파일 | 역할 |
|---|---|
| `app/predictor/ml_model.py` | ML 레지스트리 (ModelFactory, RidgePredictor, HGBRPredictor) |
| `app/predictor/dl_model.py` | DL 모델 정의 (LSTM, TCN, CryptoMamba) |
| `app/predictor/losses.py` | GMADLoss 구현 |
| `app/predictor/dl_dataset.py` | CryptoTimeSeriesDataset, DataLoader |
| `app/predictor/runner.py` | PredictionRunner (추론 루프, Alt 피처 fetch) |
| `app/models/baseline_v1.py` | Baseline Rule-based 모델 |
| `scripts/build_historical_dataset.py` | ML 학습용 데이터셋 빌드 |
| `scripts/train_regression_baseline.py` | Ridge/HGBR 학습 |
| `scripts/dl/step_dl_6_feature_engineering.py` | HFT 피처 엔지니어링 |
| `scripts/dl/step_dl_9_train_mamba.py` | CryptoMamba 학습 (현재) |
| `scripts/dl/step_dl_10_mock_trade.py` | DL 백테스트 |
