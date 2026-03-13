"""
Step DL-6: HFT Feature Engineering
CVD & Multi-Resolution Features for CryptoMamba Pipeline

입력 : data/datasets/btc_1m_dl_2y.parquet   (기존 54-컬럼 데이터셋)
출력 : data/datasets/btc_1m_hft_v2.parquet  (신규 피처 추가, ~73컬럼)

신규 피처 그룹
─────────────────────────────────────────────────────────────
[1] CVD (Cumulative Volume Delta)
    bar_delta      : Kaufman 공식  (2c-h-l)/(h-l+ε) × vol — 봉당 순매수 압력 추정
    cvd_20         : 20봉 롤링 CVD 누적합
    cvd_60         : 60봉 롤링 CVD 누적합
    cvd_slope_5    : cvd_20의 5봉 모멘텀
    cvd_norm       : bar_delta를 60봉 표준편차로 정규화

[2] 다중 타임프레임 EMA (MA는 이미 존재)
    ema15_dist     : (close/ema15 - 1) × 100
    ema60_dist     : (close/ema60 - 1) × 100
    ema_cross_15_60: (ema15 - ema60) / close × 100

[3] 볼린저 밴드 강화 시그널
    bb_upper_break : close > bb_upper → 1, else 0
    bb_lower_break : close < bb_lower → 1, else 0
    bb_squeeze     : bb_width < 25th-pct of rolling 50봉 → 1 (돌파 준비 구간)

[4] 미시구조 피처
    order_imbalance: (2c-h-l)/(h-l+ε) ∈ [-1,1]  (order flow proxy)
    vwap_dev_20    : (close - 20봉 VWAP) / VWAP × 100

[5] 다중 타임프레임 거래량
    vol_ratio_15m  : volume / volume.rolling(15).mean()

[6] OBV 모멘텀
    obv_slope_5    : 5봉 OBV 변화 / (거래량 평균+ε) — 정규화된 OBV 기울기

[7] 타겟 업데이트 (기존 horizon=60 유지 + 1분 예측 추가)
    future_ret_1   : log(close[t+1]/close[t]) — GMADL 회귀 타겟
    target_1m      : 1 if future_ret_1 > fee*2 else 0 — 1분 이진 분류 타겟
─────────────────────────────────────────────────────────────

실행:
  poetry run python scripts/dl/step_dl_6_feature_engineering.py
  poetry run python scripts/dl/step_dl_6_feature_engineering.py --input data/datasets/btc_1m_dl_2y.parquet
"""

from __future__ import annotations

import argparse
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── 경로 상수 ──────────────────────────────────────────────────────────────
_PROJ = Path(__file__).resolve().parents[2]
DEFAULT_INPUT  = _PROJ / "data" / "datasets" / "btc_1m_dl_2y.parquet"
DEFAULT_OUTPUT = _PROJ / "data" / "datasets" / "btc_1m_hft_v2.parquet"

# ── 상수 ───────────────────────────────────────────────────────────────────
FEE_RATE    = 0.0005   # 업비트 슬리피지+수수료 (왕복 0.1%)
EPS         = 1e-9
BB_SQ_WIND  = 50       # 볼린저 스퀴즈 판별 롤링 윈도우
OBV_SLOPE_W = 5        # OBV 기울기 윈도우


# ══════════════════════════════════════════════════════════════════════════════
# 피처 엔지니어링 함수
# ══════════════════════════════════════════════════════════════════════════════

def add_cvd_features(df: pd.DataFrame) -> pd.DataFrame:
    """[1] CVD(Cumulative Volume Delta) 파생 피처.

    Kaufman 공식으로 봉(Bar) 단위 순매수 압력을 추정:
        bar_delta = volume × (2·close - high - low) / (high - low + ε)

    (2c-h-l)/(h-l) ∈ [-1, +1]:
        +1 → 완전 강세봉 (close=high),  -1 → 완전 약세봉 (close=low)

    이 값을 volume으로 가중하면 OBV보다 정교한 매수/매도 압력 추정치.
    """
    c = df["close"]
    h = df["high"]
    lo = df["low"]
    v = df["volume"]

    # 봉당 순매수 압력 (Kaufman CVD proxy)
    bar_delta = v * (2 * c - h - lo) / (h - lo + EPS)

    df["bar_delta"]   = bar_delta
    df["cvd_20"]      = bar_delta.rolling(20).sum()
    df["cvd_60"]      = bar_delta.rolling(60).sum()
    df["cvd_slope_5"] = df["cvd_20"].diff(5)

    # 정규화 (z-score 스케일)
    cvd_std = bar_delta.rolling(60).std()
    df["cvd_norm"] = bar_delta / (cvd_std + EPS)

    return df


