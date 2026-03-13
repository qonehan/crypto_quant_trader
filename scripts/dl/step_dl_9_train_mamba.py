"""
Step DL-9: CryptoMambaClassifier GPU 학습 스크립트

학습 설정
──────────────────────────────────────────────────────────────────────────────
  데이터셋  : btc_1m_hft_v2.parquet  (67피처 + future_ret_1 타겟)
  모델      : CryptoMambaClassifier  (DWT + Selective SSM + KAN Mixer)
  손실함수  : GMADLoss (tau = std(future_ret_1) 동적 계산)
  옵티마이저: AdamW (lr=3e-4, wd=1e-4) + clip_grad_norm(1.0)
  스케줄러  : CosineAnnealingLR (T_max = n_epochs)
  EarlyStopping: Val GMADLoss 기준, patience=15
  저장 경로 : artifacts/dl_prod/cryptomamba_model.pt

실행 방법
──────────────────────────────────────────────────────────────────────────────
  # 로컬 (CPU)
  poetry run python scripts/dl/step_dl_9_train_mamba.py

  # Colab (GPU) — 데이터 업로드 후
  !python scripts/dl/step_dl_9_train_mamba.py --batch_size 1024 --epochs 100

  # 빠른 스모크 테스트 (로직 검증용, 3 epochs, 1% 데이터)
  poetry run python scripts/dl/step_dl_9_train_mamba.py --smoke_test
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

from app.predictor.dl_model import CryptoMambaClassifier   # noqa: E402
from app.predictor.losses import GMADLoss                  # noqa: E402

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
TARGET_COL   = "future_ret_1"                        # 연속형 회귀 타겟
EXCLUDE_COLS = {"target", "future_ret", "target_1m", "future_ret_1"}

SEQ_LEN     = 60     # 슬라이딩 윈도우 길이 (1시간 = 60분봉)
TRAIN_RATIO = 0.70
VAL_RATIO   = 0.15


# ══════════════════════════════════════════════════════════════════════════════
# 1. Dataset & DataLoader
# ══════════════════════════════════════════════════════════════════════════════

class HFTTimeSeriesDataset(Dataset):
    """v2 데이터셋용 슬라이딩 윈도우 시계열 Dataset.

    기존 CryptoTimeSeriesDataset과 동일 구조이나
    연속형 타겟(future_ret_1)을 반환.

    Args:
        X      : (T, F) 정규화된 피처 배열
        y      : (T,)  연속형 타겟 (future_ret_1)
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
        y_lbl = self.y[start + self.seq_len]             # scalar
        return x_win, y_lbl


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
    """v2 parquet → Train/Val/Test DataLoader + 메타 딕셔너리.

    Returns:
        (train_loader, val_loader, test_loader, meta)
        meta keys: n_features, tau, feature_cols, split_sizes, pos_ratios
    """
    print(f"[Data] 로드: {parquet_path.name}")
    df = pd.read_parquet(parquet_path)

    # target NaN 제거 (마지막 1행)
    df = df.dropna(subset=[TARGET_COL])

    # 피처 컬럼 결정
    feat_cols: list[str] = [c for c in df.columns if c not in EXCLUDE_COLS]
    df[feat_cols] = df[feat_cols].ffill().bfill()

    if max_rows:
        df = df.iloc[:max_rows]
        print(f"  [Debug] max_rows={max_rows} 적용")

    # 시간순 분할 (shuffle 금지)
    n = len(df)
    n_tr = int(n * train_ratio)
    n_vl = int(n * val_ratio)

    train_df = df.iloc[:n_tr]
    val_df   = df.iloc[n_tr : n_tr + n_vl]
    test_df  = df.iloc[n_tr + n_vl:]

    print(f"  Train : {len(train_df):,}행  |  Val : {len(val_df):,}행  |  Test : {len(test_df):,}행")
    print(f"  피처 수: {len(feat_cols)}  |  타겟: {TARGET_COL}")

    # ── Scaler (Train only) ─────────────────────────────────────────────
    scaler = RobustScaler()
    scaler.fit(train_df[feat_cols].values)
    joblib.dump(scaler, SCALER_PATH)

    with open(FEAT_COLS_PATH, "w") as f:
        json.dump(feat_cols, f, indent=2)

    # ── tau 동적 계산 (future_ret_1 std on train) ─────────────────────
    tau = float(train_df[TARGET_COL].std())
    print(f"  tau (future_ret_1 std) = {tau:.6f}")

    def to_arrays(split: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        X = scaler.transform(split[feat_cols].values).astype(np.float32)
        y = split[TARGET_COL].values.astype(np.float32)
        return X, y

    X_tr, y_tr = to_arrays(train_df)
    X_vl, y_vl = to_arrays(val_df)
    X_te, y_te = to_arrays(test_df)

    # ── 데이터셋 생성 ────────────────────────────────────────────────────
    train_ds = HFTTimeSeriesDataset(X_tr, y_tr, seq_len, stride=train_stride)
    val_ds   = HFTTimeSeriesDataset(X_vl, y_vl, seq_len, stride=1)
    test_ds  = HFTTimeSeriesDataset(X_te, y_te, seq_len, stride=1)

    # ── DataLoader ───────────────────────────────────────────────────────
    train_loader = DataLoader(train_ds, batch_size, shuffle=True,  num_workers=num_workers, pin_memory=True)
    val_loader   = DataLoader(val_ds,   batch_size, shuffle=False, num_workers=num_workers, pin_memory=True)
    test_loader  = DataLoader(test_ds,  batch_size, shuffle=False, num_workers=num_workers, pin_memory=True)

    # ── 방향 비율 (양수 return = LONG 방향) ─────────────────────────────
    def pos_ratio(y_arr: np.ndarray) -> float:
        return float((y_arr[seq_len:] > 0).mean())

    print(f"  LONG 방향 비율  Train={pos_ratio(y_tr):.3f}  "
          f"Val={pos_ratio(y_vl):.3f}  Test={pos_ratio(y_te):.3f}")

    meta = {
        "n_features":   len(feat_cols),
        "tau":          tau,
        "feature_cols": feat_cols,
        "seq_len":      seq_len,
        "batch_size":   batch_size,
        "train_samples": len(train_ds),
        "val_samples":   len(val_ds),
        "test_samples":  len(test_ds),
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
    min_magnitude: float = 1e-5,
) -> dict[str, float]:
    """평가 루프 — GMADLoss, 방향 정확도, MSE 계산.

    Args:
        min_magnitude: 방향 정확도 계산 시 타겟 절대값 최소 임계값
                       (0 근방 노이즈 제외)
    """
    model.eval()
    total_gmadl, total_mse, total_samples = 0.0, 0.0, 0
    n_correct, n_valid = 0, 0

    for x_batch, y_batch in loader:
        x_batch = x_batch.to(device, non_blocking=True)
        y_batch = y_batch.to(device, non_blocking=True)

        pred = model(x_batch).squeeze(-1)   # (B,)
        y    = y_batch                       # (B,)
        B    = len(y)

        # GMADLoss
        total_gmadl += criterion(pred, y).item() * B

        # MSE
        total_mse += torch.mean((pred - y) ** 2).item() * B

        # 방향 정확도 (|target| ≥ min_magnitude인 샘플만)
        mask = y.abs() >= min_magnitude
        if mask.sum() > 0:
            correct = (torch.sign(pred[mask]) == torch.sign(y[mask])).sum().item()
            n_correct += correct
            n_valid   += int(mask.sum())

        total_samples += B

    dir_acc = n_correct / n_valid if n_valid > 0 else float("nan")
    return {
        "gmadl":    total_gmadl  / total_samples,
        "mse":      total_mse    / total_samples,
        "dir_acc":  dir_acc,
        "n_valid":  n_valid,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. 학습 루프
# ══════════════════════════════════════════════════════════════════════════════

def train(
    n_epochs:    int   = 100,
    batch_size:  int   = 512,
    lr:          float = 3e-4,
    weight_decay:float = 1e-4,
    d_model:     int   = 64,
    n_low:       int   = 2,
    n_high:      int   = 1,
    d_conv:      int   = 4,
    dropout:     float = 0.10,
    gamma:       float = 500.0,
    alpha:       float = 0.70,
    max_grad_norm: float = 1.0,
    patience:    int   = 15,
    smoke_test:  bool  = False,
    scheduler_type: str = "cosine",
    num_workers: int   = 0,
    project_root: Path | None = None,
) -> dict:
    """전체 학습 파이프라인 실행."""

    global _PROJ, DATASET_PATH, MODEL_PATH, META_PATH
    if project_root is not None:
        _PROJ = Path(project_root)
        DATASET_PATH = _PROJ / "data" / "datasets" / "btc_1m_hft_v2.parquet"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\n{'═'*62}")
    print(f"  Step DL-9: CryptoMamba 학습 시작")
    print(f"{'═'*62}")
    print(f"  디바이스  : {device}")
    print(f"  배치 크기 : {batch_size}  |  에폭 : {n_epochs}  |  patience : {patience}")
    if smoke_test:
        print("  [MODE] 스모크 테스트 — 3 에폭, 데이터 1%")

    # ── 데이터 로드 ─────────────────────────────────────────────────────
    max_rows = 15_000 if smoke_test else None
    n_ep_run = 3 if smoke_test else n_epochs

    train_loader, val_loader, test_loader, meta = build_loaders(
        parquet_path=DATASET_PATH,
        batch_size=batch_size,
        num_workers=num_workers,
        max_rows=max_rows,
        train_stride=2 if not smoke_test else 4,  # stride로 학습 속도 조절
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

    # ── 손실 함수 (tau 동적 설정) ──────────────────────────────────────
    criterion = GMADLoss(tau=tau, gamma=gamma, alpha=alpha, normalize_w=True)
    print(f"  GMADLoss tau={tau:.6f}  gamma={gamma}  alpha={alpha}")

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
    t_start = time.time()

    print(f"\n  {'에폭':>5s}  {'Train GMADL':>12s}  {'Val GMADL':>10s}  "
          f"{'Val DirAcc':>10s}  {'Val MSE':>10s}  {'LR':>10s}  {'시간(s)':>8s}")
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
            pred = model(x_batch).squeeze(-1)           # (B,)
            loss = criterion(pred, y_batch)
            loss.backward()

            # ── Gradient Clipping (GMADL 극단적 그래디언트 방지) ─────
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=max_grad_norm)

            optimizer.step()
            ep_loss   += loss.item()
            n_batches += 1

        train_gmadl = ep_loss / n_batches

        # ── Validation ────────────────────────────────────────────────
        val_metrics = evaluate(model, val_loader, criterion, device)

        # ── LR 스케줄러 업데이트 ──────────────────────────────────────
        if scheduler_type == "cosine":
            scheduler.step()
        else:
            scheduler.step(val_metrics["gmadl"])

        current_lr = optimizer.param_groups[0]["lr"]
        ep_time    = time.time() - t_ep

        row = {
            "epoch":       epoch,
            "train_gmadl": train_gmadl,
            "val_gmadl":   val_metrics["gmadl"],
            "val_dir_acc": val_metrics["dir_acc"],
            "val_mse":     val_metrics["mse"],
            "lr":          current_lr,
        }
        history.append(row)

        # ── 최고 모델 저장 ─────────────────────────────────────────────
        if early_stop.best_epoch == epoch - 1 or epoch == 1:
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

        print(f"  {epoch:>5d}  {train_gmadl:>12.6f}  {val_metrics['gmadl']:>10.6f}  "
              f"{val_metrics['dir_acc']:>10.4f}  {val_metrics['mse']:>10.2e}  "
              f"{current_lr:>10.2e}  {ep_time:>8.1f}")

        # ── EarlyStopping ─────────────────────────────────────────────
        stopped = early_stop(val_metrics["gmadl"], epoch)
        if stopped:
            print(f"\n  조기 종료: {early_stop.patience}에폭 개선 없음 (best at epoch {early_stop.best_epoch})")
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
    print("  최종 Test 평가 (best 모델)")
    print(f"{'─'*62}")
    test_metrics = evaluate(model, test_loader, criterion, device)
    print(f"  Test GMADL      : {test_metrics['gmadl']:.6f}")
    print(f"  Test 방향 정확도: {test_metrics['dir_acc']*100:.2f}%  "
          f"(유효 샘플 {test_metrics['n_valid']:,}개)")
    print(f"  Test MSE        : {test_metrics['mse']:.2e}")

    # ── 학습 로그 저장 ─────────────────────────────────────────────────
    log = {
        "model_meta": {
            "n_features": n_features, "d_model": d_model, "n_low": n_low,
            "n_high": n_high, "d_conv": d_conv, "dropout": dropout,
        },
        "train_config": {
            "lr": lr, "weight_decay": weight_decay, "batch_size": batch_size,
            "n_epochs_run": len(history), "tau": tau, "gamma": gamma, "alpha": alpha,
            "max_grad_norm": max_grad_norm, "patience": patience,
        },
        "best_epoch":   early_stop.best_epoch,
        "best_val_gmadl": early_stop.best,
        "test_metrics": test_metrics,
        "elapsed_sec":  elapsed,
        "history":      history,
    }
    with open(TRAIN_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)
    print(f"\n  학습 로그 저장: {TRAIN_LOG_PATH.name}")

    # ── 방향 정확도 분포 분석 ──────────────────────────────────────────
    _print_direction_analysis(model, test_loader, device)

    print(f"\n{'═'*62}")
    print("  Step DL-9 완료")
    print(f"{'═'*62}\n")

    return log


def _print_direction_analysis(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
) -> None:
    """예측값 vs 실제값 방향 분석 — magnitude 구간별 정확도."""
    model.eval()
    all_pred, all_true = [], []

    with torch.no_grad():
        for x_batch, y_batch in loader:
            pred = model(x_batch.to(device)).squeeze(-1).cpu()
            all_pred.append(pred)
            all_true.append(y_batch)

    pred_arr = torch.cat(all_pred).numpy()
    true_arr = torch.cat(all_true).numpy()

    print(f"\n{'─'*62}")
    print("  magnitude 구간별 방향 정확도 (Test)")
    print(f"{'─'*62}")
    print(f"  {'구간':>20s}  {'샘플 수':>10s}  {'방향 정확도':>12s}")
    print(f"  {'─'*48}")

    thresholds = [
        ("전체 샘플",       0.0,    np.inf),
        ("|ret| > 0.01%",  1e-4,   np.inf),
        ("|ret| > 0.05%",  5e-4,   np.inf),
        ("|ret| > 0.1%",   1e-3,   np.inf),
        ("|ret| > 0.2%",   2e-3,   np.inf),
        ("|ret| 0.01-0.1%", 1e-4,  1e-3),
        ("|ret| > 0.1%",    1e-3,   np.inf),
    ]

    for label, lo, hi in thresholds:
        mask = (np.abs(true_arr) >= lo) & (np.abs(true_arr) < hi)
        n = mask.sum()
        if n == 0:
            continue
        acc = (np.sign(pred_arr[mask]) == np.sign(true_arr[mask])).mean()
        print(f"  {label:>20s}  {n:>10,}  {acc*100:>11.2f}%")

    # 예측 분포
    print(f"\n  예측값(pred) 분포:")
    print(f"    mean={pred_arr.mean():.6f}  std={pred_arr.std():.6f}")
    print(f"    min={pred_arr.min():.6f}  max={pred_arr.max():.6f}")
    print(f"    pred > 0 비율 (LONG 신호): {(pred_arr > 0).mean()*100:.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# 4. CLI 진입점
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Step DL-9: CryptoMamba 학습")

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
    parser.add_argument("--epochs",      type=int,   default=100)
    parser.add_argument("--batch_size",  type=int,   default=512)
    parser.add_argument("--lr",          type=float, default=3e-4)
    parser.add_argument("--weight_decay",type=float, default=1e-4)
    parser.add_argument("--patience",    type=int,   default=15)
    parser.add_argument("--max_grad_norm", type=float, default=1.0)

    # 손실 함수
    parser.add_argument("--gamma", type=float, default=500.0,
                        help="GMADLoss 지수 평활화 계수")
    parser.add_argument("--alpha", type=float, default=0.70,
                        help="GMADLoss 방향성 비율")

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
        gamma=args.gamma,
        alpha=args.alpha,
        max_grad_norm=args.max_grad_norm,
        patience=args.patience,
        smoke_test=args.smoke_test,
        scheduler_type=args.scheduler,
        num_workers=args.num_workers,
        project_root=args.project_root,
    )
