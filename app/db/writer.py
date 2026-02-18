from sqlalchemy import text
from sqlalchemy.engine import Engine

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
    k_vol_eff, none_ewma, target_none, ewma_alpha, ewma_eta, vol_dt_sec
) VALUES (
    :ts, :symbol, :h_sec, :vol_window_sec,
    :sigma_1s, :sigma_h, :r_min, :k_vol, :r_t,
    :sample_n, :status, :error,
    :k_vol_eff, :none_ewma, :target_none, :ewma_alpha, :ewma_eta, :vol_dt_sec
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
    vol_dt_sec = EXCLUDED.vol_dt_sec
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
    with engine.begin() as conn:
        conn.execute(_UPSERT_PREDICTION_SQL, row)


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
