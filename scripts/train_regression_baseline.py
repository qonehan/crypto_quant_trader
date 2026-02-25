# scripts/train_regression_baseline.py
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error
import joblib


TIME_COL_CANDIDATES = ["ts", "t0", "time", "timestamp", "datetime"]
DROP_EXACT = {
    "symbol",
    "exchange",
}
DROP_SUBSTR = [
    "label_ts",
    "future",
    "t1",
    "target",
    "direction",  # 분류 라벨 후보 제거
]
# label_return 자체는 타겟이므로 별도 처리


@dataclass
class SplitIdx:
    train_end: int
    valid_end: int
    test_end: int


def _pick_time_col(df: pd.DataFrame) -> Optional[str]:
    for c in TIME_COL_CANDIDATES:
        if c in df.columns:
            return c
    # fallback: datetime 타입 컬럼 있으면 첫 번째
    for c in df.columns:
        if np.issubdtype(df[c].dtype, np.datetime64):
            return c
    return None


def _safe_sort_by_time(df: pd.DataFrame, time_col: Optional[str]) -> pd.DataFrame:
    if time_col is None:
        # 시간이 없으면 인덱스 순으로 (최소한 shuffle은 하지 않음)
        return df.reset_index(drop=True)
    out = df.copy()
    out[time_col] = pd.to_datetime(out[time_col], utc=True, errors="coerce")
    out = out.sort_values(time_col).reset_index(drop=True)
    return out


def _build_feature_cols(df: pd.DataFrame, target_col: str) -> List[str]:
    cols = []
    for c in df.columns:
        if c == target_col:
            continue
        if c in DROP_EXACT:
            continue
        c_low = c.lower()
        if any(s in c_low for s in DROP_SUBSTR):
            continue
        # label_ 접두는 원칙적으로 제거 (label_return 제외)
        if c_low.startswith("label_"):
            continue
        # 비숫자 제거 (tz-aware datetime dtype은 np.issubdtype에서 TypeError 발생하므로 try/except)
        try:
            is_numeric = np.issubdtype(df[c].dtype, np.number)
        except TypeError:
            is_numeric = False
        is_bool = df[c].dtype == bool or str(df[c].dtype) == "bool"
        if not (is_numeric or is_bool):
            continue
        cols.append(c)
    return cols


def _time_split(n: int, train_frac=0.70, valid_frac=0.15) -> SplitIdx:
    train_end = int(n * train_frac)
    valid_end = int(n * (train_frac + valid_frac))
    test_end = n
    # 최소 안전장치
    train_end = max(train_end, 10)
    valid_end = max(valid_end, train_end + 10)
    valid_end = min(valid_end, n - 10)
    return SplitIdx(train_end=train_end, valid_end=valid_end, test_end=test_end)


def _pearson_corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3:
        return float("nan")
    aa = a - a.mean()
    bb = b - b.mean()
    denom = (np.sqrt((aa**2).sum()) * np.sqrt((bb**2).sum()))
    if denom == 0:
        return float("nan")
    return float((aa * bb).sum() / denom)


def _infer_step_seconds(time_series: pd.Series) -> Optional[float]:
    if time_series is None or len(time_series) < 3:
        return None
    t = pd.to_datetime(time_series, utc=True, errors="coerce")
    dt = t.diff().dropna().dt.total_seconds()
    if len(dt) == 0:
        return None
    return float(dt.median())


