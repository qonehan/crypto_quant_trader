"""Alt Data runner: orchestrates Binance WS + REST + Coinglass into async tasks."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.engine import Engine

from app.altdata.binance_rest import BinanceFuturesRestPoller
from app.altdata.binance_ws import BinanceForceOrderWs, BinanceMarkPriceWs
from app.altdata.coinglass_rest import CoinglassRestPoller
from app.config import Settings

log = logging.getLogger(__name__)


class BinanceAltDataRunner:
    """Runs all Binance alt-data collectors as async sub-tasks."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.mark_price_ws = BinanceMarkPriceWs(settings, engine)
        self.force_order_ws = BinanceForceOrderWs(settings, engine)
        self.rest_poller = BinanceFuturesRestPoller(settings, engine)

    async def run(self) -> None:
        log.info("BinanceAltDataRunner started (symbol=%s)", self.settings.ALT_SYMBOL_BINANCE)
        try:
            await asyncio.gather(
                self.mark_price_ws.run(),
                self.force_order_ws.run(),
                self.rest_poller.run(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            self.mark_price_ws.stop()
            self.force_order_ws.stop()
            self.rest_poller.stop()
            log.info("BinanceAltDataRunner stopped")

    # Expose internal state for diagnostics
    @property
    def mark_price_last_insert_ts(self) -> float:
        return self.mark_price_ws.last_insert_ts

    @property
    def mark_price_insert_count(self) -> int:
        return self.mark_price_ws.insert_count

    @property
    def force_order_connected(self) -> bool:
        return self.force_order_ws.connected

    @property
    def rest_poll_count(self) -> int:
        return self.rest_poller.poll_count


class CoinglassAltDataRunner:
    """Runs Coinglass REST poller as an async task."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.poller = CoinglassRestPoller(settings, engine)

    async def run(self) -> None:
        log.info(
            "CoinglassAltDataRunner started (key_set=%s)",
            bool(self.settings.COINGLASS_API_KEY),
        )
        try:
            await self.poller.run()
        except asyncio.CancelledError:
            pass
        finally:
            self.poller.stop()
            log.info("CoinglassAltDataRunner stopped")
