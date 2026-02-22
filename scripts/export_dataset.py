"""
export_dataset.py — 학습용 데이터셋 Export (라벨 누수/왜곡 방지)

사용법:
  poetry run python scripts/export_dataset.py --output dataset.parquet
  poetry run python scripts/export_dataset.py --output dataset.csv --horizon 120

라벨 매칭 규칙:
  - merge_asof(direction="forward") 사용 (nearest 금지)
  - label_ts 컬럼 저장 → 매칭된 미래 시각 기록
  - label_ts >= t0 + horizon_sec 보장 (위반 row는 drop + 집계 출력)
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd
from sqlalchemy import create_engine, text

from app.config import load_settings


def load_features(engine, symbol: str) -> pd.DataFrame:
    """Load feature data from predictions table."""
    query = text("""
        SELECT t0 as ts, symbol, p_up, p_down, p_none, ev, ev_rate,
               r_t, z_barrier, spread_bps, mom_z, imb_notional_top5,
               action_hat, model_version
        FROM predictions
        WHERE symbol = :sym
        ORDER BY t0
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"sym": symbol})
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def load_prices(engine, symbol: str) -> pd.DataFrame:
    """Load price data from market_1s for label generation."""
    query = text("""
        SELECT ts, mid_close_1s as mid
        FROM market_1s
        WHERE symbol = :sym AND mid_close_1s IS NOT NULL
        ORDER BY ts
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"sym": symbol})
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def generate_labels(
    features: pd.DataFrame, prices: pd.DataFrame, horizon_sec: int
) -> pd.DataFrame:
    """Generate labels using merge_asof(direction='forward') with horizon enforcement.

    For each feature row at time t0, finds the first price at or after t0 + horizon_sec.
    This ensures no future information leaks before the defined horizon.
    """
    if features.empty or prices.empty:
        print("  WARNING: features or prices DataFrame is empty, returning empty result")
        return pd.DataFrame()

    # Create target timestamps: t0 + horizon
    horizon_td = pd.Timedelta(seconds=horizon_sec)
    features = features.copy()
    features["t0_plus_h"] = features["ts"] + horizon_td

    # Use merge_asof with direction='forward' to find the first price >= t0 + horizon
    # This ensures we never use a price earlier than the required horizon
    features_sorted = features.sort_values("t0_plus_h").reset_index(drop=True)
    prices_sorted = prices.sort_values("ts").reset_index(drop=True)

    merged = pd.merge_asof(
        features_sorted,
        prices_sorted.rename(columns={"ts": "label_ts", "mid": "future_mid"}),
        left_on="t0_plus_h",
        right_on="label_ts",
        direction="forward",
    )

    # Also get the entry price at t0
    merged = pd.merge_asof(
        merged.sort_values("ts"),
        prices_sorted.rename(columns={"ts": "price_ts", "mid": "entry_mid"}),
        left_on="ts",
        right_on="price_ts",
        direction="backward",
    )

    # Compute label: future return
    merged["label_return"] = (merged["future_mid"] - merged["entry_mid"]) / merged["entry_mid"]

    # Enforce: label_ts >= ts + horizon_sec
    n_before = len(merged)
    violation_mask = merged["label_ts"].notna() & (merged["label_ts"] < merged["ts"] + horizon_td)
    n_violations = violation_mask.sum()
    if n_violations > 0:
        print(f"  ⚠️  Dropped {n_violations} rows: label_ts < t0 + horizon_sec")
        merged = merged[~violation_mask]

    # Drop rows where label couldn't be matched
    no_label = merged["label_ts"].isna()
    n_no_label = no_label.sum()
    if n_no_label > 0:
        print(f"  ⚠️  Dropped {n_no_label} rows: no future price found for label")
        merged = merged[~no_label]

    n_after = len(merged)
    print(f"  Label generation: {n_before} → {n_after} rows (dropped {n_before - n_after})")

    # Clean up helper columns
    cols_to_drop = ["t0_plus_h", "price_ts"]
    merged = merged.drop(columns=[c for c in cols_to_drop if c in merged.columns])

    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="학습용 데이터셋 Export (라벨 누수 방지)")
    parser.add_argument("--output", type=str, default="dataset.parquet", help="출력 파일 경로")
    parser.add_argument("--horizon", type=int, default=None, help="라벨 horizon (초, 기본: H_SEC)")
    parser.add_argument("--symbol", type=str, default=None, help="종목 (기본: SYMBOL)")
    args = parser.parse_args()

    s = load_settings()
    symbol = args.symbol or s.SYMBOL
    horizon_sec = args.horizon or s.H_SEC
    output_path = args.output

    sep = "=" * 60
    print(sep)
    print("  Dataset Export (라벨 누수 방지)")
    print(f"  symbol      = {symbol}")
    print(f"  horizon_sec = {horizon_sec}")
    print(f"  output      = {output_path}")
    print(sep)

    engine = create_engine(s.DB_URL)

    print("\n[1] Loading features...")
    features = load_features(engine, symbol)
    print(f"  features: {len(features)} rows")

    print("\n[2] Loading prices...")
    prices = load_prices(engine, symbol)
    print(f"  prices: {len(prices)} rows")

    print("\n[3] Generating labels (merge_asof direction=forward)...")
    dataset = generate_labels(features, prices, horizon_sec)

    if dataset.empty:
        print("\n  ❌ No data to export")
        return 1

    # Final validation
    horizon_td = pd.Timedelta(seconds=horizon_sec)
    violations = dataset[dataset["label_ts"] < dataset["ts"] + horizon_td]
    if len(violations) > 0:
        print(f"\n  ❌ FATAL: {len(violations)} rows violate label_ts >= t0 + horizon_sec")
        return 1

    print(f"\n  ✅ All {len(dataset)} rows pass label_ts >= t0 + horizon_sec check")

    print(f"\n[4] Exporting to {output_path}...")
    if output_path.endswith(".parquet"):
        dataset.to_parquet(output_path, index=False)
    elif output_path.endswith(".csv"):
        dataset.to_csv(output_path, index=False)
    else:
        dataset.to_parquet(output_path, index=False)

    print(f"  ✅ Exported {len(dataset)} rows to {output_path}")

    print(f"\n{sep}")
    print("  EXPORT COMPLETE ✅")
    print(sep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
