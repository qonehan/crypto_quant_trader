# Step DL-7: GMADL 손실 함수 구현 보고서
**Generalized Mean Absolute Directional Loss — PyTorch 구현 및 검증**

---

## 1. 구현 요약

| 항목 | 내용 |
|---|---|
| 파일 | `app/predictor/losses.py` |
| 클래스 | `GMADLoss(nn.Module)` |
| 검증 스크립트 | `scripts/dl/step_dl_7_test_gmadl.py` |
| 역전파 NaN | **전 케이스 없음** (6개 엣지 케이스 통과) |

---

## 2. 수식 및 구현

### 2-1. GMADL 완전 수식

$$\text{GMADL}(\hat{y}, y) = \frac{1}{N} \sum_{i=1}^{N} w_i \cdot \left[\alpha \cdot L_{\text{dir},i} + (1-\alpha) \cdot L_{\text{smooth},i}\right]$$

**가중치 (Magnitude + Exponential Smoothing):**
$$w_i = |y_i|^\beta \cdot \exp\!\left(\min(\gamma \cdot |y_i|,\; 20)\right)$$

- $|y_i|^\beta$: 변동폭 선형 가중 ($\beta=1.0$)
- $\exp(\gamma \cdot |y_i|)$: **지수 평활화** — 이상 변동 구간($|y|>0.1\%$)을 지수적으로 강조
- `max=20` 클램핑: float32 오버플로우 방지 ($e^{20} \approx 4.85 \times 10^8$)

**방향성 손실 ($L_{\text{dir}}$):**
$$L_{\text{dir},i} = 1 - \tanh\!\left(\frac{\hat{y}_i \cdot y_i}{\tau^2}\right) \in [0, 2]$$

- $\hat{y} \cdot y > 0$ (방향 일치): $\tanh(+) > 0 \Rightarrow L_{\text{dir}} < 1$ → **보상**
- $\hat{y} \cdot y < 0$ (방향 불일치): $\tanh(-) < 0 \Rightarrow L_{\text{dir}} > 1$ → **페널티**
- $|\hat{y} \cdot y| \ll \tau^2$ (노이즈 수준): $\tanh(\approx 0) \Rightarrow L_{\text{dir}} \approx 1$ → **중립**

> $\tau = 7.19 \times 10^{-4}$ (future_ret_1의 표준편차). `tanh`는 전구간 미분 가능하므로 역전파 시 NaN 발생 없음.

**안정화 손실 ($L_{\text{smooth}}$):**
$$L_{\text{smooth},i} = \text{SmoothL1}(\hat{y}_i, y_i;\; \beta_s = 10^{-4})$$

$$= \begin{cases} \frac{0.5 \cdot (\hat{y}-y)^2}{\beta_s} & |{}\hat{y}-y{}| < \beta_s \\ |\hat{y}-y| - 0.5\beta_s & |{}\hat{y}-y{}| \ge \beta_s \end{cases}$$

2차 → 1차 전환점 $\beta_s = 10^{-4}$ (0.01%): 소규모 오차는 제곱으로 부드럽게, 대규모 오차는 선형으로 gradient 안정화.

---

### 2-2. 핵심 PyTorch 구현 (losses.py 발췌)

```python
# ① Magnitude Weight
abs_y = y_true.abs()
exp_term = torch.exp(torch.clamp(self.gamma * abs_y, max=20.0))
w = abs_y.pow(self.beta) * exp_term          # 지수 평활화 가중치

if self.normalize_w:
    w = w / (w.mean().detach() + 1e-8)       # 배치 스케일 안정화

# ② Directional Component  (tanh 커널 — 전구간 미분 가능)
direction_kernel = y_pred * y_true / self.tau_sq
L_dir = 1.0 - torch.tanh(direction_kernel)   # ∈ [0, 2]

# ③ Smooth L1 (Huber) — gradient 안정화 보조항
L_smooth = F.smooth_l1_loss(y_pred, y_true, reduction="none", beta=self.smooth_beta)

# ④ 결합
loss = w * (self.alpha * L_dir + (1.0 - self.alpha) * L_smooth)
return loss.mean()
```

---

## 3. 3-케이스 검증 결과

| 케이스 | 방향 | Magnitude | GMADL | MSE | 예상 |
|---|---|---|---|---|---|
| Case 1: y=0.005, ŷ=0.004 | **일치** | 큼 | **0.000017** | 1.00e-06 | 최소 ✓ |
| Case 2: y=0.0001, ŷ=-0.0002 | **불일치** | 작음 | 0.000076 | 9.00e-08 | 중간 ✓ |
| Case 3: y=0.005, ŷ=-0.004 | **불일치** | 큼 | **0.085441** | 8.10e-05 | 극대 ✓ |

### GMADL vs MSE 순위 비교

| 지표 | 1위(낮음) | 2위 | 3위(높음) |
|---|---|---|---|
| **GMADL** | **Case 1** (방향 일치 + 큰 이동) | Case 2 (방향 틀림 + 노이즈) | **Case 3** (방향 틀림 + 큰 이동) |
| MSE | Case 2 | Case 1 | Case 3 |

**핵심 차이:**
- MSE는 Case 2(오차 작음) < Case 1(오차 큼): **방향 무시, 오차 크기만 본다**
- GMADL은 Case 1 < Case 2: **방향 일치를 보상, 방향 불일치를 페널티**

