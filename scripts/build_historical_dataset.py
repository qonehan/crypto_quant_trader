"""
build_historical_dataset.py — GCP 원천 데이터 기반 일괄 학습 데이터셋 생성기

사용법:
  poetry run python scripts/build_historical_dataset.py --hours 48
  poetry run python scripts/build_historical_dataset.py --hours 24 --horizon 120 --predict-interval 5
  poetry run python scripts/build_historical_dataset.py --hours 72 --output data/datasets/hist_72h.parquet
  poetry run python scripts/build_historical_dataset.py --hours 72 --backup-dir data/backups

흐름:
  1. 4대 원천 데이터 하이브리드 로드 (백업 Parquet 우선, GCP DB 보완):
     - upbit_orderbook  → mid, spread_bps, imb_notional_top5, cost_roundtrip_est
     - upbit_tick       → buy_volume_ratio (매수 우위 비율)
     - binance          → funding_rate, long_short_ratio, open_interest
     - macro            → dxy_index, fear_greed_index
  2. 1초 리샘플링 및 피처 엔지니어링 후 Left-join 병합
  3. 시뮬레이션 루프: 매 predict_interval_sec마다 BaselineModelV1.predict() 호출
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

# ── SQL 쿼리 ──────────────────────────────────────────────────────────────────

_LOAD_ORDERBOOK_SQL = text("""
    SELECT
        timestamp AS ts,
        (level_1_bid_price + level_1_ask_price) / 2.0 AS mid,
        level_1_ask_price - level_1_bid_price          AS spread_raw,
        orderbook_imbalance                            AS imb_notional_top5
    FROM upbit_orderbook
    WHERE level_1_bid_price IS NOT NULL
      AND level_1_ask_price IS NOT NULL
      AND timestamp >= :t_min
      AND timestamp <= :t_max
    ORDER BY timestamp
""")

_LOAD_TICK_SQL = text("""
    SELECT
        timestamp AS ts,
        price,
        volume,
        ask_bid
    FROM upbit_tick
    WHERE timestamp >= :t_min
      AND timestamp <= :t_max
    ORDER BY timestamp
""")

# binance_derivatives 단일 테이블 쿼리
_LOAD_BINANCE_SQL = text("""
    SELECT
        timestamp AS ts,
        open_interest,
        long_short_ratio,
        funding_rate
    FROM binance_derivatives
    WHERE timestamp >= :t_min
      AND timestamp <= :t_max
    ORDER BY timestamp
""")

_LOAD_MACRO_SQL = text("""
    SELECT
        timestamp AS ts,
        dxy_index,
        fear_greed_index
    FROM macro_and_sentiment
    WHERE timestamp >= :t_min
      AND timestamp <= :t_max
    ORDER BY timestamp
