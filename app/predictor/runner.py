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
from app.models.interface import BaseModel

log = logging.getLogger(__name__)

_FETCH_BARRIER_SQL = text("""
SELECT ts, symbol, h_sec, r_t, sigma_1s, sigma_h, status
FROM barrier_state
WHERE symbol = :symbol AND ts <= :t0
ORDER BY ts DESC LIMIT 1
""")

_FETCH_MARKET_WINDOW_SQL = text("""
SELECT ts, mid, spread, imbalance_top5
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
        }

        upsert_prediction(self.engine, row)

        log.info(
            "Pred: t0=%s r_t=%.6f p_up=%.4f p_down=%.4f p_none=%.4f "
            "t_up=%.1f t_down=%.1f slope=%.8f ev=%.8f hat=%s",
            t0.strftime("%H:%M:%S"),
            row["r_t"],
            output.p_up,
            output.p_down,
            output.p_none,
            output.t_up if output.t_up is not None else 0.0,
            output.t_down if output.t_down is not None else 0.0,
            output.slope_pred,
            output.ev,
            output.direction_hat,
        )

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
