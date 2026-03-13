"""
Step DL-9: CryptoMambaClassifier GPU 학습 스크립트  [v2 — 15분 타겟 이진 분류]

학습 설정
──────────────────────────────────────────────────────────────────────────────
  데이터셋  : btc_1m_hft_v2.parquet  (67피처 + future_ret_15 타겟)
  타겟 변환 : future_ret_15 > 0 → 1 (LONG), ≤ 0 → 0 (SHORT/FLAT)
  모델      : CryptoMambaClassifier  (DWT + Selective SSM + KAN Mixer)
  손실함수  : BCEWithLogitsLoss (pos_weight 동적 계산: (1-long_ratio)/long_ratio)
  옵티마이저: AdamW (lr=3e-4, wd=1e-4) + clip_grad_norm(1.0)
  스케줄러  : CosineAnnealingLR (T_max = n_epochs)
  EarlyStopping: Val BCE Loss 기준, patience=15
  저장 경로 : artifacts/dl_prod/cryptomamba_model.pt

변경 이력
──────────────────────────────────────────────────────────────────────────────
  v2 (2026-03-13)
    - TARGET_COL: future_ret_1 → future_ret_15 (노이즈 감소, 추세 신호 강화)
    - EXCLUDE_COLS에 future_ret_5/15/60 추가 (Data Leakage 원천 차단)
    - future_ret_15 없으면 close.pct_change(15).shift(-15) 즉석 계산
    - 타겟을 이진(0/1)으로 변환 후 분할 (SettingWithCopyWarning 제거)
    - 손실함수: GMADLoss → BCEWithLogitsLoss (pos_weight 동적 설정)
    - evaluate(): 시그모이드 임계값 기반 방향 정확도로 교체

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
# [수정 1] 타겟 변수: future_ret_1 → future_ret_15
TARGET_COL   = "future_ret_15"

# [수정 1] 미래 수익률 컬럼 전체 제외 (Data Leakage 원천 차단)
EXCLUDE_COLS = {
    "target", "future_ret", "target_1m",
    "future_ret_1", "future_ret_5", "future_ret_15", "future_ret_60",
}

SEQ_LEN     = 60     # 슬라이딩 윈도우 길이 (1시간 = 60분봉)
TRAIN_RATIO = 0.70
VAL_RATIO   = 0.15


# ══════════════════════════════════════════════════════════════════════════════
# 1. Dataset & DataLoader
# ══════════════════════════════════════════════════════════════════════════════

class HFTTimeSeriesDataset(Dataset):
    """v2 데이터셋용 슬라이딩 윈도우 시계열 Dataset.

    이진 분류 타겟(future_ret_15 > 0)을 반환.

    Args:
        X      : (T, F) 정규화된 피처 배열
        y      : (T,)  이진 타겟 (0 or 1)
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
        y_lbl = self.y[start + self.seq_len]             # scalar (0 or 1)
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
        meta keys: n_features, feature_cols, split_sizes, long_ratio
    """
    print(f"[Data] 로드: {parquet_path.name}")
    df = pd.read_parquet(parquet_path)

    # [수정 2] future_ret_15가 없으면 즉석 계산 (close 기준 15분 수익률)
    if TARGET_COL not in df.columns:
        print(f"  [Info] '{TARGET_COL}' 컬럼 없음 → close 기준 즉석 계산")
        if "close" not in df.columns:
            raise ValueError("parquet에 'close' 컬럼이 없어 future_ret_15를 계산할 수 없습니다.")
        df[TARGET_COL] = df["close"].pct_change(15).shift(-15)

    # target NaN 제거 (마지막 15행 + 앞쪽 결측)
    df = df.dropna(subset=[TARGET_COL])

    # [수정 2] 이진 분류 타겟으로 변환 (분할 전 일괄 처리 → SettingWithCopyWarning 제거)
    df[TARGET_COL] = (df[TARGET_COL] > 0).astype(np.float32)

    # 피처 컬럼 결정 (미래 수익률 컬럼 전체 제외)
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
    print(f"  피처 수: {len(feat_cols)}  |  타겟: {TARGET_COL} (이진 분류)")

    # ── Scaler (Train only) ─────────────────────────────────────────────
    scaler = RobustScaler()
    scaler.fit(train_df[feat_cols].values)
    joblib.dump(scaler, SCALER_PATH)

    with open(FEAT_COLS_PATH, "w") as f:
        json.dump(feat_cols, f, indent=2)

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

    # [수정 3] LONG 비율 계산 (윈도우 뒤 샘플 기준) → pos_weight 계산에 사용
    def pos_ratio(y_arr: np.ndarray) -> float:
        return float(y_arr[seq_len:].mean())

    long_ratio_tr = pos_ratio(y_tr)
    print(f"  LONG 비율  Train={long_ratio_tr:.3f}  "
          f"Val={pos_ratio(y_vl):.3f}  Test={pos_ratio(y_te):.3f}")

    meta = {
        "n_features":    len(feat_cols),
        "feature_cols":  feat_cols,
        "seq_len":       seq_len,
        "batch_size":    batch_size,
        "train_samples": len(train_ds),
        "val_samples":   len(val_ds),
        "test_samples":  len(test_ds),
        # [수정 3] pos_weight 계산용 LONG 비율
        "long_ratio":    long_ratio_tr,
    }
    return train_loader, val_loader, test_loader, meta


# ══════════════════════════════════════════════════════════════════════════════
# 2. 학습 유틸리티
# ══════════════════════════════════════════════════════════════════════════════

class EarlyStopping:
    """Val BCE Loss 기준 조기 종료.

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
    criterion: nn.Module,
    device: torch.device,
) -> dict[str, float]:
    """평가 루프 — BCE Loss, 이진 분류 정확도 계산.

    Args:
        criterion: BCEWithLogitsLoss 인스턴스
    """
    model.eval()
    total_bce, total_samples = 0.0, 0
    n_correct = 0

    for x_batch, y_batch in loader:
        x_batch = x_batch.to(device, non_blocking=True)
        y_batch = y_batch.to(device, non_blocking=True)

        logit = model(x_batch).squeeze(-1)   # (B,) — raw logit
        B = len(y_batch)

        # BCE Loss
        total_bce += criterion(logit, y_batch).item() * B

        # 이진 정확도: sigmoid(logit) > 0.5 → 1 (LONG 예측)
        pred_label = (torch.sigmoid(logit) > 0.5).float()
        n_correct  += (pred_label == y_batch).sum().item()

        total_samples += B

    dir_acc = n_correct / total_samples if total_samples > 0 else float("nan")
    return {
        "bce":      total_bce   / total_samples,
        "dir_acc":  dir_acc,
        "n_valid":  total_samples,
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
    max_grad_norm: float = 1.0,
    patience:    int   = 15,
    smoke_test:  bool  = False,
    scheduler_type: str = "cosine",
    num_workers: int   = 0,
    project_root: Path | None = None,
) -> dict:
    """전체 학습 파이프라인 실행 (이진 분류 버전)."""

    global _PROJ, DATASET_PATH, MODEL_PATH, META_PATH
    if project_root is not None:
        _PROJ = Path(project_root)
        DATASET_PATH = _PROJ / "data" / "datasets" / "btc_1m_hft_v2.parquet"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\n{'═'*62}")
    print(f"  Step DL-9: CryptoMamba 학습 시작  [타겟: {TARGET_COL}]")
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
        train_stride=2 if not smoke_test else 4,
    )

    n_features = meta["n_features"]
    long_ratio  = meta["long_ratio"]

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

    # [수정 3] 동적 pos_weight 계산: (1 - LONG비율) / LONG비율
    # LONG이 적을수록 pos_weight > 1 → LONG 클래스에 더 큰 패널티
    pos_weight_val = (1.0 - long_ratio) / max(long_ratio, 1e-6)
    pos_weight_tensor = torch.tensor([pos_weight_val], dtype=torch.float32, device=device)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight_tensor)
    print(f"  BCEWithLogitsLoss  pos_weight={pos_weight_val:.4f}  "
          f"(LONG비율={long_ratio:.3f})")

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

    print(f"\n  {'에폭':>5s}  {'Train BCE':>12s}  {'Val BCE':>10s}  "
          f"{'Val DirAcc':>10s}  {'LR':>10s}  {'시간(s)':>8s}")
    print(f"  {'─'*70}")

    for epoch in range(1, n_ep_run + 1):
        t_ep = time.time()
        model.train()
        ep_loss = 0.0
        n_batches = 0

        for x_batch, y_batch in train_loader:
            x_batch = x_batch.to(device, non_blocking=True)
            y_batch = y_batch.to(device, non_blocking=True)

            optimizer.zero_grad()
            logit = model(x_batch).squeeze(-1)           # (B,) raw logit
            loss  = criterion(logit, y_batch)
            loss.backward()

            nn.utils.clip_grad_norm_(model.parameters(), max_norm=max_grad_norm)

            optimizer.step()
            ep_loss   += loss.item()
            n_batches += 1

        train_bce = ep_loss / n_batches

        # ── Validation ────────────────────────────────────────────────
        val_metrics = evaluate(model, val_loader, criterion, device)

        # ── LR 스케줄러 업데이트 ──────────────────────────────────────
        if scheduler_type == "cosine":
            scheduler.step()
        else:
            scheduler.step(val_metrics["bce"])

        current_lr = optimizer.param_groups[0]["lr"]
        ep_time    = time.time() - t_ep

        row = {
            "epoch":       epoch,
            "train_bce":   train_bce,
            "val_bce":     val_metrics["bce"],
            "val_dir_acc": val_metrics["dir_acc"],
            "lr":          current_lr,
        }
        history.append(row)

        # ── 최고 모델 저장 ─────────────────────────────────────────────
        if early_stop.best_epoch == epoch - 1 or epoch == 1:
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

        print(f"  {epoch:>5d}  {train_bce:>12.6f}  {val_metrics['bce']:>10.6f}  "
              f"{val_metrics['dir_acc']*100:>9.2f}%  "
              f"{current_lr:>10.2e}  {ep_time:>8.1f}")

        # ── EarlyStopping ─────────────────────────────────────────────
        stopped = early_stop(val_metrics["bce"], epoch)
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
    print(f"  Test BCE Loss   : {test_metrics['bce']:.6f}")
    print(f"  Test 방향 정확도: {test_metrics['dir_acc']*100:.2f}%  "
          f"(전체 {test_metrics['n_valid']:,}샘플)")

    # ── 학습 로그 저장 ─────────────────────────────────────────────────
    log = {
        "model_meta": {
            "n_features": n_features, "d_model": d_model, "n_low": n_low,
            "n_high": n_high, "d_conv": d_conv, "dropout": dropout,
        },
        "train_config": {
            "lr": lr, "weight_decay": weight_decay, "batch_size": batch_size,
            "n_epochs_run": len(history), "long_ratio": long_ratio,
            "pos_weight": pos_weight_val, "max_grad_norm": max_grad_norm,
            "patience": patience, "target_col": TARGET_COL,
        },
        "best_epoch":     early_stop.best_epoch,
        "best_val_bce":   early_stop.best,
        "test_metrics":   test_metrics,
        "elapsed_sec":    elapsed,
        "history":        history,
    }
    with open(TRAIN_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)
    print(f"\n  학습 로그 저장: {TRAIN_LOG_PATH.name}")

    # ── 예측 분포 분석 ──────────────────────────────────────────────────
    _print_prediction_analysis(model, test_loader, device)

    print(f"\n{'═'*62}")
    print("  Step DL-9 완료")
    print(f"{'═'*62}\n")

    return log


