# Step ALT-10 — 회귀 베이스라인 학습 + 미니 백테스트 결과보고서

작성일: 2026-02-24
브랜치: copilot/review-alt-3-changes
환경: GitHub Codespaces (Linux 6.8, Python 3.11, poetry)

---

## 0) 사전 요약

- 의존성 설치: `scikit-learn 1.8.0`, `joblib 1.5.3` poetry dev group에 추가
- 학습 스크립트 생성: `scripts/train_regression_baseline.py`
- 버그 수정: `np.issubdtype`이 tz-aware `datetime64[ns, UTC]` 컬럼에서 `TypeError` 발생 → `try/except`로 안전 처리
- 4개 horizon 모두 학습 완료: h120 / h300 / h600 / h900

---

## 1) 입력 데이터

| 항목 | h120 | h300 | h600 | h900 |
|------|------|------|------|------|
| 파일 | btc_1h_h120.parquet | btc_1h_h300.parquet | btc_1h_h600.parquet | btc_1h_h900.parquet |
| rows | 512 | 477 | 417 | 357 |
| cols | 18 | 18 | 18 | 18 |
| target | label_return | label_return | label_return | label_return |
| time_col | ts | ts | ts | ts |
| feature_count | 11 | 11 | 11 | 11 |

**사용 feature(공통 11개):** `p_up`, `p_down`, `p_none`, `ev`, `ev_rate`, `r_t`, `z_barrier`, `spread_bps`, `mom_z`, `imb_notional_top5`, `entry_mid`
(누수 위험 컬럼 제외: `label_ts`, `future_mid`, `symbol`, `ts`, `action_hat`, `model_version`)

`cost_roundtrip_est`: **컬럼 없음** → 비용=0 고정 (명시)

---

## 2) 모델/학습 설정

- 모델: Ridge + StandardScaler (Pipeline)
- alpha 후보: [0.1, 1.0, 10.0, 50.0] — validation RMSE 최소값으로 선택
- **선택 alpha: 50.0 (모든 horizon 공통)**
- split: train 70% / valid 15% / test 15% (시간순)

| horizon | train_end | valid_end | test_n |
|---------|-----------|-----------|--------|
| h120 | 358 | 435 | 77 |
| h300 | 333 | 405 | 72 |
| h600 | 291 | 354 | 63 |
| h900 | 249 | 303 | 54 |

---

## 3) Test 성능

| horizon | RMSE | MAE | IC(pearson) | sign_accuracy |
|---------|------|-----|-------------|---------------|
| h120 | 0.000376 | 0.000306 | **-0.104** | 0.519 |
| h300 | 0.000602 | 0.000494 | **-0.317** | 0.528 |
| **h600** | 0.001447 | 0.001314 | **+0.445** | **0.841** |
| **h900** | 0.000975 | 0.000883 | **+0.730** | 0.574 |

**해석:**
- h120, h300: IC < 0 → 역상관. 짧은 horizon에서 Ridge가 신호를 잡지 못함
- **h600**: IC=+0.44, sign_acc=84% → 의미 있는 방향성 신호 존재
- **h900**: IC=+0.73 (가장 높음) → 장기 horizon에서 강한 상관관계
- alpha=50이 공통 선택된 것은 데이터(test_n=54~77)가 적어 강한 정규화가 유리함을 시사

---

## 4) Trade Sim (test only, non-overlap)

### 4-1) p_none_max=0.7 (원래 게이트)

| horizon | trades | win_rate | avg_pnl_net/trade | total_pnl_net |
|---------|--------|----------|-------------------|---------------|
| h120 | **0** | — | — | 0.0 |
| h300 | **0** | — | — | 0.0 |
| h600 | **0** | — | — | 0.0 |
| h900 | **0** | — | — | 0.0 |

**원인 분석 — 2가지 게이트 동시 실패:**

1. **p_none 게이트 (주요 원인):** test 구간 모든 row에서 `p_none >= 0.7` → `gate_ok=False`로 진입 차단
   - 업스트림 분류 모델이 보수적으로 설정되어 거의 항상 "액션 없음"을 높은 확률로 예측
