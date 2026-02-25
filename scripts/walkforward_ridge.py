from __future__ import annotations
import argparse, math
import numpy as np, pandas as pd
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error, mean_absolute_error


def pearson(a, b):
    a = a - a.mean()
    b = b - b.mean()
    den = np.sqrt((a * a).sum()) * np.sqrt((b * b).sum())
    return float((a * b).sum() / den) if den != 0 else float("nan")


def build_features(df: pd.DataFrame, target="label_return"):
    drop_exact = set(["symbol"])
    drop_sub = ["label_ts", "future", "target", "direction"]
    feat = []
    for c in df.columns:
        if c == target:
            continue
        if c in drop_exact:
            continue
        cl = c.lower()
        if cl == "ts":
            continue
        if cl.startswith("label_"):
            continue
        if any(s in cl for s in drop_sub):
            continue
        if pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c]):
            feat.append(c)
    X = df[feat].copy()
    for c in feat:
        if X[c].isna().any():
            X[c] = X[c].fillna(float(X[c].median()))
    return feat, X.to_numpy(dtype=float)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--target", default="label_return")
    ap.add_argument("--alpha", type=float, default=50.0)
    ap.add_argument("--min-train", type=int, default=150)
    ap.add_argument("--test-size", type=int, default=50)
    ap.add_argument("--step", type=int, default=50)
    args = ap.parse_args()

    df = pd.read_parquet(args.input).sort_values("ts").reset_index(drop=True)
    y = df[args.target].astype(float).to_numpy()
    feat, X = build_features(df, target=args.target)

    n = len(df)
    i = args.min_train
    rows = []
    fold = 0
    while i + args.test_size <= n:
        fold += 1
        tr_end = i
        te_end = i + args.test_size
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xte, yte = X[tr_end:te_end], y[tr_end:te_end]

        m = Pipeline([
            ("sc", StandardScaler()),
            ("rd", Ridge(alpha=args.alpha, random_state=42)),
        ])
        m.fit(Xtr, ytr)
        pred = m.predict(Xte)

        # baseline_0 for comparison
        pred0 = np.zeros_like(yte)

        rmse = math.sqrt(mean_squared_error(yte, pred))
        rmse0 = math.sqrt(mean_squared_error(yte, pred0))
        mae = mean_absolute_error(yte, pred)
        ic = pearson(pred, yte)
        sign = float((np.sign(pred) == np.sign(yte)).mean())

        rows.append({
            "fold": fold,
            "train_n": len(ytr),
            "test_n": len(yte),
            "rmse": round(rmse, 8),
            "rmse_naive0": round(rmse0, 8),
            "rmse_vs_naive_pct": round((rmse0 - rmse) / rmse0 * 100, 2),
            "mae": round(mae, 8),
            "ic": round(ic, 5),
            "sign_acc": round(sign, 5),
            "t_start": str(df["ts"].iloc[tr_end]),
            "t_end": str(df["ts"].iloc[te_end - 1]),
        })
        i += args.step

    out = pd.DataFrame(rows)
    print(f"features={len(feat)}  alpha={args.alpha}")
    if len(out) == 0:
        print("NO FOLDS — not enough data")
        return
    print(out.to_string(index=False))
    print("\nMEAN:")
    print(out[["rmse", "rmse_naive0", "rmse_vs_naive_pct", "mae", "ic", "sign_acc"]].mean(numeric_only=True).to_string())
    print("\nSTD:")
    print(out[["rmse", "rmse_naive0", "rmse_vs_naive_pct", "mae", "ic", "sign_acc"]].std(numeric_only=True).to_string())


if __name__ == "__main__":
    main()
