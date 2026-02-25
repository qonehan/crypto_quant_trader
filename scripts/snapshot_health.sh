#!/usr/bin/env bash
# snapshot_health.sh — 파이프라인 연속성/건강 상태 스냅샷 기록
#
# 사용법:
#   bash scripts/snapshot_health.sh           # 기본: --hours 48 --window 600
#   bash scripts/snapshot_health.sh 2 600     # hours=2, window=600
#   bash scripts/snapshot_health.sh 48 300
#
# 결과는 logs/health_snapshots.log에 누적 기록한다.
# 각 체크는 || true로 FAIL이어도 스크립트가 중단되지 않는다.

set -uo pipefail

HOURS="${1:-48}"
WIN="${2:-600}"

mkdir -p logs

STAMP="$(date -u '+%F %T')"
SEP="====="

echo "${SEP} snapshot ${STAMP} (hours=${HOURS}, window=${WIN}s) =====" \
    | tee -a logs/health_snapshots.log

echo ""
echo "--- [continuity_check] ---"
poetry run python -m app.diagnostics.continuity_check \
    --hours "${HOURS}" \
    --pred-gap-sec 60 \
    --mkt-gap-sec 10 \
    2>&1 | tee -a logs/health_snapshots.log || true

echo ""
echo "--- [altdata_check] ---"
poetry run python -m app.diagnostics.altdata_check \
    --window "${WIN}" \
    2>&1 | tee -a logs/health_snapshots.log || true

echo ""
echo "--- [feature_check] ---"
poetry run python -m app.diagnostics.feature_check \
    --window "${WIN}" \
    2>&1 | tee -a logs/health_snapshots.log || true

echo ""
echo "--- [feature_leak_check] ---"
poetry run python -m app.diagnostics.feature_leak_check \
    --window "${WIN}" \
    2>&1 | tee -a logs/health_snapshots.log || true

echo ""
echo "--- [coinglass_check] ---"
poetry run python -m app.diagnostics.coinglass_check \
    --window "${WIN}" \
    2>&1 | tee -a logs/health_snapshots.log || true

echo ""
echo "${SEP} snapshot END ${STAMP} =====" | tee -a logs/health_snapshots.log
echo ""
