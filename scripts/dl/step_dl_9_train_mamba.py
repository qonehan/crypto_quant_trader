"""
Step DL-9: CryptoMamba 학습 스크립트  [v4 — 15분 타겟 GMADLoss 회귀]

개요
──────────────────────────────────────────────────────────────────────────────
  이 스크립트는 1분봉 60개(1시간 시퀀스)를 입력받아
  15분 뒤의 로그수익률(future_ret_15)을 GMADLoss 회귀로 학습합니다.
  방향(부호)과 변동성 크기(Magnitude)를 동시에 훈련시켜
  단순 이진 분류 대비 더 풍부한 알파 신호를 포착합니다.

학습 설정
──────────────────────────────────────────────────────────────────────────────
  데이터셋   : btc_1m_hft_v2.parquet  (67+피처 + future_ret_15 타겟)
  예측 타겟  : future_ret_15 — 실수 로그수익률 (회귀, 이진화 없음)
  모델       : CryptoMambaClassifier  (DWT + Selective SSM + KAN Mixer)
  손실함수   : GMADLoss (tau=std(train_ret15), gamma, alpha — 방향+Magnitude)
  옵티마이저 : AdamW (lr=3e-4, wd=1e-4) + clip_grad_norm(1.0)
  스케줄러   : CosineAnnealingLR (T_max = n_epochs)
  EarlyStopping: Val GMADLoss 최소화 기준, patience=15
  평가 지표  : GMADLoss / MSE / DirAcc (|ret|≥0.01% 필터)
  저장 경로  : artifacts/dl_prod/cryptomamba_model.pt

변경 이력
──────────────────────────────────────────────────────────────────────────────
  v4 (2026-03-14)
    - 타겟 복구: 이진 분류(> 0) → raw float 회귀 (future_ret_15 실수값 직접 사용)
    - 손실함수 교체: BCEWithLogitsLoss → GMADLoss (방향+Magnitude 동시 최적화)
    - tau 동적 계산: train_df[TARGET_COL].std() → meta["tau"] → GMADLoss(tau=tau)
    - 평가 지표 전면 개편: BCE/F1/BalAcc → gmadl/mse/dir_acc(노이즈 필터)
    - best_state 기준: Val BCE → Val GMADLoss
    - 에폭 출력: [Train GMADL | Val GMADL | Val MSE | Val DirAcc] 형태
    - pos_weight, n_pos/n_neg, long_ratio 관련 로직 전체 제거
    - CLI args 추가: --gamma (default 100), --alpha (default 0.70)

  v3 (2026-03-14)
    - 이진 분류 버전 (BCE + Confusion Matrix + F1 + BalAcc)

  v2 (2026-03-13)
    - TARGET_COL: future_ret_1 → future_ret_15

실행 방법
──────────────────────────────────────────────────────────────────────────────
  # 로컬 (CPU)
  poetry run python scripts/dl/step_dl_9_train_mamba.py

  # Colab (GPU)
  !python scripts/dl/step_dl_9_train_mamba.py --batch_size 1024 --epochs 100

  # 스모크 테스트 (로직 검증, 3에폭, 15K행)
  poetry run python scripts/dl/step_dl_9_train_mamba.py --smoke_test

  # GMADLoss 하이퍼파라미터 커스텀
  poetry run python scripts/dl/step_dl_9_train_mamba.py --gamma 150 --alpha 0.75
──────────────────────────────────────────────────────────────────────────────
Colab 환경 설정 (필요 시):
  !pip install pyarrow joblib -q
  from google.colab import drive
  drive.mount('/content/drive')
  # 프로젝트 루트를 '/content/drive/MyDrive/crypto_quant_trader' 에 업로드 후
  # --project_root 인자로 경로 지정
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import sys

import joblib
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.preprocessing import RobustScaler
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR, ReduceLROnPlateau
from torch.utils.data import DataLoader, Dataset

# ── 프로젝트 경로 주입 ─────────────────────────────────────────────────────
_PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJ))

from app.predictor.dl_model  import CryptoMambaClassifier   # noqa: E402
from app.predictor.losses    import GMADLoss                 # noqa: E402

# ── 경로 상수 ─────────────────────────────────────────────────────────────
DATASET_PATH  = _PROJ / "data" / "datasets" / "btc_1m_hft_v2.parquet"
ARTIFACT_DIR  = _PROJ / "artifacts" / "dl_prod"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH     = ARTIFACT_DIR / "cryptomamba_model.pt"
META_PATH      = ARTIFACT_DIR / "cryptomamba_model_meta.json"
SCALER_PATH    = ARTIFACT_DIR / "cryptomamba_scaler.joblib"
FEAT_COLS_PATH = ARTIFACT_DIR / "cryptomamba_feature_cols.json"
TRAIN_LOG_PATH = ARTIFACT_DIR / "cryptomamba_train_log.json"

# ── 데이터 상수 ───────────────────────────────────────────────────────────
# 예측 타겟: 15분 뒤 수익률 실수값 (회귀 — 이진화 없음)
TARGET_COL   = "future_ret_15"

# 미래 수익률 컬럼 전체 제외 — Data Leakage 원천 차단
EXCLUDE_COLS = {
    "target", "future_ret", "target_1m",
    "future_ret_1", "future_ret_5", "future_ret_15", "future_ret_60",
}

SEQ_LEN     = 60     # 슬라이딩 윈도우 길이 (60분봉 = 1시간 → 15분 예측)
TRAIN_RATIO = 0.70
VAL_RATIO   = 0.15


# ══════════════════════════════════════════════════════════════════════════════
# 1. Dataset & DataLoader
# ══════════════════════════════════════════════════════════════════════════════

class HFTTimeSeriesDataset(Dataset):
    """1분봉 60개 슬라이딩 윈도우 → 15분 수익률 실수값 반환 (회귀 타겟).

    타겟: future_ret_15 실수값 (이진화 없음 — GMADLoss 회귀용)

    Args:
        X      : (T, F) 정규화된 피처 배열
        y      : (T,)  실수 타겟 (future_ret_15 원본값)
        seq_len: 윈도우 크기 (default 60)
        stride : 윈도우 간격 (default 1)
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
        self._indices = list(range(0, len(X) - seq_len, max(1, stride)))

    def __len__(self) -> int:
        return len(self._indices)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        start = self._indices[idx]
        x_win = self.X[start : start + self.seq_len]    # (seq_len, F)
        y_val = self.y[start + self.seq_len]             # scalar — 실수 수익률
        return x_win, y_val


