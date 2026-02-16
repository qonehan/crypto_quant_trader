from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import text

from app.config import load_settings
from app.db.session import get_engine
from app.marketdata.state import MarketState
from app.marketdata.upbit_ws import UpbitWsClient

DB_RESOLVE_HINT = (
    "DB host 'db'를 찾지 못했습니다. "
    "Codespaces에서 Dev Containers: Rebuild and Reopen in Container를 실행해 "
    "docker-compose devcontainer로 들어가 있는지 확인하세요. "
    "또한 db 컨테이너가 정상 실행 중인지 확인하세요."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def check_db(settings) -> bool:
    engine = get_engine(settings)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("Boot OK")
        print("DB OK")
        return True
    except Exception as e:
        err = str(e)
        if "failed to resolve host" in err or "could not translate host name" in err:
            print(f"DB connection failed: {e}\n{DB_RESOLVE_HINT}")
        else:
            print(f"DB connection failed: {e}")
        return False


async def consumer(queue: asyncio.Queue, state: MarketState) -> None:
    while True:
        event = await queue.get()
        etype = event["event_type"]
        payload = event["payload"]
        if etype == "ticker":
            state.update_ticker(payload)
        elif etype == "trade":
            state.update_trade(payload)
        elif etype == "orderbook":
            state.update_orderbook(payload)


async def printer(state: MarketState) -> None:
    while True:
        await asyncio.sleep(1)
        if state.last_update_ts > 0:
            log.info(state.summary_line())


async def async_main() -> None:
    settings = load_settings()
    check_db(settings)

    state = MarketState(symbol=settings.SYMBOL)
    queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
    client = UpbitWsClient(settings, queue)

    # share counters between client and state
    async def sync_counters():
        while True:
            await asyncio.sleep(1)
            state.counters["reconnect_count"] = client.reconnect_count
            state.counters["error_count"] = client.error_count

    tasks = [
        asyncio.create_task(client.run(), name="ws"),
        asyncio.create_task(consumer(queue, state), name="consumer"),
        asyncio.create_task(printer(state), name="printer"),
        asyncio.create_task(sync_counters(), name="sync_counters"),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nShutdown requested, exiting.")


if __name__ == "__main__":
    main()
