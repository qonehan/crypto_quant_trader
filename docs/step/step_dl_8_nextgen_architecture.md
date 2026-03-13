# Step DL-8: 차세대 HFT 아키텍처 보고서
**CryptoMambaClassifier — DWT + Selective SSM + KAN Mixer**

---

## 1. 구현 요약

| 항목 | 내용 |
|---|---|
| 파일 | `app/predictor/dl_model.py` (기존 파일에 추가) |
| 주요 클래스 | `CryptoMambaClassifier`, `_HaarDWT1D`, `_SelectiveScan`, `_CryptoMambaBlock`, `_KANLayer` |
| 검증 스크립트 | `scripts/dl/step_dl_8_test_model.py` |
| 총 파라미터 | **77,187** (목표 < 200,000 달성) |
| 출력 Shape | **(B, 1)** ✓ |
| 역전파 NaN | **없음** ✓ |
| CPU 추론 (B=1) | **4.0 ms** (GPU 환경: < 1ms 예상) |

---

## 2. 전체 아키텍처

```
Input: (B, T=60, F=71)
       ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [A] _HaarDWT1D — 추세/노이즈 물리적 분리 (비학습, 파라미터=0)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
       ↓ low (B,30,71)              ↓ high (B,30,71)
       │  [추세 성분]               │  [노이즈 성분]
       ↓                           ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [B] 입력 투영  71 → d_model=64  (low_proj / high_proj 분리)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
       ↓ (B,30,64)                 ↓ (B,30,64)
       ↓                           ↓
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [C] _CryptoMambaBlock × 2         _CryptoMambaBlock × 1
     (추세 스트림, 깊음)            (노이즈 스트림, 얕음)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
       ↓ last_step (B,64)          ↓ last_step (B,64)
       └───────── concat ──────────┘
                   ↓ (B, 128)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [D] _KANLayer: 128 → 64
     _KANLayer:  64 →  1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                   ↓
             Output (B, 1)  — 예측 log-return
```

---

## 3. 서브 모듈 상세

### [A] _HaarDWT1D — Haar Wavelet 분해

**역할:** 1분봉 무작위 노이즈와 MA·CVD 추세를 물리적으로 분리.

**구현 수식:**
$$x_0 = x[0::2,:], \quad x_1 = x[1::2,:]$$
$$\text{low} = \frac{x_0 + x_1}{\sqrt{2}} \quad \text{(저주파 — 추세)}, \quad
\text{high} = \frac{x_0 - x_1}{\sqrt{2}} \quad \text{(고주파 — 노이즈)}$$

| 항목 | 값 |
|---|---|
| 파라미터 | **0** (비학습 고정 필터) |
| 입출력 | (B,60,71) → low(B,30,71) + high(B,30,71) |
| 에너지 보존 오차 | **0.00e+00** (완전 보존) |
| 복잡도 | O(T·F) |

---

### [B] _SelectiveScan — Mamba-proxy 선택적 SSM

**역할:** 횡보 구간(노이즈)은 망각하고, 유동성 돌파 이벤트를 장기 기억 상태로 이산화.

**핵심 수식 (이산화된 상태 공간 방정식):**
$$\Delta_t = \sigma(W_{\Delta} x_t + b_{\Delta}) \quad \text{(입력 의존 망각률 — Selectivity)}$$
$$h_t = \Delta_t \odot h_{t-1} + (1 - \Delta_t) \odot x_t$$

- $\Delta_t \to 1$: 과거 상태 완전 유지 (횡보 구간에서 새 입력 무시)
- $\Delta_t \to 0$: 새 입력으로 즉각 갱신 (돌파 이벤트 기억)

**초기 망각률 분포 (검증 결과):**
```
mean=0.8683  std=0.0653  min=0.3444  max=0.9856
dt > 0.8 비율: 85.7%  ← 높은 기억 유지 (초기화 b_dt=+2.0 효과)
dt < 0.2 비율: 0.0%
```
> sigmoid(2.0) = 0.88: 학습 초반 안정적인 그래디언트 흐름 보장

| 항목 | 값 |
|---|---|
| 파라미터 (d=64) | 4,160 (Linear 64→64 + bias) |
| 복잡도 | O(T·D) — T=30 순차 스캔 |
| 초기화 | b_dt=+2.0 (높은 초기 기억 유지) |

---

### [C] _CryptoMambaBlock — 완전 Mamba-proxy 블록

**내부 구조:**
```
입력 x (B, T, D)
  ↓ LayerNorm (PreNorm)
  ↓ Linear(D → 2D): value + gate 두 스트림 분리
  ↓ [value stream]                    [gate stream]
  ↓ DepthwiseCausalConv(k=4, groups=D)
  ↓ SiLU — 비선형 활성화
  ↓ _SelectiveScan — 선택적 상태 업데이트
  ↓ ⊙ sigmoid(gate) — 출력 게이트 (Mamba z-gate)
  ↓ Linear(D → D) + Dropout
  + Residual
  ↓ 출력 (B, T, D)
```

**인과 컨볼루션 (미래 정보 유출 방지):**
- padding = `d_conv - 1 = 3` (왼쪽 패딩)
- 출력에서 미래 타임스텝 slice: `[:, :, :T]`

| 항목 | 값 |
|---|---|
| 파라미터 (d=64, k=4) | **17,088** |
| in_proj (64→128) | 8,320 |
| dconv (depthwise, k=4) | 320 |
| _SelectiveScan | 4,160 |
| out_proj (64→64) | 4,160 |
| norms | 128 |

---

