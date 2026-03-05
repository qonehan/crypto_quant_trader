# Step 2-5: AI 두뇌 고도화 — HGBR 모델 학습 및 봇 교체 가이드

**작업일**: 2026-03-05
**브랜치**: copilot/review-alt-3-changes

---

## 1. GCP 데이터 추출 결과

### GCP 연결 상태: ❌ 타임아웃

```
psycopg2.OperationalError: connection to server at "34.22.108.149", port 5432 failed:
Connection timed out
```

GCP 서버 방화벽 또는 PostgreSQL이 현재 외부 접근을 허용하지 않고 있습니다.
→ **로컬 DB 데이터로 대체 추출** (GCP_DB_URL="" 환경변수 오버라이드)

### 로컬 DB 추출 결과 (`local_h120_maker.parquet`)

| 항목 | 값 |
|------|-----|
| 출력 파일 | `data/datasets/local_h120_maker.parquet` |
| 총 행 수 | **935행** (segment 선택 후) |
| 데이터 기간 | 2026-02-22 11:48:50 ~ 2026-02-22 13:08:35 UTC |
| 커버리지 | **1.33시간** |
| 원본 행 수 | 2412행 (3개 세그먼트 중 가장 큰 1개 선택) |
| 비용 추정치 평균 | 0.001184 (fee=10bps + spread) |

> ⚠️ **데이터 부족 경고**: 935행 / 1.33h 은 신뢰할 수 있는 ML 학습에 부족합니다.
> 의미 있는 모델을 위해 최소 5,000행 이상의 데이터(약 7시간 이상 연속)가 필요합니다.
> GCP 재연결 후 재실행 권장.

---

## 2. 학습 결과 및 백테스트 성과

### 2-A. `h120` — 로컬 935행

| 모델 | train | valid | test | RMSE(test) | naive0 RMSE | IC | sign_acc | gamma | test 거래 | total_net |
|------|-------|-------|------|-----------|-------------|-----|----------|-------|-----------|-----------|
| **Ridge** | 654 | 140 | 141 | 0.000769 | 0.000159 | -0.115 | 0.603 | 1.0 | 1건 | -0.000945 |
| **HGBR** | 654 | 140 | 141 | 0.001134 | 0.000159 | 0.011 | 0.603 | 1.5 | 0건 | 0.000000 |

### 2-B. `h900` — 기존 btc_24h 1441행

| 모델 | train | valid | test | RMSE(test) | naive0 RMSE | IC | gamma | test 거래 | total_net |
|------|-------|-------|------|-----------|-------------|-----|-------|-----------|-----------|
| **Ridge** | 1008 | 216 | 217 | 0.003449 | 0.002722 | +0.101 | 4.0 | 0건 | 0.000 |
| **HGBR** | 1008 | 216 | 217 | 0.003443 | 0.002722 | +0.654 | 4.0 | 1건 | -0.002514 |

### 결론

**모든 모델이 naive0(예측=0)보다 RMSE가 높음** → 데이터 부족으로 인한 과적합.

- HGBR h900: IC=0.654로 상관계수는 높지만, backtest 손실 → gamma 과최적화
- Ridge h120: sign_acc=0.603으로 방향 예측 60% → 데이터 더 쌓이면 개선 여지 있음
- **현 시점 최선 모델: 데이터가 부족해 실전 투입 비권장**

**저장된 아티팩트:**

| 경로 | 모델 |
|------|------|
| `artifacts/ml_new/h120_local/local_h120_maker_ridge/ridge_model.joblib` | Ridge h120 |
| `artifacts/ml_new/h120_local/local_h120_maker_hgbr/ridge_model.joblib` | HGBR h120 |
| `artifacts/ml_new/h900_24h/btc_24h_h900_maker_ridge/ridge_model.joblib` | Ridge h900 |
| `artifacts/ml_new/h900_24h/btc_24h_h900_maker_hgbr/ridge_model.joblib` | HGBR h900 |

> 파일명이 `ridge_model.joblib`인 것은 `train_and_trade_econ_gate.py`의 저장 관례입니다.
> Ridge든 HGBR이든 동일한 파일명으로 저장됩니다.

---

## 3. 코드 변경 사항

### 3-A. `scripts/train_and_trade_econ_gate.py` — joblib 저장 추가

```python
import joblib  # 추가

# 학습 루프 내 아티팩트 저장 블록 추가
model_path = os.path.join(out_base, "ridge_model.joblib")
joblib.dump(model, model_path)
meta = {"feature_cols": feat, "model_type": name, "horizon_sec": args.horizon,
        "gamma_selected": best_gamma, ...}
with open(os.path.join(out_base, "model_meta.json"), "w") as f: json.dump(meta, f)
with open(os.path.join(out_base, "feature_cols.json"), "w") as f: json.dump(feat, f)
```

### 3-B. `app/predictor/ml_model.py` — HGBRPredictor 추가

```python
class HGBRPredictor(BaseModel):
    MODEL_VERSION = "hgbr_v1"

    def __init__(self, model_path: str = "", h_sec: int = 120) -> None:
        path = _resolve_hgbr_path(model_path, h_sec)
        self._pipeline = joblib.load(path)          # HistGradientBoosting 파이프라인
        # feature_cols.json 자동 로드
        ...

    def predict(self, *, market_window, barrier_row, settings) -> PredictionOutput:
        # RidgePredictor와 동일한 흐름:
        # BaselineModelV1 → 피처 구성 → HGBR.predict() → action_hat 재계산
        ...
```

