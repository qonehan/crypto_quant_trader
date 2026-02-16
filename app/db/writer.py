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