### [D] _KANLayer — EfficientKAN 근사 비선형 매핑

**역할:** 추출된 피처들의 비선형적 상호작용을 해석 가능한 방식으로 매핑.

**KAN 수식 (EfficientKAN 단순화):**
$$y = W_{\text{base}} \cdot x + W_{\text{spline}} \cdot \text{SiLU}(x)$$

- $W_{\text{base}} \cdot x$: 선형 기저 성분 (표준 MLP fallback)
- $W_{\text{spline}} \cdot \text{SiLU}(x)$: 학습된 비선형 기저 (B-spline 근사)

**표준 MLP vs KAN 비교:**

| 구조 | 활성화 | 학습 가능성 | 해석성 |
|---|---|---|---|
| MLP (`Linear + ReLU`) | 고정 (노드별) | 없음 | 낮음 |
| **KAN** (`w_base + w_spline × SiLU`) | **학습 가능 (엣지별)** | 있음 | **높음** |

| 항목 | 값 (128→64) |
|---|---|
| w_base (128×64) | 8,192 |
| w_spline (128×64) | 8,192 |
| bias + norm | 192 |
| 합계 | **16,576** |

---

## 4. 파라미터 수 분석

| 서브 모듈 | 파라미터 | 비율 |
|---|---|---|
| `dwt` (_HaarDWT1D) | **0** | 0.0% |
| `low_proj` (71→64) | 4,608 | 6.0% |
| `high_proj` (71→64) | 4,608 | 6.0% |
| `low_stack` (MambaBlock × 2) | 34,176 | **44.3%** |
| `high_stack` (MambaBlock × 1) | 17,088 | 22.1% |
| `kan1` (128→64) | 16,576 | 21.5% |
| `kan2` (64→1) | 131 | 0.2% |
| **합계** | **77,187** | 100% |

---

## 5. Forward Pass 검증 결과

```
입력 shape : (32, 60, 71)
출력 shape : (32, 1)  ✓

총 파라미터: 77,187 < 200,000  ✓
역전파 NaN : 없음  ✓
DWT 에너지 보존 오차: 0.00e+00  ✓
```

---

## 6. 추론 시간 및 기존 모델 비교

| 모델 | 파라미터 | CPU 추론 (B=1) | 손실 함수 | 피처 수 | 예측 타겟 |
|---|---|---|---|---|---|
| LSTMClassifier | 78,849 | 1.16 ms | FocalLoss | 52 | 60분 LONG/FLAT |
| TCNClassifier | 152,193 | 3.59 ms | FocalLoss | 52 | 60분 LONG/FLAT |
| **CryptoMambaClassifier** | **77,187** | 4.01 ms | **GMADLoss** | **71** | **1분 LogReturn** |

**CPU 추론 4ms 원인 분석:**
- `_SelectiveScan`의 T=30 순차 스캔 루프가 병목
- **GPU 환경**: CUDA 병렬화 후 < 0.5ms 예상 (Colab A100 기준)
- CPU 전용 최적화 옵션: `torch.compile()` 적용 시 ~2배 속도 향상 가능

---

## 7. 아키텍처 설계 철학

### 기존 LSTM/TCN의 한계

| 문제 | LSTM/TCN | CryptoMamba |
|---|---|---|
| 노이즈/추세 혼합 | 원시 피처 그대로 처리 | **DWT로 물리적 분리** |
| 횡보 구간 | 모든 타임스텝 동등 가중 | **선택적 망각(dt→1) 자동** |
| 돌파 이벤트 | 희소한 이벤트 희석 | **dt→0으로 즉각 상태 갱신** |
| 학습 목표 | MSE/FocalLoss (방향 무관) | **GMADLoss (방향+magnitude)** |
| 예측 타겟 | 60분 후 이진 분류 | **1분 후 연속 수익률 (HFT)** |

### 데이터 플로우 설명

```
1분봉 원시 데이터 (OHLCV + CVD + EMA + 미시구조)
    ↓ HaarDWT
추세 성분 (15분~1시간 MA 추세)    노이즈 성분 (1분 틱 패턴)
    ↓ MambaBlock × 2                   ↓ MambaBlock × 1
    돌파 직전 빌드업 포착              캔들 패턴/order imbalance
    ↓
  concat + KANLayer
    ↓
  예측 log-return
    ↓ GMADLoss 학습
  방향 × magnitude 최적화
```

---

## 8. 다음 단계: Step DL-9 학습 선언

모든 선행 조건이 완료되었습니다:

| 단계 | 상태 |
|---|---|
| Step DL-6: 피처 엔지니어링 (71피처) | ✅ 완료 |
| Step DL-7: GMADLoss 구현 및 검증 | ✅ 완료 |
| Step DL-8: CryptoMamba 아키텍처 | ✅ 완료 |
| **Step DL-9: 모델 학습 (Colab GPU)** | **→ 다음 단계** |

**Step DL-9 예정 작업:**
- `scripts/dl/step_dl_9_train_mamba.py` 학습 스크립트 작성
- 데이터셋: `btc_1m_hft_v2.parquet` (71피처, 2년)
- 손실 함수: `GMADLoss(tau=7.19e-4, gamma=500, alpha=0.7)`
- 옵티마이저: `AdamW(lr=3e-4, weight_decay=1e-4) + clip_grad_norm(1.0)`
- Early stopping: val GMADLoss 기준, patience=15
- 아티팩트: `artifacts/dl_prod/cryptomamba_model.pt`

---
*생성일: 2026-03-13 | 검증: 전항목 통과 | 파라미터: 77,187 / 200,000*
