from __future__ import annotations

from datetime import datetime, timedelta


def decide_action(
    now_utc: datetime,
    pos_row: dict,
    pred_row: dict | None,
    market_snapshot: dict,
    settings,
    recent_enter_count: int = 0,
    last_trade_time: datetime | None = None,
) -> tuple[str, str, list[str], dict]:
    """Return (action, primary_reason, reason_flags, diag) based on current position and prediction.

    reason_flags collects ALL failing conditions (not just the first).
    primary_reason is chosen by priority order.
    """
    pos_status = pos_row["status"]

    # HALTED check
    halted = pos_row.get("halted") or False

    if pos_status == "LONG":
        action, reason = _decide_long(now_utc, pos_row, pred_row, market_snapshot, settings)
        flags = [reason]
        if halted:
            flags.append("HALTED")
        return action, reason, flags, {}

    # FLAT
    if halted:
        return "STAY_FLAT", "HALTED", ["HALTED"], {}

    return _decide_flat(now_utc, pred_row, market_snapshot, settings,
                        recent_enter_count, last_trade_time)


# Priority order for FLAT → ENTER checks (higher index = lower priority)
_FLAT_PRIORITY = [
    "DATA_LAG",
    "SPREAD_WIDE",
    "NO_PRED",
    "COOLDOWN",
    "RATE_LIMIT",
    "COST_GT_RT",
    "PNONE_HIGH",
    "PDIR_WEAK",
    "EV_RATE_LOW",
]


def _get_thresholds(settings) -> dict:
    """Return effective thresholds based on policy profile."""
    profile = getattr(settings, "PAPER_POLICY_PROFILE", "strict")
    if profile == "test":
        return {
            "enter_ev_rate_th": settings.TEST_ENTER_EV_RATE_TH,
            "enter_pnone_max": settings.TEST_ENTER_PNONE_MAX,
            "enter_pdir_margin": settings.TEST_ENTER_PDIR_MARGIN,
            "cost_rmin_mult": settings.TEST_COST_RMIN_MULT,
            "max_position_frac": settings.TEST_MAX_POSITION_FRAC,
        }
    # strict (default)
    return {
        "enter_ev_rate_th": settings.ENTER_EV_RATE_TH,
        "enter_pnone_max": settings.ENTER_PNONE_MAX,
        "enter_pdir_margin": settings.ENTER_PDIR_MARGIN,
        "cost_rmin_mult": settings.COST_RMIN_MULT,
        "max_position_frac": settings.MAX_POSITION_FRAC,
    }


def _decide_flat(
    now_utc: datetime,
    pred_row: dict | None,
    market_snapshot: dict,
    settings,
    recent_enter_count: int = 0,
    last_trade_time: datetime | None = None,
) -> tuple[str, str, list[str], dict]:
    flags: list[str] = []
    diag: dict = {}
    profile = getattr(settings, "PAPER_POLICY_PROFILE", "strict")
    th = _get_thresholds(settings)

    lag_sec = market_snapshot.get("lag_sec", 999)
    spread_bps = market_snapshot.get("spread_bps", 999)

    # Data / market safety (always applied regardless of profile)
    if lag_sec > settings.DATA_LAG_SEC_MAX:
        flags.append("DATA_LAG")
    if spread_bps > settings.ENTER_SPREAD_BPS_MAX:
        flags.append("SPREAD_WIDE")

    if pred_row is None:
        flags.append("NO_PRED")
        primary = _pick_primary(flags)
        return "STAY_FLAT", primary, flags, diag

    # Test-mode rate limit and cooldown
    if profile == "test":
        if recent_enter_count >= settings.TEST_MAX_ENTRIES_PER_HOUR:
            flags.append("RATE_LIMIT")
        if last_trade_time is not None:
            elapsed = (now_utc - last_trade_time).total_seconds()
            if elapsed < settings.TEST_COOLDOWN_SEC:
                flags.append("COOLDOWN")

    ev_rate = pred_row.get("ev_rate")
    p_none = pred_row.get("p_none", 1.0)
    p_up = pred_row.get("p_up", 0)
    p_down = pred_row.get("p_down", 0)
    r_t = pred_row.get("r_t", 0)

    cost_est = _compute_cost_est(spread_bps, settings)
    diag["cost_est"] = cost_est

    # Cost vs r_t
    if r_t <= th["cost_rmin_mult"] * cost_est:
        flags.append("COST_GT_RT")

    if p_none > th["enter_pnone_max"]:
        flags.append("PNONE_HIGH")

    if p_up < p_down + th["enter_pdir_margin"]:
        flags.append("PDIR_WEAK")

    if ev_rate is None or ev_rate < th["enter_ev_rate_th"]:
        flags.append("EV_RATE_LOW")

    if flags:
        primary = _pick_primary(flags)
        return "STAY_FLAT", primary, flags, diag

    return "ENTER_LONG", "OK", ["OK"], diag


def _pick_primary(flags: list[str]) -> str:
    """Pick primary reason by priority order."""
    for reason in _FLAT_PRIORITY:
        if reason in flags:
            return reason
    return flags[0] if flags else "OK"


def _decide_long(
    now_utc: datetime,
    pos_row: dict,
    pred_row: dict | None,
    market_snapshot: dict,
    settings,
) -> tuple[str, str]:
    slip_rate = settings.SLIPPAGE_BPS / 10000.0
    best_bid = market_snapshot.get("best_bid")
    if best_bid is None or best_bid <= 0:
        return "HOLD_LONG", "NO_BID"

    exit_exec = best_bid * (1 - slip_rate)

    u_exec = pos_row.get("u_exec")
    d_exec = pos_row.get("d_exec")

    # TP
    if u_exec is not None and exit_exec >= u_exec:
        return "EXIT_LONG", "TP"

    # SL
    if d_exec is not None and exit_exec <= d_exec:
        return "EXIT_LONG", "SL"

    # Time stop
    entry_time = pos_row.get("entry_time")
    h_sec = pos_row.get("h_sec", settings.H_SEC)
    if entry_time is not None and h_sec is not None:
        if now_utc >= entry_time + timedelta(seconds=h_sec):
            return "EXIT_LONG", "TIME"

    # EV_BAD
    if pred_row is not None:
        ev_rate = pred_row.get("ev_rate")
        if ev_rate is not None and ev_rate <= settings.EXIT_EV_RATE_TH:
            return "EXIT_LONG", "EV_BAD"

    return "HOLD_LONG", "OK"


def _compute_cost_est(spread_bps: float, settings) -> float:
    fee_round = 2 * settings.FEE_RATE
    slip_round = 2 * (settings.SLIPPAGE_BPS / 10000.0)
    spread_round = spread_bps / 10000.0
    return settings.EV_COST_MULT * (fee_round + slip_round + spread_round)
