# Step DL-4-1: 운영 안정성 강화 패치 보고서

## 추가된 기능 4종 요약

| # | 기능 | 구현 위치 | 핵심 포인트 |
|---|---|---|---|
| 1 | **로그 파일 저장** | `setup_logger()` | 날짜별 `.log`, 터미널+파일 동시 출력 |
| 2 | **거래 이력 CSV** | `append_buy_csv()` / `append_sell_csv()` | BUY→행 추가, SELL→왕복 한 줄 기록 |
| 3 | **재시도 로직** | `call_with_retry()` | 5초 대기 × 최대 3회, 실패 시 `None` |
| 4 | **하트비트** | 메인 루프 내부 | 매 10분(`HEARTBEAT_SEC=600`) 생존 로그 |

---

## 1. 로그 파일 저장

### 설계
```
logs/
  mock_trade_20260313.log   ← 날짜별 자동 생성
  mock_trade_20260314.log   ← 자정 이후 재실행 시 새 파일
```

- Python `logging` 모듈 사용 — `StreamHandler`(콘솔) + `FileHandler`(파일) 동시 등록
- `mode="a"` (append): 봇을 재시작해도 같은 날 로그는 **누적** 기록
- 포맷: `2026-03-13 14:02:03  INFO   ██ [MOCK BUY] ...`

### 코드 구조
```python
def setup_logger() -> logging.Logger:
    lg = logging.getLogger("mock_trade")
    lg.addHandler(StreamHandler(sys.stdout))          # 터미널
    lg.addHandler(FileHandler(f"logs/mock_trade_{today}.log", mode="a"))
    return lg
```

> **모든 `print()`를 `logger.info()`로 교체** — 이후 추가 코드도 `logger`를 통해 자동 파일 기록.

---

## 2. 거래 이력 CSV

### 파일 경로
```
artifacts/trade_history.csv
```

### 컬럼 구조
```
timestamp, action,
entry_time, entry_price, entry_prob,
exit_time, exit_price, exit_prob,
ret_pct, held_min, reason,
cum_trade_count, cum_pnl_pct
```

### 기록 전략
| 이벤트 | 기록 내용 |
|---|---|
| `[MOCK BUY]` | `action=BUY`, 진입 정보만 기록 (청산 정보는 공란) |
| `[MOCK SELL]` | `action=SELL`, **왕복 정보 한 줄**에 완전 기록 |

- 파일이 없으면 헤더 자동 생성 (`_ensure_trade_csv()`)
- 봇 재시작 시 기존 CSV에 **이어서 추가** (덮어쓰기 없음)

### 샘플 행
```csv
timestamp,action,entry_time,entry_price,...,ret_pct,reason,cum_pnl_pct
2026-03-13 14:02:00,BUY,2026-03-13 14:02:00,128000000,0.5800,,,,,,,
2026-03-13 14:07:00,SELL,2026-03-13 14:02:00,128000000,0.5800,2026-03-13 14:07:00,128500000,0.4200,+0.3906,5,PROB_WEAK,1,+0.3906
```

---

## 3. 재시도 로직

### `call_with_retry(fn, *args, label, **kwargs)`

```
fn() 호출
  └─ 성공 → 결과 반환
  └─ 예외 → WARNING 로그 + 5초 대기
           → 재시도 (최대 3회)
           → 3회 모두 실패 → None 반환
```

### 적용 지점
| 함수 | 래핑 방식 |
|---|---|
| `fetch_ohlcv()` | `call_with_retry(_fetch_ohlcv_once, count, label="Upbit OHLCV")` |
| `fetch_macro_latest()` | `call_with_retry(_fetch_macro_once, label="Macro(yfinance)")` |

### 봇 생존 전략
```python
ohlcv = fetch_ohlcv()
if ohlcv is None:
    logger.error("3회 재시도 모두 실패. 이번 봉 스킵.")
    continue          # ← 죽지 않고 다음 분까지 대기
```

> 네트워크 오류가 발생해도 **봇은 계속 살아있으며**, 다음 분봉에서 자동 재개.

---

## 4. 하트비트

```python
HEARTBEAT_SEC = 600   # 10분

if (now - last_heartbeat).total_seconds() >= HEARTBEAT_SEC:
    logger.info(
        f"[Heartbeat] 봇 정상 작동 중 | 루프={loop_count}회 "
        f"거래={pos.trade_count}회 누적PnL={pos.total_pnl_pct:+.3f}%"
    )
    last_heartbeat = now
```

- 매분 루프 시작 시 경과 시간 체크 → 10분마다 생존 로그 1행 출력
- 로그 파일에도 기록되므로 **원격 서버에서 `tail -f`로 모니터링 가능**

---

## 검증 결과

```
2026-03-13 07:16:29  INFO   로그 파일: logs/mock_trade_20260313.log
2026-03-13 07:16:29  INFO   거래 이력 CSV 생성: artifacts/trade_history.csv
2026-03-13 07:16:29  WARNING  [Retry 1/3] Test 오류: ConnectionError — 5초 후 재시도
2026-03-13 07:16:34  WARNING  [Retry 2/3] Test 오류: ConnectionError — 5초 후 재시도
2026-03-13 07:16:39  WARNING  [Retry 3/3] Test 오류: ConnectionError — 재시도 포기
2026-03-13 07:16:39  INFO   재시도 검증 통과: 3회 시도 후 None 반환
2026-03-13 07:16:39  INFO   ██ [MOCK BUY]  가격=128,000,000원  확률=0.5800
2026-03-13 07:16:39  INFO   ▼  [MOCK SELL] 수익률=+0.391%  보유=5분  사유=PROB_WEAK
2026-03-13 07:16:39  INFO   CSV 검증 통과
2026-03-13 07:16:39  INFO   로그 파일 검증 통과
```

---

## 파라미터 한눈에 보기

```python
THRESHOLD        = 0.55    # 진입 확률 임계값
STOP_LOSS_PCT    = -0.005  # 손절 (-0.5%)
EXIT_PROB_FLOOR  = 0.45    # 확률 약화 청산선
RETRY_MAX        = 3       # 최대 재시도 횟수
RETRY_WAIT_SEC   = 5       # 재시도 대기 (초)
HEARTBEAT_SEC    = 600     # 하트비트 주기 (10분)
MACRO_REFRESH_HOURS = 12   # 매크로 갱신 주기
```

---

## 실행 및 모니터링

```bash
# 실행
poetry run python scripts/dl/step_dl_4_mock_trade.py

# 실시간 로그 모니터링 (별도 터미널)
tail -f logs/mock_trade_$(date +%Y%m%d).log

# 거래 이력 확인
cat artifacts/trade_history.csv
```
