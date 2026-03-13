"""
Step DL-1: API 기반 과거 데이터 수집 및 정제
- 업비트 API(pyupbit)로 KRW-BTC 1분봉 OHLCV 수집 (약 6개월치)
- yfinance로 매크로 지표 수집 (DXY, S&P500, Gold)
- 1분 단위로 병합 후 data/datasets/btc_1m_dl.parquet 저장
"""

import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyupbit
import yfinance as yf

warnings.filterwarnings("ignore")

OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "datasets"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = OUT_DIR / "btc_1m_dl.parquet"

# ── 설정 ──────────────────────────────────────────────
TICKER = "KRW-BTC"
INTERVAL = "minute1"
# 업비트 1분봉 API는 1회 200개 반환, 200분 = ~3.3시간
# 6개월 = ~259,200분 → 약 1,296 API 호출 (rate limit 고려 0.5s sleep)
TARGET_DAYS = 180  # 6개월

# ── 업비트 1분봉 수집 ─────────────────────────────────

def fetch_upbit_1m(target_days: int = TARGET_DAYS) -> pd.DataFrame:
    """pyupbit get_ohlcv를 이용해 1분봉을 역순으로 누적 수집.

    주의:
    - pyupbit 반환 인덱스: KST (UTC+9)
    - pyupbit `to` 파라미터: UTC 기준으로 처리됨
    - 따라서 KST 인덱스 → UTC 변환(−9h) 후 `to`에 전달해야 함
    """
    print(f"[Upbit] KRW-BTC 1분봉 수집 시작 (목표 {target_days}일)")

    KST_OFFSET = timedelta(hours=9)

    # to_utc: UTC 기준으로 현재 시각부터 역방향 순회
    to_utc = datetime.utcnow()
    end_utc = to_utc - timedelta(days=target_days)

    all_chunks: list[pd.DataFrame] = []
    call_count = 0
    max_calls = target_days * 1440 // 200 + 50  # 안전 상한

    while to_utc > end_utc and call_count < max_calls:
        try:
            chunk = pyupbit.get_ohlcv(
                TICKER,
                interval=INTERVAL,
                count=200,
                to=to_utc.strftime("%Y-%m-%d %H:%M:%S"),  # UTC 전달
            )
            if chunk is None or chunk.empty:
                print(f"  빈 응답 (to_utc={to_utc}). 종료.")
                break

            all_chunks.append(chunk)
            call_count += 1

            # chunk.index[0] = KST 최초 시각 → UTC 변환
            oldest_kst = chunk.index[0]
            oldest_utc = oldest_kst - KST_OFFSET
            to_utc = oldest_utc - timedelta(minutes=1)

            if call_count % 100 == 0:
                print(f"  호출 {call_count:4d}회 — KST 최초 {oldest_kst.strftime('%Y-%m-%d %H:%M')}")

            time.sleep(0.12)  # 업비트 rate limit 준수

        except Exception as e:
            print(f"  에러: {e!r}, 3초 대기 후 재시도")
            time.sleep(3)

    if not all_chunks:
        raise RuntimeError("업비트 데이터 수집 실패 — 청크가 없습니다.")

    df = pd.concat(all_chunks).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    # 인덱스를 KST 그대로 naive로 유지 (downstream에서 KST 사용)
    print(f"[Upbit] 수집 완료: {len(df):,}행  ({df.index[0]} ~ {df.index[-1]})")
    return df


# ── 기술적 피처 생성 ──────────────────────────────────

