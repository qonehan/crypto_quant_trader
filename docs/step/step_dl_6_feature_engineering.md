# Step DL-6: HFT Feature Engineering 보고서
**CVD & Multi-Resolution Features for CryptoMamba Pipeline**

---

## 1. 개요

| 항목 | 내용 |
|---|---|
| 입력 데이터셋 | `data/datasets/btc_1m_dl_2y.parquet` (54컬럼, 1,043,400행) |
| 출력 데이터셋 | `data/datasets/btc_1m_hft_v2.parquet` (71컬럼, 1,043,400행) |
| 기간 | 2024-03-13 ~ 2026-03-13 (2년) |
| 신규 피처 | 17개 추가 (+2개 타겟 포함) |
| 소요 시간 | 52.9초 |

---

## 2. 신규 피처 그룹

### [1] CVD (Cumulative Volume Delta) — 5개

**설계 근거:**
1분봉 OHLCV에서 틱 단위 체결 데이터를 모사하기 위해 **Kaufman CVD Proxy** 사용.

$$\text{bar\_delta}_t = V_t \times \frac{2C_t - H_t - L_t}{H_t - L_t + \varepsilon}$$

- $(2C-H-L)/(H-L)$: 봉 내에서 close가 range의 상단/하단 중 어디에 위치하는지 측정
- $+1.0$: 완전 강세봉 (close = high), $-1.0$: 완전 약세봉 (close = low)
- $\times V$: 거래량으로 가중 → 절대 매수/매도 압력 추정

| 피처 | 설명 | Q25 | Q50 | Q75 |
|---|---|---|---|---|
| `bar_delta` | 봉당 순매수 압력 | -0.54 | +0.04 | +0.63 |
| `cvd_20` | 20봉 롤링 CVD 누적합 | -3.96 | +0.56 | +5.48 |
| `cvd_60` | 60봉 롤링 CVD 누적합 | -8.77 | +1.60 | +12.70 |
| `cvd_slope_5` | cvd_20의 5봉 모멘텀 | -2.81 | -0.01 | +2.78 |
| `cvd_norm` | 60봉 std로 정규화된 bar_delta | -0.40 | +0.04 | +0.45 |

**CVD vs future_ret_1 상관계수:**
| 피처 | 상관계수 |
|---|---|
| cvd_20 | -0.0062 |
| cvd_60 | -0.0077 |
| cvd_slope_5 | **+0.0083** (양의 모멘텀 효과) |

> 1분봉의 무작위 행보(Random Walk) 특성상 절대 상관계수는 작지만, 비선형 모델(SSM, KAN)이 이를 조합하면 방향성 예측에 기여 가능.

---

### [2] EMA 다중 타임프레임 — 3개

기존 데이터셋에는 SMA(단순이동평균)만 존재. EMA는 최근 봉에 지수적으로 더 큰 가중치를 부여하므로 **가격 반전/추세 전환 탐지에 HFT에 더 적합**.

| 피처 | 설명 | 수식 |
|---|---|---|
| `ema15_dist` | 15-EMA 대비 현재가 편차(%) | $(C - \text{EMA}_{15}) / \text{EMA}_{15} \times 100$ |
| `ema60_dist` | 60-EMA 대비 현재가 편차(%) | $(C - \text{EMA}_{60}) / \text{EMA}_{60} \times 100$ |
| `ema_cross_15_60` | EMA 크로스 신호(%) | $(\text{EMA}_{15} - \text{EMA}_{60}) / C \times 100$ |

---

### [3] 볼린저 밴드 강화 시그널 — 3개

기존 `bb_pct`, `bb_width`(연속값)에 **이진 돌파 시그널** 추가.

| 피처 | 설명 | 발생 빈도 |
|---|---|---|
| `bb_upper_break` | close > bb_upper → 1 | 51,488회 (**4.93%**) |
| `bb_lower_break` | close < bb_lower → 1 | 56,425회 (**5.41%**) |
| `bb_squeeze` | bb_width < 25th-pct(rolling 50봉) → 1 | 321,393행 (**30.80%**) |

**Squeeze 해석:**
밴드 수축(Squeeze) 구간에서 이후 돌파 시 방향성이 강한 경향. Mamba SSM의 선택적 기억 메커니즘이 이 구간을 장기 상태로 유지 → 돌파 시 강한 신호 생성 기대.

---

### [4] 미시구조 피처 — 2개

틱 데이터 없이 봉(Bar) 정보만으로 Order Flow를 추정.

