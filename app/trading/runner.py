from __future__ import annotations

import asyncio
import json
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

_COUNT_RECENT_ENTERS = text("""
SELECT count(*) FROM paper_trades
WHERE symbol = :sym AND action = 'ENTER_LONG'
  AND t >= :since
""")

_LAST_TRADE_TIME = text("""
SELECT t FROM paper_trades
WHERE symbol = :sym
ORDER BY t DESC LIMIT 1
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

    def _count_recent_enters(self, now_utc: datetime) -> int:
        since = now_utc - __import__("datetime").timedelta(hours=1)
        with self.engine.connect() as conn:
            row = conn.execute(
                _COUNT_RECENT_ENTERS, {"sym": self.settings.SYMBOL, "since": since}
            ).fetchone()
        return row[0] if row else 0

    def _last_trade_time(self) -> datetime | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                _LAST_TRADE_TIME, {"sym": self.settings.SYMBOL}
            ).fetchone()
        return row[0] if row else None

    def _compute_equity(self, pos: dict, snapshot: dict, action: str) -> float:
        slip_rate = self.settings.SLIPPAGE_BPS / 10000.0
        if pos["status"] == "LONG" and action != "EXIT_LONG":
            bid = snapshot.get("best_bid") or 0
            return pos["cash_krw"] + pos["qty"] * bid * (1 - slip_rate)
        return pos["cash_krw"]

    def _run_tick(self, now_utc: datetime) -> None:
        symbol = self.settings.SYMBOL
        profile = self.settings.PAPER_POLICY_PROFILE
        pos = get_or_create_paper_position(
            self.engine, symbol, self.settings.PAPER_INITIAL_KRW
        )
        pred = self._fetch_latest_pred()
        snapshot = self._get_market_snapshot(now_utc)

        # Rate limit / cooldown data for test mode
        recent_enter_count = 0
        last_trade_time = None
        if profile == "test":
            recent_enter_count = self._count_recent_enters(now_utc)
            last_trade_time = self._last_trade_time()

        action, reason, reason_flags, diag = decide_action(
            now_utc, pos, pred, snapshot, self.settings,
            recent_enter_count=recent_enter_count,
            last_trade_time=last_trade_time,
        )

        # Execute if actionable
        if action == "ENTER_LONG":
            result = execute_enter_long(pos, pred, snapshot, self.settings, now_utc)
            if result is not None:
                new_pos, trade = result
                self._save_pos_with_risk_fields(new_pos, pos)
                insert_paper_trade(self.engine, trade)
                log.info(
                    "PaperTrade ENTER: price=%.0f qty=%.8f fee=%.2f cash=%.0f u_exec=%.0f d_exec=%.0f h=%ds",
                    trade["price"], trade["qty"], trade["fee_krw"], trade["cash_after"],
                    new_pos.get("u_exec") or 0, new_pos.get("d_exec") or 0,
                    new_pos.get("h_sec") or 0,
                )
                # Re-read pos after update for equity calc
                pos = get_or_create_paper_position(
                    self.engine, symbol, self.settings.PAPER_INITIAL_KRW
                )
            else:
                log.warning("Paper ENTER skipped: invest_krw too small")

        elif action == "EXIT_LONG":
            new_pos, trade = execute_exit_long(
                pos, snapshot, self.settings, now_utc, reason
            )
            self._save_pos_with_risk_fields(new_pos, pos)
            insert_paper_trade(self.engine, trade)
            log.info(
                "PaperTrade EXIT(%s): price=%.0f qty=%.8f fee=%.2f pnl=%.2f pnl_rate=%.4f%% hold=%.0fs cash=%.0f",
                reason, trade["price"], trade["qty"], trade["fee_krw"],
                trade["pnl_krw"] or 0, (trade["pnl_rate"] or 0) * 100,
                trade["hold_sec"] or 0, trade["cash_after"],
            )
            # Re-read pos after update
            pos = get_or_create_paper_position(
                self.engine, symbol, self.settings.PAPER_INITIAL_KRW
            )

        # Equity tracking
        equity_est = self._compute_equity(pos, snapshot, action)

        # Update equity_high
        equity_high = pos.get("equity_high") or self.settings.PAPER_INITIAL_KRW
        equity_high = max(equity_high, equity_est)

        # Day change detection (UTC)
        today_utc = now_utc.date()
        day_start_date = pos.get("day_start_date")
        day_start_equity = pos.get("day_start_equity") or self.settings.PAPER_INITIAL_KRW
        if day_start_date is None or today_utc != day_start_date:
            day_start_date = today_utc
            day_start_equity = equity_est

        # Drawdown
        dd = (equity_est / (equity_high + 1e-12)) - 1.0

        # Risk HALT check
        halted = pos.get("halted") or False
        halt_reason = pos.get("halt_reason")
        halted_at = pos.get("halted_at")

        if not halted:
            if dd <= -self.settings.PAPER_MAX_DRAWDOWN_PCT:
                halted = True
                halt_reason = "MAX_DRAWDOWN"
                halted_at = now_utc
                log.warning("PaperRisk: HALTED — MAX_DRAWDOWN dd=%.4f%%", dd * 100)
            elif equity_est <= day_start_equity * (1 - self.settings.PAPER_DAILY_LOSS_LIMIT_PCT):
                halted = True
                halt_reason = "DAILY_LOSS_LIMIT"
                halted_at = now_utc
                log.warning("PaperRisk: HALTED — DAILY_LOSS_LIMIT equity=%.0f day_start=%.0f",
                            equity_est, day_start_equity)

        # Save risk fields to position (even if no trade happened)
        risk_update = {
            "symbol": symbol,
            "status": pos["status"],
            "cash_krw": pos["cash_krw"],
            "qty": pos["qty"],
            "entry_time": pos.get("entry_time"),
            "entry_price": pos.get("entry_price"),
            "entry_fee_krw": pos.get("entry_fee_krw"),
            "u_exec": pos.get("u_exec"),
            "d_exec": pos.get("d_exec"),
            "h_sec": pos.get("h_sec"),
            "entry_pred_t0": pos.get("entry_pred_t0"),
            "entry_model_version": pos.get("entry_model_version"),
            "entry_r_t": pos.get("entry_r_t"),
            "entry_z_barrier": pos.get("entry_z_barrier"),
            "entry_ev_rate": pos.get("entry_ev_rate"),
            "entry_p_none": pos.get("entry_p_none"),
            "initial_krw": pos.get("initial_krw") or self.settings.PAPER_INITIAL_KRW,
            "equity_high": equity_high,
            "day_start_date": day_start_date,
            "day_start_equity": day_start_equity,
            "halted": halted,
            "halt_reason": halt_reason,
            "halted_at": halted_at,
        }
        update_paper_position(self.engine, risk_update)

        # Build cost estimate for decision log
        cost_est = diag.get("cost_est") or _compute_cost_est(
            snapshot.get("spread_bps", 0), self.settings
        )

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
            "reason_flags": json.dumps(reason_flags),
            "cash_krw": pos["cash_krw"],
            "qty": pos["qty"],
            "equity_est": equity_est,
            "drawdown_pct": dd,
            "policy_profile": profile,
        }
        insert_paper_decision(self.engine, decision)

        log.info(
            "Paper: pos=%s action=%s reason=%s cash=%.0f qty=%.8f equity=%.0f dd=%.4f%% halted=%s profile=%s",
            pos["status"], action, reason, pos["cash_krw"], pos["qty"],
            equity_est, dd * 100, halted, profile,
        )

    def _save_pos_with_risk_fields(self, new_pos: dict, old_pos: dict) -> None:
        """Save new position but carry over risk management fields from old."""
        new_pos["initial_krw"] = old_pos.get("initial_krw") or self.settings.PAPER_INITIAL_KRW
        new_pos["equity_high"] = old_pos.get("equity_high") or self.settings.PAPER_INITIAL_KRW
        new_pos["day_start_date"] = old_pos.get("day_start_date")
        new_pos["day_start_equity"] = old_pos.get("day_start_equity") or self.settings.PAPER_INITIAL_KRW
        new_pos["halted"] = old_pos.get("halted") or False
        new_pos["halt_reason"] = old_pos.get("halt_reason")
        new_pos["halted_at"] = old_pos.get("halted_at")
        update_paper_position(self.engine, new_pos)

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
