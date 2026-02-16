from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.writer import upsert_evaluation_result

log = logging.getLogger(__name__)

# Fetch PENDING predictions whose horizon has expired (t0 + h_sec <= now)
_FETCH_PENDING_SQL = text("""
SELECT t0, symbol, h_sec, r_t, p_up, p_down, p_none,
       ev, slope_pred, direction_hat
FROM predictions
WHERE status = 'PENDING'
  AND t0 + make_interval(secs => h_sec) <= :now
ORDER BY t0 ASC
LIMIT 50
""")

# Fetch market_1s mid prices in the horizon window [t0, t0+h_sec]
_FETCH_HORIZON_MIDS_SQL = text("""
SELECT ts, mid
FROM market_1s
WHERE symbol = :symbol
  AND ts >= :t0
  AND ts <= :t0_end
ORDER BY ts ASC
""")

# Mark prediction as SETTLED
_SETTLE_PREDICTION_SQL = text("""
UPDATE predictions SET status = 'SETTLED' WHERE symbol = :symbol AND t0 = :t0
""")


class Evaluator:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine

    def _evaluate_one(self, pred: dict, now_utc: datetime) -> dict | None:
        """Evaluate a single prediction against actual market data."""
        symbol = pred["symbol"]
        t0 = pred["t0"]
        h_sec = pred["h_sec"]
        r_t = pred["r_t"]

        t0_end = t0 + timedelta(seconds=h_sec)

        # Fetch mid prices in the horizon window
        with self.engine.connect() as conn:
            rows = conn.execute(
                _FETCH_HORIZON_MIDS_SQL,
                {"symbol": symbol, "t0": t0, "t0_end": t0_end},
            ).fetchall()

        if len(rows) < 2:
            return None  # not enough data yet

        mid_t0 = rows[0].mid
        if mid_t0 is None or mid_t0 <= 0:
            return None

        up_barrier = mid_t0 * (1.0 + r_t)
        down_barrier = mid_t0 * (1.0 - r_t)

        actual_direction = "NONE"
        actual_r_t = 0.0
        touch_time_sec = None

        for row in rows[1:]:
            if row.mid is None or row.mid <= 0:
                continue
            elapsed = (row.ts - t0).total_seconds()

            if row.mid >= up_barrier:
                actual_direction = "UP"
                actual_r_t = (row.mid - mid_t0) / mid_t0
                touch_time_sec = elapsed
                break
            elif row.mid <= down_barrier:
                actual_direction = "DOWN"
                actual_r_t = (mid_t0 - row.mid) / mid_t0
                touch_time_sec = elapsed
                break

        # If no barrier touched, compute final return
        if actual_direction == "NONE":
            last_mid = None
            for row in reversed(rows):
                if row.mid is not None and row.mid > 0:
                    last_mid = row.mid
                    break
            if last_mid is not None:
                actual_r_t = abs(last_mid - mid_t0) / mid_t0

        # Compute error: predicted direction probability minus actual outcome
        if actual_direction == "UP":
            error_val = pred["p_up"] - 1.0
        elif actual_direction == "DOWN":
            error_val = pred["p_down"] - 1.0
        else:
            error_val = pred["p_none"] - 1.0

        return {
            "ts": now_utc,
            "symbol": symbol,
            "t0": t0,
            "r_t": r_t,
            "p_up": pred["p_up"],
            "p_down": pred["p_down"],
            "p_none": pred["p_none"],
            "ev": pred["ev"],
            "slope_pred": pred["slope_pred"],
            "direction_hat": pred["direction_hat"],
            "actual_direction": actual_direction,
            "actual_r_t": actual_r_t,
            "touch_time_sec": touch_time_sec,
            "status": "COMPLETED",
            "error": f"{error_val:.6f}",
        }

    def _run_batch(self, now_utc: datetime) -> int:
        """Evaluate all eligible pending predictions. Returns count settled."""
        with self.engine.connect() as conn:
            pending = conn.execute(_FETCH_PENDING_SQL, {"now": now_utc}).fetchall()

        if not pending:
            return 0

        settled = 0
        for row in pending:
            pred = row._asdict()
            result = self._evaluate_one(pred, now_utc)
            if result is None:
                continue

            upsert_evaluation_result(self.engine, result)

            # Mark prediction as SETTLED
            with self.engine.begin() as conn:
                conn.execute(
                    _SETTLE_PREDICTION_SQL,
                    {"symbol": pred["symbol"], "t0": pred["t0"]},
                )

            settled += 1

        return settled

    def _compute_aggregate_metrics(self) -> dict | None:
        """Compute aggregate accuracy/hit_rate from recent evaluations."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT direction_hat, actual_direction, touch_time_sec, error
                    FROM evaluation_results
                    ORDER BY t0 DESC LIMIT 100
                """)
            ).fetchall()

        if not rows:
            return None

        total = len(rows)
        correct = sum(1 for r in rows if r.direction_hat == r.actual_direction)
        hits = sum(1 for r in rows if r.touch_time_sec is not None)
        errors = [float(r.error) for r in rows if r.error is not None]
        avg_error = sum(errors) / len(errors) if errors else 0.0

        return {
            "total": total,
            "accuracy": correct / total,
            "hit_rate": hits / total,
            "avg_error": avg_error,
        }

    async def run(self) -> None:
        interval = self.settings.DECISION_INTERVAL_SEC
        # Wait for at least one horizon to pass before first evaluation
        initial_wait = self.settings.H_SEC + 5
        log.info("Evaluator: waiting %ds for first horizon to expire...", initial_wait)
        await asyncio.sleep(initial_wait)

        while True:
            now_utc = datetime.now(timezone.utc).replace(microsecond=0)
            try:
                settled = await asyncio.to_thread(self._run_batch, now_utc)
                if settled > 0:
                    metrics = await asyncio.to_thread(self._compute_aggregate_metrics)
                    if metrics:
                        log.info(
                            "Eval: settled=%d total=%d accuracy=%.3f hit_rate=%.3f avg_error=%.6f",
                            settled,
                            metrics["total"],
                            metrics["accuracy"],
                            metrics["hit_rate"],
                            metrics["avg_error"],
                        )
                    else:
                        log.info("Eval: settled=%d predictions", settled)
            except Exception:
                log.exception("Evaluator error")

            await asyncio.sleep(interval)
