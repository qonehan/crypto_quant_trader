"""
app/rl/crypto_env.py — CryptoTradingEnv  (Two-Stage RL Trading Environment)
=============================================================================

Gymnasium 인터페이스를 준수하는 BTC 1분봉 이산형 액션 트레이딩 환경.

설계 원칙
----------
- 오라클 추론은 환경 외부에서 사전 계산 → 'xgb_prob', 'ridge_pred' 열로 주입
- 이산형 액션: Discrete(3)
    Action 0 → 포지션 -1.0  (풀 숏)
    Action 1 → 포지션  0.0  (관망 / 전액 청산)
    Action 2 → 포지션 +1.0  (풀 롱)
- 포지션 변화 시 수수료 + 고정 행동 변경 패널티(action_change_penalty)로
  의미 없는 포지션 스위칭을 강력히 억제

Observation (1-D flat vector, float32)
----------------------------------------
  [ window_size × n_feat_cols  |  position  |  unrealized_pnl_bps ]
    = 10 × (67 + 2) + 2 = 692 dim (기본값)

  n_feat_cols = 67 scaled features + xgb_prob + ridge_pred = 69

Reward  (단위: basis points, 1 bps = 0.01%)
---------------------------------------------
  r_t = pos_t × log(P_{t+1} / P_t) × 10_000        ← 미실현 → 실현 PnL
       - |Δpos_t| × fee_rate × 10_000                ← 거래 수수료
       - action_change_penalty  (포지션이 실제로 변경될 때만)
       - whipsaw_coeff × Δpos_t²                     ← 선택적 이차 패널티 (기본 0)

  여기서:
    Δpos_t = target_pos - pos_{t-1}
    fee_rate = 0.0005 (0.05% 단방향)
    action_change_penalty = 2.0 bps (기본; 포지션 변경 시 1회 고정 부과)

Usage Example
--------------
    df = pd.read_parquet("btc_1m_hft_v2.parquet")
    # ... (외부에서 xgb_prob, ridge_pred 사전 계산 후 열 추가) ...
    env = CryptoTradingEnv(
        df=df_with_oracle,
        feature_cols=feat_cols,      # 69 cols: 67 + xgb_prob + ridge_pred
        window_size=10,
    )
    obs, info = env.reset()
    obs, reward, terminated, truncated, info = env.step(2)   # Action 2 = 풀롱
"""

from __future__ import annotations

from typing import Any

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces

# ══════════════════════════════════════════════════════════════════════════════
# 상수
# ══════════════════════════════════════════════════════════════════════════════

REWARD_SCALE = 10_000.0   # PnL 단위를 bps로 변환 (1 bps = 0.01%)

# 이산형 액션 → 목표 포지션 매핑
_ACTION_TO_POSITION: dict[int, float] = {
    0: -1.0,   # 풀 숏
    1:  0.0,   # 관망 / 전액 청산
    2:  1.0,   # 풀 롱
}


# ══════════════════════════════════════════════════════════════════════════════
# CryptoTradingEnv
# ══════════════════════════════════════════════════════════════════════════════


