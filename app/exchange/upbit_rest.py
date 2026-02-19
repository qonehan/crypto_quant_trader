"""Upbit v1 REST 클라이언트 (동기 httpx 기반) — Step 8 안정화."""
from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from app.exchange.upbit_auth import make_auth_header

log = logging.getLogger(__name__)

# HTTP status codes that warrant a retry
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class UpbitApiError(Exception):
    """Upbit API 오류 — http_status / remaining_req 포함."""

    def __init__(
        self,
        message: str,
        http_status: int | None = None,
        remaining_req: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.remaining_req = remaining_req


class UpbitRestClient:
    """Upbit REST API wrapper.

    Step 8 추가:
    - 요청마다 latency_ms / http_status / remaining_req를 _last_call_meta에 저장
    - 재시도: exponential backoff + jitter (max_retry 횟수)
    - UpbitApiError 표준화 (runner가 status/error_msg 기록 용이)
    - order_test: POST /v1/orders/test 직접 호출 (dry-run)
    - create_order / order_test에 identifier 파라미터 지원
    """

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        base_url: str = "https://api.upbit.com",
        timeout: float = 10.0,
        max_retry: int = 3,
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retry = max_retry
        # Populated after every request; caller may read http_status / remaining_req / latency_ms
        self._last_call_meta: dict = {}

    def _auth(self, query_params: dict | None = None) -> dict[str, str]:
        return make_auth_header(self.access_key, self.secret_key, query_params)

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: dict | None = None,
    ) -> Any:
        """Execute HTTP request with exponential backoff + jitter.

        Populates self._last_call_meta with:
            http_status, remaining_req, latency_ms

        Raises UpbitApiError on non-recoverable errors or exhausted retries.
        """
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(self.max_retry):
            if attempt > 0:
                wait = (2 ** attempt) + random.uniform(0.0, 0.5)
                log.info(
                    "Retry %d/%d %s %s (wait=%.2fs)", attempt, self.max_retry - 1, method, path, wait
                )
                time.sleep(wait)

            try:
                t0 = time.monotonic()
                if method == "GET":
                    headers = self._auth(params)
                    r = httpx.get(url, params=params, headers=headers, timeout=self.timeout)
                else:  # POST
                    headers = self._auth(body)
                    r = httpx.post(url, json=body, headers=headers, timeout=self.timeout)

                latency_ms = int((time.monotonic() - t0) * 1000)
                remaining_req = r.headers.get("remaining-req")
                http_status = r.status_code

                self._last_call_meta = {
                    "http_status": http_status,
                    "remaining_req": remaining_req,
                    "latency_ms": latency_ms,
                }

                if r.status_code in _RETRYABLE_STATUS and attempt < self.max_retry - 1:
                    log.warning(
                        "Retryable HTTP %d from %s (attempt %d)", r.status_code, path, attempt
                    )
                    last_exc = UpbitApiError(
                        f"HTTP {r.status_code}",
                        http_status=r.status_code,
                        remaining_req=remaining_req,
                    )
                    continue

                if r.status_code >= 400:
                    raise UpbitApiError(
                        f"HTTP {r.status_code}: {r.text[:300]}",
                        http_status=r.status_code,
                        remaining_req=remaining_req,
                    )

                return r.json()

            except UpbitApiError:
                raise
            except Exception as e:
                last_exc = e
                log.warning("Request error (attempt %d/%d): %s", attempt, self.max_retry - 1, e)

        raise UpbitApiError(f"All {self.max_retry} retries failed: {last_exc}") from last_exc

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, body=body)

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
        ord_type: str = "price",
        identifier: str | None = None,
    ) -> dict:
        """POST /v1/orders/test — 실 주문 생성 없이 파라미터 검증 (Step 8).

        응답은 실제 주문 응답과 동일한 구조이지만 체결되지 않음.
        uuid는 임시값이므로 get_order() 조회 금지.
        """
        body: dict = {"market": market, "side": side, "ord_type": ord_type}
        if volume is not None:
            body["volume"] = str(volume)
        if price is not None:
            body["price"] = str(int(price))
        if identifier is not None:
            body["identifier"] = identifier
        log.info(
            "order_test: market=%s side=%s ord_type=%s volume=%s price=%s",
            market, side, ord_type, volume, price,
        )
        return self._post("/v1/orders/test", body)

    def create_order(
        self,
        market: str,
        side: str,
        volume: float | None = None,
        price: float | None = None,
        ord_type: str = "market",
        identifier: str | None = None,
    ) -> dict:
        """실제 주문 생성 (LIVE_TRADING_ENABLED=True 시에만 호출)."""
        body: dict = {"market": market, "side": side, "ord_type": ord_type}
        if volume is not None:
            body["volume"] = str(volume)
        if price is not None:
            body["price"] = str(int(price))
        if identifier is not None:
            body["identifier"] = identifier
        return self._post("/v1/orders", body)

    def get_order(self, uuid: str) -> dict:
        """개별 주문 조회."""
        return self._get("/v1/order", {"uuid": uuid})

    def list_open_orders(self, market: str) -> list[dict]:
        """미체결 주문 목록 조회."""
        return self._get("/v1/orders/open", {"market": market})
