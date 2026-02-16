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


class BaseModel:
    def predict(self, *, market_window: list, barrier_row: dict, settings) -> PredictionOutput:
        raise NotImplementedError
