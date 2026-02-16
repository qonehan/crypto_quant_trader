from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.writer import upsert_barrier_state

log = logging.getLogger(__name__)

_FETCH_MID_SQL = text("""
SELECT ts, mid
FROM market_1s
WHERE symbol = :symbol AND ts >= :since
ORDER BY ts ASC
""")


class BarrierController:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.last_r_t: float | None = None
        self.last_status: str | None = None

    def compute_sigma_from_db(self, symbol: str, now_utc: datetime) -> dict:
        since = now_utc.replace(microsecond=0) - __import__("datetime").timedelta(
            seconds=self.settings.VOL_WINDOW_SEC
        )
        with self.engine.connect() as conn:
            rows = conn.execute(
                _FETCH_MID_SQL, {"symbol": symbol, "since": since}
            ).fetchall()

        mids = [r.mid for r in rows if r.mid is not None and r.mid > 0]

        if len(mids) < 2:
            return {"sigma_1s": None, "sample_n": 0}

        arr = np.array(mids, dtype=np.float64)
        log_returns = np.diff(np.log(arr))
        # remove any nan/inf
        log_returns = log_returns[np.isfinite(log_returns)]

        if len(log_returns) == 0:
            return {"sigma_1s": None, "sample_n": 0}

        sigma_1s = float(np.std(log_returns, ddof=1))
        return {"sigma_1s": sigma_1s, "sample_n": len(log_returns)}

    def compute_r_t(self, sigma_1s: float | None) -> tuple[float | None, float]:
        if sigma_1s is None:
            return None, self.settings.R_MIN

        sigma_h = sigma_1s * math.sqrt(self.settings.H_SEC)
        r_t = max(self.settings.R_MIN, self.settings.K_VOL * sigma_h)
        if self.settings.R_MAX is not None:
            r_t = min(r_t, self.settings.R_MAX)
        return sigma_h, r_t

    def _build_row(self, ts: datetime, result: dict, sigma_h: float | None, r_t: float, status: str, error: str | None) -> dict:
        return {
            "ts": ts,
            "symbol": self.settings.SYMBOL,
            "h_sec": self.settings.H_SEC,
            "vol_window_sec": self.settings.VOL_WINDOW_SEC,
            "sigma_1s": result.get("sigma_1s"),
            "sigma_h": sigma_h,
            "r_min": self.settings.R_MIN,
            "k_vol": self.settings.K_VOL,
            "r_t": r_t,
            "sample_n": result.get("sample_n", 0),
            "status": status,
            "error": error,
        }

    def _warmup_threshold(self) -> int:
        return max(30, int(self.settings.VOL_WINDOW_SEC * 0.3))

    async def run(self) -> None:
        interval = self.settings.DECISION_INTERVAL_SEC
        # align to next decision boundary
        now = time.time()
        next_ts = (int(now) // interval + 1) * interval
        await asyncio.sleep(next_ts - now)

        while True:
            ts_utc = datetime.fromtimestamp(next_ts, tz=timezone.utc).replace(microsecond=0)
            try:
                result = await asyncio.to_thread(
                    self.compute_sigma_from_db, self.settings.SYMBOL, ts_utc
                )
                sigma_h, r_t = self.compute_r_t(result.get("sigma_1s"))
                sample_n = result.get("sample_n", 0)

                if sample_n < self._warmup_threshold():
                    status = "WARMUP"
                    r_t = self.settings.R_MIN
                    sigma_h = sigma_h  # keep computed value even during warmup
                else:
                    status = "OK"

                row = self._build_row(ts_utc, result, sigma_h, r_t, status, None)
                await asyncio.to_thread(upsert_barrier_state, self.engine, row)

                self.last_r_t = r_t
                self.last_status = status

                log.info(
                    "Barrier: r_t=%.6f sigma_1s=%s sigma_h=%s status=%s sample_n=%d",
                    r_t,
                    f"{result['sigma_1s']:.8f}" if result.get("sigma_1s") is not None else "N/A",
                    f"{sigma_h:.8f}" if sigma_h is not None else "N/A",
                    status,
                    sample_n,
                )
            except Exception:
                log.exception("Barrier controller error at ts=%s", ts_utc)
                try:
                    error_row = self._build_row(
                        ts_utc,
                        {"sigma_1s": None, "sample_n": 0},
                        None,
                        self.settings.R_MIN,
                        "ERROR",
                        str(__import__("traceback").format_exc()[-500:]),
                    )
                    await asyncio.to_thread(upsert_barrier_state, self.engine, error_row)
                except Exception:
                    log.exception("Failed to write error barrier_state row")

                self.last_r_t = self.settings.R_MIN
                self.last_status = "ERROR"

            next_ts += interval
            sleep_dur = next_ts - time.time()
            if sleep_dur > 0:
                await asyncio.sleep(sleep_dur)
