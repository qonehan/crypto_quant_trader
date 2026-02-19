"""Upbit 계좌 스냅샷 및 실행 Runner — Step 9 안정화."""
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
from app.exchange.upbit_rest import UpbitApiError, UpbitRestClient, parse_remaining_req

# remaining-req.sec 임계값: 이 값 이하이면 API 호출을 스로틀
_THROTTLE_SEC_THRESHOLD = 1

# Step 9: Final statuses — DB upsert 후 이 상태이면 추가 처리 스킵
_FINAL_STATUSES = frozenset({"submitted", "done", "cancel", "test_ok", "logged"})

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

    def _is_throttled(self) -> bool:
        """Step 10: remaining-req.sec throttle guard for AccountRunner."""
        meta = self.client._last_call_meta
        parsed = meta.get("remaining_req_parsed")
        if not parsed:
            return False
        sec = parsed.get("sec")
        if sec is not None and sec <= _THROTTLE_SEC_THRESHOLD:
            log.warning(
                "AccountRunner throttled: remaining-req.sec=%d <= threshold=%d — skipping poll",
                sec, _THROTTLE_SEC_THRESHOLD,
            )
            return True
        return False

    def _account_freshness(self) -> tuple[bool, float | None]:
        """Step 10: Return (is_fresh, lag_sec) based on last snapshot timestamp."""
        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT ts FROM upbit_account_snapshots
                        WHERE symbol = :sym ORDER BY ts DESC LIMIT 1
                    """),
                    {"sym": self.settings.SYMBOL},
                ).fetchone()
            if row is None:
                return False, None
            ts = row.ts
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            lag = (datetime.now(timezone.utc) - ts).total_seconds()
            threshold = self.settings.UPBIT_ACCOUNT_POLL_SEC * 3
            return lag <= threshold, lag
        except Exception:
            return False, None

    def _poll_once(self) -> None:
        # Step 10: throttle guard — skip poll if remaining-req.sec is too low
        if self._is_throttled():
            return

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

        # Step 10: account freshness check (log for dashboard Ready signal)
        is_fresh, lag_sec = self._account_freshness()
        log.info(
            "UpbitAccountRunner: saved %d snapshots  remaining-req=%s  account_fresh=%s lag=%.1fs",
            len(accounts),
            self.client._last_call_meta.get("remaining_req", "n/a"),
            is_fresh,
            lag_sec if lag_sec is not None else -1.0,
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
        identifier = f"paper-{paper_trade_id}-{action}"

        # ── Step 9: App-level idempotency (optimisation before DB upsert) ─────
        if self._has_final_status(identifier, mode):
            log.debug("Idempotency skip: identifier=%s mode=%s", identifier, mode)
            return

        # ── Order parameters ─────────────────────────────────────────────────
        # ENTER_LONG: 시장가 매수(금액 기반, ord_type="price")
        # EXIT_LONG:  시장가 매도(수량 기반, ord_type="market")
        if action == "ENTER_LONG":
            side = "bid"
            ord_type = "price"
            # Step 11: use UPBIT_TEST_BUY_KRW for test, else derive from paper trade
            if mode == "test":
                order_price = float(self.settings.UPBIT_TEST_BUY_KRW)
            else:
                qty = trade.get("qty") or 0
                price = trade.get("price") or 0
                order_price = price * qty if price and qty else None
            order_volume = None
        else:  # EXIT_LONG
            side = "ask"
            ord_type = "market"
            # Use paper trade qty; fallback to UPBIT_TEST_SELL_BTC
            paper_qty = trade.get("qty") or 0
            order_volume = paper_qty if paper_qty > 0 else self.settings.UPBIT_TEST_SELL_BTC
            order_price = None

        # request_json (no secrets)
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

        # ── Step 11: collect blocked_reasons ──────────────────────────────────
        blocked_reasons = self._collect_blocked_reasons()

        # Base row fields shared across all status paths
        retry_count = self._get_next_retry_count(paper_trade_id, action)
        base_row: dict = {
            "ts": datetime.now(timezone.utc),
            "symbol": self.settings.SYMBOL,
            "action": action,
            "mode": mode,
            "side": side,
            "ord_type": ord_type,
            "price": order_price,
            "volume": order_volume,
            "paper_trade_id": paper_trade_id,
            "response_json": None,
            "error_msg": None,
            "uuid": None,
            "identifier": identifier,
            "request_json": request_json,
            "http_status": None,
            "latency_ms": None,
            "remaining_req": None,
            "retry_count": retry_count,
            "final_state": None,
            "executed_volume": None,
            "paid_fee": None,
            "avg_price": None,
            "blocked_reasons": blocked_reasons if blocked_reasons else None,
        }

        # ── THROTTLED: always takes priority ─────────────────────────────────
        if "THROTTLED" in blocked_reasons:
            log.warning("Throttled [%s]: remaining-req.sec too low, skipping", action)
            insert_upbit_order_attempt(self.engine, {**base_row, "status": "throttled"})
            return

        # ── SHADOW mode ───────────────────────────────────────────────────────
        if mode == "shadow":
            # Intentional shadow (UPBIT_TRADE_MODE=shadow or ORDER_TEST_ENABLED=False)?
            # → status="logged" (normal shadow operation)
            # Downgraded to shadow (user wanted test but missing keys/config)?
            # → status="blocked" so dashboard can show why
            intentional = (
                self.settings.UPBIT_TRADE_MODE == "shadow"
                or not self.settings.UPBIT_ORDER_TEST_ENABLED
            )
            if intentional:
                status = "logged"
                log.info(
                    "Shadow [%s]: side=%s ord_type=%s volume=%s price=%s (no API call)",
                    action, side, ord_type, order_volume, order_price,
                )
            else:
                # UPBIT_TRADE_MODE=test but downgraded (e.g. KEYS_MISSING)
                status = "blocked"
                reasons_str = ",".join(blocked_reasons)
                log.warning("Blocked [%s]: %s (downgraded to shadow)", action, reasons_str)
                base_row["error_msg"] = f"blocked: {reasons_str}"
            insert_upbit_order_attempt(self.engine, {**base_row, "status": status})
            return

        # ── TEST mode: runtime blocking checks ───────────────────────────────
        if mode == "test":
            # Runtime blocks (AUTO_TEST_DISABLED, PAPER_PROFILE_MISMATCH, DATA_LAG)
            runtime_blocks = [
                r for r in blocked_reasons
                if r not in ("KEYS_MISSING", "TEST_DISABLED")
            ]
            if runtime_blocks:
                reasons_str = ",".join(runtime_blocks)
                log.warning("Blocked test [%s]: %s", action, reasons_str)
                insert_upbit_order_attempt(self.engine, {
                    **base_row,
                    "status": "blocked",
                    "error_msg": f"blocked: {reasons_str}",
                })
                return

            # ── All clear: call POST /v1/orders/test ────────────────────────
            response_json: dict | None = None
            status = "test_ok"
            error_msg: str | None = None
            uuid: str | None = None
            http_status: int | None = None
            latency_ms: int | None = None
            remaining_req_raw: str | None = None

            try:
                log.info(
                    "Test [%s]: POST /v1/orders/test side=%s ord_type=%s price=%s vol=%s",
                    action, side, ord_type, order_price, order_volume,
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
                remaining_req_raw = meta.get("remaining_req")
                parsed = meta.get("remaining_req_parsed", {}) or {}
                log.info(
                    "Test OK [%s]: latency=%dms http=%s remaining-req sec=%s min=%s",
                    action, latency_ms or 0, http_status,
                    parsed.get("sec"), parsed.get("min"),
                )

            except UpbitApiError as e:
                error_msg = str(e)
                status = "error"
                http_status = e.http_status
                remaining_req_raw = e.remaining_req
                log.error("ShadowExecutionRunner API error [%s]: %s", action, e)
            except Exception as e:
                error_msg = str(e)
                status = "error"
                log.error("ShadowExecutionRunner error [%s]: %s", action, e)

            insert_upbit_order_attempt(self.engine, {
                **base_row,
                "response_json": response_json,
                "status": status,
                "error_msg": error_msg,
                "uuid": uuid,
                "http_status": http_status,
                "latency_ms": latency_ms,
                "remaining_req": remaining_req_raw,
                "blocked_reasons": None,  # clear on successful test
            })
            return

        # ── LIVE mode ─────────────────────────────────────────────────────────
        response_json = None
        status = "submitted"
        error_msg = None
        uuid = None
        http_status = None
        latency_ms = None
        remaining_req_raw = None

        try:
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
            remaining_req_raw = meta.get("remaining_req")
            log.info("Live order submitted: uuid=%s latency=%dms", uuid, latency_ms or 0)

        except UpbitApiError as e:
            error_msg = str(e)
            status = "error"
            http_status = e.http_status
            remaining_req_raw = e.remaining_req
            log.error("ShadowExecutionRunner API error [%s]: %s", action, e)
        except Exception as e:
            error_msg = str(e)
            status = "error"
            log.error("ShadowExecutionRunner error [%s]: %s", action, e)

        attempt_id = insert_upbit_order_attempt(self.engine, {
            **base_row,
            "response_json": response_json,
            "status": status,
            "error_msg": error_msg,
            "uuid": uuid,
            "http_status": http_status,
            "latency_ms": latency_ms,
            "remaining_req": remaining_req_raw,
            "blocked_reasons": None,
        })

        # Live mode: poll order until done/cancel
        if uuid and attempt_id is not None:
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
            # Step 9: poll timeout — mark with final_state="poll_timeout"
            log.warning(
                "Live order polling exhausted (poll_timeout): uuid=%s max_polls=%d",
                uuid, max_polls,
            )
            update_upbit_order_attempt_final(
                self.engine, attempt_id, "poll_timeout",
                executed_volume=executed_volume,
                paid_fee=paid_fee,
                avg_price=avg_price,
            )

    def _has_final_status(self, identifier: str, mode: str) -> bool:
        """Step 9: Check if a final-status record already exists for this (identifier, mode).

        Uses the unique index for fast lookup.
        """
        with self.engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT status FROM upbit_order_attempts
                    WHERE identifier = :ident AND mode = :mode
                    LIMIT 1
                """),
                {"ident": identifier, "mode": mode},
            ).fetchone()
        if row is None:
            return False
        return row.status in _FINAL_STATUSES

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

    def _is_throttled(self) -> bool:
        """Step 9: Return True if remaining-req.sec is at or below the throttle threshold."""
        meta = self.client._last_call_meta
        parsed = meta.get("remaining_req_parsed")
        if not parsed:
            return False  # No data yet — don't throttle
        sec = parsed.get("sec")
        if sec is None:
            return False
        if sec <= _THROTTLE_SEC_THRESHOLD:
            log.warning(
                "remaining-req throttle: sec=%d <= threshold=%d",
                sec, _THROTTLE_SEC_THRESHOLD,
            )
            return True
        return False

    def _collect_blocked_reasons(self) -> list[str]:
        """Step 11: Collect all reasons why a Upbit API call cannot be made right now.

        Checks config-level conditions (KEYS_MISSING, TEST_DISABLED, AUTO_TEST_DISABLED,
        PAPER_PROFILE_MISMATCH) and runtime conditions (THROTTLED, DATA_LAG).
        """
        reasons: list[str] = []
        s = self.settings

        # Config-level checks
        if not s.UPBIT_ACCESS_KEY or not s.UPBIT_SECRET_KEY:
            reasons.append("KEYS_MISSING")
        if s.UPBIT_TRADE_MODE != "test" or not s.UPBIT_ORDER_TEST_ENABLED:
            reasons.append("TEST_DISABLED")
        if not s.UPBIT_TEST_ON_PAPER_TRADES:
            reasons.append("AUTO_TEST_DISABLED")
        if s.PAPER_POLICY_PROFILE != s.UPBIT_TEST_REQUIRE_PAPER_PROFILE:
            reasons.append("PAPER_PROFILE_MISMATCH")

        # Runtime: remaining-req throttle
        if self._is_throttled():
            reasons.append("THROTTLED")

        # Runtime: DATA_LAG — market_1s freshness
        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text("SELECT ts FROM market_1s WHERE symbol=:sym ORDER BY ts DESC LIMIT 1"),
                    {"sym": s.SYMBOL},
                ).fetchone()
            if row is not None:
                ts = row.ts
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                lag = (datetime.now(timezone.utc) - ts).total_seconds()
                if lag > s.DATA_LAG_SEC_MAX:
                    reasons.append("DATA_LAG")
        except Exception:
            pass

        return reasons

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
