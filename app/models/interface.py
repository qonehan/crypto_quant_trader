from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PredictionOutput:
    p_up: float
    p_down: float
    p_none: float
    t_up: float | None
    t_down: float | None
    slope_pred: float
    ev: float
    direction_hat: str
    model_version: str
    features: dict = field(default_factory=dict)

    # v1 extended fields
    z_barrier: float | None = None
    p_hit_base: float | None = None
    ev_rate: float | None = None
    r_none_pred: float | None = None
    t_up_cond_pred: float | None = None
    t_down_cond_pred: float | None = None
    mom_z: float | None = None
    spread_bps: float | None = None
    imb_notional_top5: float | None = None
    action_hat: str | None = None


class BaseModel:
    def predict(self, *, market_window: list, barrier_row: dict, settings) -> PredictionOutput:
        raise NotImplementedError
