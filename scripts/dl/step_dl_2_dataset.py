"""
Step DL-2: DataLoader 파이프라인 검증 스크립트

실행:
    poetry run python scripts/dl/step_dl_2_dataset.py
"""

import sys
from pathlib import Path

import numpy as np
import torch

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.predictor.dl_dataset import (
    BATCH_SIZE,
    DATASET_PATH,
    FEATURE_COLS_PATH,
    SCALER_PATH,
    SEQ_LEN,
    build_dataloaders,
    load_and_split,
)


def sep(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main() -> None:
    sep("Step DL-2: DataLoader 파이프라인 검증")

    # ── 1. 분할 검증 ─────────────────────────────────────────────────────
    sep("1. 시간순 분할 확인")
    train_df, val_df, test_df, feature_cols = load_and_split(DATASET_PATH)

    total = len(train_df) + len(val_df) + len(test_df)
    print(f"전체 유효 행: {total:,}")
    print(f"  Train : {len(train_df):,} ({len(train_df)/total*100:.1f}%)  "
          f"{train_df.index[0]} ~ {train_df.index[-1]}")
    print(f"  Val   : {len(val_df):,} ({len(val_df)/total*100:.1f}%)  "
          f"{val_df.index[0]} ~ {val_df.index[-1]}")
    print(f"  Test  : {len(test_df):,} ({len(test_df)/total*100:.1f}%)  "
          f"{test_df.index[0]} ~ {test_df.index[-1]}")
    print(f"피처 수: {len(feature_cols)}")

    # ── 2. 시간 순서 무결성 검증 ─────────────────────────────────────────
    sep("2. 시간 순서 무결성 검증")
    assert train_df.index[-1] < val_df.index[0], "❌ Train/Val 시간 겹침!"
    assert val_df.index[-1] < test_df.index[0], "❌ Val/Test 시간 겹침!"
    print("✅ Train → Val → Test 시간 순서 확인 완료 (겹침 없음)")
    print(f"  Train 끝: {train_df.index[-1]}")
    print(f"  Val   시작: {val_df.index[0]}")
    print(f"  Val   끝: {val_df.index[-1]}")
    print(f"  Test  시작: {test_df.index[0]}")

    # ── 3. DataLoader 전체 빌드 ───────────────────────────────────────────
    sep("3. DataLoader 빌드")
    train_loader, val_loader, test_loader, meta = build_dataloaders(
        parquet_path=DATASET_PATH,
        seq_len=SEQ_LEN,
        batch_size=BATCH_SIZE,
        save_artifacts=True,
    )
    print(f"  seq_len   : {meta['seq_len']}")
    print(f"  batch_size: {meta['batch_size']}")
    print(f"  n_features: {meta['n_features']}")
    print(f"  Train 윈도우 수: {meta['train_samples']:,}")
    print(f"  Val   윈도우 수: {meta['val_samples']:,}")
    print(f"  Test  윈도우 수: {meta['test_samples']:,}")
    print(f"  Train LONG 비율: {meta['train_pos_ratio']:.3f}")
    print(f"  Val   LONG 비율: {meta['val_pos_ratio']:.3f}")
    print(f"  Test  LONG 비율: {meta['test_pos_ratio']:.3f}")

    # ── 4. 첫 번째 배치 Shape 검증 ───────────────────────────────────────
    sep("4. 첫 번째 배치 Shape 검증")
    x_batch, y_batch = next(iter(train_loader))
    print(f"  x_batch shape : {tuple(x_batch.shape)}  (Batch, SeqLen, Features)")
    print(f"  y_batch shape : {tuple(y_batch.shape)}  (Batch,)")
    print(f"  x_batch dtype : {x_batch.dtype}")
    print(f"  y_batch dtype : {y_batch.dtype}")
    assert x_batch.shape[1] == SEQ_LEN, f"SeqLen 불일치: {x_batch.shape[1]} != {SEQ_LEN}"
    assert x_batch.shape[2] == meta["n_features"], f"Feature 수 불일치"
    print("✅ 배치 Shape 검증 완료")

    # ── 5. 스케일러 Data Leakage 검증 ────────────────────────────────────
    sep("5. 스케일러 Data Leakage 검증")
    import joblib
    scaler = joblib.load(SCALER_PATH)

    # 훈련 세트 스케일링 후 중앙값 ~0 인지 확인 (RobustScaler 특성)
    from app.predictor.dl_dataset import build_arrays
    X_train, y_train = build_arrays(train_df, feature_cols, scaler)
    X_val, y_val = build_arrays(val_df, feature_cols, scaler)
    X_test, y_test = build_arrays(test_df, feature_cols, scaler)

    train_median = np.median(X_train, axis=0).mean()
    val_median = np.median(X_val, axis=0).mean()
    test_median = np.median(X_test, axis=0).mean()
    print(f"  Train 피처 중앙값 평균: {train_median:.4f}  (≈0 정상)")
    print(f"  Val   피처 중앙값 평균: {val_median:.4f}")
    print(f"  Test  피처 중앙값 평균: {test_median:.4f}")

    # Train IQR 분포 확인
    p25 = np.percentile(X_train, 25, axis=0).mean()
    p75 = np.percentile(X_train, 75, axis=0).mean()
    print(f"  Train IQR 평균 — Q25: {p25:.4f}, Q75: {p75:.4f}")
    print(f"  스케일러 저장 경로: {SCALER_PATH}")
    print(f"  피처 목록 저장: {FEATURE_COLS_PATH}")
    print("✅ 스케일러 Data Leakage 검증 완료")

    # ── 6. Val/Test DataLoader 첫 배치 ───────────────────────────────────
    sep("6. Val / Test DataLoader 첫 배치")
    xv, yv = next(iter(val_loader))
    xt, yt = next(iter(test_loader))
    print(f"  Val  x_batch: {tuple(xv.shape)}, y_batch: {tuple(yv.shape)}")
    print(f"  Test x_batch: {tuple(xt.shape)}, y_batch: {tuple(yt.shape)}")

    # ── 최종 요약 ─────────────────────────────────────────────────────────
    sep("Step DL-2 완료 요약")
    print(f"  데이터셋 Tensor Shape  : (N, {SEQ_LEN}, {meta['n_features']})")
    print(f"  Train / Val / Test 샘플: {meta['train_samples']:,} / {meta['val_samples']:,} / {meta['test_samples']:,}")
    print(f"  아티팩트 저장 위치     : artifacts/dl_prod/")
    print(f"    - scaler.joblib")
    print(f"    - feature_cols.json")
    print("\n→ 다음 단계: Step DL-3 (LSTM 모델 설계 및 학습)")


if __name__ == "__main__":
    main()