def add_ema_features(df: pd.DataFrame) -> pd.DataFrame:
    """[2] 다중 타임프레임 EMA 피처 (기존 SMA와 보완적).

    기존 피처(ma15, ma60 등)는 단순이동평균(SMA).
    EMA는 최근 봉에 더 민감 → HFT 환경에서 더 빠른 반응.
    """
    c = df["close"]

    ema15 = c.ewm(span=15, adjust=False).mean()
    ema60 = c.ewm(span=60, adjust=False).mean()

    df["ema15_dist"]      = (c - ema15) / (ema15 + EPS) * 100
    df["ema60_dist"]      = (c - ema60) / (ema60 + EPS) * 100
    df["ema_cross_15_60"] = (ema15 - ema60) / (c + EPS) * 100

    return df


def add_bollinger_signals(df: pd.DataFrame) -> pd.DataFrame:
    """[3] 볼린저 밴드 강화 시그널.

    기존 피처: bb_upper, bb_lower, bb_pct, bb_width (연속값)
    신규:      bb_upper_break, bb_lower_break (이진 돌파 시그널)
               bb_squeeze (밴드 수축 → 돌파 준비 구간)

    Notes:
        bb_squeeze: bb_width < 25th percentile(rolling 50봉) → 1
        이 구간에서 돌파 시 대형 이동 가능성 높음.
    """
    c = df["close"]

    # 기존 bb_upper / bb_lower 재사용 (이미 존재하는 경우)
    if "bb_upper" in df.columns and "bb_lower" in df.columns:
        bb_upper = df["bb_upper"]
        bb_lower = df["bb_lower"]
    else:
        bb_mid   = c.rolling(20).mean()
        bb_std   = c.rolling(20).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std

    df["bb_upper_break"] = (c > bb_upper).astype(np.float32)
    df["bb_lower_break"] = (c < bb_lower).astype(np.float32)

    if "bb_width" in df.columns:
        bb_width = df["bb_width"]
    else:
        bb_mid   = c.rolling(20).mean()
        bb_std   = c.rolling(20).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bb_width = (bb_upper - bb_lower) / (bb_mid + EPS)

    bb_25pct = bb_width.rolling(BB_SQ_WIND).quantile(0.25)
    df["bb_squeeze"] = (bb_width < bb_25pct).astype(np.float32)

    return df


def add_microstructure_features(df: pd.DataFrame) -> pd.DataFrame:
    """[4] 시장 미시구조(Market Microstructure) 피처.

    order_imbalance: (2c-h-l)/(h-l+ε)
        - bar_delta와 유사하지만 거래량 가중 없이 순수 가격 위치만 반영
        - +1 = 매수 지배, -1 = 매도 지배

    vwap_dev_20: (close - VWAP_20) / VWAP × 100
        - 20봉 VWAP(거래량 가중 평균가) 대비 현재가 편차
        - 양수 = VWAP 위에서 거래 → 단기 고평가 신호
    """
    c  = df["close"]
    h  = df["high"]
    lo = df["low"]
    v  = df["volume"]

    df["order_imbalance"] = (2 * c - h - lo) / (h - lo + EPS)

    typical_price = (h + lo + c) / 3
    vwap_num  = (typical_price * v).rolling(20).sum()
    vwap_denom = v.rolling(20).sum()
    vwap_20   = vwap_num / (vwap_denom + EPS)
    df["vwap_dev_20"] = (c - vwap_20) / (vwap_20 + EPS) * 100

    return df


def add_volume_features(df: pd.DataFrame) -> pd.DataFrame:
    """[5] 다중 타임프레임 거래량 피처.

    vol_ratio_15m: volume / volume.rolling(15).mean()
        - 기존: vol_ratio_5m (5봉), vol_ratio_60m (60봉)
        - 신규: 15봉 — 5분/15분/60분 3단계 피라미드 구성
    """
    v = df["volume"]
    df["vol_ratio_15m"] = v / (v.rolling(15).mean() + EPS)
    return df


