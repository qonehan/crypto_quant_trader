#!/usr/bin/env bash
# setup_always_on.sh — 로컬 PC / VM에서 항상-켜진 수집 환경 세팅 가이드
#
# 목적: Codespaces idle 문제를 해결하기 위해 항상 켜진 환경에서 봇을 운영
# 전제: Python 3.11+, Poetry, Docker가 설치된 로컬/VM
#
# 사용법:
#   git clone <repo> ~/crypto_quant_trader && cd ~/crypto_quant_trader
#   cp .env.example .env     # .env 편집 후
#   bash scripts/setup_always_on.sh
#
# 이 스크립트는 아래 작업을 수행합니다:
#   1) Python 의존성 설치 (poetry install)
#   2) PostgreSQL Docker 컨테이너 생성/시작 (이미 실행 중이면 skip)
#   3) DB 연결 확인
#   4) 봇 supervisor 백그라운드 실행
#   5) 5분 후 첫 상태 스냅샷

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_DIR}"

echo "================================================================"
echo "  Always-on 수집 환경 세팅 (로컬/VM)"
echo "  repo_dir = ${REPO_DIR}"
echo "  $(date -u '+%F %T')"
echo "================================================================"

mkdir -p logs

# ─── (1) Poetry install ──────────────────────────────────────────────────────
echo ""
echo "[1/5] poetry install..."
poetry install --with dev 2>&1 | tail -5
echo "  OK"

# ─── (2) PostgreSQL Docker 컨테이너 ─────────────────────────────────────────
echo ""
echo "[2/5] PostgreSQL Docker 컨테이너..."
if docker inspect quant-db >/dev/null 2>&1; then
    STATUS="$(docker inspect -f '{{.State.Status}}' quant-db)"
    echo "  quant-db already exists (status=${STATUS})"
    if [[ "${STATUS}" != "running" ]]; then
        docker start quant-db
        echo "  quant-db started"
    else
        echo "  quant-db already running — skip"
    fi
else
    echo "  Creating quant-db container..."
    docker volume create quant_pgdata 2>/dev/null || true
    docker run -d --name quant-db \
        --restart unless-stopped \
        -e POSTGRES_PASSWORD=postgres \
        -e POSTGRES_USER=postgres \
        -e POSTGRES_DB=quant \
        -p 127.0.0.1:5432:5432 \
        -v quant_pgdata:/var/lib/postgresql/data \
        postgres:16
    echo "  quant-db created and started"
fi

# DB가 준비될 때까지 대기
echo "  Waiting for PostgreSQL to be ready..."
for i in $(seq 1 30); do
    if docker exec quant-db pg_isready -U postgres -q 2>/dev/null; then
        echo "  PostgreSQL ready (attempt ${i})"
        break
    fi
    sleep 1
done

# ─── (3) DB 연결 확인 ─────────────────────────────────────────────────────────
echo ""
echo "[3/5] DB 연결 확인..."
poetry run python3 - << 'PY'
from app.config import load_settings
from sqlalchemy import create_engine, text

s = load_settings()
e = create_engine(s.DB_URL, pool_pre_ping=True)
with e.connect() as c:
    c.execute(text("SELECT 1"))
host = s.DB_URL.split("@")[-1] if "@" in s.DB_URL else s.DB_URL
print(f"  DB_CONNECT_OK: {host}")
PY

# ─── (4) 봇 supervisor 시작 ─────────────────────────────────────────────────
echo ""
echo "[4/5] 봇 supervisor 시작..."

# 이미 실행 중인 supervisor/bot 중지
pkill -f "run_bot_forever.sh" 2>/dev/null || true
pkill -f "app.bot" 2>/dev/null || true
sleep 2

nohup bash scripts/run_bot_forever.sh >> logs/bot_supervisor.log 2>&1 &
SUPER_PID=$!
echo "  supervisor PID=${SUPER_PID}"
sleep 3

if ps -p "${SUPER_PID}" >/dev/null 2>&1; then
    echo "  supervisor running OK"
else
    echo "  ❌ supervisor died unexpectedly — check logs/bot_supervisor.log"
    exit 1
fi

# ─── (5) 상태 요약 ────────────────────────────────────────────────────────────
echo ""
echo "[5/5] 상태 요약..."
echo "  supervisor PID  = ${SUPER_PID}"
echo "  bot.log         = ${REPO_DIR}/logs/bot.log"
echo "  supervisor.log  = ${REPO_DIR}/logs/bot_supervisor.log"
echo ""
echo "  다음 명령으로 상태를 확인하세요:"
echo "    tail -f logs/bot.log"
echo "    bash scripts/snapshot_health.sh 2 600"
echo ""
echo "  12시간 후 연속성 게이트를 확인하세요:"
echo "    poetry run python -m app.diagnostics.continuity_check --hours 12 --pred-gap-sec 60 --mkt-gap-sec 10"
echo ""
echo "  24시간 후 표준 평가를 실행하세요:"
echo "    bash scripts/run_full_eval_alt15.sh 600 24"
echo ""
echo "================================================================"
echo "  Setup 완료: $(date -u '+%F %T')"
echo "================================================================"
