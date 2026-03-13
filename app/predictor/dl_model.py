"""
app/predictor/dl_model.py

PyTorch 시계열 모델 모음 (KRW-BTC 1분봉)

Models:
    LSTMClassifier        — 1D-CNN + Stacked LSTM             (Step DL-3)
    TCNClassifier         — Temporal Convolutional Network     (Step DL-5)
    CryptoMambaClassifier — DWT + Selective SSM + KAN Mixer   (Step DL-8)

CryptoMamba 아키텍처 개요:
    [HaarDWT1D]  → low (B, T//2, F) + high (B, T//2, F)
    [InputProj]  → F → d_model 투영
    [MambaBlock × N] → 선택적 SSM (Selectivity: 입력 의존 망각률)
    [KANLayer × 2]   → EfficientKAN 근사 (선형 + SiLU 기저)
    Output: (B, 1) — 1분 로그수익률 예측 / GMADLoss 회귀 타겟
"""

from __future__ import annotations

import json
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F

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


# ══════════════════════════════════════════════════════════════════════════════
# Step DL-8: CryptoMambaClassifier — DWT + Selective SSM + KAN Mixer
# ══════════════════════════════════════════════════════════════════════════════

CRYPTOMAMBA_MODEL_PATH = ARTIFACT_DIR / "cryptomamba_model.pt"
CRYPTOMAMBA_META_PATH  = ARTIFACT_DIR / "cryptomamba_model_meta.json"


# ── A. 입력 분해 계층: Haar DWT ───────────────────────────────────────────────

class _HaarDWT1D(nn.Module):
    """고정 1D Haar Wavelet Transform — 추세/노이즈 물리적 분리.

    Haar 필터 (비학습, 고정):
        low-pass  [+1, +1] / √2  → 인접 두 봉의 평균 = 추세 성분
        high-pass [+1, -1] / √2  → 인접 두 봉의 차이 = 노이즈/디테일 성분

    Input:  (B, T, F)
    Output: low (B, T//2, F) + high (B, T//2, F)

    파라미터 수: 0 (비학습)
    복잡도: O(T·F)
    """

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        T = x.shape[1]
        if T % 2 != 0:                                    # 홀수 T 패딩
            x = F.pad(x, (0, 0, 0, 1))
        x0, x1 = x[:, 0::2, :], x[:, 1::2, :]           # 짝수/홀수 인덱스
        k = 0.7071067811865476                             # 1/√2
        low  = (x0 + x1) * k                              # (B, T//2, F) — 추세
        high = (x0 - x1) * k                              # (B, T//2, F) — 노이즈
        return low, high


# ── B. SSM 백본: Selective Scan (Mamba-proxy) ─────────────────────────────────

class _SelectiveScan(nn.Module):
    """입력 의존적 망각률을 가진 선택적 상태 공간 스캔 (Mamba 선택성 근사).

    핵심 수식 (이산화된 SSM):
        dt_t = sigmoid(W_dt @ x_t + b_dt)   ← 입력 의존 망각률 (Selectivity)
        h_t  = dt_t ⊙ h_{t-1} + (1 - dt_t) ⊙ x_t   ← 상태 갱신 방정식

    동작 원리:
        dt_t → 1  : 과거 상태 완전 유지 (횡보 구간 — 새 입력 무시)
        dt_t → 0  : 과거 상태 망각 + 새 입력으로 완전 대체 (돌파 이벤트 포착)
        dt_t ∈ (0,1): 혼합 (대부분의 1분봉)

    초기화:
        b_dt = +2.0 → sigmoid(2) ≈ 0.88 : 학습 초반 높은 기억 유지 → 안정적 수렴

    복잡도: O(T·D) — T=30 (DWT 후) 에서 순차 스캔 실용적
    """

    def __init__(self, d_model: int) -> None:
        super().__init__()
        self.dt_proj = nn.Linear(d_model, d_model)
        nn.init.constant_(self.dt_proj.bias, 2.0)   # 초기 높은 기억 유지

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, T, D = x.shape
        dt = torch.sigmoid(self.dt_proj(x))          # (B, T, D) — 입력 의존 망각률
        h  = x.new_zeros(B, D)                       # 초기 은닉 상태
        hs: list[torch.Tensor] = []
        for t in range(T):
            h = dt[:, t] * h + (1.0 - dt[:, t]) * x[:, t]
            hs.append(h)
        return torch.stack(hs, dim=1)                # (B, T, D)


