# Step DL-1: API 기반 과거 데이터 수집 및 정제

## 1. 작업 목표

로컬 DB(PostgreSQL) 의존도를 완전히 제거하고, **업비트 REST API(pyupbit)** 와 **yfinance** 만을 사용하여 약 6개월치 KRW-BTC 1분봉 과거 데이터를 수집·정제하여 `data/datasets/btc_1m_dl.parquet`에 저장하는 파이프라인을 구축한다.

---

## 2. 수정/생성 파일

| 파일 경로 | 역할 |
|---|---|
| `scripts/dl/step_dl_1_collect_data.py` | 메인 데이터 수집 파이프라인 (신규 생성) |
| `data/datasets/btc_1m_dl.parquet` | 최종 학습용 데이터셋 (신규 생성) |
| `pyproject.toml` | pyupbit, yfinance, torch, numpy 패키지 추가 |

---

## 3. 실행 결과

### 업비트 1분봉 수집
```
[Upbit] KRW-BTC 1분봉 수집 시작 (목표 180일)
  호출  100회 — KST 최초 2026-02-26 21:29
  호출  200회 — KST 최초 2026-02-12 22:29
  ...
  호출 1200회 — KST 최초 2025-09-25 20:39
[Upbit] 수집 완료: 257,400행  (2025-09-13 17:12:00 ~ 2026-03-12 20:27:00)
```

### yfinance 매크로 지표
```
[Macro] DX-Y.NYB (DXY): 124행
[Macro] ^GSPC (S&P500): 123행
[Macro] GC=F (Gold): 124행
[Macro] BTC-USD (글로벌 BTC): 181행
```

### 최종 데이터셋
| 항목 | 값 |
|---|---|
| 기간 | 2025-09-13 ~ 2026-03-12 (약 6개월) |
| 전체 행 수 | 257,400 |
| 유효 행 수 (target 존재) | 257,340 |
| 피처 수 | 54개 |
| LONG(1) 비율 | 34.5% |
| FLAT(0) 비율 | 65.5% |
| 파일 크기 | 76.4 MB |

### 피처 목록 (54개)
- **OHLCV**: open, high, low, close, volume
- **수익률**: ret_1m, ret_5m, ret_15m, ret_60m
- **이동평균 & 거리**: ma{5,15,30,60,120,240}, ma{N}_dist
- **변동성**: vol_5m, vol_15m, vol_60m, vol_ratio_5m, vol_ratio_60m, vol_std_15m
- **기술 지표**: rsi14, macd, macd_signal, macd_hist, bb_upper, bb_lower, bb_pct, bb_width, atr14, atr_norm
- **가격 채널**: channel_high_20, channel_low_20, channel_pos
- **시간 인코딩**: hour_sin, hour_cos, dow_sin, dow_cos
- **매크로**: dxy, dxy_ret, sp500, sp500_ret, gold, gold_ret, btc_usd, btc_usd_ret
- **타겟**: future_ret (60분 후), target (0/1 이진)

---

## 4. 트러블슈팅

### 문제 1: offset-aware/naive datetime 비교 오류
- **원인:** `datetime.now(tz=timezone.utc)` (aware) vs pyupbit 내부 naive datetime 비교
- **해결:** `datetime.utcnow()` 사용 (naive UTC)

### 문제 2: 페이지네이션 오동작 (같은 구간 반복 수집)
- **원인:** pyupbit 반환 인덱스는 **KST** 기준, `to` 파라미터는 **UTC** 기준으로 처리됨
  - 예: 현재 UTC 11:25 → KST 20:25. chunk.index[0]=KST 17:06을 to에 그대로 전달하면 UTC 17:06(미래)으로 인식 → 항상 최신 데이터 반환
- **해결:** `oldest_utc = chunk.index[0] - timedelta(hours=9)` → UTC 변환 후 1분 차감
```python
oldest_kst = chunk.index[0]
oldest_utc = oldest_kst - timedelta(hours=9)
to_utc = oldest_utc - timedelta(minutes=1)
```

### 문제 3: torch poetry 의존성 충돌
- **원인:** `python = "^3.11"` (최대 `<4.0`) + triton `<3.15` 제약 충돌
- **해결:** `python = ">=3.11,<3.15"` + `websockets = ">=12.0.0"` 로 변경

---

## 5. 타겟 설계

```
horizon = 60분, fee_rate = 0.05% (업비트 maker)
future_ret = close(t+60) / close(t) - 1
target = 1 (LONG)  if future_ret > 0.001 (왕복 수수료 0.1% 초과)
target = 0 (FLAT)  otherwise
```

---

## 6. 다음 단계 (Next Step)

**Step DL-2: DL 데이터 변환 (Windowing)**

- `btc_1m_dl.parquet`를 PyTorch Dataset으로 변환
- 입력 Shape: `(Batch, Sequence=60, Features=N)`
- 정규화(MinMaxScaler 또는 StandardScaler) 적용
- Train / Val / Test 시간순 분할 (Data Leakage 방지)
- `scripts/dl/step_dl_2_dataset.py` 및 `app/predictor/dl_dataset.py` 구현
