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
# (6) paper_decisions: multi-flag reason + equity tracking
# ---------------------------------------------------------------------------
_MIG_PAPER_DECISIONS = [
    "reason_flags TEXT",
    # v1.2: equity tracking
    "cash_krw DOUBLE PRECISION",
    "qty DOUBLE PRECISION",
    "equity_est DOUBLE PRECISION",
    "drawdown_pct DOUBLE PRECISION",
    "policy_profile TEXT",
]

# ---------------------------------------------------------------------------
# (7) paper_positions: risk management + equity tracking
# ---------------------------------------------------------------------------
_MIG_PAPER_POSITIONS = [
    "initial_krw DOUBLE PRECISION",
    "equity_high DOUBLE PRECISION",
    "day_start_date DATE",
    "day_start_equity DOUBLE PRECISION",
    "halted BOOLEAN",
    "halt_reason TEXT",
    "halted_at TIMESTAMPTZ",
]


# ---------------------------------------------------------------------------
# (8) upbit_account_snapshots table (Step 7)
# ---------------------------------------------------------------------------
_CREATE_UPBIT_ACCOUNT_SNAPSHOTS = text("""
CREATE TABLE IF NOT EXISTS upbit_account_snapshots (
    id                      BIGSERIAL PRIMARY KEY,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol                  TEXT NOT NULL,
    currency                TEXT NOT NULL,
    balance                 DOUBLE PRECISION NOT NULL,
    locked                  DOUBLE PRECISION NOT NULL,
    avg_buy_price           DOUBLE PRECISION,
    avg_buy_price_modified  BOOLEAN,
    unit_currency           TEXT,
    raw_json                JSONB
)
""")

_CREATE_IDX_ACCOUNT_SNAPSHOTS_TS = text("""
CREATE INDEX IF NOT EXISTS ix_upbit_account_snapshots_ts
ON upbit_account_snapshots (ts)
""")

_CREATE_IDX_ACCOUNT_SNAPSHOTS_SYM_CUR = text("""
CREATE INDEX IF NOT EXISTS ix_upbit_account_snapshots_symbol_currency
ON upbit_account_snapshots (symbol, currency)
""")

# ---------------------------------------------------------------------------
# (9) upbit_order_attempts table (Step 7)
# ---------------------------------------------------------------------------
_CREATE_UPBIT_ORDER_ATTEMPTS = text("""
CREATE TABLE IF NOT EXISTS upbit_order_attempts (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,
    mode            TEXT NOT NULL,
    side            TEXT NOT NULL,
    ord_type        TEXT NOT NULL,
    price           DOUBLE PRECISION,
    volume          DOUBLE PRECISION,
    paper_trade_id  BIGINT,
    response_json   JSONB,
    status          TEXT NOT NULL,
    error_msg       TEXT
)
""")

_CREATE_IDX_ORDER_ATTEMPTS_TS = text("""
CREATE INDEX IF NOT EXISTS ix_upbit_order_attempts_ts
ON upbit_order_attempts (ts)
""")

_CREATE_IDX_ORDER_ATTEMPTS_SYM_TS = text("""
CREATE INDEX IF NOT EXISTS ix_upbit_order_attempts_symbol_ts
ON upbit_order_attempts (symbol, ts)
""")


def _add_columns(conn, table: str, col_defs: list[str], label: str) -> None:
    """Add columns to a table using ADD COLUMN IF NOT EXISTS (PG 9.6+)."""
    for col_def in col_defs:
        col_name = col_def.split()[0]
        conn.execute(text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_def}"
        ))
    log.info("Applied: %s (%d columns)", label, len(col_defs))


# ---------------------------------------------------------------------------
# (10) upbit_order_attempts: Step 8 extended columns
# ---------------------------------------------------------------------------
_MIG_ORDER_ATTEMPTS_STEP8 = [
    "uuid TEXT NULL",
    "identifier TEXT NULL",
    "request_json JSONB NULL",
    "http_status INTEGER NULL",
    "latency_ms INTEGER NULL",
    "remaining_req TEXT NULL",
    "retry_count INTEGER NULL DEFAULT 0",
    "final_state TEXT NULL",
    "executed_volume DOUBLE PRECISION NULL",
    "paid_fee DOUBLE PRECISION NULL",
    "avg_price DOUBLE PRECISION NULL",
]

# ---------------------------------------------------------------------------
# (11) upbit_order_snapshots table (Step 8)
# ---------------------------------------------------------------------------
_CREATE_UPBIT_ORDER_SNAPSHOTS = text("""
CREATE TABLE IF NOT EXISTS upbit_order_snapshots (
    id               BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL,
    symbol           TEXT NOT NULL,
    uuid             TEXT NOT NULL,
    state            TEXT NULL,
    side             TEXT NULL,
    ord_type         TEXT NULL,
    price            DOUBLE PRECISION NULL,
    volume           DOUBLE PRECISION NULL,
    remaining_volume DOUBLE PRECISION NULL,
    executed_volume  DOUBLE PRECISION NULL,
    paid_fee         DOUBLE PRECISION NULL,
    raw_json         JSONB NOT NULL,
    UNIQUE (uuid, ts)
)
""")