def build_loaders(
    parquet_path: Path = DATASET_PATH,
    seq_len: int = SEQ_LEN,
    batch_size: int = 512,
    num_workers: int = 0,
    train_ratio: float = TRAIN_RATIO,
    val_ratio: float = VAL_RATIO,
    max_rows: int | None = None,
    train_stride: int = 1,
) -> tuple[DataLoader, DataLoader, DataLoader, dict]:
    """btc_1m_hft_v2.parquet → Train/Val/Test DataLoader + 메타 딕셔너리.

    15분 타겟(future_ret_15) GMADLoss 회귀용 파이프라인:
      1. 시간순 분할 (누수 방지)
      2. ffill → train_median fillna (bfill 금지)
      3. 타겟은 raw float 그대로 사용 (이진화 없음)
      4. RobustScaler: Train only fit
      5. tau = train std(future_ret_15) → GMADLoss 커널 스케일

    Returns:
        (train_loader, val_loader, test_loader, meta)
        meta keys: n_features, feature_cols, split_sizes, tau
    """
    print(f"[Data] 15분 타겟 회귀 데이터 로드: {parquet_path.name}")
    df = pd.read_parquet(parquet_path)

    # future_ret_15는 데이터셋에 확정 포함됨 — 컬럼 부재 시 즉시 에러
    if TARGET_COL not in df.columns:
        raise KeyError(
            f"'{TARGET_COL}' 컬럼이 데이터셋에 없습니다. "
            f"step_dl_6_feature_engineering.py를 실행하여 {TARGET_COL}이 "
            f"포함된 parquet을 생성한 뒤 재실행하세요."
        )

    # target NaN 제거 (마지막 15봉 + 앞쪽 결측)
    df = df.dropna(subset=[TARGET_COL])

    # 피처 컬럼 결정 (미래 수익률 컬럼 전체 제외)
    feat_cols: list[str] = [c for c in df.columns if c not in EXCLUDE_COLS]

    if max_rows:
        df = df.iloc[:max_rows]
        print(f"  [Debug] max_rows={max_rows} 적용")

    # 데이터 누수 차단: 스케일링/결측치 처리 전에 먼저 시간순 분할
    n = len(df)
    n_tr = int(n * train_ratio)
    n_vl = int(n * val_ratio)

    train_df = df.iloc[:n_tr].copy()
    val_df   = df.iloc[n_tr : n_tr + n_vl].copy()
    test_df  = df.iloc[n_tr + n_vl:].copy()

    print(f"  분할 완료 → Train:{len(train_df):,}  Val:{len(val_df):,}  Test:{len(test_df):,}행")
    print(f"  피처 수: {len(feat_cols)}  |  예측 타겟: {TARGET_COL} (15분 회귀)")

    # 결측치 처리: ffill 먼저(시간 연속성 유지), 나머지는 Train median으로 채움
    # bfill은 미래 정보를 역방향으로 끌어오므로 사용 금지
    train_df[feat_cols] = train_df[feat_cols].ffill()
    val_df[feat_cols]   = val_df[feat_cols].ffill()
    test_df[feat_cols]  = test_df[feat_cols].ffill()

    train_median = train_df[feat_cols].median()
    train_df[feat_cols] = train_df[feat_cols].fillna(train_median)
    val_df[feat_cols]   = val_df[feat_cols].fillna(train_median)
    test_df[feat_cols]  = test_df[feat_cols].fillna(train_median)

    # 타겟: raw float 그대로 사용 (회귀 — 이진화 없음)
    y_tr = train_df[TARGET_COL].values.astype(np.float32)
    y_vl = val_df[TARGET_COL].values.astype(np.float32)
    y_te = test_df[TARGET_COL].values.astype(np.float32)

    # tau 계산: Train 타겟의 std → GMADLoss 커널 스케일 (누수 없음)
    tau = float(np.std(y_tr[seq_len:]))   # DataLoader가 사용하는 샘플 기준
    print(f"  15분 수익률 통계  Train: mean={y_tr.mean():+.6f}  std={y_tr.std():.6f}  "
          f"tau(GMADLoss)={tau:.6f}")

    # ── Scaler: Train only fit → Val/Test에 동일 변환 적용 (누수 없음) ──
    scaler = RobustScaler()
    scaler.fit(train_df[feat_cols].values)
    joblib.dump(scaler, SCALER_PATH)

    with open(FEAT_COLS_PATH, "w") as f:
        json.dump(feat_cols, f, indent=2)

    X_tr = scaler.transform(train_df[feat_cols].values).astype(np.float32)
    X_vl = scaler.transform(val_df[feat_cols].values).astype(np.float32)
    X_te = scaler.transform(test_df[feat_cols].values).astype(np.float32)

    # ── Dataset / DataLoader ─────────────────────────────────────────────
    train_ds = HFTTimeSeriesDataset(X_tr, y_tr, seq_len, stride=train_stride)
    val_ds   = HFTTimeSeriesDataset(X_vl, y_vl, seq_len, stride=1)
    test_ds  = HFTTimeSeriesDataset(X_te, y_te, seq_len, stride=1)

    train_loader = DataLoader(train_ds, batch_size, shuffle=True,  num_workers=num_workers, pin_memory=True)
    val_loader   = DataLoader(val_ds,   batch_size, shuffle=False, num_workers=num_workers, pin_memory=True)
    test_loader  = DataLoader(test_ds,  batch_size, shuffle=False, num_workers=num_workers, pin_memory=True)

    meta = {
        "n_features":    len(feat_cols),
        "feature_cols":  feat_cols,
        "seq_len":       seq_len,
        "batch_size":    batch_size,
        "train_samples": len(train_ds),
        "val_samples":   len(val_ds),
        "test_samples":  len(test_ds),
        "tau":           tau,   # GMADLoss 커널 스케일
    }
    return train_loader, val_loader, test_loader, meta


