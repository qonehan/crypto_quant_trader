from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.barrier.controller import BarrierController
from app.config import load_settings
from app.db.init_db import ensure_schema
from app.db.migrate import apply_migrations
from app.db.session import get_engine
from app.evaluator.evaluator import Evaluator
from app.marketdata.resampler import MarketResampler
from app.marketdata.state import MarketState
from app.marketdata.upbit_ws import UpbitWsClient
from app.models.baseline import BaselineModel
from app.predictor.runner import PredictionRunner

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


async def consumer(
    queue: asyncio.Queue,
    state: MarketState,
    resampler: MarketResampler,
) -> None:
    while True:
        event = await queue.get()
        etype = event["event_type"]
        payload = event["payload"]
        if etype == "ticker":
            state.update_ticker(payload)
        elif etype == "trade":
            state.update_trade(payload)
            vol = payload.get("trade_volume", 0)
            resampler.on_trade(vol)
        elif etype == "orderbook":
            state.update_orderbook(payload)
            # Feed bid/ask OHLC + notional imbalance to resampler
            units = payload.get("orderbook_units", [])
            if units:
                best_bid = units[0].get("bid_price")
                best_ask = units[0].get("ask_price")
                if best_bid and best_ask:
                    eps = 1e-12
                    b_notional = sum(
                        u.get("bid_price", 0) * u.get("bid_size", 0) for u in units
                    )
                    a_notional = sum(
                        u.get("ask_price", 0) * u.get("ask_size", 0) for u in units
                    )
                    imb_notional = (b_notional - a_notional) / (b_notional + a_notional + eps)
                    resampler.on_quote(
                        bid=best_bid,
                        ask=best_ask,
                        imb_notional_top5=imb_notional,
                    )


async def printer(state: MarketState) -> None:
    while True:
        await asyncio.sleep(1)
        if state.last_update_ts > 0:
            log.info(state.summary_line())


async def async_main() -> None:
    settings = load_settings()
    engine = get_engine(settings)
    check_db(settings)

    ensure_schema(engine)
    log.info("DB schema ensured (market_1s, barrier_state, predictions, evaluation_results)")

    apply_migrations(engine)
    log.info("DB migrations applied")

    state = MarketState(symbol=settings.SYMBOL)
    queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
    client = UpbitWsClient(settings, queue)
    resampler = MarketResampler(state, engine)
    barrier = BarrierController(settings, engine)
    model = BaselineModel()
    pred_runner = PredictionRunner(settings, engine, model)
    evaluator = Evaluator(settings, engine)

    async def sync_counters():
        while True:
            await asyncio.sleep(1)
            state.counters["reconnect_count"] = client.reconnect_count
            state.counters["error_count"] = client.error_count

    tasks = [
        asyncio.create_task(client.run(), name="ws"),
        asyncio.create_task(consumer(queue, state, resampler), name="consumer"),
        asyncio.create_task(printer(state), name="printer"),
        asyncio.create_task(sync_counters(), name="sync_counters"),
        asyncio.create_task(resampler.run(), name="resampler"),
        asyncio.create_task(barrier.run(), name="barrier"),
        asyncio.create_task(pred_runner.run(), name="predictor"),
        asyncio.create_task(evaluator.run(), name="evaluator"),
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
