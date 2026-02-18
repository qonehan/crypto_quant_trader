from __future__ import annotations

import math

from app.models.interface import BaseModel, PredictionOutput

_EPS = 1e-12


def _sigmoid(x: float) -> float:
    x = max(-20.0, min(20.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class BaselineModelV1(BaseModel):
    MODEL_VERSION = "baseline_v1_exec"

    def predict(self, *, market_window: list, barrier_row: dict, settings) -> PredictionOutput:
        r_t = barrier_row.get("r_t", settings.R_MIN)
        h_sec = barrier_row.get("h_sec", settings.H_SEC)
        sigma_1s = barrier_row.get("sigma_1s")
        sigma_h = barrier_row.get("sigma_h")
        barrier_status = barrier_row.get("status", "WARMUP")

        # --- feature extraction ---
        # Use mid_close_1s if available, else mid
        mids = []
        for r in market_window:
            v = r.get("mid_close_1s") or r.get("mid")
            if v is not None and v > 0:
                mids.append(v)

        if len(mids) < 2:
            return self._fallback(r_t, h_sec, settings)

        mid_last = mids[-1]

        # ret_10 / ret_60 (log-return)
        idx_10 = max(0, len(mids) - 11)
        ret_10 = math.log(mid_last / mids[idx_10]) if mids[idx_10] > 0 else 0.0
        idx_60 = max(0, len(mids) - 61)
        ret_60 = math.log(mid_last / mids[idx_60]) if mids[idx_60] > 0 else 0.0

        # mom_z (volatility-standardized)
        if sigma_1s is not None and sigma_1s > 0:
            z10 = ret_10 / (sigma_1s * math.sqrt(10) + _EPS)
            z60 = ret_60 / (sigma_1s * math.sqrt(60) + _EPS)
            mom_z = 0.7 * z10 + 0.3 * z60
        else:
            mom_z = 0.0

        # spread_bps
        spread_bps_val = 0.0
        for r in reversed(market_window):
            if r.get("spread_bps") is not None:
                spread_bps_val = r["spread_bps"]
                break
            elif r.get("spread") is not None and r.get("mid") is not None and r["mid"] > 0:
                spread_bps_val = 10000 * r["spread"] / r["mid"]
                break

        # imb_notional_top5
        imb_notional = 0.0
        for r in reversed(market_window):
            if r.get("imb_notional_top5") is not None:
                imb_notional = r["imb_notional_top5"]
                break

        # --- score → p_dir ---
        spread_term = spread_bps_val / 10.0
        score = (
            settings.SCORE_A_MOMZ * mom_z
            + settings.SCORE_B_IMB * imb_notional
            - settings.SCORE_C_SPREAD * spread_term
        )
        p_dir = _sigmoid(score)

        # --- z-based p_none ---
        if barrier_status != "OK" or sigma_h is None or sigma_h <= 0:
            p_none = 0.99
            p_up = 0.005
            p_down = 0.005
            z_barrier = None
            p_hit_base = None
        else:
            z_barrier = r_t / (sigma_h + _EPS)
            p_hit_base = math.exp(-settings.P_HIT_CZ * (z_barrier ** 2))
            p_none = _clamp(1 - p_hit_base, 0.0, 0.99)
            p_up = (1 - p_none) * p_dir
            p_down = (1 - p_none) * (1 - p_dir)

        # normalize
        total = p_up + p_down + p_none
        if total > 0:
            p_up /= total
            p_down /= total
            p_none /= total
        else:
            p_up, p_down, p_none = 0.0, 0.0, 1.0

        # --- conditional arrival times ---
        s1 = sigma_1s if sigma_1s is not None and sigma_1s > 0 else 1e-8
        base_T = _clamp((r_t ** 2) / (s1 ** 2 + _EPS), 1.0, float(h_sec))
        conf = _clamp(abs(score) / 2.0, 0.0, 1.0)

        if score >= 0:
            t_up_cond = _clamp(base_T * (1 - 0.2 * conf), 1.0, float(h_sec))
            t_down_cond = _clamp(base_T * (1 + 0.2 * conf), 1.0, float(h_sec))
        else:
            t_up_cond = _clamp(base_T * (1 + 0.2 * conf), 1.0, float(h_sec))
            t_down_cond = _clamp(base_T * (1 - 0.2 * conf), 1.0, float(h_sec))

        # --- r_none_pred ---
        drift = ret_60 * (h_sec / 60.0)
        r_none_pred = _clamp(drift, -0.5 * r_t, 0.5 * r_t)

        # --- cost model ---
        fee_round = 2 * settings.FEE_RATE
        spread_round = spread_bps_val / 10000.0
        slip_round = 2 * (settings.SLIPPAGE_BPS / 10000.0)
        cost_roundtrip = settings.EV_COST_MULT * (fee_round + spread_round + slip_round)

        # --- EV (policy-aligned) ---
        ev = p_up * r_t + p_down * (-r_t) + p_none * r_none_pred - cost_roundtrip

        # --- E[T] and EV_rate ---
        e_t = p_up * t_up_cond + p_down * t_down_cond + p_none * h_sec
        ev_rate = ev / (e_t + _EPS)

        # --- slope_pred ---
        slope_pred = p_up * (r_t / (t_up_cond + _EPS)) - p_down * (r_t / (t_down_cond + _EPS))

        # --- direction_hat (legacy compat) ---
        if ev <= 0 or p_none > settings.P_NONE_MAX_FOR_SIGNAL:
            direction_hat = "NONE"
        else:
            direction_hat = "UP" if p_up >= p_down else "DOWN"

        # --- action_hat ---
        if (
            ev_rate >= settings.ENTER_EV_RATE_TH
            and p_none <= settings.ENTER_PNONE_MAX
            and p_up >= p_down + settings.ENTER_PDIR_MARGIN
            and spread_bps_val <= settings.ENTER_SPREAD_BPS_MAX
        ):
            action_hat = "ENTER_LONG"
        else:
            action_hat = "STAY_FLAT"

        features = {
            "ret_10": ret_10,
            "ret_60": ret_60,
            "mom_z": mom_z,
            "spread_bps": spread_bps_val,
            "imb_notional_top5": imb_notional,
            "score": score,
            "spread_term": spread_term,
            "p_dir": p_dir,
            "base_T": base_T,
            "conf": conf,
        }

        return PredictionOutput(
            p_up=p_up,
            p_down=p_down,
            p_none=p_none,
            t_up=t_up_cond,
            t_down=t_down_cond,
            slope_pred=slope_pred,
            ev=ev,
            direction_hat=direction_hat,
            model_version=self.MODEL_VERSION,
            features=features,
            z_barrier=z_barrier,
            p_hit_base=p_hit_base,
            ev_rate=ev_rate,
            r_none_pred=r_none_pred,
            t_up_cond_pred=t_up_cond,
            t_down_cond_pred=t_down_cond,
            mom_z=mom_z,
            spread_bps=spread_bps_val,
            imb_notional_top5=imb_notional,
            action_hat=action_hat,
        )

    def _fallback(self, r_t: float, h_sec: int, settings) -> PredictionOutput:
        fee_round = 2 * settings.FEE_RATE
        slip_round = 2 * (settings.SLIPPAGE_BPS / 10000.0)
        cost = settings.EV_COST_MULT * (fee_round + slip_round)
        return PredictionOutput(
            p_up=0.0,
            p_down=0.0,
            p_none=1.0,
            t_up=None,
            t_down=None,
            slope_pred=0.0,
            ev=-cost,
            direction_hat="NONE",
            model_version=self.MODEL_VERSION,
            features={"fallback": True},
            z_barrier=None,
            p_hit_base=None,
            ev_rate=None,
            r_none_pred=None,
            t_up_cond_pred=None,
            t_down_cond_pred=None,
            mom_z=None,
            spread_bps=None,
            imb_notional_top5=None,
            action_hat="STAY_FLAT",
        )