# ══════════════════════════════════════════════════════════════════════════════
# 2. 학습 유틸리티
# ══════════════════════════════════════════════════════════════════════════════

class EarlyStopping:
    """Val GMADLoss 기준 조기 종료.

    Args:
        patience  : 개선 없이 허용할 최대 에폭 수
        min_delta : 개선으로 인정하는 최소 변화량
        mode      : 'min' (loss 최소화) 또는 'max' (정확도 최대화)
    """

    def __init__(self, patience: int = 15, min_delta: float = 1e-6, mode: str = "min") -> None:
        self.patience  = patience
        self.min_delta = min_delta
        self.mode      = mode
        self.counter   = 0
        self.best      = float("inf") if mode == "min" else float("-inf")
        self.should_stop = False
        self.best_epoch  = 0

    def __call__(self, metric: float, epoch: int) -> bool:
        improved = (
            metric < self.best - self.min_delta if self.mode == "min"
            else metric > self.best + self.min_delta
        )
        if improved:
            self.best       = metric
            self.counter    = 0
            self.best_epoch = epoch
        else:
            self.counter += 1
            if self.counter >= self.patience:
                self.should_stop = True
        return self.should_stop


@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: DataLoader,
    criterion: GMADLoss,
    device: torch.device,
    min_magnitude: float = 1e-4,
) -> dict[str, float]:
    """평가 루프 — 회귀 맞춤형 지표 계산.

    반환 지표:
      - gmadl    : 평균 GMADLoss (방향+Magnitude 통합 손실)
      - mse      : 평균 제곱 오차 (예측값의 절대적 정확도)
      - dir_acc  : 방향 정확도 — |y_true| ≥ min_magnitude 인 샘플만 평가
                   (노이즈 필터: 0.01% 미만 미세 변동 제외)
      - n_valid  : 전체 샘플 수
      - n_filtered: 방향 정확도 계산에 사용된 샘플 수 (magnitude 필터 후)

    Args:
        criterion    : GMADLoss 인스턴스
        min_magnitude: 방향 정확도 계산 시 최소 변동폭 임계값 (default 1e-4 = 0.01%)
    """
    model.eval()
    total_gmadl = 0.0
    all_pred: list[torch.Tensor] = []
    all_true: list[torch.Tensor] = []

    for x_batch, y_batch in loader:
        x_batch = x_batch.to(device, non_blocking=True)
        y_batch = y_batch.to(device, non_blocking=True)

        pred = model(x_batch).squeeze(-1)          # (B,) — 예측 수익률 (raw)
        total_gmadl += criterion(pred, y_batch).item() * len(y_batch)
        all_pred.append(pred.cpu())
        all_true.append(y_batch.cpu())

    pred_arr = torch.cat(all_pred).numpy()
    true_arr = torch.cat(all_true).numpy()
    total_samples = len(true_arr)

    # MSE
    mse = float(((pred_arr - true_arr) ** 2).mean())

    # 방향 정확도: |y_true| ≥ min_magnitude 샘플에 대해 부호 일치 비율
    mag_mask = np.abs(true_arr) >= min_magnitude
    n_filtered = int(mag_mask.sum())
    if n_filtered > 0:
        dir_acc = float((np.sign(pred_arr[mag_mask]) == np.sign(true_arr[mag_mask])).mean())
    else:
        dir_acc = float("nan")

    return {
        "gmadl":      total_gmadl / max(total_samples, 1),
        "mse":        mse,
        "dir_acc":    dir_acc,
        "n_valid":    total_samples,
        "n_filtered": n_filtered,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. 학습 루프
# ══════════════════════════════════════════════════════════════════════════════

def train(
    n_epochs:     int   = 100,
    batch_size:   int   = 512,
    lr:           float = 3e-4,
    weight_decay: float = 1e-4,
    d_model:      int   = 64,
    n_low:        int   = 2,
    n_high:       int   = 1,
    d_conv:       int   = 4,
    dropout:      float = 0.10,
    max_grad_norm:float = 1.0,
    patience:     int   = 15,
    gamma:        float = 100.0,
    alpha:        float = 0.70,
    smoke_test:   bool  = False,
    scheduler_type: str = "cosine",
    num_workers:  int   = 0,
    project_root: Path | None = None,
) -> dict:
    """15분 수익률 GMADLoss 회귀 — 전체 학습 파이프라인 실행."""

    global _PROJ, DATASET_PATH, MODEL_PATH, META_PATH
    if project_root is not None:
        _PROJ = Path(project_root)
        DATASET_PATH = _PROJ / "data" / "datasets" / "btc_1m_hft_v2.parquet"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\n{'═'*62}")
    print(f"  Step DL-9: CryptoMamba  15분 수익률 GMADLoss 회귀 학습")
    print(f"  타겟: {TARGET_COL}  (raw float, 이진화 없음)")
    print(f"{'═'*62}")
    print(f"  디바이스  : {device}")
    print(f"  배치 크기 : {batch_size}  |  에폭 : {n_epochs}  |  patience : {patience}")
    print(f"  GMADLoss  : gamma={gamma}  alpha={alpha}")
    if smoke_test:
        print("  [MODE] 스모크 테스트 — 3 에폭, 15K행")

    # ── 데이터 로드 ─────────────────────────────────────────────────────
    max_rows = 15_000 if smoke_test else None
    n_ep_run = 3 if smoke_test else n_epochs

    train_loader, val_loader, test_loader, meta = build_loaders(
        parquet_path=DATASET_PATH,
        batch_size=batch_size,
        num_workers=num_workers,
        max_rows=max_rows,
        train_stride=2 if not smoke_test else 4,
    )

    n_features = meta["n_features"]
    tau        = meta["tau"]

    # ── 모델 ───────────────────────────────────────────────────────────
    model = CryptoMambaClassifier(
        n_features=n_features,
        d_model=d_model,
        n_low=n_low,
        n_high=n_high,
        d_conv=d_conv,
        dropout=dropout,
    ).to(device)

    total_params = model.count_params()
    print(f"\n  모델 파라미터: {total_params:,}")

    # ── 손실함수: GMADLoss ─────────────────────────────────────────────
    # tau = train std(future_ret_15): tanh 커널 스케일 — 15분 변동성 기준 자동 설정
    criterion = GMADLoss(tau=tau, gamma=gamma, alpha=alpha, normalize_w=True)
    criterion = criterion.to(device)
    print(f"  GMADLoss  tau={tau:.6f}  gamma={gamma}  alpha={alpha}  normalize_w=True")

    # ── 옵티마이저 + 스케줄러 ──────────────────────────────────────────
    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)

    if scheduler_type == "cosine":
        scheduler = CosineAnnealingLR(optimizer, T_max=n_ep_run, eta_min=lr * 0.01)
    else:
        scheduler = ReduceLROnPlateau(optimizer, mode="min", factor=0.5, patience=5)

    early_stop = EarlyStopping(patience=patience)

    # ── 학습 루프 ──────────────────────────────────────────────────────
    history: list[dict] = []
    best_state: dict | None = None
    best_metric = float("inf")   # Val GMADLoss 직접 추적
    t_start = time.time()

    print(f"\n  {'에폭':>5s}  {'Train GMADL':>13s}  {'Val GMADL':>11s}  "
          f"{'Val MSE':>11s}  {'DirAcc':>8s}  {'LR':>10s}  {'시간(s)':>7s}")
    print(f"  {'─'*80}")

    for epoch in range(1, n_ep_run + 1):
        t_ep = time.time()
        model.train()
        ep_loss = 0.0
        n_batches = 0

        for x_batch, y_batch in train_loader:
            x_batch = x_batch.to(device, non_blocking=True)
            y_batch = y_batch.to(device, non_blocking=True)

            optimizer.zero_grad()
            pred = model(x_batch).squeeze(-1)    # (B,) — 예측 수익률
            loss = criterion(pred, y_batch)
            loss.backward()

            nn.utils.clip_grad_norm_(model.parameters(), max_norm=max_grad_norm)
            optimizer.step()

            ep_loss   += loss.item()
            n_batches += 1

        train_gmadl = ep_loss / n_batches

        # ── Validation ────────────────────────────────────────────────
        val_metrics = evaluate(model, val_loader, criterion, device)

        # Val GMADLoss 최소화 기준으로 best_state 저장
        if val_metrics["gmadl"] < best_metric:
            best_metric = val_metrics["gmadl"]
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

        # ── LR 스케줄러 업데이트 ──────────────────────────────────────
        if scheduler_type == "cosine":
            scheduler.step()
        else:
            scheduler.step(val_metrics["gmadl"])

        current_lr = optimizer.param_groups[0]["lr"]
        ep_time    = time.time() - t_ep

        dir_str = f"{val_metrics['dir_acc']*100:.2f}%" if not np.isnan(val_metrics["dir_acc"]) else "  nan  "

        row = {
            "epoch":       epoch,
            "train_gmadl": train_gmadl,
            "val_gmadl":   val_metrics["gmadl"],
            "val_mse":     val_metrics["mse"],
            "val_dir_acc": val_metrics["dir_acc"],
            "lr":          current_lr,
        }
        history.append(row)

        print(f"  {epoch:>5d}  {train_gmadl:>13.6f}  {val_metrics['gmadl']:>11.6f}  "
              f"{val_metrics['mse']:>11.8f}  {dir_str:>8s}  "
              f"{current_lr:>10.2e}  {ep_time:>7.1f}")

        # ── EarlyStopping (Val GMADLoss 기준) ─────────────────────────
        stopped = early_stop(val_metrics["gmadl"], epoch)
        if stopped:
            print(f"\n  조기 종료: {early_stop.patience}에폭 개선 없음 "
                  f"(best at epoch {early_stop.best_epoch}, gmadl={early_stop.best:.6f})")
            break

    elapsed = time.time() - t_start
    print(f"\n  총 학습 시간: {elapsed:.1f}초 ({elapsed/60:.1f}분)")

    # ── 최고 모델 가중치 복원 & 저장 ──────────────────────────────────
    if best_state is not None:
        model.load_state_dict(best_state)

    model.save(MODEL_PATH, META_PATH)
    print(f"  모델 저장: {MODEL_PATH}")

    # ── Test 평가 ──────────────────────────────────────────────────────
    print(f"\n{'─'*62}")
    print("  최종 Test 평가 (best 모델) — 회귀 지표")
    print(f"{'─'*62}")
    test_metrics = evaluate(model, test_loader, criterion, device)
    print(f"  Test GMADLoss : {test_metrics['gmadl']:.6f}")
    print(f"  Test MSE      : {test_metrics['mse']:.8f}  "
          f"(RMSE={test_metrics['mse']**0.5:.6f})")
    dir_pct = test_metrics["dir_acc"] * 100 if not np.isnan(test_metrics["dir_acc"]) else float("nan")
    print(f"  방향 정확도   : {dir_pct:.2f}%  "
          f"(|ret|≥0.01% 필터 후 {test_metrics['n_filtered']:,}샘플 / "
          f"전체 {test_metrics['n_valid']:,}샘플)")

    # ── 학습 로그 저장 ─────────────────────────────────────────────────
    log = {
        "model_meta": {
            "n_features": n_features, "d_model": d_model, "n_low": n_low,
            "n_high": n_high, "d_conv": d_conv, "dropout": dropout,
        },
        "train_config": {
            "lr": lr, "weight_decay": weight_decay, "batch_size": batch_size,
            "n_epochs_run": len(history), "tau": tau,
            "gamma": gamma, "alpha": alpha,
            "max_grad_norm": max_grad_norm, "patience": patience,
            "target_col": TARGET_COL,
        },
        "best_epoch":      early_stop.best_epoch,
        "best_val_gmadl":  early_stop.best,
        "test_metrics":    test_metrics,
        "elapsed_sec":     elapsed,
        "history":         history,
    }
    with open(TRAIN_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)
    print(f"\n  학습 로그 저장: {TRAIN_LOG_PATH.name}")

    # ── 회귀 상세 분석 ──────────────────────────────────────────────────
    _print_regression_analysis(model, test_loader, criterion, device)

    print(f"\n{'═'*62}")
    print("  Step DL-9 완료")
    print(f"{'═'*62}\n")

    return log


