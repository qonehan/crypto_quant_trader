"""
app/predictor/dl_dataset.py

CryptoTimeSeriesDataset 및 DataLoader 팩토리.

설계 원칙:
- 시간순 분할 (Train 70% / Val 15% / Test 15%) — 무작위 섞기 금지
- RobustScaler를 Train에만 fit → Val/Test는 transform만 수행 (Data Leakage 방지)
- 타겟 컬럼(target, future_ret)은 스케일링 제외
- Sliding Window: (Sequence=60, Features=N) → (Batch, 60, N)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.preprocessing import RobustScaler
from torch.utils.data import DataLoader, Dataset

# ── 경로 상수 ──────────────────────────────────────────────────────────────
_PROJ_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = _PROJ_ROOT / "artifacts" / "dl_prod"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

SCALER_PATH = ARTIFACT_DIR / "scaler.joblib"
FEATURE_COLS_PATH = ARTIFACT_DIR / "feature_cols.json"
DATASET_PATH = _PROJ_ROOT / "data" / "datasets" / "btc_1m_dl.parquet"

# ── 상수 ──────────────────────────────────────────────────────────────────
SEQ_LEN = 60          # 슬라이딩 윈도우 길이 (1시간 = 60분봉)
BATCH_SIZE = 512      # 기본 배치 크기
TARGET_COL = "target"
EXCLUDE_COLS = {"target", "future_ret"}  # 스케일링 및 피처에서 제외

TRAIN_RATIO = 0.70
VAL_RATIO = 0.15
# TEST_RATIO = 1 - TRAIN_RATIO - VAL_RATIO = 0.15


# ── Dataset 클래스 ─────────────────────────────────────────────────────────

class CryptoTimeSeriesDataset(Dataset):
    """슬라이딩 윈도우 방식 시계열 Dataset.

    Args:
        X: np.ndarray, shape (T, F) — 정규화된 피처 시퀀스
        y: np.ndarray, shape (T,)  — 이진 타겟 (0/1)
        seq_len: 윈도우 크기
        stride: 윈도우 간격 (1=매분, 3=3분마다 → 데이터 1/3로 감소)
    """

    def __init__(
        self,
        X: np.ndarray,
        y: np.ndarray,
        seq_len: int = SEQ_LEN,
        stride: int = 1,
    ):
        assert len(X) == len(y), "X와 y의 길이가 다릅니다."
        assert len(X) > seq_len, f"데이터 길이({len(X)})가 seq_len({seq_len})보다 커야 합니다."

        self.X = torch.tensor(X, dtype=torch.float32)
        self.y = torch.tensor(y, dtype=torch.float32)
        self.seq_len = seq_len
        self.stride = max(1, stride)
        # 유효 시작 인덱스 사전 계산
        self._indices = list(range(0, len(X) - seq_len, self.stride))

    def __len__(self) -> int:
        return len(self._indices)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        start = self._indices[idx]
        x_window = self.X[start : start + self.seq_len]   # (seq_len, F)
        y_label = self.y[start + self.seq_len]             # scalar
        return x_window, y_label


# ── 데이터 준비 함수 ───────────────────────────────────────────────────────

def load_and_split(
    parquet_path: Path = DATASET_PATH,
    train_ratio: float = TRAIN_RATIO,
    val_ratio: float = VAL_RATIO,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    """parquet 로드 → NaN 처리 → 시간순 분할.

    Returns:
        (train_df, val_df, test_df, feature_cols)
    """
    df = pd.read_parquet(parquet_path)

    # target NaN 행 제거 (마지막 horizon행)
    df = df.dropna(subset=[TARGET_COL])

    # 피처 컬럼 결정 (OHLCV 절대값 포함, 가격 절대값은 dist/ratio로 대체 — 모두 포함)
    feature_cols = [c for c in df.columns if c not in EXCLUDE_COLS]

    # NaN 처리: ffill → bfill (앞에서 생긴 rolling NaN 제거)
    df[feature_cols] = df[feature_cols].ffill().bfill()

    # 시간순 분할 (shuffle 금지)
    n = len(df)
    n_train = int(n * train_ratio)
    n_val = int(n * val_ratio)

    train_df = df.iloc[:n_train].copy()
    val_df = df.iloc[n_train : n_train + n_val].copy()
    test_df = df.iloc[n_train + n_val :].copy()

    return train_df, val_df, test_df, feature_cols


def fit_scaler(
    train_df: pd.DataFrame,
    feature_cols: list[str],
    scaler_path: Path = SCALER_PATH,
) -> RobustScaler:
    """Train 데이터에만 RobustScaler fit 후 저장."""
    scaler = RobustScaler()
    scaler.fit(train_df[feature_cols].values)
    joblib.dump(scaler, scaler_path)
    return scaler


def load_scaler(scaler_path: Path = SCALER_PATH) -> RobustScaler:
    return joblib.load(scaler_path)


def build_arrays(
    df: pd.DataFrame,
    feature_cols: list[str],
    scaler: RobustScaler,
) -> tuple[np.ndarray, np.ndarray]:
    """DataFrame → 스케일링된 (X, y) numpy 배열."""
    X = scaler.transform(df[feature_cols].values).astype(np.float32)
    y = df[TARGET_COL].values.astype(np.float32)
    return X, y


# ── DataLoader 팩토리 ──────────────────────────────────────────────────────

def build_dataloaders(
    parquet_path: Path = DATASET_PATH,
    seq_len: int = SEQ_LEN,
    batch_size: int = BATCH_SIZE,
    num_workers: int = 0,
    save_artifacts: bool = True,
    train_stride: int = 1,
) -> tuple[DataLoader, DataLoader, DataLoader, dict]:
    """전체 파이프라인 실행 후 (train_loader, val_loader, test_loader, meta) 반환.

    meta dict:
        n_features, seq_len, batch_size,
        train_samples, val_samples, test_samples,
        train_pos_ratio, val_pos_ratio, test_pos_ratio
    """
    # 1. 로드 & 분할
    train_df, val_df, test_df, feature_cols = load_and_split(parquet_path)

    # 2. 스케일러 fit (Train only)
    scaler = fit_scaler(train_df, feature_cols, SCALER_PATH)

    # 3. 배열 변환
    X_train, y_train = build_arrays(train_df, feature_cols, scaler)
    X_val, y_val = build_arrays(val_df, feature_cols, scaler)
    X_test, y_test = build_arrays(test_df, feature_cols, scaler)

    # 4. Dataset 생성
    # Val/Test는 stride=1 (전체 평가), Train은 train_stride로 속도 조절 가능
    train_ds = CryptoTimeSeriesDataset(X_train, y_train, seq_len, stride=train_stride)
    val_ds = CryptoTimeSeriesDataset(X_val, y_val, seq_len, stride=1)
    test_ds = CryptoTimeSeriesDataset(X_test, y_test, seq_len, stride=1)

    # 5. DataLoader 생성
    # Train: shuffle=True (윈도우 단위 셔플, 시퀀스 내부 순서는 유지)
    train_loader = DataLoader(
        train_ds, batch_size=batch_size, shuffle=True, num_workers=num_workers
    )
    val_loader = DataLoader(
        val_ds, batch_size=batch_size, shuffle=False, num_workers=num_workers
    )
    test_loader = DataLoader(
        test_ds, batch_size=batch_size, shuffle=False, num_workers=num_workers
    )

    # 6. 아티팩트 저장
    if save_artifacts:
        with open(FEATURE_COLS_PATH, "w") as f:
            json.dump(feature_cols, f, ensure_ascii=False, indent=2)

    n_features = len(feature_cols)
    meta = {
        "n_features": n_features,
        "seq_len": seq_len,
        "batch_size": batch_size,
        "feature_cols": feature_cols,
        # Dataset 샘플 수 (윈도우 기준)
        "train_samples": len(train_ds),
        "val_samples": len(val_ds),
        "test_samples": len(test_ds),
        # 원시 행 수 (분할 기준)
        "train_rows": len(train_df),
        "val_rows": len(val_df),
        "test_rows": len(test_df),
        # 클래스 비율
        "train_pos_ratio": float(y_train.mean()),
        "val_pos_ratio": float(y_val.mean()),
        "test_pos_ratio": float(y_test.mean()),
    }
    return train_loader, val_loader, test_loader, meta


# ── 실시간 추론용 피처 변환 ────────────────────────────────────────────────

def transform_realtime(
    df_window: pd.DataFrame,
    feature_cols: Optional[list[str]] = None,
    scaler: Optional[RobustScaler] = None,
) -> torch.Tensor:
    """실시간 1개 윈도우(60행 DataFrame) → 추론용 텐서 (1, seq_len, F)."""
    if feature_cols is None:
        with open(FEATURE_COLS_PATH) as f:
            feature_cols = json.load(f)
    if scaler is None:
        scaler = load_scaler()

    arr = scaler.transform(df_window[feature_cols].values).astype(np.float32)
    tensor = torch.tensor(arr, dtype=torch.float32).unsqueeze(0)  # (1, 60, F)
    return tensor
