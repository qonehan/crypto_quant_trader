# Step DL-5: TCN (Temporal Convolutional Network) 구현 보고서

## 1. 목적 및 동기

| 항목 | 기존 (DL-3) | DL-5 |
|---|---|---|
| 모델 | 1D-CNN + LSTM | TCN |
| 핵심 지표 | Val Loss 최소화 | **Val Precision 최대화** |
| 학습 방식 | 순차 처리 (LSTM) | 완전 병렬 (Dilated Conv) |
| 환경 | GPU 우선 | 1GB CPU 서버 안정화 |

현물 롱 온리 전략에서 **가짜 LONG 신호(False Positive)** 는 직접적 손실로 이어진다.
DL-5는 Precision을 Early Stopping의 제1 지표로 채택하여 이 문제를 구조적으로 해결한다.

---

## 2. TCN 아키텍처

### 2-1. 전체 구조

```
Input: (Batch, SeqLen=60, Features=52)
       │
       ▼  transpose → (B, F=52, T=60)
┌─────────────────────────────────────────────────────┐
│  TemporalBlock × 6  (dilations = [1, 2, 4, 8, 16, 32])
│                                                     │
│  각 블록:                                            │
│    WeightNorm-Conv1d → Chomp1d → ReLU → Dropout     │
│    WeightNorm-Conv1d → Chomp1d → ReLU → Dropout     │
│    + Residual Connection (1×1 Conv if ch mismatch)  │
└─────────────────────────────────────────────────────┘
       │  (B, n_channels=64, T=60)
       ▼  [:, :, -1]  마지막 타임스텝
       (B, 64)
       │
┌─────────────┐
│  Head       │  Linear(64→32) → ReLU → Dropout(0.2) → Linear(32→1)
└─────────────┘
       │
       ▼
  Logit (B, 1)  →  sigmoid  →  prob  ─── threshold=0.55 → LONG/FLAT
```

### 2-2. 핵심 구성요소

| 구성요소 | 역할 |
|---|---|
| **Causal Padding + Chomp1d** | 미래 정보 유출 차단 (시점 t는 t-1 이하만 참조) |
| **Dilated Conv** | 지수적으로 넓어지는 수용 영역 — 장기 패턴 포착 |
| **Weight Normalization** | 학습 안정성 향상, 경사 소실/폭발 억제 |
| **Residual Connection** | 깊은 층에서도 그래디언트 흐름 보장 |

### 2-3. 하이퍼파라미터

```python
N_CHANNELS      = 64      # 모든 블록의 채널 수
KERNEL_SIZE     = 3
DILATIONS       = [1, 2, 4, 8, 16, 32]   # 6개 블록
DROPOUT         = 0.2
LR              = 2e-4
POS_WEIGHT_CAP  = 1.0     # Precision 우선: positive 과도 업가중 억제
TRAIN_STRIDE    = 2       # DL-3 stride=3 → 더 많은 학습 샘플
```

---

## 3. 수용 영역 (Receptive Field) 계산

TCN의 이론적 수용 영역은 다음 공식으로 계산된다:

$$RF = 1 + 2 \times (k-1) \times \sum_{i} d_i$$

| 변수 | 값 |
|---|---|
| $k$ (kernel size) | 3 |
| Conv 레이어/블록 | 2 |
| Dilations | [1, 2, 4, 8, 16, 32] |
| $\sum d_i$ | 1+2+4+8+16+32 = **63** |

$$RF = 1 + 2 \times (3-1) \times 63 = 1 + 252 = \boxed{253 \text{ timesteps} \approx 4.2\text{시간}}$$

### 블록별 수용 영역 누적

| 블록 | Dilation | 블록 추가분 | 누적 RF |
|---|---|---|---|
| Block 1 | 1  | 2×(3-1)×1 = 4    | **5** |
| Block 2 | 2  | 2×2×2 = 8        | **13** |
| Block 3 | 4  | 2×2×4 = 16       | **29** |
| Block 4 | 8  | 2×2×8 = 32       | **61** |
| Block 5 | 16 | 2×2×16 = 64      | **125** |
| Block 6 | 32 | 2×2×32 = 128     | **253** |

> **해석**: SEQ_LEN=60이므로 현재 입력 범위 내에서 Block 4(RF=61)까지 완전히 활용됨.
> Block 5·6은 SEQ_LEN을 초과하지만 잔차 연결로 여전히 특징 추출에 기여.
> **SEQ_LEN을 120~240으로 확장하면 TCN의 장기 패턴 포착 능력이 극대화됨.**

---

## 4. 파라미터 수 비교

```
TCN  (DL-5) : 152,193  파라미터  (weight_norm 포함)
LSTM (DL-3) :  78,849  파라미터
```

TCN이 약 **1.93× 더 많은 파라미터**를 보유하지만, 병렬 연산으로 배치 학습 속도는 빠르다.

---

## 5. 추론 속도 비교 (CPU, 단일 샘플)