class _CryptoMambaBlock(nn.Module):
    """Mamba-proxy 기본 블록.

    구조:
        PreNorm(LayerNorm)
            ↓  in_proj (d→2d: value + gate)
        [value stream]               [gate stream]
            ↓ DepthwiseCausalConv         ↓
            ↓ SiLU                        ↓
            ↓ SelectiveScan               ↓
            └──────── ⊗ sigmoid(gate) ───┘  (Mamba의 z-gate 승수)
            ↓ out_proj (d→d)
            ↓ Dropout
            + Residual

    Args:
        d_model : 입력/출력 차원
        d_conv  : 인과 컨볼루션 커널 크기 (default 4)
        dropout : 드롭아웃 (default 0.1)
    """

    def __init__(self, d_model: int, d_conv: int = 4, dropout: float = 0.1) -> None:
        super().__init__()
        self.norm     = nn.LayerNorm(d_model)
        self.in_proj  = nn.Linear(d_model, d_model * 2)          # value + gate
        self.dconv    = nn.Conv1d(                                 # 인과 depthwise conv
            d_model, d_model, d_conv,
            padding=d_conv - 1,
            groups=d_model,                                        # depthwise
        )
        self.ssm      = _SelectiveScan(d_model)
        self.out_proj = nn.Linear(d_model, d_model)
        self.drop     = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, T, D)
        B, T, D = x.shape
        residual = x

        x = self.norm(x)
        xz = self.in_proj(x)                                       # (B, T, D*2)
        val, gate = xz.chunk(2, dim=-1)                            # each (B, T, D)

        # 인과 Depthwise Conv (미래 정보 유출 차단)
        v = self.dconv(val.transpose(1, 2))[:, :, :T]             # (B, D, T)
        v = F.silu(v.transpose(1, 2))                              # (B, T, D)

        # 선택적 상태 공간 스캔
        h = self.ssm(v)                                            # (B, T, D)

        # 출력 게이트 (Mamba z-gate)
        y = h * torch.sigmoid(gate)                                # (B, T, D)
        y = self.drop(self.out_proj(y))
        return y + residual                                        # Residual


# ── C. 비선형 매핑: KAN Mixer ─────────────────────────────────────────────────

class _KANLayer(nn.Module):
    """KAN 레이어 근사 (EfficientKAN 공식 기반).

    원본 KAN 수식: y_j = Σ_i φ_{ij}(x_i)  where φ is a B-spline function

    EfficientKAN 근사:
        y = W_base × silu(x) + W_spline × spline_basis(x)
          ≈ W_base × x  +  W_spline × SiLU(x)          (spline_basis ≈ SiLU)

    - W_base × x      : 선형 기저 성분 (MLP fallback)
    - W_spline × SiLU(x): 학습된 비선형 기저 (B-spline 근사)
    - LayerNorm: 학습 안정화
    - 파라미터: 2 × (in × out) — MLP 대비 2× 이나 해석 가능성 향상

    Args:
        in_features  : 입력 차원
        out_features : 출력 차원
        dropout      : 드롭아웃 (default 0.0)
    """

    def __init__(
        self,
        in_features: int,
        out_features: int,
        dropout: float = 0.0,
    ) -> None:
        super().__init__()
        self.w_base   = nn.Linear(in_features, out_features)       # 선형 기저
        self.w_spline = nn.Linear(in_features, out_features, bias=False)  # 비선형 기저
        self.norm     = nn.LayerNorm(out_features)
        self.drop     = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # KAN 근사: 선형 + SiLU 기저 합산
        y = self.w_base(x) + self.w_spline(F.silu(x))
        return self.drop(self.norm(y))


# ── D. 전체 모델: CryptoMambaClassifier ──────────────────────────────────────

