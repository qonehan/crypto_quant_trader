"""Binance Futures WebSocket collector.

두 스트림을 각각 별도 커넥션으로 처리:
  - !markPrice@arr@1s  → binance_mark_price_1s
  - !forceOrder@arr    → binance_force_orders
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone

import websockets
from sqlalchemy.engine import Engine

from app.altdata.writer import insert_force_order, insert_mark_price
from app.config import Settings

log = logging.getLogger(__name__)

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 60.0


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter."""
    base = min(_RECONNECT_MAX, _RECONNECT_MIN * (2 ** attempt))
    return base * (0.5 + random.random() * 0.5)


class BinanceMarkPriceWs:
    """Streams !markPrice@arr@1s, stores BTCUSDT rows."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self._stop = False
        self.last_insert_ts: float = 0.0
        self.connected: bool = False
        self.insert_count: int = 0

    async def run(self) -> None:
        s = self.settings
        url = f"{s.BINANCE_FUTURES_WS_BASE}/{s.BINANCE_MARK_PRICE_STREAM}"
        attempt = 0
        while not self._stop:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self.connected = True
                    log.info("Binance markPrice WS connected: %s", url)
                    attempt = 0
                    async for raw in ws:
                        if self._stop:
                            break
                        try:
                            self._handle(raw)
                        except Exception:
                            log.exception("markPrice parse error, skipping")
                self.connected = False
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                delay = _backoff(attempt)
                log.warning(
                    "Binance markPrice WS error (%s), retry #%d in %.1fs",
                    exc, attempt, delay,
                )
                await asyncio.sleep(delay)
                attempt += 1

    def _handle(self, raw) -> None:
        if isinstance(raw, bytes):
            data = json.loads(raw.decode())
        else:
            data = json.loads(raw)

        # !markPrice@arr@1s sends a list
        items = data if isinstance(data, list) else [data]
        target = self.settings.ALT_SYMBOL_BINANCE.upper()

        for item in items:
            symbol = str(item.get("s") or item.get("symbol") or "")
            if symbol.upper() != target:
                continue

            et_ms = item.get("E") or item.get("eventTime")
            if et_ms:
                ts = datetime.fromtimestamp(int(et_ms) / 1000, tz=timezone.utc)
            else:
                ts = datetime.now(timezone.utc)

            insert_mark_price(self.engine, ts, symbol, item)
            self.last_insert_ts = time.time()
            self.insert_count += 1

    def stop(self) -> None:
        self._stop = True


class BinanceForceOrderWs:
    """Streams !forceOrder@arr, stores BTCUSDT liquidation events."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self._stop = False
        self.last_recv_ts: float = 0.0
        self.connected: bool = False
        self.event_count: int = 0

    async def run(self) -> None:
        s = self.settings
        url = f"{s.BINANCE_FUTURES_WS_BASE}/{s.BINANCE_FORCE_ORDER_STREAM}"
        attempt = 0
        while not self._stop:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self.connected = True
                    log.info("Binance forceOrder WS connected: %s", url)
                    attempt = 0
                    async for raw in ws:
                        if self._stop:
                            break
                        self.last_recv_ts = time.time()
                        try:
                            self._handle(raw)
                        except Exception:
                            log.exception("forceOrder parse error, skipping")
                self.connected = False
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                delay = _backoff(attempt)
                log.warning(
                    "Binance forceOrder WS error (%s), retry #%d in %.1fs",
                    exc, attempt, delay,
                )
                await asyncio.sleep(delay)
                attempt += 1

    def _handle(self, raw) -> None:
        if isinstance(raw, bytes):
            data = json.loads(raw.decode())
        else:
            data = json.loads(raw)

        # !forceOrder@arr may be list or single event wrapper
        # Binance sends: {"e":"forceOrder","E":...,"o":{...}}
        # or array of the above
        items = data if isinstance(data, list) else [data]
        target = self.settings.ALT_SYMBOL_BINANCE.upper()

        for item in items:
            # The order is nested under "o" key
            order = item.get("o") or item
            symbol = str(order.get("s") or order.get("symbol") or item.get("s") or "")
            if symbol.upper() != target:
                continue

            et_ms = item.get("E") or item.get("eventTime") or order.get("T")
            if et_ms:
                ts = datetime.fromtimestamp(int(et_ms) / 1000, tz=timezone.utc)
            else:
                ts = datetime.now(timezone.utc)

            insert_force_order(self.engine, ts, symbol, order)
            self.event_count += 1
            log.info("ForceOrder: %s side=%s qty=%s", symbol, order.get("S"), order.get("q"))

    def stop(self) -> None:
        self._stop = True
