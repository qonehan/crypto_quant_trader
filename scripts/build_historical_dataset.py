"""
build_historical_dataset.py — GCP 원천 데이터 기반 일괄 학습 데이터셋 생성기

사용법:
  poetry run python scripts/build_historical_dataset.py --hours 48
  poetry run python scripts/build_historical_dataset.py --hours 24 --horizon 120 --predict-interval 5
  poetry run python scripts/build_historical_dataset.py --hours 72 --output data/datasets/hist_72h.parquet

흐름:
  1. GCP upbit_orderbook 로드 (최근 --hours 시간)
  2. 1초 리샘플링: mid, spread_bps, imb_notional_top5 계산
  3. 시뮬레이션 루프: 매 predict_interval_sec마다 BaselineModelV1.predict() 호출
     - rolling vol_window(600s)로 sigma_1s 실시간 계산
     - market_window(120s)로 model에 컨텍스트 공급
  4. 라벨링: merge_asof(direction='forward') → label_return 계산
  5. Parquet(또는 CSV) 저장
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from app.config import load_settings
from app.models.baseline_v1 import BaselineModelV1

# ── 상수 ──────────────────────────────────────────────────────────────────────
_EPS = 1e-12

# GCP upbit_orderbook 쿼리: Level 1 + orderbook_imbalance 전용 스키마
_LOAD_ORDERBOOK_SQL = text("""
    SELECT
        timestamp AS ts,
        (level_1_bid_price + level_1_ask_price) / 2.0 AS mid,
        level_1_ask_price - level_1_bid_price          AS spread_raw,
        orderbook_imbalance
    FROM upbit_orderbook
    WHERE level_1_bid_price IS NOT NULL
      AND level_1_ask_price IS NOT NULL
      AND timestamp >= :t_min
      AND timestamp <= :t_max
    ORDER BY timestamp
