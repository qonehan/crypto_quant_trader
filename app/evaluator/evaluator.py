from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.writer import (
    get_or_create_barrier_params,
    update_barrier_params,
    upsert_evaluation_result,
)

log = logging.getLogger(__name__)

_EPS = 1e-12

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

# Entry row: most recent bar-end <= t0
_FETCH_ENTRY_ROW_SQL = text("""
SELECT ts, bid_close_1s, ask_close_1s, bid, ask
FROM market_1s
WHERE symbol = :symbol AND ts <= :t0
ORDER BY ts DESC
LIMIT 1
""")

# Horizon rows for touch detection: ts > t0 AND ts <= t0+H (bar-end semantics)
_FETCH_HORIZON_ROWS_SQL = text("""
SELECT ts, bid_high_1s, bid_low_1s, bid_close_1s, bid
FROM market_1s
WHERE symbol = :symbol
  AND ts > :t0
  AND ts <= :t_end
ORDER BY ts ASC
""")

# Horizon-end row for r_h (NONE exit price)
_FETCH_HORIZON_END_SQL = text("""
SELECT ts, bid_close_1s, bid
FROM market_1s
WHERE symbol = :symbol AND ts <= :t_end
ORDER BY ts DESC
LIMIT 1
""")

# Mark prediction as SETTLED
_SETTLE_PREDICTION_SQL = text("""
UPDATE predictions SET status = 'SETTLED' WHERE symbol = :symbol AND t0 = :t0
""")


def compute_calibration(rows, class_name: str, bins: int = 10) -> list[dict]:
    """Compute one-vs-rest calibration for a given class (UP/DOWN/NONE)."""
    p_key = {"UP": "p_up", "DOWN": "p_down", "NONE": "p_none"}[class_name]
    result = []
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        subset = []
        for r in rows:
            p = getattr(r, p_key, None) if hasattr(r, p_key) else r.get(p_key)
            if p is None:
                continue
            if lo <= p < hi or (i == bins - 1 and p == hi):
                actual_dir = getattr(r, "actual_direction", None) if hasattr(r, "actual_direction") else r.get("actual_direction")
                y = 1.0 if actual_dir == class_name else 0.0
                subset.append((p, y))
        if not subset:
            result.append({"bin": f"{lo:.1f}-{hi:.1f}", "count": 0, "avg_p": 0.0, "actual_rate": 0.0, "abs_gap": 0.0})
            continue
        avg_p = sum(s[0] for s in subset) / len(subset)
        actual_rate = sum(s[1] for s in subset) / len(subset)
        result.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "count": len(subset),
            "avg_p": round(avg_p, 4),
            "actual_rate": round(actual_rate, 4),
            "abs_gap": round(abs(avg_p - actual_rate), 4),
        })
    return result


