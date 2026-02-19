import json

from sqlalchemy import text
from sqlalchemy.engine import Engine


def _j(val):
    """Serialize Python dict/list to JSON string for psycopg3 text() JSONB params."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return val

_UPSERT_SQL = text("""
INSERT INTO market_1s (
    ts, symbol, mid, bid, ask, spread,
    trade_count_1s, trade_volume_1s, imbalance_top5,
    last_trade_price, last_trade_volume, last_trade_side,
    ticker_ts_ms, trade_ts_ms, orderbook_ts_ms,
    bid_open_1s, bid_high_1s, bid_low_1s, bid_close_1s,
    ask_open_1s, ask_high_1s, ask_low_1s, ask_close_1s,
    spread_bps, imb_notional_top5, mid_close_1s
) VALUES (
    :ts, :symbol, :mid, :bid, :ask, :spread,
    :trade_count_1s, :trade_volume_1s, :imbalance_top5,
    :last_trade_price, :last_trade_volume, :last_trade_side,
    :ticker_ts_ms, :trade_ts_ms, :orderbook_ts_ms,
    :bid_open_1s, :bid_high_1s, :bid_low_1s, :bid_close_1s,
    :ask_open_1s, :ask_high_1s, :ask_low_1s, :ask_close_1s,
    :spread_bps, :imb_notional_top5, :mid_close_1s
)
ON CONFLICT (symbol, ts) DO UPDATE SET
    mid = EXCLUDED.mid,
    bid = EXCLUDED.bid,
    ask = EXCLUDED.ask,
    spread = EXCLUDED.spread,
    trade_count_1s = EXCLUDED.trade_count_1s,
    trade_volume_1s = EXCLUDED.trade_volume_1s,
    imbalance_top5 = EXCLUDED.imbalance_top5,
    last_trade_price = EXCLUDED.last_trade_price,
    last_trade_volume = EXCLUDED.last_trade_volume,
    last_trade_side = EXCLUDED.last_trade_side,
    ticker_ts_ms = EXCLUDED.ticker_ts_ms,
    trade_ts_ms = EXCLUDED.trade_ts_ms,
    orderbook_ts_ms = EXCLUDED.orderbook_ts_ms,
    bid_open_1s = EXCLUDED.bid_open_1s,
    bid_high_1s = EXCLUDED.bid_high_1s,
    bid_low_1s = EXCLUDED.bid_low_1s,
    bid_close_1s = EXCLUDED.bid_close_1s,
    ask_open_1s = EXCLUDED.ask_open_1s,
    ask_high_1s = EXCLUDED.ask_high_1s,
    ask_low_1s = EXCLUDED.ask_low_1s,
    ask_close_1s = EXCLUDED.ask_close_1s,
    spread_bps = EXCLUDED.spread_bps,
    imb_notional_top5 = EXCLUDED.imb_notional_top5,
    mid_close_1s = EXCLUDED.mid_close_1s
""")


def upsert_market_1s(engine: Engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPSERT_SQL, row)


_UPSERT_BARRIER_SQL = text("""
INSERT INTO barrier_state (
    ts, symbol, h_sec, vol_window_sec,
    sigma_1s, sigma_h, r_min, k_vol, r_t,
    sample_n, status, error,
    k_vol_eff, none_ewma, target_none, ewma_alpha, ewma_eta, vol_dt_sec,
    spread_bps_med, cost_roundtrip_est, r_min_eff
) VALUES (
    :ts, :symbol, :h_sec, :vol_window_sec,
    :sigma_1s, :sigma_h, :r_min, :k_vol, :r_t,
    :sample_n, :status, :error,
    :k_vol_eff, :none_ewma, :target_none, :ewma_alpha, :ewma_eta, :vol_dt_sec,
    :spread_bps_med, :cost_roundtrip_est, :r_min_eff
)
ON CONFLICT (symbol, ts) DO UPDATE SET
    h_sec = EXCLUDED.h_sec,
    vol_window_sec = EXCLUDED.vol_window_sec,
    sigma_1s = EXCLUDED.sigma_1s,
    sigma_h = EXCLUDED.sigma_h,
    r_min = EXCLUDED.r_min,
    k_vol = EXCLUDED.k_vol,
    r_t = EXCLUDED.r_t,
    sample_n = EXCLUDED.sample_n,
    status = EXCLUDED.status,
    error = EXCLUDED.error,
    k_vol_eff = EXCLUDED.k_vol_eff,
    none_ewma = EXCLUDED.none_ewma,
    target_none = EXCLUDED.target_none,
    ewma_alpha = EXCLUDED.ewma_alpha,
    ewma_eta = EXCLUDED.ewma_eta,
    vol_dt_sec = EXCLUDED.vol_dt_sec,
    spread_bps_med = EXCLUDED.spread_bps_med,
    cost_roundtrip_est = EXCLUDED.cost_roundtrip_est,
    r_min_eff = EXCLUDED.r_min_eff
