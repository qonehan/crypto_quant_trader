#!/usr/bin/env bash
# run_pipeline_checks.sh — 운영 점검 원클릭 스크립트
#
# 사용법:
#   bash scripts/run_pipeline_checks.sh
#   bash scripts/run_pipeline_checks.sh --window 600
#
# 4개 점검을 순서대로 실행하고, 하나라도 FAIL이면 exit 1.

set -euo pipefail

WINDOW="${1:---window}"
WINDOW_VAL="${2:-600}"

# If first arg is --window, use the value; otherwise use defaults
if [[ "$WINDOW" == "--window" ]]; then
    WINDOW_ARG="--window $WINDOW_VAL"
else
    WINDOW_ARG="--window 600"
fi

SEP="============================================================"
OVERALL=0

echo "$SEP"
echo "  Pipeline Checks — 운영 점검 원클릭"
echo "  $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "$SEP"
echo ""

# 1) altdata_check
echo ">>> [1/4] altdata_check"
if poetry run python -m app.diagnostics.altdata_check $WINDOW_ARG; then
    echo "  → altdata_check: PASS ✅"
else
    echo "  → altdata_check: FAIL ❌"
    OVERALL=1
fi
echo ""

# 2) feature_check
echo ">>> [2/4] feature_check"
if poetry run python -m app.diagnostics.feature_check $WINDOW_ARG; then
    echo "  → feature_check: PASS ✅"
else
    echo "  → feature_check: FAIL ❌"
    OVERALL=1
fi
echo ""

# 3) feature_leak_check
echo ">>> [3/4] feature_leak_check"
if poetry run python -m app.diagnostics.feature_leak_check $WINDOW_ARG; then
    echo "  → feature_leak_check: PASS ✅"
else
    echo "  → feature_leak_check: FAIL ❌"
    OVERALL=1
fi
echo ""

# 4) coinglass_check
echo ">>> [4/4] coinglass_check"
if poetry run python -m app.diagnostics.coinglass_check $WINDOW_ARG; then
    echo "  → coinglass_check: PASS ✅"
else
    echo "  → coinglass_check: FAIL ❌"
    OVERALL=1
fi
echo ""

echo "$SEP"
if [[ $OVERALL -eq 0 ]]; then
    echo "  PIPELINE OVERALL: PASS ✅"
else
    echo "  PIPELINE OVERALL: FAIL ❌"
fi
echo "$SEP"

exit $OVERALL
