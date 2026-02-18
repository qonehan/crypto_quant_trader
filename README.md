# BTC Quant Bot (Prototype v1) — Codespaces Quickstart

> **Quickstart 4줄** — Codespaces/Dev Container 안에서 복사·붙여넣기로 바로 실행:
> ```bash
> cp .env.example .env
> docker compose -f .devcontainer/docker-compose.yml up -d db
> poetry install
> poetry run python -m app.bot
> ```
> 대시보드(별도 터미널):
> ```bash
> poetry run streamlit run app/dashboard.py
> # → http://localhost:8501 접속
> ```

---

## 목차

1. [이 프로젝트는 무엇을 하나요?](#1-이-프로젝트는-무엇을-하나요)
2. [구성 요소(아키텍처) 한눈에 보기](#2-구성-요소아키텍처-한눈에-보기)
3. [처음 1번만 하는 설정(최초 부팅)](#3-처음-1번만-하는-설정최초-부팅)
4. [실행 방법(가장 많이 쓰는 2개)](#4-실행-방법가장-많이-쓰는-2개)
5. [Claude Code(Anthropic) 세팅 — Codespaces에서 켜는 법](#5-claude-codeanthropic-세팅--codespaces에서-켜는-법)
6. [자주 겪는 문제(Troubleshooting)](#6-자주-겪는-문제troubleshooting)
7. [(선택) Paper Smoke Test 안내(test 프로필)](#7-선택-paper-smoke-test-안내test-프로필)
8. [라이선스/면책](#8-라이선스면책)

---

## 1) 이 프로젝트는 무엇을 하나요?

Upbit WebSocket으로 BTC/KRW 실시간 시장 데이터를 수집하고, 1초 단위로 리샘플한 뒤 변동성 기반 barrier(r\_t)를 계산합니다. 이를 토대로 상승/하락/없음 확률(p\_up / p\_down / p\_none)과 기대수익률(EV, EV\_rate)을 예측하고, exec\_v1 정산 로직으로 실제 체결 조건을 시뮬레이션합니다. 예측 결과는 paper trading 정책(Policy)에 따라 가상 매수·매도 사이클로 검증되며, Streamlit 대시보드에서 실시간으로 확인할 수 있습니다.

**현재는 paper trading(가상 매매) 전용입니다.** 실제 Upbit 현물 주문 API는 미연결·비활성 상태이므로 실제 자금 손실이 발생하지 않습니다.

---

## 2) 구성 요소(아키텍처) 한눈에 보기

```
[Upbit WebSocket]
      │  ticker / trade / orderbook
      ▼
[MarketState]  ──────────────────────────────────────┐
      │  1초 리샘플                                   │
      ▼                                               │
[Resampler] ──▶ market_1s (DB)                       │
      │                                               │
      ▼                                               │
[BarrierController] ──▶ barrier_state (DB)            │
      │  r_t, r_min_eff, sigma                        │
      ▼                                               │
[PredictorRunner] ──▶ predictions (DB)                │
      │  p_up / p_down / p_none / EV / EV_rate        │
      ▼                                               │
[Evaluator] ──▶ evaluation_results (DB)               │
      │  exec_v1: Brier / LogLoss / Calibration       │
      ▼                                               │
[PaperTradingRunner]                                  │
      │  Policy(strict|test) → ENTER/EXIT/STAY_FLAT   │
      ├──▶ paper_positions (DB)                       │
      ├──▶ paper_trades    (DB)                       │
      └──▶ paper_decisions (DB)                       │
                                                      │
[Streamlit Dashboard] ◀────────────────────────────────┘
      http://localhost:8501
```

### 핵심 DB 테이블

| 테이블 | 저장 내용 |
|---|---|
| `market_1s` | 1초 캔들(OHLC) + bid/ask + 거래량 + imbalance |
| `barrier_state` | 배리어 파라미터(r\_t, sigma, k\_eff, status) |
| `predictions` | 예측 결과(p\_up/p\_down/p\_none, EV, EV\_rate, z\_barrier) |
| `evaluation_results` | exec\_v1 정산 성과(Brier, LogLoss, Calibration) |
| `barrier_params` | 배리어 EWMA 피드백 상태 |
| `paper_positions` | 현재 포지션 상태(LONG/FLAT, cash, qty, entry/exit 정보) |
| `paper_trades` | 체결 기록(ENTER\_LONG / EXIT\_LONG, fee, pnl, hold\_sec) |
| `paper_decisions` | 매 틱 의사결정 로그(action, reason, reason\_flags, equity) |

### exec\_v1 정산 규칙 요약

- **진입 가격**: `best_ask × (1 + slippage_bps/10000)`
- **터치 판정**: `bid_high_1s`(TP) / `bid_low_1s`(SL) 기준, 슬리피지 포함
- **1초 내 양방 터치 시 DOWN(SL) 우선** — 보수적 평가

---

## 3) 처음 1번만 하는 설정(최초 부팅)

### 3.1 Codespaces/Dev Container로 여는 방법(권장)

GitHub 저장소 페이지에서 **Code → Codespaces → Create codespace** 를 선택하거나,
이미 클론된 폴더를 VS Code에서 열었다면:

1. `Ctrl+Shift+P` (Mac: `Cmd+Shift+P`) → Command Palette 열기
2. **`Dev Containers: Rebuild and Reopen in Container`** 선택
3. 컨테이너 빌드가 완료되면 터미널을 열어 다음 단계로 진행

> **핵심 차이**
> - **Dev Container 안(compose 네트워크 내)**: DB 호스트 `db` 로 바로 접속 가능
> - **Dev Container 밖(호스트 직접)**: `db` 이름 resolve 불가 → `localhost:5433` 사용 ([→ Troubleshooting](#db-host-db-resolve-실패))

`postCreateCommand`에 의해 Dev Container 첫 빌드 시 아래가 자동 실행됩니다:
```bash
poetry config virtualenvs.in-project true
cp .env.example .env   # .env가 없을 때만
poetry install
```
따라서 Dev Container를 사용하면 3.2~3.4를 건너뛸 수 있습니다. 직접 확인하고 싶다면 아래를 따르세요.

---

### 3.2 .env 만들기

```bash
cp .env.example .env
```

`.env` 파일을 열어 **DB\_URL을 환경에 맞게 수정**하세요:

```dotenv
# (권장) Dev Container / compose 네트워크 안
DB_URL=postgresql+psycopg://postgres:postgres@db:5432/quant

# (대안) compose 네트워크 밖 / 호스트 직접 실행
# DB_URL=postgresql+psycopg://postgres:postgres@localhost:5433/quant
```

**꼭 확인할 핵심 환경변수:**

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MODE` | `paper` | 실행 모드 (현재 paper 전용) |
| `SYMBOL` | `KRW-BTC` | 거래 심볼 |
| `PAPER_POLICY_PROFILE` | `strict` | `strict`(기본) 또는 `test`(smoke test용, **live 금지**) |
| `DB_URL` | `...@db:5432/...` | DB 접속 주소 (위 표 참조) |

**선택 설정(고급):**

- `H_SEC` — 배리어 수명(초, 기본 120)
- `PAPER_INITIAL_KRW` — 초기 가상 자본(기본 1,000,000)
- `PAPER_MAX_DRAWDOWN_PCT` — 최대 낙폭 정지 기준(기본 0.05)
- `PAPER_DAILY_LOSS_LIMIT_PCT` — 일일 손실 한도(기본 0.03)
- `FEE_RATE` — 수수료율(기본 0.0005)
- `SLIPPAGE_BPS` — 슬리피지(기본 2 bps)
- `ENTER_EV_RATE_TH` / `ENTER_PNONE_MAX` / `ENTER_SPREAD_BPS_MAX` — 진입 임계값

> **보안 주의**: `.env`는 절대 커밋하지 마세요. `.gitignore`에 이미 포함되어 있습니다.
> API Key/Secret이 필요한 경우 반드시 `.env`에만 기입하고 채팅·로그에 붙여넣지 마세요.

---

### 3.3 Postgres(DB) 켜기

```bash
docker compose -f .devcontainer/docker-compose.yml up -d db
```

컨테이너 상태 확인:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

**정상 출력 예시:**
```
NAMES                           STATUS                   PORTS
crypto_quant_trader_db_1        Up (healthy)             0.0.0.0:5433->5432/tcp
```

- `Up (healthy)` — 정상
- 포트는 호스트 `5433` → 컨테이너 `5432`로 매핑됨
- Dev Container 안이라면 `db:5432`, 밖이라면 `localhost:5433` 사용

DB가 뜨지 않거나 `db` 이름이 resolve 실패하면 → [Troubleshooting: DB host 'db' resolve 실패](#db-host-db-resolve-실패)

---

### 3.4 Python 의존성 설치(Poetry)

```bash
poetry install
```

완료 후 `.venv/` 폴더가 생성됩니다. Dev Container 첫 빌드 시 이미 실행됐다면 생략 가능합니다.

---

## 4) 실행 방법(가장 많이 쓰는 2개)

### 4.1 봇 실행(백엔드: 수집/리샘플/배리어/예측/정산/paper)

```bash
poetry run python -m app.bot
```

**기대 로그 예시:**
```
[INFO] app.marketdata.upbit_ws: WS connected to wss://api.upbit.com/websocket/v1
[INFO] app.barrier.controller: Barrier: r_t=0.001869 r_min_eff=0.001869 cost=0.001699 sigma_1s=0.000022 status=WARMUP n=14
[INFO] app.predictor.runner: Pred(v1): t0=10:04:10 r_t=0.001869 p_none=0.9900 p_up=0.0050 p_down=0.0050 ev_rate=-0.00001572
[INFO] app.evaluator.evaluator: EvalMetrics(exec_v1): n=120 brier=0.0123 logloss=0.0456
[INFO] app.trading.runner: Paper: pos=FLAT action=STAY_FLAT reason=EV_RATE_LOW cash=1000000 equity=1000000 dd=0.00% profile=strict
```

- 처음 약 2분은 Barrier WARMUP 상태 → 예측이 기본값으로 나옴
- `Ctrl+C`로 종료

---

### 4.2 대시보드(인터페이스) 실행

별도 터미널을 열어 실행하세요:

```bash
poetry run streamlit run app/dashboard.py
```

**접속:**

| 환경 | 주소 |
|---|---|
| 로컬 / Dev Container | `http://localhost:8501` |
| GitHub Codespaces | VS Code 하단 **Ports** 탭 → `8501` → **Open in Browser** |

**중지:**
```
Ctrl+C
```

---

## 5) Claude Code(Anthropic) 세팅 — Codespaces에서 켜는 법

목표: 새 Codespace를 만든 직후에도 Claude Code를 바로 사용할 수 있게 합니다.

### 5.1 VS Code 확장(권장)

1. VS Code 왼쪽 사이드바 **Extensions** 탭(`Ctrl+Shift+X`) 열기
2. `Claude Code` 검색 → **Install**
3. 설치 후 사이드바에 Claude 아이콘이 생기거나, Command Palette(`Ctrl+Shift+P`)에서 `Claude Code` 검색으로 열기
4. 처음 실행 시 **브라우저 인증(로그인)** 화면이 뜸 — Anthropic 계정으로 로그인

---

### 5.2 터미널 CLI(선택)

Codespaces 리눅스 환경에서 터미널로 직접 설치·실행하려면:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude
```

첫 실행 시 `/login` 명령 또는 브라우저 인증 안내가 뜰 수 있습니다.

> **보안 주의**: `.env`의 키/토큰, DB 비밀번호 등을 Claude 채팅창에 직접 붙여넣지 마세요.

---

### 5.3 Claude Code에게 가장 먼저 시킬 "초기 점검 프롬프트"

새 Codespace를 열었을 때 아래 프롬프트를 Claude Code 채팅에 **그대로 복사·붙여넣기**하면 환경 전체를 자동으로 점검·수정해 줍니다:

```
지금 이 Codespace/Dev Container 환경을 점검하고 문제를 해결해 줘.
순서대로 확인해:

1. 현재 Dev Container 안인지 밖인지 판단해 (hostname, /etc/hosts, docker network 등으로 확인)
2. .env 파일이 있는지 확인하고, 없으면 .env.example에서 복사해줘.
   그리고 DB_URL이 현재 환경에 맞는지 진단해:
   - Dev Container 안이면 @db:5432
   - 밖이면 @localhost:5433
3. db 컨테이너가 Up(healthy) 상태인지 확인해. 꺼져 있으면:
   docker compose -f .devcontainer/docker-compose.yml up -d db
   를 실행하고 healthy 될 때까지 기다려줘.
4. poetry install이 이미 됐는지 확인해. .venv가 없으면 poetry install 실행해줘.
5. bot을 30초 동안 백그라운드로 실행하고 아래를 확인해:
   - "WS connected" 로그가 보이는지
   - "Barrier:" 로그가 보이는지
   - 오류(ERROR/Exception)가 있으면 원인과 해결책을 알려줘
6. streamlit을 백그라운드로 실행하고 http://localhost:8501/healthz 가 200 응답하는지 확인해줘.
7. 모든 단계 결과를 표로 정리해서 보여줘. 실패한 단계가 있으면 해결 명령을 단계별로 제시해줘.
```

---

## 6) 자주 겪는 문제(Troubleshooting)

### DB host 'db' resolve 실패

**증상**: `could not translate host name "db"` 또는 DB 연결 오류
**원인**: compose 네트워크 밖(호스트 직접 실행)에서는 `db`라는 호스트명을 알 수 없음
**해결**:
```bash
# 방법 1: Dev Container 안에서 실행 (권장)
# Command Palette → "Dev Containers: Rebuild and Reopen in Container"

# 방법 2: .env의 DB_URL을 localhost로 변경
# DB_URL=postgresql+psycopg://postgres:postgres@localhost:5433/quant

# DB가 꺼져 있다면 먼저 기동
docker compose -f .devcontainer/docker-compose.yml up -d db
```

---

### 8501 포트가 안 열림

**증상**: 브라우저에서 `localhost:8501` 접속 시 "연결할 수 없음"
**원인**: streamlit이 실행 중이 아니거나, Codespaces에서 포트 포워딩이 안 됨
**해결**:
```bash
# streamlit 실행 여부 확인
ps aux | grep streamlit

# 다시 실행
poetry run streamlit run app/dashboard.py --server.port 8501 --server.address 0.0.0.0

# Codespaces: VS Code 하단 Ports 탭 → 8501 포트 → "Open in Browser"
```

---

### Upbit WS가 수신이 안 됨

**증상**: 봇 시작 후 시장 데이터 로그가 안 찍히거나, 계속 재연결(reconnect) 로그가 반복됨
**원인**: 네트워크 방화벽, Upbit WS 서버 일시 장애, 또는 인터넷 연결 불안정
**해결**:
```bash
# 봇 재시작 (Ctrl+C 후 재실행)
poetry run python -m app.bot

# 재연결 로그 확인: "WS reconnecting..." → 자동 재시도 중이면 정상
# 계속 실패하면 Upbit 서비스 상태 페이지 확인
```

---

### "거래가 0건"인데 정상인가요?

**증상**: `paper_trades` 테이블이 비어 있고, 봇은 계속 `STAY_FLAT` 출력
**원인**: `strict` 프로필은 의도적으로 보수적입니다. p\_none이 높거나(>0.70), EV\_rate가 낮거나, 비용(수수료+슬리피지+스프레드)이 r\_t보다 크면 진입하지 않습니다.
**해결**:
```bash
# paper_decisions 테이블에서 reason/flags 확인
poetry run python - <<'PY'
from sqlalchemy import create_engine, text
from app.config import load_settings
s = load_settings()
e = create_engine(s.DB_URL)
with e.connect() as c:
    rows = c.execute(text("""
        SELECT reason, reason_flags, count(*) as cnt
        FROM paper_decisions WHERE symbol=:sym
        GROUP BY reason, reason_flags ORDER BY cnt DESC LIMIT 10
    """), {"sym": s.SYMBOL}).fetchall()
for r in rows:
    print(r)
PY

# 진입이 잦은 test 프로필로 동작 확인하려면 → 섹션 7 참조
# strict로 운영하는 것이 기본이며 정상 동작입니다
```

---

## 7) (선택) Paper Smoke Test 안내(test 프로필)

> **경고**: `test` 프로필은 진입 조건이 극도로 완화된 **검증 전용** 설정입니다.
> 실거래(live) 전환 시 반드시 `PAPER_POLICY_PROFILE=strict`로 복귀하세요.

`.env`에 아래를 추가/수정하면 수 분 내에 ENTER\_LONG → EXIT\_LONG 사이클이 발생합니다:

```dotenv
PAPER_POLICY_PROFILE=test

# test 진입 조건 완화 (검증용, live 금지)
TEST_ENTER_EV_RATE_TH=-0.001
TEST_ENTER_PNONE_MAX=0.995
TEST_ENTER_PDIR_MARGIN=-1.0
TEST_COST_RMIN_MULT=0.0
TEST_MAX_POSITION_FRAC=0.05
TEST_MAX_ENTRIES_PER_HOUR=2
TEST_COOLDOWN_SEC=60
```

봇 실행 후 약 2분(H\_SEC=120) 뒤에 TIME exit이 발생합니다.

**strict로 복귀:**
```dotenv
PAPER_POLICY_PROFILE=strict
```

smoke test 전체 절차 및 결과는 [`step6-3.md`](step6-3.md) 참조.

---

## 8) 라이선스/면책

- **투자 조언 아님**: 이 프로젝트는 교육·연구 목적의 프로토타입이며, 어떠한 투자 결과에 대해서도 책임지지 않습니다.
- **실거래 전 충분한 검증 필수**: 실제 Upbit API Key/Secret을 연결하거나 실거래 모드로 전환하기 전에, paper trading 환경에서 장기간 충분히 검증하세요. 코드·설정 오류로 인한 실제 자금 손실은 사용자 책임입니다.
- **운영 리스크 고려**: 네트워크 단절, Upbit API 레이트 리밋, 봇 프로세스 강제 종료, DB 장애 등 다양한 운영 리스크가 존재합니다. 실거래 운영 시 정지 조건(MAX\_DRAWDOWN, DAILY\_LOSS\_LIMIT), 알림, 모니터링을 별도로 구축하세요.