""")


def upsert_barrier_state(engine: Engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPSERT_BARRIER_SQL, row)


_UPSERT_PREDICTION_SQL = text("""
INSERT INTO predictions (
    t0, symbol, h_sec, r_t,
    p_up, p_down, p_none, t_up, t_down,
    slope_pred, ev, direction_hat,
    model_version, status,
    sigma_1s, sigma_h, features,
    z_barrier, p_hit_base, ev_rate, r_none_pred,
    t_up_cond_pred, t_down_cond_pred,
    spread_bps, mom_z, imb_notional_top5, action_hat
) VALUES (
    :t0, :symbol, :h_sec, :r_t,
    :p_up, :p_down, :p_none, :t_up, :t_down,
    :slope_pred, :ev, :direction_hat,
    :model_version, :status,
    :sigma_1s, :sigma_h, :features,
    :z_barrier, :p_hit_base, :ev_rate, :r_none_pred,
    :t_up_cond_pred, :t_down_cond_pred,
    :spread_bps, :mom_z, :imb_notional_top5, :action_hat
)
ON CONFLICT ON CONSTRAINT uq_predictions_symbol_t0 DO UPDATE SET
    h_sec = EXCLUDED.h_sec,
    r_t = EXCLUDED.r_t,
    p_up = EXCLUDED.p_up,
    p_down = EXCLUDED.p_down,
    p_none = EXCLUDED.p_none,
    t_up = EXCLUDED.t_up,
    t_down = EXCLUDED.t_down,
    slope_pred = EXCLUDED.slope_pred,
    ev = EXCLUDED.ev,
    direction_hat = EXCLUDED.direction_hat,
    model_version = EXCLUDED.model_version,
    status = EXCLUDED.status,
    sigma_1s = EXCLUDED.sigma_1s,
    sigma_h = EXCLUDED.sigma_h,
    features = EXCLUDED.features,
    z_barrier = EXCLUDED.z_barrier,
    p_hit_base = EXCLUDED.p_hit_base,
    ev_rate = EXCLUDED.ev_rate,
    r_none_pred = EXCLUDED.r_none_pred,
    t_up_cond_pred = EXCLUDED.t_up_cond_pred,
    t_down_cond_pred = EXCLUDED.t_down_cond_pred,
    spread_bps = EXCLUDED.spread_bps,
    mom_z = EXCLUDED.mom_z,
    imb_notional_top5 = EXCLUDED.imb_notional_top5,
    action_hat = EXCLUDED.action_hat
""")


def upsert_prediction(engine: Engine, row: dict) -> None:
    r = dict(row)
    r["features"] = _j(r.get("features"))
    with engine.begin() as conn:
        conn.execute(_UPSERT_PREDICTION_SQL, r)


_UPSERT_EVAL_SQL = text("""
INSERT INTO evaluation_results (
    ts, symbol, t0, r_t,
    p_up, p_down, p_none, ev, slope_pred,
    direction_hat, actual_direction, actual_r_t, touch_time_sec,
    status, error,
    label_version, entry_price, u_exec, d_exec, ambig_touch, r_h,
    brier, logloss
) VALUES (
    :ts, :symbol, :t0, :r_t,
    :p_up, :p_down, :p_none, :ev, :slope_pred,
    :direction_hat, :actual_direction, :actual_r_t, :touch_time_sec,
    :status, :error,
    :label_version, :entry_price, :u_exec, :d_exec, :ambig_touch, :r_h,
    :brier, :logloss
)
ON CONFLICT ON CONSTRAINT uq_evaluation_results_symbol_t0 DO UPDATE SET
    ts = EXCLUDED.ts,
    r_t = EXCLUDED.r_t,
    p_up = EXCLUDED.p_up,
    p_down = EXCLUDED.p_down,
    p_none = EXCLUDED.p_none,
    ev = EXCLUDED.ev,
    slope_pred = EXCLUDED.slope_pred,
    direction_hat = EXCLUDED.direction_hat,
    actual_direction = EXCLUDED.actual_direction,
    actual_r_t = EXCLUDED.actual_r_t,
    touch_time_sec = EXCLUDED.touch_time_sec,
    status = EXCLUDED.status,
    error = EXCLUDED.error,
    label_version = EXCLUDED.label_version,
    entry_price = EXCLUDED.entry_price,
    u_exec = EXCLUDED.u_exec,
    d_exec = EXCLUDED.d_exec,
    ambig_touch = EXCLUDED.ambig_touch,
    r_h = EXCLUDED.r_h,
    brier = EXCLUDED.brier,
    logloss = EXCLUDED.logloss
