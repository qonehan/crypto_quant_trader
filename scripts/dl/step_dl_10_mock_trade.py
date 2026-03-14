"""
Step DL-10: CryptoMamba 모의투자 백테스트

개요
──────────────────────────────────────────────────────────────────────────────
  학습된 CryptoMamba 모델(v4, GMADLoss 회귀)을 이용해
  Test 구간에서 롱 전용 돌파 전략(Long-only Threshold Strategy)을 시뮬레이션합니다.

전략 로직
──────────────────────────────────────────────────────────────────────────────
  1) 매 1분봉마다 모델 예측값(pred)이 상위 threshold_pct(소수) 이상이면 롱 진입
       예: --threshold_pct 0.1  → 상위 10% 진입
  2) 15분 뒤 종가에 무조건 청산 (future_ret_15 직접 사용)
  3) 비용 공제:
       cost_rate = fee × 2 (왕복) + slippage
       기본값: 0.0005×2 + 0.0005 = 0.15%

출력 파일
──────────────────────────────────────────────────────────────────────────────
  artifacts/dl_prod/mock_trade_preds.csv    — 전체 테스트 예측값 (대시보드용)
  artifacts/dl_prod/mock_trade_log.csv      — 기본 threshold의 매매 로그
  artifacts/dl_prod/mock_trade_metrics.json — 핵심 성과 지표 요약

실행 예시
──────────────────────────────────────────────────────────────────────────────
  # 기본 실행 (상위 10% 진입, fee 0.05%, slippage 0.05%)
  poetry run python scripts/dl/step_dl_10_mock_trade.py

  # 파라미터 커스텀
  poetry run python scripts/dl/step_dl_10_mock_trade.py \\
      --threshold_pct 0.05 --fee 0.0003 --slippage 0.0002

  # 코랩/다른 경로에서 실행
  python step_dl_10_mock_trade.py --project_root /content/crypto_quant_trader

  # 스모크 테스트 (처음 10K행만)
  poetry run python scripts/dl/step_dl_10_mock_trade.py --smoke_test
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import torch

_PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJ))

from app.predictor.dl_model import CryptoMambaClassifier   # noqa: E402

# ── 기본 경로 상수 (--project_root 로 런타임 오버라이드 가능) ─────────────────
_DEFAULT_ARTIFACT_DIR = _PROJ / "artifacts" / "dl_prod"
_DEFAULT_DATASET_PATH = _PROJ / "data" / "datasets" / "btc_1m_hft_v2.parquet"

# ── 전략 상수 ─────────────────────────────────────────────────────────────────
TARGET_COL  = "future_ret_15"
SEQ_LEN     = 60
TRAIN_RATIO = 0.70
VAL_RATIO   = 0.15

EXCLUDE_COLS = {
    "target", "future_ret", "target_1m",
    "future_ret_1", "future_ret_5", "future_ret_15", "future_ret_60",
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. 데이터 전처리
# ══════════════════════════════════════════════════════════════════════════════

def load_and_preprocess(
    dataset_path: Path,
    scaler_path: Path,
    max_rows: int | None = None,
) -> tuple[np.ndarray, pd.DataFrame, list[str]]:
    """parquet → Test split 피처 배열 + 타겟 DataFrame 반환.

    step_dl_9와 동일한 분할 로직:
      Train 0~70% / Val 70~85% / Test 85~100%
      NaN: ffill → Train median fill (누수 없음)
      스케일링: 저장된 RobustScaler 적용 (Train only fit)
    """
    print(f"[Data] 로드: {dataset_path.name}")
    df = pd.read_parquet(dataset_path)

    if TARGET_COL not in df.columns:
        raise KeyError(
            f"'{TARGET_COL}' 컬럼 없음. "
            "step_dl_6_feature_engineering.py를 먼저 실행하세요."
        )

    df = df.dropna(subset=[TARGET_COL])
    feat_cols: list[str] = [c for c in df.columns if c not in EXCLUDE_COLS]

    if max_rows:
        df = df.iloc[:max_rows]
        print(f"  [Debug] max_rows={max_rows} 적용")

    # 시간순 분할
    n    = len(df)
    n_tr = int(n * TRAIN_RATIO)
    n_vl = int(n * VAL_RATIO)

    train_df = df.iloc[:n_tr].copy()
    test_df  = df.iloc[n_tr + n_vl:].copy()

    print(f"  Train:{len(train_df):,}  |  Test:{len(test_df):,}행")

    # NaN 처리
    train_df[feat_cols] = train_df[feat_cols].ffill()
    test_df[feat_cols]  = test_df[feat_cols].ffill()
    train_median        = train_df[feat_cols].median()
    test_df[feat_cols]  = test_df[feat_cols].fillna(train_median)

    # 스케일러 적용
    scaler = joblib.load(scaler_path)
    X_test = scaler.transform(test_df[feat_cols].values).astype(np.float32)

    print(f"  피처 수: {len(feat_cols)}  |  Test 피처 배열: {X_test.shape}")
    return X_test, test_df, feat_cols


# ══════════════════════════════════════════════════════════════════════════════
# 2. 배치 추론 (슬라이딩 윈도우)
# ══════════════════════════════════════════════════════════════════════════════

def run_inference(
    X_test: np.ndarray,
    device: torch.device,
    model_path: Path,
    meta_path: Path,
    batch_size: int = 512,
) -> np.ndarray:
    """Test 피처 배열에 대해 슬라이딩 윈도우 배치 추론.

    Returns:
        preds: (n_test - SEQ_LEN,) 예측값 배열
               preds[i]는 X_test[i + SEQ_LEN]에 해당하는 타임스텝의 예측값
    """
    print("[Inference] 모델 로드 및 배치 추론 시작...")

    # load() 내부에서 cls(**meta)는 CPU에 생성 → .to(device) 로 모든 파라미터·버퍼 GPU 이동
    model = CryptoMambaClassifier.load(model_path, meta_path, device=str(device))
    model.to(device)
    model.eval()

    # CUDA AMP 사용 가능 여부 (CPU 환경에서는 비활성화)
    use_amp = device.type == "cuda"

    n_windows = len(X_test) - SEQ_LEN
    preds: list[float] = []

    t0 = time.time()
    with torch.no_grad():
        for start in range(0, n_windows, batch_size):
            end   = min(start + batch_size, n_windows)
            batch = np.stack([X_test[i : i + SEQ_LEN] for i in range(start, end)])
            # zero-copy numpy 공유 후 단일 GPU 전송
            x_tensor = torch.from_numpy(batch).to(device)

            # AMP autocast: GPU → FP16 연산 (속도↑ 메모리↓), CPU → no-op
            with torch.cuda.amp.autocast(enabled=use_amp):
                p = model(x_tensor).squeeze(-1).cpu().numpy()

            preds.extend(p.tolist())

            if (start // batch_size) % 50 == 0:
                elapsed = time.time() - t0
                print(f"  진행: {end / n_windows * 100:5.1f}%  "
                      f"({end:,}/{n_windows:,})  {elapsed:.1f}s")

    elapsed = time.time() - t0
    print(f"  추론 완료: {n_windows:,}개 예측  ({elapsed:.1f}초)")
    return np.array(preds, dtype=np.float32)


# ══════════════════════════════════════════════════════════════════════════════
# 3. 백테스트 엔진
# ══════════════════════════════════════════════════════════════════════════════

def compute_mdd(equity: pd.Series) -> float:
    """최대 낙폭(MDD) 계산."""
    rolling_max = equity.cummax()
    return float((equity / rolling_max - 1).min())


def compute_sharpe(net_ret: pd.Series, periods_per_day: int = 96) -> float:
    """연환산 Sharpe Ratio (15분봉 기준, 24/7 암호화폐 시장).

    15분봉 × 96 periods/day × 365 days = 35,040 periods/year
    """
    if net_ret.std() < 1e-10 or len(net_ret) < 2:
        return float("nan")
    annual_factor = (periods_per_day * 365) ** 0.5
    return float(net_ret.mean() / net_ret.std() * annual_factor)


def run_backtest(
    preds_df: pd.DataFrame,
    threshold_pct: float = 0.1,
    cost_rate: float = 0.0015,
) -> tuple[pd.DataFrame, dict]:
    """롱 전용 돌파 전략 백테스트.

    진입 조건: pred >= 상위 threshold_pct 분위수
               (threshold_pct는 소수 표현: 0.1 = 상위 10%)
    청산 조건: 15분 후 무조건 청산 (future_ret_15 직접 사용)
    비용 공제: cost_rate = fee×2 + slippage

    Args:
        preds_df      : 전체 예측 DataFrame (pred, actual_ret, close 포함)
        threshold_pct : 진입 상위 비율 (소수; 0.1 = 상위 10%)
        cost_rate     : 왕복 비용률 (fee×2 + slippage)

    Returns:
        trades  : 매매 로그 DataFrame
        metrics : 성과 지표 dict
    """
    # 소수 → 퍼센타일 변환: 0.1 → 상위 10% → 하위 90% 분위수
    threshold_val = float(
        np.percentile(preds_df["pred"].values, 100.0 - threshold_pct * 100.0)
    )
    trades = preds_df[preds_df["pred"] >= threshold_val].copy()

    if len(trades) == 0:
        return pd.DataFrame(), {
            "n_trades": 0, "total_pnl_pct": 0.0,
            "win_rate_pct": 0.0, "mdd_pct": 0.0,
            "sharpe": float("nan"),
            "threshold_val": threshold_val,
            "cost_rate_pct": cost_rate * 100,
        }

    trades["cost"]      = cost_rate
    trades["net_ret"]   = trades["actual_ret"] - cost_rate
    trades["equity"]    = (1 + trades["net_ret"]).cumprod()
    trades["cum_pnl"]   = trades["equity"] - 1
    trades["threshold"] = threshold_val

    n_trades  = len(trades)
    total_pnl = float(trades["cum_pnl"].iloc[-1]) * 100
    win_rate  = float((trades["net_ret"] > 0).mean()) * 100
    mdd       = compute_mdd(trades["equity"]) * 100
    sharpe    = compute_sharpe(trades["net_ret"])
    avg_ret   = float(trades["net_ret"].mean()) * 100
    avg_pred  = float(trades["pred"].mean())

    metrics = {
        "model":             "CryptoMamba v4 (GMADLoss 회귀)",
        "test_period_start": str(preds_df.index[0]),
        "test_period_end":   str(preds_df.index[-1]),
        "n_test_samples":    len(preds_df),
        "threshold_pct":     threshold_pct,
        "threshold_val":     round(threshold_val,  6),
        "n_trades":          n_trades,
        "total_pnl_pct":     round(total_pnl,      4),
        "win_rate_pct":      round(win_rate,        2),
        "mdd_pct":           round(mdd,             4),
        "sharpe":            round(sharpe, 4) if not np.isnan(sharpe) else None,
        "avg_net_ret_pct":   round(avg_ret,         4),
        "avg_pred":          round(avg_pred,         6),
        "cost_rate_pct":     round(cost_rate * 100,  4),
        "generated_at":      datetime.now(timezone.utc).isoformat(),
    }

    return trades, metrics


# ══════════════════════════════════════════════════════════════════════════════
# 4. 메인 파이프라인
# ══════════════════════════════════════════════════════════════════════════════

def main(
    project_root:  Path  = _PROJ,
    threshold_pct: float = 0.1,
    fee:           float = 0.0005,
    slippage:      float = 0.0005,
    batch_size:    int   = 512,
    smoke_test:    bool  = False,
) -> None:
    t_total   = time.time()
    device    = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    cost_rate = fee * 2 + slippage   # 왕복 수수료 + 슬리피지

    # ── 경로: project_root 기반으로 전체 오버라이드 ─────────────────────────
    artifact_dir  = Path(project_root) / "artifacts" / "dl_prod"
    dataset_path  = Path(project_root) / "data" / "datasets" / "btc_1m_hft_v2.parquet"
    model_path    = artifact_dir / "cryptomamba_model.pt"
    meta_path     = artifact_dir / "cryptomamba_model_meta.json"
    scaler_path   = artifact_dir / "cryptomamba_scaler.joblib"
    preds_path    = artifact_dir / "mock_trade_preds.csv"
    log_path      = artifact_dir / "mock_trade_log.csv"
    metrics_path  = artifact_dir / "mock_trade_metrics.json"

    print(f"\n{'═'*62}")
    print(f"  Step DL-10: CryptoMamba 모의투자 백테스트")
    print(f"  진입: 상위 {threshold_pct*100:.1f}%  |  "
          f"fee: {fee*100:.3f}%  slippage: {slippage*100:.3f}%  "
          f"→ 총 비용: {cost_rate*100:.3f}%")
    print(f"{'═'*62}")
    print(f"  디바이스    : {device}")
    print(f"  project_root: {project_root}")

    max_rows = 10_000 if smoke_test else None

    # ── 1. 데이터 전처리 ────────────────────────────────────────────────
    X_test, test_df, feat_cols = load_and_preprocess(
        dataset_path=dataset_path,
        scaler_path=scaler_path,
        max_rows=max_rows,
    )

    # ── 2. 배치 추론 ────────────────────────────────────────────────────
    preds = run_inference(
        X_test,
        device=device,
        model_path=model_path,
        meta_path=meta_path,
        batch_size=batch_size,
    )

    # X_test 추론 완료 후 즉시 해제
    del X_test
    gc.collect()

    # ── 3. 예측 결과 DataFrame 구성 ─────────────────────────────────────
    # preds[i]는 test_df.iloc[SEQ_LEN + i]에 해당
    pred_df  = test_df.iloc[SEQ_LEN:].copy()
    preds_df = pd.DataFrame({
        "close":      pred_df["close"].values,
        "pred":       preds,
        "actual_ret": pred_df[TARGET_COL].values,
    }, index=pred_df.index)
    preds_df = preds_df.dropna(subset=["actual_ret"])   # 마지막 15봉 NaN 제거
    preds_df.index.name = "timestamp"

    # ── 4. 전체 예측 저장 (대시보드 인터랙티브용) ─────────────────────
    preds_df.to_csv(preds_path)
    print(f"\n[저장] 전체 예측: {preds_path.name}  ({len(preds_df):,}행)")

    p = preds_df["pred"]
    print(f"  pred 분포: mean={p.mean():+.6f}  std={p.std():.6f}  "
          f"p10={np.percentile(p, 10):+.6f}  p90={np.percentile(p, 90):+.6f}")

    # ── 5. 기본 threshold 백테스트 ──────────────────────────────────────
    print(f"\n[Backtest] threshold={threshold_pct*100:.1f}% "
          f"(상위 {threshold_pct*100:.1f}%)  cost={cost_rate*100:.3f}% ...")
    trades, metrics = run_backtest(
        preds_df,
        threshold_pct=threshold_pct,
        cost_rate=cost_rate,
    )

    # fee / slippage 명세 추가
    metrics["fee_pct"]      = round(fee * 100,      4)
    metrics["slippage_pct"] = round(slippage * 100, 4)

    # ── 6. BTC 보유 수익률 비교 ─────────────────────────────────────────
    btc_hold_ret = float(preds_df["close"].iloc[-1] / preds_df["close"].iloc[0] - 1) * 100
    metrics["btc_hold_pnl_pct"] = round(btc_hold_ret, 4)
    metrics["alpha_vs_btc_pct"] = round(metrics["total_pnl_pct"] - btc_hold_ret, 4)

    # ── 7. 매매 로그 저장 ───────────────────────────────────────────────
    if len(trades) > 0:
        log_cols = ["close", "pred", "threshold", "actual_ret", "cost", "net_ret", "cum_pnl"]
        trades[log_cols].to_csv(log_path, index=True)
        print(f"[저장] 매매 로그: {log_path.name}  ({len(trades):,}건)")

    # ── 8. 성과 지표 저장 ───────────────────────────────────────────────
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    print(f"[저장] 성과 지표: {metrics_path.name}")

    # ── 9. 결과 출력 ────────────────────────────────────────────────────
    elapsed = time.time() - t_total
    print(f"\n{'─'*62}")
    print(f"  백테스트 결과  "
          f"(상위 {threshold_pct*100:.1f}% 진입 | 비용 {cost_rate*100:.3f}%)")
    print(f"{'─'*62}")
    print(f"  테스트 기간    : {metrics['test_period_start'][:10]} ~ {metrics['test_period_end'][:10]}")
    print(f"  진입 임계값    : pred ≥ {metrics['threshold_val']:+.6f}")
    print(f"  비용 내역      : fee {fee*100:.3f}%×2 + slippage {slippage*100:.3f}% = {cost_rate*100:.3f}%")
    print(f"  총 거래 횟수   : {metrics['n_trades']:,}건")
    print(f"  총 누적 수익률 : {metrics['total_pnl_pct']:+.2f}%")
    print(f"  BTC 보유 수익률: {metrics['btc_hold_pnl_pct']:+.2f}%")
    print(f"  알파 (초과수익): {metrics['alpha_vs_btc_pct']:+.2f}%")
    print(f"  승률           : {metrics['win_rate_pct']:.1f}%")
    print(f"  MDD            : {metrics['mdd_pct']:.2f}%")
    sharpe_str = f"{metrics['sharpe']:.3f}" if metrics["sharpe"] else "N/A"
    print(f"  Sharpe Ratio   : {sharpe_str}")
    print(f"  소요 시간      : {elapsed:.1f}초")

    # ── 10. threshold 민감도 요약 ───────────────────────────────────────
    print(f"\n{'─'*62}")
    print(f"  Threshold 민감도 분석  (fee={fee*100:.3f}%  slippage={slippage*100:.3f}%)")
    print(f"{'─'*62}")
    print(f"  {'진입 상위%':>10s}  {'거래수':>8s}  {'수익률':>10s}  {'승률':>8s}  {'MDD':>8s}")
    print(f"  {'─'*50}")
    for pct in [0.05, 0.10, 0.15, 0.20, 0.25]:
        _, m = run_backtest(preds_df, threshold_pct=pct, cost_rate=cost_rate)
        if m["n_trades"] > 0:
            print(f"  {pct*100:>8.0f}%  {m['n_trades']:>8,}  "
                  f"{m['total_pnl_pct']:>+9.2f}%  "
                  f"{m['win_rate_pct']:>7.1f}%  "
                  f"{m['mdd_pct']:>7.2f}%")

    print(f"\n{'═'*62}")
    print(f"  Step DL-10 완료")
    print(f"{'═'*62}\n")


# ══════════════════════════════════════════════════════════════════════════════
# CLI 진입점
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Step DL-10: CryptoMamba 모의투자 백테스트 (롱 전용 돌파 전략)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--project_root",
        type=str,
        default=".",
        help="프로젝트 루트 경로 (artifacts·data 상위 디렉터리). "
             "코랩 등 다른 환경에서 실행 시 지정. (default: 스크립트 기준 자동 탐지)",
    )
    parser.add_argument(
        "--threshold_pct",
        type=float,
        default=0.1,
        help="진입 상위 비율 (소수 표현; 0.1 = 상위 10%%). (default: 0.1)",
    )
    parser.add_argument(
        "--fee",
        type=float,
        default=0.0005,
        help="거래 수수료 (단방향; 왕복은 ×2 자동 적용). (default: 0.0005 = 0.05%%)",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.0005,
        help="슬리피지 비율. (default: 0.0005 = 0.05%%)",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=512,
        help="추론 배치 크기. (default: 512)",
    )
    parser.add_argument(
        "--smoke_test",
        action="store_true",
        help="빠른 검증 모드 (처음 10K행만 사용).",
    )
    args = parser.parse_args()

    # --project_root '.' → 스크립트 위치 기준 자동 감지, 그 외엔 절대경로로 변환
    project_root = (
        _PROJ
        if args.project_root == "."
        else Path(args.project_root).expanduser().resolve()
    )

    main(
        project_root=project_root,
        threshold_pct=args.threshold_pct,
        fee=args.fee,
        slippage=args.slippage,
        batch_size=args.batch_size,
        smoke_test=args.smoke_test,
    )
