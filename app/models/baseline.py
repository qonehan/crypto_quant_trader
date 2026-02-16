from __future__ import annotations

import math

from app.models.interface import BaseModel, PredictionOutput


def _sigmoid(x: float) -> float:
    x = max(-20.0, min(20.0, x))
    return 1.0 / (1.0 + math.exp(-x))


class BaselineModel(BaseModel):
    MODEL_VERSION = "baseline_v1"

    # tunable constants
    A_MOM = 500.0
    B_IMB = 1.0
    C_SPREAD = 50.0
    S0 = 1.0

    def predict(self, *, market_window: list, barrier_row: dict, settings) -> PredictionOutput:
        r_t = barrier_row.get("r_t", settings.R_MIN)
        h_sec = barrier_row.get("h_sec", settings.H_SEC)
        sigma_1s = barrier_row.get("sigma_1s")

        # --- feature extraction ---
        mids = [r["mid"] for r in market_window if r.get("mid") is not None and r["mid"] > 0]

        if len(mids) < 2:
            return self._fallback(r_t, h_sec, settings)

        mid_last = mids[-1]

        # ret_10s
        idx_10 = max(0, len(mids) - 11)
        ret_10s = math.log(mid_last / mids[idx_10]) if mids[idx_10] > 0 else 0.0

        # ret_60s
        idx_60 = max(0, len(mids) - 61)
        ret_60s = math.log(mid_last / mids[idx_60]) if mids[idx_60] > 0 else 0.0

        mom = 0.7 * ret_10s + 0.3 * ret_60s

        # imbalance
        imb = 0.0
        for r in reversed(market_window):
            if r.get("imbalance_top5") is not None:
                imb = r["imbalance_top5"]
                break

        # spread_pct
        spread_pct = 0.0
        for r in reversed(market_window):
            if r.get("spread") is not None and r.get("mid") is not None and r["mid"] > 0:
                spread_pct = r["spread"] / r["mid"]
                break

        # --- score & probabilities ---
        score = self.A_MOM * mom + self.B_IMB * imb - self.C_SPREAD * spread_pct
        p_dir = _sigmoid(score)
        conf = min(1.0, abs(score) / self.S0)
        p_none = max(0.0, min(0.95, 1.0 - conf))
        p_up = (1.0 - p_none) * p_dir
        p_down = (1.0 - p_none) * (1.0 - p_dir)

        # normalize
        total = p_up + p_down + p_none
        if total > 0:
            p_up /= total
            p_down /= total
            p_none /= total
        else:
            p_up, p_down, p_none = 0.0, 0.0, 1.0

        # --- expected arrival times ---
        a = max(r_t, 1e-6)
        s1 = sigma_1s if sigma_1s is not None and sigma_1s > 0 else 1e-8
        base_T = (a * a) / (s1 * s1 + 1e-12)
        base_T = max(1.0, min(base_T, float(h_sec)))

        t_up = base_T
        t_down = base_T
        if p_up > p_down:
            t_up *= (1.0 - 0.2 * conf)
            t_down *= (1.0 + 0.2 * conf)
        else:
            t_up *= (1.0 + 0.2 * conf)
            t_down *= (1.0 - 0.2 * conf)

        # --- EV / slope ---
        fee_cost = 2.0 * settings.FEE_RATE
        slip_cost = settings.SLIPPAGE_BPS / 10000.0
        cost = settings.EV_COST_MULT * (fee_cost + spread_pct + slip_cost)

        ev = p_up * r_t + p_down * (-r_t) - cost
        slope_pred = p_up * (r_t / max(t_up, 1e-6)) - p_down * (r_t / max(t_down, 1e-6))

        # --- direction ---
        if ev <= 0 or p_none > settings.P_NONE_MAX_FOR_SIGNAL:
            direction_hat = "NONE"
        else:
            direction_hat = "UP" if p_up >= p_down else "DOWN"

        features = {
            "ret_10s": ret_10s,
            "ret_60s": ret_60s,
            "mom": mom,
            "imb": imb,
            "spread_pct": spread_pct,
            "score": score,
            "conf": conf,
        }

        return PredictionOutput(
            p_up=p_up,
            p_down=p_down,
            p_none=p_none,
            t_up=t_up,
            t_down=t_down,
            slope_pred=slope_pred,
            ev=ev,
            direction_hat=direction_hat,
            model_version=self.MODEL_VERSION,
            features=features,
        )

    def _fallback(self, r_t: float, h_sec: int, settings) -> PredictionOutput:
        fee_cost = 2.0 * settings.FEE_RATE
        slip_cost = settings.SLIPPAGE_BPS / 10000.0
        cost = settings.EV_COST_MULT * (fee_cost + slip_cost)
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
        )