""")


# ── 1. GCP 원천 데이터 로드 ────────────────────────────────────────────────────

def load_orderbook(engine, t_min: datetime, t_max: datetime) -> pd.DataFrame:
    """upbit_orderbook에서 raw tick 데이터 로드."""
    with engine.connect() as conn:
        df = pd.read_sql(
            _LOAD_ORDERBOOK_SQL,
            conn,
            params={"t_min": t_min, "t_max": t_max},
        )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


# ── 2. 1초 리샘플링 + 파생 지표 ────────────────────────────────────────────────

def resample_1s(raw: pd.DataFrame) -> pd.DataFrame:
    """
    tick DataFrame → 1초 캔들 변환.

    반환 컬럼: ts (index→column), mid, spread_bps, imb_notional_top5
    orderbook_imbalance 컬럼을 imb_notional_top5로 직접 매핑.
    """
    raw = raw.copy()

    # spread_bps: spread_raw / mid * 10000
    raw["spread_bps"] = (
        raw["spread_raw"] / raw["mid"].where(raw["mid"] > 0, other=_EPS) * 10_000
    )

    # orderbook_imbalance → imb_notional_top5 매핑
    raw["imb_notional_top5"] = raw["orderbook_imbalance"].fillna(0.0)

    raw = raw.set_index("ts")

    agg = raw[["mid", "spread_bps", "imb_notional_top5"]].resample("1s").agg({
        "mid": "last",
        "spread_bps": "mean",
        "imb_notional_top5": "mean",
    })

    agg = agg.ffill()      # 빈 1초 구간을 직전 값으로 보간
    agg = agg.reset_index()  # ts를 컬럼으로 복원
    return agg


# ── 3. sigma_1s 인라인 계산 (rolling) ─────────────────────────────────────────

def _compute_sigma_1s(mids: list[float], vol_dt_sec: int) -> float | None:
    """vol_window 내 mid로부터 sigma_1s 계산."""
    if len(mids) < 2:
        return None
    arr = np.array(mids[::vol_dt_sec] if vol_dt_sec > 1 else mids, dtype=np.float64)
    if len(arr) < 2:
        return None
    lr = np.diff(np.log(arr))
    lr = lr[np.isfinite(lr)]
    if len(lr) == 0:
        return None
    sigma_dt = float(np.std(lr, ddof=1))
    return sigma_dt / math.sqrt(vol_dt_sec) if vol_dt_sec > 1 else sigma_dt


def _build_barrier_row(
    ts: datetime,
    sigma_1s: float | None,
    sample_n: int,
    warmup_threshold: int,
    settings,
) -> dict:
    """market_window로부터 synthetic barrier_row dict 생성 (DB 없이)."""
    r_min_eff = settings.R_MIN

    if sigma_1s is None or sample_n < warmup_threshold:
        return {
            "ts": ts,
            "r_t": r_min_eff,
            "h_sec": settings.H_SEC,
            "sigma_1s": None,
            "sigma_h": None,
            "status": "WARMUP",
        }

    sigma_h = sigma_1s * math.sqrt(settings.H_SEC)
    r_t = max(r_min_eff, settings.K_VOL * sigma_h)
    r_t = min(r_t, settings.R_MAX)

    return {
        "ts": ts,
        "r_t": r_t,
        "h_sec": settings.H_SEC,
        "sigma_1s": sigma_1s,
        "sigma_h": sigma_h,
        "status": "OK",
    }


# ── 4. 시뮬레이션 루프 ─────────────────────────────────────────────────────────

def run_simulation(
    candles: pd.DataFrame,
    settings,
    predict_interval_sec: int,
) -> pd.DataFrame:
    """
    1초 캔들을 시간순으로 순회하며 BaselineModelV1.predict()를 호출.

    predict_interval_sec 마다 예측을 수행하고 결과를 rows 리스트로 반환.
    """
    model = BaselineModelV1()

    model_lookback = settings.MODEL_LOOKBACK_SEC
    vol_window_sec = settings.VOL_WINDOW_SEC
    vol_dt_sec = max(1, settings.VOL_DT_SEC)
    warmup_threshold = max(30, int((vol_window_sec / vol_dt_sec) * 0.3))

    # rolling deques
    market_window: deque[dict] = deque(maxlen=model_lookback)
    vol_window: deque[float] = deque(maxlen=vol_window_sec)

    rows = []
    step = 0

    total = len(candles)
    print(f"  총 {total:,} 스텝 순회 시작 (predict_interval={predict_interval_sec}s) ...")

    for _, row in candles.iterrows():
        ts: datetime = row["ts"]
        mid: float = float(row["mid"]) if pd.notna(row["mid"]) else 0.0
        spread_bps: float = float(row["spread_bps"]) if pd.notna(row["spread_bps"]) else 0.0
        imb: float = float(row["imb_notional_top5"]) if pd.notna(row["imb_notional_top5"]) else 0.0

        # market_window 엔트리 (PredictionRunner와 동일한 키 구조)
        entry = {
            "ts": ts,
            "mid": mid,
            "mid_close_1s": mid,
            "spread": spread_bps / 10_000 * mid if mid > 0 else 0.0,
            "spread_bps": spread_bps,
            "imbalance_top5": imb,
            "imb_notional_top5": imb,
        }
        market_window.append(entry)

        if mid > 0:
            vol_window.append(mid)

        step += 1

        # predict_interval_sec 마다 예측
        if step % predict_interval_sec != 0:
            continue

        sigma_1s = _compute_sigma_1s(list(vol_window), vol_dt_sec)
        barrier_row = _build_barrier_row(
            ts, sigma_1s, len(vol_window), warmup_threshold, settings
        )

        output = model.predict(
            market_window=list(market_window),
            barrier_row=barrier_row,
            settings=settings,
        )

        rows.append({
            "ts": ts,
            # barrier
            "r_t": barrier_row["r_t"],
            "sigma_1s": barrier_row.get("sigma_1s"),
            "sigma_h": barrier_row.get("sigma_h"),
            "barrier_status": barrier_row["status"],
            # predictions
            "p_up": output.p_up,
            "p_down": output.p_down,
            "p_none": output.p_none,
            "ev": output.ev,
            "ev_rate": output.ev_rate,
            "z_barrier": output.z_barrier,
            "mom_z": output.mom_z,
            "spread_bps": output.spread_bps,
            "imb_notional_top5": output.imb_notional_top5,
            "action_hat": output.action_hat,
            "model_version": output.model_version,
        })

    print(f"  시뮬레이션 완료: {len(rows):,} 예측 행 생성")
    return pd.DataFrame(rows)


# ── 5. 라벨 생성 (export_dataset.py와 동일 방식) ──────────────────────────────

def generate_labels(
    features: pd.DataFrame,
    prices: pd.DataFrame,
    horizon_sec: int,
    max_label_lag_sec: int,
) -> tuple[pd.DataFrame, dict]:
    """
    merge_asof(direction='forward')로 horizon_sec 이후 mid를 매칭.
    label_return = (future_mid - entry_mid) / entry_mid
    """
    drop_stats = {"dropped_early": 0, "dropped_late": 0, "dropped_no_label": 0}

    if features.empty or prices.empty:
        print("  WARNING: features 또는 prices DataFrame이 비어 있음")
        return pd.DataFrame(), drop_stats

    horizon_td = pd.Timedelta(seconds=horizon_sec)
    features = features.copy()
    features["t0_plus_h"] = features["ts"] + horizon_td

    features_sorted = features.sort_values("t0_plus_h").reset_index(drop=True)
    prices_sorted = prices.sort_values("ts").reset_index(drop=True)

    # 미래 가격 매칭
    merged = pd.merge_asof(
        features_sorted,
        prices_sorted.rename(columns={"ts": "label_ts", "mid": "future_mid"}),
        left_on="t0_plus_h",
        right_on="label_ts",
        direction="forward",
    )

    # 진입 가격 매칭
    merged = pd.merge_asof(
        merged.sort_values("ts"),
        prices_sorted.rename(columns={"ts": "price_ts", "mid": "entry_mid"}),
        left_on="ts",
        right_on="price_ts",
        direction="backward",
    )

    n_before = len(merged)

    no_label = merged["label_ts"].isna()
    drop_stats["dropped_no_label"] = int(no_label.sum())
    if drop_stats["dropped_no_label"] > 0:
        print(f"  ⚠  Dropped {drop_stats['dropped_no_label']} rows: no future price")
        merged = merged[~no_label].copy()

    merged["label_lag_sec"] = (merged["label_ts"] - merged["ts"]).dt.total_seconds()

    early_mask = merged["label_lag_sec"] < horizon_sec
    drop_stats["dropped_early"] = int(early_mask.sum())
    if drop_stats["dropped_early"] > 0:
        print(f"  ⚠  Dropped {drop_stats['dropped_early']} rows: label_lag_sec < horizon")
        merged = merged[~early_mask].copy()

    late_mask = merged["label_lag_sec"] > max_label_lag_sec
    drop_stats["dropped_late"] = int(late_mask.sum())
    if drop_stats["dropped_late"] > 0:
        print(f"  ⚠  Dropped {drop_stats['dropped_late']} rows: label_lag_sec > max_lag")
        merged = merged[~late_mask].copy()

    n_after = len(merged)
    print(
        f"  라벨 생성: {n_before} → {n_after} rows "
        f"(early={drop_stats['dropped_early']}, "
        f"late={drop_stats['dropped_late']}, "
        f"no_label={drop_stats['dropped_no_label']})"
    )

    merged["label_return"] = (
        (merged["future_mid"] - merged["entry_mid"]) / merged["entry_mid"]
    )

    cols_drop = ["t0_plus_h", "price_ts"]
    merged = merged.drop(columns=[c for c in cols_drop if c in merged.columns])
    return merged, drop_stats


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="GCP 원천 데이터 기반 일괄 학습 데이터셋 생성기"
    )
    parser.add_argument("--hours", type=float, default=24.0,
                        help="GCP에서 가져올 과거 데이터 기간 (시간, 기본 24)")
    parser.add_argument("--horizon", type=int, default=None,
                        help="라벨 horizon 초 (기본: settings.H_SEC)")
    parser.add_argument("--predict-interval", type=int, default=5,
                        help="예측 수행 간격 초 (기본 5)")
    parser.add_argument("--max-label-lag-mult", type=float, default=2.0,
                        help="max_label_lag = horizon × mult (기본 2.0)")
    parser.add_argument("--output", type=str,
                        default="data/datasets/historical_dataset.parquet",
                        help="출력 파일 경로")
    args = parser.parse_args()

    s = load_settings()
    horizon_sec = args.horizon or s.H_SEC
    max_label_lag_sec = int(horizon_sec * args.max_label_lag_mult)

    sep = "=" * 60
    print(sep)
    print("  Historical Dataset Builder (GCP 원천 데이터 기반)")
    print(f"  hours               = {args.hours}")
    print(f"  horizon_sec         = {horizon_sec}")
    print(f"  predict_interval    = {args.predict_interval}s")
    print(f"  max_label_lag_sec   = {max_label_lag_sec}")
    print(f"  output              = {args.output}")
    print(sep)

    # ── 엔진 생성 ─────────────────────────────────────────────────────────────
    if not s.GCP_DB_URL:
        print("  ERROR: GCP_DB_URL 환경변수가 설정되지 않았습니다.")
        return 1

    engine_gcp = create_engine(s.GCP_DB_URL)

    now_utc = datetime.now(tz=timezone.utc)
    t_min = now_utc - timedelta(hours=args.hours)
    t_max = now_utc
    print(f"  query range: {t_min.strftime('%Y-%m-%d %H:%M')} ~ {t_max.strftime('%Y-%m-%d %H:%M')} UTC")

    # ── Step 1: GCP 원천 데이터 로드 ──────────────────────────────────────────
    print("\n[1] upbit_orderbook 로드 중...")
    raw = load_orderbook(engine_gcp, t_min, t_max)
    if raw.empty:
        print("  ERROR: GCP에서 데이터를 가져오지 못했습니다.")
        return 1
    print(f"  raw ticks: {len(raw):,} rows  "
          f"({raw['ts'].min()} ~ {raw['ts'].max()})")

    # ── Step 2: 1초 리샘플링 ──────────────────────────────────────────────────
    print("\n[2] 1초 리샘플링 중...")
    candles = resample_1s(raw)
    del raw  # 메모리 해제
    print(f"  1s candles: {len(candles):,} rows")

    # ── Step 3: 시뮬레이션 루프 ───────────────────────────────────────────────
    print("\n[3] 시뮬레이션 루프 (feature 계산)...")
    features = run_simulation(candles, s, args.predict_interval)
    if features.empty:
        print("  ERROR: 시뮬레이션 결과가 없습니다. 데이터 범위를 확인하세요.")
        return 1

    # ── Step 4: 라벨 생성 ─────────────────────────────────────────────────────
    print("\n[4] 라벨 생성 (merge_asof direction=forward)...")
    # 라벨용 가격은 1초 캔들 (이미 리샘플링된 candles) 재활용
    prices = candles[["ts", "mid"]].copy()
    # 라벨 horizon 이후 데이터가 필요하므로 상한을 여유 있게 설정 (이미 포함됨)

    dataset, drop_stats = generate_labels(features, prices, horizon_sec, max_label_lag_sec)
    if dataset.empty:
        print("  ERROR: 라벨 생성 후 데이터가 없습니다.")
        return 1

    # ── Hard FAIL 검증 ────────────────────────────────────────────────────────
    lag = dataset["label_lag_sec"]
    viol_low = int((lag < horizon_sec).sum())
    viol_high = int((lag > max_label_lag_sec).sum())
    if viol_low > 0 or viol_high > 0:
        print(f"\n  FATAL: label_lag_sec 위반 잔존!")
        print(f"    viol_low  (lag < {horizon_sec}s) = {viol_low}")
        print(f"    viol_high (lag > {max_label_lag_sec}s) = {viol_high}")
        return 1
    print(f"  label_lag_sec PASS  min={lag.min():.1f}s  max={lag.max():.1f}s")

    # ── Step 5: 저장 ──────────────────────────────────────────────────────────
    print(f"\n[5] {args.output} 저장 중...")
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.output.endswith(".csv"):
        dataset.to_csv(out_path, index=False)
    else:
        dataset.to_parquet(out_path, index=False)

    print(f"  Exported {len(dataset):,} rows → {out_path}")

    # ── 요약 ──────────────────────────────────────────────────────────────────
    print(f"\n  Drop summary:")
    print(f"    dropped_early    = {drop_stats['dropped_early']}")
    print(f"    dropped_late     = {drop_stats['dropped_late']}")
    print(f"    dropped_no_label = {drop_stats['dropped_no_label']}")
    print(f"\n  label_return: mean={dataset['label_return'].mean():.6f}  "
          f"std={dataset['label_return'].std():.6f}")
    action_dist = dataset["action_hat"].value_counts().to_dict()
    print(f"  action_hat dist:  {action_dist}")

    print(f"\n{sep}")
    print("  BUILD COMPLETE")
    print(sep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
