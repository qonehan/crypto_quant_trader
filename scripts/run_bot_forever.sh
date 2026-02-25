#!/usr/bin/env bash
# run_bot_forever.sh — 봇 자동 재시작 supervisor
#
# 사용법:
#   bash scripts/run_bot_forever.sh              # 포어그라운드 실행
#   nohup bash scripts/run_bot_forever.sh &      # 백그라운드 실행
#
# 봇이 크래시/네트워크 오류로 종료되면 지수 백오프로 재시작한다.
# Codespaces 환경 자체가 내려가는 건 막지 못하지만, 프로세스 크래시는 복구한다.

set -euo pipefail

mkdir -p logs

BACKOFF=3
MAX_BACKOFF=60
ATTEMPT=0

echo "[run_bot_forever] started at $(date -u '+%F %T')" | tee -a logs/bot_supervisor.log

while true; do
    ATTEMPT=$((ATTEMPT + 1))
    echo "[run_bot_forever] attempt=${ATTEMPT} launching bot at $(date -u '+%F %T')" \
        | tee -a logs/bot_supervisor.log

    # run bot; capture exit code without set -e killing the loop
    poetry run python -m app.bot 2>&1 | tee -a logs/bot.log
    RC=${PIPESTATUS[0]}

    echo "[run_bot_forever] bot exited rc=${RC} at $(date -u '+%F %T')" \
        | tee -a logs/bot_supervisor.log
    echo "[run_bot_forever] sleeping ${BACKOFF}s before restart (attempt ${ATTEMPT})" \
        | tee -a logs/bot_supervisor.log

    sleep "${BACKOFF}"

    # exponential backoff with cap
    BACKOFF=$((BACKOFF * 2))
    if [ "${BACKOFF}" -gt "${MAX_BACKOFF}" ]; then
        BACKOFF="${MAX_BACKOFF}"
    fi
done