| 피처 | 설명 | 상관계수(vs future_ret_1) |
|---|---|---|
| `order_imbalance` | $(2C-H-L)/(H-L+\varepsilon)$, 범위 [-1,1] | **-0.0443** (평균 회귀 효과) |
| `vwap_dev_20` | (close - VWAP₂₀) / VWAP × 100 | -0.0252 |

> `order_imbalance`의 음의 상관계수: "현재 봉이 강세일수록 다음 봉은 약세"라는 1분봉 평균 회귀 패턴 시사. GMADL이 이 magnitude-weighted 패턴을 학습할 것으로 기대.

---

### [5] 거래량 피처 — 1개

| 피처 | 설명 |
|---|---|
| `vol_ratio_15m` | volume / volume.rolling(15).mean() |

기존 피처 (5봉, 60봉) + 15봉 추가 → 5분/15분/60분 3단계 거래량 피라미드 완성.

---

### [6] OBV 모멘텀 — 1개

| 피처 | 설명 |
|---|---|
| `obv_slope_5` | OBV.diff(5) / (vol_mean_5 + ε) |

Raw OBV는 절대값이 커서 비교 불가 → 5봉 변화율로 정규화. 거래량 누적 추세의 **가속/감속**을 포착.

---

## 3. 타겟 업데이트

| 타겟 | 설명 | 분포 |
|---|---|---|
| `future_ret` (기존) | 60분 후 가격 변화율, horizon=60 | LONG 35.47% |
| `future_ret_1` **(신규)** | log(close[t+1]/close[t]), 1분 로그수익률 | 연속값 |
| `target` (기존) | future_ret > fee×2 → 1, horizon=60 | LONG 35.47% |
| `target_1m` **(신규)** | future_ret_1 > fee×2 → 1, 1분 | LONG **4.75%** |

**target_1m 불균형 분석:**
- 1분봉 수수료 임계값 (0.1%) 초과 상승: 4.75%
- 이는 1분봉 특성상 정상 → GMADL 회귀 모델은 `future_ret_1`(연속값)을 직접 예측하므로 클래스 불균형 무관

**future_ret_1 분포 (GMADL 크기 가중치 분포):**
| 임계값 | 해당 비율 |
|---|---|
| \|ret\| > 0.001 (0.1%) | 9.35% |
| \|ret\| > 0.002 (0.2%) | 1.73% |
| \|ret\| > 0.005 (0.5%) | 0.07% |

> GMADL은 magnitude에 비례한 가중치를 부여. 전체 바의 9.35%에 해당하는 "유의미한 이동" 구간에 최적화 역량 집중 가능.

---

## 4. 최종 피처셋 요약

| 그룹 | 기존 피처 수 | 신규 피처 수 |
|---|---|---|
| OHLCV 원시 | 5 | — |
| 수익률 (1m/5m/15m/60m) | 4 | — |
| 이동평균 SMA (6종 × 2) | 12 | — |
| 변동성 | 6 | — |
| RSI | 1 | — |
| MACD | 3 | — |
| 볼린저 밴드 | 4 | +3 (break×2, squeeze) |
| ATR | 2 | — |
| 채널 | 3 | — |
| 시간 피처 | 4 | — |
| 매크로 (DXY/SP500/Gold/BTC-USD) | 8 | — |
| **CVD** | 0 | **+5** |
| **EMA 다중 TF** | 0 | **+3** |
| **미시구조** | 0 | **+2** |
| **거래량 15m** | 0 | **+1** |
| **OBV 모멘텀** | 0 | **+1** |
| **타겟** | 2 | +2 (future_ret_1, target_1m) |
| **합계** | **54** | **+17 = 71** |

---

## 5. 데이터 품질 검증

- **NaN 비율**: 신규 17개 피처 모두 **0.0%** (롤링 윈도우 최대 = 60봉, warm-up 자동 처리됨)
- **무결성**: 행 수 불변 (1,043,400행)
- **Data Leakage**: future_ret_1은 `.shift(-1)` 사용, 마지막 1행 NaN 처리로 leakage 없음
- **출력 파일**: `data/datasets/btc_1m_hft_v2.parquet` (381.0 MB)

---

## 6. 다음 단계

이 보고서에 대한 **승인 후** Step DL-7(GMADL 손실 함수)을 진행합니다.

- **Step DL-7**: `app/predictor/losses.py` — GMADL 구현, PyTorch 텐서 연산 검증
- **Step DL-8**: `app/predictor/dl_model.py` — DWT + SSM(Mamba) + KAN 아키텍처

---
*생성일: 2026-03-13 | 데이터: KRW-BTC 1분봉 2년 (업비트)*
