"""Step 7 Smoke Test — Upbit REST API 연결 및 계좌 조회 확인.

사용법:
  poetry run python -m app.exchange.smoke
"""
from __future__ import annotations

import sys

from app.config import load_settings
from app.exchange.upbit_rest import UpbitRestClient


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
    )

    results: dict[str, str] = {}

    # 1) 계좌 조회
    print("[1] GET /v1/accounts")
    try:
        accounts = client.get_accounts()
        krw = next((a for a in accounts if a["currency"] == "KRW"), None)
        btc_sym = s.SYMBOL.split("-")[1] if "-" in s.SYMBOL else "BTC"
        coin = next((a for a in accounts if a["currency"] == btc_sym), None)
        if krw:
            print(f"  KRW  balance={krw['balance']}  locked={krw['locked']}")
        if coin:
            print(f"  {btc_sym}  balance={coin['balance']}  avg_buy={coin['avg_buy_price']}")
        print(f"  총 {len(accounts)}개 통화 확인")
        results["get_accounts"] = "PASS"
    except Exception as e:
        print(f"  ERROR: {e}")
        results["get_accounts"] = "FAIL"

    # 2) 주문 가능 정보
    print(f"[2] GET /v1/orders/chance?market={s.SYMBOL}")
    try:
        chance = client.get_orders_chance(s.SYMBOL)
        print(
            f"  bid_fee={chance.get('bid_fee')}  ask_fee={chance.get('ask_fee')}  "
            f"market={chance.get('market', {}).get('id')}"
        )
        results["get_orders_chance"] = "PASS"
    except Exception as e:
        print(f"  ERROR: {e}")
        results["get_orders_chance"] = "FAIL"

    # 3) order_test (dry-run)
    print("[3] order_test (dry-run via orders/chance)")
    if s.UPBIT_ORDER_TEST_ENABLED:
        try:
            result = client.order_test(market=s.SYMBOL, side="bid", volume=0.0001)
            print(f"  status={result['status']}  chance_checked={result['chance_checked']}")
            results["order_test"] = "PASS"
        except Exception as e:
            print(f"  ERROR: {e}")
            results["order_test"] = "FAIL"
    else:
        print("  SKIP (UPBIT_ORDER_TEST_ENABLED=false)")
        results["order_test"] = "SKIP"

    # Summary
    print()
    print("=" * 50)
    overall = all(v in ("PASS", "SKIP") for v in results.values())
    for k, v in results.items():
        icon = "✅" if v == "PASS" else ("⏭ " if v == "SKIP" else "❌")
        print(f"  {icon} {k}: {v}")
    print()
    print(f"  OVERALL: {'PASS ✅' if overall else 'FAIL ❌'}")
    print("=" * 50)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