def simulate_non_overlap_trades(
    df: pd.DataFrame,
    y_true: np.ndarray,
    y_pred: np.ndarray,
    horizon_sec: int,
    time_col: Optional[str],
    p_none_max: float = 0.7,
) -> pd.DataFrame:
    """
    간단/현실적인 버전:
    - 신호가 나오면 "해당 row의 label_return"을 1회 트레이드 성과로 간주하고,
      다음 horizon 동안은 새 트레이드 진입을 막는다(non-overlap).
    - 비용은 cost_roundtrip_est가 있으면 trade 1회당 차감.
    """
    n = len(y_true)
    if n == 0:
        return pd.DataFrame()

    # threshold: rowwise r_t가 있으면 사용, 없으면 mean(|y|) 기반으로 fallback
    if "r_t" in df.columns:
        rt = df["r_t"].astype(float).to_numpy()
        thr = rt
    else:
        thr0 = float(np.mean(np.abs(y_true))) if np.isfinite(y_true).all() else 0.0
        thr = np.full(n, max(thr0, 1e-6), dtype=float)

    # p_none gate (있으면 적용)
    if "p_none" in df.columns:
        p_none = df["p_none"].astype(float).to_numpy()
    else:
        p_none = np.zeros(n, dtype=float)

    # cost
    if "cost_roundtrip_est" in df.columns:
        cost = df["cost_roundtrip_est"].astype(float).to_numpy()
        cost = np.nan_to_num(cost, nan=0.0, posinf=0.0, neginf=0.0)
    else:
        cost = np.zeros(n, dtype=float)

    # skip steps 계산
    step_sec = _infer_step_seconds(df[time_col]) if time_col and time_col in df.columns else None
    if step_sec is None or step_sec <= 0:
        # fallback: 예측 주기 5초 가정
        step_sec = 5.0
    skip_n = max(1, int(round(horizon_sec / step_sec)))

    rows = []
    i = 0
    while i < n:
        gate_ok = (p_none[i] < p_none_max)
        pos = 0
        if gate_ok:
            if y_pred[i] > +thr[i]:
                pos = +1
            elif y_pred[i] < -thr[i]:
                pos = -1

        if pos == 0:
            i += 1
            continue

        pnl_gross = pos * y_true[i]
        pnl_net = pnl_gross - cost[i]

        ts_val = df[time_col].iloc[i] if (time_col and time_col in df.columns) else i
        rows.append({
            "idx": i,
            "ts": str(ts_val),
            "pos": int(pos),
            "y_true": float(y_true[i]),
            "y_pred": float(y_pred[i]),
            "thr": float(thr[i]),
            "p_none": float(p_none[i]),
            "cost_roundtrip_est": float(cost[i]),
            "pnl_gross": float(pnl_gross),
            "pnl_net": float(pnl_net),
        })
        i += skip_n

    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="parquet path")
    ap.add_argument("--target", default="label_return", help="target column name")
    ap.add_argument("--horizon-sec", type=int, default=120, help="used for non-overlap sim")
    ap.add_argument("--out-dir", default="./artifacts/ml1", help="output directory")
    ap.add_argument("--p-none-max", type=float, default=0.7, help="trade gate")
    args = ap.parse_args()

    inp = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(inp)
    if args.target not in df.columns:
        raise SystemExit(f"Target col not found: {args.target}. Available={list(df.columns)[:30]}...")

    time_col = _pick_time_col(df)
    df = _safe_sort_by_time(df, time_col)

    # y
    y = df[args.target].astype(float).to_numpy()
    y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)

    # X
    feat_cols = _build_feature_cols(df, target_col=args.target)
    if len(feat_cols) < 5:
        raise SystemExit(f"Too few features detected: {len(feat_cols)} -> {feat_cols}")

    X = df[feat_cols].copy()
    # 결측 처리(중앙값)
    for c in feat_cols:
        if X[c].isna().any():
            med = float(X[c].median()) if np.isfinite(X[c].median()) else 0.0
            X[c] = X[c].fillna(med)
    X = X.to_numpy(dtype=float)

    n = len(df)
    sp = _time_split(n)
    X_tr, y_tr = X[:sp.train_end], y[:sp.train_end]
    X_va, y_va = X[sp.train_end:sp.valid_end], y[sp.train_end:sp.valid_end]
    X_te, y_te = X[sp.valid_end:], y[sp.valid_end:]

    # 모델 후보: Ridge alpha grid
    alphas = [0.1, 1.0, 10.0, 50.0]
    best = None
    best_alpha = None
    best_va_rmse = float("inf")

    for a in alphas:
        model = Pipeline([
            ("scaler", StandardScaler(with_mean=True, with_std=True)),
            ("ridge", Ridge(alpha=a, random_state=42)),
        ])
        model.fit(X_tr, y_tr)
        va_pred = model.predict(X_va)
        rmse = math.sqrt(mean_squared_error(y_va, va_pred))
        if rmse < best_va_rmse:
            best_va_rmse = rmse
            best = model
            best_alpha = a

    assert best is not None
    te_pred = best.predict(X_te)

    # metrics (test)
    rmse = math.sqrt(mean_squared_error(y_te, te_pred))
    mae = mean_absolute_error(y_te, te_pred)
    ic = _pearson_corr(te_pred.astype(float), y_te.astype(float))
    sign_acc = float((np.sign(te_pred) == np.sign(y_te)).mean())

    # trade sim (test region only)
    df_te = df.iloc[sp.valid_end:].reset_index(drop=True)
    trades = simulate_non_overlap_trades(
        df_te, y_true=y_te, y_pred=te_pred,
        horizon_sec=args.horizon_sec,
        time_col=time_col,
        p_none_max=args.p_none_max,
    )
    if len(trades) > 0:
        total_pnl = float(trades["pnl_net"].sum())
        win_rate = float((trades["pnl_net"] > 0).mean())
        avg_pnl = float(trades["pnl_net"].mean())
    else:
        total_pnl = 0.0
        win_rate = float("nan")
        avg_pnl = float("nan")

    result = {
        "input": str(inp),
        "n_rows": int(n),
        "time_col": time_col,
        "target": args.target,
        "horizon_sec": int(args.horizon_sec),
        "split": {
            "train_end": sp.train_end,
            "valid_end": sp.valid_end,
            "test_n": int(n - sp.valid_end),
        },
        "feature_count": int(len(feat_cols)),
        "best_model": {
            "type": "Ridge+StandardScaler",
            "alpha": float(best_alpha),
            "valid_rmse": float(best_va_rmse),
        },
        "test_metrics": {
            "rmse": float(rmse),
            "mae": float(mae),
            "ic_pearson": float(ic) if np.isfinite(ic) else None,
            "sign_accuracy": float(sign_acc),
        },
        "trade_sim_test": {
            "p_none_max": float(args.p_none_max),
            "trades": int(len(trades)),
            "win_rate": None if not np.isfinite(win_rate) else float(win_rate),
            "avg_pnl_net_per_trade": None if not np.isfinite(avg_pnl) else float(avg_pnl),
            "total_pnl_net": float(total_pnl),
        },
        "notes": [
            "non-overlap trade sim uses label_return as per-trade return at entry row",
            "cost_roundtrip_est is deducted if present; else cost=0",
        ],
    }

    # save artifacts
    model_path = out_dir / "ridge_model.joblib"
    meta_path = out_dir / "model_meta.json"
    trades_path = out_dir / "test_trades.csv"
    feats_path = out_dir / "feature_cols.json"
    metrics_path = out_dir / "metrics.json"

    joblib.dump(best, model_path)
    feats_path.write_text(json.dumps(feat_cols, ensure_ascii=False, indent=2))
    meta_path.write_text(json.dumps({"feature_cols": feat_cols, "time_col": time_col}, ensure_ascii=False, indent=2))
    metrics_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    trades.to_csv(trades_path, index=False)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nSaved model -> {model_path}")
    print(f"Saved metrics -> {metrics_path}")
    print(f"Saved trades -> {trades_path}")
    print(f"Saved feature cols -> {feats_path}")


if __name__ == "__main__":
    main()