""")


# ── 공통 날짜 리스트 생성 헬퍼 ─────────────────────────────────────────────────

def _date_range(t_min: datetime, t_max: datetime) -> list:
    """t_min ~ t_max 범위의 UTC date 리스트 반환."""
    t_min_utc = t_min.astimezone(timezone.utc) if t_min.tzinfo else t_min.replace(tzinfo=timezone.utc)
    t_max_utc = t_max.astimezone(timezone.utc) if t_max.tzinfo else t_max.replace(tzinfo=timezone.utc)
    dates = []
    cur = t_min_utc.date()
    end = t_max_utc.date()
    while cur <= end:
        dates.append(cur)
        cur += timedelta(days=1)
    return dates


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read_parquet_days(
    backup_root: Path,
    file_name: str,
    all_dates: list,
    t_min: datetime,
    t_max: datetime,
    ts_col: str = "ts",
) -> list[pd.DataFrame]:
    """날짜별 백업 Parquet을 읽어 list로 반환. ts_col을 UTC ts로 변환하고 기간 자르기."""
    frames = []
    for day in all_dates:
        path = backup_root / day.strftime("%Y-%m-%d") / file_name
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path)
            if df.empty:
                continue
            if "timestamp" in df.columns and ts_col not in df.columns:
                df = df.rename(columns={"timestamp": ts_col})
            df[ts_col] = pd.to_datetime(df[ts_col], utc=True)
            df = df[(df[ts_col] >= t_min) & (df[ts_col] <= t_max)]
            if not df.empty:
                frames.append(df)
                print(f"    [backup] {path}  {len(df):,} rows")
        except Exception as exc:
            print(f"    [backup] WARNING: {path} 읽기 실패 — {exc}")
    return frames


def _merge_and_dedup(frames: list[pd.DataFrame], ts_col: str = "ts") -> pd.DataFrame:
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(ts_col).drop_duplicates(subset=[ts_col]).reset_index(drop=True)
    return combined


# ── 1. 하이브리드 데이터 로드 함수들 ───────────────────────────────────────────

def load_orderbook(
    engine,
    t_min: datetime,
    t_max: datetime,
    backup_dir: str = "data/backups",
) -> pd.DataFrame:
    """
    upbit_orderbook: 백업 Parquet + GCP DB 하이브리드 로드.
    반환 컬럼: ts, mid, spread_raw, imb_notional_top5
    """
    backup_root = Path(backup_dir)
    t_min_utc, t_max_utc = _utc(t_min), _utc(t_max)
    all_dates = _date_range(t_min, t_max)

    # ── 백업 Parquet ──────────────────────────────────────────────────────────
    parquet_frames: list[pd.DataFrame] = []
    for day in all_dates:
        path = backup_root / day.strftime("%Y-%m-%d") / "upbit_orderbook.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path)
            if df.empty:
                continue
            if "timestamp" in df.columns:
                df = df.rename(columns={"timestamp": "ts"})
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            df["mid"] = (df["level_1_bid_price"] + df["level_1_ask_price"]) / 2.0
            if "orderbook_imbalance" in df.columns:
                df = df.rename(columns={"orderbook_imbalance": "imb_notional_top5"})
            df["spread_raw"] = df["level_1_ask_price"] - df["level_1_bid_price"]
            df = df[df["level_1_bid_price"].notna() & df["level_1_ask_price"].notna()]
            df = df[(df["ts"] >= t_min_utc) & (df["ts"] <= t_max_utc)]
            if not df.empty:
                parquet_frames.append(df[["ts", "mid", "spread_raw", "imb_notional_top5"]])
                print(f"    [backup] {path}  {len(df):,} rows")
        except Exception as exc:
            print(f"    [backup] WARNING: {path} 읽기 실패 — {exc}")

    # ── GCP DB ────────────────────────────────────────────────────────────────
    db_df = pd.DataFrame()
    try:
        with engine.connect() as conn:
            db_df = pd.read_sql(_LOAD_ORDERBOOK_SQL, conn,
                                params={"t_min": t_min_utc, "t_max": t_max_utc})
        if not db_df.empty:
            db_df["ts"] = pd.to_datetime(db_df["ts"], utc=True)
            print(f"    [db]     {len(db_df):,} rows from GCP DB (orderbook)")
    except Exception as exc:
        print(f"    [db]     WARNING: GCP DB 쿼리 실패 (orderbook) — {exc}")

    frames = parquet_frames + ([db_df] if not db_df.empty else [])
    return _merge_and_dedup(frames)


def load_tick(
    engine,
    t_min: datetime,
    t_max: datetime,
    backup_dir: str = "data/backups",
) -> pd.DataFrame:
    """
    upbit_tick: 백업 Parquet + GCP DB 하이브리드 로드.
    반환 컬럼: ts, price, volume, ask_bid
    """
    backup_root = Path(backup_dir)
    t_min_utc, t_max_utc = _utc(t_min), _utc(t_max)
    all_dates = _date_range(t_min, t_max)

    # ── 백업 Parquet ──────────────────────────────────────────────────────────
    raw_frames = _read_parquet_days(backup_root, "upbit_tick.parquet", all_dates,
                                    t_min_utc, t_max_utc, ts_col="ts")

    # ── GCP DB ────────────────────────────────────────────────────────────────
    db_df = pd.DataFrame()
    try:
        with engine.connect() as conn:
            db_df = pd.read_sql(_LOAD_TICK_SQL, conn,
                                params={"t_min": t_min_utc, "t_max": t_max_utc})
        if not db_df.empty:
            db_df["ts"] = pd.to_datetime(db_df["ts"], utc=True)
            print(f"    [db]     {len(db_df):,} rows from GCP DB (tick)")
    except Exception as exc:
        print(f"    [db]     WARNING: GCP DB 쿼리 실패 (tick) — {exc}")

    frames = raw_frames + ([db_df] if not db_df.empty else [])
    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values("ts").reset_index(drop=True)
    # tick은 ts 중복 허용 (동일 초에 여러 체결)
    return combined


def load_binance(
    engine,
    t_min: datetime,
    t_max: datetime,
    backup_dir: str = "data/backups",
) -> pd.DataFrame:
    """
    Binance 파생 데이터: 백업 Parquet + GCP DB 하이브리드 로드.
    DB 테이블: binance_derivatives (timestamp, open_interest, long_short_ratio, funding_rate)
    백업 파일: binance_derivatives.parquet
    반환 컬럼: ts, funding_rate, long_short_ratio, open_interest
    """
    backup_root = Path(backup_dir)
    t_min_utc, t_max_utc = _utc(t_min), _utc(t_max)
    all_dates = _date_range(t_min, t_max)

    # ── 백업 Parquet ──────────────────────────────────────────────────────────
    raw_frames = _read_parquet_days(backup_root, "binance_derivatives.parquet",
                                    all_dates, t_min_utc, t_max_utc, ts_col="ts")

    # ── GCP DB ────────────────────────────────────────────────────────────────
    db_df = pd.DataFrame()
    try:
        with engine.connect() as conn:
            db_df = pd.read_sql(_LOAD_BINANCE_SQL, conn,
                                params={"t_min": t_min_utc, "t_max": t_max_utc})
        if not db_df.empty:
            db_df["ts"] = pd.to_datetime(db_df["ts"], utc=True)
            print(f"    [db]     {len(db_df):,} rows from GCP DB (binance_derivatives)")
    except Exception as exc:
        print(f"    [db]     WARNING: GCP DB 쿼리 실패 (binance_derivatives) — {exc}")

    frames = raw_frames + ([db_df] if not db_df.empty else [])
    result = _merge_and_dedup(frames)
    if result.empty:
        return pd.DataFrame()

    for col in ("funding_rate", "long_short_ratio", "open_interest"):
        if col not in result.columns:
            result[col] = float("nan")

    return result[["ts", "funding_rate", "long_short_ratio", "open_interest"]].copy()


def load_macro(
    engine,
    t_min: datetime,
    t_max: datetime,
    backup_dir: str = "data/backups",
) -> pd.DataFrame:
    """
    Macro 데이터: 백업 Parquet + GCP DB 하이브리드 로드.
    반환 컬럼: ts, dxy_index, fear_greed_index
    """
    backup_root = Path(backup_dir)
    t_min_utc, t_max_utc = _utc(t_min), _utc(t_max)
    all_dates = _date_range(t_min, t_max)

    # ── 백업 Parquet ──────────────────────────────────────────────────────────
    raw_frames = _read_parquet_days(backup_root, "macro_and_sentiment.parquet", all_dates,
                                    t_min_utc, t_max_utc, ts_col="ts")

    # ── GCP DB ────────────────────────────────────────────────────────────────
    db_df = pd.DataFrame()
    try:
        with engine.connect() as conn:
            db_df = pd.read_sql(_LOAD_MACRO_SQL, conn,
                                params={"t_min": t_min_utc, "t_max": t_max_utc})
        if not db_df.empty:
            db_df["ts"] = pd.to_datetime(db_df["ts"], utc=True)
            print(f"    [db]     {len(db_df):,} rows from GCP DB (macro)")
    except Exception as exc:
        print(f"    [db]     WARNING: GCP DB 쿼리 실패 (macro) — {exc}")

    frames = raw_frames + ([db_df] if not db_df.empty else [])
    result = _merge_and_dedup(frames)
    if result.empty:
        return pd.DataFrame()

    for col in ("dxy_index", "fear_greed_index"):
        if col not in result.columns:
            result[col] = float("nan")

    return result[["ts", "dxy_index", "fear_greed_index"]].copy()


# ── 2. 리샘플링 헬퍼 함수 ──────────────────────────────────────────────────────

def _resample_tick_1s(raw_tick: pd.DataFrame) -> pd.DataFrame:
    """
    tick raw → 1초 집계.
    반환 컬럼: ts, total_volume, buy_volume, sell_volume, buy_volume_ratio
    빈 구간은 0으로 채움.
    ask_bid == 'ASK' → 매수 체결 (업비트 컨벤션: ASK = 시장 매수)
    """
    if raw_tick.empty:
        return pd.DataFrame()

    df = raw_tick.copy()
    df["is_buy"] = (df["ask_bid"].str.upper() == "ASK").astype(float)
    df["buy_vol"] = df["volume"] * df["is_buy"]
    df = df.set_index("ts")

    agg = df[["volume", "buy_vol"]].resample("1s").sum()
    agg.columns = ["total_volume", "buy_volume"]
    agg["sell_volume"] = agg["total_volume"] - agg["buy_volume"]
    agg["buy_volume_ratio"] = agg["buy_volume"] / agg["total_volume"].where(
        agg["total_volume"] > 0, other=float("nan")
    )

    # 거래 없는 구간: 0으로 채움
    agg = agg.fillna({"total_volume": 0.0, "buy_volume": 0.0,
                       "sell_volume": 0.0, "buy_volume_ratio": 0.0})
    return agg.reset_index()


def _resample_binance_1s(raw_binance: pd.DataFrame, index_ts: pd.Series) -> pd.DataFrame:
    """
    binance 파생 데이터 → 1초 단위 ffill 리샘플링.
    반환 컬럼: ts, funding_rate, long_short_ratio, open_interest
    """
    if raw_binance.empty:
        return pd.DataFrame()

    df = raw_binance.set_index("ts")
    # 1초 기준으로 리인덱스 후 ffill
    df = df.resample("1s").last().ffill()
    return df.reset_index()


def _resample_macro_1s(raw_macro: pd.DataFrame) -> pd.DataFrame:
    """
    macro 데이터 → 1초 단위 ffill 리샘플링.
    반환 컬럼: ts, dxy_index, fear_greed_index
    """
    if raw_macro.empty:
        return pd.DataFrame()

    df = raw_macro.set_index("ts")
    df = df.resample("1s").last().ffill()
    return df.reset_index()


# ── 3. 통합 1초 리샘플링 + 4대 소스 병합 ──────────────────────────────────────

def resample_1s(
    raw: pd.DataFrame,
    raw_tick: pd.DataFrame | None = None,
    raw_binance: pd.DataFrame | None = None,
    raw_macro: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    4대 원천 tick DataFrame → 1초 캔들 변환 및 병합.

    주축(Left): Orderbook 기반 1초 캔들
    Right-join:  Tick, Binance, Macro (merge_asof direction='backward', 이후 bfill)

    반환 컬럼:
      ts, mid, spread_bps, imb_notional_top5, cost_roundtrip_est,
      total_volume, buy_volume, sell_volume, buy_volume_ratio,
      funding_rate, long_short_ratio, open_interest,
      dxy_index, fear_greed_index
    """
    # ── Orderbook 1초 캔들 ────────────────────────────────────────────────────
    ob = raw.copy()

    ob["spread_bps"] = (
        ob["spread_raw"] / ob["mid"].where(ob["mid"] > 0, other=_EPS) * 10_000
    )
    ob["imb_notional_top5"] = ob["imb_notional_top5"].fillna(0.0)
    ob["cost_roundtrip_est"] = (
        ob["spread_raw"] / ob["mid"].where(ob["mid"] > 0, other=_EPS)
    ) + 0.001

    ob = ob.set_index("ts")
    agg = ob[["mid", "spread_bps", "imb_notional_top5", "cost_roundtrip_est"]].resample("1s").agg({
        "mid": "last",
        "spread_bps": "mean",
        "imb_notional_top5": "mean",
        "cost_roundtrip_est": "mean",
    })
    agg = agg.ffill().reset_index()  # 주축 캔들

    # ── Tick 1초 집계 ─────────────────────────────────────────────────────────
    tick_1s = _resample_tick_1s(raw_tick) if raw_tick is not None and not raw_tick.empty else pd.DataFrame()

    # ── Binance 1초 ffill ──────────────────────────────────────────────────────
    binance_1s = (_resample_binance_1s(raw_binance, agg["ts"])
                  if raw_binance is not None and not raw_binance.empty else pd.DataFrame())

    # ── Macro 1초 ffill ────────────────────────────────────────────────────────
    macro_1s = (_resample_macro_1s(raw_macro)
                if raw_macro is not None and not raw_macro.empty else pd.DataFrame())

    # ── Left-join: orderbook 기준으로 Tick, Binance, Macro 병합 ───────────────
    result = agg.sort_values("ts").reset_index(drop=True)

    if not tick_1s.empty:
        tick_1s = tick_1s.sort_values("ts").reset_index(drop=True)
        result = pd.merge_asof(result, tick_1s, on="ts", direction="backward")
    else:
        result["total_volume"] = 0.0
        result["buy_volume"] = 0.0
        result["sell_volume"] = 0.0
        result["buy_volume_ratio"] = 0.0

    if not binance_1s.empty:
        binance_1s = binance_1s.sort_values("ts").reset_index(drop=True)
        result = pd.merge_asof(result, binance_1s, on="ts", direction="backward")
    else:
        result["funding_rate"] = float("nan")
        result["long_short_ratio"] = float("nan")
        result["open_interest"] = float("nan")

    if not macro_1s.empty:
        macro_1s = macro_1s.sort_values("ts").reset_index(drop=True)
        result = pd.merge_asof(result, macro_1s, on="ts", direction="backward")
    else:
        result["dxy_index"] = float("nan")
        result["fear_greed_index"] = float("nan")

    # ── NaN 보간: 초반 결측치 bfill → 나머지 기본값 ────────────────────────────
    tick_cols = ["total_volume", "buy_volume", "sell_volume", "buy_volume_ratio"]
    binance_cols = ["funding_rate", "long_short_ratio", "open_interest"]
    macro_cols = ["dxy_index", "fear_greed_index"]

    result[tick_cols] = result[tick_cols].bfill().fillna(0.0)
    result[binance_cols] = result[binance_cols].bfill().fillna(0.0)
    result[macro_cols] = result[macro_cols].bfill().fillna(0.0)

    return result