def _print_prediction_analysis(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
) -> None:
    """예측 확률 분포 및 신뢰도 구간별 정확도 분석."""
    model.eval()
    all_prob, all_true = [], []

    with torch.no_grad():
        for x_batch, y_batch in loader:
            logit = model(x_batch.to(device)).squeeze(-1)
            prob  = torch.sigmoid(logit).cpu()
            all_prob.append(prob)
            all_true.append(y_batch)

    prob_arr = torch.cat(all_prob).numpy()
    true_arr = torch.cat(all_true).numpy()

    print(f"\n{'─'*62}")
    print("  신뢰도 구간별 정확도 (Test, 이진 분류)")
    print(f"{'─'*62}")
    print(f"  {'구간':>25s}  {'샘플 수':>10s}  {'정확도':>10s}")
    print(f"  {'─'*50}")

    bands = [
        ("전체",                        0.00, 1.00),
        ("LONG 고확신  (prob > 0.60)",   0.60, 1.00),
        ("LONG 중확신  (0.55 < p ≤ 0.60)", 0.55, 0.60),
        ("SHORT 고확신 (prob < 0.40)",   0.00, 0.40),
        ("SHORT 중확신 (0.40 ≤ p < 0.45)", 0.40, 0.45),
        ("불확실 구간  (0.45 ≤ p ≤ 0.55)", 0.45, 0.55),
    ]

    for label, lo, hi in bands:
        mask = (prob_arr >= lo) & (prob_arr < hi)
        n = mask.sum()
        if n == 0:
            continue
        pred_bin = (prob_arr[mask] > 0.5).astype(float)
        acc = (pred_bin == true_arr[mask]).mean()
        print(f"  {label:>25s}  {n:>10,}  {acc*100:>9.2f}%")

    print(f"\n  예측 확률(sigmoid) 분포:")
    print(f"    mean={prob_arr.mean():.4f}  std={prob_arr.std():.4f}")
    print(f"    min={prob_arr.min():.4f}  max={prob_arr.max():.4f}")
    print(f"    LONG 예측 비율 (prob > 0.5): {(prob_arr > 0.5).mean()*100:.1f}%")
    print(f"    실제 LONG 비율             : {true_arr.mean()*100:.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# 4. CLI 진입점
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Step DL-9: CryptoMamba 학습 (15분 이진 분류)")

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
        smoke_test=args.smoke_test,
        scheduler_type=args.scheduler,
        num_workers=args.num_workers,
        project_root=args.project_root,
    )
