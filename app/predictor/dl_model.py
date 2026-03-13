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

    def __init__(self, n_features: int, hidden_size: int, num_layers: int, dropout: float = 0.3):
        super().__init__()
        self.n_features = n_features
        self.hidden_size = hidden_size
        
        self.num_layers = num_layers
        self.dropout = dropout

        # 🌟 성능 향상 핵심 1: 1D-CNN (단기 캔들 패턴 추출)
        self.cnn = nn.Sequential(
            nn.Conv1d(in_channels=n_features, out_channels=hidden_size, kernel_size=3, padding=1),
            nn.BatchNorm1d(hidden_size),
            nn.ReLU(),
            nn.Dropout(dropout)
        )
        
        # 장기 추세 파악용 LSTM
        self.lstm = nn.LSTM(
            input_size=hidden_size,  # CNN의 출력 크기를 받음
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0
        )
        
        # 최종 결정 (분류기)
        self.fc = nn.Sequential(
            nn.Linear(hidden_size, hidden_size // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, 1)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x shape: (Batch, SeqLen, Features)
        
        # CNN은 (Batch, Channels, Length) 형태를 원하므로 차원 변경
        x = x.transpose(1, 2) 
        x = self.cnn(x)
        x = x.transpose(1, 2) # 다시 LSTM이 원하는 (Batch, SeqLen, Features)로 복구
        
        out, _ = self.lstm(x)
        
        # 시퀀스의 마지막 타임스텝 데이터만 사용하여 최종 예측
        last_out = out[:, -1, :]
        return self.fc(last_out)

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