> 측정 환경: CPU (Intel), 단일 샘플 (1×60×52), 500회 평균

| 모델 | ms/inference | 상대 속도 |
|---|---|---|
| LSTM (DL-3) | 1.15 ms | 기준 (1×) |
| **TCN (DL-5)** | 5.20 ms | 0.22× (4.5배 느림) |

### CPU에서 TCN이 느린 이유

| 원인 | 설명 |
|---|---|
| **Weight Normalization 오버헤드** | 추론 시에도 weight_g × weight_v/‖v‖ 재계산 |
| **Dilated Conv 메모리 접근** | 비연속 메모리 접근 패턴 → CPU 캐시 미스 |
| **파라미터 수** | LSTM의 1.93× → 행렬 연산량 증가 |

> **GPU 환경에서는 역전됨**: Dilated Conv는 CUDA 커널에서 완전 병렬화 → TCN이 LSTM보다 **2~5× 빠름** (배치 크기 클수록 격차 커짐).
> 1GB CPU 서버에서는 단일 샘플 추론 시 5ms로도 매분 루프에 영향 없음 (루프 주기=60,000ms).

---

## 6. Precision 비교 결과

> LSTM 기준치: Step DL-3 학습 결과 (threshold=0.55)

| 지표 | LSTM (DL-3) | TCN (DL-5) |
|---|---|---|
| Test Precision | **60.58%** | *학습 후 기록* |
| Test Recall | — | *학습 후 기록* |
| Test F1 | — | *학습 후 기록* |
| Test AUC-ROC | — | *학습 후 기록* |
| 최적 Threshold | 0.55 | *threshold sweep 후 기록* |
| 학습 에포크 | — | *학습 후 기록* |

> `step_dl_5_train_tcn.py` 실행 완료 후 `artifacts/dl_prod/tcn_train_result.json`에 전체 수치 기록됨.

### Precision 우선 Early Stopping 설계

```python
# 재현율 최소 보장 조건 충족 시 Precision 최대화
if val_recall >= 0.05 and val_precision > best_precision:
    save_model()
# 단 한 번도 조건 미충족 시 Val Loss로 폴백
elif val_loss < best_loss and best_precision == 0.0:
    save_model()
```

이 설계는 "절대 LONG 예측 안 함"이라는 trivial 해를 방지하면서
Precision을 직접 최적화한다.

---

## 7. TCN의 LSTM 대비 구조적 장점

| 항목 | LSTM | TCN |
|---|---|---|
| 장기 의존성 | 은닉 상태(hidden state) 압축 | **수용 영역 내 직접 참조** |
| 경사 흐름 | Vanishing gradient 위험 | Residual + weight_norm으로 안전 |
| 학습 병렬성 | 순차적 (병렬화 불가) | **완전 병렬화** (GPU 효율 극대화) |
| 재현 가능성 | 은닉 상태 의존 | 결정론적 (같은 입력 = 같은 출력) |
| 하이퍼파라미터 | hidden_size, num_layers | n_channels, kernel_size, dilations |

---

## 8. 아티팩트 경로

```
artifacts/dl_prod/
  ├── tcn_model.pt          ← TCN 학습 가중치
  ├── tcn_model_meta.json   ← n_features, n_channels, kernel_size, dropout
  └── tcn_train_result.json ← 전체 학습 결과 (Precision, AUC, 추론속도 등)
```

---

## 9. 실행 방법

```bash
# TCN 학습 (GPU 있으면 자동 감지)
PYTHONPATH=. poetry run python scripts/dl/step_dl_5_train_tcn.py

# 학습 결과 확인
cat artifacts/dl_prod/tcn_train_result.json
```

---

## 10. 다음 단계 선언 — 실시간 봇 교체

Step DL-5 학습이 완료되면 즉시 실시간 모의투자 봇에 TCN 모델을 탑재한다.

### 교체 방법 (step_dl_4_mock_trade.py)

```python
# 기존 (DL-3 LSTM)
from app.predictor.dl_model import LSTMClassifier
model = LSTMClassifier.load(MODEL_PATH, MODEL_META_PATH, device=DEVICE)

# 교체 후 (DL-5 TCN)
from app.predictor.dl_model import TCNClassifier, TCN_MODEL_PATH, TCN_MODEL_META_PATH
model = TCNClassifier.load(TCN_MODEL_PATH, TCN_MODEL_META_PATH, device=DEVICE)
```

`forward()` 시그니처가 동일(`(B, T, F) → (B, 1)`)하므로
**추론 파이프라인 코드 변경 없이** 모델만 교체 가능.

### 판단 기준

| 조건 | 결정 |
|---|---|
| TCN Precision > LSTM Precision | TCN으로 완전 교체 |
| TCN Precision ≈ LSTM Precision | 앙상블(AND 조건) 검토 |
| TCN Precision < LSTM Precision | 하이퍼파라미터 재조정 후 재학습 |
