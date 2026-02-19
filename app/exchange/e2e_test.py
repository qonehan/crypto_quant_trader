"""Step 10 E2E 검증 스크립트 — Upbit TEST 모드 end-to-end 확인.

키가 없으면 SKIP (exit 0).
키가 있으면:
  1) GET /v1/accounts
  2) GET /v1/orders/chance
  3) POST /v1/orders/test (BUY)
  4) (선택) POST /v1/orders/test (SELL) — BTC 잔고 있을 때만

성공 시: upbit_order_attempts에 mode='test', status='test_ok' 기록 (identifier 포함).
실패 시: status='error' 기록 후 exit 1.

실행:
  poetry run python -m app.exchange.e2e_test
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import load_settings
from app.db.session import get_engine
from app.db.writer import insert_upbit_order_attempt
from app.exchange.upbit_rest import UpbitApiError, UpbitRestClient, parse_remaining_req

_SEP = "=" * 60


def _meta_summary(client: UpbitRestClient) -> str:
    meta = client._last_call_meta
    parsed = meta.get("remaining_req_parsed", {}) or {}
    return (
        f"http={meta.get('http_status')}  "
        f"latency={meta.get('latency_ms')}ms  "
        f"remaining-req sec={parsed.get('sec')} min={parsed.get('min')}"
    )


def main() -> int:
    s = load_settings()

    if not s.UPBIT_ACCESS_KEY or not s.UPBIT_SECRET_KEY:
        print("ℹ️  UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 미설정 — E2E 검증 SKIP")
        print("   (shadow 모드에서는 실거래 API 호출이 없으므로 E2E 불필요)")
        return 0

    # Keys are set — mask for log
    ak_len = len(s.UPBIT_ACCESS_KEY)
    sk_len = len(s.UPBIT_SECRET_KEY)
    print(_SEP)
    print(f"E2E Test: {s.SYMBOL}  [{datetime.now(timezone.utc).isoformat()}]")
    print(f"  ACCESS_KEY length={ak_len}  SECRET_KEY length={sk_len}")
    print(f"  TRADE_MODE={s.UPBIT_TRADE_MODE}  ORDER_TEST_ENABLED={s.UPBIT_ORDER_TEST_ENABLED}")
    print(f"  E2E_TEST_ORDER_KRW={s.UPBIT_E2E_TEST_ORDER_KRW}")
    print(_SEP)

    engine = get_engine(s)
    client = UpbitRestClient(
        access_key=s.UPBIT_ACCESS_KEY,
        secret_key=s.UPBIT_SECRET_KEY,
        base_url=s.UPBIT_API_BASE,
        timeout=s.UPBIT_REST_TIMEOUT_SEC,
        max_retry=s.UPBIT_REST_MAX_RETRY,
    )

    # ── (1) GET /v1/accounts ─────────────────────────────────────────
    print("[1] GET /v1/accounts")
    try:
        accounts = client.get_accounts()
        print(f"  계좌 수: {len(accounts)}")
        coin_sym = s.SYMBOL.split("-")[1] if "-" in s.SYMBOL else "BTC"
        krw_bal: float = 0.0
        btc_bal: float = 0.0
        for a in accounts:
            cur = a.get("currency", "")
            bal = float(a.get("balance", 0) or 0)
            if cur == "KRW":
                krw_bal = bal
            elif cur == coin_sym:
                btc_bal = bal
            print(f"  currency={cur}  balance={bal}  locked={a.get('locked', 0)}")
        print(f"  {_meta_summary(client)}")
    except UpbitApiError as e:
        print(f"  ERROR: {e}  (http={e.http_status})")
        return 1
    except Exception as e:
        print(f"  ERROR: {e}")
        return 1

    # ── (2) GET /v1/orders/chance ────────────────────────────────────
    print(f"[2] GET /v1/orders/chance?market={s.SYMBOL}")
    try:
        chance = client.get_orders_chance(s.SYMBOL)
        bid_acc = chance.get("bid_account", {})
        ask_acc = chance.get("ask_account", {})
        print(
            f"  bid_fee={chance.get('bid_fee')}  ask_fee={chance.get('ask_fee')}"
        )
        print(
            f"  bid_available={bid_acc.get('balance')}  ask_available={ask_acc.get('balance')}"
        )
        print(f"  {_meta_summary(client)}")
    except Exception as e:
        print(f"  WARN: orders/chance error — {e} (continuing)")

    # ── (3) POST /v1/orders/test (BUY) ──────────────────────────────
    print(f"[3] POST /v1/orders/test (BUY) price={s.UPBIT_E2E_TEST_ORDER_KRW} KRW")
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    identifier_buy = f"e2e-{ts_str}-ORDER_TEST_BUY"
    test_ok = False
    exit_code = 0

    try:
        result = client.order_test(
            market=s.SYMBOL,
            side="bid",
            price=float(s.UPBIT_E2E_TEST_ORDER_KRW),
            ord_type="price",
            identifier=identifier_buy,
        )
        meta = client._last_call_meta
        http_st = meta.get("http_status")
        latency = meta.get("latency_ms")
        remaining_raw = meta.get("remaining_req")
        parsed = meta.get("remaining_req_parsed", {}) or {}

        print(f"  ✅ order_test OK: uuid={result.get('uuid')}  side={result.get('side')}")
        print(f"  ord_type={result.get('ord_type')}  state={result.get('state')}")
        print(
            f"  {_meta_summary(client)}"
        )

        # Write to DB: mode=test, status=test_ok
        row = {
            "ts": datetime.now(timezone.utc),
            "symbol": s.SYMBOL,
            "action": "E2E_ORDER_TEST_BUY",
            "mode": "test",
            "side": "bid",
            "ord_type": "price",
            "price": float(s.UPBIT_E2E_TEST_ORDER_KRW),
            "volume": None,
            "paper_trade_id": None,
            "response_json": result,
            "status": "test_ok",
            "error_msg": None,
            "uuid": result.get("uuid"),
            "identifier": identifier_buy,
            "request_json": {
                "market": s.SYMBOL,
                "side": "bid",
                "ord_type": "price",
                "price": str(s.UPBIT_E2E_TEST_ORDER_KRW),
                "identifier": identifier_buy,
            },
            "http_status": http_st,
            "latency_ms": latency,
            "remaining_req": remaining_raw,
            "retry_count": 0,
            "final_state": None,
            "executed_volume": None,
            "paid_fee": None,
            "avg_price": None,
        }
        attempt_id = insert_upbit_order_attempt(engine, row)
        print(f"  DB 기록 완료: attempt_id={attempt_id}  status=test_ok  identifier={identifier_buy}")
        test_ok = True

    except UpbitApiError as e:
        http_st = e.http_status
        remaining_raw = e.remaining_req
        err_msg = str(e)
        print(f"  ❌ order_test FAILED: {e}  (http={http_st})")
        # Write error to DB
        row_err = {
            "ts": datetime.now(timezone.utc),
            "symbol": s.SYMBOL,
            "action": "E2E_ORDER_TEST_BUY",
            "mode": "test",
            "side": "bid",
            "ord_type": "price",
            "price": float(s.UPBIT_E2E_TEST_ORDER_KRW),
            "volume": None,
            "paper_trade_id": None,
            "response_json": None,
            "status": "error",
            "error_msg": err_msg,
            "uuid": None,
            "identifier": identifier_buy,
            "request_json": {
                "market": s.SYMBOL,
                "side": "bid",
                "ord_type": "price",
                "price": str(s.UPBIT_E2E_TEST_ORDER_KRW),
                "identifier": identifier_buy,
            },
            "http_status": http_st,
            "latency_ms": None,
            "remaining_req": remaining_raw,
            "retry_count": 0,
            "final_state": None,
            "executed_volume": None,
            "paid_fee": None,
            "avg_price": None,
        }
        insert_upbit_order_attempt(engine, row_err)
        exit_code = 1
    except Exception as e:
        print(f"  ❌ order_test FAILED: {e}")
        exit_code = 1

    # ── (4) POST /v1/orders/test (SELL) — optional ──────────────────
    if btc_bal >= s.UPBIT_E2E_TEST_SELL_BTC and s.UPBIT_E2E_TEST_SELL_BTC > 0:
        print(
            f"[4] POST /v1/orders/test (SELL) volume={s.UPBIT_E2E_TEST_SELL_BTC} BTC"
            f"  (balance={btc_bal})"
        )
        ts_str2 = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        identifier_sell = f"e2e-{ts_str2}-ORDER_TEST_SELL"
        try:
            result_sell = client.order_test(
                market=s.SYMBOL,
                side="ask",
                volume=s.UPBIT_E2E_TEST_SELL_BTC,
                ord_type="market",
                identifier=identifier_sell,
            )
            meta_sell = client._last_call_meta
            print(
                f"  ✅ SELL order_test OK: uuid={result_sell.get('uuid')}  "
                f"state={result_sell.get('state')}"
            )
            print(f"  {_meta_summary(client)}")

            row_sell = {
                "ts": datetime.now(timezone.utc),
                "symbol": s.SYMBOL,
                "action": "E2E_ORDER_TEST_SELL",
                "mode": "test",
                "side": "ask",
                "ord_type": "market",
                "price": None,
                "volume": s.UPBIT_E2E_TEST_SELL_BTC,
                "paper_trade_id": None,
                "response_json": result_sell,
                "status": "test_ok",
                "error_msg": None,
                "uuid": result_sell.get("uuid"),
                "identifier": identifier_sell,
                "request_json": {
                    "market": s.SYMBOL,
                    "side": "ask",
                    "ord_type": "market",
                    "volume": str(s.UPBIT_E2E_TEST_SELL_BTC),
                    "identifier": identifier_sell,
                },
                "http_status": meta_sell.get("http_status"),
                "latency_ms": meta_sell.get("latency_ms"),
                "remaining_req": meta_sell.get("remaining_req"),
                "retry_count": 0,
                "final_state": None,
                "executed_volume": None,
                "paid_fee": None,
                "avg_price": None,
            }
            insert_upbit_order_attempt(engine, row_sell)
            print(f"  DB 기록 완료: status=test_ok  identifier={identifier_sell}")

        except UpbitApiError as e:
            print(f"  ⚠️  SELL order_test WARN: {e}  (http={e.http_status}) — non-fatal")
        except Exception as e:
            print(f"  ⚠️  SELL order_test WARN: {e} — non-fatal")
    else:
        print(
            f"[4] SELL order_test SKIP "
            f"(btc_balance={btc_bal} < threshold={s.UPBIT_E2E_TEST_SELL_BTC})"
        )

    # ── Summary ──────────────────────────────────────────────────────
    print(_SEP)
    if test_ok:
        # Verify DB record
        with engine.connect() as conn:
            cnt = conn.execute(
                text("""
                    SELECT count(*) FROM upbit_order_attempts
                    WHERE symbol = :sym AND mode = 'test' AND status = 'test_ok'
                """),
                {"sym": s.SYMBOL},
            ).scalar()
        print(f"✅ E2E 검증 완료. DB test_ok 건수: {cnt}")
    else:
        print("❌ E2E 검증 실패. 로그를 확인하세요.")
    print(_SEP)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
