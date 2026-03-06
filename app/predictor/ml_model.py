"""
예측기 모듈 — ModelSpec 레지스트리 + ModelFactory 팩토리 패턴

ACTIVE_MODEL 환경변수 하나로 모델 교체:
  ridge_h3600  →  artifacts/ml_prod/h3600/…/ridge_model.joblib  (H=1h, γ=1.5)
  ridge_h600   →  artifacts/ml_prod/h600/…/ridge_model.joblib   (H=10m, γ=1.5)
  ridge_h120   →  artifacts/ml_prod/h120/…/ridge_model.joblib   (H=2m,  γ=1.5)
  hgbr_h600    →  artifacts/ml_prod/h600/…/hgbr/ridge_model.joblib
  hgbr_h120    →  artifacts/ml_prod/h120/…/hgbr/ridge_model.joblib
  baseline_v1  →  BaselineModelV1 (규칙 기반, 모델 파일 없음)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.models.baseline_v1 import BaselineModelV1
from app.models.interface import BaseModel, PredictionOutput

log = logging.getLogger(__name__)

_BASELINE = BaselineModelV1()


# ══════════════════════════════════════════════════════════════════════════════
# 1. ModelSpec — 레지스트리 엔트리 (변경 불가 메타)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ModelSpec:
    """레지스트리에 등록된 모델의 정적 메타 정보."""
    model_id: str       # ACTIVE_MODEL 식별자
    model_class: str    # "ridge" | "hgbr" | "baseline"
    artifact_path: str  # joblib 상대 경로 (baseline이면 빈 문자열)
    h_sec: int          # 예측 호흡 (초)
    gamma: float        # 진입 문턱 배수 (|return| > γ × cost)
    version: str        # predictions 테이블에 기록할 model_version 태그
    display_name: str   # 대시보드 표시명


# ── 등록된 모델 레지스트리 ────────────────────────────────────────────────────
_REGISTRY: dict[str, ModelSpec] = {
    "ridge_h3600": ModelSpec(
        model_id="ridge_h3600",
        model_class="ridge",
        artifact_path="artifacts/ml_prod/h3600/historical_dataset_ridge/ridge_model.joblib",
        h_sec=3600,
        gamma=1.5,
        version="ridge_h3600_v1",
        display_name="Ridge H1h (sign_acc 82%, γ=1.5)",
    ),
    "ridge_h600": ModelSpec(
        model_id="ridge_h600",
        model_class="ridge",
        artifact_path="artifacts/ml_prod/h600/historical_dataset_ridge/ridge_model.joblib",
        h_sec=600,
        gamma=1.5,
        version="ridge_h600_v1",
        display_name="Ridge H10m (γ=1.5)",
    ),
    "ridge_h120": ModelSpec(
        model_id="ridge_h120",
        model_class="ridge",
        artifact_path="artifacts/ml_prod/h120/historical_dataset_ridge/ridge_model.joblib",
        h_sec=120,
        gamma=1.5,
        version="ridge_h120_v1",
        display_name="Ridge H2m (γ=1.5)",
    ),
    "hgbr_h600": ModelSpec(
        model_id="hgbr_h600",
        model_class="hgbr",
        artifact_path="artifacts/ml_prod/h600/historical_dataset_hgbr/ridge_model.joblib",
        h_sec=600,
        gamma=1.5,
        version="hgbr_h600_v1",
        display_name="HGBR H10m (γ=1.5)",
    ),
    "hgbr_h120": ModelSpec(
        model_id="hgbr_h120",
        model_class="hgbr",
        artifact_path="artifacts/ml_prod/h120/historical_dataset_hgbr/ridge_model.joblib",
        h_sec=120,
        gamma=1.5,
        version="hgbr_h120_v1",
        display_name="HGBR H2m (γ=1.5)",
    ),
    "baseline_v1": ModelSpec(
        model_id="baseline_v1",
        model_class="baseline",
        artifact_path="",
        h_sec=120,
        gamma=0.0,
        version="baseline_v1",
        display_name="Baseline (규칙 기반)",
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# 2. 공유 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def _load_pipeline(path: Path, label: str):
    """joblib 파이프라인 로드. 실패 시 명확한 에러 메시지."""
    try:
        import joblib
    except ImportError as exc:
        raise ImportError("joblib이 설치되어 있지 않습니다. `pip install joblib` 실행 필요") from exc
    if not path.exists():
        raise FileNotFoundError(
            f"{label} 모델 파일을 찾을 수 없습니다: {path}\n"
            "artifacts/ml_prod/h{H_SEC}/ 폴더 구조를 확인하세요."
        )
    pipeline = joblib.load(path)
    log.info("%s: 모델 로드 완료 — %s", label, path)
    return pipeline


def _load_feature_cols(path: Path, fallback: list[str]) -> list[str]:
    """모델 디렉터리의 feature_cols.json 로드. 없으면 fallback 사용."""
    feat_path = path.parent / "feature_cols.json"
    if feat_path.exists():
        with open(feat_path) as f:
            cols = json.load(f)
        if cols:
            log.info("feature_cols 로드 — %d개: %s", len(cols), cols)
            return cols
    return fallback


def _build_feat_map(base: PredictionOutput, barrier_row: dict, entry_mid: float, settings) -> dict:
    """학습 피처 19개를 동일한 순서로 구성한 딕셔너리 반환.

    barrier_row에 alt 피처(buy_volume_ratio 등)가 주입되어 있으면 사용하고,
    없으면 0.0으로 폴백한다 (GCP 미연결 시에도 추론 가능).
    """
    return {
        "r_t":              barrier_row.get("r_t", settings.R_MIN),
        "sigma_1s":         barrier_row.get("sigma_1s") or 0.0,
        "sigma_h":          barrier_row.get("sigma_h") or 0.0,
        "p_up":             base.p_up,
        "p_down":           base.p_down,
        "p_none":           base.p_none,
        "ev":               base.ev,
        "ev_rate":          base.ev_rate if base.ev_rate is not None else 0.0,
        "z_barrier":        base.z_barrier,
        "mom_z":            base.mom_z if base.mom_z is not None else 0.0,
        "spread_bps":       base.spread_bps if base.spread_bps is not None else 0.0,
        "imb_notional_top5": base.imb_notional_top5 if base.imb_notional_top5 is not None else 0.0,
        # Alt / Macro 피처 — PredictionRunner.fetch_alt_row()가 barrier_row에 주입
        "buy_volume_ratio": barrier_row.get("buy_volume_ratio") or 0.0,
        "funding_rate":     barrier_row.get("funding_rate") or 0.0,
        "long_short_ratio": barrier_row.get("long_short_ratio") or 0.0,
        "open_interest":    barrier_row.get("open_interest") or 0.0,
        "dxy_index":        barrier_row.get("dxy_index") or 0.0,
        "fear_greed_index": barrier_row.get("fear_greed_index") or 0.0,
        "entry_mid":        entry_mid,
    }


def _compute_action_hat(pred_return: float, gamma: float, settings) -> str:
    """gamma 기반 진입 신호 산출.

    |pred_return| > gamma × cost_roundtrip 일 때 방향성 신호 발생.
    gamma=0 이면 항상 STAY_FLAT (baseline 모드).
    """
    if gamma <= 0:
        return "STAY_FLAT"
    cost = 2 * settings.FEE_RATE + 2 * (settings.SLIPPAGE_BPS / 10000.0)
    threshold = gamma * cost
    if pred_return > threshold:
        return "ENTER_LONG"
    if pred_return < -threshold:
        return "ENTER_SHORT"
    return "STAY_FLAT"


def _compute_direction_hat(pred_return: float, p_none: float, settings) -> str:
    if pred_return > 0 and p_none <= settings.P_NONE_MAX_FOR_SIGNAL:
        return "UP"
    if pred_return < 0 and p_none <= settings.P_NONE_MAX_FOR_SIGNAL:
        return "DOWN"
    return "NONE"


def _extract_entry_mid(market_window: list) -> float | None:
    for r in reversed(market_window):
        v = r.get("mid_close_1s") or r.get("mid")
        if v is not None and v > 0:
            return float(v)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 3. 예측기 클래스
# ══════════════════════════════════════════════════════════════════════════════

# 11개 기본 피처 (feature_cols.json 없을 때 폴백 — 구버전 호환)
_FEATURE_COLS_LEGACY = [
    "p_up", "p_down", "p_none", "ev", "ev_rate",
    "r_t", "z_barrier", "spread_bps", "mom_z", "imb_notional_top5", "entry_mid",
]

# 19개 표준 피처 (h3600 학습 기준)
_FEATURE_COLS_V2 = [
    "r_t", "sigma_1s", "sigma_h",
    "p_up", "p_down", "p_none",
    "ev", "ev_rate", "z_barrier",
    "mom_z", "spread_bps", "imb_notional_top5",
    "buy_volume_ratio", "funding_rate", "long_short_ratio",
    "open_interest", "dxy_index", "fear_greed_index",
    "entry_mid",
]


class RidgePredictor(BaseModel):
    """Ridge+StandardScaler 파이프라인 기반 예측기.

    ModelFactory.create('ridge_h3600') 등을 통해 인스턴스화됩니다.
    """

    def __init__(
        self,
        model_path: str = "",
        h_sec: int = 3600,
        gamma: float = 1.5,
        version: str = "ridge_h3600_v1",
    ) -> None:
        path = Path(model_path) if model_path else Path(f"artifacts/ml_prod/h{h_sec}/historical_dataset_ridge/ridge_model.joblib")
        self._pipeline = _load_pipeline(path, "RidgePredictor")
        self._feature_cols = _load_feature_cols(path, _FEATURE_COLS_V2)
        self._gamma = gamma
        self.MODEL_VERSION = version

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
            return self._wrap(base)

        entry_mid = _extract_entry_mid(market_window)
        if entry_mid is None:
            return self._wrap(base)

        feat_map = _build_feat_map(base, barrier_row, entry_mid, settings)

        try:
            X = np.array([[feat_map[c] for c in self._feature_cols]], dtype=np.float64)
            ridge_return = float(self._pipeline.predict(X)[0])
        except Exception:
            log.exception("RidgePredictor: 추론 실패 — baseline으로 폴백")
            return self._wrap(base)

        action_hat = _compute_action_hat(ridge_return, self._gamma, settings)
        direction_hat = _compute_direction_hat(ridge_return, base.p_none, settings)

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
            features={**base.features, "ridge_return_pred": ridge_return, "entry_mid": entry_mid},
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

    def _wrap(self, base: PredictionOutput) -> PredictionOutput:
        """model_version만 교체해 baseline 결과를 그대로 반환."""
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

    # 하위 호환성 alias
    _wrap_with_version = _wrap


class HGBRPredictor(BaseModel):
    """HistGradientBoostingRegressor 파이프라인 기반 예측기.

    RidgePredictor와 동일한 추론 흐름 + gamma 기반 action_hat을 사용.
    """

    def __init__(
        self,
        model_path: str = "",
        h_sec: int = 120,
        gamma: float = 1.5,
        version: str = "hgbr_h120_v1",
    ) -> None:
        path = Path(model_path) if model_path else Path(f"artifacts/ml_prod/h{h_sec}/historical_dataset_hgbr/ridge_model.joblib")
        self._pipeline = _load_pipeline(path, "HGBRPredictor")
        self._feature_cols = _load_feature_cols(path, _FEATURE_COLS_V2)
        self._gamma = gamma
        self.MODEL_VERSION = version

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
            return self._wrap(base)

        entry_mid = _extract_entry_mid(market_window)
        if entry_mid is None:
            return self._wrap(base)

        feat_map = _build_feat_map(base, barrier_row, entry_mid, settings)

        try:
            X = np.array([[feat_map[c] for c in self._feature_cols]], dtype=np.float64)
            hgbr_return = float(self._pipeline.predict(X)[0])
        except Exception:
            log.exception("HGBRPredictor: 추론 실패 — baseline으로 폴백")
            return self._wrap(base)

        action_hat = _compute_action_hat(hgbr_return, self._gamma, settings)
        direction_hat = _compute_direction_hat(hgbr_return, base.p_none, settings)

        return PredictionOutput(
            p_up=base.p_up, p_down=base.p_down, p_none=base.p_none,
            t_up=base.t_up, t_down=base.t_down, slope_pred=hgbr_return,
            ev=base.ev, direction_hat=direction_hat,
            model_version=self.MODEL_VERSION,
            features={**base.features, "hgbr_return_pred": hgbr_return, "entry_mid": entry_mid},
            z_barrier=base.z_barrier, p_hit_base=base.p_hit_base,
            ev_rate=base.ev_rate, r_none_pred=base.r_none_pred,
            t_up_cond_pred=base.t_up_cond_pred, t_down_cond_pred=base.t_down_cond_pred,
            mom_z=base.mom_z, spread_bps=base.spread_bps,
            imb_notional_top5=base.imb_notional_top5, action_hat=action_hat,
        )

    def _wrap(self, base: PredictionOutput) -> PredictionOutput:
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

    # 하위 호환성 alias
    _wrap_baseline = _wrap


# ══════════════════════════════════════════════════════════════════════════════
# 4. ModelFactory — ACTIVE_MODEL → BaseModel 인스턴스
# ══════════════════════════════════════════════════════════════════════════════

class ModelFactory:
    """ACTIVE_MODEL 식별자를 받아 올바르게 구성된 BaseModel 인스턴스를 반환한다.

    사용법::

        model = ModelFactory.create("ridge_h3600")
        # .env에서: ACTIVE_MODEL=ridge_h3600

    등록 가능한 모델 목록::

        ModelFactory.list_models()  # → ['ridge_h3600', 'ridge_h600', ...]
    """

    @staticmethod
    def list_models() -> list[str]:
        """등록된 모든 모델 ID를 반환."""
        return list(_REGISTRY.keys())

    @staticmethod
    def get_spec(model_id: str) -> ModelSpec:
        """모델 ID → ModelSpec 반환. 미등록 ID면 ValueError."""
        if model_id not in _REGISTRY:
            available = ", ".join(_REGISTRY.keys())
            raise ValueError(
                f"ACTIVE_MODEL='{model_id}'은 레지스트리에 없습니다.\n"
                f"사용 가능한 모델: {available}"
            )
        return _REGISTRY[model_id]

    @staticmethod
    def create(model_id: str) -> BaseModel:
        """모델 ID → BaseModel 인스턴스 생성.

        ModelSpec의 artifact_path, h_sec, gamma, version을 자동으로 주입한다.
        """
        spec = ModelFactory.get_spec(model_id)
        log.info(
            "ModelFactory.create: model_id=%s class=%s h_sec=%d gamma=%.1f version=%s",
            model_id, spec.model_class, spec.h_sec, spec.gamma, spec.version,
        )
        if spec.model_class == "ridge":
            return RidgePredictor(
                model_path=spec.artifact_path,
                h_sec=spec.h_sec,
                gamma=spec.gamma,
                version=spec.version,
            )
        if spec.model_class == "hgbr":
            return HGBRPredictor(
                model_path=spec.artifact_path,
                h_sec=spec.h_sec,
                gamma=spec.gamma,
                version=spec.version,
            )
        # baseline — joblib 없음
        log.info("ModelFactory.create: BaselineModelV1 선택")
        return BaselineModelV1()
