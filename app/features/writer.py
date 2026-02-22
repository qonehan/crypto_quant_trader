"""feature_snapshots DB upsert helper."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


def _j(obj) -> str | None:
    """Serialize to JSONB-compatible string."""
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, default=str)


def upsert_feature_snapshot(engine: Engine, snap: dict) -> None:
    """Upsert one feature_snapshot row. ON CONFLICT (ts, symbol) DO UPDATE."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO feature_snapshots (
                        ts, symbol,
                        mid_krw, spread_bps, imb_notional_top5,
                        r_t, r_min_eff, cost_roundtrip_est,
                        sigma_1s, sigma_h, k_vol_eff, barrier_status,
                        p_up, p_down, p_none, ev, ev_rate, action_hat, model_version,
                        bin_mark_price, bin_index_price, bin_funding_rate, bin_mark_index_basis,
                        oi_value, global_ls_ratio, taker_ls_ratio, basis_value,
                        liq_5m_notional, liq_5m_count,
                        bin_mark_ts, oi_ts, liq_last_ts,
                        raw_json
                    ) VALUES (
                        :ts, :symbol,
                        :mid_krw, :spread_bps, :imb_notional_top5,
                        :r_t, :r_min_eff, :cost_roundtrip_est,
                        :sigma_1s, :sigma_h, :k_vol_eff, :barrier_status,
                        :p_up, :p_down, :p_none, :ev, :ev_rate, :action_hat, :model_version,
                        :bin_mark_price, :bin_index_price, :bin_funding_rate, :bin_mark_index_basis,
                        :oi_value, :global_ls_ratio, :taker_ls_ratio, :basis_value,
                        :liq_5m_notional, :liq_5m_count,
                        :bin_mark_ts, :oi_ts, :liq_last_ts,
                        CAST(:raw_json AS JSONB)
                    )
                    ON CONFLICT (ts, symbol) DO UPDATE SET
                        mid_krw              = EXCLUDED.mid_krw,
                        spread_bps           = EXCLUDED.spread_bps,
                        imb_notional_top5    = EXCLUDED.imb_notional_top5,
                        r_t                  = EXCLUDED.r_t,
                        r_min_eff            = EXCLUDED.r_min_eff,
                        cost_roundtrip_est   = EXCLUDED.cost_roundtrip_est,
                        sigma_1s             = EXCLUDED.sigma_1s,
                        sigma_h              = EXCLUDED.sigma_h,
                        k_vol_eff            = EXCLUDED.k_vol_eff,
                        barrier_status       = EXCLUDED.barrier_status,
                        p_up                 = EXCLUDED.p_up,
                        p_down               = EXCLUDED.p_down,
                        p_none               = EXCLUDED.p_none,
                        ev                   = EXCLUDED.ev,
                        ev_rate              = EXCLUDED.ev_rate,
                        action_hat           = EXCLUDED.action_hat,
                        model_version        = EXCLUDED.model_version,
                        bin_mark_price       = EXCLUDED.bin_mark_price,
                        bin_index_price      = EXCLUDED.bin_index_price,
                        bin_funding_rate     = EXCLUDED.bin_funding_rate,
                        bin_mark_index_basis = EXCLUDED.bin_mark_index_basis,
                        oi_value             = EXCLUDED.oi_value,
                        global_ls_ratio      = EXCLUDED.global_ls_ratio,
                        taker_ls_ratio       = EXCLUDED.taker_ls_ratio,
                        basis_value          = EXCLUDED.basis_value,
                        liq_5m_notional      = EXCLUDED.liq_5m_notional,
                        liq_5m_count         = EXCLUDED.liq_5m_count,
                        bin_mark_ts          = EXCLUDED.bin_mark_ts,
                        oi_ts                = EXCLUDED.oi_ts,
                        liq_last_ts          = EXCLUDED.liq_last_ts,
                        raw_json             = EXCLUDED.raw_json
                """),
                {
                    "ts": snap.get("ts"),
                    "symbol": snap.get("symbol"),
                    "mid_krw": snap.get("mid_krw"),
                    "spread_bps": snap.get("spread_bps"),
                    "imb_notional_top5": snap.get("imb_notional_top5"),
                    "r_t": snap.get("r_t"),
                    "r_min_eff": snap.get("r_min_eff"),
                    "cost_roundtrip_est": snap.get("cost_roundtrip_est"),
                    "sigma_1s": snap.get("sigma_1s"),
                    "sigma_h": snap.get("sigma_h"),
                    "k_vol_eff": snap.get("k_vol_eff"),
                    "barrier_status": snap.get("barrier_status"),
                    "p_up": snap.get("p_up"),
                    "p_down": snap.get("p_down"),
                    "p_none": snap.get("p_none"),
                    "ev": snap.get("ev"),
                    "ev_rate": snap.get("ev_rate"),
                    "action_hat": snap.get("action_hat"),
                    "model_version": snap.get("model_version"),
                    "bin_mark_price": snap.get("bin_mark_price"),
                    "bin_index_price": snap.get("bin_index_price"),
                    "bin_funding_rate": snap.get("bin_funding_rate"),
                    "bin_mark_index_basis": snap.get("bin_mark_index_basis"),
                    "oi_value": snap.get("oi_value"),
                    "global_ls_ratio": snap.get("global_ls_ratio"),
                    "taker_ls_ratio": snap.get("taker_ls_ratio"),
                    "basis_value": snap.get("basis_value"),
                    "liq_5m_notional": snap.get("liq_5m_notional"),
                    "liq_5m_count": snap.get("liq_5m_count"),
                    "bin_mark_ts": snap.get("bin_mark_ts"),
                    "oi_ts": snap.get("oi_ts"),
                    "liq_last_ts": snap.get("liq_last_ts"),
                    "raw_json": _j(snap.get("raw_json")),
                },
            )
    except Exception:
        log.exception("upsert_feature_snapshot error (ts=%s)", snap.get("ts"))
