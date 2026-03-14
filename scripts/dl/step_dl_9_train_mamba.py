"""
Step DL-9: CryptoMambaClassifier GPU 학습 스크립트  [v3 — 15분 타겟 이진 분류]

개요
──────────────────────────────────────────────────────────────────────────────
  이 스크립트는 1분봉 60개(1시간 시퀀스)를 입력받아
  15분 뒤의 가격 추세 방향(LONG / SHORT·FLAT)을 이진 분류하는 모델을 학습합니다.

학습 설정
──────────────────────────────────────────────────────────────────────────────
  데이터셋   : btc_1m_hft_v2.parquet  (67+피처 + future_ret_15 타겟)
  예측 타겟  : future_ret_15 — 15분 뒤 종가 기준 로그수익률
  라벨 변환  : future_ret_15 > 0 → 1 (LONG), ≤ 0 → 0 (SHORT/FLAT)
               ※ 분할 후 각 split 독립 처리 (Data Leakage 완전 차단)
  모델       : CryptoMambaClassifier  (DWT + Selective SSM + KAN Mixer)
  손실함수   : BCEWithLogitsLoss  (pos_weight = n_neg / max(n_pos, 1), 동적 계산)
  옵티마이저 : AdamW (lr=3e-4, wd=1e-4) + clip_grad_norm(1.0)
  스케줄러   : CosineAnnealingLR (T_max = n_epochs)
  EarlyStopping: Val BCE Loss 기준, patience=15
  평가 지표  : Confusion Matrix / Precision / Recall / F1 / Balanced Accuracy
  저장 경로  : artifacts/dl_prod/cryptomamba_model.pt

변경 이력
──────────────────────────────────────────────────────────────────────────────
  v3 (2026-03-14)
    - 타임프레임 피벗 완료: 전체 타겟을 future_ret_15로 일원화
    - future_ret_15 즉석 계산 코드 제거 (데이터셋에 컬럼 확정 포함)
    - 평가 지표 전면 교체: BCE+Acc → Confusion Matrix + Precision/Recall/F1/BalAcc
    - best_state 직접 추적 (best_metric 변수) → 저장 순서 버그 수정
    - pos_weight = n_neg/max(n_pos,1) 동적 계산 (하드코딩 제거)
    - Data Leakage 원천 차단: 분할 후 ffill→train_median fill (bfill 제거)
    - LayerNorm(1) 상수화 버그 수정, dt_proj.bias=2.0 복구 (dl_model.py)

  v2 (2026-03-13)
    - TARGET_COL: future_ret_1 → future_ret_15 (노이즈 감소, 추세 신호 강화)
    - 손실함수: GMADLoss → BCEWithLogitsLoss

실행 방법
──────────────────────────────────────────────────────────────────────────────
  # 로컬 (CPU)
  poetry run python scripts/dl/step_dl_9_train_mamba.py

  # Colab (GPU) — 데이터 업로드 후
  !python scripts/dl/step_dl_9_train_mamba.py --batch_size 1024 --epochs 100

  # 빠른 스모크 테스트 (로직 검증용, 3 epochs, 15K행)
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
# 예측 타겟: 15분 뒤 로그수익률 방향 (future_ret_15 > 0 → LONG)
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
    """1분봉 60개 슬라이딩 윈도우 → 15분 추세 방향 이진 라벨 반환.

    타겟: future_ret_15 > 0 → 1 (LONG), ≤ 0 → 0 (SHORT/FLAT)

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
    """btc_1m_hft_v2.parquet → Train/Val/Test DataLoader + 메타 딕셔너리.

    15분 타겟(future_ret_15) 이진 분류용 파이프라인:
      1. 시간순 분할 (누수 방지)
      2. ffill → train_median fillna (bfill 금지)
      3. Raw target > 0 기준 이진 라벨링 (각 split 독립)
      4. RobustScaler: Train only fit

    Returns:
        (train_loader, val_loader, test_loader, meta)
        meta keys: n_features, feature_cols, split_sizes,
                   long_ratio, n_pos, n_neg
    """
    print(f"[Data] 15분 타겟 분류 데이터 로드: {parquet_path.name}")
    df = pd.read_parquet(parquet_path)

    # future_ret_15는 데이터셋에 확정 포함됨 — 컬럼 부재 시 즉시 에러
    if TARGET_COL not in df.columns:
        raise KeyError(
            f"'{TARGET_COL}' 컬럼이 데이터셋에 없습니다. "
            f"데이터 파이프라인(step_dl_*_collect.py)을 통해 {TARGET_COL}이 "
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
    print(f"  피처 수: {len(feat_cols)}  |  예측 타겟: {TARGET_COL} (15분 이진 분류)")

    # 결측치 처리: ffill 먼저(시간 연속성 유지), 나머지는 Train median으로 채움
    # bfill은 미래 정보를 역방향으로 끌어오므로 사용 금지
    train_df[feat_cols] = train_df[feat_cols].ffill()
    val_df[feat_cols]   = val_df[feat_cols].ffill()
    test_df[feat_cols]  = test_df[feat_cols].ffill()

    train_median = train_df[feat_cols].median()
    train_df[feat_cols] = train_df[feat_cols].fillna(train_median)
    val_df[feat_cols]   = val_df[feat_cols].fillna(train_median)
    test_df[feat_cols]  = test_df[feat_cols].fillna(train_median)

    # Raw target 기준 이진 분류 라벨링: 원본 future_ret_15 값 > 0 여부로 각 split 독립 처리
    # (Z-score 정규화 후 판단 방식 불사용 → 라벨 의미 보존)
    y_tr = (train_df[TARGET_COL].values > 0).astype(np.float32)
    y_vl = (val_df[TARGET_COL].values > 0).astype(np.float32)
    y_te = (test_df[TARGET_COL].values > 0).astype(np.float32)

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

    # pos_weight 계산용: DataLoader가 실제 사용하는 샘플(seq_len 이후) 기준 n_pos/n_neg
    valid_y_tr = y_tr[seq_len:]
    n_pos = int(valid_y_tr.sum())
    n_neg = int(len(valid_y_tr) - n_pos)

    def pos_ratio(y_arr: np.ndarray) -> float:
        return float(y_arr[seq_len:].mean()) if len(y_arr) > seq_len else float(y_arr.mean())

    long_ratio_tr = pos_ratio(y_tr)
    print(f"  15분 LONG 비율  Train={long_ratio_tr:.3f} "
          f" Val={pos_ratio(y_vl):.3f}  Test={pos_ratio(y_te):.3f}")
    print(f"  Train 15분 LONG={n_pos:,}  SHORT/FLAT={n_neg:,}")

    meta = {
        "n_features":    len(feat_cols),
        "feature_cols":  feat_cols,
        "seq_len":       seq_len,
        "batch_size":    batch_size,
        "train_samples": len(train_ds),
        "val_samples":   len(val_ds),
        "test_samples":  len(test_ds),
        "long_ratio":    long_ratio_tr,
        # [Fix 5] 실제 학습 타겟 기준 pos/neg 샘플 수
        "n_pos":         n_pos,
        "n_neg":         n_neg,
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
    """[Fix 7] 평가 루프 — 분류 문제 맞춤형 지표 계산.

    이진 분류에 특화된 지표를 계산합니다:
      - BCE Loss
      - Confusion Matrix (TP/TN/FP/FN)
      - Precision, Recall, F1 Score
      - Balanced Accuracy: 클래스 불균형 상태에서 모델 지능을 정확히 파악
        = (Sensitivity + Specificity) / 2
        = 클래스별 recall의 평균 (단순 accuracy와 달리 불균형에 강인)

    Args:
        criterion: BCEWithLogitsLoss 인스턴스
    """
    model.eval()
    total_bce = 0.0
    all_pred: list[torch.Tensor] = []
    all_true: list[torch.Tensor] = []

    for x_batch, y_batch in loader:
        x_batch = x_batch.to(device, non_blocking=True)
        y_batch = y_batch.to(device, non_blocking=True)

        logit = model(x_batch).squeeze(-1)          # (B,) — raw logit
        total_bce += criterion(logit, y_batch).item() * len(y_batch)

        pred_label = (torch.sigmoid(logit) > 0.5).float()
        all_pred.append(pred_label.cpu())
        all_true.append(y_batch.cpu())

    pred_arr = torch.cat(all_pred).numpy()
    true_arr = torch.cat(all_true).numpy()
    total_samples = len(true_arr)

    # ── Confusion Matrix ──────────────────────────────────────────────
    tp = int(((pred_arr == 1) & (true_arr == 1)).sum())
    tn = int(((pred_arr == 0) & (true_arr == 0)).sum())
    fp = int(((pred_arr == 1) & (true_arr == 0)).sum())
    fn = int(((pred_arr == 0) & (true_arr == 1)).sum())

    # ── 분류 지표 ─────────────────────────────────────────────────────
    precision    = tp / max(tp + fp, 1)
    recall       = tp / max(tp + fn, 1)          # = Sensitivity
    f1           = 2 * precision * recall / max(precision + recall, 1e-8)
    specificity  = tn / max(tn + fp, 1)
    balanced_acc = (recall + specificity) / 2    # 클래스 불균형에 강인한 지표
    dir_acc      = (tp + tn) / max(total_samples, 1)

    return {
        "bce":          total_bce / max(total_samples, 1),
        "precision":    precision,
        "recall":       recall,
        "f1":           f1,
        "balanced_acc": balanced_acc,
        "dir_acc":      dir_acc,
        "n_valid":      total_samples,
        "confusion":    {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
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
    """15분 추세 이진 분류 — 전체 학습 파이프라인 실행."""

    global _PROJ, DATASET_PATH, MODEL_PATH, META_PATH
    if project_root is not None:
        _PROJ = Path(project_root)
        DATASET_PATH = _PROJ / "data" / "datasets" / "btc_1m_hft_v2.parquet"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\n{'═'*62}")
    print(f"  Step DL-9: CryptoMamba  15분 방향 분류 학습")
    print(f"  타겟: {TARGET_COL} > 0 → LONG(1) / ≤ 0 → SHORT·FLAT(0)")
    print(f"{'═'*62}")
    print(f"  디바이스  : {device}")
    print(f"  배치 크기 : {batch_size}  |  에폭 : {n_epochs}  |  patience : {patience}")
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

    # pos_weight 동적 계산: Train의 실제 15분 LONG/SHORT 비율 기반 (n_neg / n_pos)
    n_pos = meta["n_pos"]
    n_neg = meta["n_neg"]
    pos_weight_val = n_neg / max(n_pos, 1)
    pos_weight_tensor = torch.tensor([pos_weight_val], dtype=torch.float32, device=device)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight_tensor)
    print(f"  BCEWithLogitsLoss  pos_weight={pos_weight_val:.4f}  "
          f"(15분 LONG={n_pos:,}  SHORT/FLAT={n_neg:,}  비율={long_ratio:.3f})")

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
    # [Fix 6] early_stop.best_epoch 의존 방식 폐기 → 직접 best_metric 추적
    best_metric = float("inf")
    t_start = time.time()

    print(f"\n  {'에폭':>5s}  {'Train BCE':>12s}  {'Val BCE':>10s}  "
          f"{'Val F1':>8s}  {'BalAcc':>8s}  {'LR':>10s}  {'시간(s)':>7s}")
    print(f"  {'─'*75}")

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

        # [Fix 6] 평가 수치가 갱신될 때만 best_state 저장
        # early_stop 호출 전에 직접 best_metric을 비교하여 올바른 에폭의 가중치 보존
        if val_metrics["bce"] < best_metric:
            best_metric = val_metrics["bce"]
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

        # ── LR 스케줄러 업데이트 ──────────────────────────────────────
        if scheduler_type == "cosine":
            scheduler.step()
        else:
            scheduler.step(val_metrics["bce"])

        current_lr = optimizer.param_groups[0]["lr"]
        ep_time    = time.time() - t_ep

        row = {
            "epoch":         epoch,
            "train_bce":     train_bce,
            "val_bce":       val_metrics["bce"],
            "val_f1":        val_metrics["f1"],
            "val_balanced_acc": val_metrics["balanced_acc"],
            "val_precision": val_metrics["precision"],
            "val_recall":    val_metrics["recall"],
            "lr":            current_lr,
        }
        history.append(row)

        print(f"  {epoch:>5d}  {train_bce:>12.6f}  {val_metrics['bce']:>10.6f}  "
              f"{val_metrics['f1']:>8.4f}  {val_metrics['balanced_acc']:>8.4f}  "
              f"{current_lr:>10.2e}  {ep_time:>7.1f}")

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
    print("  최종 Test 평가 (best 모델) — 분류 지표")
    print(f"{'─'*62}")
    test_metrics = evaluate(model, test_loader, criterion, device)
    cm = test_metrics["confusion"]
    print(f"  Test BCE Loss    : {test_metrics['bce']:.6f}")
    print(f"  Precision        : {test_metrics['precision']*100:.2f}%")
    print(f"  Recall           : {test_metrics['recall']*100:.2f}%")
    print(f"  F1 Score         : {test_metrics['f1']:.4f}")
    print(f"  Balanced Accuracy: {test_metrics['balanced_acc']*100:.2f}%  ← 불균형 강인 지표")
    print(f"  일반 정확도      : {test_metrics['dir_acc']*100:.2f}%  (전체 {test_metrics['n_valid']:,}샘플)")
    print(f"  Confusion Matrix → TP={cm['tp']:,}  TN={cm['tn']:,}  FP={cm['fp']:,}  FN={cm['fn']:,}")

    # ── 학습 로그 저장 ─────────────────────────────────────────────────
    log = {
        "model_meta": {
            "n_features": n_features, "d_model": d_model, "n_low": n_low,
            "n_high": n_high, "d_conv": d_conv, "dropout": dropout,
        },
        "train_config": {
            "lr": lr, "weight_decay": weight_decay, "batch_size": batch_size,
            "n_epochs_run": len(history), "long_ratio": long_ratio,
            "pos_weight": pos_weight_val, "n_pos": n_pos, "n_neg": n_neg,
            "max_grad_norm": max_grad_norm, "patience": patience,
            "target_col": TARGET_COL,
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

    # ── 분류 상세 분석 ──────────────────────────────────────────────────
    _print_classification_analysis(model, test_loader, device)

    print(f"\n{'═'*62}")
    print("  Step DL-9 완료")
    print(f"{'═'*62}\n")

    return log


def _print_classification_analysis(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
) -> None:
    """[Fix 7] 분류 문제 맞춤형 상세 분석.

    출력:
      - Confusion Matrix (전체 + 신뢰도 구간별)
      - Precision / Recall / F1 / Balanced Accuracy
      - 예측 확률(sigmoid) 분포
    """
    model.eval()
    all_prob: list[torch.Tensor] = []
    all_true: list[torch.Tensor] = []

    with torch.no_grad():
        for x_batch, y_batch in loader:
            logit = model(x_batch.to(device)).squeeze(-1)
            prob  = torch.sigmoid(logit).cpu()
            all_prob.append(prob)
            all_true.append(y_batch)

    prob_arr = torch.cat(all_prob).numpy()
    true_arr = torch.cat(all_true).numpy()
    pred_arr = (prob_arr > 0.5).astype(float)

    # ── 전체 분류 지표 ────────────────────────────────────────────────
    tp = int(((pred_arr == 1) & (true_arr == 1)).sum())
    tn = int(((pred_arr == 0) & (true_arr == 0)).sum())
    fp = int(((pred_arr == 1) & (true_arr == 0)).sum())
    fn = int(((pred_arr == 0) & (true_arr == 1)).sum())

    precision    = tp / max(tp + fp, 1)
    recall       = tp / max(tp + fn, 1)
    f1           = 2 * precision * recall / max(precision + recall, 1e-8)
    specificity  = tn / max(tn + fp, 1)
    balanced_acc = (recall + specificity) / 2

    print(f"\n{'─'*62}")
    print("  [Fix 7] 분류 상세 분석 (Test, 이진 분류)")
    print(f"{'─'*62}")
    print(f"  Confusion Matrix:")
    print(f"    TP(LONG→LONG)   = {tp:>8,}    FN(LONG→SHORT) = {fn:>8,}")
    print(f"    FP(SHORT→LONG)  = {fp:>8,}    TN(SHORT→SHORT)= {tn:>8,}")
    print(f"\n  분류 지표:")
    print(f"    Precision        : {precision*100:.2f}%  (예측 LONG 중 실제 LONG 비율)")
    print(f"    Recall(Sensitivity): {recall*100:.2f}%  (실제 LONG 중 맞힌 비율)")
    print(f"    Specificity      : {specificity*100:.2f}%  (실제 SHORT 중 맞힌 비율)")
    print(f"    F1 Score         : {f1:.4f}")
    print(f"    Balanced Accuracy: {balanced_acc*100:.2f}%  ← 클래스 불균형 강인 지표")
    print(f"    일반 정확도      : {(tp+tn)/len(true_arr)*100:.2f}%")

    # ── 신뢰도 구간별 정밀 분석 ──────────────────────────────────────
    print(f"\n{'─'*62}")
    print("  신뢰도 구간별 Precision / Recall")
    print(f"{'─'*62}")
    print(f"  {'구간':>28s}  {'샘플':>7s}  {'Prec':>7s}  {'Rec':>7s}  {'F1':>7s}")
    print(f"  {'─'*58}")

    bands = [
        ("전체                      ", 0.00, 1.00),
        ("LONG 고확신  (prob > 0.65) ", 0.65, 1.00),
        ("LONG 중확신  (0.55~0.65)   ", 0.55, 0.65),
        ("불확실 구간  (0.45~0.55)   ", 0.45, 0.55),
        ("SHORT 중확신 (0.35~0.45)   ", 0.35, 0.45),
        ("SHORT 고확신 (prob < 0.35) ", 0.00, 0.35),
    ]

    for label, lo, hi in bands:
        mask = (prob_arr >= lo) & (prob_arr < hi)
        n = int(mask.sum())
        if n == 0:
            continue
        p_bin = (prob_arr[mask] > 0.5).astype(float)
        t_bin = true_arr[mask]
        b_tp = int(((p_bin == 1) & (t_bin == 1)).sum())
        b_fp = int(((p_bin == 1) & (t_bin == 0)).sum())
        b_fn = int(((p_bin == 0) & (t_bin == 1)).sum())
        b_prec = b_tp / max(b_tp + b_fp, 1)
        b_rec  = b_tp / max(b_tp + b_fn, 1)
        b_f1   = 2 * b_prec * b_rec / max(b_prec + b_rec, 1e-8)
        print(f"  {label}  {n:>7,}  {b_prec*100:>6.1f}%  {b_rec*100:>6.1f}%  {b_f1:>7.4f}")

    # ── 예측 확률 분포 ────────────────────────────────────────────────
    print(f"\n  예측 확률(sigmoid) 분포:")
    print(f"    mean={prob_arr.mean():.4f}  std={prob_arr.std():.4f}")
    print(f"    min={prob_arr.min():.4f}  max={prob_arr.max():.4f}")
    print(f"    LONG 예측 비율 (prob > 0.5): {(prob_arr > 0.5).mean()*100:.1f}%")
    print(f"    실제 LONG 비율             : {true_arr.mean()*100:.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# 4. CLI 진입점
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Step DL-9: CryptoMambaClassifier 학습 — 1분봉 60개로 15분 뒤 추세 방향 이진 분류"
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