""")


def upsert_evaluation_result(engine: Engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPSERT_EVAL_SQL, row)


# ---------------------------------------------------------------------------
# barrier_params CRUD
# ---------------------------------------------------------------------------

_SELECT_BARRIER_PARAMS = text("""
SELECT symbol, k_vol_eff, none_ewma, target_none, ewma_alpha, ewma_eta,
       last_eval_t0, updated_at
FROM barrier_params WHERE symbol = :symbol
""")

_INSERT_BARRIER_PARAMS = text("""
INSERT INTO barrier_params (symbol, k_vol_eff, none_ewma, target_none, ewma_alpha, ewma_eta)
VALUES (:symbol, :k_vol_eff, :none_ewma, :target_none, :ewma_alpha, :ewma_eta)
ON CONFLICT (symbol) DO NOTHING
""")

_UPDATE_BARRIER_PARAMS = text("""
UPDATE barrier_params
SET k_vol_eff = :k_vol_eff,
    none_ewma = :none_ewma,
    last_eval_t0 = :last_eval_t0,
    updated_at = now()
WHERE symbol = :symbol
""")


def get_or_create_barrier_params(engine: Engine, symbol: str, defaults: dict) -> dict:
    with engine.begin() as conn:
        row = conn.execute(_SELECT_BARRIER_PARAMS, {"symbol": symbol}).fetchone()
        if row is not None:
            return row._asdict()
        conn.execute(_INSERT_BARRIER_PARAMS, {"symbol": symbol, **defaults})
        row = conn.execute(_SELECT_BARRIER_PARAMS, {"symbol": symbol}).fetchone()
        return row._asdict()


def update_barrier_params(
    engine: Engine, symbol: str, k_vol_eff: float, none_ewma: float, last_eval_t0
) -> None:
    with engine.begin() as conn:
        conn.execute(
            _UPDATE_BARRIER_PARAMS,
            {
                "symbol": symbol,
                "k_vol_eff": k_vol_eff,
                "none_ewma": none_ewma,
                "last_eval_t0": last_eval_t0,
            },
        )


# ---------------------------------------------------------------------------
# Paper Trading CRUD
# ---------------------------------------------------------------------------

_SELECT_PAPER_POS = text("""
SELECT symbol, status, cash_krw, qty, entry_time, entry_price, entry_fee_krw,
       u_exec, d_exec, h_sec, entry_pred_t0, entry_model_version,
       entry_r_t, entry_z_barrier, entry_ev_rate, entry_p_none,
       initial_krw, equity_high, day_start_date, day_start_equity,
       halted, halt_reason, halted_at, updated_at
FROM paper_positions WHERE symbol = :symbol
""")

_INSERT_PAPER_POS = text("""
INSERT INTO paper_positions (symbol, status, cash_krw, qty,
                             initial_krw, equity_high, day_start_date, day_start_equity,
                             halted)
VALUES (:symbol, 'FLAT', :cash_krw, 0,
        :initial_krw, :equity_high, :day_start_date, :day_start_equity,
        false)
ON CONFLICT (symbol) DO NOTHING
""")

_UPDATE_PAPER_POS = text("""
UPDATE paper_positions SET
    status = :status, cash_krw = :cash_krw, qty = :qty,
    entry_time = :entry_time, entry_price = :entry_price, entry_fee_krw = :entry_fee_krw,
    u_exec = :u_exec, d_exec = :d_exec, h_sec = :h_sec,
    entry_pred_t0 = :entry_pred_t0, entry_model_version = :entry_model_version,
    entry_r_t = :entry_r_t, entry_z_barrier = :entry_z_barrier,
    entry_ev_rate = :entry_ev_rate, entry_p_none = :entry_p_none,
    initial_krw = :initial_krw, equity_high = :equity_high,
    day_start_date = :day_start_date, day_start_equity = :day_start_equity,
    halted = :halted, halt_reason = :halt_reason, halted_at = :halted_at,
    updated_at = now()
