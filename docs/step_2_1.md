# Step 2-1: 수집/저장 로직 제거 및 설정 분리 (GCP 연동 준비)

**작업일**: 2026-03-05
**브랜치**: copilot/review-alt-3-changes

---

## 개요

로컬 시계열 수집 파이프라인을 GCP 읽기 전용 연동 구조로 분리하는 1단계 작업.
이후 GCP_DB_URL을 설정하면 봇은 GCP PostgreSQL에서 market_1s 데이터를 읽어오고,
로컬 market_1s 쓰기는 완전히 비활성화된다.

---

## 변경 파일 목록

| 파일 | 변경 유형 | 내용 |
|------|-----------|------|
| `app/config.py` | 수정 | `GCP_DB_URL`, `PREDICTOR_TYPE`, `RIDGE_MODEL_PATH` 추가; `ALT_DATA_ENABLED` 기본값 `False` 변경 |
| `app/marketdata/resampler.py` | 수정 | `upsert_market_1s()` 호출 주석 처리, 관련 import 제거 |
| `scripts/export_dataset.py` | 수정 | `GCP_DB_URL or DB_URL` 우선순위 엔진 생성 |
| `app/dashboard.py` | 수정 | `get_read_engine()` 헬퍼 추가 → GCP 우선 엔진 사용 |

---

## 상세 변경 내용

### 1. `app/config.py`

```python
# 추가된 필드
GCP_DB_URL: Optional[str] = None          # GCP PostgreSQL 읽기 URL (미설정 시 로컬 DB 사용)

PREDICTOR_TYPE: str = "baseline"          # baseline | ridge (2단계에서 사용)
RIDGE_MODEL_PATH: str = ""               # 빈 문자열 → artifacts/ml1/h{H_SEC}/ridge_model.joblib

# 변경된 기본값
ALT_DATA_ENABLED: bool = False            # True → False (바이낸스/코인글래스 수집 비활성화)
```

**결정 이유:**
- `GCP_DB_URL`은 Optional로 두어 기존 로컬 환경을 깨지 않음
- `ALT_DATA_ENABLED=False`로 변경해 Alt Data 수집기가 기본적으로 실행되지 않도록 함
- `PREDICTOR_TYPE`, `RIDGE_MODEL_PATH`는 2단계 Ridge 모델 연동에서 사용

### 2. `app/marketdata/resampler.py`

```python
# 제거된 import
# from app.db.writer import upsert_market_1s   ← 삭제

# run() 루프 내 변경 (주석 처리)
# try:
#     await asyncio.to_thread(upsert_market_1s, self.engine, row)
# except Exception:
#     log.exception("Failed to upsert market_1s row ts=%s", ts_utc)
_ = row  # suppress unused warning (state updates handled by MarketState)
```

**결정 이유:**
- WebSocket 실시간 데이터는 `MarketState` 메모리 업데이트에만 사용
- market_1s DB 쓰기는 GCP 파이프라인이 담당
- `engine` 파라미터는 생성자 시그니처 호환성을 위해 유지 (bot.py 변경 최소화)

### 3. `scripts/export_dataset.py`

```python
# 변경 전
engine = create_engine(s.DB_URL)

# 변경 후
read_url = s.GCP_DB_URL or s.DB_URL
engine = create_engine(read_url)
print(f"  db_url (read)  = {'GCP_DB_URL' if s.GCP_DB_URL else 'DB_URL (local)'}")
```

### 4. `app/dashboard.py`

```python
# 추가된 헬퍼 함수
def get_read_engine(settings):
    """GCP_DB_URL이 설정된 경우 읽기 전용 GCP DB 엔진을 반환, 아니면 로컬 엔진."""
    if settings.GCP_DB_URL:
        return create_engine(settings.GCP_DB_URL, echo=False)
    return get_engine(settings)

# 변경된 main() 내 호출
engine = get_read_engine(settings)   # ← 기존 get_engine(settings) 대체
```

연결 성공 메시지에 `"GCP DB"` 또는 `"Local DB"` 표시 추가.

---

## ALT_DATA_ENABLED 차단 확인

`app/bot.py`는 기존부터 아래 패턴으로 Alt Data Runner를 조건부 실행:

```python
if settings.ALT_DATA_ENABLED:
    binance_runner = BinanceAltDataRunner(settings, engine)
    tasks.append(asyncio.create_task(binance_runner.run(), ...))
    coinglass_runner = CoinglassAltDataRunner(settings, engine)
    tasks.append(asyncio.create_task(coinglass_runner.run(), ...))
```

`ALT_DATA_ENABLED=False`(기본값) → 두 Runner 모두 `tasks`에 추가되지 않아 실행 없음.
추가 수정 불필요 — 이미 올바르게 차단됨.

---

## 환경 변수 설정 가이드

GCP 연동 시 `.env`에 추가:

```env
GCP_DB_URL=postgresql+psycopg://user:pass@<GCP-IP>:5432/quant
ALT_DATA_ENABLED=False
```

GCP_DB_URL 미설정 시 기존 로컬 `DB_URL`로 동작 (하위 호환 유지).

---

## 검증 포인트

- [x] `ALT_DATA_ENABLED=False` 기본값 → binance/coinglass runner 비실행
- [x] `upsert_market_1s` import 제거 → resampler 모듈 로드 오류 없음
- [x] `GCP_DB_URL=None` 상태에서 export_dataset, dashboard 모두 `DB_URL` 폴백 정상
- [x] `get_read_engine()` 헬퍼로 dashboard 엔진 선택 투명화
