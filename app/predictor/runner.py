from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.writer import upsert_prediction
from app.features.writer import upsert_feature_snapshot
from app.models.interface import BaseModel

log = logging.getLogger(__name__)

_FETCH_BARRIER_SQL = text("""
SELECT ts, symbol, h_sec, r_t, sigma_1s, sigma_h, status,
       k_vol_eff, r_min_eff, cost_roundtrip_est
FROM barrier_state
WHERE symbol = :symbol AND ts <= :t0
ORDER BY ts DESC LIMIT 1
""")

_FETCH_MARKET_WINDOW_SQL = text("""
SELECT ts, mid, mid_close_1s, spread, spread_bps, imbalance_top5, imb_notional_top5
FROM market_1s
WHERE symbol = :symbol AND ts <= :t0 AND ts >= :since
ORDER BY ts ASC
""")


class PredictionRunner:
    def __init__(self, settings: Settings, engine: Engine, model: BaseModel) -> None:
        self.settings = settings
        self.engine = engine
        self.model = model

    def fetch_latest_barrier(self, symbol: str, t0: datetime) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(_FETCH_BARRIER_SQL, {"symbol": symbol, "t0": t0}).fetchone()
        if row is None:
            return None
        return row._asdict()

    def fetch_market_window(self, symbol: str, t0: datetime) -> list[dict]:
        since = t0 - __import__("datetime").timedelta(seconds=self.settings.MODEL_LOOKBACK_SEC)
        with self.engine.connect() as conn:
            rows = conn.execute(
                _FETCH_MARKET_WINDOW_SQL, {"symbol": symbol, "t0": t0, "since": since}
            ).fetchall()
        return [r._asdict() for r in rows]

    def _run_tick(self, t0: datetime) -> None:
        symbol = self.settings.SYMBOL

        barrier_row = self.fetch_latest_barrier(symbol, t0)
        if barrier_row is None:
            log.warning("Pred: no barrier_state row found for t0=%s, skipping", t0)
            return

        market_window = self.fetch_market_window(symbol, t0)

        output = self.model.predict(
            market_window=market_window,
            barrier_row=barrier_row,
            settings=self.settings,
        )

        row = {
            "t0": t0,
            "symbol": symbol,
            "h_sec": barrier_row.get("h_sec", self.settings.H_SEC),
            "r_t": barrier_row.get("r_t", self.settings.R_MIN),
            "p_up": output.p_up,
            "p_down": output.p_down,
            "p_none": output.p_none,
            "t_up": output.t_up,
            "t_down": output.t_down,
            "slope_pred": output.slope_pred,
            "ev": output.ev,
            "direction_hat": output.direction_hat,
            "model_version": output.model_version,
            "status": "PENDING",
            "sigma_1s": barrier_row.get("sigma_1s"),
            "sigma_h": barrier_row.get("sigma_h"),
            "features": json.dumps(output.features),
            # v1 fields
            "z_barrier": output.z_barrier,
            "p_hit_base": output.p_hit_base,
            "ev_rate": output.ev_rate,
            "r_none_pred": output.r_none_pred,
            "t_up_cond_pred": output.t_up_cond_pred,
            "t_down_cond_pred": output.t_down_cond_pred,
            "spread_bps": output.spread_bps,
            "mom_z": output.mom_z,
            "imb_notional_top5": output.imb_notional_top5,
            "action_hat": output.action_hat,
        }

        upsert_prediction(self.engine, row)

        try:
            self._save_feature_snapshot(t0, barrier_row, output, market_window)
        except Exception:
            log.exception("feature_snapshot non-fatal error at t0=%s", t0)

        log.info(
            "Pred(v1): t0=%s r_t=%.6f z=%s p_none=%.4f p_up=%.4f p_down=%.4f "
            "ev=%.8f ev_rate=%s action=%s",
            t0.strftime("%H:%M:%S"),
            row["r_t"],
            f"{output.z_barrier:.3f}" if output.z_barrier is not None else "N/A",
            output.p_none,
            output.p_up,
            output.p_down,
            output.ev,
            f"{output.ev_rate:.8f}" if output.ev_rate is not None else "N/A",
            output.action_hat or "N/A",
        )

    def _fetch_binance_mark_near(self, symbol: str, t0: datetime) -> dict | None:
        """binance_mark_price_1s에서 t0 이하 3초 이내 최신 row (ts 포함)."""
        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT mark_price, index_price, funding_rate,
                               ts AS mark_ts
                        FROM binance_mark_price_1s
                        WHERE symbol = :sym
                          AND ts <= :t0
                          AND ts >= :t0 - interval '3 seconds'
                        ORDER BY ts DESC LIMIT 1
                    """),
                    {"sym": symbol, "t0": t0},
                ).fetchone()
            return row._asdict() if row else None
        except Exception:
            return None

    def _fetch_binance_metrics_near(
        self, symbol: str, t0: datetime, fresh_sec: int
    ) -> tuple[dict, dict]:
        """binance_futures_metrics에서 신선도 window 내 각 metric 최신값과 ts를 반환.
        Returns: (values_dict, ts_dict)  — ts_dict는 누수 검사용."""
        values: dict = {}
        ts_dict: dict = {}
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT DISTINCT ON (metric) metric, value, ts
                        FROM binance_futures_metrics
                        WHERE symbol = :sym
                          AND ts <= :t0
                          AND ts >= :t0 - :fresh * interval '1 second'
                        ORDER BY metric, ts DESC
                    """),
                    {"sym": symbol, "t0": t0, "fresh": fresh_sec},
                ).fetchall()
            for r in rows:
                values[r.metric] = r.value
                ts_dict[r.metric] = r.ts
        except Exception:
            pass
        return values, ts_dict

    def _fetch_liq_aggregate(self, symbol: str, t0: datetime) -> dict:
        """binance_force_orders에서 (t0-5min, t0] 구간 청산 합계 (USDT 기준).
        반환: {notional: float, count: int, liq_last_ts: datetime|None}"""
        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT
                            COALESCE(SUM(notional), 0.0) AS liq_5m_notional,
                            COALESCE(COUNT(*), 0)        AS liq_5m_count,
                            MAX(ts)                      AS liq_last_ts
                        FROM binance_force_orders
                        WHERE symbol = :sym
                          AND ts >  (:t0 - interval '5 minutes')
                          AND ts <= :t0
                    """),
                    {"sym": symbol, "t0": t0},
                ).fetchone()
            if row:
                return {
                    "notional": float(row.liq_5m_notional),
                    "count": int(row.liq_5m_count),
                    "liq_last_ts": row.liq_last_ts,
                }
            return {}
        except Exception:
            return {}

    def _save_feature_snapshot(self, t0: datetime, barrier_row: dict, output, market_window: list) -> None:
        """predictor tick 직후 feature_snapshots에 정렬 저장.
        모든 AltData는 ts <= t0 쿼리로 미래값 사용 금지."""
        s = self.settings
        sym_binance = s.ALT_SYMBOL_BINANCE
        fresh_sec = s.BINANCE_METRICS_FRESH_SEC

        # Upbit market — market_window의 가장 최근 행 (ts <= t0)
        latest_mkt = market_window[-1] if market_window else {}

        # Binance alt data — 모두 ts <= t0 보장
        mark_row = self._fetch_binance_mark_near(sym_binance, t0)
        metrics, metrics_ts = self._fetch_binance_metrics_near(sym_binance, t0, fresh_sec)
        liq_agg = self._fetch_liq_aggregate(sym_binance, t0)

        mp = mark_row.get("mark_price") if mark_row else None
        ip = mark_row.get("index_price") if mark_row else None
        basis = (mp - ip) if (mp is not None and ip is not None) else None
        mark_ts = mark_row.get("mark_ts") if mark_row else None

        snap: dict = {
            "ts": t0,
            "symbol": s.SYMBOL,
            # Upbit market
            "mid_krw": latest_mkt.get("mid"),
            "spread_bps": latest_mkt.get("spread_bps"),
            "imb_notional_top5": latest_mkt.get("imb_notional_top5"),
            # Barrier
            "r_t": barrier_row.get("r_t"),
            "r_min_eff": barrier_row.get("r_min_eff"),
            "cost_roundtrip_est": barrier_row.get("cost_roundtrip_est"),
            "sigma_1s": barrier_row.get("sigma_1s"),
            "sigma_h": barrier_row.get("sigma_h"),
            "k_vol_eff": barrier_row.get("k_vol_eff"),
            "barrier_status": barrier_row.get("status"),
            # Prediction
            "p_up": output.p_up,
            "p_down": output.p_down,
            "p_none": output.p_none,
            "ev": output.ev,
            "ev_rate": output.ev_rate,
            "action_hat": output.action_hat,
            "model_version": output.model_version,
            # Binance mark
            "bin_mark_price": mp,
            "bin_index_price": ip,
            "bin_funding_rate": mark_row.get("funding_rate") if mark_row else None,
            "bin_mark_index_basis": basis,
            # Binance metrics
            "oi_value": metrics.get("open_interest"),
            "global_ls_ratio": metrics.get("global_ls_ratio"),
            "taker_ls_ratio": metrics.get("taker_ls_ratio"),
            "basis_value": metrics.get("basis"),
            # Liquidation aggregates (USDT, t0-5min < ts <= t0)
            "liq_5m_notional": liq_agg.get("notional"),
            "liq_5m_count": liq_agg.get("count"),
            # Source timestamps for leak detection (모두 <= t0이어야 함)
            "bin_mark_ts": mark_ts,
            "oi_ts": metrics_ts.get("open_interest"),
            "liq_last_ts": liq_agg.get("liq_last_ts"),
        }
        upsert_feature_snapshot(self.engine, snap)

    async def run(self) -> None:
        interval = self.settings.DECISION_INTERVAL_SEC
        now = time.time()
        next_ts = (int(now) // interval + 1) * interval
        # offset slightly to let barrier controller write first
        await asyncio.sleep(next_ts - now + 0.5)

        while True:
            t0_utc = datetime.fromtimestamp(next_ts, tz=timezone.utc).replace(microsecond=0)
            try:
                await asyncio.to_thread(self._run_tick, t0_utc)
            except Exception:
                log.exception("PredictionRunner error at t0=%s", t0_utc)

            next_ts += interval
            sleep_dur = next_ts - time.time() + 0.5
            if sleep_dur > 0:
                await asyncio.sleep(sleep_dur)