def _print_regression_analysis(
    model: nn.Module,
    loader: DataLoader,
    criterion: GMADLoss,
    device: torch.device,
) -> None:
    """회귀 예측 결과 상세 분석.

    출력:
      - 예측값 분포 (mean, std, min, max)
      - magnitude 구간별 방향 정확도
      - 실제 vs 예측 부호 일치 분포
    """
    model.eval()
    all_pred: list[torch.Tensor] = []
    all_true: list[torch.Tensor] = []

    with torch.no_grad():
        for x_batch, y_batch in loader:
            pred = model(x_batch.to(device)).squeeze(-1)
            all_pred.append(pred.cpu())
            all_true.append(y_batch)

    pred_arr = torch.cat(all_pred).numpy()
    true_arr = torch.cat(all_true).numpy()

    print(f"\n{'─'*62}")
    print("  회귀 예측 상세 분석 (Test)")
    print(f"{'─'*62}")

    # ── 예측값 / 실제값 분포 ──────────────────────────────────────────
    print(f"  예측값 분포:")
    print(f"    mean={pred_arr.mean():+.6f}  std={pred_arr.std():.6f}  "
          f"min={pred_arr.min():+.6f}  max={pred_arr.max():+.6f}")
    print(f"  실제값 분포:")
    print(f"    mean={true_arr.mean():+.6f}  std={true_arr.std():.6f}  "
          f"min={true_arr.min():+.6f}  max={true_arr.max():+.6f}")

    # ── magnitude 구간별 방향 정확도 ──────────────────────────────────
    print(f"\n  magnitude 구간별 방향 정확도 (실제 |ret| 기준)")
    print(f"  {'구간':>30s}  {'샘플':>8s}  {'방향 정확도':>12s}")
    print(f"  {'─'*55}")

    thresholds = [
        ("전체 (필터 없음)             ", 0.0),
        ("|ret| ≥ 0.01%  (1e-4)       ", 1e-4),
        ("|ret| ≥ 0.05%  (5e-4)       ", 5e-4),
        ("|ret| ≥ 0.10%  (1e-3)       ", 1e-3),
        ("|ret| ≥ 0.20%  (2e-3)       ", 2e-3),
        ("|ret| ≥ 0.50%  (5e-3)       ", 5e-3),
    ]

    for label, thr in thresholds:
        mask = np.abs(true_arr) >= thr
        n = int(mask.sum())
        if n == 0:
            continue
        acc = float((np.sign(pred_arr[mask]) == np.sign(true_arr[mask])).mean())
        print(f"  {label}  {n:>8,}  {acc*100:>11.2f}%")

    # ── 예측 방향 분포 ────────────────────────────────────────────────
    pred_long  = (pred_arr > 0).mean()
    true_long  = (true_arr > 0).mean()
    print(f"\n  방향 예측 분포:")
    print(f"    LONG 예측 비율 (pred > 0): {pred_long*100:.1f}%")
    print(f"    실제 LONG 비율 (ret  > 0): {true_long*100:.1f}%")

    # ── 4분위 방향 정확도 ─────────────────────────────────────────────
    print(f"\n  실제 수익률 4분위별 방향 정확도:")
    q25, q50, q75 = np.percentile(true_arr, [25, 50, 75])
    quartiles = [
        (f"Q1 (ret < {q25:+.4f})          ", true_arr < q25),
        (f"Q2 ({q25:+.4f} ≤ ret < {q50:+.4f})", (true_arr >= q25) & (true_arr < q50)),
        (f"Q3 ({q50:+.4f} ≤ ret < {q75:+.4f})", (true_arr >= q50) & (true_arr < q75)),
        (f"Q4 (ret ≥ {q75:+.4f})          ", true_arr >= q75),
    ]
    for label, mask in quartiles:
        n = int(mask.sum())
        if n == 0:
            continue
        acc = float((np.sign(pred_arr[mask]) == np.sign(true_arr[mask])).mean())
        print(f"  {label}  {n:>8,}  {acc*100:>11.2f}%")


