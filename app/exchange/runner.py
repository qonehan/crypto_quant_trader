"""Upbit 계좌 스냅샷 및 Shadow 실행 Runner."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.writer import insert_upbit_account_snapshot, insert_upbit_order_attempt
from app.exchange.upbit_rest import UpbitRestClient

log = logging.getLogger(__name__)


class UpbitAccountRunner:
    """Upbit 계좌 잔액을 주기적으로 polling하여 DB에 스냅샷 저장.

    UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY가 설정된 경우에만 실행.
    """

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.client = UpbitRestClient(
            access_key=settings.UPBIT_ACCESS_KEY,
            secret_key=settings.UPBIT_SECRET_KEY,
            base_url=settings.UPBIT_API_BASE,
            timeout=settings.UPBIT_REST_TIMEOUT_SEC,
        )

    async def run(self) -> None:
        log.info("UpbitAccountRunner started (poll=%ds)", self.settings.UPBIT_ACCOUNT_POLL_SEC)
        while True:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._poll_once)
            except Exception as e:
                log.warning("UpbitAccountRunner poll error: %s", e)
            await asyncio.sleep(self.settings.UPBIT_ACCOUNT_POLL_SEC)

    def _poll_once(self) -> None:
        accounts = self.client.get_accounts()
        now = datetime.now(timezone.utc)
        for acct in accounts:
            avg_price = acct.get("avg_buy_price")
            row = {
                "ts": now,
                "symbol": self.settings.SYMBOL,
                "currency": acct.get("currency", ""),
                "balance": float(acct.get("balance", 0) or 0),
                "locked": float(acct.get("locked", 0) or 0),
                "avg_buy_price": float(avg_price) if avg_price else None,
                "avg_buy_price_modified": acct.get("avg_buy_price_modified"),
                "unit_currency": acct.get("unit_currency"),
                "raw_json": acct,  # pass dict; SQLAlchemy handles JSONB conversion
            }
            insert_upbit_account_snapshot(self.engine, row)
        log.info("UpbitAccountRunner: saved %d account snapshots", len(accounts))


class ShadowExecutionRunner:
    """Paper 거래를 shadow 실행하는 Runner.

    paper_trades 테이블에서 새 행을 감지하면:
      - shadow 모드 (기본): DB에 로깅만 (API 호출 없음)
      - test 모드 (UPBIT_ORDER_TEST_ENABLED=true): orders/chance dry-run
      - live 모드 (3중 안전장치 통과 시): 실제 주문 생성

    3중 안전장치:
      1. LIVE_TRADING_ENABLED=true
      2. UPBIT_TRADE_MODE=live
      3. LIVE_GUARD_PHRASE="I_CONFIRM_LIVE_TRADING"
    """

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.client = UpbitRestClient(
            access_key=settings.UPBIT_ACCESS_KEY,
            secret_key=settings.UPBIT_SECRET_KEY,
            base_url=settings.UPBIT_API_BASE,
            timeout=settings.UPBIT_REST_TIMEOUT_SEC,
        )
        self._last_seen_id: int = 0

    async def run(self) -> None:
        log.info(
            "ShadowExecutionRunner started (mode=%s live=%s)",
            self.settings.UPBIT_TRADE_MODE,
            self.settings.LIVE_TRADING_ENABLED,
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._init_cursor)

        while True:
            try:
                await loop.run_in_executor(None, self._process_new_trades)
            except Exception as e:
                log.warning("ShadowExecutionRunner error: %s", e)
            await asyncio.sleep(self.settings.DECISION_INTERVAL_SEC)

    def _init_cursor(self) -> None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT COALESCE(max(id), 0) FROM paper_trades WHERE symbol=:sym"),
                {"sym": self.settings.SYMBOL},
            ).fetchone()
            self._last_seen_id = row[0] if row else 0
        log.info("ShadowExecutionRunner cursor init: last_id=%d", self._last_seen_id)

    def _process_new_trades(self) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, action, reason, price, qty, fee_krw, t
                    FROM paper_trades
                    WHERE symbol=:sym AND id > :last_id
                    ORDER BY id ASC
                """),
                {"sym": self.settings.SYMBOL, "last_id": self._last_seen_id},
            ).fetchall()

        for row in rows:
            self._handle_trade(dict(row._mapping))
            self._last_seen_id = row.id

    def _handle_trade(self, trade: dict) -> None:
        action = trade["action"]
        if action not in ("ENTER_LONG", "EXIT_LONG"):
            return

        side = "bid" if action == "ENTER_LONG" else "ask"
        volume = trade.get("qty")
        price = trade.get("price")
        mode = self._determine_mode()
        response_json: dict = {}
        status = "logged"
        error_msg = None

        try:
            if mode == "shadow":
                log.info(
                    "Shadow order [%s]: side=%s volume=%.8f price=%.0f (no API call)",
                    action, side, volume or 0, price or 0,
                )
                status = "logged"

            elif mode == "test":
                log.info(
                    "Order test [%s]: side=%s volume=%.8f price=%.0f",
                    action, side, volume or 0, price or 0,
                )
                result = self.client.order_test(
                    market=self.settings.SYMBOL,
                    side=side,
                    volume=volume,
                    price=price,
                )
                response_json = result
                status = "test_ok"

            elif mode == "live":
                log.warning(
                    "LIVE order [%s]: side=%s volume=%.8f (LIVE_TRADING_ENABLED=True)",
                    action, side, volume or 0,
                )
                result = self.client.create_order(
                    market=self.settings.SYMBOL,
                    side=side,
                    volume=volume,
                    ord_type="market",
                )
                response_json = result
                status = "submitted"

        except Exception as e:
            error_msg = str(e)
            status = "error"
            log.error("ShadowExecutionRunner order error: %s", e)

        row = {
            "ts": datetime.now(timezone.utc),
            "symbol": self.settings.SYMBOL,
            "action": action,
            "mode": mode,
            "side": side,
            "ord_type": "market",
            "price": price,
            "volume": volume,
            "paper_trade_id": trade.get("id"),
            "response_json": response_json if response_json else None,  # dict for JSONB
            "status": status,
            "error_msg": error_msg,
        }
        insert_upbit_order_attempt(self.engine, row)

    def _determine_mode(self) -> str:
        s = self.settings
        if (
            s.LIVE_TRADING_ENABLED
            and s.UPBIT_TRADE_MODE == "live"
            and s.LIVE_GUARD_PHRASE == "I_CONFIRM_LIVE_TRADING"
        ):
            return "live"
        if s.UPBIT_ORDER_TEST_ENABLED:
            return "test"
        return "shadow"
