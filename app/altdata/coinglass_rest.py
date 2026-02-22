"""Coinglass REST polling collector.

Polls Pair Liquidation History every COINGLASS_POLL_SEC seconds.
COINGLASS_ENABLED=True 시 is_real_key() 검사 강제. False(기본)면 키 없을 때 SKIP.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from app.altdata.writer import insert_coinglass_call_status, insert_coinglass_liq_map
from app.config import Settings, is_real_key
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

_MAX_RETRY = 3
_RETRY_BASE = 5.0


def _build_summary(raw: dict | list) -> dict:
    """Extract key summary stats from Coinglass liquidation payload."""
    summary: dict = {}
    try:
        data = raw
        if isinstance(raw, dict):
            data = raw.get("data") or raw
        if isinstance(data, list):
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
        self.enabled: bool = is_real_key(settings.COINGLASS_API_KEY)
        self._last_warn_ts: float = 0.0  # anti-spam for key-invalid log

    async def run(self) -> None:
        s = self.settings
        if not self.enabled:
            raw_val = s.COINGLASS_API_KEY or ""
            if not raw_val.strip():
                reason = "COINGLASS_API_KEY 미설정(빈 값)"
            else:
                reason = f"COINGLASS_API_KEY 비정상(placeholder/너무 짧음): '{raw_val[:20]}'"
            if s.COINGLASS_ENABLED:
                log.error(
                    "CoinglassRestPoller: 설정 오류(COINGLASS_ENABLED=True) — %s. "
                    "COINGLASS_API_KEY에 실제 키를 입력하세요.",
                    reason,
                )
            else:
                log.warning("CoinglassRestPoller: %s → SKIP (COINGLASS_ENABLED=False).", reason)
            return

        symbol = s.ALT_SYMBOL_COINGLASS
        poll_sec = s.COINGLASS_POLL_SEC
        base = s.COINGLASS_BASE
        headers = {
            "CG-API-KEY": s.COINGLASS_API_KEY,
            "coinglassSecret": s.COINGLASS_API_KEY,
        }

        async with httpx.AsyncClient(base_url=base, headers=headers) as client:
            while not self._stop:
                try:
                    now = datetime.now(timezone.utc)
                    t_start = time.time()
                    ok, http_status, error_msg = await self._poll_liq_history(
                        client, symbol, now
                    )
                    latency_ms = int((time.time() - t_start) * 1000)
                    self.last_poll_ts = time.time()
                    self.poll_count += 1
                    insert_coinglass_call_status(
                        self.engine, now, ok, http_status, error_msg,
                        latency_ms, self.poll_count,
                    )
                    if ok:
                        log.info(
                            "Coinglass poll #%d done (symbol=%s latency=%dms)",
                            self.poll_count, symbol, latency_ms,
                        )
                    else:
                        log.warning(
                            "Coinglass poll #%d FAILED (http=%s err=%s)",
                            self.poll_count, http_status, (error_msg or "")[:100],
                        )
                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("Coinglass poll loop error")
                await asyncio.sleep(poll_sec)

    async def _poll_liq_history(
        self, client: httpx.AsyncClient, symbol: str, now: datetime
    ) -> tuple[bool, int | None, str | None]:
        """Fetch liquidation history. Returns (ok, http_status, error_msg)."""
        endpoint = "/api/pro/v1/futures/liquidation/detail"
        params = {
            "symbol": symbol,
            "timeType": 1,
            "limit": 12,
        }

        last_exc_msg: str | None = None
        for attempt in range(_MAX_RETRY):
            try:
                resp = await client.get(endpoint, params=params, timeout=15.0)
                if resp.status_code == 200:
                    raw = resp.json()
                    summary = _build_summary(raw)
                    insert_coinglass_liq_map(
                        self.engine, now, symbol,
                        exchange="all", timeframe="1h",
                        summary=summary, raw=raw,
                    )
                    return True, 200, None
                if resp.status_code in (429, 418):
                    wait = _RETRY_BASE * (2 ** attempt)
                    log.warning(
                        "Coinglass rate limit %d, retry in %.1fs", resp.status_code, wait
                    )
                    await asyncio.sleep(wait)
                    continue
                err_body = resp.text[:300]
                log.warning(
                    "Coinglass HTTP %d — endpoint=%s body=%s",
                    resp.status_code, endpoint, err_body,
                )
                return False, resp.status_code, err_body
            except Exception as exc:
                last_exc_msg = str(exc)
                wait = _RETRY_BASE * (2 ** attempt)
                log.warning("Coinglass request error (%s), retry in %.1fs", exc, wait)
                await asyncio.sleep(wait)

        return False, None, last_exc_msg or "max retries exceeded"

    def stop(self) -> None:
        self._stop = True
