#!/usr/bin/env python
"""
scripts/rl/step_rl_1_train_ppo.py  —  Two-Stage RL PPO 학습 파이프라인 (Discrete Action)
=========================================================================================

전체 흐름
----------
  1. 데이터 로드 & 전처리 (RobustScaler, train-fit only)
  2. 오라클 학습
       - XGBoost Classifier  (타겟: future_ret_15 > 0 → 분류)
       - Ridge Regressor      (타겟: future_ret_15 raw float → 회귀)
  3. 전체 데이터프레임에 xgb_prob / ridge_pred 사전 계산 (미래 누수 없음)
  4. Train / Val / Test 환경 구성 (CryptoTradingEnv)
  5. PPO 학습 (Stable-Baselines3, TensorBoard, Checkpoint 저장)
  6. Test 구간에서 에이전트 궤적 시뮬레이션
  7. 비교 전략 계산 (Buy&Hold, XGBoost-only)
  8. 자동 결과 보고서 생성 → artifacts/rl_training_report.md

실행 예시
----------
  # 정식 학습 (기본 5M 스텝)
  poetry run python scripts/rl/step_rl_1_train_ppo.py

  # 스모크 테스트 (빠른 검증, 50K 스텝 + 데이터 10K행)
  poetry run python scripts/rl/step_rl_1_train_ppo.py --smoke

  # 학습 스텝 수 조정
  poetry run python scripts/rl/step_rl_1_train_ppo.py --timesteps 10000000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import RobustScaler
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import (
    BaseCallback,
    CheckpointCallback,
    EvalCallback,
)
from stable_baselines3.common.env_checker import check_env
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

warnings.filterwarnings("ignore", message="X does not have valid feature names")
warnings.filterwarnings("ignore", category=UserWarning, module="stable_baselines3")

# ── 프로젝트 루트 추가 ─────────────────────────────────────────────────────
_PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJ))

import xgboost as xgb  # noqa: E402

from app.rl.crypto_env import CryptoTradingEnv  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════════
# 전역 상수
# ══════════════════════════════════════════════════════════════════════════════

DATASET_PATH = _PROJ / "data" / "datasets" / "btc_1m_hft_v2.parquet"
ARTIFACTS_DIR = _PROJ / "artifacts" / "rl_prod"
ORACLE_DIR = ARTIFACTS_DIR / "oracles"
CKPT_DIR = ARTIFACTS_DIR / "checkpoints"
TB_LOG_DIR = ARTIFACTS_DIR / "tensorboard"
REPORT_PATH = _PROJ / "artifacts" / "rl_training_report.md"

TARGET_COL = "future_ret_15"
EXCLUDE_COLS: frozenset[str] = frozenset(
    {
        "target",
        "future_ret",
        "target_1m",
        "future_ret_1",
        "future_ret_15",
    }
)

TRAIN_RATIO = 0.80
VAL_RATIO = 0.10

# RL 환경 기본 파라미터 (Discrete Action)
ENV_KWARGS: dict[str, Any] = {
    "window_size": 10,
    "fee_rate": 0.0005,
    "action_change_penalty": 2.0,   # 포지션 변경 시 고정 패널티 (bps)
    "whipsaw_coeff": 0.0,           # 이산형에서는 비활성화 (action_change_penalty로 대체)
}

# PPO 기본 하이퍼파라미터
# Discrete action space에서 SB3 PPO는 별도 설정 없이 자동 호환됨.
# (MlpPolicy가 Categorical 분포를 자동으로 사용)
PPO_KWARGS: dict[str, Any] = {
    "learning_rate": 3e-4,
    "n_steps": 2048,
    "batch_size": 512,
    "n_epochs": 10,
    "gamma": 0.99,
    "gae_lambda": 0.95,
    "clip_range": 0.2,
    "ent_coef": 0.01,    # Discrete 환경에서 탐색 강화를 위해 0.005→0.01 상향
    "vf_coef": 0.5,
    "max_grad_norm": 0.5,
    "verbose": 1,
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. 데이터 로드 & 전처리
# ══════════════════════════════════════════════════════════════════════════════


def load_and_preprocess(
    parquet_path: Path,
    max_rows: int | None = None,
) -> tuple[pd.DataFrame, list[str], int, int]:
    """
    Returns
    -------
    df_scaled : pd.DataFrame
        전처리 완료 데이터 (RobustScaler, ffill/median 처리).
        원본 'close' 열은 스케일링하지 않고 별도 보존.
    feat_cols_raw : list[str]
        원본(비스케일) 67개 피처 열 이름 목록 (XGBoost용).
    n_tr : int
        Train 경계 인덱스.
    n_vl : int
        Val 경계 인덱스 (te starts at n_tr + n_vl).
    """
    print(f"\n[1/7] 데이터 로드: {parquet_path.name}")
    df = pd.read_parquet(parquet_path)
    df = df.dropna(subset=[TARGET_COL])

    if max_rows:
        df = df.iloc[:max_rows].copy()
        print(f"  [smoke] max_rows={max_rows:,} 적용")

    feat_cols_raw: list[str] = [c for c in df.columns if c not in EXCLUDE_COLS]
    n = len(df)
    n_tr = int(n * TRAIN_RATIO)
    n_vl = int(n * VAL_RATIO)
    print(
        f"  행 {n:,}  |  피처 {len(feat_cols_raw)}개  |  "
        f"Train {n_tr:,} / Val {n_vl:,} / Test {n - n_tr - n_vl:,}"
    )

    # ── 결측치 처리 (미래 누수 방지: ffill 후 train-median) ──────────────
    df[feat_cols_raw] = df[feat_cols_raw].ffill()
    train_median = df[feat_cols_raw].iloc[:n_tr].median()
    df[feat_cols_raw] = df[feat_cols_raw].fillna(train_median)

    # ── RobustScaler (Train fit, 전체 transform) ─────────────────────────
    scaler = RobustScaler()
    scaler.fit(df[feat_cols_raw].iloc[:n_tr])
    df_scaled = df.copy()
    # 'close' 만 원본 보존 (PnL 계산용); 나머지 피처는 스케일링
    close_backup = df["close"].copy()
    df_scaled[feat_cols_raw] = scaler.transform(df[feat_cols_raw])
    df_scaled["close"] = close_backup  # 원본 close 복원

    # ── 스케일러 저장 ─────────────────────────────────────────────────────
    ORACLE_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(scaler, ORACLE_DIR / "scaler.joblib")
    with open(ORACLE_DIR / "feature_cols.json", "w") as f:
        json.dump(feat_cols_raw, f, ensure_ascii=False, indent=2)
    print(f"  스케일러 저장 → {ORACLE_DIR / 'scaler.joblib'}")

    return df_scaled, feat_cols_raw, n_tr, n_vl


# ══════════════════════════════════════════════════════════════════════════════
# 2. 오라클 학습 & 사전 추론
# ══════════════════════════════════════════════════════════════════════════════


def train_oracles(
    df: pd.DataFrame,
    feat_cols_raw: list[str],
    n_tr: int,
) -> tuple[xgb.XGBClassifier, Ridge]:
    """XGBoost Classifier + Ridge Regressor 학습 (Train 구간만 사용)."""
    print("\n[2/7] 오라클 학습")

    X_tr_raw = df[feat_cols_raw].iloc[:n_tr].values
    X_tr_scaled = df[feat_cols_raw].iloc[:n_tr].values  # df_scaled 이미 스케일됨

    y_tr_cls = (df[TARGET_COL].iloc[:n_tr].values > 0).astype(int)
    y_tr_reg = df[TARGET_COL].iloc[:n_tr].values.astype(np.float32)

    # ── XGBoost Classifier ────────────────────────────────────────────────
    print("  XGBoost Classifier 학습 중...")
    t0 = time.time()
    xgb_clf = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        reg_alpha=0.1,
        reg_lambda=1.0,
        eval_metric="logloss",
        tree_method="hist",
        n_jobs=-1,
        random_state=42,
    )
    xgb_clf.fit(
        X_tr_raw,
        y_tr_cls,
        eval_set=[(X_tr_raw, y_tr_cls)],
        verbose=False,
    )
    print(f"    완료 ({time.time() - t0:.1f}s)")

    # ── Ridge Regressor ───────────────────────────────────────────────────
    print("  Ridge Regressor 학습 중...")
    t0 = time.time()
    ridge = Ridge(alpha=100.0, fit_intercept=True)
    ridge.fit(X_tr_scaled, y_tr_reg)
    print(f"    완료 ({time.time() - t0:.1f}s)")

    # ── 저장 ─────────────────────────────────────────────────────────────
    joblib.dump(xgb_clf, ORACLE_DIR / "xgb_classifier.joblib")
    joblib.dump(ridge, ORACLE_DIR / "ridge_regressor.joblib")
    print(f"  오라클 저장 → {ORACLE_DIR}")

    return xgb_clf, ridge


def add_oracle_predictions(
    df: pd.DataFrame,
    feat_cols_raw: list[str],
    xgb_clf: xgb.XGBClassifier,
    ridge: Ridge,
) -> pd.DataFrame:
    """전체 df에 xgb_prob, ridge_pred 열을 사전 계산하여 추가."""
    print("\n[3/7] 오라클 예측 사전 계산 (전체 데이터프레임)")

    X_raw = df[feat_cols_raw].values
    X_scaled = df[feat_cols_raw].values  # df_scaled 이미 스케일됨

    # XGBoost: predict_proba[:, 1] = 상승 확률
    xgb_prob = xgb_clf.predict_proba(X_raw)[:, 1].astype(np.float32)
    # Ridge: 예상 log-return 연속값
    ridge_pred = ridge.predict(X_scaled).astype(np.float32)

    df = df.copy()
    df["xgb_prob"] = xgb_prob
    df["ridge_pred"] = ridge_pred

    print(
        f"  xgb_prob  → mean={xgb_prob.mean():.4f}, std={xgb_prob.std():.4f}"
    )
    print(
        f"  ridge_pred → mean={ridge_pred.mean():.6f}, std={ridge_pred.std():.6f}"
    )
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 3. 환경 구성 헬퍼
# ══════════════════════════════════════════════════════════════════════════════


def make_env_fn(df_split: pd.DataFrame, feature_cols: list[str], env_kwargs: dict):
    """SB3 DummyVecEnv 용 factory 함수를 반환."""
    def _init():
        env = CryptoTradingEnv(df=df_split, feature_cols=feature_cols, **env_kwargs)
        env = Monitor(env)
        return env
    return _init


def build_vec_env(
    df_split: pd.DataFrame,
    feature_cols: list[str],
    env_kwargs: dict,
    n_envs: int = 1,
    norm_path: Path | None = None,
    training: bool = True,
) -> VecNormalize:
    """
    DummyVecEnv + VecNormalize 래핑.
    norm_path 가 주어지면 저장된 통계를 로드 (평가 전용 모드).
    """
    fns = [make_env_fn(df_split, feature_cols, env_kwargs) for _ in range(n_envs)]
    vec_env = DummyVecEnv(fns)

    if norm_path and norm_path.exists():
        vec_norm = VecNormalize.load(str(norm_path), vec_env)
        vec_norm.training = training
        vec_norm.norm_reward = False
    else:
        vec_norm = VecNormalize(
            vec_env,
            training=training,
            norm_obs=True,
            norm_reward=False,   # reward는 bps 단위로 이미 의미 있음
            clip_obs=10.0,
        )

    return vec_norm


# ══════════════════════════════════════════════════════════════════════════════
# 4. PPO 학습
# ══════════════════════════════════════════════════════════════════════════════


class ValRewardCallback(BaseCallback):
    """검증 환경에서 주기적으로 에피소드 완주 수익률을 평가하는 콜백."""

    def __init__(self, val_df: pd.DataFrame, feature_cols: list[str],
                 env_kwargs: dict, eval_freq: int = 100_000, verbose: int = 0):
        super().__init__(verbose)
        self.val_df = val_df
        self.feature_cols = feature_cols
        self.env_kwargs = env_kwargs
        self.eval_freq = eval_freq
        self._best_reward = -np.inf

    def _on_step(self) -> bool:
        if self.n_calls % self.eval_freq == 0:
            env = CryptoTradingEnv(
                df=self.val_df, feature_cols=self.feature_cols, **self.env_kwargs
            )
            obs, _ = env.reset()
            done = False
            total_reward = 0.0
            while not done:
                action, _ = self.model.predict(obs, deterministic=True)
                # Discrete 환경: action은 numpy scalar 또는 (1,) 배열 → int 변환
                obs, r, done, _, _ = env.step(int(action))
                total_reward += r
            if self.verbose > 0:
                print(
                    f"  [Val @ {self.num_timesteps:,}] "
                    f"누적 보상 = {total_reward:.2f} bps"
                )
            if self.logger:
                self.logger.record("eval/val_episode_reward", total_reward)
        return True


def train_ppo(
    train_env: VecNormalize,
    val_df: pd.DataFrame,
    feature_cols: list[str],
    env_kwargs: dict,
    total_timesteps: int,
    ppo_kwargs: dict,
) -> PPO:
    """PPO 학습 및 체크포인트 저장."""
    print(f"\n[4/7] PPO 학습 시작 (총 {total_timesteps:,} 스텝)")

    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    TB_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # SB3 PPO는 Discrete(3) action_space를 자동 감지하여
    # MlpPolicy 내부에서 Categorical 분포를 사용함 → 별도 설정 불필요
    model = PPO(
        policy="MlpPolicy",
        env=train_env,
        tensorboard_log=str(TB_LOG_DIR),
        **ppo_kwargs,
    )

    callbacks = [
        CheckpointCallback(
            save_freq=max(100_000 // 1, 1),
            save_path=str(CKPT_DIR),
            name_prefix="ppo_rl",
            verbose=0,
        ),
        ValRewardCallback(
            val_df=val_df,
            feature_cols=feature_cols,
            env_kwargs=env_kwargs,
            eval_freq=max(total_timesteps // 20, 10_000),
            verbose=1,
        ),
    ]

    t0 = time.time()
    model.learn(
        total_timesteps=total_timesteps,
        callback=callbacks,
        tb_log_name="ppo_crypto_discrete",
        reset_num_timesteps=True,
        progress_bar=True,
    )
    elapsed = time.time() - t0
    print(f"  학습 완료: {elapsed:.1f}s ({elapsed/60:.1f}분)")

    # 최종 모델 저장
    model.save(str(ARTIFACTS_DIR / "ppo_final"))
    train_env.save(str(ARTIFACTS_DIR / "vecnorm.pkl"))
    print(f"  모델 저장 → {ARTIFACTS_DIR / 'ppo_final.zip'}")

    return model


# ══════════════════════════════════════════════════════════════════════════════
# 5. 평가 유틸리티
# ══════════════════════════════════════════════════════════════════════════════


def compute_metrics(
    traj: pd.DataFrame,
    label: str,
    risk_free_bps: float = 0.0,
    steps_per_day: int = 1440,
) -> dict[str, Any]:
    """
    거래 궤적 DataFrame → 성과 지표 딕셔너리.

    Parameters
    ----------
    traj : pd.DataFrame
        'pnl_bps', 'delta', 'position' 열 필요.
    steps_per_day : int
        일일 봉 수 (1분봉=1440).
    """
    pnl = traj["pnl_bps"].values
    cum_pnl = np.cumsum(pnl)

    # 누적 수익률 (%)
    total_return_pct = cum_pnl[-1] / 100.0

    # MDD
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = running_max - cum_pnl
    mdd_bps = float(drawdowns.max())
    mdd_pct = mdd_bps / 100.0

    # Sharpe (일일 집계 → 연환산; 일수가 2 미만이면 스텝 단위로 대체)
    n_days = len(pnl) / steps_per_day
    if n_days >= 2:
        daily_chunks = np.array_split(pnl, max(int(n_days), 2))
        daily_rets = np.array([chunk.sum() for chunk in daily_chunks])
        sharpe = float(
            (daily_rets.mean() - risk_free_bps)
            / (daily_rets.std(ddof=1) + 1e-9)
            * np.sqrt(365)
        )
    else:
        # 스텝 단위 Sharpe (연환산 factor = sqrt(525_600))
        sharpe = float(
            (pnl.mean() - risk_free_bps / steps_per_day)
            / (pnl.std(ddof=1) + 1e-9)
            * np.sqrt(525_600)
        )

    # 거래 횟수 & 승률
    trades = traj[traj["delta"].abs() > 1e-3]
    n_trades = len(trades)
    win_rate = float((traj["pnl_bps"] > 0).mean())

    return {
        "label": label,
        "total_return_pct": round(total_return_pct, 4),
        "mdd_pct": round(mdd_pct, 4),
        "sharpe": round(sharpe, 4),
        "n_trades": n_trades,
        "win_rate_pct": round(win_rate * 100, 2),
    }


def run_agent_on_test(
    model: PPO,
    test_df: pd.DataFrame,
    feature_cols: list[str],
    env_kwargs: dict,
    vecnorm_path: Path,
) -> pd.DataFrame:
    """
    학습된 PPO 에이전트를 test_df 에서 실행, 궤적 반환.

    VecNormalize auto-reset 로 인한 trajectory 유실 방지:
    raw CryptoTradingEnv 를 직접 구동하고, VecNormalize 통계로
    관측값을 수동 정규화하여 model.predict() 에 전달.

    Discrete Action 처리:
    model.predict()가 반환하는 action (numpy scalar 또는 배열)을
    int()로 변환한 뒤 raw_env.step()에 전달.
    """
    print("\n[5/7] Test 구간 에이전트 시뮬레이션")

    # ── raw 환경 구성 ────────────────────────────────────────────────────────
    raw_env = CryptoTradingEnv(df=test_df, feature_cols=feature_cols, **env_kwargs)
    obs_raw, _ = raw_env.reset()

    # ── VecNormalize 통계 로드 (obs 정규화에만 사용) ─────────────────────────
    vecnorm: VecNormalize | None = None
    if vecnorm_path.exists():
        _dummy_vec = DummyVecEnv(
            [make_env_fn(test_df, feature_cols, env_kwargs)]
        )
        vecnorm = VecNormalize.load(str(vecnorm_path), _dummy_vec)
        vecnorm.training = False
        vecnorm.norm_reward = False

    def _normalize(obs: np.ndarray) -> np.ndarray:
        if vecnorm is None:
            return obs[np.newaxis]   # (1, obs_dim)
        return vecnorm.normalize_obs(obs[np.newaxis])

    # ── 에피소드 실행 루프 ────────────────────────────────────────────────────
    terminated = False
    while not terminated:
        obs_norm = _normalize(obs_raw)
        action, _ = model.predict(obs_norm, deterministic=True)
        # Discrete 환경: action은 numpy scalar or shape-(1,) 배열 → int 변환
        action_int = int(action.flatten()[0])
        obs_raw, _, terminated, _, _ = raw_env.step(action_int)

    traj = raw_env.get_trajectory()
    print(f"  시뮬레이션 완료: {len(traj):,} 스텝")
    return traj


# ──────────────────────────────────────────────────────────────────────────────
# Buy & Hold 기준선
# ──────────────────────────────────────────────────────────────────────────────


def baseline_buy_and_hold(test_df: pd.DataFrame) -> pd.DataFrame:
    """단순 Buy & Hold: 첫 봉에 롱 진입, 마지막 봉에 청산."""
    close = test_df["close"].values
    log_rets = np.diff(np.log(close))    # (N-1,)

    records = []
    for i, lr in enumerate(log_rets):
        records.append(
            {
                "step": i,
                "price": close[i],
                "position": 1.0,
                "delta": 1.0 if i == 0 else 0.0,
                "log_ret": lr,
                "pnl_bps": lr * 10_000.0,
                "fee_bps": 5.0 if i == 0 else 0.0,  # 진입 수수료 1회
                "fixed_penalty_bps": 0.0,
                "whipsaw_penalty": 0.0,
                "reward": 0.0,
                "xgb_prob": 0.5,
                "ridge_pred": 0.0,
            }
        )
    # 청산 수수료
    if records:
        records[-1]["fee_bps"] += 5.0

    return pd.DataFrame(records)


# ──────────────────────────────────────────────────────────────────────────────
# XGBoost-only 기준선
# ──────────────────────────────────────────────────────────────────────────────


def baseline_xgb_only(
    test_df: pd.DataFrame,
    threshold: float = 0.5,
    fee_rate: float = 0.0005,
) -> pd.DataFrame:
    """
    XGBoost 단독 전략:
      prob > threshold  → +1 (롱)
      prob < (1 - threshold) → -1 (숏)
      else              →  0 (관망)
    매봉마다 시그널을 재계산하고 포지션 변화 시 수수료 부과.
    """
    probs = test_df["xgb_prob"].values
    close = test_df["close"].values

    records = []
    position = 0.0
    for i in range(len(probs) - 1):
        prob = float(probs[i])
        if prob > threshold:
            target = 1.0
        elif prob < (1.0 - threshold):
            target = -1.0
        else:
            target = 0.0

        delta = target - position
        fee_bps = abs(delta) * fee_rate * 10_000.0

        log_ret = np.log(close[i + 1] / close[i])
        pnl_bps = target * log_ret * 10_000.0

        records.append(
            {
                "step": i,
                "price": close[i],
                "position": target,
                "delta": delta,
                "log_ret": log_ret,
                "pnl_bps": pnl_bps,
                "fee_bps": fee_bps,
                "fixed_penalty_bps": 0.0,
                "whipsaw_penalty": 0.0,
                "reward": pnl_bps - fee_bps,
                "xgb_prob": prob,
                "ridge_pred": float(test_df["ridge_pred"].values[i]),
            }
        )
        position = target

    return pd.DataFrame(records)


# ══════════════════════════════════════════════════════════════════════════════
# 6. 인사이트 분석
# ══════════════════════════════════════════════════════════════════════════════


def analyze_betting_behavior(traj: pd.DataFrame) -> str:
    """에이전트가 어떤 오라클 조합에서 어떤 포지션을 선택하는지 분석."""
    lines = []

    # Discrete 액션 분포 요약
    if "action" in traj.columns:
        action_counts = traj["action"].value_counts().sort_index()
        total = len(traj)
        lines.append("### 에이전트 액션 분포 (Discrete)")
        lines.append("")
        lines.append("| 액션 | 포지션 | 횟수 | 비율 |")
        lines.append("|---|---|---|---|")
        action_labels = {0: "풀 숏 (-1.0)", 1: "관망 (0.0)", 2: "풀 롱 (+1.0)"}
        for a in [0, 1, 2]:
            cnt = int(action_counts.get(a, 0))
            lines.append(
                f"| {a} | {action_labels[a]} | {cnt:,} | {cnt/total*100:.1f}% |"
            )
        lines.append("")

    # 포지션 변경 횟수
    if "delta" in traj.columns:
        n_switches = int((traj["delta"].abs() > 1e-3).sum())
        lines.append(f"- **포지션 변경 횟수**: {n_switches:,}회")
        lines.append(
            f"- **포지션 변경률**: {n_switches / len(traj) * 100:.2f}% "
            f"({n_switches:,} / {len(traj):,} 스텝)"
        )
        lines.append("")

    # xgb_prob 분포 (포지션별)
    lines.append("### 오라클 신호별 포지션 선택 분석")
    lines.append("")

    traj["prob_bucket"] = pd.cut(traj["xgb_prob"], bins=[0, 0.4, 0.5, 0.6, 1.0],
                                  labels=["낮음(<0.4)", "중립(0.4-0.5)",
                                          "중립(0.5-0.6)", "높음(>0.6)"])
    traj["pred_bucket"] = pd.cut(traj["ridge_pred"],
                                  bins=[-np.inf, -0.001, 0.001, np.inf],
                                  labels=["음수(<-0.001)", "중립", "양수(>0.001)"])
    lines.append("**prob × pred 조합별 평균 포지션 & PnL**")
    lines.append("")
    lines.append("| XGB Prob 구간 | Ridge Pred 구간 | 평균 포지션 | 평균 PnL(bps) |")
    lines.append("|---|---|---|---|")
    pivot = (
        traj.groupby(["prob_bucket", "pred_bucket"], observed=True)
        .agg(pos=("position", "mean"), pnl=("pnl_bps", "mean"))
        .reset_index()
    )
    for _, row in pivot.iterrows():
        lines.append(
            f"| {row['prob_bucket']} | {row['pred_bucket']} "
            f"| {row['pos']:.3f} | {row['pnl']:.4f} |"
        )

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 7. 보고서 생성
# ══════════════════════════════════════════════════════════════════════════════


def generate_report(
    rl_metrics: dict,
    bh_metrics: dict,
    xgb_metrics: dict,
    rl_traj: pd.DataFrame,
    train_timesteps: int,
    env_kwargs: dict,
    test_date_range: tuple[str, str],
    elapsed_train: float,
) -> None:
    """artifacts/rl_training_report.md 자동 생성."""
    print("\n[7/7] 보고서 생성 중...")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W = env_kwargs["window_size"]
    FEE = env_kwargs["fee_rate"] * 100
    ACP = env_kwargs.get("action_change_penalty", 2.0)

    insight = analyze_betting_behavior(rl_traj.copy())

    # 포지션 변경 횟수 계산
    n_switches = int((rl_traj["delta"].abs() > 1e-3).sum())
    total_fixed_penalty = float(rl_traj.get("fixed_penalty_bps", pd.Series([0])).sum())

    lines = [
        "# Two-Stage RL Trading Agent — 학습 결과 보고서 (Discrete Action)",
        "",
        f"> 생성일시: {now}  |  총 학습 시간: {elapsed_train/60:.1f}분",
        f"> 테스트 구간: {test_date_range[0]} ~ {test_date_range[1]}",
        "",
        "---",
        "",
        "## 1. RL 환경 및 보상 함수 설계 요약",
        "",
        "### 1.1 오라클 (사전 계산)",
        "",
        "| 오라클 | 타입 | 타겟 변수 | 출력 | 학습 알고리즘 |",
        "|---|---|---|---|---|",
        "| 메인 오라클 | 분류기 | `future_ret_15 > 0` | 상승 확률 `xgb_prob ∈ [0,1]` | XGBoost Classifier |",
        "| 보조 오라클 | 회귀기 | `future_ret_15` (raw) | 예상 log-return `ridge_pred` | Ridge Regressor |",
        "",
        "**사전 계산 방식**: 전체 데이터프레임에 대해 오라클 추론 결과를 열로 추가한 뒤",
        "환경에 주입. RL 학습 루프 내에서 모델 추론을 수행하지 않아 속도 최적화.",
        "",
        "### 1.2 상태(State) 설계",
        "",
        "```",
        f"obs ∈ R^{{{W} × 69 + 2}} = R^{{{W * 69 + 2}}}",
        "",
        "  [ (scaled 67 features + xgb_prob + ridge_pred) × window_size  |  position  |  unrealized_pnl_bps ]",
        f"     ────────────────── {W * 69} dim ───────────────────────────────   ─── 2 dim ───",
        "```",
        "",
        f"- **window_size**: {W} (과거 {W}분 슬라이딩 윈도우)",
        "- **피처 스케일링**: RobustScaler (Train 구간 fit, 전체 transform)",
        "- **position**: 현재 보유 포지션 ∈ {-1.0, 0.0, +1.0}",
        "- **unrealized_pnl_bps**: 진입가 대비 미실현 손익 (단위: bps)",
        "",
        "### 1.3 액션(Action) 설계 — Discrete(3)",
        "",
        "```",
        "a_t ∈ Discrete(3)",
        "",
        "  Action 0 → 포지션 -1.0  (풀 숏, Short 100%)",
        "  Action 1 → 포지션  0.0  (관망 / 전액 청산)",
        "  Action 2 → 포지션 +1.0  (풀 롱, Long 100%)",
        "```",
        "",
        "- 이산형 공간 → 명확한 3가지 포지션만 허용, 미세 조정 불가",
        "- SB3 PPO의 MlpPolicy는 Discrete action space를 자동 감지하여",
        "  내부적으로 Categorical 분포를 사용 (별도 설정 불필요)",
        "- **Δpos** = target_position − prev_position ∈ {-2, -1, 0, +1, +2}",
        "",
        "### 1.4 보상(Reward) 함수",
        "",
        "$$",
        r"r_t = \underbrace{p_t \cdot \log\!\left(\frac{P_{t+1}}{P_t}\right) \times 10^4}_{\text{1-step PnL (bps)}}",
        r"    - \underbrace{|\Delta p_t| \cdot f \times 10^4}_{\text{거래 수수료}}",
        r"    - \underbrace{\mathbb{1}[\Delta p_t \neq 0] \cdot \alpha}_{\text{행동 변경 패널티 (고정)}}",
        "$$",
        "",
        "| 기호 | 의미 | 값 |",
        "|---|---|---|",
        f"| $p_t$ | 현재 포지션 | ∈ {{-1, 0, +1}} |",
        f"| $P_t$ | t봉 종가 | — |",
        f"| $f$ | 단방향 수수료율 | {FEE:.2f}% |",
        f"| $\\alpha$ | 행동 변경 고정 패널티 | {ACP} bps |",
        f"| $\\Delta p_t$ | 포지션 변화량 | — |",
        "",
        f"- 보상 단위: **bps (basis points, 1/100%)**",
        f"- 고정 행동 변경 패널티 `{ACP} bps`: 포지션이 실제로 바뀔 때만 1회 부과.",
        "  수수료와 별개로 추가되어 의미 없는 포지션 스위칭을 강력히 억제.",
        "- 연속형 이차 휩소 패널티(`whipsaw_coeff`)는 비활성화(0.0). ",
        "  이산형 환경에서는 고정 패널티가 동일한 역할을 더 명확하게 수행.",
        "",
        "---",
        "",
        "## 2. 테스트 구간 백테스트 성과",
        "",
        "### 2.1 RL 에이전트 (PPO) 성과",
        "",
        f"- **총 스텝**: {len(rl_traj):,}",
        f"- **누적 보상**: {rl_traj['reward'].sum():.2f} bps",
        f"- **누적 순수익**: {rl_traj['pnl_bps'].sum() - rl_traj['fee_bps'].sum():.2f} bps",
        f"- **총 수수료**: {rl_traj['fee_bps'].sum():.2f} bps",
        f"- **총 고정 패널티**: {total_fixed_penalty:.2f} bps",
        f"- **포지션 변경 횟수**: {n_switches:,}회",
        f"- **포지션 변경률**: {n_switches / len(rl_traj) * 100:.2f}%",
        "",
        "---",
        "",
        "## 3. 전략 비교 표",
        "",
        "| 지표 | Buy & Hold | XGBoost-only | **PPO RL (Discrete)** |",
        "|---|---|---|---|",
        f"| 누적 수익률 (%) | {bh_metrics['total_return_pct']:.4f}% "
        f"| {xgb_metrics['total_return_pct']:.4f}% "
        f"| **{rl_metrics['total_return_pct']:.4f}%** |",
        f"| MDD (%) | {bh_metrics['mdd_pct']:.4f}% "
        f"| {xgb_metrics['mdd_pct']:.4f}% "
        f"| **{rl_metrics['mdd_pct']:.4f}%** |",
        f"| Sharpe Ratio | {bh_metrics['sharpe']:.4f} "
        f"| {xgb_metrics['sharpe']:.4f} "
        f"| **{rl_metrics['sharpe']:.4f}** |",
        f"| 거래 횟수 | {bh_metrics['n_trades']:,} "
        f"| {xgb_metrics['n_trades']:,} "
        f"| **{rl_metrics['n_trades']:,}** |",
        f"| 승률 (%) | {bh_metrics['win_rate_pct']:.2f}% "
        f"| {xgb_metrics['win_rate_pct']:.2f}% "
        f"| **{rl_metrics['win_rate_pct']:.2f}%** |",
        "",
        "> ※ 수익률 단위: bps 기준 누적 합계 ÷ 100",
        "> ※ XGBoost-only: 매봉 재계산, threshold=0.5, 수수료 0.05%",
        "",
        "---",
        "",
        "## 4. 에이전트 베팅 행동 인사이트",
        "",
        insight,
        "",
        "---",
        "",
        "## 5. 학습 설정 요약",
        "",
        f"- **알고리즘**: PPO (Stable-Baselines3 v{_get_sb3_version()})",
        f"- **액션 공간**: Discrete(3) — 풀숏 / 관망 / 풀롱",
        f"- **총 학습 스텝**: {train_timesteps:,}",
        f"- **학습 소요 시간**: {elapsed_train/60:.1f}분",
        f"- **Policy 네트워크**: MlpPolicy (Categorical 분포, 64-64 기본)",
        f"- **데이터 분할**: Train 80% / Val 10% / Test 10%",
        f"- **오라클 저장 경로**: `artifacts/rl_prod/oracles/`",
        f"- **모델 저장 경로**: `artifacts/rl_prod/ppo_final.zip`",
        "",
        "```",
        f"학습 PPO 하이퍼파라미터:",
        f"  n_steps     = {PPO_KWARGS['n_steps']}",
        f"  batch_size  = {PPO_KWARGS['batch_size']}",
        f"  n_epochs    = {PPO_KWARGS['n_epochs']}",
        f"  gamma       = {PPO_KWARGS['gamma']}",
        f"  gae_lambda  = {PPO_KWARGS['gae_lambda']}",
        f"  clip_range  = {PPO_KWARGS['clip_range']}",
        f"  ent_coef    = {PPO_KWARGS['ent_coef']}  (Discrete 탐색 강화)",
        f"  lr          = {PPO_KWARGS['learning_rate']}",
        "```",
        "",
        "```",
        f"환경 파라미터:",
        f"  fee_rate             = {ENV_KWARGS['fee_rate']} (0.05% 단방향)",
        f"  action_change_penalty= {ENV_KWARGS['action_change_penalty']} bps (포지션 변경 시 고정 패널티)",
        f"  whipsaw_coeff        = {ENV_KWARGS['whipsaw_coeff']} (비활성화)",
        "```",
        "",
        "---",
        "*Generated by `scripts/rl/step_rl_1_train_ppo.py`*",
    ]

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"  보고서 저장 → {REPORT_PATH}")


def _get_sb3_version() -> str:
    try:
        import stable_baselines3
        return stable_baselines3.__version__
    except Exception:
        return "unknown"


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PPO RL 트레이딩 학습 파이프라인 (Discrete Action)")
    p.add_argument(
        "--timesteps",
        type=int,
        default=5_000_000,
        help="총 PPO 학습 스텝 수 (기본 5_000_000)",
    )
    p.add_argument(
        "--smoke",
        action="store_true",
        help="스모크 테스트 모드: 데이터 20K행 + 50K 스텝",
    )
    p.add_argument(
        "--skip_train",
        action="store_true",
        help="이미 학습된 모델 로드 후 평가만 실행",
    )
    p.add_argument(
        "--no_check",
        action="store_true",
        help="Gymnasium env 유효성 검사 생략",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    total_timesteps = 50_000 if args.smoke else args.timesteps
    max_rows = 20_000 if args.smoke else None

    t_start = time.time()
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: 데이터 로드 & 전처리 ────────────────────────────────────────
    df_scaled, feat_cols_raw, n_tr, n_vl = load_and_preprocess(
        DATASET_PATH, max_rows=max_rows
    )
    n_te_start = n_tr + n_vl

    # ── Step 2–3: 오라클 학습 & 사전 추론 ────────────────────────────────────
    xgb_clf, ridge = train_oracles(df_scaled, feat_cols_raw, n_tr)
    df_oracle = add_oracle_predictions(df_scaled, feat_cols_raw, xgb_clf, ridge)

    # ── 오라클 열 포함한 피처 목록 (RL obs용) ─────────────────────────────────
    oracle_feature_cols = feat_cols_raw + ["xgb_prob", "ridge_pred"]  # 69 cols

    # ── Step 4: 데이터 분할 ──────────────────────────────────────────────────
    df_train = df_oracle.iloc[:n_tr].copy()
    df_val = df_oracle.iloc[n_tr : n_te_start].copy()
    df_test = df_oracle.iloc[n_te_start:].copy()

    print(
        f"\n[3.5] 분할 완료: "
        f"Train {len(df_train):,} / Val {len(df_val):,} / Test {len(df_test):,} 행"
    )

    # 테스트 날짜 범위
    import pandas.api.types as pat
    _is_int_idx = pat.is_integer_dtype(df_test.index)
    test_date_start = "N/A" if _is_int_idx else str(df_test.index[0])[:10]
    test_date_end = "N/A" if _is_int_idx else str(df_test.index[-1])[:10]

    # ── Gymnasium 유효성 검사 ──────────────────────────────────────────────
    if not args.no_check and not args.skip_train:
        print("\n[3.6] Gymnasium 환경 유효성 검사...")
        _sample = df_train.iloc[:500].copy()
        _env = CryptoTradingEnv(
            df=_sample, feature_cols=oracle_feature_cols, **ENV_KWARGS
        )
        check_env(_env, warn=True)
        print("  환경 검사 통과 ✓")

    vecnorm_path = ARTIFACTS_DIR / "vecnorm.pkl"

    if args.skip_train:
        # ── 학습 건너뜀: 저장된 모델 로드 ──────────────────────────────────
        print("\n[4/7] 저장된 모델 로드 (--skip_train)")
        train_env = build_vec_env(
            df_train, oracle_feature_cols, ENV_KWARGS,
            norm_path=vecnorm_path, training=False,
        )
        model = PPO.load(str(ARTIFACTS_DIR / "ppo_final"), env=train_env)
        elapsed_train = 0.0
    else:
        # ── Step 4: PPO 학습 ─────────────────────────────────────────────────
        print("\n[4/7] 학습 환경 구성")
        train_env = build_vec_env(
            df_train, oracle_feature_cols, ENV_KWARGS, n_envs=1
        )

        t_train_start = time.time()
        model = train_ppo(
            train_env=train_env,
            val_df=df_val,
            feature_cols=oracle_feature_cols,
            env_kwargs=ENV_KWARGS,
            total_timesteps=total_timesteps,
            ppo_kwargs={**PPO_KWARGS, "verbose": 1},
        )
        elapsed_train = time.time() - t_train_start

    # ── Step 5: 에이전트 Test 시뮬레이션 ─────────────────────────────────────
    rl_traj = run_agent_on_test(
        model=model,
        test_df=df_test,
        feature_cols=oracle_feature_cols,
        env_kwargs=ENV_KWARGS,
        vecnorm_path=vecnorm_path,
    )

    # ── Step 6: 기준선 계산 ─────────────────────────────────────────────────
    print("\n[6/7] 기준선 전략 계산")
    bh_traj = baseline_buy_and_hold(df_test)
    xgb_traj = baseline_xgb_only(df_test, threshold=0.5, fee_rate=ENV_KWARGS["fee_rate"])

    rl_metrics = compute_metrics(rl_traj, "PPO RL (Discrete)")
    bh_metrics = compute_metrics(bh_traj, "Buy & Hold")
    xgb_metrics = compute_metrics(xgb_traj, "XGBoost-only")

    # 콘솔 요약 출력
    print("\n" + "═" * 60)
    print("  테스트 성과 요약")
    print("═" * 60)
    for m in [bh_metrics, xgb_metrics, rl_metrics]:
        print(
            f"  {m['label']:25s}  수익률={m['total_return_pct']:+.4f}%  "
            f"MDD={m['mdd_pct']:.4f}%  Sharpe={m['sharpe']:.3f}  "
            f"거래={m['n_trades']:,}  승률={m['win_rate_pct']:.1f}%"
        )
    print("═" * 60)

    # ── Step 7: 보고서 생성 ──────────────────────────────────────────────────
    generate_report(
        rl_metrics=rl_metrics,
        bh_metrics=bh_metrics,
        xgb_metrics=xgb_metrics,
        rl_traj=rl_traj,
        train_timesteps=total_timesteps,
        env_kwargs=ENV_KWARGS,
        test_date_range=(test_date_start, test_date_end),
        elapsed_train=elapsed_train,
    )

    # 궤적 저장
    traj_path = ARTIFACTS_DIR / "rl_test_trajectory.parquet"
    rl_traj.to_parquet(traj_path, index=False)
    print(f"\n  궤적 저장 → {traj_path}")

    total_elapsed = time.time() - t_start
    print(f"\n✓ 전체 파이프라인 완료: {total_elapsed/60:.1f}분")
    print(f"  보고서: {REPORT_PATH}")


if __name__ == "__main__":
    main()
