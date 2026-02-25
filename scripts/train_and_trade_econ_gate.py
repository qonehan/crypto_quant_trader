from __future__ import annotations
import argparse, json, math, os
import numpy as np
import pandas as pd
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_squared_error, mean_absolute_error


def pearson(a, b):
    a = a - a.mean()
    b = b - b.mean()
    den = np.sqrt((a * a).sum()) * np.sqrt((b * b).sum())
    return float((a * b).sum() / den) if den != 0 else float("nan")


def select_features(df: pd.DataFrame, target="label_return", drop_pnone=False):
    drop_exact = set(["symbol"])
    drop_sub = ["label_ts", "future", "target", "direction", "action_hat", "model_version", "cost_roundtrip_est"]
    feat = []
    for c in df.columns:
        if c == target:
            continue
        cl = c.lower()
        if c in drop_exact:
            continue
        if cl == "ts":
            continue
        if cl.startswith("label_"):
            continue
        if any(s in cl for s in drop_sub):
            continue
        if drop_pnone and cl == "p_none":
            continue
        if pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c]):
            feat.append(c)
    X = df[feat].copy()
    for c in feat:
        if X[c].isna().any():
            X[c] = X[c].fillna(float(X[c].median()))
    return feat, X.to_numpy(dtype=float)


def non_overlap_trades(
    ts: pd.Series,
    pred: np.ndarray,
    y_true: np.ndarray,
    cost: np.ndarray,
    horizon_sec: int,
    gamma: float,
) -> pd.DataFrame:
    """Enter if |pred| > gamma*cost. Non-overlap: skip horizon_sec after each entry."""
    trades = []
    next_ok_time = None
    ts_arr = pd.to_datetime(ts, utc=True, errors="coerce")
    for i in range(len(ts_arr)):
        t = ts_arr.iloc[i]
        if next_ok_time is not None and t < next_ok_time:
            continue
        c = float(cost[i])
        p = float(pred[i])
        side = 0
        if p > gamma * c:
            side = +1
        elif p < -gamma * c:
            side = -1
        else:
            continue
        gross = float(side * y_true[i])
        net = gross - c
        trades.append({
            "ts": str(t),
            "side": "LONG" if side == 1 else "SHORT",
            "pred": p,
            "y_true": float(y_true[i]),
            "cost": c,
            "pnl_gross": gross,
            "pnl_net": net,
        })
        next_ok_time = t + pd.Timedelta(seconds=int(horizon_sec))
    return pd.DataFrame(trades)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--horizon", type=int, required=True)
    ap.add_argument("--outdir", default="./artifacts/ml2")
    ap.add_argument("--drop-pnone", action="store_true")
    ap.add_argument("--models", default="ridge,hgbr")
    ap.add_argument("--alpha", type=float, default=50.0)
    ap.add_argument("--gamma-grid", default="1.0,1.5,2.0,2.5,3.0")
    args = ap.parse_args()

    df = pd.read_parquet(args.input).sort_values("ts").reset_index(drop=True)
    assert "label_return" in df.columns, "label_return column missing"
    assert "cost_roundtrip_est" in df.columns, (
        "cost_roundtrip_est missing — run export_dataset.py with updated script"
    )

    ts_col = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    y = df["label_return"].astype(float).to_numpy()
    cost = df["cost_roundtrip_est"].astype(float).to_numpy()

    feat, X = select_features(df, drop_pnone=args.drop_pnone)

    n = len(df)
    tr_end = max(int(n * 0.70), 10)
    va_end = max(int(n * 0.85), tr_end + 10)
    va_end = min(va_end, n - 10)

    Xtr, ytr = X[:tr_end], y[:tr_end]
    Xva, yva = X[tr_end:va_end], y[tr_end:va_end]
    Xte, yte = X[va_end:], y[va_end:]

    cva = cost[tr_end:va_end]
    cte = cost[va_end:]
    ts_va = ts_col.iloc[tr_end:va_end].reset_index(drop=True)
    ts_te = ts_col.iloc[va_end:].reset_index(drop=True)

    gammas = [float(x) for x in args.gamma_grid.split(",") if x.strip()]
    os.makedirs(args.outdir, exist_ok=True)

    model_names = [m.strip() for m in args.models.split(",") if m.strip()]
    all_results = {}

    for name in model_names:
        if name == "ridge":
            model = Pipeline([
                ("sc", StandardScaler()),
                ("rd", Ridge(alpha=args.alpha, random_state=42)),
            ])
        elif name == "hgbr":
            model = HistGradientBoostingRegressor(
                random_state=42, max_depth=3, learning_rate=0.05, max_iter=200,
            )
        else:
            raise ValueError(f"Unknown model: {name}")

        model.fit(Xtr, ytr)
        pva = model.predict(Xva)
        pte = model.predict(Xte)

        # test prediction metrics
        rmse = math.sqrt(mean_squared_error(yte, pte))
        mae = mean_absolute_error(yte, pte)
        ic = pearson(pte, yte)
        sign_acc = float((np.sign(pte) == np.sign(yte)).mean())
        rmse_naive0 = math.sqrt(mean_squared_error(yte, np.zeros_like(yte)))

        # gamma selection on VALID: maximize total_pnl_net
        best_gamma = gammas[0]
        best_va_pnl = -1e18
        gamma_results = []
        for g in gammas:
            tv = non_overlap_trades(ts_va, pva, yva, cva, args.horizon, g)
            tot = float(tv["pnl_net"].sum()) if len(tv) else 0.0
            n_t = len(tv)
            gamma_results.append({"gamma": g, "valid_n_trades": n_t, "valid_total_pnl_net": tot})
            if tot > best_va_pnl:
                best_va_pnl = tot
                best_gamma = g

        # evaluate on TEST with chosen gamma
        tt = non_overlap_trades(ts_te, pte, yte, cte, args.horizon, best_gamma)
        if len(tt) > 0:
            total_gross = float(tt["pnl_gross"].sum())
            total_net = float(tt["pnl_net"].sum())
            win_gross = float((tt["pnl_gross"] > 0).mean())
            win_net = float((tt["pnl_net"] > 0).mean())
            avg_net = float(tt["pnl_net"].mean())
            long_n = int((tt["side"] == "LONG").sum())
            short_n = int((tt["side"] == "SHORT").sum())
        else:
            total_gross = total_net = win_gross = win_net = avg_net = 0.0
            long_n = short_n = 0

        res = {
            "input": args.input,
            "horizon": args.horizon,
            "drop_pnone": bool(args.drop_pnone),
            "features": feat,
            "n_total": n,
            "split": {"train_end": tr_end, "valid_end": va_end, "test_n": int(n - va_end)},
            "metrics_test": {
                "rmse": round(rmse, 8),
                "rmse_naive0": round(rmse_naive0, 8),
                "mae": round(mae, 8),
                "ic_pearson": round(ic, 5) if np.isfinite(ic) else None,
                "sign_acc": round(sign_acc, 5),
            },
            "gamma_grid_valid": gamma_results,
            "gamma_selected": {
                "gamma": best_gamma,
                "valid_total_pnl_net": round(best_va_pnl, 8),
            },
            "test_trades": {
                "n": int(len(tt)),
                "long_n": long_n,
                "short_n": short_n,
                "win_rate_gross": round(win_gross, 4),
                "win_rate_net": round(win_net, 4),
                "total_pnl_gross": round(total_gross, 8),
                "total_pnl_net": round(total_net, 8),
                "avg_pnl_net_per_trade": round(avg_net, 8) if len(tt) else None,
            },
        }
        all_results[name] = res

        # save artifacts
        tag = os.path.splitext(os.path.basename(args.input))[0]
        sfx = "_nopnone" if args.drop_pnone else ""
        out_base = os.path.join(args.outdir, f"{tag}_{name}{sfx}")
        os.makedirs(out_base, exist_ok=True)
        with open(os.path.join(out_base, "metrics.json"), "w") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
        tt.to_csv(os.path.join(out_base, "test_trades.csv"), index=False)

        print(f"\n{'='*60}")
        print(f"Model: {name} | drop_pnone={args.drop_pnone} | horizon={args.horizon}s")
        print(f"  features   : {len(feat)}")
        print(f"  split      : train={tr_end}, valid={va_end - tr_end}, test={n - va_end}")
        print(f"  test RMSE  : {rmse:.8f}  (naive0={rmse_naive0:.8f})")
        print(f"  test IC    : {ic:.5f}  sign_acc={sign_acc:.4f}")
        print(f"  gamma sel. : {best_gamma} (valid_pnl={best_va_pnl:.6f})")
        print(f"  test trades: n={len(tt)}  long={long_n}  short={short_n}")
        print(f"  test PnL   : gross={total_gross:.6f}  net={total_net:.6f}")
        print(f"  win_rate   : gross={win_gross:.3f}  net={win_net:.3f}")
        print(f"  artifacts  : {out_base}")

    print(f"\n{'='*60}")
    print("SUMMARY:")
    for k, v in all_results.items():
        tt = v["test_trades"]
        print(f"  {k:8s} gamma={v['gamma_selected']['gamma']} "
              f"trades={tt['n']} total_net={tt['total_pnl_net']:.6f} win_net={tt['win_rate_net']:.3f}")

    # save combined summary
    summary_path = os.path.join(
        args.outdir,
        os.path.splitext(os.path.basename(args.input))[0]
        + ("_nopnone" if args.drop_pnone else "")
        + "_summary.json",
    )
    with open(summary_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nSaved summary -> {summary_path}")


if __name__ == "__main__":
    main()
