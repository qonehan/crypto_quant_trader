"""Binance Futures REST polling collector.

Polls 4 metrics every BINANCE_POLL_SEC seconds:
  - open_interest
  - global_ls_ratio  (globalLongShortAccountRatio)
  - taker_ls_ratio   (takerlongshortRatio)
  - basis
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from app.altdata.writer import upsert_futures_metric
from app.config import Settings
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

_MAX_RETRY = 3
_RETRY_BASE = 2.0  # seconds


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> dict | list | None:
    """GET with retry/backoff on 429/418/5xx."""
    for attempt in range(_MAX_RETRY):
        try:
            t0 = time.time()
            resp = await client.get(url, params=params, timeout=10.0)
            latency_ms = int((time.time() - t0) * 1000)
            if resp.status_code == 200:
                log.debug("GET %s params=%s latency=%dms", url, params, latency_ms)
                return resp.json()
            if resp.status_code in (429, 418):
                wait = _RETRY_BASE * (2 ** attempt)
                log.warning("Rate limit %d on %s, retry in %.1fs", resp.status_code, url, wait)
                await asyncio.sleep(wait)
                continue
            log.warning("HTTP %d on %s", resp.status_code, url)
            return None
        except Exception as exc:
            wait = _RETRY_BASE * (2 ** attempt)
            log.warning("Request error (%s) %s, retry in %.1fs", exc, url, wait)
            await asyncio.sleep(wait)
    return None


def _ts_from_ms(ms_val) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms_val) / 1000, tz=timezone.utc)
    except Exception:
        return None


class BinanceFuturesRestPoller:
    """Polls Binance Futures REST endpoints periodically."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self._stop = False
        self.last_poll_ts: float = 0.0
        self.poll_count: int = 0

    async def run(self) -> None:
        s = self.settings
        base = s.BINANCE_FUTURES_REST_BASE
        symbol = s.ALT_SYMBOL_BINANCE
        period = s.BINANCE_METRIC_PERIOD
        poll_sec = s.BINANCE_POLL_SEC

        async with httpx.AsyncClient(base_url=base) as client:
            while not self._stop:
                try:
                    now = datetime.now(timezone.utc)
                    await self._poll_all(client, symbol, period, now)
                    self.last_poll_ts = time.time()
                    self.poll_count += 1
                    log.info(
                        "Binance REST poll #%d done (symbol=%s period=%s)",
                        self.poll_count, symbol, period,
                    )
                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("Binance REST poll error")
                await asyncio.sleep(poll_sec)

    async def _poll_all(
        self,
        client: httpx.AsyncClient,
        symbol: str,
        period: str,
        now: datetime,
    ) -> None:
        # Bucket ts to minute
        ts_bucket = now.replace(second=0, microsecond=0)

        # 1) Open Interest (snapshot)
        data = await _get(client, "/fapi/v1/openInterest", {"symbol": symbol})
        if data and isinstance(data, dict):
            value = float(data.get("openInterest") or 0) or None
            ts = _ts_from_ms(data.get("time")) or ts_bucket
            upsert_futures_metric(
                self.engine, ts, symbol, "open_interest", value, None, "snapshot", data
            )

        # 2) Global Long/Short Account Ratio
        # Use ts_bucket (poll time) as ts so lag reflects when we polled, not bucket age
        rows = await _get(
            client,
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": period, "limit": 2},
        )
        if rows and isinstance(rows, list) and len(rows) > 0:
            row = rows[-1]  # most recent
            long_acct = float(row.get("longAccount") or 0) or None
            short_acct = float(row.get("shortAccount") or 0) or None
            ls_ratio = float(row.get("longShortRatio") or 0) or None
            upsert_futures_metric(
                self.engine, ts_bucket, symbol, "global_ls_ratio", ls_ratio, long_acct, period, row
            )

        # 3) Taker Buy/Sell Volume Ratio
        rows = await _get(
            client,
            "/futures/data/takerlongshortRatio",
            {"symbol": symbol, "period": period, "limit": 2},
        )
        if rows and isinstance(rows, list) and len(rows) > 0:
            row = rows[-1]
            buy_vol = float(row.get("buySellRatio") or 0) or None
            sell_vol = float(row.get("sellVol") or 0) or None
            upsert_futures_metric(
                self.engine, ts_bucket, symbol, "taker_ls_ratio", buy_vol, sell_vol, period, row
            )

        # 4) Basis (uses "pair" param instead of "symbol")
        basis_params = {"pair": symbol, "contractType": "PERPETUAL", "period": period, "limit": 2}
        rows = await _get(client, "/futures/data/basis", basis_params)
        if rows and isinstance(rows, list) and len(rows) > 0:
            row = rows[-1]
            basis = float(row.get("basis") or 0) or None
            basis_rate = float(row.get("basisRate") or 0) or None
            upsert_futures_metric(
                self.engine, ts_bucket, symbol, "basis", basis, basis_rate, period, row
            )
        else:
            log.warning(
                "basis poll empty/None (symbol=%s period=%s): rows=%r — skipping this cycle",
                symbol, period, rows,
            )

    def stop(self) -> None:
        self._stop = True
