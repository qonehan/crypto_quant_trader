# Step 2-4: 아키텍처 롤백 — 로컬 DB 파이프라인 복구

**작업일**: 2026-03-05
**브랜치**: copilot/review-alt-3-changes

---

## 롤백 배경

Step 2-1에서 `upsert_market_1s()` 비활성화 및 dashboard GCP 우선 연결로 변경했으나,
실시간 트레이딩 루프가 로컬 `market_1s` 테이블에 의존하고 있어 데이터 파이프라인이 단절됨:

- `PredictionRunner` → `market_1s` 읽기 → 로컬 DB에 데이터가 없으면 예측 불가
- `BarrierController` → `market_1s` 기반 변동성 계산 → 동일 단절
- `Dashboard` → 로컬 실시간 상태 표시 → GCP 바라볼 이유 없음

**GCP 역할 재정립:**
- GCP: `upbit_tick` 등 원천 데이터 수집 (별도 파이프라인)
- 로컬: `market_1s` + `predictions` + `paper_trades` 실시간 생성 및 대시보드 렌더링
- `GCP_DB_URL`: 장기 학습 데이터 추출(`export_dataset.py`)에만 사용

---

## 변경 파일

| 파일 | 변경 내용 |
|------|-----------|
| `app/marketdata/resampler.py` | `upsert_market_1s` import 복구 + 호출 로직 원상 복구 |
| `app/dashboard.py` | `get_read_engine()` 헬퍼 삭제 + `get_engine(settings)` 복구 |

---

## 복구 내용

### 1. `app/marketdata/resampler.py`

```python
# 복구: import 재추가
from app.db.writer import upsert_market_1s

# 복구: run() 루프 내 원상 복구
try:
    await asyncio.to_thread(upsert_market_1s, self.engine, row)
except Exception:
    log.exception("Failed to upsert market_1s row ts=%s", ts_utc)
```

→ WebSocket 수신 1초 캔들이 로컬 `market_1s` 테이블에 즉시 기록됨

### 2. `app/dashboard.py`

```python
# 삭제: get_read_engine() 헬퍼 함수 전체 제거
# 삭제: from sqlalchemy import create_engine (불필요)

# 복구: 기존 방식 그대로
engine = get_engine(settings)   # 항상 로컬 DB_URL 사용
st.success("DB connection OK")
```

→ 대시보드는 항상 로컬 실시간 DB를 바라봄

---

## GCP_DB_URL 격리 현황

| 컴포넌트 | DB 연결 | 비고 |
|----------|---------|------|
| `resampler.py` | 로컬 (`DB_URL`) | 1초 캔들 쓰기 |
| `predictor/runner.py` | 로컬 (`DB_URL`) | market_1s 읽기 + predictions 쓰기 |
| `barrier/controller.py` | 로컬 (`DB_URL`) | market_1s 읽기 |
| `trading/runner.py` | 로컬 (`DB_URL`) | predictions 읽기 + paper_trades 쓰기 |
| `dashboard.py` | 로컬 (`DB_URL`) | 실시간 상태 조회 |
| `scripts/export_dataset.py` | **`GCP_DB_URL or DB_URL`** | 학습 데이터 추출 전용 |

`GCP_DB_URL`은 `config.py`에 `Optional[str] = None`으로 선언되어 있으며,
`export_dataset.py` 외에는 어디서도 참조하지 않음.

---

## 현재 최종 아키텍처

```
[Upbit WebSocket]
       ↓
[UpbitWsClient] → queue → [consumer]
                               ↓
                         [MarketState]  (메모리)
                               ↓
                         [MarketResampler]
                               ├─→ MarketState 업데이트 (메모리)
                               └─→ upsert_market_1s() → [로컬 PostgreSQL]
                                                               ↓
                                               ┌──────────────┼──────────────┐
                                    [BarrierController]  [PredictionRunner]  [Dashboard]
                                               ↓                  ↓
                                         barrier_state       predictions
                                                              paper_trades
                                                                   ↓
                                                        [PaperTradingRunner]

[GCP_DB_URL] ←── export_dataset.py (학습 데이터 추출 전용)
```