2. **|y_pred| < r_t:** Ridge alpha=50의 강한 수축(shrinkage)으로 `|y_pred|`가 `r_t`(평균 ~0.00167) 이하로 압축됨
   - h120: `|y_pred|_max = 0.000307`, `r_t_min = 0.001551` → 한 건도 초과 못 함
   - h900: `|y_pred|_max = 0.001542`, `r_t_min = 0.001551` → 0.000009 차이로 미달

### 4-2) p_none_max=1.0 (p_none 게이트 비활성화)

| horizon | trades | win_rate | avg_pnl_net/trade | total_pnl_net |
|---------|--------|----------|-------------------|---------------|
| h120 | 0 | — | — | 0.0 |
| h300 | 0 | — | — | 0.0 |
| **h600** | **1** | **1.0** | **+0.000270** | **+0.000270** |
| h900 | 0 | — | — | 0.0 |

- h600에서 1건 트레이드 발생, PnL 양수 (작은 샘플이므로 통계적 의미는 제한적)
- horizon: `h120=120s, h300=300s, h600=600s, h900=900s`
- gate: `y_pred > r_t`(롱) / `y_pred < -r_t`(숏)
- cost: `cost_roundtrip_est` 컬럼 없음 → **0으로 고정**
- 아티팩트: `artifacts/ml1/{h}/` 및 `artifacts/ml1/{h}_gate1/`

---

## 5) Coinglass 상태 (운영 게이트)

- `coinglass_check`: **FAIL** (지속)
- 원인: `COINGLASS_API_KEY` 미설정 / `COINGLASS_ENABLED` 미활성화
- 정책상 실수집 강제 → 키 없으면 구조적으로 PASS 불가
- **ML-1(회귀 학습)은 Coinglass 없이 완료 가능** (본 단계에서 진행)
- 다음 조치: 라이브 의사결정 통합 전 실제 API 키 발급 및 환경변수 설정 필요

---

## 6) 결론

### 주요 발견

| 구분 | 내용 |
|------|------|
| 최우선 horizon | **h600** (IC=+0.44, sign_acc=84%) |
| 차순위 | **h900** (IC=+0.73, 그러나 test 샘플=54개로 과신 금지) |
| h120/h300 | IC < 0 → 단기 horizon에서 Ridge는 유효 신호 없음 |
| 트레이드 게이트 | p_none 게이트가 실질적 병목 (항상 p_none≥0.7) |
| 데이터 양 | test_n=54~77건으로 통계적 신뢰도 낮음 → 더 긴 데이터 수집 필요 |

### 다음 Step(ALT-11)에서 할 일 3가지:

1. **Naive baseline(항상 0 예측) 대비 개선도 측정**: RMSE_naive vs RMSE_model → 실질적 예측력 수치화
2. **피처 중요도/누수 재확인**: Ridge coefficient 분석으로 `entry_mid` 등 quasi-price 컬럼의 미래 정보 포함 여부 재검증, Walk-forward 검증(Expanding Window) 추가
3. **p_none 게이트 정책 재설계**: 업스트림 분류 모델의 `p_none` 분포가 항상 0.7+ → 게이트 임계값 데이터 기반으로 재보정하거나, `p_none` 대신 `|y_pred| / r_t` 비율 기반 게이트로 교체 검토

---

## 7) 아티팩트 목록

```
artifacts/ml1/
├── h120/   ridge_model.joblib, metrics.json, test_trades.csv, feature_cols.json
├── h300/   ridge_model.joblib, metrics.json, test_trades.csv, feature_cols.json
├── h600/   ridge_model.joblib, metrics.json, test_trades.csv, feature_cols.json  ← 최우선
├── h900/   ridge_model.joblib, metrics.json, test_trades.csv, feature_cols.json
├── h120_gate1/  (p_none_max=1.0 재실행)
├── h300_gate1/
├── h600_gate1/  ← 1 trade, pnl=+0.000270
└── h900_gate1/
scripts/train_regression_baseline.py
```
