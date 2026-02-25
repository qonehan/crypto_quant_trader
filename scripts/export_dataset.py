"""
export_dataset.py — 학습용 데이터셋 Export (라벨 누수/왜곡 방지)

사용법:
  poetry run python scripts/export_dataset.py --output dataset.parquet
  poetry run python scripts/export_dataset.py --output dataset.csv --horizon 120
  poetry run python scripts/export_dataset.py --output ds.parquet --max-feature-gap-sec 60

라벨 매칭 규칙:
  - merge_asof(direction="forward") 사용 (nearest 금지)
  - label_ts 컬럼 저장 → 매칭된 미래 시각 기록
  - label_lag_sec = (label_ts - ts).total_seconds() 계산 + 저장
  - label_lag_sec < horizon_sec → dropped_early (자동 drop)
  - label_lag_sec > max_label_lag_sec → dropped_late (자동 drop, 재기동/갭 오염 방지)
  - 위반 row가 남으면 exit 1 (하드 FAIL)

연속 구간 선택 (--max-feature-gap-sec):
  - feature ts에서 gap > max_feature_gap_sec인 경우 segment 경계로 인식
  - 가장 큰(rows 기준) segment만 남기고 나머지는 dropped_by_gap으로 제거
  - 0이면 비활성(전체 사용)
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd
from sqlalchemy import create_engine, text

from app.config import load_settings


def select_largest_segment(
    features: pd.DataFrame, max_gap_sec: float
) -> tuple[pd.DataFrame, dict]:
    """feature ts 기준으로 연속 구간을 분리하고 가장 큰 구간만 반환.

    Args:
        features: ts 컬럼이 있는 DataFrame (정렬 필요 없음, 내부에서 정렬)
        max_gap_sec: 이 초를 초과하는 간격이 있으면 새 segment 시작

    Returns:
        (selected_df, stats) where stats contains segment info.
    """
    if features.empty:
        return features, {"segments": 0, "selected_segment_id": None, "dropped_by_gap": 0}

    df = features.sort_values("ts").reset_index(drop=True)
    diffs = df["ts"].diff().dt.total_seconds().fillna(0)

    # segment_id: gap 초과 시마다 증가
    seg_ids = (diffs > max_gap_sec).cumsum()
    df = df.copy()
    df["_seg_id"] = seg_ids

    seg_counts = df["_seg_id"].value_counts().sort_values(ascending=False)
    n_segments = len(seg_counts)
    best_seg = int(seg_counts.index[0])

    selected = df[df["_seg_id"] == best_seg].drop(columns=["_seg_id"]).reset_index(drop=True)
    dropped_by_gap = len(df) - len(selected)

    seg_start = selected["ts"].min()
    seg_end = selected["ts"].max()
    dur_h = (seg_end - seg_start).total_seconds() / 3600

    stats = {
        "segments": n_segments,
        "selected_segment_id": best_seg,
        "selected_rows": len(selected),
        "dropped_by_gap": dropped_by_gap,
        "seg_start": str(seg_start),
        "seg_end": str(seg_end),
        "duration_h": round(dur_h, 3),
    }

    # 상위 segment 정보도 추가
    seg_info = []
    for seg_id, cnt in seg_counts.items():
        mask = df["_seg_id"] == seg_id
        s_start = df.loc[mask, "ts"].min()
        s_end = df.loc[mask, "ts"].max()
        s_dur = (s_end - s_start).total_seconds() / 3600
        seg_info.append({
            "seg_id": int(seg_id),
            "rows": int(cnt),
            "dur_h": round(s_dur, 3),
            "start": str(s_start)[:19],
            "end": str(s_end)[:19],
        })
    stats["all_segments"] = seg_info

    return selected, stats


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


def load_prices(engine, symbol: str, t_min=None, t_max=None) -> pd.DataFrame:
    """Load price data from market_1s for label generation.

    t_min / t_max: optional bounds to limit price query range.
    Passing t_max prevents far-future prices from being loaded even if they
    exist in the DB (e.g. after a bot restart with a big gap).
    """
    if t_min is not None and t_max is not None:
        query = text("""
            SELECT ts, mid_close_1s as mid
            FROM market_1s
            WHERE symbol = :sym
              AND mid_close_1s IS NOT NULL
              AND ts >= :t_min
              AND ts <= :t_max
            ORDER BY ts
        """)
        params = {"sym": symbol, "t_min": t_min, "t_max": t_max}
    else:
        query = text("""
            SELECT ts, mid_close_1s as mid
            FROM market_1s
            WHERE symbol = :sym AND mid_close_1s IS NOT NULL
            ORDER BY ts
        """)
        params = {"sym": symbol}

    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params=params)
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def generate_labels(
    features: pd.DataFrame,
    prices: pd.DataFrame,
    horizon_sec: int,
    max_label_lag_sec: int,
) -> tuple[pd.DataFrame, dict]:
    """Generate labels using merge_asof(direction='forward') with horizon enforcement.

    For each feature row at time t0, finds the first price at or after t0 + horizon_sec.
    Drops rows where label_lag_sec is outside [horizon_sec, max_label_lag_sec].
    Returns (merged_df, drop_stats).
    """
    drop_stats = {"dropped_early": 0, "dropped_late": 0, "dropped_no_label": 0}

    if features.empty or prices.empty:
        print("  WARNING: features or prices DataFrame is empty, returning empty result")
        return pd.DataFrame(), drop_stats

    # Create target timestamps: t0 + horizon
    horizon_td = pd.Timedelta(seconds=horizon_sec)
    features = features.copy()
    features["t0_plus_h"] = features["ts"] + horizon_td

    # Use merge_asof with direction='forward' to find the first price >= t0 + horizon
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

    n_before = len(merged)

    # Drop rows where label couldn't be matched
    no_label = merged["label_ts"].isna()
    drop_stats["dropped_no_label"] = int(no_label.sum())
    if drop_stats["dropped_no_label"] > 0:
        print(f"  ⚠️  Dropped {drop_stats['dropped_no_label']} rows: no future price found")
        merged = merged[~no_label].copy()

    # Compute label_lag_sec
    merged["label_lag_sec"] = (
        merged["label_ts"] - merged["ts"]
    ).dt.total_seconds()

    # Drop: label_lag_sec < horizon_sec (too early — horizon violation)
    early_mask = merged["label_lag_sec"] < horizon_sec
    drop_stats["dropped_early"] = int(early_mask.sum())
    if drop_stats["dropped_early"] > 0:
        print(f"  ⚠️  Dropped {drop_stats['dropped_early']} rows: label_lag_sec < horizon_sec (early match)")
        merged = merged[~early_mask].copy()

    # Drop: label_lag_sec > max_label_lag_sec (gap/restart contamination)
    late_mask = merged["label_lag_sec"] > max_label_lag_sec
    drop_stats["dropped_late"] = int(late_mask.sum())
    if drop_stats["dropped_late"] > 0:
        print(f"  ⚠️  Dropped {drop_stats['dropped_late']} rows: label_lag_sec > max_label_lag_sec (gap contamination)")
        merged = merged[~late_mask].copy()

    n_after = len(merged)
    print(f"  Label generation: {n_before} → {n_after} rows "
          f"(dropped_early={drop_stats['dropped_early']}, "
          f"dropped_late={drop_stats['dropped_late']}, "
          f"dropped_no_label={drop_stats['dropped_no_label']})")

    # Compute label: future return
    merged["label_return"] = (merged["future_mid"] - merged["entry_mid"]) / merged["entry_mid"]

    # Clean up helper columns
    cols_to_drop = ["t0_plus_h", "price_ts"]
    merged = merged.drop(columns=[c for c in cols_to_drop if c in merged.columns])

    return merged, drop_stats


def main() -> int:
    parser = argparse.ArgumentParser(description="학습용 데이터셋 Export (라벨 누수 방지)")
    parser.add_argument("--output", type=str, default="dataset.parquet", help="출력 파일 경로")
    parser.add_argument("--horizon", type=int, default=None, help="라벨 horizon (초, 기본: H_SEC)")
    parser.add_argument("--symbol", type=str, default=None, help="종목 (기본: SYMBOL)")
    parser.add_argument(
        "--max-label-lag-mult",
        type=float,
        default=2.0,
        help="max label lag = horizon × mult (기본 2.0). 이 배수를 초과하는 row는 자동 drop.",
    )
    parser.add_argument(
        "--fee-bps-roundtrip",
        type=int,
        default=10,
        help="왕복 수수료 bps (기본 10 = taker). 4 = maker 근사.",
    )
    parser.add_argument(
        "--max-feature-gap-sec",
        type=float,
        default=0.0,
        help="feature ts 간격이 이 초를 초과하면 segment 경계로 처리. "
             "가장 큰 segment만 export. 0이면 비활성(전체 사용).",
    )
    args = parser.parse_args()

    s = load_settings()
    symbol = args.symbol or s.SYMBOL
    horizon_sec = args.horizon or s.H_SEC
    output_path = args.output
    max_label_lag_sec = int(horizon_sec * args.max_label_lag_mult)
    fee_bps_roundtrip = args.fee_bps_roundtrip
    max_feature_gap_sec = args.max_feature_gap_sec

    sep = "=" * 60
    print(sep)
    print("  Dataset Export (라벨 누수 방지 + lag 가드 + 연속구간 선택)")
    print(f"  symbol              = {symbol}")
    print(f"  horizon_sec         = {horizon_sec}")
    print(f"  max_label_lag_mult  = {args.max_label_lag_mult}")
    print(f"  max_label_lag_sec   = {max_label_lag_sec}")
    print(f"  fee_bps_roundtrip   = {fee_bps_roundtrip}")
    print(f"  max_feature_gap_sec = {max_feature_gap_sec if max_feature_gap_sec > 0 else 'disabled'}")
    print(f"  output              = {output_path}")
    print(sep)

    engine = create_engine(s.DB_URL)

    print("\n[1] Loading features...")
    features = load_features(engine, symbol)
    print(f"  features: {len(features)} rows (raw)")

    if features.empty:
        print("\n  ❌ No features found in DB")
        return 1

    # === 연속 구간 선택 (옵션) ===
    seg_stats: dict | None = None
    if max_feature_gap_sec > 0:
        print(f"\n[1b] Selecting largest continuous segment (gap_threshold={max_feature_gap_sec}s)...")
        features, seg_stats = select_largest_segment(features, max_feature_gap_sec)
        n_segs = seg_stats["segments"]
        print(f"  total segments    = {n_segs}")
        for si in seg_stats.get("all_segments", [])[:5]:
            marker = " ← selected" if si["seg_id"] == seg_stats["selected_segment_id"] else ""
            print(f"  seg#{si['seg_id']:2d}: rows={si['rows']:4d}  dur={si['dur_h']:.2f}h  "
                  f"[{si['start']} ~ {si['end']}]{marker}")
        if n_segs > 5:
            print(f"  ... ({n_segs}개 구간 중 상위 5개 표시)")
        print(f"  selected: {seg_stats['selected_rows']} rows  "
              f"[{seg_stats['seg_start'][:19]} ~ {seg_stats['seg_end'][:19]}]  "
              f"({seg_stats['duration_h']:.2f}h)")
        print(f"  dropped_by_gap    = {seg_stats['dropped_by_gap']}")

        if features.empty:
            print("\n  ❌ No features after segment selection")
            return 1

    # Determine price query bounds from feature timestamps
    t_min = features["ts"].min()
    t_max = features["ts"].max()
    price_t_max = t_max + pd.Timedelta(seconds=max_label_lag_sec)
    print(f"\n  feature ts range (selected): {t_min} ~ {t_max}")
    print(f"  price query upper bound: {price_t_max} (t_max + max_label_lag_sec)")

    print("\n[2] Loading prices (bounded)...")
    prices = load_prices(engine, symbol, t_min=t_min, t_max=price_t_max)
    print(f"  prices: {len(prices)} rows")

    print("\n[3] Generating labels (merge_asof direction=forward + lag guard)...")
    dataset, drop_stats = generate_labels(features, prices, horizon_sec, max_label_lag_sec)

    if dataset.empty:
        print("\n  ❌ No data to export after label generation")
        return 1

    # === Hard FAIL validation ===
    lag = dataset["label_lag_sec"]
    viol_low = int((lag < horizon_sec).sum())
    viol_high = int((lag > max_label_lag_sec).sum())

    if viol_low > 0 or viol_high > 0:
        print(f"\n  ❌ FATAL: label_lag_sec violations remain after drop!")
        print(f"     viol_low  (lag < {horizon_sec}s)          = {viol_low}")
        print(f"     viol_high (lag > {max_label_lag_sec}s) = {viol_high}")
        return 1

    print(f"\n  ✅ label_lag_sec validation PASS")
    print(f"     min={float(lag.min()):.1f}s  max={float(lag.max()):.1f}s  "
          f"(bounds: [{horizon_sec}, {max_label_lag_sec}])")

    # Add estimated roundtrip cost column
    if "spread_bps" in dataset.columns:
        dataset["cost_roundtrip_est"] = (fee_bps_roundtrip + dataset["spread_bps"].fillna(0)) / 10_000
    else:
        dataset["cost_roundtrip_est"] = fee_bps_roundtrip / 10_000
    print(f"  cost_roundtrip_est: mean={dataset['cost_roundtrip_est'].mean():.6f} "
          f"(fee={fee_bps_roundtrip}bps)")

    print(f"\n[4] Exporting to {output_path}...")
    if output_path.endswith(".parquet"):
        dataset.to_parquet(output_path, index=False)
    elif output_path.endswith(".csv"):
        dataset.to_csv(output_path, index=False)
    else:
        dataset.to_parquet(output_path, index=False)

    print(f"  ✅ Exported {len(dataset)} rows to {output_path}")

    # Summary stats
    print(f"\n  Drop summary:")
    if seg_stats is not None:
        print(f"    dropped_by_gap  (segment filter)    = {seg_stats['dropped_by_gap']}")
    print(f"    dropped_early   (lag < horizon)     = {drop_stats['dropped_early']}")
    print(f"    dropped_late    (lag > max_lag)      = {drop_stats['dropped_late']}")
    print(f"    dropped_no_label                     = {drop_stats['dropped_no_label']}")

    print(f"\n{sep}")
    print("  EXPORT COMPLETE ✅")
    print(sep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
