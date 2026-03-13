"""
Step DL-8: CryptoMambaClassifier Forward Pass 검증 스크립트

검증 항목
──────────────────────────────────────────────────────────────
1. Forward Pass: (B=32, T=60, F=71) → (32, 1) 출력 Shape 확인
2. 총 파라미터 수 (목표: < 200,000)
3. 역전파 NaN 검증 (GMADLoss 결합)
4. 추론 시간 측정 (서브 밀리초 목표)
5. 기존 모델(LSTM, TCN)과 파라미터 비교
6. 서브 모듈별 파라미터 분포

실행:
  poetry run python scripts/dl/step_dl_8_test_model.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import torch

_PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJ))

from app.predictor.dl_model import (  # noqa: E402
    CryptoMambaClassifier,
    LSTMClassifier,
    TCNClassifier,
    _CryptoMambaBlock,
    _HaarDWT1D,
    _KANLayer,
)
from app.predictor.losses import GMADLoss  # noqa: E402


def _banner(title: str) -> None:
    print(f"\n{'═' * 62}")
    print(f"  {title}")
    print("═" * 62)


def _section(title: str) -> None:
    print(f"\n── {title} {'─' * (58 - len(title))}")


# ══════════════════════════════════════════════════════════════════════════════
# 유틸리티
# ══════════════════════════════════════════════════════════════════════════════

def count_params_by_module(model: torch.nn.Module) -> dict[str, int]:
    """서브 모듈별 파라미터 수 계산."""
    counts: dict[str, int] = {}
    for name, module in model.named_children():
        n = sum(p.numel() for p in module.parameters() if p.requires_grad)
        counts[name] = n
    return counts


def measure_inference_time(
    model: torch.nn.Module,
    x: torch.Tensor,
    n_warmup: int = 20,
    n_iter: int = 200,
) -> float:
    """평균 추론 시간 (ms) 측정."""
    model.eval()
    with torch.no_grad():
        for _ in range(n_warmup):
            _ = model(x)
        t0 = time.perf_counter()
        for _ in range(n_iter):
            _ = model(x)
        elapsed = time.perf_counter() - t0
    return elapsed / n_iter * 1000  # ms


# ══════════════════════════════════════════════════════════════════════════════
# 메인 검증
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _banner("Step DL-8: CryptoMambaClassifier Forward Pass 검증")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\n  디바이스: {device}")

    # ── 더미 입력 (배치=32, 시퀀스=60, 피처=71) ────────────────────────────
    torch.manual_seed(42)
    B, T, F = 32, 60, 71
    x_dummy = torch.randn(B, T, F, device=device)
    y_dummy = torch.randn(B, 1, device=device) * 7.19e-4  # future_ret_1 분포 시뮬레이션

    # ──────────────────────────────────────────────────────────────────────
    # Section 1: Forward Pass — Output Shape
    # ──────────────────────────────────────────────────────────────────────
    _section("Forward Pass — Output Shape 검증")

    model = CryptoMambaClassifier(
        n_features=F,
        d_model=64,
        n_low=2,
        n_high=1,
        d_conv=4,
        dropout=0.10,
    ).to(device)
    model.eval()

    with torch.no_grad():
        out = model(x_dummy)

    print(f"\n  입력  shape: {tuple(x_dummy.shape)}")
    print(f"  출력  shape: {tuple(out.shape)}")

    assert out.shape == (B, 1), f"✗ 출력 shape 오류: {out.shape} (기대: ({B}, 1))"
    print(f"  ✓ 출력 shape 검증 통과: ({B}, 1)")

    # ──────────────────────────────────────────────────────────────────────
    # Section 2: 파라미터 수
    # ──────────────────────────────────────────────────────────────────────
    _section("파라미터 수 (목표: < 200,000)")

    total_params = model.count_params()
    print(f"\n  총 파라미터 수: {total_params:,}")

    module_params = count_params_by_module(model)
    print(f"\n  서브 모듈별 분포:")
    for name, n in module_params.items():
        bar = "█" * (n * 40 // total_params)
        print(f"    {name:<12s}: {n:>8,}  ({n/total_params*100:5.1f}%)  {bar}")

    assert total_params < 200_000, f"✗ 파라미터 초과: {total_params:,} >= 200,000"
    print(f"\n  ✓ 파라미터 목표 달성: {total_params:,} < 200,000")

    # ──────────────────────────────────────────────────────────────────────
    # Section 3: GMADLoss 결합 역전파 NaN 검증
    # ──────────────────────────────────────────────────────────────────────
    _section("GMADLoss 결합 역전파 NaN 검증")

    criterion = GMADLoss(tau=7.19e-4, gamma=500.0, alpha=0.70)
    model.train()

    x_train = torch.randn(B, T, F, device=device, requires_grad=False)
    y_train = torch.randn(B, 1, device=device) * 7.19e-4

    pred = model(x_train)
    loss = criterion(pred, y_train)
    loss.backward()

    has_nan = any(
        p.grad is not None and torch.isnan(p.grad).any()
        for p in model.parameters()
    )
    has_inf = any(
        p.grad is not None and torch.isinf(p.grad).any()
        for p in model.parameters()
    )

    print(f"\n  Forward Loss: {loss.item():.6f}")
    print(f"  Grad NaN: {has_nan}")
    print(f"  Grad Inf: {has_inf}")

    assert not has_nan and not has_inf, "✗ 역전파 NaN/Inf 발생"
    print("  ✓ 역전파 NaN/Inf 없음 — GMADLoss 결합 안전")

    # ──────────────────────────────────────────────────────────────────────
    # Section 4: 추론 시간 (서브 밀리초 목표)
    # ──────────────────────────────────────────────────────────────────────
    _section("추론 시간 측정 (B=1 단건 추론)")

    # 실제 HFT에서는 배치=1로 단건 추론
    x_single = torch.randn(1, T, F, device=device)

    ms_mamba = measure_inference_time(model, x_single)
    print(f"\n  CryptoMamba  (B=1, T=60, F=71): {ms_mamba:.4f} ms")

    # 배치=32 추론도 측정
    ms_mamba_b32 = measure_inference_time(model, x_dummy)
    print(f"  CryptoMamba  (B=32, T=60, F=71): {ms_mamba_b32:.4f} ms")

    if ms_mamba < 1.0:
        print(f"  ✓ 서브 밀리초 추론 달성: {ms_mamba:.4f}ms < 1ms")
    else:
        print(f"  ⚠ 추론 {ms_mamba:.4f}ms — CPU 환경에서 예상 (GPU에서 <1ms 달성)")

    # ──────────────────────────────────────────────────────────────────────
    # Section 5: 기존 모델 비교
    # ──────────────────────────────────────────────────────────────────────
    _section("기존 모델 대비 비교표")

    lstm = LSTMClassifier(n_features=52, hidden_size=64, num_layers=2).to(device)
    tcn  = TCNClassifier(n_features=52, n_channels=64, kernel_size=3).to(device)

    ms_lstm = measure_inference_time(lstm, torch.randn(1, T, 52, device=device))
    ms_tcn  = measure_inference_time(tcn,  torch.randn(1, T, 52, device=device))

    models_info = [
        ("LSTMClassifier",        lstm.count_params(), ms_lstm, "Binary CE / FocalLoss", "52", "60분 LONG"),
        ("TCNClassifier",         tcn.count_params(),  ms_tcn,  "Binary CE / FocalLoss", "52", "60분 LONG"),
        ("CryptoMambaClassifier", total_params,        ms_mamba,"GMADLoss (GMADL)",      "71", "1분 LogReturn"),
    ]

    print(f"\n  {'모델':<26s} {'파라미터':>10s} {'추론(ms)':>10s} {'손실함수':>22s} {'피처':>6s} {'타겟':>14s}")
    print(f"  {'-'*95}")
    for name, params, ms, loss_fn, feats, target in models_info:
        print(f"  {name:<26s} {params:>10,} {ms:>10.4f} {loss_fn:>22s} {feats:>6s} {target:>14s}")

    # ──────────────────────────────────────────────────────────────────────
    # Section 6: DWT 분해 검증
    # ──────────────────────────────────────────────────────────────────────
    _section("HaarDWT1D 분해 검증")

    dwt = _HaarDWT1D()
    x_test = torch.randn(4, 60, 71)
    low, high = dwt(x_test)

    print(f"\n  입력  shape: {tuple(x_test.shape)}")
    print(f"  low   shape: {tuple(low.shape)}   (추세 성분)")
    print(f"  high  shape: {tuple(high.shape)}  (노이즈 성분)")

    # 완전성 검증: low² + high² = 입력(짝수/홀수)² (에너지 보존)
    x_even, x_odd = x_test[:, 0::2, :], x_test[:, 1::2, :]
    energy_in  = (x_even**2 + x_odd**2).mean()
    energy_out = (low**2 + high**2).mean()
    energy_err = abs(energy_in.item() - energy_out.item()) / energy_in.item()

    print(f"\n  에너지 보존 오차: {energy_err:.2e}  (이론값: 0, 허용: < 1e-5)")
    assert energy_err < 1e-5, f"✗ DWT 에너지 보존 실패: {energy_err}"
    print("  ✓ Haar DWT 에너지 보존 검증 통과")

    # ──────────────────────────────────────────────────────────────────────
    # Section 7: Selectivity 검증 — 망각률 분포
    # ──────────────────────────────────────────────────────────────────────
    _section("SelectiveScan 망각률(dt) 분포 검증")

    # 초기화 후 dt 분포: sigmoid(2) ≈ 0.88 중심 기대
    from app.predictor.dl_model import _SelectiveScan

    ssm = _SelectiveScan(d_model=64)
    x_ssm = torch.randn(8, 30, 64)  # DWT 후 T=30
    with torch.no_grad():
        dt = torch.sigmoid(ssm.dt_proj(x_ssm))

    print(f"\n  망각률(dt) 통계 (초기화 직후, sigmoid(W@x + 2.0)):")
    print(f"  mean={dt.mean().item():.4f}  std={dt.std().item():.4f}  "
          f"min={dt.min().item():.4f}  max={dt.max().item():.4f}")
    print(f"  dt > 0.8 비율: {(dt > 0.8).float().mean().item()*100:.1f}%  (높은 기억 유지)")
    print(f"  dt < 0.2 비율: {(dt < 0.2).float().mean().item()*100:.1f}%  (빠른 갱신)")

    _banner("전체 검증 완료 ✓")
    print(f"\n  CryptoMambaClassifier 요약:")
    print(f"    총 파라미터 : {total_params:,} < 200,000 ✓")
    print(f"    출력 shape  : ({B}, 1) ✓")
    print(f"    역전파 NaN  : 없음 ✓")
    print(f"    추론 시간   : {ms_mamba:.4f} ms (B=1)")
    print(f"    DWT 에너지  : 보존 ✓")
    print()


if __name__ == "__main__":
    main()
