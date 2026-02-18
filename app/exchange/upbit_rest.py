"""Upbit v1 REST 클라이언트 (동기 httpx 기반)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.exchange.upbit_auth import make_auth_header

log = logging.getLogger(__name__)


class UpbitRestClient:
    """Upbit REST API wrapper.

    지원 메서드:
        get_accounts          - GET  /v1/accounts
        get_orders_chance     - GET  /v1/orders/chance
        order_test            - orders/chance 기반 dry-run (체결 없음)
        create_order          - POST /v1/orders
        get_order             - GET  /v1/order
        list_open_orders      - GET  /v1/orders/open
    """

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        base_url: str = "https://api.upbit.com",
        timeout: float = 10.0,
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _auth(self, query_params: dict | None = None) -> dict[str, str]:
        return make_auth_header(self.access_key, self.secret_key, query_params)

    def _get(self, path: str, params: dict | None = None) -> Any:
        headers = self._auth(params)
        url = f"{self.base_url}{path}"
        r = httpx.get(url, params=params, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> Any:
        headers = self._auth(body)
        url = f"{self.base_url}{path}"
        r = httpx.post(url, json=body, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── Public API methods ──────────────────────────────────────

    def get_accounts(self) -> list[dict]:
        """계좌 잔액 전체 조회."""
        return self._get("/v1/accounts")

    def get_orders_chance(self, market: str) -> dict:
        """주문 가능 정보 조회 (수수료, 잔액 포함)."""
        return self._get("/v1/orders/chance", {"market": market})

    def order_test(
        self,
        market: str,
        side: str,
        volume: float | None = None,
        price: float | None = None,
        ord_type: str = "market",
    ) -> dict:
        """주문 테스트 (orders/chance 조회 기반 dry-run, 실제 체결 없음).

        Upbit에 공식 sandbox/test 엔드포인트가 없으므로
        주문 가능 정보만 확인하고 파라미터를 로깅합니다.
        """
        chance = self.get_orders_chance(market)
        log.info(
            "order_test: market=%s side=%s volume=%s price=%s ord_type=%s "
            "bid_fee=%s ask_fee=%s bid_balance=%s ask_balance=%s",
            market, side, volume, price, ord_type,
            chance.get("bid_fee"),
            chance.get("ask_fee"),
            chance.get("bid_account", {}).get("balance"),
            chance.get("ask_account", {}).get("balance"),
        )
        return {"status": "test_ok", "market": market, "side": side, "chance_checked": True}

    def create_order(
        self,
        market: str,
        side: str,
        volume: float | None = None,
        price: float | None = None,
        ord_type: str = "market",
    ) -> dict:
        """실제 주문 생성 (LIVE_TRADING_ENABLED=True 시에만 호출)."""
        body: dict = {"market": market, "side": side, "ord_type": ord_type}
        if volume is not None:
            body["volume"] = str(volume)
        if price is not None:
            body["price"] = str(price)
        return self._post("/v1/orders", body)

    def get_order(self, uuid: str) -> dict:
        """개별 주문 조회."""
        return self._get("/v1/order", {"uuid": uuid})

    def list_open_orders(self, market: str) -> list[dict]:
        """미체결 주문 목록 조회."""
        return self._get("/v1/orders/open", {"market": market})
