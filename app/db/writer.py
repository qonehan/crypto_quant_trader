from sqlalchemy import text
from sqlalchemy.engine import Engine

_UPSERT_SQL = text("""
INSERT INTO market_1s (
    ts, symbol, mid, bid, ask, spread,
    trade_count_1s, trade_volume_1s, imbalance_top5,
    last_trade_price, last_trade_volume, last_trade_side,
    ticker_ts_ms, trade_ts_ms, orderbook_ts_ms
) VALUES (
    :ts, :symbol, :mid, :bid, :ask, :spread,
    :trade_count_1s, :trade_volume_1s, :imbalance_top5,
    :last_trade_price, :last_trade_volume, :last_trade_side,
    :ticker_ts_ms, :trade_ts_ms, :orderbook_ts_ms
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
    orderbook_ts_ms = EXCLUDED.orderbook_ts_ms
""")


def upsert_market_1s(engine: Engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPSERT_SQL, row)


_UPSERT_BARRIER_SQL = text("""
INSERT INTO barrier_state (
    ts, symbol, h_sec, vol_window_sec,
    sigma_1s, sigma_h, r_min, k_vol, r_t,
    sample_n, status, error
) VALUES (
    :ts, :symbol, :h_sec, :vol_window_sec,
    :sigma_1s, :sigma_h, :r_min, :k_vol, :r_t,
    :sample_n, :status, :error
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
    error = EXCLUDED.error
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
    sigma_1s, sigma_h, features
) VALUES (
    :t0, :symbol, :h_sec, :r_t,
    :p_up, :p_down, :p_none, :t_up, :t_down,
    :slope_pred, :ev, :direction_hat,
    :model_version, :status,
    :sigma_1s, :sigma_h, :features
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
    features = EXCLUDED.features
""")


def upsert_prediction(engine: Engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPSERT_PREDICTION_SQL, row)


_UPSERT_EVAL_SQL = text("""
INSERT INTO evaluation_results (
    ts, symbol, t0, r_t,
    p_up, p_down, p_none, ev, slope_pred,
    direction_hat, actual_direction, actual_r_t, touch_time_sec,
    status, error
) VALUES (
    :ts, :symbol, :t0, :r_t,
    :p_up, :p_down, :p_none, :ev, :slope_pred,
    :direction_hat, :actual_direction, :actual_r_t, :touch_time_sec,
    :status, :error
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
    error = EXCLUDED.error
""")


def upsert_evaluation_result(engine: Engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPSERT_EVAL_SQL, row)
