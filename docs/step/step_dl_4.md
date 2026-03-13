# Step DL-4: 실시간 모의투자 구현 보고서

## 개요

`scripts/dl/step_dl_4_mock_trade.py` — 1D-CNN + LSTM 스나이퍼 모델을 실시간 업비트 1분봉에 적용하는 모의투자(Mock) 루프.

---

## 아키텍처 흐름

```
[매분 정각+3초]
      │
      ▼
pyupbit.get_ohlcv(count=320)   ← KRW-BTC 1분봉 최신 320개
      │
      ▼
make_tech_features()            ← 44개 기술 지표 (step_dl_1과 동일 로직)
+ 매크로 피처 주입(8개)         ← yfinance 일봉 → ffill (12시간 주기 갱신)
      │
      ▼
window = 마지막 SEQ_LEN(60)행   ← feature_cols.json 52컬럼 순서 맞춤
      │
      ▼
transform_realtime()            ← scaler.joblib(RobustScaler) 적용
tensor: (1, 60, 52)
      │
      ▼
LSTMClassifier.forward()        ← artifacts/dl_prod/lstm_model.pt
logit → sigmoid → prob
      │
      ▼
매매 판단
  ├─ 대기 중 + prob ≥ 0.55  → [MOCK BUY]
  ├─ 보유 중 + 수익률 ≤ -0.5% → [MOCK SELL] (STOP_LOSS)
  └─ 보유 중 + prob < 0.45   → [MOCK SELL] (PROB_WEAK)
```

---

## 핵심 설계 결정

### 1. 피처 일치 보장
| 항목 | 학습(step_dl_1) | 실시간(step_dl_4) |
|---|---|---|
| OHLCV 지표 | `make_tech_features()` | 동일 함수 코드 복사 |
| 매크로 8개 | yfinance 일봉 → ffill | yfinance 최근 10일 → 마지막값 |
| 스케일링 | `RobustScaler` fit (train only) | `scaler.joblib` transform |
| 피처 순서 | `feature_cols.json` 저장 | `feature_cols.json` 로드 |

### 2. OHLCV 버퍼 크기 = 320
- `ma240` 계산에 240행 필요
- `SEQ_LEN = 60` 추론 윈도우
- 안전 여유 20행
- **합계 320행** (`FETCH_COUNT = 320`)

### 3. 매크로 피처 실시간 처리
- 학습 데이터는 일봉 forward-fill 방식으로 매크로를 분봉에 주입
- 실시간도 동일하게 **당일 최신 일봉값을 모든 분에 상수 주입**
- 12시간마다 yfinance 재조회 (`MACRO_REFRESH_HOURS = 12`)

### 4. 진입/청산 조건
```python
THRESHOLD       = 0.55    # 진입: prob ≥ 이 값일 때만 MOCK BUY
STOP_LOSS_PCT   = -0.005  # 청산: 미실현 손실 -0.5% 이하
EXIT_PROB_FLOOR = 0.45    # 청산: 확률이 이 값 아래로 하락
```

---

## 출력 예시

```
  시각               확률    현재가      상태           미실현PnL
  -----------------------------------------------------------------
  2026-03-13 14:01  0.4821  128,540,000원  대기               -
  2026-03-13 14:02  0.5723  128,610,000원  대기               -

  ██ [MOCK BUY]  2026-03-13 14:02  가격=128,610,000원  확률=0.5723

  2026-03-13 14:03  0.5901  128,780,000원  보유중         +0.132%
  2026-03-13 14:04  0.4210  128,450,000원  보유중         -0.124%

  ▼  [MOCK SELL] 2026-03-13 14:04  가격=128,450,000원  확률=0.4210
     수익률=-0.124%  보유=2분  사유=PROB_WEAK
     누적 거래수=1  누적 PnL=-0.124%
```

---

## 실행 방법

```bash
# 의존성 확인 (pyupbit, yfinance, torch, joblib, python-dotenv)
poetry run python scripts/dl/step_dl_4_mock_trade.py
```

Ctrl+C 로 종료 시 세션 요약 출력.

---

## 파라미터 튜닝 가이드

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `THRESHOLD` | 0.55 | 높일수록 진입 횟수 감소, 정밀도 향상 |
| `STOP_LOSS_PCT` | -0.005 | 손절폭 (절대값 키울수록 더 오래 버팀) |
| `EXIT_PROB_FLOOR` | 0.45 | 낮출수록 보유 시간 증가 |
| `MACRO_REFRESH_HOURS` | 12 | 매크로 갱신 주기 |

---

## 의존 아티팩트

```
artifacts/dl_prod/
  ├── lstm_model.pt        ← 학습된 가중치 (Step DL-3 산출물)
  ├── dl_model_meta.json   ← n_features=52, hidden_size=64, num_layers=2
  ├── scaler.joblib        ← RobustScaler (Train set fit)
  └── feature_cols.json    ← 52개 피처 순서 목록
```

---

## 다음 단계 (Step DL-5 제안)

- **백테스트**: 테스트셋 기간에 동일 로직 적용 → 실제 PnL 시뮬레이션
- **앙상블**: Ridge 모델(`ACTIVE_MODEL=ridge_h3600`) 신호와 AND 조건
- **실거래 연동**: `MockPosition.open/close` → 업비트 REST API 주문 전환
