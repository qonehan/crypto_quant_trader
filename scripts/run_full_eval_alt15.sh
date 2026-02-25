#!/usr/bin/env bash
# run_full_eval_alt15.sh — ALT-15/16 표준 평가 원클릭 스크립트
#
# 사용법:
#   bash scripts/run_full_eval_alt15.sh                    # 기본: window=600 hours=48
#   bash scripts/run_full_eval_alt15.sh 600 24             # window=600s, hours=24
#   bash scripts/run_full_eval_alt15.sh 600 48 --skip-continuity   # 연속성 FAIL 무시(임시)
#
# 실행 순서:
#   (1) continuity_check  — HARD GATE (--skip-continuity 없으면 FAIL 시 중단)
#   (2) core checks       — HARD GATE (altdata/feature/leak)
#   (3) export            — h900/h1800 × maker/taker (segment ON)
#   (4) econ summary + baseline 자동 계산
#   (5) artifacts/alt15/ 에 결과 저장

set -euo pipefail

WIN="${1:-600}"
HOURS="${2:-48}"
SKIP_CONT="${3:-}"  # "--skip-continuity" 이면 continuity FAIL도 진행
OUTDIR="artifacts/alt15"
DATADIR="data/datasets"

mkdir -p "${OUTDIR}" "${DATADIR}" logs

STAMP="$(date -u '+%F_%H%M%S')"
LOGFILE="${OUTDIR}/run_${STAMP}.log"

echo "===== ALT-15/16 FULL EVAL $(date -u '+%F %T') =====" | tee "${LOGFILE}"
echo "  window=${WIN}s  hours=${HOURS}h  outdir=${OUTDIR}" | tee -a "${LOGFILE}"
if [[ "${SKIP_CONT}" == "--skip-continuity" ]]; then
    echo "  ⚠️  --skip-continuity: continuity FAIL 무시 모드 (임시 평가용)" | tee -a "${LOGFILE}"
fi
echo "" | tee -a "${LOGFILE}"

# ─── (1) Continuity HARD gate ───────────────────────────────────────────────
echo "[1/4] continuity_check..." | tee -a "${LOGFILE}"
if [[ "${SKIP_CONT}" == "--skip-continuity" ]]; then
    echo "  [SKIP_CONT] continuity_check를 경고로만 실행 (FAIL해도 계속 진행)" | tee -a "${LOGFILE}"
    poetry run python -m app.diagnostics.continuity_check \
        --hours "${HOURS}" \
        --pred-gap-sec 60 \
        --mkt-gap-sec 10 \
        2>&1 | tee -a "${LOGFILE}" || true
    echo "  [SKIP_CONT] 계속 진행..." | tee -a "${LOGFILE}"
else
    echo "  [HARD GATE] continuity FAIL이면 중단" | tee -a "${LOGFILE}"
    poetry run python -m app.diagnostics.continuity_check \
        --hours "${HOURS}" \
        --pred-gap-sec 60 \
        --mkt-gap-sec 10 \
        2>&1 | tee -a "${LOGFILE}"
fi
echo "" | tee -a "${LOGFILE}"

# ─── (2) Core pipeline checks HARD gate ─────────────────────────────────────
echo "[2/4] core pipeline checks (HARD GATE)..." | tee -a "${LOGFILE}"
poetry run python -m app.diagnostics.altdata_check   --window "${WIN}" 2>&1 | tee -a "${LOGFILE}"
poetry run python -m app.diagnostics.feature_check   --window "${WIN}" 2>&1 | tee -a "${LOGFILE}"
poetry run python -m app.diagnostics.feature_leak_check --window "${WIN}" 2>&1 | tee -a "${LOGFILE}"
echo "" | tee -a "${LOGFILE}"

# ─── (3) Export ─────────────────────────────────────────────────────────────
echo "[3/4] export (h900/h1800 × maker/taker, segment ON)..." | tee -a "${LOGFILE}"
for H in 900 1800; do
    for FEE_LABEL in maker taker; do
        if [[ "${FEE_LABEL}" == "maker" ]]; then FEE=4; else FEE=10; fi
        OUT="${DATADIR}/btc_alt15_h${H}_${FEE_LABEL}.parquet"

        echo "  export h${H} ${FEE_LABEL} (${FEE}bps) -> ${OUT}" | tee -a "${LOGFILE}"
        poetry run python scripts/export_dataset.py \
            --output "${OUT}" \
            --horizon "${H}" \
            --max-label-lag-mult 2.0 \
            --fee-bps-roundtrip "${FEE}" \
            --max-feature-gap-sec 60 \
            2>&1 | tee -a "${LOGFILE}"
        echo "" | tee -a "${LOGFILE}"
    done
done

# ─── (4) Econ summary + baseline ────────────────────────────────────────────
echo "[4/4] econ summary + baseline..." | tee -a "${LOGFILE}"