# ── 4. sigma_1s 인라인 계산 (rolling) ─────────────────────────────────────────

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


# ── 5. 시뮬레이션 루프 ─────────────────────────────────────────────────────────

def run_simulation(
    candles: pd.DataFrame,
    settings,
    predict_interval_sec: int,
) -> pd.DataFrame:
    """
    1초 캔들을 시간순으로 순회하며 BaselineModelV1.predict()를 호출.
    predict_interval_sec 마다 예측을 수행하고 결과를 rows 리스트로 반환.
    4대 원천 피처(orderbook + tick + binance + macro) 모두 출력에 포함.
    """
    model = BaselineModelV1()

    model_lookback = settings.MODEL_LOOKBACK_SEC
    vol_window_sec = settings.VOL_WINDOW_SEC
    vol_dt_sec = max(1, settings.VOL_DT_SEC)
    warmup_threshold = max(30, int((vol_window_sec / vol_dt_sec) * 0.3))

    market_window: deque[dict] = deque(maxlen=model_lookback)
    vol_window: deque[float] = deque(maxlen=vol_window_sec)

    # 새 컬럼이 없을 경우를 대비한 safe getter
    def _fval(row, col, default=0.0):
        return float(row[col]) if col in row.index and pd.notna(row[col]) else default

    rows = []
    step = 0
    total = len(candles)
    print(f"  총 {total:,} 스텝 순회 시작 (predict_interval={predict_interval_sec}s) ...")

    for _, row in candles.iterrows():
        ts: datetime = row["ts"]
        mid: float = _fval(row, "mid", 0.0)
        spread_bps: float = _fval(row, "spread_bps", 0.0)
        imb: float = _fval(row, "imb_notional_top5", 0.0)
        cost_rt: float = _fval(row, "cost_roundtrip_est", 0.001)

        # 새 피처
        buy_vol_ratio: float = _fval(row, "buy_volume_ratio", 0.0)
        funding_rate: float = _fval(row, "funding_rate", 0.0)
        long_short_ratio: float = _fval(row, "long_short_ratio", 0.0)
        open_interest: float = _fval(row, "open_interest", 0.0)
        dxy_index: float = _fval(row, "dxy_index", 0.0)
        fear_greed_index: float = _fval(row, "fear_greed_index", 0.0)

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

        if step % predict_interval_sec != 0:
            continue

        sigma_1s = _compute_sigma_1s(list(vol_window), vol_dt_sec)
        barrier_row = _build_barrier_row(ts, sigma_1s, len(vol_window), warmup_threshold, settings)

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
            "cost_roundtrip_est": cost_rt,
            "action_hat": output.action_hat,
            "model_version": output.model_version,
            # tick 피처
            "buy_volume_ratio": buy_vol_ratio,
            # binance 피처
            "funding_rate": funding_rate,
            "long_short_ratio": long_short_ratio,
            "open_interest": open_interest,
            # macro 피처
            "dxy_index": dxy_index,
            "fear_greed_index": fear_greed_index,
        })

    print(f"  시뮬레이션 완료: {len(rows):,} 예측 행 생성")
    return pd.DataFrame(rows)