| 비율 | GMADL | MSE |
|---|---|---|
| Case3 / Case1 (치명적 오답 vs 정답) | **4,922배** | 81배 |
| Case2 / Case1 (노이즈 방향 틀림 vs 정답) | 4.4배 | 0.09배 |

---

## 4. 역전파 NaN 검증

6개 엣지 케이스 전부 NaN 없이 역전파 성공:

```
✓ 정상 케이스            loss=+0.000017  grad=-0.018274
✓ 제로 예측              loss=+0.042729  grad=-412.4      ← 큰 gradient (클리핑 필요)
✓ 제로 타겟 (노이즈)     loss=+0.000000  grad=+0.000000
✓ 제로 예측+타겟         loss=+0.000000  grad=+0.000000
✓ 양쪽 반대 방향         loss=+0.085459  grad=-0.018274
✓ 극단값 예측            loss=+34688948  grad=+7277478    ← 극단 outlier
```

> **주의:** `제로 예측` 케이스(gradient=-412)와 `극단값` 케이스에서 gradient가 큼.
> 학습 시 `torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)` 적용 권장.

---

## 5. 배치 통계 — '항상 0 예측' 억제 효과

| 예측 전략 | GMADL | 방향 정확도 |
|---|---|---|
| 좋은 예측 (노이즈 20%) | **0.161** | **94.9%** |
| 랜덤 예측 | 0.640 | 49.1% |
| **항상 0 예측 (안전한 횡보)** | **0.701** | N/A |

> **MSE의 함정:** MSE 최소화 전략은 '항상 0 예측' (평균 예측)으로 수렴하는 경향 있음.
> GMADL은 '항상 0'이 랜덤 예측보다도 높은 손실을 받아 **안전한 횡보 예측을 능동적으로 억제**.

---

## 6. γ 민감도 분석 — 지수 평활화 강도

| γ | Case1 (노이즈 방향틀림) | Case3 (돌파 방향틀림) | C3/C1 배율 |
|---|---|---|---|
| 0 (비활성) | 0.000073 | 0.007013 | 96.5× |
| 100 | 0.000073 | 0.011563 | 157× |
| **500 (권장)** | **0.000076** | **0.085441** | **1,118×** |
| 1000 | 0.000080 | 1.040884 | 12,953× |
| 2000 | 0.000089 | 154.5 | 1,739,387× |

**γ=500 선택 근거:**
- 0.1% 이동($|y|=0.001$): 가중치 $\times 1.65$ (적절한 강조)
- 0.5% 이동($|y|=0.005$): 가중치 $\times 12.2$ (돌파 구간 집중)
- 5.0% 이동($|y|=0.05$): 클램핑($e^{20}$)으로 안정 유지

---

## 7. 모델 학습 메커니즘 — '안전한 횡보 억제'

### 기존 MSE/MAE의 문제

```
1분봉 노이즈 구간 (93.5%):  작은 오차 다수
돌파 이벤트 구간 (6.5%):  큰 오차 소수

MSE → 노이즈 93.5%의 오차를 줄이는 것이 더 유리
    → 모델이 '항상 0 또는 작은 값'으로 수렴하는 안전한 전략 채택
    → 정작 수익이 나는 돌파 구간에서 예측 포기
```

### GMADL의 해결책

```
w_i = |y_i|^β × exp(γ|y_i|)

돌파 구간(|y|=0.5%): w = 0.005 × exp(2.5) ≈ 0.061
노이즈 구간(|y|=0.01%): w = 0.0001 × exp(0.05) ≈ 0.000105

→ 돌파 구간의 손실 가중치가 581배 높음

+ 방향성 항 L_dir: 이 구간에서 방향을 틀리면 최대 페널티(×2)
                   방향을 맞추면 최소 손실(≈0)

결과: 모델이 "노이즈 구간은 0 예측 = 패널티 없음"이 아닌
      "돌파 구간에서 정확한 방향 예측 = 큰 보상" 전략 채택
```

---

## 8. 하이퍼파라미터 권장값

| 파라미터 | 권장값 | 설명 |
|---|---|---|
| `tau` | `std(future_ret_1)` | 데이터셋별 자동 계산 |
| `beta` | 1.0 | 선형 magnitude 가중 |
| `gamma` | 500.0 | 지수 평활화 강도 |
| `alpha` | 0.70 | L_dir 비율 (방향 70% + smooth 30%) |
| `smooth_beta` | 1e-4 | SmoothL1 전환점 (0.01%) |
| Grad Clip | `max_norm=1.0` | 학습 시 필수 (극단값 gradient 대응) |

---

## 9. 다음 단계

이 보고서에 대한 **승인 후** Step DL-8(CryptoMamba 아키텍처)을 진행합니다.

- **Step DL-8**: `app/predictor/dl_model.py` — DWT 분해 + Mamba SSM + KAN Mixer
- 학습 손실: `GMADLoss(tau=std(future_ret_1), gamma=500)`
- 입력 피처: `btc_1m_hft_v2.parquet` 71컬럼

---
*생성일: 2026-03-13 | 검증: 6/6 엣지케이스 통과, NaN 없음*
