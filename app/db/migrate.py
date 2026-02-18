"""Lightweight idempotent migrations using ALTER TABLE ... ADD COLUMN IF NOT EXISTS."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# (1) market_1s: bid/ask OHLC + spread_bps + imb_notional_top5 + mid_close_1s
# ---------------------------------------------------------------------------
_MIG_MARKET_1S = [
    "bid_open_1s DOUBLE PRECISION",
    "bid_high_1s DOUBLE PRECISION",
    "bid_low_1s DOUBLE PRECISION",
    "bid_close_1s DOUBLE PRECISION",
    "ask_open_1s DOUBLE PRECISION",
    "ask_high_1s DOUBLE PRECISION",
    "ask_low_1s DOUBLE PRECISION",
    "ask_close_1s DOUBLE PRECISION",
    "spread_bps DOUBLE PRECISION",
    "imb_notional_top5 DOUBLE PRECISION",
    "mid_close_1s DOUBLE PRECISION",
]

# ---------------------------------------------------------------------------
# (2) barrier_state: feedback / state tracking
# ---------------------------------------------------------------------------
_MIG_BARRIER_STATE = [
    "k_vol_eff DOUBLE PRECISION",
    "none_ewma DOUBLE PRECISION",
    "target_none DOUBLE PRECISION",
    "ewma_alpha DOUBLE PRECISION",
    "ewma_eta DOUBLE PRECISION",
    "vol_dt_sec INTEGER",
    # v1.1: cost-based r_t floor
    "spread_bps_med DOUBLE PRECISION",
    "cost_roundtrip_est DOUBLE PRECISION",
    "r_min_eff DOUBLE PRECISION",
]

# ---------------------------------------------------------------------------
# (3) predictions: v1 probability / EV fields
# ---------------------------------------------------------------------------
_MIG_PREDICTIONS = [
    "z_barrier DOUBLE PRECISION",
    "p_hit_base DOUBLE PRECISION",
    "ev_rate DOUBLE PRECISION",
    "r_none_pred DOUBLE PRECISION",
    "t_up_cond_pred DOUBLE PRECISION",
    "t_down_cond_pred DOUBLE PRECISION",
    "spread_bps DOUBLE PRECISION",
    "mom_z DOUBLE PRECISION",
    "imb_notional_top5 DOUBLE PRECISION",
    "action_hat TEXT",
]

# ---------------------------------------------------------------------------
# (4) evaluation_results: exec_v1 label / probability assessment
# ---------------------------------------------------------------------------
_MIG_EVALUATION_RESULTS = [
    "label_version TEXT",
    "entry_price DOUBLE PRECISION",
    "u_exec DOUBLE PRECISION",
    "d_exec DOUBLE PRECISION",
    "ambig_touch BOOLEAN",
    "r_h DOUBLE PRECISION",
    "p_up DOUBLE PRECISION",
    "p_down DOUBLE PRECISION",
    "p_none DOUBLE PRECISION",
    "brier DOUBLE PRECISION",
    "logloss DOUBLE PRECISION",
]

# ---------------------------------------------------------------------------
# (5) barrier_params table
# ---------------------------------------------------------------------------
_CREATE_BARRIER_PARAMS = text("""
CREATE TABLE IF NOT EXISTS barrier_params (
    symbol         TEXT PRIMARY KEY,
    k_vol_eff      DOUBLE PRECISION NOT NULL,
    none_ewma      DOUBLE PRECISION NOT NULL,
    target_none    DOUBLE PRECISION NOT NULL,
    ewma_alpha     DOUBLE PRECISION NOT NULL,
    ewma_eta       DOUBLE PRECISION NOT NULL,
    last_eval_t0   TIMESTAMPTZ NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)
""")


# ---------------------------------------------------------------------------
# (6) paper_decisions: multi-flag reason
# ---------------------------------------------------------------------------
_MIG_PAPER_DECISIONS = [
    "reason_flags TEXT",
]


def _add_columns(conn, table: str, col_defs: list[str], label: str) -> None:
    """Add columns to a table using ADD COLUMN IF NOT EXISTS (PG 9.6+)."""
    for col_def in col_defs:
        col_name = col_def.split()[0]
        conn.execute(text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_def}"
        ))
    log.info("Applied: %s (%d columns)", label, len(col_defs))


def apply_migrations(engine: Engine) -> None:
    """Run all idempotent migrations sequentially."""
    with engine.begin() as conn:
        # (1) market_1s columns
        _add_columns(conn, "market_1s", _MIG_MARKET_1S, "market_1s bid/ask OHLC + extras")

        # (2) barrier_state columns
        _add_columns(conn, "barrier_state", _MIG_BARRIER_STATE, "barrier_state feedback cols")

        # (3) predictions columns
        _add_columns(conn, "predictions", _MIG_PREDICTIONS, "predictions v1 cols")

        # (4) evaluation_results columns
        # p_up, p_down, p_none already exist – use IF NOT EXISTS so no error
        _add_columns(conn, "evaluation_results", _MIG_EVALUATION_RESULTS, "evaluation_results exec_v1 cols")

        # (5) barrier_params table
        conn.execute(_CREATE_BARRIER_PARAMS)
        log.info("Applied: barrier_params table (CREATE IF NOT EXISTS)")

        # (6) paper_decisions multi-flag
        _add_columns(conn, "paper_decisions", _MIG_PAPER_DECISIONS, "paper_decisions reason_flags")

    log.info("All v1 migrations complete")
