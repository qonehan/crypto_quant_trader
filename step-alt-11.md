# Step ALT-11 — Baseline 대비 개선도 + Walk-forward + Gate 실험 결과보고서

작성일: 2026-02-24
브랜치: copilot/review-alt-3-changes
환경: GitHub Codespaces (Linux 6.8, Python 3.11, poetry)

---

## 0) 변경사항 요약

| 파일 | 변경 내용 |
|------|----------|
| `scripts/walkforward_ridge.py` | 신규 생성 — fold별 IC/sign_acc/RMSE 기록 |
| `scripts/export_dataset.py` | Option A 적용: `cost_roundtrip_est = (10 + spread_bps) / 10000` 컬럼 추가 (step [4] 직전) |

---

## 1) 대상 데이터

| 항목 | h600 | h900 |
|------|------|------|
| 파일 | btc_1h_h600.parquet | btc_1h_h900.parquet |
| rows | 417 | 357 |
| feature_count | 11 | 11 |
| time range | ~2026-02-22 12:00 (17분 분량) | ~2026-02-22 12:00 (17분 분량) |
| `cost_roundtrip_est` 포함 여부 | **기존 없음 → Option A 인-메모리 적용** | 동일 |

**Option A 비용 공식:**
```
cost_roundtrip_est = (fee_bps_roundtrip=10 + spread_bps) / 10_000
```
- 실측 cost: mean=0.001111 (0.111%), min=0.00101, max=0.00158
- Binance taker 왕복 ~8-10 bps + spread ~0.1-0.2 bps

---

## 2) Baseline 대비 성능 (Test, hold-out)

### h600 (test_n=63)

| name | RMSE | MAE | IC(pearson) | sign_acc |
|------|------|-----|-------------|----------|
| model_ridge (alpha=50) | 0.001447 | 0.001314 | **+0.4447** | **0.8413** |
| baseline_0 (항상 0) | 0.000345 | 0.000328 | NaN | 0.0000 |
| baseline_mean (train 평균) | 0.000430 | 0.000367 | NaN | 0.8413 |

- **RMSE 개선율 vs baseline_0: -319.55%** (모델이 RMSE 기준으로 훨씬 나쁨)
- **RMSE 개선율 vs baseline_mean: -236.13%** (동일하게 나쁨)

**해석:** IC=+0.44, sign_acc=84%이지만 RMSE는 naive_0 대비 3배 이상 나쁨.
sign_acc가 baseline_mean과 동일한 점이 결정적: 테스트 구간 자체가 일방향(하락) 편향 시장.
모델은 방향을 맞추지만 magnitude를 크게 과대예측 → RMSE 악화.
Ridge의 shrinkage(alpha=50)에도 불구하고 예측 스케일이 실제 수익률 스케일(~0.03%)보다 훨씬 큼.

### h900 (test_n=54)

| name | RMSE | MAE | IC(pearson) | sign_acc |
|------|------|-----|-------------|----------|
| model_ridge (alpha=50) | 0.000975 | 0.000883 | **+0.7305** | 0.5741 |
| baseline_0 (항상 0) | 0.000578 | 0.000558 | NaN | 0.0000 |
| baseline_mean (train 평균) | 0.001223 | 0.001085 | ~0 | 0.5741 |

- **RMSE 개선율 vs baseline_0: -68.77%** (여전히 나쁨)
- **RMSE 개선율 vs baseline_mean: +20.24%** (baseline_mean 대비는 유일하게 개선)

**해석:** IC=+0.73(가장 높음)이지만 RMSE는 naive_0보다 나쁨.
h900이 h600보다 안정적 신호를 가지고 있으나, 모델 magnitude 예측이 맞지 않는 문제는 동일.
RMSE 기준으로는 두 horizon 모두 "항상 0 예측"이 best baseline.

---

## 3) Walk-forward 결과

### h600 (alpha=50, min_train=200, test_size=50, step=50)

