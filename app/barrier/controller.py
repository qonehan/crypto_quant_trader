from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.writer import get_or_create_barrier_params, upsert_barrier_state

log = logging.getLogger(__name__)

_FETCH_MID_SQL = text("""
SELECT ts, mid_close_1s, mid
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

    def _get_params(self) -> dict:
        """Load or create barrier_params for this symbol."""
        defaults = {
            "k_vol_eff": self.settings.K_VOL,
            "none_ewma": self.settings.TARGET_NONE,
            "target_none": self.settings.TARGET_NONE,
            "ewma_alpha": self.settings.EWMA_ALPHA,
            "ewma_eta": self.settings.EWMA_ETA,
        }
        return get_or_create_barrier_params(self.engine, self.settings.SYMBOL, defaults)

    def compute_sigma_from_db(self, symbol: str, now_utc: datetime) -> dict:
        dt = self.settings.VOL_DT_SEC
        since = now_utc.replace(microsecond=0) - timedelta(seconds=self.settings.VOL_WINDOW_SEC)

        with self.engine.connect() as conn:
            rows = conn.execute(
                _FETCH_MID_SQL, {"symbol": symbol, "since": since}
            ).fetchall()

        # Use mid_close_1s if available, otherwise mid
        mids = []
        for r in rows:
            v = r.mid_close_1s if r.mid_close_1s is not None else r.mid
            if v is not None and v > 0:
                mids.append(v)

        if len(mids) < 2:
            return {"sigma_1s": None, "sigma_dt": None, "sample_n": 0}

        # Downsample by dt
        use = mids[::dt] if dt > 1 else mids
        if len(use) < 2:
            return {"sigma_1s": None, "sigma_dt": None, "sample_n": 0}

        arr = np.array(use, dtype=np.float64)
        log_returns = np.diff(np.log(arr))
        log_returns = log_returns[np.isfinite(log_returns)]

        if len(log_returns) == 0:
            return {"sigma_1s": None, "sigma_dt": None, "sample_n": 0}

        sigma_dt = float(np.std(log_returns, ddof=1))
        sigma_1s = sigma_dt / math.sqrt(dt) if dt > 0 else sigma_dt

        return {"sigma_1s": sigma_1s, "sigma_dt": sigma_dt, "sample_n": len(log_returns)}

    def compute_r_t(self, sigma_1s: float | None, k_vol_eff: float) -> tuple[float | None, float]:
        if sigma_1s is None:
            return None, self.settings.R_MIN

        sigma_h = sigma_1s * math.sqrt(self.settings.H_SEC)
        r_t = max(self.settings.R_MIN, k_vol_eff * sigma_h)
        r_t = min(r_t, self.settings.R_MAX)
        return sigma_h, r_t

    def _warmup_threshold(self) -> int:
        dt = max(1, self.settings.VOL_DT_SEC)
        return max(30, int((self.settings.VOL_WINDOW_SEC / dt) * 0.3))

    def _build_row(
        self, ts: datetime, result: dict, sigma_h: float | None,
        r_t: float, status: str, error: str | None, params: dict
    ) -> dict:
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
            # v1 feedback fields
            "k_vol_eff": params.get("k_vol_eff", self.settings.K_VOL),
            "none_ewma": params.get("none_ewma", self.settings.TARGET_NONE),
            "target_none": self.settings.TARGET_NONE,
            "ewma_alpha": self.settings.EWMA_ALPHA,
            "ewma_eta": self.settings.EWMA_ETA,
            "vol_dt_sec": self.settings.VOL_DT_SEC,
        }

    async def run(self) -> None:
        interval = self.settings.DECISION_INTERVAL_SEC
        now = time.time()
        next_ts = (int(now) // interval + 1) * interval
        await asyncio.sleep(next_ts - now)

        while True:
            ts_utc = datetime.fromtimestamp(next_ts, tz=timezone.utc).replace(microsecond=0)
            try:
                params = await asyncio.to_thread(self._get_params)
                k_vol_eff = params["k_vol_eff"]

                result = await asyncio.to_thread(
                    self.compute_sigma_from_db, self.settings.SYMBOL, ts_utc
                )
                sigma_h, r_t = self.compute_r_t(result.get("sigma_1s"), k_vol_eff)
                sample_n = result.get("sample_n", 0)

                if sample_n < self._warmup_threshold():
                    status = "WARMUP"
                    r_t = self.settings.R_MIN
                else:
                    status = "OK"

                row = self._build_row(ts_utc, result, sigma_h, r_t, status, None, params)
                await asyncio.to_thread(upsert_barrier_state, self.engine, row)

                self.last_r_t = r_t
                self.last_status = status

                log.info(
                    "Barrier: r_t=%.6f sigma_1s=%s sigma_h=%s status=%s sample_n=%d k_eff=%.4f",
                    r_t,
                    f"{result['sigma_1s']:.8f}" if result.get("sigma_1s") is not None else "N/A",
                    f"{sigma_h:.8f}" if sigma_h is not None else "N/A",
                    status,
                    sample_n,
                    k_vol_eff,
                )
            except Exception:
                log.exception("Barrier controller error at ts=%s", ts_utc)
                try:
                    params = {"k_vol_eff": self.settings.K_VOL, "none_ewma": self.settings.TARGET_NONE}
                    error_row = self._build_row(
                        ts_utc,
                        {"sigma_1s": None, "sample_n": 0},
                        None,
                        self.settings.R_MIN,
                        "ERROR",
                        str(__import__("traceback").format_exc()[-500:]),
                        params,
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
