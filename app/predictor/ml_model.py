"""
RidgePredictor — Ridge 회귀 모델 기반 예측기

학습된 Ridge+StandardScaler 파이프라인을 로드하여 실시간 예측을 수행한다.
BaselineModelV1을 내부적으로 실행해 피처를 추출하고, Ridge 모델로 수익률을 예측한 뒤
PredictionOutput 포맷으로 반환한다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from app.models.baseline_v1 import BaselineModelV1
from app.models.interface import BaseModel, PredictionOutput

log = logging.getLogger(__name__)

_BASELINE = BaselineModelV1()

# 기본 피처 순서 (feature_cols.json이 없을 때 폴백)
_FEATURE_COLS = [
    "p_up",
    "p_down",
    "p_none",
    "ev",
    "ev_rate",
    "r_t",
    "z_barrier",
    "spread_bps",
    "mom_z",
    "imb_notional_top5",
    "entry_mid",
]


def _resolve_model_path(model_path: str, h_sec: int, model_type: str = "ridge") -> Path:
    """model_path가 비어 있으면 H_SEC 기반 기본 경로를 반환."""
    if model_path:
        return Path(model_path)
    return Path(f"artifacts/ml1/h{h_sec}/ridge_model.joblib")


class RidgePredictor(BaseModel):
    """학습된 Ridge 파이프라인을 래핑하는 예측기."""

    MODEL_VERSION = "ridge_v1"

    def __init__(self, model_path: str = "", h_sec: int = 120) -> None:
        try:
            import joblib
        except ImportError as exc:
            raise ImportError("joblib이 설치되어 있지 않습니다. `pip install joblib` 실행 필요") from exc

        path = _resolve_model_path(model_path, h_sec)
        if not path.exists():
            raise FileNotFoundError(
                f"Ridge 모델 파일을 찾을 수 없습니다: {path}\n"
                f"artifacts/ml1/h{{H_SEC}}/ 폴더에 ridge_model.joblib이 있는지 확인하세요."
            )

        self._pipeline = joblib.load(path)
        log.info("RidgePredictor: 모델 로드 완료 — %s", path)

        # feature_cols.json 로드 (있으면 덮어쓰기)
        feat_path = path.parent / "feature_cols.json"
        if feat_path.exists():
            with open(feat_path) as f:
                loaded_cols = json.load(f)
            if loaded_cols:
                self._feature_cols = loaded_cols
                log.info("RidgePredictor: feature_cols 로드 — %s", self._feature_cols)
            else:
                self._feature_cols = _FEATURE_COLS
        else:
            self._feature_cols = _FEATURE_COLS

    def predict(
        self,
        *,
        market_window: list,
        barrier_row: dict,
        settings,
    ) -> PredictionOutput:
        # ── Step 1: BaselineModelV1으로 기반 피처 계산 ──────────────
        base = _BASELINE.predict(
            market_window=market_window,
            barrier_row=barrier_row,
            settings=settings,
        )

        # baseline이 z_barrier를 계산하지 못한 경우(워밍업) — baseline 그대로 반환
        if base.z_barrier is None:
            return self._wrap_with_version(base)

        # ── Step 2: entry_mid 추출 ──────────────────────────────────
        entry_mid: float | None = None
        for r in reversed(market_window):
            v = r.get("mid_close_1s") or r.get("mid")
            if v is not None and v > 0:
                entry_mid = float(v)
                break

        if entry_mid is None:
            return self._wrap_with_version(base)

        # ── Step 3: 피처 벡터 구성 ─────────────────────────────────
        feat_map = {
            "p_up": base.p_up,
            "p_down": base.p_down,
            "p_none": base.p_none,
            "ev": base.ev,
            "ev_rate": base.ev_rate if base.ev_rate is not None else 0.0,
            "r_t": barrier_row.get("r_t", settings.R_MIN),
            "z_barrier": base.z_barrier,
            "spread_bps": base.spread_bps if base.spread_bps is not None else 0.0,
            "mom_z": base.mom_z if base.mom_z is not None else 0.0,
            "imb_notional_top5": base.imb_notional_top5 if base.imb_notional_top5 is not None else 0.0,
            "entry_mid": entry_mid,
        }

        try:
            X = np.array([[feat_map[c] for c in self._feature_cols]], dtype=np.float64)
            ridge_return = float(self._pipeline.predict(X)[0])
        except Exception:
            log.exception("RidgePredictor: 추론 실패 — baseline으로 폴백")
            return self._wrap_with_version(base)

        # ── Step 4: Ridge 결과로 action_hat / direction_hat 재계산 ──
        ev_rate = base.ev_rate
        spread_bps_val = base.spread_bps or 0.0

        if (
            ridge_return > 0
            and ev_rate is not None
            and ev_rate >= settings.ENTER_EV_RATE_TH
            and base.p_none <= settings.ENTER_PNONE_MAX
            and base.p_up >= base.p_down + settings.ENTER_PDIR_MARGIN
            and spread_bps_val <= settings.ENTER_SPREAD_BPS_MAX
        ):
            action_hat = "ENTER_LONG"
        else:
            action_hat = "STAY_FLAT"

        if ridge_return > 0 and base.p_none <= settings.P_NONE_MAX_FOR_SIGNAL:
            direction_hat = "UP"
        elif ridge_return < 0 and base.p_none <= settings.P_NONE_MAX_FOR_SIGNAL:
            direction_hat = "DOWN"
        else:
            direction_hat = "NONE"

        features = {
            **base.features,
            "ridge_return_pred": ridge_return,
            "entry_mid": entry_mid,
        }

        return PredictionOutput(
            p_up=base.p_up,
            p_down=base.p_down,
            p_none=base.p_none,
            t_up=base.t_up,
            t_down=base.t_down,
            slope_pred=ridge_return,
            ev=base.ev,
            direction_hat=direction_hat,
            model_version=self.MODEL_VERSION,
            features=features,
            z_barrier=base.z_barrier,
            p_hit_base=base.p_hit_base,
            ev_rate=base.ev_rate,
            r_none_pred=base.r_none_pred,
            t_up_cond_pred=base.t_up_cond_pred,
            t_down_cond_pred=base.t_down_cond_pred,
            mom_z=base.mom_z,
            spread_bps=base.spread_bps,
            imb_notional_top5=base.imb_notional_top5,
            action_hat=action_hat,
        )

    def _wrap_with_version(self, base: PredictionOutput) -> PredictionOutput:
        """모델 버전만 교체하여 baseline 결과를 그대로 반환."""
        return PredictionOutput(
            p_up=base.p_up,
            p_down=base.p_down,
            p_none=base.p_none,
            t_up=base.t_up,
            t_down=base.t_down,
            slope_pred=base.slope_pred,
            ev=base.ev,
            direction_hat=base.direction_hat,
            model_version=self.MODEL_VERSION,
            features=base.features,
            z_barrier=base.z_barrier,
            p_hit_base=base.p_hit_base,
            ev_rate=base.ev_rate,
            r_none_pred=base.r_none_pred,
            t_up_cond_pred=base.t_up_cond_pred,
            t_down_cond_pred=base.t_down_cond_pred,
            mom_z=base.mom_z,
            spread_bps=base.spread_bps,
            imb_notional_top5=base.imb_notional_top5,
            action_hat=base.action_hat,
        )


def _resolve_hgbr_path(model_path: str, h_sec: int) -> Path:
    if model_path:
        return Path(model_path)
    return Path(f"artifacts/ml1/h{h_sec}/ridge_model.joblib")


class HGBRPredictor(BaseModel):
    """HistGradientBoostingRegressor 파이프라인 래퍼.

    RidgePredictor와 동일한 추론 흐름을 사용하지만
    비선형 HGBR 모델을 로드한다.
    ``train_and_trade_econ_gate.py``로 학습한 ``ridge_model.joblib``
    (이름은 ridge이지만 hgbr 객체도 동일 파일명으로 저장됨)을 사용.
    """

    MODEL_VERSION = "hgbr_v1"

    def __init__(self, model_path: str = "", h_sec: int = 120) -> None:
        try:
            import joblib
        except ImportError as exc:
            raise ImportError("joblib이 설치되어 있지 않습니다.") from exc

        path = _resolve_hgbr_path(model_path, h_sec)
        if not path.exists():
            raise FileNotFoundError(
                f"HGBR 모델 파일을 찾을 수 없습니다: {path}"
            )
        self._pipeline = joblib.load(path)
        log.info("HGBRPredictor: 모델 로드 완료 — %s", path)

        feat_path = path.parent / "feature_cols.json"
        if feat_path.exists():
            with open(feat_path) as f:
                loaded = json.load(f)
            self._feature_cols = loaded if loaded else _FEATURE_COLS
        else:
            self._feature_cols = _FEATURE_COLS

    def predict(
        self,
        *,
        market_window: list,
        barrier_row: dict,
        settings,
    ) -> PredictionOutput:
        base = _BASELINE.predict(
            market_window=market_window,
            barrier_row=barrier_row,
            settings=settings,
        )

        if base.z_barrier is None:
            return self._wrap_baseline(base)

        entry_mid: float | None = None
        for r in reversed(market_window):
            v = r.get("mid_close_1s") or r.get("mid")
            if v is not None and v > 0:
                entry_mid = float(v)
                break

        if entry_mid is None:
            return self._wrap_baseline(base)

        feat_map = {
            "p_up": base.p_up,
            "p_down": base.p_down,
            "p_none": base.p_none,
            "ev": base.ev,
            "ev_rate": base.ev_rate if base.ev_rate is not None else 0.0,
            "r_t": barrier_row.get("r_t", settings.R_MIN),
            "z_barrier": base.z_barrier,
            "spread_bps": base.spread_bps if base.spread_bps is not None else 0.0,
            "mom_z": base.mom_z if base.mom_z is not None else 0.0,
            "imb_notional_top5": base.imb_notional_top5 if base.imb_notional_top5 is not None else 0.0,
            "entry_mid": entry_mid,
        }

        try:
            X = np.array([[feat_map[c] for c in self._feature_cols]], dtype=np.float64)
            hgbr_return = float(self._pipeline.predict(X)[0])
        except Exception:
            log.exception("HGBRPredictor: 추론 실패 — baseline으로 폴백")
            return self._wrap_baseline(base)

        ev_rate = base.ev_rate
        spread_bps_val = base.spread_bps or 0.0

        if (
            hgbr_return > 0
            and ev_rate is not None
            and ev_rate >= settings.ENTER_EV_RATE_TH
            and base.p_none <= settings.ENTER_PNONE_MAX
            and base.p_up >= base.p_down + settings.ENTER_PDIR_MARGIN
            and spread_bps_val <= settings.ENTER_SPREAD_BPS_MAX
        ):
            action_hat = "ENTER_LONG"
        else:
            action_hat = "STAY_FLAT"

        if hgbr_return > 0 and base.p_none <= settings.P_NONE_MAX_FOR_SIGNAL:
            direction_hat = "UP"
        elif hgbr_return < 0 and base.p_none <= settings.P_NONE_MAX_FOR_SIGNAL:
            direction_hat = "DOWN"
        else:
            direction_hat = "NONE"

        features = {**base.features, "hgbr_return_pred": hgbr_return, "entry_mid": entry_mid}

        return PredictionOutput(
            p_up=base.p_up,
            p_down=base.p_down,
            p_none=base.p_none,
            t_up=base.t_up,
            t_down=base.t_down,
            slope_pred=hgbr_return,
            ev=base.ev,
            direction_hat=direction_hat,
            model_version=self.MODEL_VERSION,
            features=features,
            z_barrier=base.z_barrier,
            p_hit_base=base.p_hit_base,
            ev_rate=base.ev_rate,
            r_none_pred=base.r_none_pred,
            t_up_cond_pred=base.t_up_cond_pred,
            t_down_cond_pred=base.t_down_cond_pred,
            mom_z=base.mom_z,
            spread_bps=base.spread_bps,
            imb_notional_top5=base.imb_notional_top5,
            action_hat=action_hat,
        )

    def _wrap_baseline(self, base: PredictionOutput) -> PredictionOutput:
        return PredictionOutput(
            p_up=base.p_up, p_down=base.p_down, p_none=base.p_none,
            t_up=base.t_up, t_down=base.t_down, slope_pred=base.slope_pred,
            ev=base.ev, direction_hat=base.direction_hat,
            model_version=self.MODEL_VERSION, features=base.features,
            z_barrier=base.z_barrier, p_hit_base=base.p_hit_base,
            ev_rate=base.ev_rate, r_none_pred=base.r_none_pred,
            t_up_cond_pred=base.t_up_cond_pred, t_down_cond_pred=base.t_down_cond_pred,
            mom_z=base.mom_z, spread_bps=base.spread_bps,
            imb_notional_top5=base.imb_notional_top5, action_hat=base.action_hat,
        )