def add_obv_features(df: pd.DataFrame) -> pd.DataFrame:
    """[6] OBV 모멘텀 피처.

    OBV(On Balance Volume): close 방향에 따라 거래량 누적
    raw OBV는 절대값이 커서 비교 불가 → 5봉 변화율로 정규화

    obv_slope_5 = OBV.diff(5) / (vol_mean_5 + ε)
    """
    c = df["close"]
    v = df["volume"]

    obv = (np.sign(c.diff()) * v).cumsum()
    vol_mean_5 = v.rolling(5).mean()
    df["obv_slope_5"] = obv.diff(OBV_SLOPE_W) / (vol_mean_5 + EPS)

    return df


def add_regression_target(df: pd.DataFrame, fee_rate: float = FEE_RATE) -> pd.DataFrame:
    """[7] 1분 단위 회귀/분류 타겟 추가.

    future_ret_1: log(close[t+1] / close[t])
        → GMADL 손실 함수의 회귀 타겟 (부호 = 방향, 크기 = magnitude)

    target_1m: 1 if future_ret_1 > fee_rate × 2 else 0
        → 이진 분류 타겟 (수수료 초과 상승만 LONG)

    Notes:
        기존 target (60분 horizon)과 future_ret (60분)은 그대로 유지.
        마지막 1행은 미래 데이터 없으므로 NaN.
    """
    c = df["close"]

    future_ret_1 = np.log(c.shift(-1) / (c + EPS))
    df["future_ret_1"] = future_ret_1
    df["target_1m"]    = (future_ret_1 > fee_rate * 2).astype(np.float32)

    # 마지막 행 NaN 처리 (미래 데이터 없음)
    df.loc[df.index[-1], ["future_ret_1", "target_1m"]] = np.nan

    return df


# ══════════════════════════════════════════════════════════════════════════════
# 전체 파이프라인
# ══════════════════════════════════════════════════════════════════════════════