WHERE symbol = :symbol
""")

_INSERT_PAPER_TRADE = text("""
INSERT INTO paper_trades (t, symbol, action, reason, price, qty, fee_krw, cash_after,
                          pnl_krw, pnl_rate, hold_sec, pred_t0, model_version)
VALUES (:t, :symbol, :action, :reason, :price, :qty, :fee_krw, :cash_after,
        :pnl_krw, :pnl_rate, :hold_sec, :pred_t0, :model_version)
""")

_INSERT_PAPER_DECISION = text("""
INSERT INTO paper_decisions (ts, symbol, pos_status, action, reason,
                             ev_rate, ev, p_up, p_down, p_none, r_t, z_barrier,
                             spread_bps, lag_sec, cost_roundtrip_est, model_version, pred_t0,
                             reason_flags,
                             cash_krw, qty, equity_est, drawdown_pct, policy_profile)
VALUES (:ts, :symbol, :pos_status, :action, :reason,
        :ev_rate, :ev, :p_up, :p_down, :p_none, :r_t, :z_barrier,
        :spread_bps, :lag_sec, :cost_roundtrip_est, :model_version, :pred_t0,
        :reason_flags,
        :cash_krw, :qty, :equity_est, :drawdown_pct, :policy_profile)
""")


def get_or_create_paper_position(engine: Engine, symbol: str, initial_krw: float) -> dict:
    from datetime import date, timezone, datetime as dt
    today_utc = dt.now(timezone.utc).date()
    with engine.begin() as conn:
        row = conn.execute(_SELECT_PAPER_POS, {"symbol": symbol}).fetchone()
        if row is not None:
            d = row._asdict()
            # Backfill missing fields for existing rows
            if d.get("initial_krw") is None:
                conn.execute(text(
                    "UPDATE paper_positions SET initial_krw=:v, equity_high=:v, "
                    "day_start_date=:d, day_start_equity=:v, halted=false "
                    "WHERE symbol=:s AND initial_krw IS NULL"
                ), {"v": initial_krw, "d": today_utc, "s": symbol})
                d["initial_krw"] = initial_krw
                d["equity_high"] = initial_krw
                d["day_start_date"] = today_utc
                d["day_start_equity"] = initial_krw
                d["halted"] = False
                d["halt_reason"] = None
                d["halted_at"] = None
            return d
        conn.execute(_INSERT_PAPER_POS, {
            "symbol": symbol,
            "cash_krw": initial_krw,
            "initial_krw": initial_krw,
            "equity_high": initial_krw,
            "day_start_date": today_utc,
            "day_start_equity": initial_krw,
        })
        row = conn.execute(_SELECT_PAPER_POS, {"symbol": symbol}).fetchone()
        return row._asdict()


def update_paper_position(engine: Engine, pos: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPDATE_PAPER_POS, pos)


def insert_paper_trade(engine: Engine, trade: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_INSERT_PAPER_TRADE, trade)


def insert_paper_decision(engine: Engine, decision: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_INSERT_PAPER_DECISION, decision)


# ---------------------------------------------------------------------------
# Upbit Exchange (Step 7)
# ---------------------------------------------------------------------------

_INSERT_UPBIT_ACCOUNT_SNAPSHOT = text("""
INSERT INTO upbit_account_snapshots
    (ts, symbol, currency, balance, locked,
     avg_buy_price, avg_buy_price_modified, unit_currency, raw_json)
VALUES
    (:ts, :symbol, :currency, :balance, :locked,
     :avg_buy_price, :avg_buy_price_modified, :unit_currency, :raw_json)
""")

_UPSERT_UPBIT_ORDER_ATTEMPT = text("""
INSERT INTO upbit_order_attempts
    (ts, symbol, action, mode, side, ord_type,
     price, volume, paper_trade_id, response_json, status, error_msg,
     uuid, identifier, request_json, http_status, latency_ms, remaining_req,
     retry_count, final_state, executed_volume, paid_fee, avg_price,
     blocked_reasons)
VALUES
    (:ts, :symbol, :action, :mode, :side, :ord_type,
     :price, :volume, :paper_trade_id, :response_json, :status, :error_msg,
     :uuid, :identifier, :request_json, :http_status, :latency_ms, :remaining_req,
     :retry_count, :final_state, :executed_volume, :paid_fee, :avg_price,
     :blocked_reasons)