class CryptoMambaClassifier(nn.Module):
    """DWT + Selective SSM (Mamba-proxy) + KAN Mixer 하이브리드 모델.

    설계 목표:
        - 1분봉 HFT 환경에서 변동성 돌파 구간(|ret|>0.1%)의 방향성 예측
        - GMADLoss 회귀 학습 타겟: future_ret_1 (log-return)
        - 파라미터 < 200,000 (경량 추론)
        - Sub-millisecond 추론 (T=60 → DWT 후 T=30)

    아키텍처:
        Input (B, T=60, F=71)
              ↓
        [_HaarDWT1D]  — 추세/노이즈 물리적 분리
              ↓                      ↓
        low (B, 30, 71)       high (B, 30, 71)
              ↓                      ↓
        [low_proj: F→d]       [high_proj: F→d]
              ↓                      ↓
        [MambaBlock×n_low]    [MambaBlock×n_high]
              ↓ last_step            ↓ last_step
              └──── concat(B, 2d) ────┘
                          ↓
                   [_KANLayer: 2d→d]
                          ↓
                   [_KANLayer: d→1]
                          ↓
                    Output (B, 1)  — 예측 log-return

    Args:
        n_features: 입력 피처 수 (default 71, v2 데이터셋)
        d_model   : 내부 은닉 차원 (default 64)
        n_low     : 저주파(추세) 스트림 Mamba 블록 수 (default 2)
        n_high    : 고주파(노이즈) 스트림 Mamba 블록 수 (default 1)
        d_conv    : 인과 컨볼루션 커널 크기 (default 4)
        dropout   : 드롭아웃 (default 0.10)

    Outputs:
        (B, 1) — 예측 로그수익률 (GMADLoss 회귀 / sigmoid 이진 분류 겸용)

    학습:
        손실: GMADLoss(tau=std(future_ret_1), gamma=500)
        옵티마이저: AdamW + clip_grad_norm(max_norm=1.0)
        데이터셋: btc_1m_hft_v2.parquet (71피처, 2년)
    """

    def __init__(
        self,
        n_features: int = 71,
        d_model: int = 64,
        n_low: int = 2,
        n_high: int = 1,
        d_conv: int = 4,
        dropout: float = 0.10,
    ) -> None:
        super().__init__()
        self.n_features = n_features
        self.d_model    = d_model
        self.n_low      = n_low
        self.n_high     = n_high
        self.d_conv     = d_conv
        self.dropout    = dropout

        # ── A. DWT 분해 (비학습) ─────────────────────────────────────────────
        self.dwt = _HaarDWT1D()

        # ── B. 스트림별 입력 투영 ─────────────────────────────────────────────
        self.low_proj  = nn.Linear(n_features, d_model)
        self.high_proj = nn.Linear(n_features, d_model)

        # ── C. Mamba 블록 스택 ───────────────────────────────────────────────
        # 저주파 스트림: 추세 포착 (더 깊음)
        self.low_stack  = nn.Sequential(*[
            _CryptoMambaBlock(d_model, d_conv, dropout) for _ in range(n_low)
        ])
        # 고주파 스트림: 노이즈/단기 패턴 포착 (얕음)
        self.high_stack = nn.Sequential(*[
            _CryptoMambaBlock(d_model, d_conv, dropout) for _ in range(n_high)
        ])

        # ── D. KAN Mixer 헤드 ────────────────────────────────────────────────
        self.kan1 = _KANLayer(d_model * 2, d_model, dropout)
        self.kan2 = _KANLayer(d_model, 1, dropout=0.0)

        self._init_weights()

    def _init_weights(self) -> None:
        """Xavier 균일 초기화 (Linear) + Kaiming (Conv1d)."""
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
        # SelectiveScan dt_proj bias는 _SelectiveScan.__init__에서 +2.0으로 설정됨

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: (B, T, F) — 정규화된 피처 시퀀스 (RobustScaler 적용 후)

        Returns:
            (B, 1) — 예측 로그수익률 (GMADLoss 회귀 타겟)
                      추론 시: output > threshold → LONG, output ≤ threshold → FLAT
        """
        # ① DWT 분해: 추세/노이즈 분리
        low, high = self.dwt(x)           # each (B, T//2, F)

        # ② 입력 투영: F → d_model
        low  = self.low_proj(low)         # (B, T//2, d_model)
        high = self.high_proj(high)       # (B, T//2, d_model)

        # ③ Mamba 스택: 선택적 SSM으로 장기/단기 패턴 포착
        low  = self.low_stack(low)        # (B, T//2, d_model)
        high = self.high_stack(high)      # (B, T//2, d_model)

        # ④ 마지막 타임스텝 추출 (인과적 예측)
        low_feat  = low[:, -1, :]         # (B, d_model)
        high_feat = high[:, -1, :]        # (B, d_model)

        # ⑤ 추세 + 노이즈 특징 융합
        feat = torch.cat([low_feat, high_feat], dim=-1)  # (B, 2·d_model)

        # ⑥ KAN Mixer: 비선형 매핑 → 예측값
        out = self.kan1(feat)             # (B, d_model)
        out = self.kan2(out)              # (B, 1)

        return out

    # ── 저장 / 로드 ──────────────────────────────────────────────────────────

    def save(
        self,
        model_path: Path = CRYPTOMAMBA_MODEL_PATH,
        meta_path: Path = CRYPTOMAMBA_META_PATH,
    ) -> None:
        """가중치 + 메타데이터 저장."""
        model_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(self.state_dict(), model_path)
        meta = {
            "n_features": self.n_features,
            "d_model":    self.d_model,
            "n_low":      self.n_low,
            "n_high":     self.n_high,
            "d_conv":     self.d_conv,
            "dropout":    self.dropout,
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

    @classmethod
    def load(
        cls,
        model_path: Path = CRYPTOMAMBA_MODEL_PATH,
        meta_path: Path = CRYPTOMAMBA_META_PATH,
        device: str = "cpu",
    ) -> "CryptoMambaClassifier":
        """저장된 가중치로 모델 복원."""
        with open(meta_path) as f:
            meta = json.load(f)
        model = cls(**meta)
        model.load_state_dict(
            torch.load(model_path, map_location=device, weights_only=True)
        )
        model.eval()
        return model

    def count_params(self) -> int:
        return sum(p.numel() for p in self.parameters() if p.requires_grad)
