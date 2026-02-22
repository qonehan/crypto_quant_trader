"""
export_dataset.py — feature_snapshots → 학습/백테스트용 Dataset 내보내기

사용법:
  poetry run python -m app.features.export_dataset \\
    --symbol KRW-BTC \\
    --start "2026-02-22T00:00:00Z" \\
    --end   "2026-02-22T06:00:00Z" \\
    --horizon-sec 120 \\
    --out ./data/datasets/krw_btc_features.parquet \\
    --format parquet

옵션:
  --format parquet|csv   (기본 parquet)
  --label-type direction|binary|continuous  (기본 direction)
    - direction: y = UP/DOWN/NONE (future_return 기준 ±r_t)
    - binary:    y = 1 if future_return > 0 else 0
    - continuous: y = future_return (float)
  --no-label             라벨 없이 피처만 export

누수 방지:
  - 피처는 모두 t0 이하 값 (DB에서 ts <= t0 쿼리로 보장)
  - 라벨(future_return)은 t0+horizon의 mid_krw 사용 (라벨 생성에만 미래 사용)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from app.config import load_settings


def _parse_dt(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_feature_snapshots(
    engine,
    symbol: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """feature_snapshots를 시간 범위로 로딩."""
    with engine.connect() as conn:
        df = pd.read_sql_query(
            text("""
                SELECT
                    ts, symbol,
                    mid_krw, spread_bps, imb_notional_top5,
                    r_t, r_min_eff, cost_roundtrip_est,
                    sigma_1s, sigma_h, k_vol_eff, barrier_status,
                    p_up, p_down, p_none, ev, ev_rate, action_hat, model_version,
                    bin_mark_price, bin_index_price, bin_funding_rate, bin_mark_index_basis,
                    oi_value, global_ls_ratio, taker_ls_ratio, basis_value,
                    liq_5m_notional, liq_5m_count,
                    bin_mark_ts, oi_ts, liq_last_ts
                FROM feature_snapshots
                WHERE symbol = :sym
                  AND ts >= :start
                  AND ts <= :end
                ORDER BY ts ASC
            """),
            conn,
            params={"sym": symbol, "start": start, "end": end},
        )
    return df


def create_labels(
    df_features: pd.DataFrame,
    df_future: pd.DataFrame,
    horizon_sec: int,
    label_type: str,
    tolerance_sec: int = 5,
) -> pd.DataFrame:
    """미래 mid_krw 기준으로 라벨을 생성한다.

    피처 df_features와 미래용 df_future를 merge_asof로 결합.
    - label_type='direction': future_return > r_t → UP, < -r_t → DOWN, else NONE
    - label_type='binary':    future_return > 0 → 1 else 0
    - label_type='continuous': future_return (float)
    """
    df_feat = df_features.copy()
    df_feat["ts"] = pd.to_datetime(df_feat["ts"], utc=True)

    df_fut = df_future[["ts", "mid_krw"]].copy()
    df_fut["ts"] = pd.to_datetime(df_fut["ts"], utc=True)
    df_fut = df_fut.rename(columns={"mid_krw": "future_mid_krw"})

    # 타겟 ts: t0 + horizon_sec
    df_feat["target_ts"] = df_feat["ts"] + pd.Timedelta(seconds=horizon_sec)

    # merge_asof: target_ts 기준으로 가장 가까운 미래 row 매칭
    df_feat_sorted = df_feat.sort_values("target_ts")
    df_fut_sorted = df_fut.sort_values("ts")

    merged = pd.merge_asof(
        df_feat_sorted,
        df_fut_sorted,
        left_on="target_ts",
        right_on="ts",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=tolerance_sec),
        suffixes=("", "_future"),
    )

    # 미래 mid가 없는 row 제거
    before_drop = len(merged)
    merged = merged.dropna(subset=["future_mid_krw"])
    dropped = before_drop - len(merged)

    # future_return 계산
    merged["future_return"] = merged["future_mid_krw"] / merged["mid_krw"] - 1

    # 라벨 생성
    if label_type == "direction":
        r_t = merged["r_t"].fillna(0.001)
        merged["y"] = "NONE"
        merged.loc[merged["future_return"] > r_t, "y"] = "UP"
        merged.loc[merged["future_return"] < -r_t, "y"] = "DOWN"
    elif label_type == "binary":
        merged["y"] = (merged["future_return"] > 0).astype(int)
    elif label_type == "continuous":
        merged["y"] = merged["future_return"]
    else:
        raise ValueError(f"Unknown label_type: {label_type}")

    # 불필요한 컬럼 정리
    merged = merged.drop(columns=["target_ts", "ts_future"], errors="ignore")
    merged = merged.sort_values("ts").reset_index(drop=True)

    return merged, dropped


def main() -> int:
    parser = argparse.ArgumentParser(description="feature_snapshots → 학습용 Dataset export")
    parser.add_argument("--symbol", default="KRW-BTC", help="심볼 (기본 KRW-BTC)")
    parser.add_argument("--start", required=True, help="시작 시각 (ISO8601, UTC)")
    parser.add_argument("--end", required=True, help="종료 시각 (ISO8601, UTC)")
    parser.add_argument("--horizon-sec", type=int, default=120, help="라벨 horizon(초, 기본 120)")
    parser.add_argument("--out", required=True, help="출력 파일 경로")
    parser.add_argument("--format", choices=["parquet", "csv"], default="parquet", help="출력 형식")
    parser.add_argument("--label-type", choices=["direction", "binary", "continuous"],
                        default="direction", help="라벨 타입")
    parser.add_argument("--no-label", action="store_true", help="라벨 없이 피처만 export")
    args = parser.parse_args()

    start_dt = _parse_dt(args.start)
    end_dt = _parse_dt(args.end)
    horizon_sec = args.horizon_sec

    if start_dt >= end_dt:
        print("ERROR: --start must be before --end")
        return 1

    s = load_settings()
    engine = create_engine(s.DB_URL)

    print("=" * 60)
    print("  export_dataset")
    print(f"  symbol      = {args.symbol}")
    print(f"  start       = {start_dt.isoformat()}")
    print(f"  end         = {end_dt.isoformat()}")
    print(f"  horizon_sec = {horizon_sec}s")
    print(f"  label_type  = {'none' if args.no_label else args.label_type}")
    print(f"  format      = {args.format}")
    print(f"  out         = {args.out}")
    print("=" * 60)

    # 피처 로딩 (기간 내)
    print("Loading feature_snapshots...")
    df_features = load_feature_snapshots(engine, args.symbol, start_dt, end_dt)
    print(f"  Loaded {len(df_features)} feature rows")

    if df_features.empty:
        print("ERROR: No feature_snapshots data in the given range")
        return 1

    if args.no_label:
        df_out = df_features
        dropped = 0
    else:
        # 라벨용 미래 데이터 로딩 (end + horizon + tolerance)
        future_end = end_dt + timedelta(seconds=horizon_sec + 10)
        print(f"Loading future snapshots up to {future_end.isoformat()}...")
        df_future = load_feature_snapshots(engine, args.symbol, start_dt, future_end)
        print(f"  Loaded {len(df_future)} rows for label matching")

        if df_future.empty:
            print("ERROR: No future data available for label creation")
            return 1

        print("Creating labels...")
        df_out, dropped = create_labels(
            df_features, df_future, horizon_sec, args.label_type
        )
        print(f"  Label created: {len(df_out)} rows ({dropped} dropped — no future match)")

        if args.label_type == "direction":
            dist = df_out["y"].value_counts()
            print(f"  Label distribution: {dist.to_dict()}")

    # 출력 경로 생성
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Writing {args.format} to {out_path}...")
    if args.format == "parquet":
        df_out.to_parquet(out_path, index=False)
    else:
        df_out.to_csv(out_path, index=False)

    size_kb = out_path.stat().st_size / 1024
    print(f"  Written: {len(df_out)} rows, {len(df_out.columns)} cols, {size_kb:.1f} KB")
    print(f"  Columns: {list(df_out.columns)[:10]}{'...' if len(df_out.columns) > 10 else ''}")
    print("=" * 60)
    print("DONE ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
