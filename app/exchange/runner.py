"""Upbit 계좌 스냅샷 및 실행 Runner — Step 8 업그레이드."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.writer import (
    insert_upbit_account_snapshot,
    insert_upbit_order_attempt,
    insert_upbit_order_snapshot,
    update_upbit_order_attempt_final,
    upsert_live_position,
)
from app.exchange.upbit_rest import UpbitApiError, UpbitRestClient

log = logging.getLogger(__name__)


class UpbitAccountRunner:
    """Upbit 계좌 잔액을 주기적으로 polling하여 DB에 스냅샷 저장.

    UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY가 설정된 경우에만 실행.
    오류 시 polling 간격을 최대 2배까지 자동 증가 (simple backoff).
    """

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.client = UpbitRestClient(
            access_key=settings.UPBIT_ACCESS_KEY,
            secret_key=settings.UPBIT_SECRET_KEY,
            base_url=settings.UPBIT_API_BASE,
            timeout=settings.UPBIT_REST_TIMEOUT_SEC,
            max_retry=settings.UPBIT_REST_MAX_RETRY,
        )
        self._poll_interval = settings.UPBIT_ACCOUNT_POLL_SEC

    async def run(self) -> None:
        log.info("UpbitAccountRunner started (poll=%ds)", self.settings.UPBIT_ACCOUNT_POLL_SEC)
        loop = asyncio.get_event_loop()
        while True:
            try:
                await loop.run_in_executor(None, self._poll_once)
                # Reset interval on success
                self._poll_interval = self.settings.UPBIT_ACCOUNT_POLL_SEC
            except Exception as e:
                log.warning("UpbitAccountRunner poll error: %s", e)
                # Backoff: up to 2x base interval
                self._poll_interval = min(
                    self._poll_interval * 2,
                    self.settings.UPBIT_ACCOUNT_POLL_SEC * 2,
                )
                log.info("UpbitAccountRunner backoff: next poll in %ds", self._poll_interval)
            await asyncio.sleep(self._poll_interval)

    def _poll_once(self) -> None:
        accounts = self.client.get_accounts()
        now = datetime.now(timezone.utc)

        coin_sym = (
            self.settings.SYMBOL.split("-")[1]
            if "-" in self.settings.SYMBOL
            else "BTC"
        )
        krw_acct = next((a for a in accounts if a.get("currency") == "KRW"), None)
        coin_acct = next((a for a in accounts if a.get("currency") == coin_sym), None)

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
                "raw_json": acct,
            }
            insert_upbit_account_snapshot(self.engine, row)

        # Upsert live_positions summary
        if krw_acct is not None or coin_acct is not None:
            krw_bal = float(krw_acct.get("balance", 0) or 0) if krw_acct else None
            btc_bal = float(coin_acct.get("balance", 0) or 0) if coin_acct else None
            avg_buy = None
            if coin_acct:
                raw_avg = coin_acct.get("avg_buy_price")
                avg_buy = float(raw_avg) if raw_avg else None
            pos_status = "LONG" if (btc_bal and btc_bal > 0) else "FLAT"
            upsert_live_position(self.engine, {
                "symbol": self.settings.SYMBOL,
                "ts": now,
                "krw_balance": krw_bal,
                "btc_balance": btc_bal,
                "btc_avg_buy_price": avg_buy,
                "position_status": pos_status,
            })

        log.info(
            "UpbitAccountRunner: saved %d account snapshots  remaining-req=%s",
            len(accounts),
            self.client._last_call_meta.get("remaining_req", "n/a"),
        )


class ShadowExecutionRunner:
    """Paper 거래를 실행하는 Runner — Step 8 업그레이드.

    paper_trades 테이블에서 새 행을 감지하면:
      - shadow 모드 (기본): DB에 로깅만 (API 호출 없음)
      - test 모드 (UPBIT_ORDER_TEST_ENABLED=true): POST /v1/orders/test
      - live 모드 (4중 안전장치 통과 시): POST /v1/orders → uuid 폴링 → snapshots 저장

    4중 안전장치 (모두 true여야 live 허용):
      1. LIVE_TRADING_ENABLED=true
      2. UPBIT_TRADE_MODE=live
      3. LIVE_GUARD_PHRASE="I_CONFIRM_LIVE_TRADING"
      4. PAPER_POLICY_PROFILE != "test"  (test 프로필일 때 live 금지)

    Idempotency:
      - identifier = f"paper-{paper_trade_id}-{action}"
      - 동일 paper_trade_id + action 조합으로 이미 (submitted/done/test_ok/logged) 상태가
        있으면 재시도 금지.
      - error 상태이면 retry_count < UPBIT_REST_MAX_RETRY 일 때만 재시도.
    """

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.client = UpbitRestClient(
            access_key=settings.UPBIT_ACCESS_KEY,
            secret_key=settings.UPBIT_SECRET_KEY,
            base_url=settings.UPBIT_API_BASE,
            timeout=settings.UPBIT_REST_TIMEOUT_SEC,
            max_retry=settings.UPBIT_REST_MAX_RETRY,
        )
        self._last_seen_id: int = 0

    async def run(self) -> None:
        mode = self._determine_mode()
        log.info(
            "ShadowExecutionRunner started (effective_mode=%s live_enabled=%s trade_mode=%s)",
            mode,
            self.settings.LIVE_TRADING_ENABLED,
            self.settings.UPBIT_TRADE_MODE,
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
                    SELECT id, action, reason, price, qty, fee_krw, t, cash_after
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

        paper_trade_id = trade["id"]
        mode = self._determine_mode()

        # ── Idempotency check ──────────────────────────────────
        skip_status = self._check_idempotency(paper_trade_id, action)
        if skip_status is not None:
            log.debug(
                "Idempotency skip: paper_trade_id=%d action=%s existing=%s",
                paper_trade_id, action, skip_status,
            )
            return

        retry_count = self._get_next_retry_count(paper_trade_id, action)
        identifier = f"paper-{paper_trade_id}-{action}"

        # ── Order parameters ───────────────────────────────────
        # ENTER_LONG: 시장가 매수(금액 기반, ord_type="price")
        # EXIT_LONG:  시장가 매도(수량 기반, ord_type="market")
        if action == "ENTER_LONG":
            side = "bid"
            ord_type = "price"
            # Use cash_after context to estimate KRW amount; fallback to price*qty
            qty = trade.get("qty") or 0
            price = trade.get("price") or 0
            order_price = price * qty if price and qty else None
            order_volume = None
        else:  # EXIT_LONG
            side = "ask"
            ord_type = "market"
            order_volume = trade.get("qty")
            order_price = None

        # request_json (민감정보 제외)
        request_json: dict = {
            "market": self.settings.SYMBOL,
            "side": side,
            "ord_type": ord_type,
            "identifier": identifier,
        }
        if order_price is not None:
            request_json["price"] = str(int(order_price))
        if order_volume is not None:
            request_json["volume"] = str(order_volume)

        # ── Execute by mode ────────────────────────────────────
        response_json: dict | None = None
        status = "logged"
        error_msg: str | None = None
        uuid: str | None = None
        http_status: int | None = None
        latency_ms: int | None = None
        remaining_req: str | None = None
        attempt_id: int | None = None

        try:
            if mode == "shadow":
                log.info(
                    "Shadow [%s]: side=%s ord_type=%s volume=%s price=%s (no API call)",
                    action, side, ord_type, order_volume, order_price,
                )
                status = "logged"

            elif mode == "test":
                log.info(
                    "Test [%s]: POST /v1/orders/test side=%s ord_type=%s",
                    action, side, ord_type,
                )
                result = self.client.order_test(
                    market=self.settings.SYMBOL,
                    side=side,
                    volume=order_volume,
                    price=order_price,
                    ord_type=ord_type,
                    identifier=identifier,
                )
                meta = self.client._last_call_meta
                response_json = result
                status = "test_ok"
                http_status = meta.get("http_status")
                latency_ms = meta.get("latency_ms")
                remaining_req = meta.get("remaining_req")
                log.info(
                    "Test OK: latency=%dms http=%s remaining-req=%s",
                    latency_ms or 0, http_status, remaining_req,
                )

            elif mode == "live":
                log.warning(
                    "LIVE [%s]: POST /v1/orders side=%s ord_type=%s (LIVE_TRADING_ENABLED=True)",
                    action, side, ord_type,
                )
                result = self.client.create_order(
                    market=self.settings.SYMBOL,
                    side=side,
                    volume=order_volume,
                    price=order_price,
                    ord_type=ord_type,
                    identifier=identifier,
                )
                meta = self.client._last_call_meta
                response_json = result
                status = "submitted"
                uuid = result.get("uuid")
                http_status = meta.get("http_status")
                latency_ms = meta.get("latency_ms")
                remaining_req = meta.get("remaining_req")
                log.info("Live order submitted: uuid=%s latency=%dms", uuid, latency_ms or 0)

        except UpbitApiError as e:
            error_msg = str(e)
            status = "error"
            http_status = e.http_status
            remaining_req = e.remaining_req
            log.error("ShadowExecutionRunner API error [%s]: %s", action, e)
        except Exception as e:
            error_msg = str(e)
            status = "error"
            log.error("ShadowExecutionRunner error [%s]: %s", action, e)

        row = {
            "ts": datetime.now(timezone.utc),
            "symbol": self.settings.SYMBOL,
            "action": action,
            "mode": mode,
            "side": side,
            "ord_type": ord_type,
            "price": order_price,
            "volume": order_volume,
            "paper_trade_id": paper_trade_id,
            "response_json": response_json,
            "status": status,
            "error_msg": error_msg,
            # Step 8 extended columns
            "uuid": uuid,
            "identifier": identifier,
            "request_json": request_json,
            "http_status": http_status,
            "latency_ms": latency_ms,
            "remaining_req": remaining_req,
            "retry_count": retry_count,
            "final_state": None,
            "executed_volume": None,
            "paid_fee": None,
            "avg_price": None,
        }
        attempt_id = insert_upbit_order_attempt(self.engine, row)

        # Live mode: poll order until done/cancel
        if mode == "live" and uuid and attempt_id is not None:
            self._poll_live_order(attempt_id, uuid)

    def _poll_live_order(self, attempt_id: int, uuid: str) -> None:
        """Poll live order status and save snapshots until done/cancel."""
        max_polls = self.settings.LIVE_ORDER_MAX_POLLS
        poll_interval = self.settings.LIVE_ORDER_POLL_INTERVAL_SEC
        final_state: str | None = None
        executed_volume: float | None = None
        paid_fee: float | None = None
        avg_price: float | None = None

        for poll_n in range(max_polls):
            time.sleep(poll_interval)
            try:
                result = self.client.get_order(uuid)
                now = datetime.now(timezone.utc)
                state = result.get("state")

                def _safe_float(v: Any) -> float | None:
                    try:
                        return float(v) if v is not None else None
                    except (TypeError, ValueError):
                        return None

                snap_row = {
                    "ts": now,
                    "symbol": self.settings.SYMBOL,
                    "uuid": uuid,
                    "state": state,
                    "side": result.get("side"),
                    "ord_type": result.get("ord_type"),
                    "price": _safe_float(result.get("price")),
                    "volume": _safe_float(result.get("volume")),
                    "remaining_volume": _safe_float(result.get("remaining_volume")),
                    "executed_volume": _safe_float(result.get("executed_volume")),
                    "paid_fee": _safe_float(result.get("paid_fee")),
                    "raw_json": result,
                }
                insert_upbit_order_snapshot(self.engine, snap_row)
                log.info("Live order poll %d/%d uuid=%s state=%s", poll_n + 1, max_polls, uuid, state)

                if state in ("done", "cancel"):
                    final_state = state
                    executed_volume = snap_row["executed_volume"]
                    paid_fee = snap_row["paid_fee"]
                    if snap_row["executed_volume"] and snap_row["volume"]:
                        vol = snap_row["executed_volume"]
                        pr = snap_row["price"]
                        avg_price = float(pr) if pr else None
                    break

            except Exception as e:
                log.warning("get_order poll error (poll=%d uuid=%s): %s", poll_n, uuid, e)

        if final_state:
            update_upbit_order_attempt_final(
                self.engine, attempt_id, final_state,
                executed_volume=executed_volume,
                paid_fee=paid_fee,
                avg_price=avg_price,
            )
            log.info("Live order finalized: uuid=%s final_state=%s", uuid, final_state)
        else:
            log.warning("Live order polling exhausted: uuid=%s (max_polls=%d)", uuid, max_polls)

    def _check_idempotency(self, paper_trade_id: int, action: str) -> str | None:
        """Return existing status if we should skip, None if we should proceed."""
        with self.engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT status, COALESCE(retry_count, 0) AS retry_count
                    FROM upbit_order_attempts
                    WHERE paper_trade_id = :ptid AND action = :action
                    ORDER BY ts DESC
                    LIMIT 1
                """),
                {"ptid": paper_trade_id, "action": action},
            ).fetchone()

        if row is None:
            return None  # No existing attempt — proceed

        status = row.status
        retry_count = row.retry_count

        if status in ("submitted", "done", "test_ok", "logged"):
            return status  # Already succeeded — skip

        if status == "error" and retry_count < self.settings.UPBIT_REST_MAX_RETRY:
            return None  # Allow retry

        return status  # Too many errors or unknown — skip

    def _get_next_retry_count(self, paper_trade_id: int, action: str) -> int:
        """Return retry_count value to use for the next attempt."""
        with self.engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT COALESCE(retry_count, 0) + 1
                    FROM upbit_order_attempts
                    WHERE paper_trade_id = :ptid AND action = :action
                    ORDER BY ts DESC
                    LIMIT 1
                """),
                {"ptid": paper_trade_id, "action": action},
            ).fetchone()
        return row[0] if row else 0

    def _determine_mode(self) -> str:
        s = self.settings
        # 4-layer live guard
        if (
            s.LIVE_TRADING_ENABLED
            and s.UPBIT_TRADE_MODE == "live"
            and s.LIVE_GUARD_PHRASE == "I_CONFIRM_LIVE_TRADING"
            and s.PAPER_POLICY_PROFILE != "test"  # test 프로필에서 live 금지
        ):
            # Also require API keys
            if s.UPBIT_ACCESS_KEY and s.UPBIT_SECRET_KEY:
                return "live"
            log.warning("Live conditions met but API keys missing — downgrading to shadow")
            return "shadow"

        if s.UPBIT_ORDER_TEST_ENABLED:
            # Require API keys for test mode
            if s.UPBIT_ACCESS_KEY and s.UPBIT_SECRET_KEY:
                return "test"
            log.info("UPBIT_ORDER_TEST_ENABLED=true but no API keys — downgrading to shadow")
            return "shadow"

        return "shadow"