| fold | train_n | test_n | RMSE | RMSE_naive0 | RMSE_vs_naive_% | IC | sign_acc |
|------|---------|--------|------|-------------|----------------|----|----------|
| 1 | 200 | 50 | 0.000739 | 0.000605 | **-22.06%** | **+0.790** | 0.74 |
| 2 | 250 | 50 | 0.000673 | 0.000242 | -178.60% | **+0.629** | 0.86 |
| 3 | 300 | 50 | 0.000737 | 0.000582 | -26.69% | **+0.459** | 0.30 |
| 4 | 350 | 50 | 0.001127 | 0.000387 | -191.61% | **-0.190** | 1.00 |

**Mean/Std:**

| 지표 | 평균 | 표준편차 |
|------|------|---------|
| RMSE | 0.000819 | 0.000208 |
| IC | **+0.422** | **0.430** |
| sign_acc | 0.725 | 0.303 |

**해석:**
- IC 평균 +0.42이지만 std=0.43으로 매우 불안정 (fold 4에서 -0.19로 역전)
- sign_acc도 0.30~1.00으로 swing이 큼
- fold 4에서 sign_acc=1.00 & IC=-0.19: 해당 구간 y_true가 모두 같은 부호이나 예측 순위가 역순 → 시장이 단방향일 때 IC 신뢰도 낮음

### h900 (alpha=50, min_train=200, test_size=50, step=50)

| fold | train_n | test_n | RMSE | RMSE_naive0 | RMSE_vs_naive_% | IC | sign_acc |
|------|---------|--------|------|-------------|----------------|----|----------|
| 1 | 200 | 50 | 0.000531 | 0.000633 | **+16.17%** | **+0.442** | 0.98 |
| 2 | 250 | 50 | 0.000873 | 0.000464 | -88.39% | **+0.456** | 0.12 |
| 3 | 300 | 50 | 0.000706 | 0.000566 | -24.74% | **+0.814** | 0.58 |

**Mean/Std:**

| 지표 | 평균 | 표준편차 |
|------|------|---------|
| RMSE | 0.000703 | 0.000171 |
| IC | **+0.571** | **0.211** |
| sign_acc | 0.560 | 0.430 |

**해석:**
- **h900이 h600보다 IC 안정성 우수**: IC가 모든 fold에서 양수 유지(+0.44 ~ +0.81)
- RMSE는 fold 1에서만 naive_0보다 좋음(-16.17% 개선)
- sign_acc 분산 여전히 큼 (0.12~0.98) — 단기 구간별 시장 방향성 편향이 지배

**데이터 경고:** 전체 데이터가 2026-02-22 약 17분 분량의 5초 데이터이므로 fold당 test 50개 = 4분. 통계적 신뢰도 매우 낮음. h900 IC 안정성은 긍정 신호이지만, **더 긴 데이터 수집 후 재검증 필수**.

---

## 4) Gate 실험

### p_none 분포 분석

- **train+valid(~354행)**: q50=0.981, q90=0.990, q95=0.990, q99=0.990, max=1.000
- **test(63행)**: min=0.976, max=0.990, mean=0.988
- **결론: p_none이 항상 ≥ 0.976** — 업스트림 분류 모델이 구조적으로 p_none≥0.97 출력

어떤 quantile 기반 임계값(q90=0.99, q95=0.99, q99=0.99)도 test의 p_none 범위(0.976~0.990)와 겹쳐서 **gate가 모두 실패**.

### Calibration (valid → test)

- valid에서 계산한 k = **-0.065** (음수!)
- 원인: valid 구간과 test 구간 시장 동역학이 다르다 → valid에서 fitting한 보정값이 test에서 역방향
- calibration 적용 후 IC = -0.445 (부호 반전) → calibration 미적용이 나음

### Gate 실험 전체 결과 (h600 test, 63행)

