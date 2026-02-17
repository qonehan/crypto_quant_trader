from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class MarketState:
    symbol: str = "KRW-BTC"
    last_update_ts: float = 0.0

    # per-stream timestamps (exchange ms)
    ticker_ts_ms: int | None = None
    trade_ts_ms: int | None = None
    orderbook_ts_ms: int | None = None

    # ticker
    last_price: float | None = None

    # orderbook
    best_bid: float | None = None
    best_ask: float | None = None
    mid: float | None = None
    spread: float | None = None

    # trade
    last_trade_price: float | None = None
    last_trade_volume: float | None = None
    last_trade_side: str | None = None  # "BID" / "ASK"

    # orderbook aggregates
    ob_top5_bid_size_sum: float | None = None
    ob_top5_ask_size_sum: float | None = None
    ob_imbalance_top5: float | None = None

    counters: dict = field(
        default_factory=lambda: {
            "ticker_count": 0,
            "trade_count": 0,
            "orderbook_count": 0,
            "error_count": 0,
            "reconnect_count": 0,
        }
    )

    # ── updaters ──────────────────────────────────────────────

    def update_ticker(self, msg: dict) -> None:
        self.last_price = msg.get("trade_price")
        self.ticker_ts_ms = msg.get("timestamp")
        self.last_update_ts = time.time()
        self.counters["ticker_count"] += 1

    def update_trade(self, msg: dict) -> None:
        self.last_trade_price = msg.get("trade_price")
        self.last_trade_volume = msg.get("trade_volume")
        self.last_trade_side = msg.get("ask_bid")
        self.trade_ts_ms = msg.get("trade_timestamp") or msg.get("timestamp")
        self.last_update_ts = time.time()
        self.counters["trade_count"] += 1

    def update_orderbook(self, msg: dict) -> None:
        units = msg.get("orderbook_units", [])
        if units:
            self.best_ask = units[0].get("ask_price")
            self.best_bid = units[0].get("bid_price")

        bid_sum = sum(u.get("bid_size", 0) for u in units)
        ask_sum = sum(u.get("ask_size", 0) for u in units)
        self.ob_top5_bid_size_sum = bid_sum
        self.ob_top5_ask_size_sum = ask_sum

        if self.best_bid is not None and self.best_ask is not None:
            self.mid = (self.best_bid + self.best_ask) / 2
            self.spread = self.best_ask - self.best_bid

        total = bid_sum + ask_sum
        self.ob_imbalance_top5 = (bid_sum - ask_sum) / total if total > 0 else 0.0

        self.orderbook_ts_ms = msg.get("timestamp")
        self.last_update_ts = time.time()
        self.counters["orderbook_count"] += 1

    def summary_line(self) -> str:
        mid_s = f"{self.mid:,.0f}" if self.mid else "N/A"
        spread_s = f"{self.spread:,.0f}" if self.spread is not None else "N/A"
        # spread in bps
        if self.spread is not None and self.mid and self.mid > 0:
            spd_bps = f"{10000 * self.spread / self.mid:.1f}"
        else:
            spd_bps = "N/A"
        tp = f"{self.last_trade_price:,.0f}" if self.last_trade_price else "N/A"
        tv = f"{self.last_trade_volume:.4f}" if self.last_trade_volume else "N/A"
        side = self.last_trade_side or "N/A"
        imb = f"{self.ob_imbalance_top5:+.3f}" if self.ob_imbalance_top5 is not None else "N/A"
        c = self.counters
        return (
            f"mid={mid_s} spd={spread_s}({spd_bps}bp) "
            f"trade={tp}/{tv}/{side} imb={imb} "
            f"T={c['ticker_count']} Tr={c['trade_count']} OB={c['orderbook_count']} "
            f"err={c['error_count']} reconn={c['reconnect_count']}"
        )
