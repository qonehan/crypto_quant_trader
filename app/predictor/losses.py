"""
app/predictor/losses.py

GMADL — Generalized Mean Absolute Directional Loss (Step DL-7)

설계 원칙
─────────────────────────────────────────────────────────────────────────────
1. 방향성 페널티: 예측 부호와 실제 부호가 엇갈릴 때 magnitude에 비례한 큰 페널티
2. Magnitude 가중치: |y|^β × exp(γ|y|) — 변동성 돌파 구간에 최적화 역량 집중
3. 미분 가능성: tanh 기반 방향 근사 + SmoothL1 결합 → AdamW backprop NaN 없음

수식
─────────────────────────────────────────────────────────────────────────────

  GMADL(ŷ, y) = (1/N) Σ wᵢ · [α · L_dir(ŷᵢ, yᵢ) + (1-α) · L_smooth(ŷᵢ, yᵢ)]

  wᵢ = |yᵢ|^β · exp(min(γ·|yᵢ|, 20))       — magnitude + exp 평활화 가중치

  L_dir(ŷ, y) = 1 - tanh(ŷ·y / τ²)  ∈ [0, 2]
                  → 0   : 방향 완전 일치 + 큰 이동 (보상)
                  → 2   : 방향 완전 불일치 + 큰 이동 (최대 페널티)
                  → ≈1  : 매우 작은 이동 (노이즈 — 중립)

  L_smooth(ŷ, y) = SmoothL1(ŷ, y; β_s)      — 미분 안정성 보조항

파라미터 가이드 (future_ret_1, std≈0.000719 기준)
─────────────────────────────────────────────────────────────────────────────
  tau   = 0.000719  (fut_ret_1의 std — tanh 커널 스케일)
  beta  = 1.0       (선형 magnitude 가중)
  gamma = 500.0     (exp 평활화: 0.1% 이동 시 ×1.05, 0.5% 이동 시 ×12.2)
  alpha = 0.70      (방향 손실 70% + smooth 손실 30%)
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


class GMADLoss(nn.Module):
    """Generalized Mean Absolute Directional Loss.

    Args:
        tau        : tanh 커널 스케일 ≈ future_ret_1의 std (default: 7.19e-4)
        beta       : magnitude 지수 (default: 1.0 — 선형 스케일링)
        gamma      : 지수 평활화 계수 (default: 500.0 — 이상 이동 지수적 강조)
        alpha      : L_dir 비율 (default: 0.7; 나머지 0.3 = L_smooth)
        smooth_beta: SmoothL1 전환 임계값 (default: 1e-4 ≈ 0.01%)
        normalize_w: 배치 내 가중치 평균=1 로 정규화 (default: True)

    Shape:
        y_pred: (B,) or (B, 1) — 예측 로그수익률
        y_true: (B,) or (B, 1) — 실제 로그수익률
        → scalar loss
    """

    def __init__(
        self,
        tau: float = 7.19e-4,
        beta: float = 1.0,
        gamma: float = 500.0,
        alpha: float = 0.70,
        smooth_beta: float = 1e-4,
        normalize_w: bool = True,
    ) -> None:
        super().__init__()
        self.beta = beta
        self.gamma = gamma
        self.alpha = alpha
        self.smooth_beta = smooth_beta
        self.normalize_w = normalize_w

        # τ² = variance scale for tanh kernel; stored as buffer (device-aware)
        tau_sq = float(tau) ** 2 + 1e-16   # ε 방어
        self.register_buffer("tau_sq", torch.tensor(tau_sq, dtype=torch.float32))

    # ── forward ──────────────────────────────────────────────────────────────

    def forward(
        self,
        y_pred: torch.Tensor,
        y_true: torch.Tensor,
    ) -> torch.Tensor:
        """
        Args:
            y_pred: 예측 로그수익률 (B,) or (B,1)
            y_true: 실제 로그수익률 (B,) or (B,1)

        Returns:
            scalar GMADL loss
        """
        y_pred = y_pred.view(-1).float()
        y_true = y_true.view(-1).float()

        # ── 1. Magnitude Weight ──────────────────────────────────────────────
        # wᵢ = |yᵢ|^β · exp(min(γ·|yᵢ|, 20))
        # exp clamp: 최대 exp(20) ≈ 4.85e8 — float32 범위 안전
        abs_y = y_true.abs()
        exp_term = torch.exp(torch.clamp(self.gamma * abs_y, max=20.0))
        w = abs_y.pow(self.beta) * exp_term          # (B,)

        if self.normalize_w:
            # 배치 평균=1로 정규화 (학습률 스케일 안정)
            w = w / (w.mean().detach() + 1e-8)

        # ── 2. Directional Component ─────────────────────────────────────────
        # L_dir = 1 - tanh(ŷ·y / τ²)  ∈ [0, 2]
        # ŷ·y > 0 (같은 부호) → tanh(+) → L_dir ↓  (보상)
        # ŷ·y < 0 (다른 부호) → tanh(-) → L_dir ↑  (페널티)
        # |ŷ·y| ≪ τ²         → tanh(≈0) → L_dir ≈ 1 (노이즈 중립)
        direction_kernel = y_pred * y_true / self.tau_sq   # (B,)
        L_dir = 1.0 - torch.tanh(direction_kernel)          # (B,) ∈ [0, 2]

        # ── 3. Smooth L1 (Huber) Component ───────────────────────────────────
        # |error| < smooth_beta : L = 0.5 × error²/β  (2차, gradient 안정)
        # |error| ≥ smooth_beta : L = |error| - 0.5β  (선형, large error 억제)
        L_smooth = F.smooth_l1_loss(
            y_pred, y_true, reduction="none", beta=self.smooth_beta
        )   # (B,)

        # ── 4. Combine & Weight ───────────────────────────────────────────────
        loss_per_sample = w * (self.alpha * L_dir + (1.0 - self.alpha) * L_smooth)

        return loss_per_sample.mean()

    # ── 추가 유틸 ─────────────────────────────────────────────────────────────

    def direction_accuracy(
        self,
        y_pred: torch.Tensor,
        y_true: torch.Tensor,
        min_magnitude: float = 0.0,
    ) -> torch.Tensor:
        """방향 정확도 (부호 일치 비율, magnitude 임계값 초과 샘플 기준).

        Args:
            y_pred       : 예측 log-return
            y_true       : 실제 log-return
            min_magnitude: 이 값 이상의 실제 이동만 평가 (기본: 모든 샘플)

        Returns:
            direction accuracy ∈ [0, 1]
        """
        y_pred = y_pred.view(-1)
        y_true = y_true.view(-1)

        mask = y_true.abs() >= min_magnitude
        if mask.sum() == 0:
            return torch.tensor(float("nan"))

        correct = (torch.sign(y_pred[mask]) == torch.sign(y_true[mask])).float()
        return correct.mean()

    def extra_repr(self) -> str:
        return (
            f"tau_sq={float(self.tau_sq):.3e}, beta={self.beta}, "
            f"gamma={self.gamma}, alpha={self.alpha}, "
            f"smooth_beta={self.smooth_beta}, normalize_w={self.normalize_w}"
        )
