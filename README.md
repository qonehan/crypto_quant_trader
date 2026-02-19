# BTC Quant Bot (Prototype v1) — Codespaces Quickstart

> **Quickstart 4줄** — Dev Container 안에서 복사·붙여넣기로 바로 실행:
> ```bash
> cp .env.example .env
> docker compose -f .devcontainer/docker-compose.yml up -d db
> poetry install
> poetry run python -m app.bot
> ```
> 대시보드(별도 터미널):
> ```bash
> poetry run streamlit run app/dashboard.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
> # → Ports 탭에서 8501 포트를 열어 접속
> ```

---

## 목차

1. [이 프로젝트는 무엇을 하나요?](#1-이-프로젝트는-무엇을-하나요)
2. [Dev Container 시작](#2-dev-container-시작)
3. [처음 한 번만 하는 설정](#3-처음-한-번만-하는-설정)
4. [봇 실행](#4-봇-실행)
5. [대시보드 실행](#5-대시보드-실행)
6. [Upbit API 키 설정 (선택)](#6-upbit-api-키-설정-선택)
7. [Upbit TEST 자동 연동 설정](#7-upbit-test-자동-연동-설정)
8. [자동 연동 상태 점검](#8-자동-연동-상태-점검)
9. [live(실거래) 금지 안내](#9-live실거래-금지-안내)
10. [자주 겪는 문제](#10-자주-겪는-문제)
11. [라이선스 / 면책](#11-라이선스--면책)

---

## 1. 이 프로젝트는 무엇을 하나요?

Upbit KRW-BTC 실시간 데이터를 기반으로:
- 변동성 배리어(Barrier) 계산 → 예측(Predictor) → 매매 신호(Paper Trading)
- **Paper Trading**: 실제 돈 없이 가상 매매 성과 추적
- **Shadow/Test 모드**: Upbit API를 호출해 주문 검증 (실거래 없음)
- **대시보드**: Streamlit으로 실시간 지표 시각화

---

## 2. Dev Container 시작

### GitHub Codespaces
1. 이 저장소에서 **`<> Code` → `Codespaces` → `Create codespace on main`** 클릭
2. VS Code가 열리면 좌하단에 `Dev Container: ...` 표시가 뜰 때까지 대기
3. 터미널이 열리면 컨테이너 안에 있는 것 — `poetry` 명령이 바로 사용 가능

### 로컬 VS Code
1. VS Code에서 저장소 폴더 열기
2. **`Dev Containers: Reopen in Container`** 명령 실행 (Command Palette `Ctrl+Shift+P`)
3. 컨테이너 빌드 완료 후 터미널 사용

> **`poetry: command not found`** 에러가 나면 **컨테이너 밖**에 있는 것입니다.
> Dev Containers: Reopen in Container를 실행하세요.

---

## 3. 처음 한 번만 하는 설정

```bash
# 1. 환경 변수 파일 생성
cp .env.example .env

# 2. PostgreSQL 컨테이너 시작 (이미 devcontainer가 자동으로 시작했으면 생략 가능)
docker compose -f .devcontainer/docker-compose.yml up -d db

# 3. Python 패키지 설치
poetry install
```

DB 연결 확인:
```bash
poetry run python -c "
from app.config import load_settings
from app.db.session import get_engine
from sqlalchemy import text
engine = get_engine(load_settings())
with engine.connect() as c:
    print('DB OK:', c.execute(text('SELECT 1')).scalar())
"
```

---

## 4. 봇 실행

```bash
poetry run python -m app.bot
```

정상 기동 로그 예시:
```
[INFO] app.db.migrate: All migrations complete (v1 + Step 7 + Step 8 + Step 9 + Step 11)
[INFO] __main__: Paper trading enabled
[INFO] __main__: ShadowExecutionRunner enabled (mode=shadow)
[INFO] __main__: UpbitAccountRunner skipped (UPBIT_ACCESS_KEY not set)
[INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
[INFO] app.barrier.controller: Barrier: r_t=0.001740 ... status=WARMUP
[INFO] app.predictor.runner: Pred(v1): ... action=STAY_FLAT
[INFO] app.trading.runner: Paper: pos=FLAT action=STAY_FLAT ...
```

- `err=0 reconn=0` 이면 WebSocket 정상
- `UpbitAccountRunner skipped` 는 API 키 없을 때 정상

---

## 5. 대시보드 실행

별도 터미널에서:

```bash
poetry run streamlit run app/dashboard.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true
```

접속:
- **Codespaces**: Ports 탭 → `8501` 포트 → `Open in Browser`
- **로컬**: `http://localhost:8501`

대시보드 섹션:
| 섹션 | 내용 |
|------|------|
| market_1s | 실시간 호가/체결 데이터 |
| [A] Barrier | 변동성 배리어 상태 |
| [B] Probabilistic | Brier/LogLoss 평가 지표 |
| [C] Calibration | 확률 교정 테이블 |
| [D] EV/Cost | 기대값/비용 진단 |
| [E] Paper Trading | 가상 매매 성과 |
| [F] Upbit Exchange | API 연동 상태 / Ready 표시 |

---

## 6. Upbit API 키 설정 (선택)

> API 키는 `.env` 파일에만 저장하고 **절대 커밋하지 마세요**.

`.env` 파일에서 아래 두 줄의 **주석(`#`)을 제거**하고 값 입력:

```ini
UPBIT_ACCESS_KEY=발급받은_액세스_키
UPBIT_SECRET_KEY=발급받은_시크릿_키
```

키 설정 확인 (키 원문은 출력하지 않음):
```bash
poetry run python -c "
from app.config import load_settings
s = load_settings()
print('has_access_key =', bool(s.UPBIT_ACCESS_KEY))
print('has_secret_key =', bool(s.UPBIT_SECRET_KEY))
"
```

키가 있으면 봇 재시작 시 `UpbitAccountRunner started` 로그가 나타납니다.

---

## 7. Upbit TEST 자동 연동 설정

> **실거래(live) 없음** — `POST /v1/orders/test` 만 호출 (파라미터 검증용)

paper_trade가 발생할 때마다 자동으로 `/v1/orders/test`를 호출하도록 설정:

`.env` 파일에서 아래 항목 주석 해제 및 값 설정:

```ini
# Upbit API 키 (필수)
UPBIT_ACCESS_KEY=발급받은_액세스_키
UPBIT_SECRET_KEY=발급받은_시크릿_키

# TEST 모드 활성화
UPBIT_TRADE_MODE=test
UPBIT_ORDER_TEST_ENABLED=true
UPBIT_TEST_ON_PAPER_TRADES=true

# Paper 프로필 (test 모드에서 신호가 잘 나오도록)
PAPER_POLICY_PROFILE=test
```

설정 후 봇 재시작:
```bash
poetry run python -m app.bot
```

paper_trade가 1건 이상 생기면 자동으로 `/v1/orders/test` 호출 → `upbit_order_attempts` 테이블에 `mode=test, status=test_ok` 기록.

---

## 8. 자동 연동 상태 점검

봇이 실행 중인 상태에서 별도 터미널로 실행:

```bash
poetry run python -m app.exchange.paper_test_smoke --window 600
```

**PASS 출력 예시:**
```
============================================================
paper_test_smoke: window=600s  symbol=KRW-BTC
============================================================
[1] paper_trades (last 600s): 3
[2] upbit_order_attempts mode=test status=test_ok (last 600s): 3
✅ PASS — paper_trades=3  test_ok=3
============================================================
```

**FAIL 시 blocked_reasons 진단:**
```
❌ FAIL — paper=3  test_ok=0
[3] blocked/throttled/error rows:
  ts=2026-02-19 04:10:00  action=ENTER_LONG  status=blocked  error_msg=blocked: PAPER_PROFILE_MISMATCH
    blocked_reasons=['PAPER_PROFILE_MISMATCH', 'AUTO_TEST_DISABLED']
[4] blocked_reasons top 5:
  PAPER_PROFILE_MISMATCH: 3건
  AUTO_TEST_DISABLED: 3건
```

→ `.env`에서 `PAPER_POLICY_PROFILE=test` 설정 후 재시작.

E2E 검증 (수동):
```bash
poetry run python -m app.exchange.e2e_test
```

리컨실리에이션 (주문/계좌 정합성):
```bash
poetry run python -m app.exchange.reconcile
```

---

## 9. live(실거래) 금지 안내

> **경고: 이 가이드에는 live 활성화 방법을 상세히 기재하지 않습니다.**

실거래는 별도 검토 및 승인 없이 절대 활성화하지 마세요.
live 모드는 4중 안전 가드가 있으며, 이 저장소의 현재 구현은 `TEST` 단계까지만 검증합니다.

---

## 10. 자주 겪는 문제

### `poetry: command not found`
→ Dev Container 밖에 있음. VS Code에서 **`Dev Containers: Reopen in Container`** 실행.

### DB 연결 오류 (`could not translate host name "db"`)
→ Docker Compose로 DB 컨테이너가 실행 중인지 확인:
```bash
docker compose -f .devcontainer/docker-compose.yml up -d db
docker compose -f .devcontainer/docker-compose.yml ps
```

### `UpbitAccountRunner skipped`
→ API 키 미설정 시 정상. 키 설정 후 봇 재시작.

### `test_ok가 안 쌓임`
→ 아래 순서로 진단:
1. `poetry run python -m app.exchange.paper_test_smoke --window 600`
2. blocked_reasons 확인 → `.env` 설정 수정
3. 봇 재시작

### `Ports 탭에 8501이 없음`
→ Streamlit 실행 명령에 `--server.address 0.0.0.0 --server.headless true` 포함 여부 확인.

---

## 11. 라이선스 / 면책

- 이 소프트웨어는 연구/학습 목적 프로토타입입니다.
- 실제 금융 손실에 대해 제작자는 책임지지 않습니다.
- **라이브 트레이딩 활성화 전에 충분한 검토를 거치세요.**