class Evaluator:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self._slip_rate = settings.SLIPPAGE_BPS / 10000.0

    def _evaluate_one(self, pred: dict, now_utc: datetime) -> dict | None:
        """Evaluate a single prediction using exec_v1 label logic."""
        symbol = pred["symbol"]
        t0 = pred["t0"]
        h_sec = pred["h_sec"]
        r_t = pred["r_t"]
        t_end = t0 + timedelta(seconds=h_sec)

        # (1) Entry row
        with self.engine.connect() as conn:
            row0 = conn.execute(
                _FETCH_ENTRY_ROW_SQL, {"symbol": symbol, "t0": t0}
            ).fetchone()

        if row0 is None:
            log.warning("exec_v1: no entry row for t0=%s, skipping", t0)
            return None

        # Skip if entry row too stale (>5s before t0)
        if (t0 - row0.ts).total_seconds() > 5.0:
            log.warning("exec_v1: entry row ts=%s too far from t0=%s, skipping", row0.ts, t0)
            return None

        # (2) Entry price (ask + slippage)
        ask0 = row0.ask_close_1s if row0.ask_close_1s is not None else row0.ask
        if ask0 is None or ask0 <= 0:
            log.warning("exec_v1: no valid ask for t0=%s, skipping", t0)
            return None
        entry_price = ask0 * (1 + self._slip_rate)

        # (3) Barriers
        u_exec = entry_price * (1 + r_t)
        d_exec = entry_price * (1 - r_t)

        # (4) Touch detection (ts > t0, ts <= t_end)
        with self.engine.connect() as conn:
            rows = conn.execute(
                _FETCH_HORIZON_ROWS_SQL,
                {"symbol": symbol, "t0": t0, "t_end": t_end},
            ).fetchall()

        if not rows:
            log.warning("exec_v1: no horizon rows for t0=%s, skipping", t0)
            return None

        actual_direction = "NONE"
        touch_time_sec = None
        ambig_touch = False
        actual_r_t = 0.0

        for row in rows:
            bh = row.bid_high_1s
            bl = row.bid_low_1s
            if bh is None or bl is None:
                continue

            exec_bid_high = bh * (1 - self._slip_rate)
            exec_bid_low = bl * (1 - self._slip_rate)

            up_hit = exec_bid_high >= u_exec
            dn_hit = exec_bid_low <= d_exec

            if up_hit and dn_hit:
                # Ambiguous: both barriers touched in same 1s bar → DOWN priority
                ambig_touch = True
                actual_direction = "DOWN"
                actual_r_t = r_t
                touch_time_sec = max(0.0, (row.ts - t0).total_seconds() - 0.5)
                break
            elif dn_hit:
                actual_direction = "DOWN"
                actual_r_t = r_t
                touch_time_sec = max(0.0, (row.ts - t0).total_seconds() - 0.5)
                break
            elif up_hit:
                actual_direction = "UP"
                actual_r_t = r_t
                touch_time_sec = max(0.0, (row.ts - t0).total_seconds() - 0.5)
                break

        # (6) NONE: compute r_h
        r_h = None
        if actual_direction == "NONE":
            with self.engine.connect() as conn:
                rowH = conn.execute(
                    _FETCH_HORIZON_END_SQL, {"symbol": symbol, "t_end": t_end}
                ).fetchone()
            if rowH is not None:
                exit_bid = rowH.bid_close_1s if rowH.bid_close_1s is not None else rowH.bid
                if exit_bid is not None and exit_bid > 0:
                    exit_exec = exit_bid * (1 - self._slip_rate)
                    r_h = (exit_exec - entry_price) / entry_price
                    actual_r_t = abs(r_h)

        # (8) Brier score & logloss
        p_up = pred["p_up"]
        p_down = pred["p_down"]
        p_none = pred["p_none"]

        if actual_direction == "UP":
            y = (1, 0, 0)
        elif actual_direction == "DOWN":
            y = (0, 1, 0)
        else:
            y = (0, 0, 1)

        brier = (p_up - y[0]) ** 2 + (p_down - y[1]) ** 2 + (p_none - y[2]) ** 2

        p_actual = (p_up, p_down, p_none)[("UP", "DOWN", "NONE").index(actual_direction)]
        logloss = -math.log(max(p_actual, _EPS))

        return {
            "ts": now_utc,
            "symbol": symbol,
            "t0": t0,
            "r_t": r_t,
            "p_up": p_up,
            "p_down": p_down,
            "p_none": p_none,
            "ev": pred["ev"],
            "slope_pred": pred["slope_pred"],
            "direction_hat": pred["direction_hat"],
            "actual_direction": actual_direction,
            "actual_r_t": actual_r_t,
            "touch_time_sec": touch_time_sec,
            "status": "COMPLETED",
            "error": None,
            # exec_v1 fields
            "label_version": "exec_v1",
            "entry_price": entry_price,
            "u_exec": u_exec,
            "d_exec": d_exec,
            "ambig_touch": ambig_touch,
            "r_h": r_h,
            "brier": brier,
            "logloss": logloss,
        }

    def _run_batch(self, now_utc: datetime) -> tuple[int, list[dict]]:
        """Evaluate all eligible pending predictions. Returns (count, settled_results)."""
        with self.engine.connect() as conn:
            pending = conn.execute(_FETCH_PENDING_SQL, {"now": now_utc}).fetchall()

        if not pending:
            return 0, []

        settled = 0
        settled_results: list[dict] = []
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
            settled_results.append(result)

        return settled, settled_results

    def _update_ewma_feedback(self, settled_results: list[dict]) -> None:
        """Update EWMA none_rate feedback in barrier_params."""
        if not settled_results:
            return

        symbol = self.settings.SYMBOL
        alpha = self.settings.EWMA_ALPHA
        eta = self.settings.EWMA_ETA

        defaults = {
            "k_vol_eff": self.settings.K_VOL,
            "none_ewma": self.settings.TARGET_NONE,
            "target_none": self.settings.TARGET_NONE,
            "ewma_alpha": alpha,
            "ewma_eta": eta,
        }
        params = get_or_create_barrier_params(self.engine, symbol, defaults)
        none_ewma = params["none_ewma"]
        k_vol_eff = params["k_vol_eff"]

        # Process in t0 ascending order
        sorted_results = sorted(settled_results, key=lambda r: r["t0"])
        last_t0 = None
        for res in sorted_results:
            none_flag = 1.0 if res["actual_direction"] == "NONE" else 0.0
            none_ewma = alpha * none_ewma + (1 - alpha) * none_flag
            k_vol_eff = k_vol_eff * math.exp(-eta * (none_ewma - self.settings.TARGET_NONE))
            k_vol_eff = max(self.settings.K_VOL_MIN, min(self.settings.K_VOL_MAX, k_vol_eff))
            last_t0 = res["t0"]

        update_barrier_params(self.engine, symbol, k_vol_eff, none_ewma, last_t0)

        log.info(
            "BarrierFeedback: n_new=%d none_ewma=%.4f k_vol_eff=%.4f "
            "(target=%.2f alpha=%.2f eta=%.2f)",
            len(sorted_results), none_ewma, k_vol_eff,
            self.settings.TARGET_NONE, alpha, eta,
        )

    def _compute_aggregate_metrics(self) -> dict | None:
        """Compute aggregate metrics from recent exec_v1 evaluations."""
        window_n = getattr(self.settings, "EVAL_WINDOW_N", 500)
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT direction_hat, actual_direction, touch_time_sec,
                           p_up, p_down, p_none,
                           brier, logloss, ambig_touch
                    FROM evaluation_results
                    WHERE label_version = 'exec_v1'
                      AND brier IS NOT NULL AND logloss IS NOT NULL
                    ORDER BY t0 DESC LIMIT :n
                """),
                {"n": window_n},
            ).fetchall()

        if not rows:
            return None

        total = len(rows)
        correct = sum(1 for r in rows if r.direction_hat == r.actual_direction)
        hits = sum(1 for r in rows if r.touch_time_sec is not None)
        ambig = sum(1 for r in rows if r.ambig_touch is True)
        none_count = sum(1 for r in rows if r.actual_direction == "NONE")
        up_count = sum(1 for r in rows if r.actual_direction == "UP")
        down_count = sum(1 for r in rows if r.actual_direction == "DOWN")
        briers = [r.brier for r in rows if r.brier is not None]
        loglosses = [r.logloss for r in rows if r.logloss is not None]
        avg_brier = sum(briers) / len(briers) if briers else 0.0
        avg_logloss = sum(loglosses) / len(loglosses) if loglosses else 0.0

        # Calibration ECE per class
        calib_ece = {}
        for cls in ("UP", "DOWN", "NONE"):
            calib = compute_calibration(rows, cls)
            weighted_gap = 0.0
            total_count = 0
            for b in calib:
                weighted_gap += b["abs_gap"] * b["count"]
                total_count += b["count"]
            calib_ece[cls] = weighted_gap / total_count if total_count > 0 else 0.0

        return {
            "total": total,
            "accuracy": correct / total,
            "hit_rate": hits / total,
            "none_rate": none_count / total,
            "up_rate": up_count / total,
            "down_rate": down_count / total,
            "ambig_count": ambig,
            "avg_brier": avg_brier,
            "avg_logloss": avg_logloss,
            "calib_ece": calib_ece,
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
                settled, settled_results = await asyncio.to_thread(self._run_batch, now_utc)
                if settled > 0:
                    await asyncio.to_thread(self._update_ewma_feedback, settled_results)
                    metrics = await asyncio.to_thread(self._compute_aggregate_metrics)
                    if metrics:
                        log.info(
                            "EvalMetrics(exec_v1): N=%d acc=%.3f hit=%.3f "
                            "none=%.3f brier=%.4f logloss=%.4f",
                            metrics["total"],
                            metrics["accuracy"],
                            metrics["hit_rate"],
                            metrics["none_rate"],
                            metrics["avg_brier"],
                            metrics["avg_logloss"],
                        )
                        ece = metrics.get("calib_ece", {})
                        log.info(
                            "CalibECE: UP=%.4f DOWN=%.4f NONE=%.4f",
                            ece.get("UP", 0), ece.get("DOWN", 0), ece.get("NONE", 0),
                        )
                    else:
                        log.info("Eval(exec_v1): settled=%d predictions", settled)
            except Exception:
                log.exception("Evaluator error")

            await asyncio.sleep(interval)
