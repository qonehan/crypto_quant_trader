# step_2_18 결과 보고 — 예측 엔진 Plug-and-Play 아키텍처 리팩토링

## 목표
`.env`의 `ACTIVE_MODEL` 변수 하나만 바꾸고 봇을 재시작하면, 코드 수정 없이 모델·호흡(H_SEC)·gamma가 즉시 교체되는 팩토리 패턴 구조 완성.

---

## 변경 파일 요약

| 파일 | 변경 내용 |
|---|---|
| `app/predictor/ml_model.py` | `ModelSpec` 데이터클래스 + `_REGISTRY` 딕셔너리 + `ModelFactory` 클래스 추가; 공유 헬퍼 함수 추출; 예측기 gamma/version 파라미터화 |
| `app/config.py` | `ACTIVE_MODEL` 필드 추가; `model_validator`로 H_SEC·MODEL_LOOKBACK_SEC 자동 동기화 |
| `app/predictor/runner.py` | `create_model()` 내부를 `ModelFactory.create(settings.ACTIVE_MODEL)`으로 단순화 |
| `app/dashboard.py` | `ModelFactory.get_spec()`으로 활성 모델 메타 정보(display_name·H·γ·version)를 상단에 자동 렌더링 |
| `.env` | `ACTIVE_MODEL=ridge_h3600` 추가 |

---

## 1. ModelSpec — 레지스트리 엔트리 (`app/predictor/ml_model.py`)

```python
@dataclass(frozen=True)
class ModelSpec:
    model_id: str       # ACTIVE_MODEL 식별자
    model_class: str    # "ridge" | "hgbr" | "baseline"
    artifact_path: str  # joblib 상대 경로
    h_sec: int          # 예측 호흡(초)
    gamma: float        # 진입 문턱 배수
    version: str        # predictions 테이블 model_version 태그
    display_name: str   # 대시보드 표시명
```

## 2. _REGISTRY — 등록된 모델 목록

| ACTIVE_MODEL | class | artifact_path | H_SEC | γ | version |
|---|---|---|---|---|---|
| `ridge_h3600` | ridge | `ml_prod/h3600/…/ridge_model.joblib` | 3600 | 1.5 | ridge_h3600_v1 |
| `ridge_h600` | ridge | `ml_prod/h600/…/ridge_model.joblib` | 600 | 1.5 | ridge_h600_v1 |
| `ridge_h120` | ridge | `ml_prod/h120/…/ridge_model.joblib` | 120 | 1.5 | ridge_h120_v1 |
| `hgbr_h600` | hgbr | `ml_prod/h600/…/hgbr/ridge_model.joblib` | 600 | 1.5 | hgbr_h600_v1 |
| `hgbr_h120` | hgbr | `ml_prod/h120/…/hgbr/ridge_model.joblib` | 120 | 1.5 | hgbr_h120_v1 |
| `baseline_v1` | baseline | (없음) | 120 | 0.0 | baseline_v1 |

## 3. ModelFactory API

```python
# 전체 모델 목록
ModelFactory.list_models()
# → ['ridge_h3600', 'ridge_h600', 'ridge_h120', 'hgbr_h600', 'hgbr_h120', 'baseline_v1']

# 메타 정보 조회
spec = ModelFactory.get_spec("ridge_h3600")
# spec.h_sec → 3600, spec.gamma → 1.5, spec.display_name → "Ridge H1h (sign_acc 82%, γ=1.5)"

# 인스턴스 생성 (artifact_path·gamma·version 자동 주입)
model = ModelFactory.create("ridge_h3600")
```

## 4. config.py — ACTIVE_MODEL → H_SEC 자동 동기화

```python
# _ACTIVE_MODEL_HORIZON 매핑으로 model_validator가 자동 반영
ACTIVE_MODEL=ridge_h3600  # .env에 설정
# → H_SEC=3600, MODEL_LOOKBACK_SEC=3600 자동 설정 (별도 H_SEC 줄 불필요)
```

## 5. 공유 헬퍼 함수 추출 (중복 제거)

- `_build_feat_map()` — 19개 피처 딕셔너리 구성 (RidgePredictor/HGBRPredictor 공유)
- `_compute_action_hat()` — gamma 기반 ENTER_LONG/ENTER_SHORT/STAY_FLAT 판정 (공유)
- `_compute_direction_hat()` — UP/DOWN/NONE 판정 (공유)
- `_extract_entry_mid()` — market_window에서 최신 mid 추출 (공유)
- `_load_pipeline()` — joblib 로드 + 에러 메시지 (공유)
- `_load_feature_cols()` — feature_cols.json 로드 + 폴백 (공유)

## 6. 대시보드 — 활성 모델 자동 렌더링

```
활성 모델 — Ridge H1h (sign_acc 82%, γ=1.5) | model_id: ridge_h3600 | H=3600s | γ=1.5 | version: ridge_h3600_v1
```

환경 변수 변경 시 재시작만 하면 위 배지가 즉시 업데이트됩니다.

---

## 모델 교체 방법 (코드 변경 없음)

```bash
# 1. .env에서 한 줄만 수정
ACTIVE_MODEL=hgbr_h600   # 예: HGBR 10분 호흡으로 교체

# 2. 봇 재시작
poetry run python -m app

# 3. 대시보드 재시작
poetry run streamlit run app/dashboard.py --server.port 8501
```

> `ACTIVE_MODEL=hgbr_h600` 설정 시 H_SEC=600이 자동 반영되어
> 배리어 컨트롤러·예측 러너·모의투자 엔진 모두 10분 호흡으로 동작합니다.

---

## 하위 호환성

- `PREDICTOR_TYPE` / `RIDGE_MODEL_PATH` / `RIDGE_GAMMA` 환경변수는 `.env`에 유지
- `ACTIVE_MODEL`이 설정된 경우 위 변수들은 무시됨
- `create_model(settings)` 함수 시그니처 유지 (bot.py 변경 없음)

---

## 실행 명령어

```bash
# 봇
poetry run python -m app

# 대시보드
poetry run streamlit run app/dashboard.py --server.port 8501

# 레지스트리 확인 (Python REPL)
from app.predictor.ml_model import ModelFactory
print(ModelFactory.list_models())
spec = ModelFactory.get_spec("ridge_h3600")
print(spec)
```