| Gate 구성 | trades | win_rate(gross) | win_rate(net) | total_pnl_gross | total_pnl_net |
|-----------|--------|-----------------|---------------|-----------------|---------------|
| raw \| p_none<0.70 | **0** | — | — | 0 | 0 |
| raw \| p_none<q95=0.99 | **0** | — | — | 0 | 0 |
| raw \| p_none<1.00 (gate off) | **30** | **1.000** | **0.000** | **+0.01075** | **-0.02413** |
| cal(k=-0.065) \| p_none<1.00 | **0** | — | — | 0 | 0 |

**핵심 관찰:**
1. p_none 게이트는 ≥0.70 어떤 임계값도 test에서 작동 안 함 (p_none_min_test=0.976)
2. 게이트 완전 해제(p_none<1.0) + raw prediction → 30 trades, win_rate_gross=100%
3. **비용(avg 0.00116/trade) 포함 시 win_rate_net=0%, total_pnl_net=-0.024** — 비용이 gross PnL을 완전히 흡수
   - avg gross PnL/trade ≈ 0.000358 (0.036%)
   - avg cost/trade ≈ 0.001163 (0.116%) → 비용이 수익의 **3.3배**

---

## 5) 비용 포함 처리

**선택: 옵션 A**

**`scripts/export_dataset.py`에 적용 완료 (step [4] 직전에 삽입):**

```python
FEE_BPS_ROUNDTRIP = 10
if "spread_bps" in dataset.columns:
    dataset["cost_roundtrip_est"] = (FEE_BPS_ROUNDTRIP + dataset["spread_bps"].fillna(0)) / 10_000
else:
    dataset["cost_roundtrip_est"] = FEE_BPS_ROUNDTRIP / 10_000
```

**적용 효과:** 다음 export 실행 시 parquet에 `cost_roundtrip_est` 컬럼이 자동 포함됨.

**현재 trade sim 변화:** 비용 포함 시 총 PnL이 +0.0107 → **-0.0241** 로 역전. 비용이 신호를 압도하는 구조.

---

## 6) Coinglass 상태

- `coinglass_check`: **FAIL** (지속)
- 원인: `COINGLASS_API_KEY` 미설정
- ML 검증(ALT-11)은 Coinglass 없이 완료
- 키 확보 시점에 환경변수 설정 후 `run_pipeline_checks` 재실행으로 운영 게이트 달성 가능

---

## 7) 결론 — 다음 Step에서 할 일 3가지

### 핵심 문제 정리

| 문제 | 현황 | 심각도 |
|------|------|--------|
| 데이터 양 부족 | 전체 ~17분 분량의 5초 데이터 | **HIGH** — 통계적 의미 없음 |
| 비용 > 신호 | avg_cost=0.116% vs avg_gross_pnl=0.036% | **HIGH** — 현재 전략 수익 불가 |
| p_none 게이트 구조적 실패 | p_none 항상 ≥ 0.976, 기본 게이트 작동 안 함 | **HIGH** |
| RMSE baseline 대비 열세 | magnitude 예측 부재 | **MEDIUM** |
| IC 불안정 (h600) | fold별 -0.19 ~ +0.79 | **MEDIUM** |

### 다음 Step(ALT-12)에서 할 일 3가지

1. **데이터 대폭 확장 후 재학습**: 봇을 최소 24~48시간 추가 실행해 10,000행+ 확보 후 ALT-10/11 전 과정을 재실행. 현재 결과는 17분 데이터 기반으로 통계적 의미가 없음.

2. **비용 대비 수익률 개선 방향 탐색**: 두 가지 중 하나 선택 — (A) 비용을 낮추는 maker 주문 전략으로 전환(fee_bps 4 이하) 또는 (B) 더 큰 수익률을 기대할 수 있는 longer horizon(h900 이상) 집중 + 신호 임계값을 r_t × 2 이상으로 강화해 cost coverage 확보.

3. **p_none 게이트 근본 재설계**: 업스트림 분류 모델이 항상 p_none≥0.97을 출력하는 원인 파악 — action_hat 분포 점검, 모델 confidence calibration 적용, 또는 p_none gate를 폐기하고 회귀 예측값 기반(예: |y_pred|/r_t > 1.5) 독립 게이트로 교체.
