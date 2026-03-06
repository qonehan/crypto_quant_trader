from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.writer import upsert_prediction
from app.models.interface import BaseModel

log = logging.getLogger(__name__)

# ── GCP 원천 데이터 쿼리 (alt 피처: Binance + Macro) ──────────────────────────
_FETCH_GCP_BINANCE_SQL = text("""
SELECT funding_rate, long_short_ratio, open_interest
FROM binance_derivatives
ORDER BY timestamp DESC LIMIT 1
""")

_FETCH_GCP_MACRO_SQL = text("""
SELECT dxy_index, fear_greed_index
FROM macro_and_sentiment
ORDER BY timestamp DESC LIMIT 1
""")

_FETCH_GCP_TICK_SQL = text("""
SELECT
    SUM(CASE WHEN ask_bid = 'ASK' THEN volume ELSE 0 END)
    / NULLIF(SUM(volume), 0) AS buy_volume_ratio
FROM upbit_tick
WHERE timestamp >= now() AT TIME ZONE 'UTC' - interval '60 seconds'
""")


def create_model(settings: Settings) -> BaseModel:
    """ACTIVE_MODEL 식별자를 ModelFactory에 전달해 모델 인스턴스를 반환한다.

    .env 에서 ACTIVE_MODEL=ridge_h3600 한 줄만 바꾸면
    모델 클래스·경로·gamma·version이 자동으로 결정된다.

    등록 가능한 모델::

        ridge_h3600  ridge_h600  ridge_h120
        hgbr_h600    hgbr_h120   baseline_v1
    """
    from app.predictor.ml_model import ModelFactory
    model_id = settings.ACTIVE_MODEL
    model = ModelFactory.create(model_id)
    spec = ModelFactory.get_spec(model_id)
    log.info(
        "create_model: ACTIVE_MODEL=%s → %s (h_sec=%d gamma=%.1f version=%s)",
        model_id, type(model).__name__, spec.h_sec, spec.gamma, spec.version,
    )
    return model

_FETCH_BARRIER_SQL = text("""
SELECT ts, symbol, h_sec, r_t, sigma_1s, sigma_h, status
FROM barrier_state
WHERE symbol = :symbol AND ts <= :t0
ORDER BY ts DESC LIMIT 1
""")

_FETCH_MARKET_WINDOW_SQL = text("""
SELECT ts, mid, mid_close_1s, spread, spread_bps, imbalance_top5, imb_notional_top5
FROM market_1s
WHERE symbol = :symbol AND ts <= :t0 AND ts >= :since
ORDER BY ts ASC
""")


