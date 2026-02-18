from __future__ import annotations

from datetime import datetime


def execute_enter_long(
    pos_row: dict,
    pred_row: dict,
    market_snapshot: dict,
    settings,
    now_utc: datetime,
) -> tuple[dict, dict] | None:
    """Execute paper ENTER_LONG. Returns (new_pos, trade) or None if too small."""
    slip_rate = settings.SLIPPAGE_BPS / 10000.0
    best_ask = market_snapshot["best_ask"]
    entry_exec = best_ask * (1 + slip_rate)

    cash_krw = pos_row["cash_krw"]
    invest_krw = cash_krw * settings.MAX_POSITION_FRAC
    if invest_krw < settings.MIN_ORDER_KRW:
        return None

    qty = invest_krw / (entry_exec * (1 + settings.FEE_RATE))
    entry_cost = entry_exec * qty
    entry_fee = entry_cost * settings.FEE_RATE
    cash_after = cash_krw - entry_cost - entry_fee

    # Sanity check: cancel entry if cash goes negative
    if cash_after < 0:
        return None

    r_t = pred_row.get("r_t", settings.R_MIN)
    h_sec = pred_row.get("h_sec", settings.H_SEC)

    new_pos = {
        "symbol": pos_row["symbol"],
        "status": "LONG",
        "cash_krw": cash_after,
        "qty": qty,
        "entry_time": now_utc,
        "entry_price": entry_exec,
        "entry_fee_krw": entry_fee,
        "u_exec": entry_exec * (1 + r_t),
        "d_exec": entry_exec * (1 - r_t),
        "h_sec": h_sec,
        "entry_pred_t0": pred_row.get("t0"),
        "entry_model_version": pred_row.get("model_version"),
        "entry_r_t": r_t,
        "entry_z_barrier": pred_row.get("z_barrier"),
        "entry_ev_rate": pred_row.get("ev_rate"),
        "entry_p_none": pred_row.get("p_none"),
    }

    trade = {
        "t": now_utc,
        "symbol": pos_row["symbol"],
        "action": "ENTER_LONG",
        "reason": "SIGNAL",
        "price": entry_exec,
        "qty": qty,
        "fee_krw": entry_fee,
        "cash_after": cash_after,
        "pnl_krw": None,
        "pnl_rate": None,
        "hold_sec": None,
        "pred_t0": pred_row.get("t0"),
        "model_version": pred_row.get("model_version"),
    }

    return new_pos, trade


def execute_exit_long(
    pos_row: dict,
    market_snapshot: dict,
    settings,
    now_utc: datetime,
    reason: str,
) -> tuple[dict, dict]:
    """Execute paper EXIT_LONG. Returns (new_pos, trade)."""
    slip_rate = settings.SLIPPAGE_BPS / 10000.0
    best_bid = market_snapshot["best_bid"]
    exit_exec = best_bid * (1 - slip_rate)

    qty = pos_row["qty"]
    proceeds = exit_exec * qty
    exit_fee = proceeds * settings.FEE_RATE
    cash_after = pos_row["cash_krw"] + proceeds - exit_fee

    entry_price = pos_row["entry_price"]
    entry_fee_krw = pos_row.get("entry_fee_krw", 0) or 0
    entry_cost_total = entry_price * qty + entry_fee_krw
    realized_pnl = (proceeds - exit_fee) - entry_cost_total
    pnl_rate = realized_pnl / (entry_cost_total + 1e-12)

    entry_time = pos_row.get("entry_time")
    hold_sec = (now_utc - entry_time).total_seconds() if entry_time else None

    new_pos = {
        "symbol": pos_row["symbol"],
        "status": "FLAT",
        "cash_krw": cash_after,
        "qty": 0,
        "entry_time": None,
        "entry_price": None,
        "entry_fee_krw": None,
        "u_exec": None,
        "d_exec": None,
        "h_sec": None,
        "entry_pred_t0": None,
        "entry_model_version": None,
        "entry_r_t": None,
        "entry_z_barrier": None,
        "entry_ev_rate": None,
        "entry_p_none": None,
    }

    trade = {
        "t": now_utc,
        "symbol": pos_row["symbol"],
        "action": "EXIT_LONG",
        "reason": reason,
        "price": exit_exec,
        "qty": qty,
        "fee_krw": exit_fee,
        "cash_after": cash_after,
        "pnl_krw": realized_pnl,
        "pnl_rate": pnl_rate,
        "hold_sec": hold_sec,
        "pred_t0": pos_row.get("entry_pred_t0"),
        "model_version": pos_row.get("entry_model_version"),
    }

    return new_pos, trade
