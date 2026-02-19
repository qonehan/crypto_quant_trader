"""Step 8 Smoke Test — Upbit REST API 연결 및 주문 테스트 확인.

키가 없으면 명확한 안내 후 종료코드 1.
키가 있으면:
  1) GET /v1/accounts        — 잔액 조회 + remaining-req 출력
  2) GET /v1/orders/chance   — 수수료/잔액 확인
  3) POST /v1/orders/test    — UPBIT_ORDER_TEST_ENABLED=true일 때만 실행

사용법:
  poetry run python -m app.exchange.smoke
"""
from __future__ import annotations

import sys

from app.config import load_settings
from app.exchange.upbit_rest import UpbitApiError, UpbitRestClient


def main() -> int:
    s = load_settings()

    if not s.UPBIT_ACCESS_KEY or not s.UPBIT_SECRET_KEY:
        print("❌ UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 미설정")
        print("   .env 파일에 키를 설정하거나, Shadow 모드로 bot을 실행하세요.")
        print("   (UPBIT_SHADOW_ENABLED=true 이면 API key 없이도 bot 동작 가능)")
        return 1

    client = UpbitRestClient(
        access_key=s.UPBIT_ACCESS_KEY,
        secret_key=s.UPBIT_SECRET_KEY,
        base_url=s.UPBIT_API_BASE,
        timeout=s.UPBIT_REST_TIMEOUT_SEC,
        max_retry=s.UPBIT_REST_MAX_RETRY,
    )

    results: dict[str, str] = {}

    # 1) 계좌 조회
    print("[1] GET /v1/accounts")
    try:
        accounts = client.get_accounts()
        meta = client._last_call_meta
        krw = next((a for a in accounts if a["currency"] == "KRW"), None)
        btc_sym = s.SYMBOL.split("-")[1] if "-" in s.SYMBOL else "BTC"
        coin = next((a for a in accounts if a["currency"] == btc_sym), None)
        if krw:
            print(f"  KRW  balance={krw['balance']}  locked={krw['locked']}")
        if coin:
            print(f"  {btc_sym}  balance={coin['balance']}  avg_buy={coin['avg_buy_price']}")
        print(f"  총 {len(accounts)}개 통화 확인")
        print(f"  latency={meta.get('latency_ms')}ms  http={meta.get('http_status')}")
        print(f"  remaining-req: {meta.get('remaining_req', 'n/a')}")
        results["get_accounts"] = "PASS"
    except UpbitApiError as e:
        print(f"  ERROR: {e}  (http={e.http_status})")
        results["get_accounts"] = "FAIL"
    except Exception as e:
        print(f"  ERROR: {e}")
        results["get_accounts"] = "FAIL"

    # 2) 주문 가능 정보
    print(f"[2] GET /v1/orders/chance?market={s.SYMBOL}")
    try:
        chance = client.get_orders_chance(s.SYMBOL)
        meta = client._last_call_meta
        print(
            f"  bid_fee={chance.get('bid_fee')}  ask_fee={chance.get('ask_fee')}  "
            f"market={chance.get('market', {}).get('id')}"
        )
        print(f"  latency={meta.get('latency_ms')}ms  remaining-req: {meta.get('remaining_req', 'n/a')}")
        results["get_orders_chance"] = "PASS"
    except UpbitApiError as e:
        print(f"  ERROR: {e}  (http={e.http_status})")
        results["get_orders_chance"] = "FAIL"
    except Exception as e:
        print(f"  ERROR: {e}")
        results["get_orders_chance"] = "FAIL"

    # 3) POST /v1/orders/test (dry-run — 실 주문 아님)
    print("[3] POST /v1/orders/test (dry-run, 실 주문 생성 없음)")
    if s.UPBIT_ORDER_TEST_ENABLED:
        try:
            # 최소 bid 금액 (5,000 KRW) 으로 테스트
            result = client.order_test(
                market=s.SYMBOL,
                side="bid",
                price=5000,
                ord_type="price",
                identifier="smoke-test-bid",
            )
            meta = client._last_call_meta
            print(f"  uuid={result.get('uuid')}  state={result.get('state')}")
            print(f"  latency={meta.get('latency_ms')}ms  http={meta.get('http_status')}")
            print(f"  remaining-req: {meta.get('remaining_req', 'n/a')}")
            results["order_test"] = "PASS"
        except UpbitApiError as e:
            print(f"  ERROR: {e}  (http={e.http_status})")
            results["order_test"] = "FAIL"
        except Exception as e:
            print(f"  ERROR: {e}")
            results["order_test"] = "FAIL"
    else:
        print("  SKIP (UPBIT_ORDER_TEST_ENABLED=false)")
        results["order_test"] = "SKIP"

    # Summary
    print()
    print("=" * 60)
    overall = all(v in ("PASS", "SKIP") for v in results.values())
    for k, v in results.items():
        icon = "✅" if v == "PASS" else ("⏭ " if v == "SKIP" else "❌")
        print(f"  {icon} {k}: {v}")
    print()
    print(f"  OVERALL: {'PASS ✅' if overall else 'FAIL ❌'}")
    print("=" * 60)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