ON CONFLICT (identifier, mode) WHERE identifier IS NOT NULL
DO UPDATE SET
    ts              = EXCLUDED.ts,
    status          = EXCLUDED.status,
    request_json    = COALESCE(EXCLUDED.request_json,  upbit_order_attempts.request_json),
    response_json   = COALESCE(EXCLUDED.response_json, upbit_order_attempts.response_json),
    http_status     = EXCLUDED.http_status,
    latency_ms      = EXCLUDED.latency_ms,
    remaining_req   = EXCLUDED.remaining_req,
    retry_count     = GREATEST(upbit_order_attempts.retry_count, EXCLUDED.retry_count),
    error_msg       = EXCLUDED.error_msg,
    uuid            = COALESCE(EXCLUDED.uuid, upbit_order_attempts.uuid),
    final_state     = COALESCE(EXCLUDED.final_state, upbit_order_attempts.final_state),
    blocked_reasons = COALESCE(EXCLUDED.blocked_reasons, upbit_order_attempts.blocked_reasons)
RETURNING id
""")


def insert_upbit_account_snapshot(engine: Engine, row: dict) -> None:
    r = dict(row)
    r["raw_json"] = _j(r.get("raw_json"))
    with engine.begin() as conn:
        conn.execute(_INSERT_UPBIT_ACCOUNT_SNAPSHOT, r)


def insert_upbit_order_attempt(engine: Engine, row: dict) -> int | None:
    """Upsert an order attempt row and return its id.

    ON CONFLICT (identifier, mode) WHERE identifier IS NOT NULL
    ensures exactly one row per (identifier, mode) pair at the DB level.
    Rows with identifier=NULL (legacy) are always inserted.
    """
    defaults = {
        "uuid": None,
        "identifier": None,
        "request_json": None,
        "http_status": None,
        "latency_ms": None,
        "remaining_req": None,
        "retry_count": 0,
        "final_state": None,
        "executed_volume": None,
        "paid_fee": None,
        "avg_price": None,
        "blocked_reasons": None,
    }
    merged = {**defaults, **row}
    # Serialize JSONB fields for psycopg3 text() queries
    merged["response_json"] = _j(merged.get("response_json"))
    merged["request_json"] = _j(merged.get("request_json"))
    merged["blocked_reasons"] = _j(merged.get("blocked_reasons"))
    with engine.begin() as conn:
        result = conn.execute(_UPSERT_UPBIT_ORDER_ATTEMPT, merged)
        row_res = result.fetchone()
        return row_res[0] if row_res else None


_UPDATE_ORDER_ATTEMPT_FINAL = text("""
UPDATE upbit_order_attempts
SET final_state = :final_state,
    executed_volume = :executed_volume,
    paid_fee = :paid_fee,
    avg_price = :avg_price
WHERE id = :attempt_id
""")


def update_upbit_order_attempt_final(
    engine: Engine,
    attempt_id: int,
    final_state: str,
    executed_volume: float | None = None,
    paid_fee: float | None = None,
    avg_price: float | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(_UPDATE_ORDER_ATTEMPT_FINAL, {
            "attempt_id": attempt_id,
            "final_state": final_state,
            "executed_volume": executed_volume,
            "paid_fee": paid_fee,
            "avg_price": avg_price,
        })


_INSERT_UPBIT_ORDER_SNAPSHOT = text("""
INSERT INTO upbit_order_snapshots
    (ts, symbol, uuid, state, side, ord_type, price, volume,
     remaining_volume, executed_volume, paid_fee, raw_json)
VALUES
    (:ts, :symbol, :uuid, :state, :side, :ord_type, :price, :volume,
     :remaining_volume, :executed_volume, :paid_fee, :raw_json)
ON CONFLICT (uuid, ts) DO NOTHING
""")


def insert_upbit_order_snapshot(engine: Engine, row: dict) -> None:
    r = dict(row)
    r["raw_json"] = _j(r.get("raw_json"))
    with engine.begin() as conn:
        conn.execute(_INSERT_UPBIT_ORDER_SNAPSHOT, r)


_UPSERT_LIVE_POSITION = text("""
INSERT INTO live_positions
    (symbol, ts, krw_balance, btc_balance, btc_avg_buy_price, position_status, updated_at)
VALUES
    (:symbol, :ts, :krw_balance, :btc_balance, :btc_avg_buy_price, :position_status, now())
ON CONFLICT (symbol) DO UPDATE SET
    ts = EXCLUDED.ts,
    krw_balance = EXCLUDED.krw_balance,
    btc_balance = EXCLUDED.btc_balance,
    btc_avg_buy_price = EXCLUDED.btc_avg_buy_price,
    position_status = EXCLUDED.position_status,
    updated_at = now()
""")


def upsert_live_position(engine: Engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPSERT_LIVE_POSITION, row)
