"""
app/rl/crypto_env.py — CryptoTradingEnv  (Two-Stage RL Trading Environment)
=============================================================================

Gymnasium 인터페이스를 준수하는 BTC 1분봉 연속 액션 트레이딩 환경.

설계 원칙
----------
- 오라클 추론은 환경 외부에서 사전 계산 → 'xgb_prob', 'ridge_pred' 열로 주입
- 연속형 액션: target_position ∈ [-1, 1]  (-1=풀숏, 0=관망, +1=풀롱)
- 포지션 변화(delta)에 비례하는 수수료 + 이차 휩소 패널티

Observation (1-D flat vector, float32)
----------------------------------------
  [ window_size × n_feat_cols  |  position  |  unrealized_pnl_bps ]
    = 10 × (67 + 2) + 2 = 692 dim (기본값)

  n_feat_cols = 67 scaled features + xgb_prob + ridge_pred = 69

Reward  (단위: basis points, 1 bps = 0.01%)
---------------------------------------------
  r_t = pos_t × log(P_{t+1} / P_t) × 10_000        ← 미실현 → 실현 PnL
       - |Δpos_t| × fee_rate × 10_000                ← 거래 수수료
       - whipsaw_coeff × Δpos_t²                     ← 빈번한 방향 전환 패널티

  여기서:
    Δpos_t = target_pos - pos_{t-1}
    fee_rate = 0.0005 (0.05% 단방향, 왕복이면 2× = 0.1%)

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
    obs, reward, terminated, truncated, info = env.step(np.array([0.5]))
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
    whipsaw_coeff : float
        포지션 변화의 이차 패널티 계수 (λ_w × Δpos²). 기본 0.001.
    max_position : float
        허용 최대 포지션 절대값. 기본 1.0.
    """

    metadata: dict[str, Any] = {"render_modes": []}

    def __init__(
        self,
        df: pd.DataFrame,
        feature_cols: list[str],
        window_size: int = 10,
        fee_rate: float = 0.0005,
        whipsaw_coeff: float = 0.001,
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
        self.whipsaw_coeff = whipsaw_coeff
        self.max_position = max_position

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
        self.action_space = spaces.Box(
            low=-1.0,
            high=1.0,
            shape=(1,),
            dtype=np.float32,
        )

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
        self, action: np.ndarray
    ) -> tuple[np.ndarray, float, bool, bool, dict]:
        """
        Parameters
        ----------
        action : np.ndarray shape (1,)
            목표 포지션 [-1, 1].

        Returns
        -------
        obs, reward (bps), terminated, truncated=False, info
        """
        # ── 액션 처리 ────────────────────────────────────────────────────────
        target_pos = float(
            np.clip(action[0], -self.max_position, self.max_position)
        )
        delta = target_pos - self._position

        idx = self._current_idx()
        price_t = self._close_arr[idx]
        price_t1 = self._close_arr[idx + 1]   # 다음 봉 종가

        # ── 수수료: |Δpos| × fee_rate (bps 단위) ────────────────────────────
        fee_bps = abs(delta) * self.fee_rate * REWARD_SCALE

        # ── 진입 가격 업데이트 (VWAP 방식 가중 평균) ─────────────────────────
        prev_pos = self._position
        if abs(delta) > 1e-6:
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

        # ── 휩소 패널티 (Δpos² 이차 패널티) ─────────────────────────────────
        # 소폭 조정은 관용, 급격한 방향 전환은 강하게 패널티
        whipsaw_penalty = float(self.whipsaw_coeff * delta ** 2 * REWARD_SCALE)

        reward = pnl_bps - fee_bps - whipsaw_penalty

        # ── 거래 기록 ─────────────────────────────────────────────────────────
        self._trades.append(
            {
                "step": self._step,
                "idx": idx,
                "price": price_t,
                "prev_position": prev_pos,
                "position": self._position,
                "delta": delta,
                "log_ret": float(log_ret),
                "pnl_bps": pnl_bps,
                "fee_bps": fee_bps,
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
