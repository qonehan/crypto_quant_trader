from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.db.writer import upsert_market_1s
from app.marketdata.state import MarketState

log = logging.getLogger(__name__)


class MarketResampler:
    def __init__(self, state: MarketState, engine: Engine) -> None:
        self.state = state
        self.engine = engine
        self._lock = threading.Lock()
        self._trade_count_delta = 0
        self._trade_volume_delta = 0.0

    def on_trade(self, volume: float) -> None:
        with self._lock:
            self._trade_count_delta += 1
            self._trade_volume_delta += volume

    def _snapshot_and_reset(self) -> tuple[int, float]:
        with self._lock:
            count, vol = self._trade_count_delta, self._trade_volume_delta
            self._trade_count_delta = 0
            self._trade_volume_delta = 0.0
        return count, vol

    async def run(self) -> None:
        # align to next second boundary
        now = time.time()
        next_ts = float(int(now) + 1)
        await asyncio.sleep(next_ts - now)

        while True:
            ts_utc = datetime.fromtimestamp(next_ts, tz=timezone.utc).replace(microsecond=0)
            trade_count, trade_vol = self._snapshot_and_reset()
            s = self.state

            row = {
                "ts": ts_utc,
                "symbol": s.symbol,
                "mid": s.mid,
                "bid": s.best_bid,
                "ask": s.best_ask,
                "spread": s.spread,
                "trade_count_1s": trade_count,
                "trade_volume_1s": trade_vol,
                "imbalance_top5": s.ob_imbalance_top5,
                "last_trade_price": s.last_trade_price,
                "last_trade_volume": s.last_trade_volume,
                "last_trade_side": s.last_trade_side,
                "ticker_ts_ms": s.ticker_ts_ms,
                "trade_ts_ms": s.trade_ts_ms,
                "orderbook_ts_ms": s.orderbook_ts_ms,
            }

            try:
                await asyncio.to_thread(upsert_market_1s, self.engine, row)
            except Exception:
                log.exception("Failed to upsert market_1s row ts=%s", ts_utc)

            next_ts += 1.0
            sleep_dur = next_ts - time.time()
            if sleep_dur > 0:
                await asyncio.sleep(sleep_dur)