class CryptoTradingEnv(gym.Env):
    """
    Parameters
    ----------
    df : pd.DataFrame
        반드시 'close' 열과 feature_cols에 지정된 열을 포함해야 함.
        'xgb_prob', 'ridge_pred' 는 이미 계산된 열로 포함되어 있어야 함.
        인덱스는 정수(RangeIndex)로 reset 된 상태를 권장.
    feature_cols : list[str]
        관측에 사용할 피처 열 이름 목록 (예: 67 scaled feats + xgb_prob + ridge_pred).
    window_size : int
        슬라이딩 윈도우 크기 (분봉 개수). 기본 10.
    fee_rate : float
        단방향 수수료율. 기본 0.0005 (0.05%).
    action_change_penalty : float
        포지션이 실제로 변경될 때 부과하는 고정 패널티 (단위: bps). 기본 2.0.
        수수료와 별개로 추가 부과되어 잦은 포지션 스위칭을 강력히 억제.
    whipsaw_coeff : float
        포지션 변화의 이차 패널티 계수 (λ_w × Δpos²). 기본 0.0.
        이산형 환경에서는 action_change_penalty 로 대체하므로 기본 비활성화.
    """

    metadata: dict[str, Any] = {"render_modes": []}

    def __init__(
        self,
        df: pd.DataFrame,
        feature_cols: list[str],
        window_size: int = 10,
        fee_rate: float = 0.0005,
        action_change_penalty: float = 2.0,
        whipsaw_coeff: float = 0.0,
        # max_position은 이산형 환경에서 사용되지 않지만 하위 호환성을 위해 유지
        max_position: float = 1.0,
    ) -> None:
        super().__init__()

        # ── 데이터 ──────────────────────────────────────────────────────────
        self.df = df.reset_index(drop=True)
        self.feature_cols = feature_cols
        self.n_feat = len(feature_cols)

        # ── 하이퍼파라미터 ──────────────────────────────────────────────────
        self.window_size = window_size
        self.fee_rate = fee_rate
        self.action_change_penalty = action_change_penalty
        self.whipsaw_coeff = whipsaw_coeff
        self.max_position = max_position   # 이산형에서는 미사용 (호환성 보존)

        # ── 유효 스텝 범위: window_size ≤ idx ≤ N-2 (next close 필요) ──────
        self._n_rows = len(self.df)
        self._max_steps = self._n_rows - window_size - 1  # 에피소드 길이

        if self._max_steps <= 0:
            raise ValueError(
                f"데이터({self._n_rows}행)가 window_size({window_size})+1 보다 적습니다."
            )

        # ── 관측 / 액션 공간 ────────────────────────────────────────────────
        n_obs = self.n_feat * window_size + 2   # flat feats + position + unrealized
        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(n_obs,),
            dtype=np.float32,
        )
        # 이산형 액션 공간: 0=풀숏, 1=관망, 2=풀롱
        self.action_space = spaces.Discrete(3)

        # ── 내부 상태 초기화 ─────────────────────────────────────────────────
        self._step: int = 0
        self._position: float = 0.0
        self._entry_price: float | None = None
        self._trades: list[dict] = []

        # ── 피처 행렬을 numpy로 캐싱 (속도 향상) ────────────────────────────
        self._feat_arr: np.ndarray = (
            self.df[self.feature_cols].values.astype(np.float32)
        )
        self._close_arr: np.ndarray = self.df["close"].values.astype(np.float64)
        self._xgb_arr: np.ndarray = (
            self.df["xgb_prob"].values.astype(np.float32)
            if "xgb_prob" in self.df.columns
            else np.zeros(self._n_rows, dtype=np.float32)
        )
        self._ridge_arr: np.ndarray = (
            self.df["ridge_pred"].values.astype(np.float32)
            if "ridge_pred" in self.df.columns
            else np.zeros(self._n_rows, dtype=np.float32)
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _current_idx(self) -> int:
        """현재 환경 스텝 t 에 대응하는 데이터프레임 인덱스."""
        return self._step + self.window_size

    def _get_obs(self) -> np.ndarray:
        """현재 상태에서 관측 벡터를 반환합니다."""
        idx = self._current_idx()

        # ── 슬라이딩 윈도우 (window_size × n_feat) ───────────────────────────
        window = self._feat_arr[idx - self.window_size : idx]   # (W, F)
        flat = window.flatten()                                  # (W*F,)

        # ── 미실현 PnL (bps) ─────────────────────────────────────────────────
        unrealized_bps = 0.0
        if self._position != 0.0 and self._entry_price is not None:
            cur_price = self._close_arr[idx]
            unrealized_bps = (
                self._position
                * np.log(cur_price / self._entry_price)
                * REWARD_SCALE
            )

        obs = np.concatenate(
            [flat, [np.float32(self._position), np.float32(unrealized_bps)]]
        )
        return obs.astype(np.float32)

    # ──────────────────────────────────────────────────────────────────────────
    # Gymnasium API
    # ──────────────────────────────────────────────────────────────────────────

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict | None = None,
    ) -> tuple[np.ndarray, dict]:
        super().reset(seed=seed)

        self._step = 0
        self._position = 0.0
        self._entry_price = None
        self._trades = []

        obs = self._get_obs()
        return obs, {}

    def step(
        self, action: int | np.integer
    ) -> tuple[np.ndarray, float, bool, bool, dict]:
        """
        Parameters
        ----------
        action : int  ∈ {0, 1, 2}
            0 → 풀 숏 (-1.0),  1 → 관망 (0.0),  2 → 풀 롱 (+1.0)

        Returns
        -------
        obs, reward (bps), terminated, truncated=False, info
        """
        # ── 이산 액션 → 목표 포지션 변환 ────────────────────────────────────
        action_int = int(action)
        target_pos = _ACTION_TO_POSITION[action_int]
        delta = target_pos - self._position

        idx = self._current_idx()
        price_t = self._close_arr[idx]
        price_t1 = self._close_arr[idx + 1]   # 다음 봉 종가

        # ── 수수료: |Δpos| × fee_rate (bps 단위) ────────────────────────────
        fee_bps = abs(delta) * self.fee_rate * REWARD_SCALE

        # ── 고정 행동 변경 패널티 (포지션이 실제로 바뀔 때만) ────────────────
        prev_pos = self._position
        position_changed = abs(delta) > 1e-6
        fixed_penalty_bps = self.action_change_penalty if position_changed else 0.0

        # ── 진입 가격 업데이트 ────────────────────────────────────────────────
        if position_changed:
            if abs(target_pos) < 1e-6:
                # 완전 청산 → 진입가 초기화
                self._entry_price = None
            elif self._entry_price is None:
                self._entry_price = price_t
            else:
                total = abs(self._position) + abs(delta)
                self._entry_price = (
                    abs(self._position) * self._entry_price + abs(delta) * price_t
                ) / total
        self._position = target_pos

        # ── 1-step PnL (bps) ─────────────────────────────────────────────────
        log_ret = np.log(price_t1 / price_t)
        pnl_bps = float(self._position * log_ret * REWARD_SCALE)

        # ── 선택적 이차 휩소 패널티 (기본 0.0) ───────────────────────────────
        whipsaw_penalty = float(self.whipsaw_coeff * delta ** 2 * REWARD_SCALE)

        reward = pnl_bps - fee_bps - fixed_penalty_bps - whipsaw_penalty

        # ── 거래 기록 ─────────────────────────────────────────────────────────
        self._trades.append(
            {
                "step": self._step,
                "idx": idx,
                "price": price_t,
                "prev_position": prev_pos,
                "position": self._position,
                "action": action_int,
                "delta": delta,
                "log_ret": float(log_ret),
                "pnl_bps": pnl_bps,
                "fee_bps": fee_bps,
                "fixed_penalty_bps": fixed_penalty_bps,
                "whipsaw_penalty": whipsaw_penalty,
                "reward": reward,
                "xgb_prob": float(self._xgb_arr[idx]),
                "ridge_pred": float(self._ridge_arr[idx]),
            }
        )

        # ── 스텝 진행 ─────────────────────────────────────────────────────────
        self._step += 1
        terminated = self._step >= self._max_steps

        if terminated:
            obs = np.zeros(self.observation_space.shape, dtype=np.float32)
        else:
            obs = self._get_obs()

        info = {
            "pnl_bps": pnl_bps,
            "fee_bps": fee_bps,
            "fixed_penalty_bps": fixed_penalty_bps,
            "position": self._position,
        }
        return obs, float(reward), terminated, False, info

    # ──────────────────────────────────────────────────────────────────────────
    # Utilities
    # ──────────────────────────────────────────────────────────────────────────

    def get_trajectory(self) -> pd.DataFrame:
        """에피소드 전체 거래 기록을 DataFrame으로 반환."""
        return pd.DataFrame(self._trades)

    def render(self) -> None:
        pass
