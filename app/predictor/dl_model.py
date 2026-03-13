"""
app/predictor/dl_model.py

PyTorch LSTM 분류 모델 (KRW-BTC 1분봉 → 60분 LONG/FLAT 이진 분류)

Architecture:
    Input  : (Batch, SeqLen=60, Features=52)
    LSTM   : 2-layer stacked, hidden_size=64, dropout=0.2
    Head   : LayerNorm → Linear(64→32) → GELU → Dropout → Linear(32→1)
    Output : (Batch, 1) — logit (BCEWithLogitsLoss 사용)
"""

from __future__ import annotations

import json
from pathlib import Path

import torch
import torch.nn as nn

_PROJ_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = _PROJ_ROOT / "artifacts" / "dl_prod"
MODEL_PATH = ARTIFACT_DIR / "lstm_model.pt"
MODEL_META_PATH = ARTIFACT_DIR / "dl_model_meta.json"


class LSTMClassifier(nn.Module):
    """Stacked LSTM binary classifier.

    Args:
        n_features : 입력 피처 수 (default 52)
        hidden_size: LSTM 은닉 크기 (default 64)
        num_layers : LSTM 레이어 수 (default 2)
        dropout    : LSTM 내부 + 헤드 드롭아웃 (default 0.2)
    """

    def __init__(
        self,
        n_features: int = 52,
        hidden_size: int = 64,
        num_layers: int = 2,
        dropout: float = 0.2,
    ):
        super().__init__()
        self.n_features = n_features
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        # LSTM: dropout은 레이어 간에만 적용 (num_layers>1 필요)
        lstm_dropout = dropout if num_layers > 1 else 0.0
        self.lstm = nn.LSTM(
            input_size=n_features,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,       # (Batch, Seq, Feature)
            dropout=lstm_dropout,
            bidirectional=False,
        )

        # 분류 헤드: 마지막 시점 hidden state → logit
        self.head = nn.Sequential(
            nn.LayerNorm(hidden_size),
            nn.Linear(hidden_size, 32),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(32, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: (Batch, SeqLen, Features)
        Returns:
            logit: (Batch, 1)
        """
        # LSTM forward — 마지막 hidden state 사용
        out, _ = self.lstm(x)          # out: (Batch, SeqLen, hidden_size)
        last_hidden = out[:, -1, :]    # (Batch, hidden_size)
        logit = self.head(last_hidden) # (Batch, 1)
        return logit

    # ── 저장 / 로드 헬퍼 ──────────────────────────────────────────────────

    def save(
        self,
        model_path: Path = MODEL_PATH,
        meta_path: Path = MODEL_META_PATH,
    ) -> None:
        """가중치 + 메타 저장."""
        torch.save(self.state_dict(), model_path)
        meta = {
            "n_features": self.n_features,
            "hidden_size": self.hidden_size,
            "num_layers": self.num_layers,
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

    @classmethod
    def load(
        cls,
        model_path: Path = MODEL_PATH,
        meta_path: Path = MODEL_META_PATH,
        device: str = "cpu",
    ) -> "LSTMClassifier":
        """저장된 가중치로 모델 복원."""
        with open(meta_path) as f:
            meta = json.load(f)
        model = cls(**meta)
        model.load_state_dict(torch.load(model_path, map_location=device))
        model.eval()
        return model

    def count_params(self) -> int:
        return sum(p.numel() for p in self.parameters() if p.requires_grad)
