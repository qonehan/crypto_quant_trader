#!/usr/bin/env python
"""
scripts/compare_models_arena.py — Model Arena v1.0

ML/DL 전 모델 통합 비교 아레나.
단 한 번의 실행으로 8개 모델을 순차 학습·평가하고 리더보드를 출력합니다.

모델 라인업 (8종)
──────────────────────────────────────────────────────────────────────────────
  ML 분류기 : LightGBM Classifier, XGBoost Classifier
  ML 회귀기 : Ridge Regressor,    LightGBM Regressor
  DL 분류기 : CryptoMamba (BCE),  TCN (BCE),  LSTM (BCE)
  DL 회귀기 : CryptoMamba (GMADLoss)

데이터 규칙
──────────────────────────────────────────────────────────────────────────────
  소스  : data/datasets/btc_1m_hft_v2.parquet  (67 피처, future_ret_15)
  분할  : Train 80% / Val 10% / Test 10%  ← 시간 순서, 절대 셔플 금지
  스케일 : DL/Ridge → RobustScaler(Train fit), ML 트리 → 원본 피처 그대로
  타겟  : 회귀 = future_ret_15 raw float  |  분류 = (future_ret_15 > 0).astype(int)

평가 지표 (공통 + 분류/회귀 전용)
──────────────────────────────────────────────────────────────────────────────
  공통 : 방향정확도(전체), 방향정확도(|ret|≥0.01% 필터), 추론 레이턴시 (ms/batch)
  분류 : F1, Precision, Recall, ROC-AUC
  회귀 : MSE, RMSE, |pred| 상위 10% 구간 방향 승률

실행 방법
──────────────────────────────────────────────────────────────────────────────
  # 전체 실행 (기본 30 에폭, patience=5)
  poetry run python scripts/compare_models_arena.py

  # 스모크 테스트 (15K 행, 5 에폭)
  poetry run python scripts/compare_models_arena.py --smoke_test

  # ML 모델만 빠르게 비교
  poetry run python scripts/compare_models_arena.py --no_dl

  # DL 에폭 수 조정
  poetry run python scripts/compare_models_arena.py --epochs 50 --patience 8
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

# LightGBM/XGBoost 가 numpy 배열로 predict 시 발생하는 sklearn 호환 경고 억제
warnings.filterwarnings(
    "ignore",
    message="X does not have valid feature names",
    category=UserWarning,
)

import joblib
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.linear_model import Ridge as SklearnRidge
from sklearn.metrics import (
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.preprocessing import RobustScaler
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader, Dataset

# ── 프로젝트 루트 경로 주입 ─────────────────────────────────────────────────
_PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJ))

# ── 옵셔널 의존성 ──────────────────────────────────────────────────────────
try:
    import lightgbm as lgb

    HAS_LGBM = True
except ImportError:
    HAS_LGBM = False
    print("[경고] lightgbm 미설치 — LightGBM 모델 건너뜀 (pip install lightgbm)")

try:
    import xgboost as xgb

    HAS_XGB = True
except ImportError:
    HAS_XGB = False
    print("[경고] xgboost 미설치 — XGBoost 모델 건너뜀 (pip install xgboost)")

# ── 내부 모듈 ──────────────────────────────────────────────────────────────
from app.predictor.dl_model import (  # noqa: E402
    CryptoMambaClassifier,
    LSTMClassifier,
    TCNClassifier,
)
from app.predictor.losses import GMADLoss  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════════
# 상수
# ══════════════════════════════════════════════════════════════════════════════

DATASET_PATH = _PROJ / "data" / "datasets" / "btc_1m_hft_v2.parquet"
ARENA_DIR = _PROJ / "artifacts"
ARENA_DIR.mkdir(parents=True, exist_ok=True)

TARGET_COL = "future_ret_15"
EXCLUDE_COLS: set[str] = {
    "target",
    "future_ret",
    "target_1m",
    "future_ret_1",
    "future_ret_5",
    "future_ret_15",
    "future_ret_60",
}

SEQ_LEN = 60        # 슬라이딩 윈도우 길이 (1분봉 × 60 = 1시간)
TRAIN_RATIO = 0.80
VAL_RATIO = 0.10
# TEST_RATIO = 0.10

MIN_MAG = 1e-4      # 방향정확도 필터 임계값 (0.01% = 1bp)
LATENCY_REPS = 20   # 레이턴시 측정 반복 횟수
LATENCY_BATCH = 512 # 레이턴시 측정 배치 크기


# ══════════════════════════════════════════════════════════════════════════════
# 1. 데이터 로딩
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DataBundle:
    """분할·전처리 완료 데이터 번들 (ML flat + DL window 겸용)."""

    # ── ML용 (flat, unscaled for tree / scaled for Ridge·DL) ──────────────
    X_tr_raw: np.ndarray   # (N_tr, F) — tree 모델용 원본 피처
    X_vl_raw: np.ndarray
    X_te_raw: np.ndarray
    X_tr: np.ndarray       # (N_tr, F) — RobustScaled (Ridge / DL 입력)
    X_vl: np.ndarray
    X_te: np.ndarray

    # ── 타겟: 회귀 (raw continuous) ────────────────────────────────────────
    y_tr_reg: np.ndarray   # future_ret_15 원본값
    y_vl_reg: np.ndarray
    y_te_reg: np.ndarray

    # ── 타겟: 분류 (binary 0/1) ────────────────────────────────────────────
    y_tr_cls: np.ndarray   # (future_ret_15 > 0).astype(float32)
    y_vl_cls: np.ndarray
    y_te_cls: np.ndarray

    # ── 메타 ───────────────────────────────────────────────────────────────
    n_features: int
    feature_cols: list[str]
    tau: float             # std(y_tr_reg[SEQ_LEN:]) — GMADLoss 커널 스케일
    scaler: RobustScaler


def load_data(
    parquet_path: Path = DATASET_PATH,
    max_rows: int | None = None,
) -> DataBundle:
    """btc_1m_hft_v2.parquet 로드 → 분할·전처리 → DataBundle 반환."""
    print(f"\n[Data] 로드 중: {parquet_path.name}")
    df = pd.read_parquet(parquet_path)
    df = df.dropna(subset=[TARGET_COL])

    feat_cols: list[str] = [c for c in df.columns if c not in EXCLUDE_COLS]

    if max_rows:
        df = df.iloc[:max_rows].copy()
        print(f"  [Debug] max_rows={max_rows} 적용")

    # ── 시간순 분할 (누수 방지 — 분할 먼저, 처리 나중) ──────────────────
    n = len(df)
    n_tr = int(n * TRAIN_RATIO)
    n_vl = int(n * VAL_RATIO)

    tr = df.iloc[:n_tr].copy()
    vl = df.iloc[n_tr : n_tr + n_vl].copy()
    te = df.iloc[n_tr + n_vl :].copy()

    print(
        f"  분할: Train {len(tr):,} / Val {len(vl):,} / Test {len(te):,} 행  "
        f"(피처 {len(feat_cols)}개)"
    )

    # ── 결측치 처리: ffill → train median (bfill 사용 금지 — 미래 누수) ──
    for split in [tr, vl, te]:
        split[feat_cols] = split[feat_cols].ffill()
    train_median = tr[feat_cols].median()
    for split in [tr, vl, te]:
        split[feat_cols] = split[feat_cols].fillna(train_median)

    # ── 타겟 추출 ──────────────────────────────────────────────────────────
    y_tr = tr[TARGET_COL].values.astype(np.float32)
    y_vl = vl[TARGET_COL].values.astype(np.float32)
    y_te = te[TARGET_COL].values.astype(np.float32)

    # ── 스케일러 (Train only fit) ─────────────────────────────────────────
    X_tr_raw = tr[feat_cols].values.astype(np.float32)
    X_vl_raw = vl[feat_cols].values.astype(np.float32)
    X_te_raw = te[feat_cols].values.astype(np.float32)

    scaler = RobustScaler()
    scaler.fit(X_tr_raw)
    X_tr = scaler.transform(X_tr_raw).astype(np.float32)
    X_vl = scaler.transform(X_vl_raw).astype(np.float32)
    X_te = scaler.transform(X_te_raw).astype(np.float32)

    # tau: DL DataLoader 기준 (SEQ_LEN 이후 샘플) train std
    tau = float(np.std(y_tr[SEQ_LEN:]))
    print(f"  tau (GMADLoss 커널): {tau:.6f}")

    return DataBundle(
        X_tr_raw=X_tr_raw,
        X_vl_raw=X_vl_raw,
        X_te_raw=X_te_raw,
        X_tr=X_tr,
        X_vl=X_vl,
        X_te=X_te,
        y_tr_reg=y_tr,
        y_vl_reg=y_vl,
        y_te_reg=y_te,
        y_tr_cls=(y_tr > 0).astype(np.float32),
        y_vl_cls=(y_vl > 0).astype(np.float32),
        y_te_cls=(y_te > 0).astype(np.float32),
        n_features=len(feat_cols),
        feature_cols=feat_cols,
        tau=tau,
        scaler=scaler,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. 결과 레코드
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ModelResult:
    name: str
    task: str  # "cls" | "reg"

    # ── 공통 ───────────────────────────────────────────────────────────────
    dir_acc_all: float = float("nan")       # 전체 방향정확도
    dir_acc_filtered: float = float("nan")  # |ret|≥0.01% 필터 방향정확도
    latency_ms: float = float("nan")        # 추론 레이턴시 (ms/batch)

    # ── 분류 전용 ──────────────────────────────────────────────────────────
    f1: float = float("nan")
    precision: float = float("nan")
    recall: float = float("nan")
    roc_auc: float = float("nan")

    # ── 회귀 전용 ──────────────────────────────────────────────────────────
    mse: float = float("nan")
    rmse: float = float("nan")
    top10_dir_acc: float = float("nan")     # |pred| 상위 10% 구간 방향 승률

    # ── 메타 ───────────────────────────────────────────────────────────────
    train_time_sec: float = float("nan")
    n_params: int = 0
    notes: str = ""


# ══════════════════════════════════════════════════════════════════════════════
# 3. 공통 평가 유틸리티
# ══════════════════════════════════════════════════════════════════════════════

def _dir_accuracy(
    pred_raw: np.ndarray,
    true_ret: np.ndarray,
    task: str,
    min_mag: float = 0.0,
) -> float:
    """방향 정확도 (부호 일치율).

    Args:
        pred_raw : task="cls" → sigmoid prob [0,1] | task="reg" → log-return
        true_ret : 실제 future_ret_15 연속값 (공통 기준)
        min_mag  : |true_ret| ≥ min_mag 인 샘플만 평가
    """
    mask = np.abs(true_ret) >= min_mag
    if mask.sum() == 0:
        return float("nan")

    true_dir = (true_ret[mask] > 0).astype(int)

    if task == "cls":
        pred_dir = (pred_raw[mask] > 0.5).astype(int)
    else:
        pred_dir = (pred_raw[mask] > 0).astype(int)

    return float((pred_dir == true_dir).mean())


def _eval_clf(
    pred_prob: np.ndarray,
    true_ret: np.ndarray,
    name: str,
    train_time: float,
    latency_ms: float,
    n_params: int = 0,
) -> ModelResult:
    """분류 모델 공통 평가."""
    true_dir = (true_ret > 0).astype(int)
    pred_dir = (pred_prob > 0.5).astype(int)

    mask = np.abs(true_ret) >= MIN_MAG

    r = ModelResult(name=name, task="cls")
    r.train_time_sec = train_time
    r.latency_ms = latency_ms
    r.n_params = n_params

    r.dir_acc_all = float((pred_dir == true_dir).mean())
    r.dir_acc_filtered = (
        float((pred_dir[mask] == true_dir[mask]).mean()) if mask.sum() > 0 else float("nan")
    )
    r.f1 = f1_score(true_dir, pred_dir, zero_division=0)
    r.precision = precision_score(true_dir, pred_dir, zero_division=0)
    r.recall = recall_score(true_dir, pred_dir, zero_division=0)
    try:
        r.roc_auc = roc_auc_score(true_dir, pred_prob)
    except Exception:
        r.roc_auc = float("nan")
    return r


def _eval_reg(
    pred_ret: np.ndarray,
    true_ret: np.ndarray,
    name: str,
    train_time: float,
    latency_ms: float,
    n_params: int = 0,
) -> ModelResult:
    """회귀 모델 공통 평가."""
    true_dir = (true_ret > 0).astype(int)
    pred_dir = (pred_ret > 0).astype(int)
    mask = np.abs(true_ret) >= MIN_MAG

    # 상위 10% 고확신 구간 (|pred| 기준)
    thr10 = np.percentile(np.abs(pred_ret), 90)
    top10_mask = np.abs(pred_ret) >= thr10

    r = ModelResult(name=name, task="reg")
    r.train_time_sec = train_time
    r.latency_ms = latency_ms
    r.n_params = n_params

    r.dir_acc_all = float((pred_dir == true_dir).mean())
    r.dir_acc_filtered = (
        float((pred_dir[mask] == true_dir[mask]).mean()) if mask.sum() > 0 else float("nan")
    )
    r.mse = float(np.mean((pred_ret - true_ret) ** 2))
    r.rmse = float(r.mse**0.5)
    r.top10_dir_acc = (
        float((pred_dir[top10_mask] == true_dir[top10_mask]).mean())
        if top10_mask.sum() > 0
        else float("nan")
    )
    return r


def _latency_ml(predict_fn, X_sample: np.ndarray) -> float:
    """ML 모델 추론 레이턴시 측정 (ms/배치)."""
    n = min(LATENCY_BATCH, len(X_sample))
    X = X_sample[:n]
    _ = predict_fn(X)  # warm-up
    times = []
    for _ in range(LATENCY_REPS):
        t = time.perf_counter()
        predict_fn(X)
        times.append(time.perf_counter() - t)
    return float(np.median(times)) * 1000.0


def _latency_dl(
    model: nn.Module,
    n_features: int,
    device: torch.device,
    seq_len: int = SEQ_LEN,
    batch_size: int = LATENCY_BATCH,
) -> float:
    """DL 모델 추론 레이턴시 측정 (ms/배치)."""
    model.eval()
    dummy = torch.randn(batch_size, seq_len, n_features, device=device)

    with torch.no_grad():
        for _ in range(3):  # warm-up
            model(dummy)
    if device.type == "cuda":
        torch.cuda.synchronize()

    times = []
    with torch.no_grad():
        for _ in range(LATENCY_REPS):
            if device.type == "cuda":
                torch.cuda.synchronize()
            t = time.perf_counter()
            model(dummy)
            if device.type == "cuda":
                torch.cuda.synchronize()
            times.append(time.perf_counter() - t)

    return float(np.median(times)) * 1000.0


def _cleanup(*objs) -> None:
    """메모리 해제 — OOM 방지."""
    for obj in objs:
        del obj
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


# ══════════════════════════════════════════════════════════════════════════════
# 4. ML 모델: LightGBM Classifier
# ══════════════════════════════════════════════════════════════════════════════

def run_lgbm_clf(bundle: DataBundle, n_estimators: int = 300) -> ModelResult:
    tag = "[1/8] LightGBM Classifier"
    if not HAS_LGBM:
        r = ModelResult(name="LightGBM_Clf", task="cls", notes="lightgbm 미설치")
        print(f"\n  {tag} — 건너뜀 (lightgbm 미설치)")
        return r

    _section(tag)
    t0 = time.time()

    model = lgb.LGBMClassifier(
        n_estimators=n_estimators,
        learning_rate=0.05,
        num_leaves=63,
        min_child_samples=20,
        subsample=0.8,
        colsample_bytree=0.8,
        n_jobs=-1,
        random_state=42,
        verbose=-1,
    )
    model.fit(
        bundle.X_tr_raw,
        bundle.y_tr_cls.astype(int),
        eval_set=[(bundle.X_vl_raw, bundle.y_vl_cls.astype(int))],
        callbacks=[
            lgb.early_stopping(stopping_rounds=30, verbose=False),
            lgb.log_evaluation(period=-1),
        ],
    )
    train_time = time.time() - t0
    print(f"  완료 ({train_time:.1f}s, best_iter={model.best_iteration_})")

    pred_prob = model.predict_proba(bundle.X_te_raw)[:, 1]
    latency = _latency_ml(lambda x: model.predict_proba(x), bundle.X_te_raw)
    result = _eval_clf(pred_prob, bundle.y_te_reg, "LightGBM_Clf", train_time, latency)

    _cleanup(model)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 5. ML 모델: XGBoost Classifier
# ══════════════════════════════════════════════════════════════════════════════

def run_xgb_clf(bundle: DataBundle, n_estimators: int = 300) -> ModelResult:
    tag = "[2/8] XGBoost Classifier"
    if not HAS_XGB:
        r = ModelResult(name="XGBoost_Clf", task="cls", notes="xgboost 미설치")
        print(f"\n  {tag} — 건너뜀 (xgboost 미설치)")
        return r

    _section(tag)
    t0 = time.time()

    model = xgb.XGBClassifier(
        n_estimators=n_estimators,
        learning_rate=0.05,
        max_depth=6,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        early_stopping_rounds=30,
        random_state=42,
        verbosity=0,
        n_jobs=-1,
    )
    model.fit(
        bundle.X_tr_raw,
        bundle.y_tr_cls.astype(int),
        eval_set=[(bundle.X_vl_raw, bundle.y_vl_cls.astype(int))],
        verbose=False,
    )
    train_time = time.time() - t0
    print(f"  완료 ({train_time:.1f}s)")

    pred_prob = model.predict_proba(bundle.X_te_raw)[:, 1]
    latency = _latency_ml(lambda x: model.predict_proba(x), bundle.X_te_raw)
    result = _eval_clf(pred_prob, bundle.y_te_reg, "XGBoost_Clf", train_time, latency)

    _cleanup(model)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 6. ML 모델: Ridge Regressor
# ══════════════════════════════════════════════════════════════════════════════

def run_ridge_reg(bundle: DataBundle) -> ModelResult:
    _section("[3/8] Ridge Regressor")
    t0 = time.time()

    model = SklearnRidge(alpha=1.0)
    model.fit(bundle.X_tr, bundle.y_tr_reg)
    train_time = time.time() - t0
    print(f"  완료 ({train_time:.1f}s)")

    pred_ret = model.predict(bundle.X_te).astype(np.float32)
    latency = _latency_ml(lambda x: model.predict(x), bundle.X_te)
    result = _eval_reg(pred_ret, bundle.y_te_reg, "Ridge_Reg", train_time, latency)

    _cleanup(model)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 7. ML 모델: LightGBM Regressor
# ══════════════════════════════════════════════════════════════════════════════

def run_lgbm_reg(bundle: DataBundle, n_estimators: int = 300) -> ModelResult:
    tag = "[4/8] LightGBM Regressor"
    if not HAS_LGBM:
        r = ModelResult(name="LightGBM_Reg", task="reg", notes="lightgbm 미설치")
        print(f"\n  {tag} — 건너뜀")
        return r

    _section(tag)
    t0 = time.time()

    model = lgb.LGBMRegressor(
        n_estimators=n_estimators,
        learning_rate=0.05,
        num_leaves=63,
        min_child_samples=20,
        subsample=0.8,
        colsample_bytree=0.8,
        n_jobs=-1,
        random_state=42,
        verbose=-1,
    )
    model.fit(
        bundle.X_tr_raw,
        bundle.y_tr_reg,
        eval_set=[(bundle.X_vl_raw, bundle.y_vl_reg)],
        callbacks=[
            lgb.early_stopping(stopping_rounds=30, verbose=False),
            lgb.log_evaluation(period=-1),
        ],
    )
    train_time = time.time() - t0
    print(f"  완료 ({train_time:.1f}s, best_iter={model.best_iteration_})")

    pred_ret = model.predict(bundle.X_te_raw).astype(np.float32)
    latency = _latency_ml(lambda x: model.predict(x), bundle.X_te_raw)
    result = _eval_reg(pred_ret, bundle.y_te_reg, "LightGBM_Reg", train_time, latency)

    _cleanup(model)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 8. DL 공통 — Dataset / DataLoader / 학습 루프
# ══════════════════════════════════════════════════════════════════════════════

class _ArenaDataset(Dataset):
    """슬라이딩 윈도우 시계열 Dataset.

    dataset[i] → (X[i : i+seq_len], y[i+seq_len])

    stride 파라미터로 학습 속도를 조절합니다.
    (Val/Test는 stride=1 고정)
    """

    def __init__(
        self,
        X: np.ndarray,
        y: np.ndarray,
        seq_len: int = SEQ_LEN,
        stride: int = 1,
    ) -> None:
        self.X = torch.tensor(X, dtype=torch.float32)
        self.y = torch.tensor(y, dtype=torch.float32)
        self.seq_len = seq_len
        self._idx = list(range(0, len(X) - seq_len, max(1, stride)))

    def __len__(self) -> int:
        return len(self._idx)

    def __getitem__(self, i: int) -> tuple[torch.Tensor, torch.Tensor]:
        s = self._idx[i]
        return self.X[s : s + self.seq_len], self.y[s + self.seq_len]


def _make_loaders(
    bundle: DataBundle,
    task: str,
    batch_size: int = 512,
    train_stride: int = 2,
) -> tuple[DataLoader, DataLoader, DataLoader]:
    """(train / val / test) DataLoader 생성.

    task="cls" → y = future_ret_15 > 0  (binary)
    task="reg" → y = future_ret_15 raw float
    """
    y_tr = bundle.y_tr_cls if task == "cls" else bundle.y_tr_reg
    y_vl = bundle.y_vl_cls if task == "cls" else bundle.y_vl_reg
    y_te = bundle.y_te_cls if task == "cls" else bundle.y_te_reg

    tr_ds = _ArenaDataset(bundle.X_tr, y_tr, stride=train_stride)
    vl_ds = _ArenaDataset(bundle.X_vl, y_vl, stride=1)
    te_ds = _ArenaDataset(bundle.X_te, y_te, stride=1)

    kw = {"pin_memory": torch.cuda.is_available(), "num_workers": 0}
    return (
        DataLoader(tr_ds, batch_size, shuffle=True,  **kw),
        DataLoader(vl_ds, batch_size, shuffle=False, **kw),
        DataLoader(te_ds, batch_size, shuffle=False, **kw),
    )


class _EarlyStopping:
    """Val loss 기반 EarlyStopping + best state 자동 보존."""

    def __init__(self, patience: int = 5) -> None:
        self.patience = patience
        self.best = float("inf")
        self.counter = 0
        self.best_epoch = 0
        self.best_state: dict | None = None

    def __call__(self, val_loss: float, epoch: int, model: nn.Module) -> bool:
        if val_loss < self.best - 1e-6:
            self.best = val_loss
            self.counter = 0
            self.best_epoch = epoch
            self.best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            self.counter += 1
        return self.counter >= self.patience


def _train_dl(
    model: nn.Module,
    train_loader: DataLoader,
    val_loader: DataLoader,
    device: torch.device,
    task: str,
    tau: float,
    max_epochs: int = 30,
    patience: int = 5,
    lr: float = 3e-4,
    gmad_gamma: float = 100.0,
    gmad_alpha: float = 0.70,
) -> float:
    """공통 DL 학습 루프.

    Returns:
        train_time_sec (float)
    """
    # ── 손실 함수 설정 ──────────────────────────────────────────────────
    if task == "cls":
        # 동적 pos_weight (클래스 불균형 보정)
        all_y = torch.cat([y for _, y in train_loader])
        n_pos = float(all_y.sum().item())
        n_neg = float(len(all_y) - n_pos)
        pw = torch.tensor([n_neg / max(n_pos, 1)], device=device)
        criterion: nn.Module = nn.BCEWithLogitsLoss(pos_weight=pw)
    else:
        criterion = GMADLoss(tau=tau, gamma=gmad_gamma, alpha=gmad_alpha).to(device)

    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = CosineAnnealingLR(optimizer, T_max=max_epochs, eta_min=lr * 0.01)
    es = _EarlyStopping(patience=patience)

    t0 = time.time()
    for epoch in range(1, max_epochs + 1):
        # ── Train ───────────────────────────────────────────────────────
        model.train()
        ep_loss, n_b = 0.0, 0
        for x, y in train_loader:
            x = x.to(device, non_blocking=True)
            y = y.to(device, non_blocking=True)
            optimizer.zero_grad()
            pred = model(x).squeeze(-1)
            loss = criterion(pred, y)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            ep_loss += loss.item()
            n_b += 1

        # ── Validation ──────────────────────────────────────────────────
        model.eval()
        val_loss, n_vb = 0.0, 0
        with torch.no_grad():
            for x, y in val_loader:
                x = x.to(device, non_blocking=True)
                y = y.to(device, non_blocking=True)
                pred = model(x).squeeze(-1)
                val_loss += criterion(pred, y).item()
                n_vb += 1
        val_loss /= max(n_vb, 1)
        scheduler.step()

        if epoch % 5 == 0 or epoch == 1:
            print(
                f"    Epoch {epoch:3d}/{max_epochs}  "
                f"train={ep_loss/n_b:.5f}  val={val_loss:.5f}"
            )

        if es(val_loss, epoch, model):
            print(
                f"    EarlyStop → best epoch={es.best_epoch}  "
                f"val_loss={es.best:.5f}"
            )
            break

    # best 가중치 복원
    if es.best_state is not None:
        model.load_state_dict(es.best_state)

    return time.time() - t0


@torch.no_grad()
def _dl_predict(
    model: nn.Module,
    test_loader: DataLoader,
    device: torch.device,
) -> np.ndarray:
    """DL 모델 테스트셋 추론 → raw output (B,) numpy array."""
    model.eval()
    preds = []
    for x, _ in test_loader:
        x = x.to(device, non_blocking=True)
        preds.append(model(x).squeeze(-1).cpu().numpy())
    return np.concatenate(preds)


def _true_ret_aligned(bundle: DataBundle, n_preds: int) -> np.ndarray:
    """DL 슬라이딩 윈도우와 정렬된 실제 future_ret_15 배열.

    dataset[i].y = y_te[i + SEQ_LEN]
    → preds[0..N] 에 대응하는 실제 수익률 = y_te_reg[SEQ_LEN : SEQ_LEN+N]
    """
    return bundle.y_te_reg[SEQ_LEN : SEQ_LEN + n_preds]


# ══════════════════════════════════════════════════════════════════════════════
# 9. DL 모델: CryptoMamba (BCE)
# ══════════════════════════════════════════════════════════════════════════════

def run_cryptomamba_bce(
    bundle: DataBundle,
    device: torch.device,
    max_epochs: int,
    patience: int,
    batch_size: int = 512,
) -> ModelResult:
    _section("[5/8] CryptoMamba (BCEWithLogitsLoss)")

    train_loader, val_loader, test_loader = _make_loaders(bundle, "cls", batch_size)

    model = CryptoMambaClassifier(
        n_features=bundle.n_features,
        d_model=64,
        n_low=2,
        n_high=1,
        d_conv=4,
        dropout=0.1,
    ).to(device)
    n_params = model.count_params()
    print(f"  파라미터: {n_params:,}")

    train_time = _train_dl(
        model, train_loader, val_loader, device,
        task="cls", tau=bundle.tau,
        max_epochs=max_epochs, patience=patience,
    )
    print(f"  학습 완료 ({train_time:.1f}s)")

    preds_logit = _dl_predict(model, test_loader, device)
    preds_prob = 1.0 / (1.0 + np.exp(-preds_logit))   # sigmoid
    true_ret = _true_ret_aligned(bundle, len(preds_logit))

    latency = _latency_dl(model, bundle.n_features, device)
    result = _eval_clf(preds_prob, true_ret, "CryptoMamba_BCE", train_time, latency, n_params)

    _cleanup(model, train_loader, val_loader, test_loader)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 10. DL 모델: TCN (BCE)
# ══════════════════════════════════════════════════════════════════════════════

def run_tcn_bce(
    bundle: DataBundle,
    device: torch.device,
    max_epochs: int,
    patience: int,
    batch_size: int = 512,
) -> ModelResult:
    _section("[6/8] TCN (BCEWithLogitsLoss)")

    train_loader, val_loader, test_loader = _make_loaders(bundle, "cls", batch_size)

    model = TCNClassifier(
        n_features=bundle.n_features,
        n_channels=64,
        kernel_size=3,
        dropout=0.2,
    ).to(device)
    n_params = model.count_params()
    print(f"  파라미터: {n_params:,}")

    train_time = _train_dl(
        model, train_loader, val_loader, device,
        task="cls", tau=bundle.tau,
        max_epochs=max_epochs, patience=patience,
    )
    print(f"  학습 완료 ({train_time:.1f}s)")

    preds_logit = _dl_predict(model, test_loader, device)
    preds_prob = 1.0 / (1.0 + np.exp(-preds_logit))
    true_ret = _true_ret_aligned(bundle, len(preds_logit))

    latency = _latency_dl(model, bundle.n_features, device)
    result = _eval_clf(preds_prob, true_ret, "TCN_BCE", train_time, latency, n_params)

    _cleanup(model, train_loader, val_loader, test_loader)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 11. DL 모델: LSTM (BCE)
# ══════════════════════════════════════════════════════════════════════════════

def run_lstm_bce(
    bundle: DataBundle,
    device: torch.device,
    max_epochs: int,
    patience: int,
    batch_size: int = 512,
) -> ModelResult:
    _section("[7/8] LSTM (BCEWithLogitsLoss)")

    train_loader, val_loader, test_loader = _make_loaders(bundle, "cls", batch_size)

    model = LSTMClassifier(
        n_features=bundle.n_features,
        hidden_size=64,
        num_layers=2,
        dropout=0.2,
    ).to(device)
    n_params = model.count_params()
    print(f"  파라미터: {n_params:,}")

    train_time = _train_dl(
        model, train_loader, val_loader, device,
        task="cls", tau=bundle.tau,
        max_epochs=max_epochs, patience=patience,
    )
    print(f"  학습 완료 ({train_time:.1f}s)")

    preds_logit = _dl_predict(model, test_loader, device)
    preds_prob = 1.0 / (1.0 + np.exp(-preds_logit))
    true_ret = _true_ret_aligned(bundle, len(preds_logit))

    latency = _latency_dl(model, bundle.n_features, device)
    result = _eval_clf(preds_prob, true_ret, "LSTM_BCE", train_time, latency, n_params)

    _cleanup(model, train_loader, val_loader, test_loader)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 12. DL 모델: CryptoMamba (GMADLoss)
# ══════════════════════════════════════════════════════════════════════════════

def run_cryptomamba_gmad(
    bundle: DataBundle,
    device: torch.device,
    max_epochs: int,
    patience: int,
    batch_size: int = 512,
) -> ModelResult:
    _section("[8/8] CryptoMamba (GMADLoss / Regression)")

    train_loader, val_loader, test_loader = _make_loaders(bundle, "reg", batch_size)

    model = CryptoMambaClassifier(
        n_features=bundle.n_features,
        d_model=64,
        n_low=2,
        n_high=1,
        d_conv=4,
        dropout=0.1,
    ).to(device)
    n_params = model.count_params()
    print(f"  파라미터: {n_params:,}")

    train_time = _train_dl(
        model, train_loader, val_loader, device,
        task="reg", tau=bundle.tau,
        max_epochs=max_epochs, patience=patience,
    )
    print(f"  학습 완료 ({train_time:.1f}s)")

    preds_ret = _dl_predict(model, test_loader, device)
    true_ret = _true_ret_aligned(bundle, len(preds_ret))

    latency = _latency_dl(model, bundle.n_features, device)
    result = _eval_reg(preds_ret, true_ret, "CryptoMamba_GMAD", train_time, latency, n_params)

    _cleanup(model, train_loader, val_loader, test_loader)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 13. 리더보드 구성 & 저장
# ══════════════════════════════════════════════════════════════════════════════

def _fmt(v: float, pct: bool = False, decimals: int = 4) -> str:
    if np.isnan(v):
        return "—"
    if pct:
        return f"{v * 100:.2f}%"
    return f"{v:.{decimals}f}"


def build_leaderboard(results: list[ModelResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        row = {
            "모델": r.name,
            "태스크": r.task,
            "방향정확도(전체)": _fmt(r.dir_acc_all, pct=True),
            "방향정확도(필터↑)": _fmt(r.dir_acc_filtered, pct=True),
            "레이턴시(ms)": _fmt(r.latency_ms, decimals=2),
            # 분류 전용
            "F1": _fmt(r.f1),
            "Precision": _fmt(r.precision),
            "Recall": _fmt(r.recall),
            "AUC": _fmt(r.roc_auc),
            # 회귀 전용
            "RMSE": _fmt(r.rmse, decimals=7),
            "Top10%_DirAcc": _fmt(r.top10_dir_acc, pct=True),
            # 메타
            "학습시간(s)": _fmt(r.train_time_sec, decimals=1),
            "파라미터수": f"{r.n_params:,}" if r.n_params > 0 else "—",
        }
        rows.append(row)

    df = pd.DataFrame(rows)

    # dir_acc_filtered 숫자값으로 정렬 후 순위 부여
    def _parse_pct(s: str) -> float:
        try:
            return float(s.replace("%", ""))
        except Exception:
            return -1.0

    df["_sort"] = df["방향정확도(필터↑)"].apply(_parse_pct)
    df = df.sort_values("_sort", ascending=False).drop(columns=["_sort"]).reset_index(drop=True)
    df.index = df.index + 1
    df.index.name = "Rank"
    return df


def _md_table(df: pd.DataFrame) -> str:
    """DataFrame → Markdown 테이블 문자열."""
    try:
        return df.to_markdown()  # tabulate 있을 때
    except Exception:
        # tabulate 없을 때 수동 생성
        cols = df.columns.tolist()
        lines = ["| Rank | " + " | ".join(cols) + " |"]
        lines.append("|---" * (len(cols) + 1) + "|")
        for idx, row in df.iterrows():
            lines.append(f"| {idx} | " + " | ".join(str(v) for v in row) + " |")
        return "\n".join(lines)


def save_leaderboard(df: pd.DataFrame) -> None:
    """CSV + Markdown 파일 저장."""
    csv_path = ARENA_DIR / "arena_leaderboard.csv"
    md_path = ARENA_DIR / "arena_leaderboard.md"

    df.to_csv(csv_path, encoding="utf-8-sig")
    print(f"\n  [저장] {csv_path}")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Model Arena — Leaderboard\n\n")
        f.write(f"> 생성: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}  \n")
        f.write(f"> 데이터: `btc_1m_hft_v2.parquet`  |  분할: Train 80% / Val 10% / Test 10%  \n")
        f.write(f"> 방향정확도(필터): `|future_ret_15| ≥ 0.01%` 구간 기준  \n")
        f.write(f"> Top10%_DirAcc: `|pred| 상위 10%` 고확신 구간 기준 (회귀 전용)  \n\n")
        f.write(_md_table(df))
        f.write("\n\n")
        f.write("## 해석 가이드\n\n")
        f.write(
            "- **방향정확도(필터↑)**: RL 오라클 선발의 핵심 지표 — "
            "`|future_ret_15| ≥ 0.01%` 인 유의미한 구간에서 방향(UP/DOWN) 예측 정확도\n"
        )
        f.write(
            "- **Top10%_DirAcc**: 회귀 모델의 '확신 구간' 정밀도 — "
            "예측 크기가 클수록 얼마나 맞추는지\n"
        )
        f.write("- **레이턴시(ms)**: 512 배치 기준 중앙값, 작을수록 HFT 적합\n")
    print(f"  [저장] {md_path}")


# ══════════════════════════════════════════════════════════════════════════════
# 14. 출력 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def _section(title: str) -> None:
    print(f"\n{'─' * 62}")
    print(f"  {title}")
    print(f"{'─' * 62}")


def _print_leaderboard(df: pd.DataFrame) -> None:
    LINE = "═" * 62
    print(f"\n\n{LINE}")
    print("  🏆  ARENA LEADERBOARD  (방향정확도(필터↑) 기준 정렬)")
    print(LINE)
    try:
        print(df.to_string())
    except Exception:
        print(df)
    print(LINE)


# ══════════════════════════════════════════════════════════════════════════════
# 15. main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="compare_models_arena: ML/DL 8개 모델 통합 비교 아레나"
    )
    parser.add_argument(
        "--smoke_test", action="store_true",
        help="빠른 검증 모드 (15K 행, 5 에폭, n_estimators=50)"
    )
    parser.add_argument(
        "--epochs", type=int, default=30,
        help="DL 모델 최대 학습 에폭 (default: 30)"
    )
    parser.add_argument(
        "--patience", type=int, default=5,
        help="DL EarlyStopping patience (default: 5)"
    )
    parser.add_argument(
        "--no_dl", action="store_true",
        help="DL 모델 건너뜀 (ML 전용 빠른 비교)"
    )
    parser.add_argument(
        "--batch_size", type=int, default=512,
        help="DL 배치 크기 (default: 512)"
    )
    parser.add_argument(
        "--n_estimators", type=int, default=300,
        help="LGBM/XGB n_estimators (default: 300)"
    )
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # 스모크 테스트 오버라이드
    max_rows   = 15_000 if args.smoke_test else None
    max_epochs = 5      if args.smoke_test else args.epochs
    n_est      = 50     if args.smoke_test else args.n_estimators

    LINE = "═" * 62
    print(f"\n{LINE}")
    print("  Model Arena — ML/DL 전 모델 통합 비교")
    print(LINE)
    print(f"  디바이스    : {device}")
    print(f"  DL 에폭     : {max_epochs}  |  patience : {args.patience}")
    print(f"  n_estimators: {n_est}  |  batch_size : {args.batch_size}")
    if args.smoke_test:
        print("  [MODE] 스모크 테스트 (15K행, 5에폭)")
    if args.no_dl:
        print("  [MODE] ML 전용 — DL 건너뜀")

    # ── 1. 공유 데이터 로딩 ────────────────────────────────────────────
    bundle = load_data(max_rows=max_rows)

    results: list[ModelResult] = []

    # ── 2. ML 모델 ─────────────────────────────────────────────────────
    results.append(run_lgbm_clf(bundle, n_estimators=n_est))
    results.append(run_xgb_clf(bundle, n_estimators=n_est))
    results.append(run_ridge_reg(bundle))
    results.append(run_lgbm_reg(bundle, n_estimators=n_est))

    # ── 3. DL 모델 ─────────────────────────────────────────────────────
    if not args.no_dl:
        dl_kw = dict(
            device=device,
            max_epochs=max_epochs,
            patience=args.patience,
            batch_size=args.batch_size,
        )
        results.append(run_cryptomamba_bce(bundle, **dl_kw))
        results.append(run_tcn_bce(bundle, **dl_kw))
        results.append(run_lstm_bce(bundle, **dl_kw))
        results.append(run_cryptomamba_gmad(bundle, **dl_kw))

    # ── 4. 리더보드 구성 & 출력 ────────────────────────────────────────
    leaderboard = build_leaderboard(results)
    _print_leaderboard(leaderboard)
    save_leaderboard(leaderboard)

    print(f"\n  완료! 결과 파일:")
    print(f"    artifacts/arena_leaderboard.csv")
    print(f"    artifacts/arena_leaderboard.md\n")


if __name__ == "__main__":
    main()
