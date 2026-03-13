"""
scripts/dl/step_dl_5_train_tcn.py

Step DL-5: TCN (Temporal Convolutional Network) 학습 스크립트

핵심 목표:
  - 현물 롱 온리 전략에 특화 → Precision(정밀도) 최우선 지표
  - LSTM 대비 추론 속도·Precision 비교
  - 1GB 저사양 서버 안정 작동 (CPU 전용, 메모리 최적화)

Early Stopping 기준:
  - Val Precision 최대화  (val_recall >= MIN_RECALL_FLOOR 조건 필수)
  - 조건 미충족 시 Val Loss 최소화로 폴백

실행:
    PYTHONPATH=. poetry run python scripts/dl/step_dl_5_train_tcn.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
import sys

import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.predictor.dl_dataset import (
    BATCH_SIZE,
    DATASET_PATH,
    SEQ_LEN,
    build_dataloaders,
)
from app.predictor.dl_model import (
    ARTIFACT_DIR,
    MODEL_PATH,
    MODEL_META_PATH,
    TCN_MODEL_PATH,
    TCN_MODEL_META_PATH,
    LSTMClassifier,
    TCNClassifier,
)

# ── 하이퍼파라미터 ──────────────────────────────────────────────────────────
N_CHANNELS = 64
KERNEL_SIZE = 3
DROPOUT = 0.2
LR = 2e-4
WEIGHT_DECAY = 1e-3
MAX_EPOCHS = 60
ES_PATIENCE = 12          # 조기 종료 인내심
LR_PATIENCE = 5
LR_FACTOR = 0.5
TRAIN_STRIDE = 2          # TCN 병렬 학습으로 stride 축소 (DL-3: 3 → DL-5: 2)
POS_WEIGHT_CAP = 1.0      # Precision 우선: positive 과도 업가중 억제
MIN_RECALL_FLOOR = 0.05   # 재현율 최소 보장 (0이면 trivial 해 방지)

# Threshold 탐색 범위
THRESHOLD_CANDIDATES = [round(t, 2) for t in np.arange(0.45, 0.76, 0.05)]

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
INFERENCE_RUNS = 500      # 추론 속도 비교 반복 수


# ── 유틸 ────────────────────────────────────────────────────────────────────

def sep(title: str) -> None:
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")


class FocalLoss(nn.Module):
    """Focal Loss — 어려운 샘플에 집중, 쉬운 샘플 가중치 감소."""

    def __init__(self, alpha: float = 1.0, gamma: float = 2.0, pos_weight=None):
        super().__init__()
        self.alpha = alpha
        self.gamma = gamma
        self.bce = nn.BCEWithLogitsLoss(pos_weight=pos_weight, reduction="none")

    def forward(self, inputs: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        bce = self.bce(inputs, targets)
        pt = torch.exp(-bce)
        return (self.alpha * (1 - pt) ** self.gamma * bce).mean()


def compute_pos_weight(y_train: np.ndarray) -> torch.Tensor:
    n_long = y_train.sum()
    n_flat = len(y_train) - n_long
    pw = min(n_flat / max(n_long, 1), POS_WEIGHT_CAP)
    print(f"  pos_weight={pw:.4f}  (raw={n_flat/max(n_long,1):.4f}, "
          f"cap={POS_WEIGHT_CAP})  FLAT {int(n_flat):,} / LONG {int(n_long):,}")
    return torch.tensor([pw], dtype=torch.float32, device=DEVICE)


# ── 학습 / 평가 ─────────────────────────────────────────────────────────────

def train_epoch(
    model: nn.Module,
    loader: torch.utils.data.DataLoader,
    optimizer: torch.optim.Optimizer,
    criterion: nn.Module,
) -> float:
    model.train()
    total = 0.0
    for xb, yb in loader:
        xb = xb.to(DEVICE)
        yb = yb.to(DEVICE).unsqueeze(1)
        optimizer.zero_grad()
        loss = criterion(model(xb), yb)
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        total += loss.item()
    return total / len(loader)


@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: torch.utils.data.DataLoader,
    criterion: nn.Module,
    threshold: float = 0.55,
) -> dict:
    model.eval()
    total_loss = 0.0
    all_probs, all_labels = [], []

    for xb, yb in loader:
        xb = xb.to(DEVICE)
        yb = yb.to(DEVICE).unsqueeze(1)
        logit = model(xb)
        total_loss += criterion(logit, yb).item()
        all_probs.append(torch.sigmoid(logit).squeeze(1).cpu().numpy())
        all_labels.append(yb.squeeze(1).cpu().numpy())

    probs = np.concatenate(all_probs)
    labels = np.concatenate(all_labels)
    preds = (probs >= threshold).astype(int)

    return {
        "loss": total_loss / len(loader),
        "auc": roc_auc_score(labels, probs),
        "precision": precision_score(labels, preds, zero_division=0),
        "recall": recall_score(labels, preds, zero_division=0),
        "f1": f1_score(labels, preds, zero_division=0),
        "acc": float((preds == labels).mean()),
        "probs": probs,
        "labels": labels,
        "preds": preds,
    }


# ── Threshold 탐색 ───────────────────────────────────────────────────────────

def threshold_sweep(probs: np.ndarray, labels: np.ndarray) -> dict:
    """Precision을 최대화하는 최적 threshold를 탐색 (recall >= MIN_RECALL_FLOOR 조건)."""
    best = {"threshold": 0.55, "precision": 0.0, "recall": 0.0, "f1": 0.0, "n_pred": 0}

    print(f"\n  {'Threshold':>10}  {'Precision':>10}  {'Recall':>8}  {'F1':>8}  {'N_LONG_pred':>12}")
    print("  " + "-" * 55)

    for thr in THRESHOLD_CANDIDATES:
        preds = (probs >= thr).astype(int)
        prec = precision_score(labels, preds, zero_division=0)
        rec = recall_score(labels, preds, zero_division=0)
        f1 = f1_score(labels, preds, zero_division=0)
        n_pred = int(preds.sum())
        marker = ""

        if rec >= MIN_RECALL_FLOOR and prec > best["precision"]:
            best = {"threshold": thr, "precision": prec, "recall": rec, "f1": f1, "n_pred": n_pred}
            marker = " ← best"

        print(f"  {thr:>10.2f}  {prec:>10.4f}  {rec:>8.4f}  {f1:>8.4f}  {n_pred:>12,}{marker}")

    return best


# ── 추론 속도 비교 ───────────────────────────────────────────────────────────

@torch.no_grad()
def benchmark_inference(
    tcn: TCNClassifier,
    n_features: int,
    runs: int = INFERENCE_RUNS,
) -> dict[str, float]:
    """TCN vs LSTM 단일 샘플 추론 속도 비교 (ms/inference)."""
    dummy = torch.randn(1, SEQ_LEN, n_features)

    def time_model(model: nn.Module) -> float:
        model.eval()
        # 워밍업
        for _ in range(20):
            model(dummy)
        t0 = time.perf_counter()
        for _ in range(runs):
            model(dummy)
        return (time.perf_counter() - t0) / runs * 1000  # ms

    tcn_ms = time_model(tcn)

    # LSTM 로드 (존재할 때만)
    lstm_ms: float | None = None
    if MODEL_PATH.exists() and MODEL_META_PATH.exists():
        try:
            lstm = LSTMClassifier.load(MODEL_PATH, MODEL_META_PATH, device="cpu")
            lstm_ms = time_model(lstm)
        except Exception as e:
            print(f"  [Bench] LSTM 로드 실패: {e}")

    return {"tcn_ms": tcn_ms, "lstm_ms": lstm_ms}


# ── 메인 ────────────────────────────────────────────────────────────────────

def main() -> None:
    sep("Step DL-5: TCN 학습 (Precision 최우선)")
    print(f"  디바이스: {DEVICE}")
    rf = TCNClassifier.receptive_field(KERNEL_SIZE)
    print(f"  TCN 수용 영역: {rf} timesteps ({rf/60:.2f}시간)")
    print(f"  Precision 우선 Early Stopping (MIN_RECALL_FLOOR={MIN_RECALL_FLOOR})")
    t_start = time.time()

    # ── 1. DataLoader ──────────────────────────────────────────────────────
    sep("1. DataLoader 준비")
    train_loader, val_loader, test_loader, meta = build_dataloaders(
        parquet_path=DATASET_PATH,
        seq_len=SEQ_LEN,
        batch_size=BATCH_SIZE,
        save_artifacts=False,
        train_stride=TRAIN_STRIDE,
    )
    n_features = meta["n_features"]
    print(f"  n_features={n_features}  SEQ_LEN={SEQ_LEN}  stride={TRAIN_STRIDE}")
    print(f"  Train {meta['train_samples']:,} / Val {meta['val_samples']:,} / Test {meta['test_samples']:,}")
    print(f"  Train LONG 비율: {meta['train_pos_ratio']:.3f}")

    # ── 2. 모델 초기화 ────────────────────────────────────────────────────
    sep("2. TCN 모델 초기화")
    model = TCNClassifier(
        n_features=n_features,
        n_channels=N_CHANNELS,
        kernel_size=KERNEL_SIZE,
        dropout=DROPOUT,
    ).to(DEVICE)
    print(f"  파라미터 수: {model.count_params():,}")
    print(f"  n_channels={N_CHANNELS}, kernel_size={KERNEL_SIZE}, "
          f"dropout={DROPOUT}, dilations={TCNClassifier.DILATIONS}")

    # LSTM과 파라미터 수 비교
    if MODEL_PATH.exists() and MODEL_META_PATH.exists():
        try:
            lstm_ref = LSTMClassifier.load(MODEL_PATH, MODEL_META_PATH)
            print(f"  [비교] LSTM 파라미터 수: {lstm_ref.count_params():,}")
        except Exception:
            pass

    # ── 3. 손실함수 / 옵티마이저 / 스케줄러 ──────────────────────────────
    sep("3. 학습 설정")
    y_train_all = np.array([y.numpy() for _, y in train_loader.dataset])
    pw = compute_pos_weight(y_train_all)

    criterion = FocalLoss(alpha=1.0, gamma=2.0, pos_weight=pw).to(DEVICE)
    optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", patience=LR_PATIENCE, factor=LR_FACTOR
    )
    print(f"  FocalLoss(gamma=2.0, pos_weight={pw.item():.4f})")
    print(f"  AdamW(lr={LR}, weight_decay={WEIGHT_DECAY})")
    print(f"  ReduceLROnPlateau(patience={LR_PATIENCE}, factor={LR_FACTOR})")
    print(f"  MAX_EPOCHS={MAX_EPOCHS}, ES_PATIENCE={ES_PATIENCE}")

    # ── 4. 학습 루프 ──────────────────────────────────────────────────────
    sep("4. 학습 시작  [조기종료 기준: Val Precision 최대 (recall≥{:.2f})]".format(MIN_RECALL_FLOOR))

    best_precision = 0.0
    best_loss = float("inf")
    best_epoch = 0
    es_counter = 0
    history: list[dict] = []

    print(f"{'Ep':>4} | {'TrLoss':>7} | {'VaLoss':>7} | {'VaAUC':>7} | "
          f"{'VaPrec':>7} | {'VaRec':>7} | {'VaF1':>7} | {'LR':>8} | {'s':>5}")
    print("-" * 80)

    for epoch in range(1, MAX_EPOCHS + 1):
        t_ep = time.time()

        tr_loss = train_epoch(model, train_loader, optimizer, criterion)
        vm = evaluate(model, val_loader, criterion, threshold=0.55)

        scheduler.step(vm["loss"])
        cur_lr = optimizer.param_groups[0]["lr"]
        elapsed = time.time() - t_ep

        history.append({
            "epoch": epoch, "train_loss": tr_loss,
            **{f"val_{k}": v for k, v in vm.items() if k not in ("probs", "labels", "preds")},
            "lr": cur_lr,
        })

        # Precision 기반 모델 저장 (recall 최소 조건 충족 시)
        improved = False
        if vm["recall"] >= MIN_RECALL_FLOOR and vm["precision"] > best_precision:
            best_precision = vm["precision"]
            best_epoch = epoch
            es_counter = 0
            model.save(TCN_MODEL_PATH, TCN_MODEL_META_PATH)
            improved = True
        elif vm["loss"] < best_loss and best_precision == 0.0:
            # Recall 조건을 한 번도 충족하지 못한 경우 Loss로 폴백
            best_loss = vm["loss"]
            best_epoch = epoch
            es_counter = 0
            model.save(TCN_MODEL_PATH, TCN_MODEL_META_PATH)
            improved = True
        else:
            es_counter += 1

        marker = " ★" if improved else ""
        print(
            f"{epoch:4d} | {tr_loss:7.4f} | {vm['loss']:7.4f} | {vm['auc']:7.4f} | "
            f"{vm['precision']:7.4f} | {vm['recall']:7.4f} | {vm['f1']:7.4f} | "
            f"{cur_lr:8.2e} | {elapsed:4.1f}s{marker}"
        )

        if es_counter >= ES_PATIENCE:
            print(f"\n⏹  Early Stopping — {ES_PATIENCE} epoch 개선 없음 (epoch={epoch})")
            break

    total_time = time.time() - t_start

    # ── 5. Best 모델 로드 → Test 평가 ────────────────────────────────────
    sep("5. Test 평가 (Best Model)")
    best_model = TCNClassifier.load(TCN_MODEL_PATH, TCN_MODEL_META_PATH, device=str(DEVICE))
    best_model = best_model.to(DEVICE)
    tm = evaluate(best_model, test_loader, criterion, threshold=0.55)

    print(f"  Best Epoch    : {best_epoch}")
    print(f"  Test Loss     : {tm['loss']:.4f}")
    print(f"  Test AUC-ROC  : {tm['auc']:.4f}")
    print(f"  Test Precision: {tm['precision']:.4f}  ← 핵심 지표")
    print(f"  Test Recall   : {tm['recall']:.4f}")
    print(f"  Test F1       : {tm['f1']:.4f}")
    print(f"  Test Accuracy : {tm['acc']:.4f}")

    # 혼동 행렬
    sep("6. 혼동 행렬 (Test, threshold=0.55)")
    cm = confusion_matrix(tm["labels"], tm["preds"])
    print(f"  실제\\예측    FLAT(0)  LONG(1)")
    print(f"  FLAT(0) :  {cm[0,0]:8,}  {cm[0,1]:8,}")
    print(f"  LONG(1) :  {cm[1,0]:8,}  {cm[1,1]:8,}")
    tn, fp, fn, tp = cm.ravel()
    print(f"\n  TP={tp:,}  FP={fp:,}  FN={fn:,}  TN={tn:,}")
    print(f"  LONG 예측 정밀도: {tp/(tp+fp+1e-9):.4f}  잘못된 진입 비율: {fp/(tp+fp+1e-9):.4f}")

    # ── 6. Threshold 탐색 ─────────────────────────────────────────────────
    sep("7. Threshold 탐색 (Test set)")
    best_thr = threshold_sweep(tm["probs"], tm["labels"])
    print(f"\n  최적 Threshold: {best_thr['threshold']:.2f}  "
          f"Precision={best_thr['precision']:.4f}  "
          f"Recall={best_thr['recall']:.4f}  "
          f"F1={best_thr['f1']:.4f}  "
          f"N_LONG={best_thr['n_pred']:,}")

    # 최적 threshold 재평가
    opt_thr = best_thr["threshold"]
    tm_opt = evaluate(best_model, test_loader, criterion, threshold=opt_thr)
    print(f"\n  [Threshold={opt_thr}] Precision={tm_opt['precision']:.4f}  "
          f"Recall={tm_opt['recall']:.4f}  F1={tm_opt['f1']:.4f}")

    # ── 7. 추론 속도 비교 ──────────────────────────────────────────────────
    sep("8. 추론 속도 비교 (CPU, 단일 샘플)")
    bench = benchmark_inference(best_model, n_features, runs=INFERENCE_RUNS)
    tcn_ms = bench["tcn_ms"]
    lstm_ms = bench["lstm_ms"]
    print(f"  TCN  추론 시간: {tcn_ms:.4f} ms/inference  ({INFERENCE_RUNS}회 평균)")
    if lstm_ms is not None:
        speedup = lstm_ms / tcn_ms
        print(f"  LSTM 추론 시간: {lstm_ms:.4f} ms/inference  ({INFERENCE_RUNS}회 평균)")
        print(f"  속도 비율: TCN이 LSTM 대비 {speedup:.2f}× {'빠름' if speedup>1 else '느림'}")
    else:
        print("  LSTM 모델 없음 — 단독 측정")

    # ── 8. 메타 저장 (보고서 자동 생성용) ───────────────────────────────────
    report_meta = {
        "best_epoch": best_epoch,
        "total_time_min": round(total_time / 60, 2),
        "test_loss": round(tm["loss"], 4),
        "test_auc": round(tm["auc"], 4),
        "test_precision_055": round(tm["precision"], 4),
        "test_recall_055": round(tm["recall"], 4),
        "test_f1_055": round(tm["f1"], 4),
        "test_acc_055": round(tm["acc"], 4),
        "best_threshold": opt_thr,
        "test_precision_opt": round(tm_opt["precision"], 4),
        "test_recall_opt": round(tm_opt["recall"], 4),
        "test_f1_opt": round(tm_opt["f1"], 4),
        "tcn_ms": round(tcn_ms, 4),
        "lstm_ms": round(lstm_ms, 4) if lstm_ms else None,
        "tcn_params": best_model.count_params(),
        "receptive_field": TCNClassifier.receptive_field(KERNEL_SIZE),
        "n_channels": N_CHANNELS,
        "kernel_size": KERNEL_SIZE,
        "dilations": TCNClassifier.DILATIONS,
        "pos_weight_cap": POS_WEIGHT_CAP,
        "train_stride": TRAIN_STRIDE,
    }
    meta_out = ARTIFACT_DIR / "tcn_train_result.json"
    with open(meta_out, "w") as f:
        json.dump(report_meta, f, indent=2, ensure_ascii=False)

    # ── 최종 요약 ─────────────────────────────────────────────────────────
    sep("Step DL-5 완료 요약")
    print(f"  학습 에포크      : {best_epoch}")
    print(f"  총 소요 시간     : {total_time/60:.1f}분")
    print(f"  Test Precision   : {tm['precision']:.4f}  (threshold=0.55)")
    print(f"  Test Precision★  : {tm_opt['precision']:.4f}  (threshold={opt_thr})")
    print(f"  Test AUC-ROC     : {tm['auc']:.4f}")
    print(f"  TCN 파라미터     : {best_model.count_params():,}")
    print(f"  TCN 수용 영역    : {TCNClassifier.receptive_field(KERNEL_SIZE)} timesteps")
    print(f"  TCN 추론 속도    : {tcn_ms:.4f} ms/inference")
    print(f"  모델 저장 경로   : {TCN_MODEL_PATH}")
    print(f"  메타 저장 경로   : {meta_out}")
    print("\n→ 다음 단계: step_dl_4_mock_trade.py에서 TCNClassifier로 교체하여 실시간 연동")


if __name__ == "__main__":
    main()