_CREATE_IDX_ORDER_SNAPSHOTS_SYM_TS = text("""
CREATE INDEX IF NOT EXISTS ix_upbit_order_snapshots_symbol_ts
ON upbit_order_snapshots (symbol, ts DESC)
""")

_CREATE_IDX_ORDER_SNAPSHOTS_UUID_TS = text("""
CREATE INDEX IF NOT EXISTS ix_upbit_order_snapshots_uuid_ts
ON upbit_order_snapshots (uuid, ts DESC)
""")

# ---------------------------------------------------------------------------
# (12) live_positions table (Step 8 — optional but recommended)
# ---------------------------------------------------------------------------
_CREATE_LIVE_POSITIONS = text("""
CREATE TABLE IF NOT EXISTS live_positions (
    symbol               TEXT PRIMARY KEY,
    ts                   TIMESTAMPTZ NOT NULL,
    krw_balance          DOUBLE PRECISION,
    btc_balance          DOUBLE PRECISION,
    btc_avg_buy_price    DOUBLE PRECISION,
    position_status      TEXT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
)
""")


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

        # (6) paper_decisions multi-flag + equity
        _add_columns(conn, "paper_decisions", _MIG_PAPER_DECISIONS, "paper_decisions equity+flags")

        # (7) paper_positions risk management
        _add_columns(conn, "paper_positions", _MIG_PAPER_POSITIONS, "paper_positions risk mgmt")

        # (8) upbit_account_snapshots table
        conn.execute(_CREATE_UPBIT_ACCOUNT_SNAPSHOTS)
        conn.execute(_CREATE_IDX_ACCOUNT_SNAPSHOTS_TS)
        conn.execute(_CREATE_IDX_ACCOUNT_SNAPSHOTS_SYM_CUR)
        log.info("Applied: upbit_account_snapshots table (CREATE IF NOT EXISTS)")

        # (9) upbit_order_attempts table
        conn.execute(_CREATE_UPBIT_ORDER_ATTEMPTS)
        conn.execute(_CREATE_IDX_ORDER_ATTEMPTS_TS)
        conn.execute(_CREATE_IDX_ORDER_ATTEMPTS_SYM_TS)
        log.info("Applied: upbit_order_attempts table (CREATE IF NOT EXISTS)")

        # (10) upbit_order_attempts: Step 8 extended columns
        _add_columns(conn, "upbit_order_attempts", _MIG_ORDER_ATTEMPTS_STEP8, "upbit_order_attempts Step 8 cols")

        # (11) upbit_order_snapshots table (Step 8)
        conn.execute(_CREATE_UPBIT_ORDER_SNAPSHOTS)
        conn.execute(_CREATE_IDX_ORDER_SNAPSHOTS_SYM_TS)
        conn.execute(_CREATE_IDX_ORDER_SNAPSHOTS_UUID_TS)
        log.info("Applied: upbit_order_snapshots table (CREATE IF NOT EXISTS)")

        # (12) live_positions table (Step 8)
        conn.execute(_CREATE_LIVE_POSITIONS)
        log.info("Applied: live_positions table (CREATE IF NOT EXISTS)")

        # (13) upbit_order_attempts: unique index for idempotency (Step 9)
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_upbit_order_attempts_identifier_mode
            ON upbit_order_attempts (identifier, mode)
            WHERE identifier IS NOT NULL
        """))
        log.info("Applied: ux_upbit_order_attempts_identifier_mode (CREATE UNIQUE INDEX IF NOT EXISTS)")

        # (14) upbit_order_attempts: blocked_reasons JSONB (Step 11)
        conn.execute(text("""
            ALTER TABLE upbit_order_attempts
            ADD COLUMN IF NOT EXISTS blocked_reasons JSONB
        """))
        log.info("Applied: upbit_order_attempts.blocked_reasons JSONB (Step 11)")

        # ── Step ALT: Alt Data tables ────────────────────────────────────
        # (ALT-1) binance_mark_price_1s
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS binance_mark_price_1s (
                id              BIGSERIAL PRIMARY KEY,
                ts              TIMESTAMPTZ NOT NULL,
                symbol          TEXT NOT NULL,
                mark_price      DOUBLE PRECISION,
                index_price     DOUBLE PRECISION,
                funding_rate    DOUBLE PRECISION,
                next_funding_time TIMESTAMPTZ,
                raw_json        JSONB NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_binance_mark_price_symbol_ts
            ON binance_mark_price_1s (symbol, ts DESC)
        """))
        log.info("Applied: binance_mark_price_1s (CREATE IF NOT EXISTS)")

        # (ALT-2) binance_force_orders
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS binance_force_orders (
                id          BIGSERIAL PRIMARY KEY,
                ts          TIMESTAMPTZ NOT NULL,
                symbol      TEXT NOT NULL,
                side        TEXT,
                price       DOUBLE PRECISION,
                qty         DOUBLE PRECISION,
                notional    DOUBLE PRECISION,
                order_type  TEXT,
                raw_json    JSONB NOT NULL,
                UNIQUE (symbol, ts, side, price, qty)
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_binance_force_orders_symbol_ts
            ON binance_force_orders (symbol, ts DESC)
        """))
        log.info("Applied: binance_force_orders (CREATE IF NOT EXISTS)")

        # (ALT-3) binance_futures_metrics
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS binance_futures_metrics (
                id          BIGSERIAL PRIMARY KEY,
                ts          TIMESTAMPTZ NOT NULL,
                symbol      TEXT NOT NULL,
                metric      TEXT NOT NULL,
                value       DOUBLE PRECISION,
                value2      DOUBLE PRECISION,
                period      TEXT,
                raw_json    JSONB NOT NULL,
                UNIQUE (metric, symbol, ts, period)
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_binance_futures_metrics_metric_symbol_ts
            ON binance_futures_metrics (metric, symbol, ts DESC)
        """))
        log.info("Applied: binance_futures_metrics (CREATE IF NOT EXISTS)")

        # (ALT-4) coinglass_liquidation_map
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS coinglass_liquidation_map (
                id           BIGSERIAL PRIMARY KEY,
                ts           TIMESTAMPTZ NOT NULL,
                symbol       TEXT NOT NULL,
                exchange     TEXT,
                timeframe    TEXT,
                summary_json JSONB,
                raw_json     JSONB NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_coinglass_liq_map_symbol_ts
            ON coinglass_liquidation_map (symbol, ts DESC)
        """))
        log.info("Applied: coinglass_liquidation_map (CREATE IF NOT EXISTS)")

        # ── Step ALT-1: feature_snapshots (학습/모델 입력용 정렬 스냅샷) ──────
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS feature_snapshots (
                ts                      TIMESTAMPTZ NOT NULL,
                symbol                  TEXT NOT NULL,
                -- Upbit market
                mid_krw                 DOUBLE PRECISION,
                spread_bps              DOUBLE PRECISION,
                imb_notional_top5       DOUBLE PRECISION,
                -- Barrier
                r_t                     DOUBLE PRECISION,
                r_min_eff               DOUBLE PRECISION,
                cost_roundtrip_est      DOUBLE PRECISION,
                sigma_1s                DOUBLE PRECISION,
                sigma_h                 DOUBLE PRECISION,
                k_vol_eff               DOUBLE PRECISION,
                barrier_status          TEXT,
                -- Prediction
                p_up                    DOUBLE PRECISION,
                p_down                  DOUBLE PRECISION,
                p_none                  DOUBLE PRECISION,
                ev                      DOUBLE PRECISION,
                ev_rate                 DOUBLE PRECISION,
                action_hat              TEXT,
                model_version           TEXT,
                -- Binance mark/index/funding (latest near ts)
                bin_mark_price          DOUBLE PRECISION,
                bin_index_price         DOUBLE PRECISION,
                bin_funding_rate        DOUBLE PRECISION,
                bin_mark_index_basis    DOUBLE PRECISION,
                -- Binance metrics (latest within freshness window)
                oi_value                DOUBLE PRECISION,
                global_ls_ratio         DOUBLE PRECISION,
                taker_ls_ratio          DOUBLE PRECISION,
                basis_value             DOUBLE PRECISION,
                -- Binance liquidation aggregates (rolling 5m)
                liq_5m_notional         DOUBLE PRECISION,
                liq_5m_count            INTEGER,
                -- Raw debug payload (optional)
                raw_json                JSONB,
                PRIMARY KEY (ts, symbol)
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_feature_snapshots_symbol_ts
            ON feature_snapshots (symbol, ts DESC)
        """))
        log.info("Applied: feature_snapshots (CREATE IF NOT EXISTS)")

        # ── Step ALT-2: feature_snapshots에 source_ts 컬럼 추가 (누수 감지용) ──
        for col_def in [
            "bin_mark_ts  TIMESTAMPTZ",
            "oi_ts        TIMESTAMPTZ",
            "liq_last_ts  TIMESTAMPTZ",
        ]:
            conn.execute(text(
                f"ALTER TABLE feature_snapshots ADD COLUMN IF NOT EXISTS {col_def}"
            ))
        log.info("Applied: feature_snapshots source_ts columns (Step ALT-2)")

        # ── Step ALT-2: coinglass_call_status 테이블 ─────────────────────────
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS coinglass_call_status (
                id          BIGSERIAL PRIMARY KEY,
                ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
                ok          BOOLEAN NOT NULL,
                http_status INTEGER,
                error_msg   TEXT,
                latency_ms  INTEGER,
                poll_count  INTEGER
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_coinglass_call_status_ts
            ON coinglass_call_status (ts DESC)
        """))
        log.info("Applied: coinglass_call_status (CREATE IF NOT EXISTS)")

    log.info("All migrations complete (v1 + Step 7-11 + Step ALT + Step ALT-1 + Step ALT-2)")
