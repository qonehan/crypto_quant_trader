"""
app/predictor/dl_model.py

PyTorch 이진 분류 모델 모음 (KRW-BTC 1분봉 → 60분 LONG/FLAT)

Models:
    LSTMClassifier  — 1D-CNN + Stacked LSTM  (Step DL-3)
    TCNClassifier   — Temporal Convolutional Network (Step DL-5)

TCN 수용 영역:
    dilations=[1,2,4,8,16,32], kernel_size=3, 2 conv/block
    RF = 1 + 2*(k-1)*sum(dilations) = 1 + 4*63 = 253 timesteps (≈4.2시간)
"""

from __future__ import annotations

import json
from pathlib import Path

import torch
import torch.nn as nn

_PROJ_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = _PROJ_ROOT / "artifacts" / "dl_prod"

# ── LSTM 경로 ────────────────────────────────────────────────────────────────
MODEL_PATH = ARTIFACT_DIR / "lstm_model.pt"
MODEL_META_PATH = ARTIFACT_DIR / "dl_model_meta.json"

# ── TCN 경로 ─────────────────────────────────────────────────────────────────
TCN_MODEL_PATH = ARTIFACT_DIR / "tcn_model.pt"
TCN_MODEL_META_PATH = ARTIFACT_DIR / "tcn_model_meta.json"


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


# ══════════════════════════════════════════════════════════════════════════════
# TCN — Temporal Convolutional Network
# ══════════════════════════════════════════════════════════════════════════════

class _Chomp1d(nn.Module):
    """인과적 패딩의 오른쪽(미래) 부분을 제거해 causal convolution을 완성."""

    def __init__(self, chomp_size: int) -> None:
        super().__init__()
        self.chomp_size = chomp_size

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x[:, :, : -self.chomp_size].contiguous()


class _TemporalBlock(nn.Module):
    """TCN의 기본 빌딩 블록.

    구조:
        [WeightNorm-Conv1d → Chomp → ReLU → Dropout] × 2
        + Residual Connection (채널 수 불일치 시 1×1 Conv 다운샘플)
    """

    def __init__(
        self,
        n_inputs: int,
        n_outputs: int,
        kernel_size: int,
        dilation: int,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        pad = (kernel_size - 1) * dilation  # causal padding

        self.net = nn.Sequential(
            nn.utils.weight_norm(
                nn.Conv1d(n_inputs, n_outputs, kernel_size, padding=pad, dilation=dilation)
            ),
            _Chomp1d(pad),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.utils.weight_norm(
                nn.Conv1d(n_outputs, n_outputs, kernel_size, padding=pad, dilation=dilation)
            ),
            _Chomp1d(pad),
            nn.ReLU(),
            nn.Dropout(dropout),
        )
        self.downsample = (
            nn.Conv1d(n_inputs, n_outputs, kernel_size=1)
            if n_inputs != n_outputs
            else None
        )
        self.relu = nn.ReLU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        residual = x if self.downsample is None else self.downsample(x)
        return self.relu(self.net(x) + residual)


class TCNClassifier(nn.Module):
    """Temporal Convolutional Network 이진 분류기.

    Args:
        n_features  : 입력 피처 수 (default 52)
        n_channels  : 각 블록의 채널 수 (default 64)
        kernel_size : 커널 크기 (default 3)
        dropout     : 드롭아웃 (default 0.2)

    Architecture:
        Input  : (Batch, SeqLen=60, Features=52)
        TCN    : 6× _TemporalBlock, dilations=[1,2,4,8,16,32]
        Head   : last_timestep → Linear(64→32) → ReLU → Dropout → Linear(32→1)
        Output : (Batch, 1) — logit

    Receptive Field:
        RF = 1 + 2*(kernel_size-1)*sum(dilations)
           = 1 + 2*2*63 = 253 timesteps ≈ 4.2시간
    """

    DILATIONS: list[int] = [1, 2, 4, 8, 16, 32]

    def __init__(
        self,
        n_features: int,
        n_channels: int = 64,
        kernel_size: int = 3,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        self.n_features = n_features
        self.n_channels = n_channels
        self.kernel_size = kernel_size
        self.dropout = dropout

        blocks: list[nn.Module] = []
        for i, d in enumerate(self.DILATIONS):
            in_ch = n_features if i == 0 else n_channels
            blocks.append(_TemporalBlock(in_ch, n_channels, kernel_size, d, dropout))
        self.tcn = nn.Sequential(*blocks)

        self.head = nn.Sequential(
            nn.Linear(n_channels, n_channels // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(n_channels // 2, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, T, F) → Conv1d expects (B, F, T)
        x = x.transpose(1, 2)          # (B, F, T)
        x = self.tcn(x)                 # (B, n_channels, T)
        x = x[:, :, -1]                 # 마지막 타임스텝: (B, n_channels)
        return self.head(x)             # (B, 1)

    # ── 저장 / 로드 ────────────────────────────────────────────────────────

    def save(
        self,
        model_path: Path = TCN_MODEL_PATH,
        meta_path: Path = TCN_MODEL_META_PATH,
    ) -> None:
        torch.save(self.state_dict(), model_path)
        meta = {
            "n_features": self.n_features,
            "n_channels": self.n_channels,
            "kernel_size": self.kernel_size,
            "dropout": self.dropout,
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

    @classmethod
    def load(
        cls,
        model_path: Path = TCN_MODEL_PATH,
        meta_path: Path = TCN_MODEL_META_PATH,
        device: str = "cpu",
    ) -> "TCNClassifier":
        with open(meta_path) as f:
            meta = json.load(f)
        model = cls(**meta)
        model.load_state_dict(torch.load(model_path, map_location=device, weights_only=True))
        model.eval()
        return model

    def count_params(self) -> int:
        return sum(p.numel() for p in self.parameters() if p.requires_grad)

    @staticmethod
    def receptive_field(kernel_size: int = 3, dilations: list[int] | None = None) -> int:
        """이론적 수용 영역 계산 (2 conv/block 기준)."""
        if dilations is None:
            dilations = [1, 2, 4, 8, 16, 32]
        return 1 + 2 * (kernel_size - 1) * sum(dilations)