def build_hft_dataset(
    input_path:  Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
) -> pd.DataFrame:
    """기존 데이터셋에 HFT 피처를 추가하여 v2 데이터셋을 생성."""
    t0 = time.time()

    # ── 로드 ──────────────────────────────────────────────────────────────
    print(f"[Step DL-6] 로드: {input_path.name}")
    df = pd.read_parquet(input_path)
    n_rows_orig, n_cols_orig = df.shape
    print(f"  입력 shape : {df.shape}")
    print(f"  기간       : {df.index[0]} ~ {df.index[-1]}")
    print(f"  기존 컬럼  : {sorted(df.columns.tolist())}\n")

    # ── 피처 추가 (순서 의존성 없음 — 각 함수 독립) ──────────────────────
    print("[Step DL-6] 피처 엔지니어링 실행...")

    df = add_cvd_features(df)
    print("  [1/6] CVD 피처 완료      → bar_delta, cvd_20, cvd_60, cvd_slope_5, cvd_norm")

    df = add_ema_features(df)
    print("  [2/6] EMA 피처 완료      → ema15_dist, ema60_dist, ema_cross_15_60")

    df = add_bollinger_signals(df)
    print("  [3/6] 볼린저 시그널 완료 → bb_upper_break, bb_lower_break, bb_squeeze")

    df = add_microstructure_features(df)
    print("  [4/6] 미시구조 피처 완료 → order_imbalance, vwap_dev_20")

    df = add_volume_features(df)
    print("  [5/6] 거래량 피처 완료   → vol_ratio_15m")

    df = add_obv_features(df)
    print("  [6/6] OBV 피처 완료      → obv_slope_5")

    # ── 타겟 추가 ─────────────────────────────────────────────────────────
    df = add_regression_target(df)
    print("  [+T] 1분 타겟 완료       → future_ret_1, target_1m\n")

    # ── 검증 ──────────────────────────────────────────────────────────────
    new_cols = [c for c in df.columns if c not in pd.read_parquet(input_path).columns]
    print(f"[Step DL-6] 신규 피처 {len(new_cols)}개:")
    for c in sorted(new_cols):
        nan_pct = df[c].isna().mean() * 100
        q25, q50, q75 = df[c].dropna().quantile([0.25, 0.50, 0.75])
        print(f"  {c:<22s}  NaN={nan_pct:5.1f}%  "
              f"Q25={q25:+10.4f}  Q50={q50:+10.4f}  Q75={q75:+10.4f}")

    # ── 저장 ──────────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=True)

    elapsed = time.time() - t0
    print(f"\n[Step DL-6] 저장 완료: {output_path.name}")
    print(f"  출력 shape : {df.shape}  (기존 {n_cols_orig}→신규 {df.shape[1]} 컬럼)")
    print(f"  행 변화    : {n_rows_orig:,} → {len(df):,} (동일)")
    print(f"  파일 크기  : {output_path.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"  소요 시간  : {elapsed:.1f}초")

    # ── 추가 통계 ─────────────────────────────────────────────────────────
    _print_statistics(df)

    return df


def _print_statistics(df: pd.DataFrame) -> None:
    """학습/검증에 유용한 통계 출력."""
    print("\n" + "=" * 60)
    print("데이터셋 통계 요약")
    print("=" * 60)

    # 타겟 비율
    if "target" in df.columns:
        t60 = df["target"].dropna()
        print(f"[기존 target   60분] LONG={t60.mean()*100:.2f}%  "
              f"FLAT={(1-t60.mean())*100:.2f}%  N={len(t60):,}")
    if "target_1m" in df.columns:
        t1m = df["target_1m"].dropna()
        print(f"[신규 target_1m 1분] LONG={t1m.mean()*100:.2f}%  "
              f"FLAT={(1-t1m.mean())*100:.2f}%  N={len(t1m):,}")

    # CVD 상관관계
    if "cvd_20" in df.columns and "future_ret_1" in df.columns:
        valid = df[["cvd_20", "cvd_60", "cvd_slope_5", "future_ret_1"]].dropna()
        corr20   = valid["cvd_20"].corr(valid["future_ret_1"])
        corr60   = valid["cvd_60"].corr(valid["future_ret_1"])
        corr_sl  = valid["cvd_slope_5"].corr(valid["future_ret_1"])
        print(f"\n[CVD vs future_ret_1 상관계수]")
        print(f"  cvd_20:       {corr20:+.6f}")
        print(f"  cvd_60:       {corr60:+.6f}")
        print(f"  cvd_slope_5:  {corr_sl:+.6f}")

    # 미시구조 상관관계
    if "order_imbalance" in df.columns and "future_ret_1" in df.columns:
        valid = df[["order_imbalance", "vwap_dev_20", "future_ret_1"]].dropna()
        corr_oi  = valid["order_imbalance"].corr(valid["future_ret_1"])
        corr_vw  = valid["vwap_dev_20"].corr(valid["future_ret_1"])
        print(f"\n[미시구조 vs future_ret_1 상관계수]")
        print(f"  order_imbalance: {corr_oi:+.6f}")
        print(f"  vwap_dev_20:     {corr_vw:+.6f}")

    # 볼린저 돌파 빈도
    if "bb_upper_break" in df.columns:
        n_up  = int(df["bb_upper_break"].sum())
        n_dn  = int(df["bb_lower_break"].sum())
        n_sq  = int(df["bb_squeeze"].sum())
        n_tot = len(df)
        print(f"\n[볼린저 시그널 발생 빈도]")
        print(f"  상단 돌파 : {n_up:,}회 ({n_up/n_tot*100:.2f}%)")
        print(f"  하단 돌파 : {n_dn:,}회 ({n_dn/n_tot*100:.2f}%)")
        print(f"  스퀴즈 구간: {n_sq:,}행 ({n_sq/n_tot*100:.2f}%)")

    # future_ret_1 분포 (magnitude 분포 — GMADL 가중치에 영향)
    if "future_ret_1" in df.columns:
        fr = df["future_ret_1"].dropna()
        print(f"\n[future_ret_1 분포 (GMADL 타겟)]")
        print(f"  mean  = {fr.mean():+.6f}")
        print(f"  std   = {fr.std():.6f}")
        print(f"  |ret| > 0.001 비율: {(fr.abs() > 0.001).mean()*100:.2f}%  (유의미 이동)")
        print(f"  |ret| > 0.002 비율: {(fr.abs() > 0.002).mean()*100:.2f}%")
        print(f"  |ret| > 0.005 비율: {(fr.abs() > 0.005).mean()*100:.2f}%")

    print("=" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# CLI 진입점
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Step DL-6: HFT Feature Engineering")
    parser.add_argument(
        "--input", type=Path, default=DEFAULT_INPUT,
        help="입력 parquet 경로 (기존 2년 데이터셋)",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="출력 parquet 경로 (v2 데이터셋)",
    )
    args = parser.parse_args()

    build_hft_dataset(input_path=args.input, output_path=args.output)
