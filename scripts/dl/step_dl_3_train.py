"""
Step DL-3: LSTM 모델 학습 스크립트 (GPU 최적화 & 체크포인트 재개 기능 추가)

실행:
    !PYTHONPATH=. python scripts/dl/step_dl_3_train.py
"""

import sys
import time
from pathlib import Path
import os # 추가

import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import (
    auc,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.predictor.dl_dataset import (
    BATCH_SIZE,
    DATASET_PATH,
    SEQ_LEN,
    build_dataloaders,
)
from app.predictor.dl_model import ARTIFACT_DIR, MODEL_META_PATH, MODEL_PATH, LSTMClassifier

# ── 하이퍼파라미터 ─────────────────────────────────────────────────────────
HIDDEN_SIZE = 64
NUM_LAYERS = 2
DROPOUT = 0.35
LR = 3e-4
WEIGHT_DECAY = 1e-3
MAX_EPOCHS = 50
ES_PATIENCE = 10
LR_PATIENCE = 4
LR_FACTOR = 0.5
TRAIN_STRIDE = 3
POS_WEIGHT_CAP = 1.5

# ⭐ 수정 포인트 1: 디바이스 자동 설정 (GPU가 있으면 cuda, 없으면 cpu)
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# ── 유틸리티 ──────────────────────────────────────────────────────────────

def sep(title: str) -> None:
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")


def compute_pos_weight(y_train: np.ndarray) -> torch.Tensor:
    """LONG 클래스 pos_weight = min(num_flat/num_long, POS_WEIGHT_CAP)."""
    n_long = y_train.sum()
    n_flat = len(y_train) - n_long
    pw_raw = n_flat / max(n_long, 1)
    pw = min(pw_raw, POS_WEIGHT_CAP)
    print(f"  pos_weight: {pw:.4f}  (raw={pw_raw:.4f}, FLAT {int(n_flat):,} / LONG {int(n_long):,})")
    
    # ⭐ 수정 포인트 2: pos_weight 텐서도 GPU로 생성
    return torch.tensor([pw], dtype=torch.float32, device=DEVICE)


# ── 학습 / 평가 함수 ───────────────────────────────────────────────────────

def train_epoch(
    model: nn.Module,
    loader: torch.utils.data.DataLoader,
    optimizer: torch.optim.Optimizer,
    criterion: nn.Module,
) -> float:
    model.train()
    total_loss = 0.0
    for x_batch, y_batch in loader:
        # ⭐ 수정 포인트 3: 데이터를 GPU 메모리로 이동
        x_batch = x_batch.to(DEVICE)
        y_batch = y_batch.to(DEVICE).unsqueeze(1)

        optimizer.zero_grad()
        logit = model(x_batch)
        loss = criterion(logit, y_batch)
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        total_loss += loss.item()

    return total_loss / len(loader)


@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: torch.utils.data.DataLoader,
    criterion: nn.Module,
) -> dict:
    model.eval()
    total_loss = 0.0
    all_logits: list[np.ndarray] = []
    all_labels: list[np.ndarray] = []

    for x_batch, y_batch in loader:
        # ⭐ 수정 포인트 4: 평가 데이터도 GPU 메모리로 이동
        x_batch = x_batch.to(DEVICE)
        y_batch = y_batch.to(DEVICE).unsqueeze(1)
        
        logit = model(x_batch)
        loss = criterion(logit, y_batch)
        total_loss += loss.item()

        probs = torch.sigmoid(logit).squeeze(1).cpu().numpy()
        labels = y_batch.squeeze(1).cpu().numpy()
        all_logits.append(probs)
        all_labels.append(labels)

    probs_all = np.concatenate(all_logits)
    labels_all = np.concatenate(all_labels)
    preds_all = (probs_all >= 0.5).astype(int)

    auc_roc = roc_auc_score(labels_all, probs_all)
    f1 = f1_score(labels_all, preds_all, zero_division=0)
    precision = precision_score(labels_all, preds_all, zero_division=0)
    recall = recall_score(labels_all, preds_all, zero_division=0)
    acc = (preds_all == labels_all).mean()

    return {
        "loss": total_loss / len(loader),
        "auc": auc_roc,
        "f1": f1,
        "precision": precision,
        "recall": recall,
        "acc": acc,
        "probs": probs_all,
        "labels": labels_all,
        "preds": preds_all,
    }