# ── 6. 라벨 생성 (export_dataset.py와 동일 방식) ──────────────────────────────

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

    merged = pd.merge_asof(
        features_sorted,
        prices_sorted.rename(columns={"ts": "label_ts", "mid": "future_mid"}),
        left_on="t0_plus_h",
        right_on="label_ts",
        direction="forward",
    )

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
    parser.add_argument("--backup-dir", type=str,
                        default="data/backups",
                        help="로컬 백업 Parquet 루트 디렉터리 (기본: data/backups)")
    args = parser.parse_args()

    s = load_settings()
    horizon_sec = args.horizon or s.H_SEC
    max_label_lag_sec = int(horizon_sec * args.max_label_lag_mult)

    sep = "=" * 60
    print(sep)
    print("  Historical Dataset Builder (4대 원천 데이터 하이브리드)")
    print(f"  hours               = {args.hours}")
    print(f"  backup_dir          = {args.backup_dir}")
    print(f"  horizon_sec         = {horizon_sec}")
    print(f"  predict_interval    = {args.predict_interval}s")
    print(f"  max_label_lag_sec   = {max_label_lag_sec}")
    print(f"  output              = {args.output}")
    print(sep)

    if not s.GCP_DB_URL:
        print("  ERROR: GCP_DB_URL 환경변수가 설정되지 않았습니다.")
        return 1

    engine_gcp = create_engine(s.GCP_DB_URL)

    now_utc = datetime.now(tz=timezone.utc)
    t_min = now_utc - timedelta(hours=args.hours)
    t_max = now_utc
    print(f"  query range: {t_min.strftime('%Y-%m-%d %H:%M')} ~ {t_max.strftime('%Y-%m-%d %H:%M')} UTC")

    # ── Step 1: 4대 원천 데이터 하이브리드 로드 ───────────────────────────────
    print("\n[1a] upbit_orderbook 로드 중...")
    raw_ob = load_orderbook(engine_gcp, t_min, t_max, backup_dir=args.backup_dir)
    if raw_ob.empty:
        print("  ERROR: orderbook 데이터를 가져오지 못했습니다.")
        return 1
    print(f"  orderbook: {len(raw_ob):,} rows  ({raw_ob['ts'].min()} ~ {raw_ob['ts'].max()})")

    print("\n[1b] upbit_tick 로드 중...")
    raw_tick = load_tick(engine_gcp, t_min, t_max, backup_dir=args.backup_dir)
    if raw_tick.empty:
        print("  WARN: tick 데이터 없음 — buy_volume_ratio=0.0으로 대체")
    else:
        print(f"  tick: {len(raw_tick):,} rows")

    print("\n[1c] Binance 파생 데이터 로드 중...")
    raw_binance = load_binance(engine_gcp, t_min, t_max, backup_dir=args.backup_dir)
    if raw_binance.empty:
        print("  WARN: Binance 데이터 없음 — funding_rate/long_short_ratio/open_interest=0.0으로 대체")
    else:
        print(f"  binance: {len(raw_binance):,} rows")

    print("\n[1d] Macro 데이터 로드 중...")
    raw_macro = load_macro(engine_gcp, t_min, t_max, backup_dir=args.backup_dir)
    if raw_macro.empty:
        print("  WARN: Macro 데이터 없음 — dxy_index/fear_greed_index=0.0으로 대체")
    else:
        print(f"  macro: {len(raw_macro):,} rows")

    # ── Step 2: 1초 리샘플링 + 4대 소스 병합 ──────────────────────────────────
    print("\n[2] 1초 리샘플링 및 병합 중...")
    candles = resample_1s(raw_ob, raw_tick=raw_tick,
                          raw_binance=raw_binance, raw_macro=raw_macro)
    del raw_ob, raw_tick, raw_binance, raw_macro
    print(f"  1s candles: {len(candles):,} rows  columns: {list(candles.columns)}")

    # ── Step 3: 시뮬레이션 루프 ───────────────────────────────────────────────
    print("\n[3] 시뮬레이션 루프 (feature 계산)...")
    features = run_simulation(candles, s, args.predict_interval)
    if features.empty:
        print("  ERROR: 시뮬레이션 결과가 없습니다. 데이터 범위를 확인하세요.")
        return 1

    # ── Step 4: 라벨 생성 ─────────────────────────────────────────────────────
    print("\n[4] 라벨 생성 (merge_asof direction=forward)...")
    prices = candles[["ts", "mid"]].copy()
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
    print(f"  Columns ({len(dataset.columns)}): {list(dataset.columns)}")

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
