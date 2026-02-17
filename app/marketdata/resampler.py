from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import Engine

from app.db.writer import upsert_market_1s
from app.marketdata.state import MarketState

log = logging.getLogger(__name__)


@dataclass
class QuoteBar:
    bid_open: float | None = None
    bid_high: float | None = None
    bid_low: float | None = None
    bid_close: float | None = None
    ask_open: float | None = None
    ask_high: float | None = None
    ask_low: float | None = None
    ask_close: float | None = None
    imb_notional_top5_last: float | None = None
    quote_count: int = 0

    def update(self, bid: float, ask: float, imb_notional_top5: float | None) -> None:
        # bid OHLC
        if self.bid_open is None:
            self.bid_open = bid
        self.bid_high = max(self.bid_high, bid) if self.bid_high is not None else bid
        self.bid_low = min(self.bid_low, bid) if self.bid_low is not None else bid
        self.bid_close = bid

        # ask OHLC
        if self.ask_open is None:
            self.ask_open = ask
        self.ask_high = max(self.ask_high, ask) if self.ask_high is not None else ask
        self.ask_low = min(self.ask_low, ask) if self.ask_low is not None else ask
        self.ask_close = ask

        if imb_notional_top5 is not None:
            self.imb_notional_top5_last = imb_notional_top5
        self.quote_count += 1


class MarketResampler:
    def __init__(self, state: MarketState, engine: Engine) -> None:
        self.state = state
        self.engine = engine
        self._lock = threading.Lock()
        self._trade_count_delta = 0
        self._trade_volume_delta = 0.0
        self._quote_bars: dict[datetime, QuoteBar] = {}

    def on_trade(self, volume: float) -> None:
        with self._lock:
            self._trade_count_delta += 1
            self._trade_volume_delta += volume

    def on_quote(
        self,
        bid: float,
        ask: float,
        imb_notional_top5: float | None,
    ) -> None:
        now_utc = datetime.now(timezone.utc)
        if now_utc.microsecond == 0:
            bar_end_ts = now_utc
        else:
            bar_end_ts = now_utc.replace(microsecond=0) + timedelta(seconds=1)

        with self._lock:
            bar = self._quote_bars.get(bar_end_ts)
            if bar is None:
                bar = QuoteBar()
                self._quote_bars[bar_end_ts] = bar
            bar.update(bid, ask, imb_notional_top5)

            # memory safety: remove keys older than 10 seconds
            cutoff = bar_end_ts - timedelta(seconds=10)
            stale = [k for k in self._quote_bars if k < cutoff]
            for k in stale:
                del self._quote_bars[k]

    def _snapshot_and_reset(self, flush_ts: datetime) -> tuple[int, float, QuoteBar | None]:
        with self._lock:
            count, vol = self._trade_count_delta, self._trade_volume_delta
            self._trade_count_delta = 0
            self._trade_volume_delta = 0.0
            qbar = self._quote_bars.pop(flush_ts, None)
        return count, vol, qbar

    async def run(self) -> None:
        # align to next second boundary
        now = time.time()
        next_ts = float(int(now) + 1)
        await asyncio.sleep(next_ts - now)

        while True:
            ts_utc = datetime.fromtimestamp(next_ts, tz=timezone.utc).replace(microsecond=0)
            trade_count, trade_vol, qbar = self._snapshot_and_reset(ts_utc)
            s = self.state

            # If no QuoteBar from orderbook ticks, fallback to MarketState snapshot
            if qbar is None and s.best_bid is not None and s.best_ask is not None:
                qbar = QuoteBar(
                    bid_open=s.best_bid,
                    bid_high=s.best_bid,
                    bid_low=s.best_bid,
                    bid_close=s.best_bid,
                    ask_open=s.best_ask,
                    ask_high=s.best_ask,
                    ask_low=s.best_ask,
                    ask_close=s.best_ask,
                    imb_notional_top5_last=None,
                    quote_count=0,
                )

            # Compute derived fields from QuoteBar
            bid_close = qbar.bid_close if qbar else None
            ask_close = qbar.ask_close if qbar else None

            mid_close = None
            spread_val = None
            spread_bps = None
            if bid_close is not None and ask_close is not None:
                mid_close = (bid_close + ask_close) / 2
                spread_val = ask_close - bid_close
                if mid_close > 0:
                    spread_bps = 10000 * spread_val / mid_close

            row = {
                "ts": ts_utc,
                "symbol": s.symbol,
                "mid": mid_close if mid_close is not None else s.mid,
                "bid": bid_close if bid_close is not None else s.best_bid,
                "ask": ask_close if ask_close is not None else s.best_ask,
                "spread": spread_val if spread_val is not None else s.spread,
                "trade_count_1s": trade_count,
                "trade_volume_1s": trade_vol,
                "imbalance_top5": s.ob_imbalance_top5,
                "last_trade_price": s.last_trade_price,
                "last_trade_volume": s.last_trade_volume,
                "last_trade_side": s.last_trade_side,
                "ticker_ts_ms": s.ticker_ts_ms,
                "trade_ts_ms": s.trade_ts_ms,
                "orderbook_ts_ms": s.orderbook_ts_ms,
                # v1 OHLC columns
                "bid_open_1s": qbar.bid_open if qbar else None,
                "bid_high_1s": qbar.bid_high if qbar else None,
                "bid_low_1s": qbar.bid_low if qbar else None,
                "bid_close_1s": qbar.bid_close if qbar else None,
                "ask_open_1s": qbar.ask_open if qbar else None,
                "ask_high_1s": qbar.ask_high if qbar else None,
                "ask_low_1s": qbar.ask_low if qbar else None,
                "ask_close_1s": qbar.ask_close if qbar else None,
                "spread_bps": spread_bps,
                "imb_notional_top5": qbar.imb_notional_top5_last if qbar else None,
                "mid_close_1s": mid_close,
            }

            try:
                await asyncio.to_thread(upsert_market_1s, self.engine, row)
            except Exception:
                log.exception("Failed to upsert market_1s row ts=%s", ts_utc)

            next_ts += 1.0
            sleep_dur = next_ts - time.time()
            if sleep_dur > 0:
                await asyncio.sleep(sleep_dur)