def make_tech_features(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV 기반 기술 지표 생성. 미래 참조 없음."""
    c = df["close"].copy()
    h = df["high"].copy()
    lo = df["low"].copy()
    v = df["volume"].copy()

    out = df[["open", "high", "low", "close", "volume"]].copy()

    # 수익률
    out["ret_1m"] = c.pct_change()
    out["ret_5m"] = c.pct_change(5)
    out["ret_15m"] = c.pct_change(15)
    out["ret_60m"] = c.pct_change(60)

    # 이동평균
    for w in [5, 15, 30, 60, 120, 240]:
        out[f"ma{w}"] = c.rolling(w).mean()
        out[f"ma{w}_dist"] = (c - out[f"ma{w}"]) / out[f"ma{w}"]

    # 변동성
    out["vol_5m"] = out["ret_1m"].rolling(5).std()
    out["vol_15m"] = out["ret_1m"].rolling(15).std()
    out["vol_60m"] = out["ret_1m"].rolling(60).std()

    # 거래량 피처
    out["vol_ratio_5m"] = v / v.rolling(5).mean()
    out["vol_ratio_60m"] = v / v.rolling(60).mean()
    out["vol_std_15m"] = v.rolling(15).std() / v.rolling(15).mean()

    # RSI (14)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    out["rsi14"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    # Bollinger Band (20)
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    out["bb_upper"] = bb_mid + 2 * bb_std
    out["bb_lower"] = bb_mid - 2 * bb_std
    out["bb_pct"] = (c - out["bb_lower"]) / (out["bb_upper"] - out["bb_lower"] + 1e-9)
    out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / (bb_mid + 1e-9)

    # ATR (14)
    tr = pd.concat(
        [h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1
    ).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    out["atr_norm"] = out["atr14"] / (c + 1e-9)

    # 고가/저가 채널
    out["channel_high_20"] = h.rolling(20).max()
    out["channel_low_20"] = lo.rolling(20).min()
    out["channel_pos"] = (c - out["channel_low_20"]) / (
        out["channel_high_20"] - out["channel_low_20"] + 1e-9
    )

    # 시간 피처 (sine/cosine 인코딩)
    idx = pd.DatetimeIndex(out.index)
    out["hour_sin"] = np.sin(2 * np.pi * idx.hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * idx.hour / 24)
    out["dow_sin"] = np.sin(2 * np.pi * idx.dayofweek / 7)
    out["dow_cos"] = np.cos(2 * np.pi * idx.dayofweek / 7)

    return out


# ── yfinance 매크로 지표 수집 ─────────────────────────

def fetch_macro(start: str, end: str) -> pd.DataFrame:
    """DXY(달러 인덱스), ^GSPC(S&P500), GC=F(Gold) 일봉 수집 후 forward-fill."""
    tickers = {
        "DX-Y.NYB": "dxy",
        "^GSPC": "sp500",
        "GC=F": "gold",
        "BTC-USD": "btc_usd",  # 글로벌 BTC 가격 참조
    }
    frames: dict[str, pd.Series] = {}
    for yf_ticker, col_name in tickers.items():
        try:
            raw = yf.download(
                yf_ticker,
                start=start,
                end=end,
                interval="1d",
                auto_adjust=True,
                progress=False,
            )
            if raw.empty:
                print(f"  [Macro] {yf_ticker} 빈 응답 — 스킵")
                continue
            close_col = raw["Close"]
            if isinstance(close_col, pd.DataFrame):
                close_col = close_col.iloc[:, 0]
            s = close_col.squeeze()
            s.index = pd.to_datetime(s.index).tz_localize(None)
            # 수익률 및 level 모두 보관
            frames[col_name] = s
            frames[f"{col_name}_ret"] = s.pct_change()
            print(f"  [Macro] {yf_ticker}: {len(s)}행")
        except Exception as e:
            print(f"  [Macro] {yf_ticker} 에러: {e}")

    if not frames:
        return pd.DataFrame()

    macro_df = pd.DataFrame(frames)
    macro_df.index = pd.to_datetime(macro_df.index).normalize()
    return macro_df


# ── 병합 ─────────────────────────────────────────────

def merge_macro(ohlcv: pd.DataFrame, macro: pd.DataFrame) -> pd.DataFrame:
    """1분봉에 일봉 매크로 데이터를 날짜 기준 forward-fill 병합."""
    if macro.empty:
        return ohlcv

    # 1분봉 index → 날짜 컬럼
    ohlcv_daily = ohlcv.copy()
    ohlcv_daily["_date"] = pd.to_datetime(ohlcv_daily.index).normalize()

    macro_reset = macro.reset_index().rename(columns={"index": "_date", "Date": "_date"})
    macro_reset["_date"] = pd.to_datetime(macro_reset["_date"]).dt.normalize()

    merged = ohlcv_daily.reset_index().merge(macro_reset, on="_date", how="left").set_index(
        ohlcv_daily.index.name or "index"
    )
    merged = merged.drop(columns=["_date"])
    merged.index = ohlcv_daily.index

    # 매크로 컬럼 forward-fill
    macro_cols = list(macro.columns)
    merged[macro_cols] = merged[macro_cols].ffill()

    return merged


# ── 타겟 생성 ─────────────────────────────────────────

def make_target(df: pd.DataFrame, horizon: int = 60, fee_rate: float = 0.001) -> pd.DataFrame:
    """
    horizon분 후 가격 변화율 기반 이진 분류 타겟.
    - future_ret = close(t+horizon) / close(t) - 1
    - target = 1 (LONG)  if future_ret > fee_rate * 2  (왕복 수수료 초과 상승)
    - target = 0 (FLAT)  otherwise
    """
    out = df.copy()
    future_close = out["close"].shift(-horizon)
    future_ret = future_close / out["close"] - 1
    out["future_ret"] = future_ret
    out["target"] = (future_ret > fee_rate * 2).astype(int)
    # 마지막 horizon행은 미래 정보 없으므로 NaN → 제거는 학습 시 처리
    out.loc[out.index[-horizon:], ["future_ret", "target"]] = np.nan
    return out


# ── 메인 ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Step DL-1: 업비트 1분봉 + 매크로 데이터 수집 파이프라인")
    print("=" * 60)

    # 1. 업비트 1분봉 수집
    raw = fetch_upbit_1m(TARGET_DAYS)

    # 2. 기술 피처 생성
    print("\n[Feature] 기술 지표 생성 중...")
    feat = make_tech_features(raw)
    print(f"  피처 수: {feat.shape[1]}개 (OHLCV 포함)")

    # 3. yfinance 매크로 수집
    start_str = feat.index[0].strftime("%Y-%m-%d")
    end_str = (feat.index[-1] + timedelta(days=2)).strftime("%Y-%m-%d")
    print(f"\n[Macro] 매크로 데이터 수집 ({start_str} ~ {end_str})")
    macro = fetch_macro(start_str, end_str)

    # 4. 병합
    print("\n[Merge] 1분봉 + 매크로 병합...")
    merged = merge_macro(feat, macro)
    print(f"  병합 후 shape: {merged.shape}")

    # 5. 타겟 생성 (60분 후 LONG vs FLAT)
    print("\n[Target] 60분 후 타겟 생성 (fee=0.1%×2)...")
    final = make_target(merged, horizon=60, fee_rate=0.0005)

    # 6. 기초 통계
    valid = final.dropna(subset=["target"])
    print(f"\n[Stats] 유효 행 수: {len(valid):,}")
    print(f"  LONG(1) 비율: {valid['target'].mean():.3f}")
    print(f"  FLAT(0) 비율: {1 - valid['target'].mean():.3f}")
    print(f"  기간: {valid.index[0]} ~ {valid.index[-1]}")
    print(f"  피처 컬럼 수: {len(final.columns)}")

    # 7. 저장
    final.to_parquet(OUT_PATH, index=True)
    print(f"\n[Save] 저장 완료: {OUT_PATH}")
    print(f"  파일 크기: {OUT_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"  전체 행 수: {len(final):,} / 유효(target 존재): {len(valid):,}")
    print("\n피처 컬럼 목록:")
    print(list(final.columns))


if __name__ == "__main__":
    main()
