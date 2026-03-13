"""
Step DL-7: GMADL 텐서 연산 검증 스크립트

검증 항목
──────────────────────────────────────────────────────────────
1. 3가지 케이스 Loss 비교 (방향·크기 조합)
2. MSE / MAE / GMADL 3종 손실 함수 비교 테이블
3. 역전파(Backpropagation) NaN 점검
4. 배치 통계 (정규 분포 샘플)

실행:
  poetry run python scripts/dl/step_dl_7_test_gmadl.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import torch
import torch.nn as nn

# ── 프로젝트 루트를 sys.path에 추가 ────────────────────────────────────────
_PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJ))

from app.predictor.losses import GMADLoss  # noqa: E402

# ── 컬러 출력 헬퍼 (선택적) ────────────────────────────────────────────────
def _banner(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print('═' * 60)


def _section(title: str) -> None:
    print(f"\n── {title} {'─' * (54 - len(title))}")


# ══════════════════════════════════════════════════════════════════════════════
# 케이스 정의
# ══════════════════════════════════════════════════════════════════════════════

CASES = {
    "Case 1 (방향 일치 + 큰 이동)": {
        "y_true": 0.005,    # +0.5% 상승
        "y_pred": 0.004,    # +0.4% 예측 (부호 일치, 오차 0.1%)
        "expected": "Loss 최소 — 방향 맞춤, 돌파 구간",
    },
    "Case 2 (방향 불일치 + 작은 이동)": {
        "y_true": 0.0001,   # +0.01% 상승 (노이즈 수준)
        "y_pred": -0.0002,  # -0.02% 예측 (부호 불일치, 작은 magnitude)
        "expected": "Loss 중간 — 방향 틀렸으나 magnitude가 작아 페널티 제한",
    },
    "Case 3 (방향 불일치 + 큰 이동)": {
        "y_true": 0.005,    # +0.5% 상승
        "y_pred": -0.004,   # -0.4% 예측 (부호 완전 반대, 큰 magnitude)
        "expected": "Loss 극대 — 치명적 오답, 돌파 구간에서 반대 방향 예측",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# 베이스라인 손실 함수들
# ══════════════════════════════════════════════════════════════════════════════

def mse_loss(y_pred: float, y_true: float) -> float:
    return (y_pred - y_true) ** 2


def mae_loss(y_pred: float, y_true: float) -> float:
    return abs(y_pred - y_true)


def huber_loss(y_pred: float, y_true: float, delta: float = 1e-4) -> float:
    e = abs(y_pred - y_true)
    if e < delta:
        return 0.5 * e**2 / delta
    return e - 0.5 * delta


# ══════════════════════════════════════════════════════════════════════════════
# 검증 실행
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _banner("Step DL-7: GMADL 텐서 연산 검증")

    # ── GMADL 인스턴스 (future_ret_1 std 기반) ─────────────────────────────
    criterion = GMADLoss(
        tau=7.19e-4,     # future_ret_1 std
        beta=1.0,
        gamma=500.0,
        alpha=0.70,
        smooth_beta=1e-4,
        normalize_w=False,  # 개별 케이스 직관적 비교를 위해 OFF
    )
    print(f"\nGMADLoss 설정:\n  {criterion}")

    # ──────────────────────────────────────────────────────────────────────
    # Section 1: 3가지 케이스 Loss 비교
    # ──────────────────────────────────────────────────────────────────────
    _section("케이스별 Loss 비교")

    results: dict[str, dict[str, float]] = {}
    print(f"\n{'케이스':<38s} {'GMADL':>12s} {'MSE':>12s} {'MAE':>12s} {'Huber':>12s}")
    print("-" * 90)

    for name, case in CASES.items():
        yt = torch.tensor([case["y_true"]], dtype=torch.float32)
        yp = torch.tensor([case["y_pred"]], dtype=torch.float32)

        with torch.no_grad():
            loss_gmadl = criterion(yp, yt).item()
        loss_mse   = mse_loss(case["y_pred"], case["y_true"])
        loss_mae   = mae_loss(case["y_pred"], case["y_true"])
        loss_huber = huber_loss(case["y_pred"], case["y_true"])

        results[name] = {
            "GMADL": loss_gmadl,
            "MSE": loss_mse,
            "MAE": loss_mae,
            "Huber": loss_huber,
        }

        short = name[:36]
        print(f"  {short:<36s} {loss_gmadl:12.6f} {loss_mse:12.2e} {loss_mae:12.6f} {loss_huber:12.6f}")
        print(f"  {'→ '+case['expected']:<88s}")
        print()

    # ──────────────────────────────────────────────────────────────────────
    # Section 2: 순위 비교 (핵심)
    # ──────────────────────────────────────────────────────────────────────
    _section("순위 비교 (GMADL vs MSE)")

    names = list(CASES.keys())
    gmadl_vals = [results[n]["GMADL"] for n in names]
    mse_vals   = [results[n]["MSE"]   for n in names]

    gmadl_order = sorted(range(3), key=lambda i: gmadl_vals[i])
    mse_order   = sorted(range(3), key=lambda i: mse_vals[i])

    print("\n  GMADL 순위 (낮을수록 좋음):")
    for rank, idx in enumerate(gmadl_order, 1):
        print(f"    {rank}위: {names[idx]}  ({gmadl_vals[idx]:.6f})")

    print("\n  MSE 순위 (낮을수록 좋음):")
    for rank, idx in enumerate(mse_order, 1):
        print(f"    {rank}위: {names[idx]}  ({mse_vals[idx]:.2e})")

    # 핵심 비교
    c1_g, c2_g, c3_g = [gmadl_vals[i] for i in range(3)]
    c1_m, c2_m, c3_m = [mse_vals[i] for i in range(3)]

    print("\n  [GMADL] Case3 / Case1 배율:", f"{c3_g/c1_g:.0f}x")
    print("  [MSE  ] Case3 / Case1 배율:", f"{c3_m/c1_m:.0f}x")
    print()
    print("  [GMADL] Case2 / Case1 배율:", f"{c2_g/c1_g:.1f}x  (방향 틀림 + 노이즈 패널티)")
    print("  [MSE  ] Case2 / Case1 배율:", f"{c2_m/c1_m:.1f}x  (MSE는 Case1 > Case2 — 방향 무시)")

    assert c1_g < c2_g, "GMADL: Case1 < Case2 기대 실패"
    assert c2_g < c3_g, "GMADL: Case2 < Case3 기대 실패"
    print("\n  ✓ GMADL 순위 검증 통과: Case1 < Case2 < Case3")

    # ──────────────────────────────────────────────────────────────────────
    # Section 3: 역전파 NaN 검증
    # ──────────────────────────────────────────────────────────────────────
    _section("역전파(Backpropagation) NaN 검증")

    edge_cases = [
        ("정상 케이스",         0.004,  0.005),
        ("제로 예측",           0.0,    0.005),
        ("제로 타겟 (노이즈)",  0.001,  0.0),
        ("제로 예측+타겟",      0.0,    0.0),
        ("양쪽 반대 방향",     -0.005,  0.005),
        ("극단값 예측",         0.05,  -0.05),
    ]

    all_ok = True
    for desc, pred_v, true_v in edge_cases:
        yp = torch.tensor([pred_v], dtype=torch.float32, requires_grad=True)
        yt = torch.tensor([true_v], dtype=torch.float32)

        loss = criterion(yp, yt)
        loss.backward()

        grad_ok   = yp.grad is not None and not torch.isnan(yp.grad).any()
        loss_ok   = not torch.isnan(loss) and not torch.isinf(loss)
        status    = "✓" if (grad_ok and loss_ok) else "✗"
        if not (grad_ok and loss_ok):
            all_ok = False

        print(f"  {status} {desc:<26s}  loss={loss.item():+.6f}  "
              f"grad={yp.grad.item():+.6f}")

        yp.grad = None  # 초기화

    if all_ok:
        print("\n  ✓ 모든 엣지 케이스에서 NaN 없이 역전파 성공")
    else:
        print("\n  ✗ 일부 케이스에서 NaN/Inf 발생 — 확인 필요")

    # ──────────────────────────────────────────────────────────────────────
    # Section 4: 배치 통계 (실제 데이터 분포 시뮬레이션)
    # ──────────────────────────────────────────────────────────────────────
    _section("배치 통계 (future_ret_1 분포 시뮬레이션, N=10000)")

    torch.manual_seed(42)
    std = 7.19e-4  # future_ret_1의 실측 std

    # 실제 분포: 정규 + fat tail (혼합 정규)
    n_normal = 9350  # 93.5% (|ret| < 0.1%)
    n_large  = 650   # 6.5%  (|ret| > 0.1% — 유의미한 이동)

    y_true_normal = torch.randn(n_normal) * std * 0.5        # 작은 변동
    y_true_large  = torch.randn(n_large)  * std * 3.0        # 큰 변동 (fat tail)
    y_true = torch.cat([y_true_normal, y_true_large])

    # 세 가지 예측 전략 시뮬레이션
    y_pred_random    = torch.randn_like(y_true) * std   # 랜덤 예측
    y_pred_correct   = y_true + torch.randn_like(y_true) * std * 0.2  # 좋은 예측
    y_pred_safe_zero = torch.zeros_like(y_true)          # "항상 0" 예측 (safe/lazy)

    # normalize_w=True인 criterion으로 배치 테스트
    criterion_norm = GMADLoss(tau=std, gamma=500.0, alpha=0.70, normalize_w=True)

    with torch.no_grad():
        l_random = criterion_norm(y_pred_random, y_true).item()
        l_correct = criterion_norm(y_pred_correct, y_true).item()
        l_zero = criterion_norm(y_pred_safe_zero, y_true).item()

    print(f"\n  예측 전략별 GMADL:")
    print(f"    1. 좋은 예측 (노이즈 20% 추가)  : {l_correct:.6f}")
    print(f"    2. 랜덤 예측                     : {l_random:.6f}")
    print(f"    3. 항상 0 예측 (안전한 횡보)     : {l_zero:.6f}")

    assert l_correct < l_random, "좋은 예측이 랜덤보다 낮아야 함"
    assert l_correct < l_zero,   "좋은 예측이 '항상 0'보다 낮아야 함"
    print("\n  ✓ 검증: '항상 0 예측' 전략이 GMADL로는 높은 페널티 부여됨")
    print("  → MSE의 경우 '항상 0'이 나쁘지 않게 보이지만, GMADL은 이를 억제")

    # 방향 정확도 비교
    acc_correct = criterion_norm.direction_accuracy(
        y_pred_correct, y_true, min_magnitude=1e-4
    ).item()
    acc_random = criterion_norm.direction_accuracy(
        y_pred_random, y_true, min_magnitude=1e-4
    ).item()

    print(f"\n  방향 정확도 (|y| > 0.01%):")
    print(f"    좋은 예측:  {acc_correct*100:.1f}%")
    print(f"    랜덤 예측:  {acc_random*100:.1f}%")

    # ──────────────────────────────────────────────────────────────────────
    # Section 5: 하이퍼파라미터 γ 민감도 분석
    # ──────────────────────────────────────────────────────────────────────
    _section("γ (gamma) 민감도 분석 — 지수 평활화 강도")

    print(f"\n  {'γ':>8s}  {'Case1 (small)':>15s}  {'Case3 (large)':>15s}  "
          f"{'Ratio (C3/C1)':>15s}")
    print(f"  {'-'*60}")

    yt_small = torch.tensor([0.0001])
    yp_small = torch.tensor([-0.0002])
    yt_large = torch.tensor([0.005])
    yp_large = torch.tensor([-0.004])

    for gamma_val in [0.0, 100.0, 500.0, 1000.0, 2000.0]:
        crit_g = GMADLoss(tau=std, gamma=gamma_val, alpha=0.70, normalize_w=False)
        with torch.no_grad():
            l_small = crit_g(yp_small, yt_small).item()
            l_large = crit_g(yp_large, yt_large).item()
        ratio = l_large / (l_small + 1e-12)
        print(f"  {gamma_val:>8.0f}  {l_small:>15.6f}  {l_large:>15.6f}  {ratio:>15.1f}x")

    print("\n  γ=0: 순수 크기 선형 가중  →  돌파 구간 강조 없음")
    print("  γ=500 (기본): 돌파 구간(±0.5%) 가중치 ×12.2  →  권장값")
    print("  γ=2000: 극단 이벤트 초집중  →  일반 구간 학습 저하 우려")

    _banner("검증 완료 ✓")


if __name__ == "__main__":
    main()
