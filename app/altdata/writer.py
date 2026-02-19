"""Alt Data DB writer: insert/upsert helpers for binance_mark_price_1s,
binance_force_orders, binance_futures_metrics, coinglass_liquidation_map."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


def _j(obj) -> str:
    """Serialize to JSONB-compatible string."""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# binance_mark_price_1s
# ──────────────────────────────────────────────────────────────────────────────

def insert_mark_price(engine: Engine, ts: datetime, symbol: str, row: dict) -> None:
    """Insert one mark-price row. Silently skips on duplicate key errors."""
    try:
        mark_price = float(row.get("p") or row.get("markPrice") or 0) or None
        index_price = float(row.get("i") or row.get("indexPrice") or 0) or None
        funding_rate = float(row.get("r") or row.get("fundingRate") or 0) or None
        nft_ms = row.get("T") or row.get("nextFundingTime")
        next_funding_time = (
            datetime.fromtimestamp(int(nft_ms) / 1000, tz=timezone.utc)
            if nft_ms
            else None
        )
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO binance_mark_price_1s
                        (ts, symbol, mark_price, index_price, funding_rate,
                         next_funding_time, raw_json)
                    VALUES
                        (:ts, :symbol, :mark_price, :index_price, :funding_rate,
                         :next_funding_time, CAST(:raw_json AS JSONB))
                    ON CONFLICT DO NOTHING
                """),
                {
                    "ts": ts,
                    "symbol": symbol,
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "funding_rate": funding_rate,
                    "next_funding_time": next_funding_time,
                    "raw_json": _j(row),
                },
            )
    except Exception:
        log.exception("insert_mark_price error")


# ──────────────────────────────────────────────────────────────────────────────
# binance_force_orders
# ──────────────────────────────────────────────────────────────────────────────

def insert_force_order(engine: Engine, ts: datetime, symbol: str, order: dict) -> None:
    """Insert one liquidation event with UNIQUE guard."""
    try:
        side = str(order.get("S") or order.get("side") or "")
        price = float(order.get("p") or order.get("price") or 0) or None
        qty = float(order.get("q") or order.get("origQty") or 0) or None
        notional = (price * qty) if (price and qty) else None
        order_type = str(order.get("o") or order.get("type") or "")
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO binance_force_orders
                        (ts, symbol, side, price, qty, notional, order_type, raw_json)
                    VALUES
                        (:ts, :symbol, :side, :price, :qty, :notional, :order_type,
                         CAST(:raw_json AS JSONB))
                    ON CONFLICT (symbol, ts, side, price, qty) DO NOTHING
                """),
                {
                    "ts": ts,
                    "symbol": symbol,
                    "side": side,
                    "price": price,
                    "qty": qty,
                    "notional": notional,
                    "order_type": order_type,
                    "raw_json": _j(order),
                },
            )
    except Exception:
        log.exception("insert_force_order error")


# ──────────────────────────────────────────────────────────────────────────────
# binance_futures_metrics
# ──────────────────────────────────────────────────────────────────────────────

def upsert_futures_metric(
    engine: Engine,
    ts: datetime,
    symbol: str,
    metric: str,
    value: float | None,
    value2: float | None,
    period: str,
    raw: dict,
) -> None:
    """Upsert one futures metric row. Uses (metric, symbol, ts, period) as unique key."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO binance_futures_metrics
                        (ts, symbol, metric, value, value2, period, raw_json)
                    VALUES
                        (:ts, :symbol, :metric, :value, :value2, :period, CAST(:raw_json AS JSONB))
                    ON CONFLICT (metric, symbol, ts, period) DO UPDATE SET
                        value    = EXCLUDED.value,
                        value2   = EXCLUDED.value2,
                        raw_json = EXCLUDED.raw_json
                """),
                {
                    "ts": ts,
                    "symbol": symbol,
                    "metric": metric,
                    "value": value,
                    "value2": value2,
                    "period": period,
                    "raw_json": _j(raw),
                },
            )
    except Exception:
        log.exception("upsert_futures_metric error (metric=%s)", metric)


# ──────────────────────────────────────────────────────────────────────────────
# coinglass_liquidation_map
# ──────────────────────────────────────────────────────────────────────────────

def insert_coinglass_liq_map(
    engine: Engine,
    ts: datetime,
    symbol: str,
    exchange: str,
    timeframe: str,
    summary: dict,
    raw: dict,
) -> None:
    """Insert Coinglass liquidation map snapshot."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO coinglass_liquidation_map
                        (ts, symbol, exchange, timeframe, summary_json, raw_json)
                    VALUES
                        (:ts, :symbol, :exchange, :timeframe,
                         CAST(:summary_json AS JSONB), CAST(:raw_json AS JSONB))
                """),
                {
                    "ts": ts,
                    "symbol": symbol,
                    "exchange": exchange,
                    "timeframe": timeframe,
                    "summary_json": _j(summary),
                    "raw_json": _j(raw),
                },
            )
    except Exception:
        log.exception("insert_coinglass_liq_map error")
