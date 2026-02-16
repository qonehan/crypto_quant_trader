from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import websockets

from app.config import Settings

log = logging.getLogger(__name__)


class UpbitWsClient:
    """Async Upbit WebSocket client with auto-reconnect."""

    def __init__(self, settings: Settings, queue: asyncio.Queue) -> None:
        self.settings = settings
        self.queue = queue
        self._last_recv_ts: float = 0.0
        self._reconnect_count = 0
        self._error_count = 0
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._stop = False

    # ── public ────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect loop with exponential backoff."""
        attempt = 0
        while not self._stop:
            try:
                await self._connect_and_consume()
            except (
                websockets.ConnectionClosed,
                websockets.InvalidURI,
                websockets.InvalidHandshake,
                OSError,
            ) as exc:
                self._reconnect_count += 1
                backoff = min(
                    self.settings.UPBIT_RECONNECT_MAX_SEC,
                    self.settings.UPBIT_RECONNECT_MIN_SEC * (2 ** attempt),
                )
                log.warning(
                    "WS disconnected (%s), reconnect #%d in %.1fs",
                    exc,
                    self._reconnect_count,
                    backoff,
                )
                await asyncio.sleep(backoff)
                attempt += 1
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Unexpected WS error")
                self._error_count += 1
                await asyncio.sleep(self.settings.UPBIT_RECONNECT_MIN_SEC)
            else:
                attempt = 0  # reset on clean exit

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None:
            await self._ws.close()

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def error_count(self) -> int:
        return self._error_count

    # ── internal ──────────────────────────────────────────────

    async def _connect_and_consume(self) -> None:
        async with websockets.connect(
            self.settings.UPBIT_WS_URL,
            ping_interval=self.settings.UPBIT_PING_INTERVAL_SEC,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            log.info("WS connected to %s", self.settings.UPBIT_WS_URL)
            await self._subscribe(ws)
            self._last_recv_ts = time.time()

            watchdog = asyncio.create_task(self._watchdog(ws))
            try:
                await self._reader(ws)
            finally:
                watchdog.cancel()
                self._ws = None

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        s = self.settings
        symbol = s.SYMBOL
        ob_code = f"{symbol}.{s.UPBIT_ORDERBOOK_UNIT}"
        payload = [
            {"ticket": str(uuid.uuid4())},
            {"type": "ticker", "codes": [symbol]},
            {"type": "trade", "codes": [symbol]},
            {"type": "orderbook", "codes": [ob_code]},
            {"format": s.UPBIT_WS_FORMAT},
        ]
        await ws.send(json.dumps(payload))
        log.info("Subscribed: %s", payload)

    async def _reader(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            self._last_recv_ts = time.time()
            try:
                if isinstance(raw, bytes):
                    msg = json.loads(raw.decode("utf-8"))
                else:
                    msg = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                log.warning("Parse error, skipping message: %s", exc)
                self._error_count += 1
                continue

            # Upbit error frame
            if "error" in msg:
                log.warning("Upbit error: %s", msg["error"])
                self._error_count += 1
                continue

            event_type = msg.get("type")
            if event_type not in ("ticker", "trade", "orderbook"):
                continue

            event = {
                "event_type": event_type,
                "symbol": msg.get("code", self.settings.SYMBOL),
                "ts_exchange_ms": msg.get("trade_timestamp") or msg.get("timestamp"),
                "ts_recv": time.time(),
                "payload": msg,
            }
            await self.queue.put(event)

    async def _watchdog(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Close connection if no data received for too long."""
        timeout = self.settings.UPBIT_NO_MESSAGE_TIMEOUT_SEC
        while True:
            await asyncio.sleep(5)
            elapsed = time.time() - self._last_recv_ts
            if elapsed > timeout:
                log.warning("No message for %.0fs, forcing reconnect", elapsed)
                await ws.close()
                return
