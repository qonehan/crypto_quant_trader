# Step 2-2: AI 추론 모델 래핑 — RidgePredictor

**작업일**: 2026-03-05
**브랜치**: copilot/review-alt-3-changes

---

## 개요

학습된 Ridge+StandardScaler 파이프라인(`artifacts/ml1/h{H_SEC}/ridge_model.joblib`)을 로드하여
`BaseModel` 인터페이스를 구현하는 `RidgePredictor` 클래스를 신규 생성한다.

---

## 신규 파일

**`app/predictor/ml_model.py`**

---

## 아키텍처 설계

### 입력 피처 (feature_cols.json 기준)

Ridge 모델은 BaselineModelV1의 출력값을 피처로 학습되었다:

| 피처 | 출처 |
|------|------|
| `p_up` | BaselineModelV1 barrier 계산 |
| `p_down` | BaselineModelV1 barrier 계산 |
| `p_none` | BaselineModelV1 barrier 계산 |
| `ev` | BaselineModelV1 비용 모델 |
| `ev_rate` | BaselineModelV1 EV/E[T] |
| `r_t` | BarrierController |
| `z_barrier` | BaselineModelV1 (r_t / sigma_h) |
| `spread_bps` | market_window 최신 값 |
| `mom_z` | BaselineModelV1 모멘텀 |
| `imb_notional_top5` | market_window 최신 값 |
| `entry_mid` | market_window 최신 mid 가격 |

### 예측 흐름

```
market_window, barrier_row, settings
       ↓
BaselineModelV1.predict()        ← 기반 피처 및 장벽 확률 계산
       ↓
피처 벡터 구성 (11개 feature_cols)
       ↓
Ridge+StandardScaler.predict()   ← 수익률 회귀 예측 (label_return)
       ↓
action_hat / direction_hat 재계산
       ↓
PredictionOutput 반환 (model_version="ridge_v1")
```

### 폴백 전략

다음 조건에서 BaselineModelV1 결과를 그대로 반환 (model_version은 ridge_v1로 교체):
- `z_barrier is None` → 워밍업 구간
- `entry_mid is None` → market_window 비어있음
- Ridge inference 예외 발생 → joblib 오류 등

---

## 핵심 코드

```python
class RidgePredictor(BaseModel):
    MODEL_VERSION = "ridge_v1"

    def __init__(self, model_path: str = "", h_sec: int = 120) -> None:
        import joblib
        path = _resolve_model_path(model_path, h_sec)
        if not path.exists():
            raise FileNotFoundError(f"Ridge 모델 파일을 찾을 수 없습니다: {path}")
        self._pipeline = joblib.load(path)          # sklearn Pipeline (Scaler+Ridge)
        # feature_cols.json 자동 로드 (모델 폴더 내)
        feat_path = path.parent / "feature_cols.json"
        self._feature_cols = json.load(open(feat_path)) if feat_path.exists() else _FEATURE_COLS

    def predict(self, *, market_window, barrier_row, settings) -> PredictionOutput:
        # 1. BaselineModelV1 실행 → 기반 피처 획득
        base = _BASELINE.predict(market_window=market_window,
                                 barrier_row=barrier_row, settings=settings)

        # 2. entry_mid 추출
        entry_mid = next((r.get("mid_close_1s") or r.get("mid")
                          for r in reversed(market_window)
                          if (r.get("mid_close_1s") or r.get("mid"))), None)

        # 3. 피처 벡터 구성 → Ridge 추론
        X = np.array([[feat_map[c] for c in self._feature_cols]])
        ridge_return = float(self._pipeline.predict(X)[0])

        # 4. action_hat / direction_hat 재계산
        action_hat = "ENTER_LONG" if (
            ridge_return > 0
            and ev_rate >= settings.ENTER_EV_RATE_TH
            and base.p_none <= settings.ENTER_PNONE_MAX
            and base.p_up >= base.p_down + settings.ENTER_PDIR_MARGIN
            and spread_bps <= settings.ENTER_SPREAD_BPS_MAX
        ) else "STAY_FLAT"

        # 5. PredictionOutput 반환 (slope_pred = ridge_return)
        return PredictionOutput(slope_pred=ridge_return, action_hat=action_hat, ...)
```

---

## 모델 경로 해석 규칙

| `RIDGE_MODEL_PATH` 설정값 | 실제 경로 |
|--------------------------|----------|
| `""` (빈 문자열, 기본값) | `artifacts/ml1/h{H_SEC}/ridge_model.joblib` |
| 직접 경로 지정 | 그 경로 그대로 사용 |

예시: `H_SEC=120` → `artifacts/ml1/h120/ridge_model.joblib`

---

## PredictionOutput 호환성

`RidgePredictor`는 `BaselineModelV1`과 동일한 `PredictionOutput` 구조체를 반환:

| 필드 | Ridge 처리 |
|------|-----------|
| `p_up`, `p_down`, `p_none` | BaselineModelV1에서 그대로 사용 (장벽 확률 유지) |
| `slope_pred` | **Ridge가 예측한 label_return으로 교체** |
| `ev`, `ev_rate` | BaselineModelV1 그대로 (비용 모델 동일) |
| `direction_hat` | ridge_return 부호 기반 재계산 |
| `action_hat` | ridge_return > 0 조건 추가 |
| `model_version` | `"ridge_v1"` |
| `features` | baseline features + `ridge_return_pred`, `entry_mid` |

---

## 검증 포인트

- [x] `RidgePredictor("", h_sec=120)` → `artifacts/ml1/h120/ridge_model.joblib` 자동 로드
- [x] `FileNotFoundError` → 모델 파일 없을 때 명확한 메시지
- [x] `feature_cols.json` 자동 로드 → 피처 순서 불일치 방지
- [x] 워밍업 구간(z_barrier=None) 폴백 → `p_none=0.99` 반환 유지
- [x] Ridge inference 예외 → baseline으로 폴백 후 로그 기록
- [x] `PredictionOutput` 모든 필드 채워짐 → `upsert_prediction()` 충돌 없음