# ── 메인 학습 루프 ─────────────────────────────────────────────────────────

def main() -> None:
    sep("Step DL-3: LSTM 모델 설계 및 학습 (GPU 버전)")
    print(f"  사용 디바이스: {DEVICE}") # GPU가 정상적으로 잡히는지 출력
    t_start = time.time()

    # ── 1. DataLoader 로드 ────────────────────────────────────────────────
    sep("1. DataLoader 준비")
    train_loader, val_loader, test_loader, meta = build_dataloaders(
        parquet_path=DATASET_PATH,
        seq_len=SEQ_LEN,
        batch_size=BATCH_SIZE,
        save_artifacts=False,
        train_stride=TRAIN_STRIDE,
    )
    print(f"  Train stride: {TRAIN_STRIDE} (매 {TRAIN_STRIDE}분 1샘플 → CPU 학습 최적화)")
    n_features = meta["n_features"]
    print(f"  n_features: {n_features}")
    print(f"  Train 샘플: {meta['train_samples']:,}")
    print(f"  Val   샘플: {meta['val_samples']:,}")
    print(f"  Test  샘플: {meta['test_samples']:,}")
    print(f"  Train LONG 비율: {meta['train_pos_ratio']:.3f}")

    # ── 2. 모델 초기화 (또는 로드) ────────────────────────────────────────────────────
    sep("2. 모델 초기화 및 체크포인트 확인")
    
    # ⭐ 수정 포인트 5: 기존 학습된 모델 파일(.pt)이 있다면 불러와서 이어서 학습
    if os.path.exists(MODEL_PATH) and os.path.exists(MODEL_META_PATH):
        print(f"  기존 모델 체크포인트 발견! 이어서 학습을 준비합니다: {MODEL_PATH}")
        model = LSTMClassifier.load(MODEL_PATH, MODEL_META_PATH, device=DEVICE)
        model = model.to(DEVICE) 
    else:
        print("  새로운 모델을 초기화합니다.")
        model = LSTMClassifier(
            n_features=n_features,
            hidden_size=HIDDEN_SIZE,
            num_layers=NUM_LAYERS,
            dropout=DROPOUT,
        ).to(DEVICE)
        
    print(f"  파라미터 수: {model.count_params():,}")

    # ── 3. 손실함수 / 옵티마이저 / 스케줄러 ──────────────────────────────
    sep("3. 훈련 설정")

    y_train_all = np.array([y.numpy() for _, y in train_loader.dataset])
    pw = compute_pos_weight(y_train_all)

    # ⭐ 수정 포인트 6: loss 함수를 GPU로 할당
    criterion = nn.BCEWithLogitsLoss(pos_weight=pw).to(DEVICE)
    optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", patience=LR_PATIENCE, factor=LR_FACTOR
    )

    print(f"  Loss: BCEWithLogitsLoss (pos_weight={pw.item():.4f})")
    print(f"  Optimizer: AdamW (lr={LR}, weight_decay={WEIGHT_DECAY})")
    print(f"  Scheduler: ReduceLROnPlateau (patience={LR_PATIENCE}, factor={LR_FACTOR})")
    print(f"  Max Epochs: {MAX_EPOCHS}, Early Stopping patience: {ES_PATIENCE}")

    # ── 4. 학습 루프 ──────────────────────────────────────────────────────
    sep("4. 학습 시작")

    best_val_loss = float("inf")
    best_val_auc = 0.0
    best_epoch = 0
    es_counter = 0
    history: list[dict] = []

    print(f"{'Epoch':>5} | {'TrLoss':>7} | {'VaLoss':>7} | {'VaAUC':>6} | "
          f"{'VaPrec':>7} | {'VaRec':>6} | {'VaF1':>6} | {'LR':>8} | {'Time':>6}")
    print("-" * 75)

    for epoch in range(1, MAX_EPOCHS + 1):
        t_ep = time.time()

        train_loss = train_epoch(model, train_loader, optimizer, criterion)
        val_metrics = evaluate(model, val_loader, criterion)
        val_loss = val_metrics["loss"]

        scheduler.step(val_loss)
        current_lr = optimizer.param_groups[0]["lr"]
        elapsed = time.time() - t_ep

        history.append({
            "epoch": epoch,
            "train_loss": train_loss,
            **{f"val_{k}": v for k, v in val_metrics.items() if k not in ("probs", "labels", "preds")},
            "lr": current_lr,
        })

        print(
            f"{epoch:5d} | {train_loss:7.4f} | {val_loss:7.4f} | "
            f"{val_metrics['auc']:6.4f} | {val_metrics['precision']:7.4f} | "
            f"{val_metrics['recall']:6.4f} | {val_metrics['f1']:6.4f} | "
            f"{current_lr:8.2e} | {elapsed:5.1f}s"
        )

        # Best model 저장 (Val Loss 기준)
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_val_auc = val_metrics["auc"]
            best_epoch = epoch
            es_counter = 0
            model.save(MODEL_PATH, MODEL_META_PATH)
        else:
            es_counter += 1

        if es_counter >= ES_PATIENCE:
            print(f"\n⏹  Early Stopping: {ES_PATIENCE} epoch 동안 Val Loss 개선 없음 (epoch={epoch})")
            break

    total_time = time.time() - t_start

    # ── 5. 최종 Test 평가 ─────────────────────────────────────────────────
    sep("5. Test 평가 (Best Model 로드)")
    best_model = LSTMClassifier.load(MODEL_PATH, MODEL_META_PATH, device=DEVICE)
    best_model = best_model.to(DEVICE)
    test_metrics = evaluate(best_model, test_loader, criterion)

    print(f"  Best Epoch   : {best_epoch}")
    print(f"  Test Loss    : {test_metrics['loss']:.4f}")
    print(f"  Test AUC-ROC : {test_metrics['auc']:.4f}")
    print(f"  Test F1      : {test_metrics['f1']:.4f}")
    print(f"  Test Precision: {test_metrics['precision']:.4f}")
    print(f"  Test Recall  : {test_metrics['recall']:.4f}")
    print(f"  Test Accuracy: {test_metrics['acc']:.4f}")

    # 혼동 행렬
    sep("6. 혼동 행렬 (Test)")
    cm = confusion_matrix(test_metrics["labels"], test_metrics["preds"])
    print(f"  실제\\예측    FLAT(0)  LONG(1)")
    print(f"  FLAT(0):    {cm[0,0]:7,}  {cm[0,1]:7,}")
    print(f"  LONG(1):    {cm[1,0]:7,}  {cm[1,1]:7,}")
    tn, fp, fn, tp = cm.ravel()
    print(f"\n  TN={tn:,}  FP={fp:,}  FN={fn:,}  TP={tp:,}")
    print(f"  LONG 예측 정밀도: {tp/(tp+fp+1e-9):.4f}  (잘못된 진입 비율: {fp/(tp+fp+1e-9):.4f})")

    # 최종 요약
    sep("Step DL-3 완료 요약")
    print(f"  학습 에포크    : {best_epoch} (Early Stop: {'YES' if es_counter >= ES_PATIENCE else 'NO'})")
    print(f"  총 소요 시간   : {total_time/60:.1f}분")
    print(f"  Best Val Loss  : {best_val_loss:.4f}")
    print(f"  Best Val AUC   : {best_val_auc:.4f}")
    print(f"  Test AUC-ROC   : {test_metrics['auc']:.4f}")
    print(f"  Test Precision : {test_metrics['precision']:.4f}")
    print(f"  Test F1        : {test_metrics['f1']:.4f}")
    print(f"  모델 저장 경로 : {MODEL_PATH}")
    print("\n→ 다음 단계: Step DL-4 (실시간 모의투자 연동)")


if __name__ == "__main__":
    main()