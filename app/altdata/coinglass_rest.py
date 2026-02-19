"""Coinglass REST polling collector.

Polls Pair Liquidation History every COINGLASS_POLL_SEC seconds.
Requires COINGLASS_API_KEY in env; skips gracefully if key is empty.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from app.altdata.writer import insert_coinglass_liq_map
from app.config import Settings
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

_MAX_RETRY = 3
_RETRY_BASE = 5.0


def _build_summary(raw: dict | list) -> dict:
    """Extract key summary stats from Coinglass liquidation payload."""
    summary: dict = {}
    try:
        # Handle various response shapes
        data = raw
        if isinstance(raw, dict):
            data = raw.get("data") or raw
        if isinstance(data, list):
            # Liquidation history: list of {t, longLiquidationUsd, shortLiquidationUsd}
            long_total = sum(
                float(r.get("longLiquidationUsd") or r.get("longLiq") or 0)
                for r in data
            )
            short_total = sum(
                float(r.get("shortLiquidationUsd") or r.get("shortLiq") or 0)
                for r in data
            )
            summary["long_liq_usd_total"] = long_total
            summary["short_liq_usd_total"] = short_total
            summary["row_count"] = len(data)
            if data:
                # Most recent entry
                last = data[-1]
                summary["last_long_liq"] = float(
                    last.get("longLiquidationUsd") or last.get("longLiq") or 0
                )
                summary["last_short_liq"] = float(
                    last.get("shortLiquidationUsd") or last.get("shortLiq") or 0
                )
        elif isinstance(data, dict):
            summary["keys"] = list(data.keys())[:10]
    except Exception:
        log.debug("summary extraction error", exc_info=True)
    return summary


class CoinglassRestPoller:
    """Polls Coinglass liquidation data periodically."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self._stop = False
        self.last_poll_ts: float = 0.0
        self.poll_count: int = 0
        self.enabled: bool = bool(settings.COINGLASS_API_KEY)

    async def run(self) -> None:
        s = self.settings
        if not self.enabled:
            log.info("CoinglassRestPoller: COINGLASS_API_KEY not set, skipping")
            return

        symbol = s.ALT_SYMBOL_COINGLASS
        poll_sec = s.COINGLASS_POLL_SEC
        base = s.COINGLASS_BASE
        headers = {
            "CG-API-KEY": s.COINGLASS_API_KEY,
            "coinglassSecret": s.COINGLASS_API_KEY,  # some endpoints use this header
        }

        async with httpx.AsyncClient(base_url=base, headers=headers) as client:
            while not self._stop:
                try:
                    now = datetime.now(timezone.utc)
                    await self._poll_liq_history(client, symbol, now)
                    self.last_poll_ts = time.time()
                    self.poll_count += 1
                    log.info(
                        "Coinglass poll #%d done (symbol=%s)", self.poll_count, symbol
                    )
                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("Coinglass poll error")
                await asyncio.sleep(poll_sec)

    async def _poll_liq_history(
        self, client: httpx.AsyncClient, symbol: str, now: datetime
    ) -> None:
        """Fetch liquidation history and store to DB."""
        # Try the public open-api endpoint for liquidation history
        endpoint = "/api/pro/v1/futures/liquidation/detail"
        params = {
            "symbol": symbol,
            "timeType": 1,   # 1h
            "limit": 12,
        }

        for attempt in range(_MAX_RETRY):
            try:
                resp = await client.get(endpoint, params=params, timeout=15.0)
                if resp.status_code == 200:
                    raw = resp.json()
                    summary = _build_summary(raw)
                    insert_coinglass_liq_map(
                        self.engine,
                        now,
                        symbol,
                        exchange="all",
                        timeframe="1h",
                        summary=summary,
                        raw=raw,
                    )
                    return
                if resp.status_code in (429, 418):
                    wait = _RETRY_BASE * (2 ** attempt)
                    log.warning(
                        "Coinglass rate limit %d, retry in %.1fs", resp.status_code, wait
                    )
                    await asyncio.sleep(wait)
                    continue
                log.warning(
                    "Coinglass HTTP %d body=%s", resp.status_code, resp.text[:200]
                )
                return
            except Exception as exc:
                wait = _RETRY_BASE * (2 ** attempt)
                log.warning("Coinglass request error (%s), retry in %.1fs", exc, wait)
                await asyncio.sleep(wait)

    def stop(self) -> None:
        self._stop = True