class PredictionRunner:
    def __init__(self, settings: Settings, engine: Engine, model: BaseModel) -> None:
        self.settings = settings
        self.engine = engine
        self.model = model
        # GCP DB 엔진 (alt 피처 조회용) — GCP_DB_URL 없으면 None
        self._gcp_engine: Engine | None = None
        if settings.GCP_DB_URL:
            try:
                self._gcp_engine = create_engine(settings.GCP_DB_URL, pool_pre_ping=True)
                log.info("PredictionRunner: GCP 엔진 초기화 완료 (alt 피처 조회)")
            except Exception:
                log.warning("PredictionRunner: GCP 엔진 초기화 실패 — alt 피처 0.0 폴백")

    def fetch_alt_row(self) -> dict:
        """GCP DB에서 최신 alt 피처(Binance/Macro/Tick)를 조회. 실패 시 0.0 폴백."""
        result: dict = {
            "funding_rate": 0.0,
            "long_short_ratio": 0.0,
            "open_interest": 0.0,
            "dxy_index": 0.0,
            "fear_greed_index": 0.0,
            "buy_volume_ratio": 0.0,
        }
        if self._gcp_engine is None:
            return result
        try:
            with self._gcp_engine.connect() as conn:
                row = conn.execute(_FETCH_GCP_BINANCE_SQL).fetchone()
                if row:
                    result["funding_rate"] = float(row.funding_rate or 0.0)
                    result["long_short_ratio"] = float(row.long_short_ratio or 0.0)
                    result["open_interest"] = float(row.open_interest or 0.0)
        except Exception:
            log.debug("PredictionRunner: GCP binance_derivatives 조회 실패")
        try:
            with self._gcp_engine.connect() as conn:
                row = conn.execute(_FETCH_GCP_MACRO_SQL).fetchone()
                if row:
                    result["dxy_index"] = float(row.dxy_index or 0.0)
                    result["fear_greed_index"] = float(row.fear_greed_index or 0.0)
        except Exception:
            log.debug("PredictionRunner: GCP macro_and_sentiment 조회 실패")
        try:
            with self._gcp_engine.connect() as conn:
                row = conn.execute(_FETCH_GCP_TICK_SQL).fetchone()
                if row and row.buy_volume_ratio is not None:
                    result["buy_volume_ratio"] = float(row.buy_volume_ratio)
        except Exception:
            log.debug("PredictionRunner: GCP upbit_tick 조회 실패")
        return result

    def fetch_latest_barrier(self, symbol: str, t0: datetime) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(_FETCH_BARRIER_SQL, {"symbol": symbol, "t0": t0}).fetchone()
        if row is None:
            return None
        return row._asdict()

    def fetch_market_window(self, symbol: str, t0: datetime) -> list[dict]:
        since = t0 - __import__("datetime").timedelta(seconds=self.settings.MODEL_LOOKBACK_SEC)
        with self.engine.connect() as conn:
            rows = conn.execute(
                _FETCH_MARKET_WINDOW_SQL, {"symbol": symbol, "t0": t0, "since": since}
            ).fetchall()
        return [r._asdict() for r in rows]

    def _run_tick(self, t0: datetime) -> None:
        symbol = self.settings.SYMBOL

        barrier_row = self.fetch_latest_barrier(symbol, t0)
        if barrier_row is None:
            log.warning("Pred: no barrier_state row found for t0=%s, skipping", t0)
            return

        # alt 피처(Binance/Macro/Tick)를 GCP에서 조회하여 barrier_row에 병합
        alt_row = self.fetch_alt_row()
        barrier_row = {**barrier_row, **alt_row}

        market_window = self.fetch_market_window(symbol, t0)

        output = self.model.predict(
            market_window=market_window,
            barrier_row=barrier_row,
            settings=self.settings,
        )

        row = {
            "t0": t0,
            "symbol": symbol,
            "h_sec": barrier_row.get("h_sec", self.settings.H_SEC),
            "r_t": barrier_row.get("r_t", self.settings.R_MIN),
            "p_up": output.p_up,
            "p_down": output.p_down,
            "p_none": output.p_none,
            "t_up": output.t_up,
            "t_down": output.t_down,
            "slope_pred": output.slope_pred,
            "ev": output.ev,
            "direction_hat": output.direction_hat,
            "model_version": output.model_version,
            "status": "PENDING",
            "sigma_1s": barrier_row.get("sigma_1s"),
            "sigma_h": barrier_row.get("sigma_h"),
            "features": json.dumps(output.features),
            # v1 fields
            "z_barrier": output.z_barrier,
            "p_hit_base": output.p_hit_base,
            "ev_rate": output.ev_rate,
            "r_none_pred": output.r_none_pred,
            "t_up_cond_pred": output.t_up_cond_pred,
            "t_down_cond_pred": output.t_down_cond_pred,
            "spread_bps": output.spread_bps,
            "mom_z": output.mom_z,
            "imb_notional_top5": output.imb_notional_top5,
            "action_hat": output.action_hat,
        }

        upsert_prediction(self.engine, row)

        log.info(
            "Pred(v1): t0=%s r_t=%.6f z=%s p_none=%.4f p_up=%.4f p_down=%.4f "
            "ev=%.8f ev_rate=%s action=%s",
            t0.strftime("%H:%M:%S"),
            row["r_t"],
            f"{output.z_barrier:.3f}" if output.z_barrier is not None else "N/A",
            output.p_none,
            output.p_up,
            output.p_down,
            output.ev,
            f"{output.ev_rate:.8f}" if output.ev_rate is not None else "N/A",
            output.action_hat or "N/A",
        )

    async def run(self) -> None:
        interval = self.settings.DECISION_INTERVAL_SEC
        now = time.time()
        next_ts = (int(now) // interval + 1) * interval
        # offset slightly to let barrier controller write first
        await asyncio.sleep(next_ts - now + 0.5)

        while True:
            t0_utc = datetime.fromtimestamp(next_ts, tz=timezone.utc).replace(microsecond=0)
            try:
                await asyncio.to_thread(self._run_tick, t0_utc)
            except Exception:
                log.exception("PredictionRunner error at t0=%s", t0_utc)

            next_ts += interval
            sleep_dur = next_ts - time.time() + 0.5
            if sleep_dur > 0:
                await asyncio.sleep(sleep_dur)