### 3-C. `app/predictor/runner.py` — 팩토리에 HGBR 분기 추가

```python
def create_model(settings: Settings) -> BaseModel:
    ptype = (settings.PREDICTOR_TYPE or "baseline").lower()

    if ptype == "ridge":
        from app.predictor.ml_model import RidgePredictor
        return RidgePredictor(model_path=settings.RIDGE_MODEL_PATH, h_sec=settings.H_SEC)

    if ptype == "hgbr":
        from app.predictor.ml_model import HGBRPredictor
        return HGBRPredictor(model_path=settings.RIDGE_MODEL_PATH, h_sec=settings.H_SEC)

    return BaselineModelV1()
```

---

## 4. 실전 봇 모델 교체 가이드

### 4-A. Ridge 모델로 교체

```env
# .env
PREDICTOR_TYPE=ridge
RIDGE_MODEL_PATH=artifacts/ml_new/h120_local/local_h120_maker_ridge/ridge_model.joblib
```

또는 H_SEC과 매칭되는 기존 ml1 경로 사용:
```env
PREDICTOR_TYPE=ridge
RIDGE_MODEL_PATH=artifacts/ml1/h120/ridge_model.joblib
```

### 4-B. HGBR 모델로 교체

`PREDICTOR_TYPE=hgbr`로 변경하고 동일한 `RIDGE_MODEL_PATH` 키를 사용:

```env
# .env
PREDICTOR_TYPE=hgbr
RIDGE_MODEL_PATH=artifacts/ml_new/h120_local/local_h120_maker_hgbr/ridge_model.joblib
```

봇 재시작 후 로그에서 확인:
```
[INFO] create_model: HGBRPredictor 선택 (path=artifacts/..., h_sec=120)
[INFO] Predictor: PREDICTOR_TYPE=hgbr → HGBRPredictor
```

### 4-C. PREDICTOR_TYPE 전체 옵션 정리

| `PREDICTOR_TYPE` | 클래스 | MODEL_VERSION | 비고 |
|-----------------|--------|---------------|------|
| `baseline` | `BaselineModelV1` | `baseline_v1_exec` | 기본값, 규칙 기반 |
| `ridge` | `RidgePredictor` | `ridge_v1` | Ridge+StandardScaler |
| `hgbr` | `HGBRPredictor` | `hgbr_v1` | HistGradientBoosting |

### 4-D. RIDGE_MODEL_PATH 해석 규칙

| `RIDGE_MODEL_PATH` | 실제 경로 |
|--------------------|-----------|
| `""` (기본값) | `artifacts/ml1/h{H_SEC}/ridge_model.joblib` |
| 상대 경로 지정 | 그대로 사용 |

> **HGBR도 동일 키 `RIDGE_MODEL_PATH`를 사용**합니다.
> (설정 키 이름이 ridge이지만, 지정한 경로의 joblib 파일이 어떤 모델이든 로드함)

---

## 5. GCP 재연결 후 실행 절차

GCP 방화벽/PostgreSQL 재설정 후 아래 순서로 실행:

```bash
# Step 1: 최신 데이터 추출 (GCP DB 사용)
poetry run python scripts/export_dataset.py \
  --output data/datasets/gcp_h120_maker.parquet \
  --horizon 120 \
  --max-feature-gap-sec 120 \
  --fee-bps-roundtrip 10

# Step 2: Ridge + HGBR 학습
poetry run python scripts/train_and_trade_econ_gate.py \
  --input data/datasets/gcp_h120_maker.parquet \
  --horizon 120 \
  --outdir artifacts/ml_gcp/h120 \
  --models ridge,hgbr \
  --gamma-grid "1.0,1.5,2.0,2.5,3.0,4.0"

# Step 3: 결과 확인 후 .env 업데이트
# 예: HGBR이 최적이라면
# PREDICTOR_TYPE=hgbr
# RIDGE_MODEL_PATH=artifacts/ml_gcp/h120/gcp_h120_maker_hgbr/ridge_model.joblib
```

> 데이터 목표량: **최소 5,000행 이상** (약 7h 연속 데이터).
> GCP에 쌓인 데이터가 충분하면 HGBR IC=0.65 수준에서 backtest 수익이 나올 가능성 높음.

---

## 요약

| 단계 | 결과 |
|------|------|
| GCP 데이터 추출 | ❌ 연결 타임아웃 (34.22.108.149:5432) |
| 로컬 데이터 추출 | ✅ 935행, 1.33h (`local_h120_maker.parquet`) |
| Ridge h120 학습 | ✅ 완료 (RMSE > naive0, 데이터 부족) |
| HGBR h120 학습 | ✅ 완료 (거래 0건, 데이터 부족) |
| joblib 저장 기능 | ✅ train 스크립트에 추가 완료 |
| HGBRPredictor 코드 | ✅ `app/predictor/ml_model.py` 추가 완료 |
| 팩토리 HGBR 지원 | ✅ `app/predictor/runner.py` 업데이트 완료 |
| import 검증 | ✅ `import OK` |