ECON_OUT="${OUTDIR}/econ_and_baseline.txt"

poetry run python3 - << 'PY' | tee "${ECON_OUT}" | tee -a "${LOGFILE}"
import pandas as pd
import numpy as np
from pathlib import Path

FILES = [
    "./data/datasets/btc_alt15_h900_maker.parquet",
    "./data/datasets/btc_alt15_h900_taker.parquet",
    "./data/datasets/btc_alt15_h1800_maker.parquet",
    "./data/datasets/btc_alt15_h1800_taker.parquet",
]


def q(arr, p):
    return float(np.quantile(arr, p)) if len(arr) > 0 else float("nan")


def summarize(path):
    if not Path(path).exists():
        return {"file": Path(path).name, "error": "file not found"}
    df = pd.read_parquet(path).sort_values("ts")
    r = df["label_return"].astype(float).to_numpy()
    c = df["cost_roundtrip_est"].astype(float).to_numpy()
    absr = np.abs(r)
    return {
        "file": Path(path).name,
        "n": len(df),
        "absret_q50": q(absr, 0.50),
        "absret_q80": q(absr, 0.80),
        "cost_q50": q(c, 0.50),
        "P(|ret|>cost)": float((absr > c).mean()) if len(df) else float("nan"),
        "P(|ret|>2cost)": float((absr > 2 * c).mean()) if len(df) else float("nan"),
        "P(ret>cost)": float((r > c).mean()) if len(df) else float("nan"),
        "P(ret<-cost)": float((r < -c).mean()) if len(df) else float("nan"),
        "mean_ret": float(r.mean()) if len(df) else float("nan"),
        "std_ret": float(r.std()) if len(df) else float("nan"),
        "max_lag_sec": float(df["label_lag_sec"].max()) if "label_lag_sec" in df.columns else float("nan"),
    }


def non_overlap(ts, horizon_sec):
    idx = []
    next_ok = None
    for i, t in enumerate(ts):
        if next_ok is not None and t < next_ok:
            continue
        idx.append(i)
        next_ok = t + pd.Timedelta(seconds=int(horizon_sec))
    return idx


print("=" * 70)
print("ECON SUMMARY")
print("=" * 70)
rows = [summarize(f) for f in FILES]
valid_rows = [r for r in rows if "error" not in r]
if valid_rows:
    pd.set_option("display.max_columns", 999)
    pd.set_option("display.width", 200)
    print(pd.DataFrame(valid_rows).to_string(index=False))
for r in rows:
    if "error" in r:
        print(f"  SKIP {r['file']}: {r['error']}")

print()
print("=" * 70)
print("BASELINE (test 15%, non-overlap)")
print("=" * 70)

for f in FILES:
    if not Path(f).exists():
        print(f"  SKIP {Path(f).name}: not found")
        continue
    df = pd.read_parquet(f).sort_values("ts").reset_index(drop=True)
    if len(df) == 0:
        print(f"  SKIP {Path(f).name}: empty")
        continue

    H = int(df["label_lag_sec"].iloc[0]) if "label_lag_sec" in df.columns else (900 if "h900" in f else 1800)
    te_start = int(len(df) * 0.85)
    ts = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    ts_te = ts.iloc[te_start:].reset_index(drop=True)
    y_te = df["label_return"].astype(float).to_numpy()[te_start:]
    c_te = df["cost_roundtrip_est"].astype(float).to_numpy()[te_start:]

    idx = non_overlap(ts_te, H)
    n_te = len(y_te)
    n_no = len(idx)
    flat = 0.0
    short_net = float(((-y_te[idx]) - c_te[idx]).sum()) if n_no else 0.0
    long_net = float(((+y_te[idx]) - c_te[idx]).sum()) if n_no else 0.0
    avg_ret = float(y_te[idx].mean()) if n_no else float("nan")
    std_ret = float(y_te[idx].std()) if n_no > 1 else 0.0

    print(f"\n  {Path(f).name}  H={H}s")
    print(f"    test_rows={n_te}  nonoverlap_n={n_no}")
    print(f"    avg_label_ret={avg_ret:.6f}  std={std_ret:.6f}")
    print(f"    FLAT         net={flat:.6f}")
    print(f"    ALWAYS_SHORT net={short_net:.6f}")
    print(f"    ALWAYS_LONG  net={long_net:.6f}")
    if n_no < 10:
        print(f"    ⚠️  nonoverlap_n={n_no} < 10 — 통계 의미 부족")
PY

echo "" | tee -a "${LOGFILE}"
echo "===== ALT-15 FULL EVAL COMPLETE $(date -u '+%F %T') =====" | tee -a "${LOGFILE}"
echo "  artifacts -> ${OUTDIR}/" | tee -a "${LOGFILE}"
echo "  econ      -> ${ECON_OUT}" | tee -a "${LOGFILE}"