# ══════════════════════════════════════════════════════════════════════════════
# 4. CLI 진입점
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Step DL-9: CryptoMamba 학습 — 1분봉 60개로 15분 수익률 GMADLoss 회귀"
    )

    # 데이터
    parser.add_argument("--project_root", type=Path, default=None,
                        help="프로젝트 루트 경로 (Colab Drive 경로)")

    # 모델 하이퍼파라미터
    parser.add_argument("--d_model",  type=int,   default=64)
    parser.add_argument("--n_low",    type=int,   default=2)
    parser.add_argument("--n_high",   type=int,   default=1)
    parser.add_argument("--d_conv",   type=int,   default=4)
    parser.add_argument("--dropout",  type=float, default=0.10)

    # 학습 하이퍼파라미터
    parser.add_argument("--epochs",       type=int,   default=100)
    parser.add_argument("--batch_size",   type=int,   default=512)
    parser.add_argument("--lr",           type=float, default=3e-4)
    parser.add_argument("--weight_decay", type=float, default=1e-4)
    parser.add_argument("--patience",     type=int,   default=15)
    parser.add_argument("--max_grad_norm",type=float, default=1.0)

    # GMADLoss 하이퍼파라미터
    parser.add_argument("--gamma", type=float, default=100.0,
                        help="GMADLoss exp 평활화 계수 (15분 기준 기본값=100)")
    parser.add_argument("--alpha", type=float, default=0.70,
                        help="GMADLoss 방향 손실 비율 (0~1, 기본값=0.70)")

    # 스케줄러
    parser.add_argument("--scheduler", type=str, default="cosine",
                        choices=["cosine", "plateau"],
                        help="LR 스케줄러 종류")

    # 실행 모드
    parser.add_argument("--smoke_test", action="store_true",
                        help="빠른 로직 검증 (3에폭, 15K행)")
    parser.add_argument("--num_workers", type=int, default=0)

    args = parser.parse_args()

    result = train(
        n_epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        weight_decay=args.weight_decay,
        d_model=args.d_model,
        n_low=args.n_low,
        n_high=args.n_high,
        d_conv=args.d_conv,
        dropout=args.dropout,
        max_grad_norm=args.max_grad_norm,
        patience=args.patience,
        gamma=args.gamma,
        alpha=args.alpha,
        smoke_test=args.smoke_test,
        scheduler_type=args.scheduler,
        num_workers=args.num_workers,
        project_root=args.project_root,
    )
