# Step 2-3: 실시간 봇 Brain 교체 — 팩토리 연결 및 파이프라인 검증

**작업일**: 2026-03-05
**브랜치**: copilot/review-alt-3-changes

---

## 개요

`RidgePredictor`를 실제 트레이딩 루프에 연결하는 3단계 작업.
`app/predictor/runner.py`에 팩토리 함수를 추가하고, `app/bot.py`에서 설정 기반으로
`BaselineModelV1` 또는 `RidgePredictor`를 동적 선택한다.

---

## 변경 파일

| 파일 | 변경 유형 | 내용 |
|------|-----------|------|
| `app/predictor/runner.py` | 수정 | `create_model()` 팩토리 함수 추가 |
| `app/bot.py` | 수정 | `create_model(settings)` 사용, 기존 하드코딩 제거 |

---

## 상세 변경 내용

### 1. `app/predictor/runner.py` — 팩토리 추가

```python
def create_model(settings: Settings) -> BaseModel:
    """PREDICTOR_TYPE 설정에 따라 모델 인스턴스를 생성하는 팩토리."""
    ptype = (settings.PREDICTOR_TYPE or "baseline").lower()
    if ptype == "ridge":
        from app.predictor.ml_model import RidgePredictor
        model = RidgePredictor(
            model_path=settings.RIDGE_MODEL_PATH,
            h_sec=settings.H_SEC,
        )
        log.info("create_model: RidgePredictor 선택 ...")
        return model
    else:
        log.info("create_model: BaselineModelV1 선택")
        return BaselineModelV1()
```

**설계 원칙:**
- 지연 import(`from app.predictor.ml_model import RidgePredictor`) → joblib 의존성을 ridge 사용 시에만 로드
- 기본값 `"baseline"` → 기존 동작 완전 보존
- ridge 파일 없을 시 `FileNotFoundError` → 봇 시작 실패로 명확히 알림

### 2. `app/bot.py` — 하드코딩 제거

```python
# 변경 전
from app.models.baseline_v1 import BaselineModelV1
from app.predictor.runner import PredictionRunner
...
model = BaselineModelV1()
pred_runner = PredictionRunner(settings, engine, model)

# 변경 후
from app.predictor.runner import PredictionRunner, create_model
...
model = create_model(settings)
log.info("Predictor: PREDICTOR_TYPE=%s → %s", settings.PREDICTOR_TYPE, type(model).__name__)
pred_runner = PredictionRunner(settings, engine, model)
```

---

## Ridge 활성화 방법

`.env` 또는 환경 변수에 추가:

```env
PREDICTOR_TYPE=ridge
# RIDGE_MODEL_PATH는 비워두면 H_SEC 기반 자동 탐색
```

또는 `docker-compose.yml`:

```yaml
environment:
  PREDICTOR_TYPE: "ridge"
```

---

## 데이터 파이프라인 호환성 검증

### PredictionRunner → DB 쓰기

`PredictionRunner._run_tick()`은 `model.predict()` 결과를 `upsert_prediction()`으로 저장:

```python
row = {
    "p_up": output.p_up,         # float ← RidgePredictor: baseline과 동일 타입 ✓
    "p_down": output.p_down,      # float ✓
    "p_none": output.p_none,      # float ✓
    "ev": output.ev,              # float ✓
    "ev_rate": output.ev_rate,    # float | None ✓
    "slope_pred": output.slope_pred,    # float (ridge_return) ✓
    "direction_hat": output.direction_hat,  # str ✓
    "action_hat": output.action_hat,        # str ✓
    "model_version": output.model_version,  # "ridge_v1" ✓
    "features": json.dumps(output.features), # dict → JSON ✓
    "z_barrier": output.z_barrier,   # float | None ✓
    "ev_rate": output.ev_rate,       # float | None ✓
    ...
}
```

모든 필드 타입 호환 — DB 스키마 변경 불필요.

### PaperTradingRunner 호환성

`PaperTradingRunner._fetch_latest_pred()`는 predictions 테이블에서 다음 컬럼만 읽음:

```sql
SELECT t0, symbol, h_sec, r_t, p_up, p_down, p_none,
       ev, ev_rate, z_barrier, spread_bps, action_hat, model_version
FROM predictions WHERE symbol = :sym ORDER BY t0 DESC LIMIT 1
```

`RidgePredictor`가 이 모든 필드를 정확히 채움:

| 필드 | RidgePredictor 처리 | 타입 | 충돌 |
|------|---------------------|------|------|
| `p_up` | baseline에서 상속 | float | 없음 |
| `p_down` | baseline에서 상속 | float | 없음 |
| `p_none` | baseline에서 상속 | float | 없음 |
| `ev` | baseline에서 상속 | float | 없음 |
| `ev_rate` | baseline에서 상속 | float\|None | 없음 |
| `z_barrier` | baseline에서 상속 | float\|None | 없음 |
| `spread_bps` | baseline에서 상속 | float\|None | 없음 |
| `action_hat` | Ridge 부호 기반 재계산 | str | 없음 |
| `model_version` | `"ridge_v1"` | str | 없음 |

### decide_action() 호환성

`app/trading/policy.py`의 `decide_action()`은 pred dict에서 다음 키를 사용:
- `ev_rate`, `p_none`, `p_up`, `p_down`, `r_t`, `z_barrier`, `spread_bps`, `action_hat`

모두 RidgePredictor 출력에서 정상 공급됨.

### 경제적 게이트(EV Gate) 통과 여부

`PaperTradingRunner`는 `action_hat="ENTER_LONG"`을 확인하는 게이트를 가짐.
RidgePredictor에서 `action_hat` 계산 시 기존 설정값(`ENTER_EV_RATE_TH`, `ENTER_PNONE_MAX` 등)을
그대로 사용하므로 경제적 게이트가 동일하게 적용됨.

---

## 전체 변경 요약 (1~3단계)

```
app/config.py
  + GCP_DB_URL: Optional[str] = None
  + PREDICTOR_TYPE: str = "baseline"
  + RIDGE_MODEL_PATH: str = ""
  ~ ALT_DATA_ENABLED: False (기본값 변경)

app/marketdata/resampler.py
  - import upsert_market_1s
  ~ upsert_market_1s() 호출 주석 처리

scripts/export_dataset.py
  ~ engine = create_engine(s.GCP_DB_URL or s.DB_URL)

app/dashboard.py
  + get_read_engine() 헬퍼 추가
  ~ engine = get_read_engine(settings)

app/predictor/ml_model.py  [신규]
  + RidgePredictor(BaseModel) 클래스

app/predictor/runner.py
  + create_model(settings) 팩토리 함수

app/bot.py
  - from app.models.baseline_v1 import BaselineModelV1
  ~ model = create_model(settings)  (팩토리 사용)
```

---

## 다음 단계 권장 사항

1. **GCP DB 연결 테스트**: `GCP_DB_URL` 설정 후 `export_dataset.py` 실행으로 읽기 검증
2. **Ridge 모델 활성화 테스트**: `PREDICTOR_TYPE=ridge` → 봇 시작 로그에서 `"RidgePredictor 선택"` 확인
3. **PaperTrading 분리**: Ridge 모델 기반 paper trade 히스토리와 baseline 비교 (model_version 필드 활용)
4. **market_1s GCP 파이프라인**: GCP에서 실시간으로 market_1s 테이블을 채우는 ingestion 파이프라인 구축 필요
