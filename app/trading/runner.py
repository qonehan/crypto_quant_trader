from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.writer import (
    get_or_create_paper_position,
    insert_paper_decision,
    insert_paper_trade,
    update_paper_position,
)
from app.marketdata.state import MarketState
from app.trading.paper import execute_enter_long, execute_exit_long
from app.trading.policy import decide_action, _compute_cost_est

log = logging.getLogger(__name__)

_FETCH_LATEST_PRED = text("""
SELECT t0, symbol, h_sec, r_t, p_up, p_down, p_none,
       ev, ev_rate, z_barrier, spread_bps, action_hat, model_version
FROM predictions
WHERE symbol = :sym
ORDER BY t0 DESC LIMIT 1
""")


class PaperTradingRunner:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        market_state: MarketState,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.market_state = market_state

    def _get_market_snapshot(self, now_utc: datetime) -> dict:
        ms = self.market_state
        best_bid = ms.best_bid
        best_ask = ms.best_ask

        if best_bid and best_ask and best_ask > 0:
            mid = (best_bid + best_ask) / 2
            spread_bps = 10000 * (best_ask - best_bid) / mid if mid > 0 else 999
        else:
            spread_bps = 999

        lag_sec = time.time() - ms.last_update_ts if ms.last_update_ts > 0 else 999

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_bps": spread_bps,
            "lag_sec": lag_sec,
        }

    def _fetch_latest_pred(self) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                _FETCH_LATEST_PRED, {"sym": self.settings.SYMBOL}
            ).fetchone()
        if row is None:
            return None
        return row._asdict()

    def _run_tick(self, now_utc: datetime) -> None:
        symbol = self.settings.SYMBOL
        pos = get_or_create_paper_position(
            self.engine, symbol, self.settings.PAPER_INITIAL_KRW
        )
        pred = self._fetch_latest_pred()
        snapshot = self._get_market_snapshot(now_utc)

        action, reason = decide_action(now_utc, pos, pred, snapshot, self.settings)

        # Build cost estimate for decision log
        cost_est = _compute_cost_est(snapshot.get("spread_bps", 0), self.settings)

        decision = {
            "ts": now_utc,
            "symbol": symbol,
            "pos_status": pos["status"],
            "action": action,
            "reason": reason,
            "ev_rate": pred.get("ev_rate") if pred else None,
            "ev": pred.get("ev") if pred else None,
            "p_up": pred.get("p_up") if pred else None,
            "p_down": pred.get("p_down") if pred else None,
            "p_none": pred.get("p_none") if pred else None,
            "r_t": pred.get("r_t") if pred else None,
            "z_barrier": pred.get("z_barrier") if pred else None,
            "spread_bps": snapshot.get("spread_bps"),
            "lag_sec": snapshot.get("lag_sec"),
            "cost_roundtrip_est": cost_est,
            "model_version": pred.get("model_version") if pred else None,
            "pred_t0": pred.get("t0") if pred else None,
        }
        insert_paper_decision(self.engine, decision)

        # Execute if actionable
        if action == "ENTER_LONG":
            result = execute_enter_long(pos, pred, snapshot, self.settings, now_utc)
            if result is not None:
                new_pos, trade = result
                update_paper_position(self.engine, new_pos)
                insert_paper_trade(self.engine, trade)
                log.info(
                    "Paper ENTER: price=%.0f qty=%.8f fee=%.2f cash=%.0f",
                    trade["price"], trade["qty"], trade["fee_krw"], trade["cash_after"],
                )
            else:
                log.warning("Paper ENTER skipped: invest_krw too small")

        elif action == "EXIT_LONG":
            new_pos, trade = execute_exit_long(
                pos, snapshot, self.settings, now_utc, reason
            )
            update_paper_position(self.engine, new_pos)
            insert_paper_trade(self.engine, trade)
            log.info(
                "Paper EXIT(%s): price=%.0f pnl=%.2f pnl_rate=%.4f%% hold=%.0fs cash=%.0f",
                reason, trade["price"],
                trade["pnl_krw"] or 0, (trade["pnl_rate"] or 0) * 100,
                trade["hold_sec"] or 0, trade["cash_after"],
            )

        # Equity estimate
        slip_rate = self.settings.SLIPPAGE_BPS / 10000.0
        if pos["status"] == "LONG" and action not in ("EXIT_LONG",):
            bid = snapshot.get("best_bid") or 0
            equity = pos["cash_krw"] + pos["qty"] * bid * (1 - slip_rate)
        else:
            equity = pos["cash_krw"]

        log.info(
            "Paper: pos=%s action=%s reason=%s cash=%.0f qty=%.8f equity_est=%.0f",
            pos["status"], action, reason, pos["cash_krw"], pos["qty"], equity,
        )

    async def run(self) -> None:
        interval = self.settings.DECISION_INTERVAL_SEC
        # Wait a bit for initial data
        await asyncio.sleep(interval + 1)

        while True:
            now_utc = datetime.now(timezone.utc).replace(microsecond=0)
            try:
                await asyncio.to_thread(self._run_tick, now_utc)
            except Exception:
                log.exception("PaperTradingRunner error")

            await asyncio.sleep(interval)
