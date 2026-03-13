"""
Step DL-1 (개정판): API 기반 과거 데이터 수집 및 정제
- 업비트 API(pyupbit)로 KRW-BTC 1분봉 OHLCV 수집
- yfinance로 매크로 지표 수집 (DXY, S&P500, Gold, BTC-USD)
- 1분 단위로 병합 후 parquet 저장

출력 파일:
  6개월: data/datasets/btc_1m_dl.parquet
  2년  : data/datasets/btc_1m_dl_2y.parquet  (기본값)

실행 예시:
  poetry run python scripts/dl/step_dl_1_collect_data.py           # 2년(730일)
  poetry run python scripts/dl/step_dl_1_collect_data.py --days 180 # 6개월
  poetry run python scripts/dl/step_dl_1_collect_data.py --resume   # 체크포인트 재개
"""

import argparse
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyupbit
import yfinance as yf

warnings.filterwarnings("ignore")

# ── 경로 상수 ──────────────────────────────────────────────────────────────
OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "datasets"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CKPT_DIR = Path(__file__).resolve().parents[2] / "data" / ".checkpoints"
CKPT_DIR.mkdir(parents=True, exist_ok=True)

# ── 설정 ───────────────────────────────────────────────────────────────────
TICKER = "KRW-BTC"
INTERVAL = "minute1"
TARGET_DAYS_DEFAULT = 730        # 기본값: 2년
CHUNK_SIZE = 200                 # 업비트 1회 최대 반환 수
SLEEP_SEC = 0.13                 # rate limit 준수 (업비트 ~8req/s)
SLEEP_ON_ERROR_SEC = 5           # 에러 발생 시 대기
CHECKPOINT_EVERY = 300           # N 청크마다 체크포인트 저장


def _out_path(days: int) -> Path:
    if days <= 200:
        return OUT_DIR / "btc_1m_dl.parquet"
    return OUT_DIR / f"btc_1m_dl_2y.parquet"


def _ckpt_path(days: int) -> Path:
    return CKPT_DIR / f"upbit_1m_{days}d_ckpt.parquet"


# ══════════════════════════════════════════════════════════════════════════════
# 1. 업비트 1분봉 수집 (체크포인트/재개 지원)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_upbit_1m(target_days: int = TARGET_DAYS_DEFAULT, resume: bool = False) -> pd.DataFrame:
    """pyupbit get_ohlcv를 이용해 1분봉을 역순으로 누적 수집.

    Args:
        target_days: 수집 목표 일수
        resume: True면 기존 체크포인트에서 재개

    Notes:
        - pyupbit 반환 인덱스: KST (UTC+9)
        - `to` 파라미터는 UTC 기준 → KST 인덱스를 UTC로 변환 후 전달
        - CHECKPOINT_EVERY 청크마다 중간 저장 → 인터럽트 후 재개 가능
    """
    KST_OFFSET = timedelta(hours=9)
    ckpt = _ckpt_path(target_days)

    # ── 재개 모드: 기존 체크포인트 로드 ────────────────────────────────────
    saved_chunks: list[pd.DataFrame] = []
    to_utc = datetime.utcnow()
    end_utc = to_utc - timedelta(days=target_days)

    if resume and ckpt.exists():
        print(f"[Upbit] 체크포인트 발견 — 재개 모드: {ckpt}")
        prev = pd.read_parquet(ckpt)
        saved_chunks.append(prev)
        # 가장 오래된 시각(KST) → UTC 변환 후 그 이전부터 수집
        oldest_kst = prev.index[0]
        oldest_utc = oldest_kst - KST_OFFSET
        to_utc = oldest_utc - timedelta(minutes=1)
        print(f"  저장된 데이터: {len(prev):,}행  ({prev.index[-1]} ~ {prev.index[0]})")
        print(f"  재개 시작점: {to_utc} (UTC)")
    else:
        if resume:
            print("[Upbit] 체크포인트 없음 — 처음부터 수집")

    total_target_mins = target_days * 1440
    total_target_calls = total_target_mins // CHUNK_SIZE + 50
    est_min = total_target_calls * SLEEP_SEC / 60

    print(f"[Upbit] KRW-BTC 1분봉 수집 시작 (목표 {target_days}일 = {total_target_mins:,}분)")
    print(f"  예상 API 호출: ~{total_target_calls:,}회  예상 소요: ~{est_min:.0f}분")

    all_chunks: list[pd.DataFrame] = []
    call_count = 0
    max_calls = total_target_calls

    while to_utc > end_utc and call_count < max_calls:
        try:
            chunk = pyupbit.get_ohlcv(
                TICKER,
                interval=INTERVAL,
                count=CHUNK_SIZE,
                to=to_utc.strftime("%Y-%m-%d %H:%M:%S"),
            )
            if chunk is None or chunk.empty:
                print(f"  빈 응답 (to_utc={to_utc}). 수집 한계 도달.")
                break

            all_chunks.append(chunk)
            call_count += 1

            oldest_kst = chunk.index[0]
            oldest_utc = oldest_kst - KST_OFFSET
            to_utc = oldest_utc - timedelta(minutes=1)

            # 진행률 출력
            if call_count % 100 == 0:
                elapsed_days = (datetime.utcnow() - (oldest_utc)).days
                progress_pct = min(elapsed_days / target_days * 100, 100)
                print(
                    f"  호출 {call_count:5d}회  KST={oldest_kst.strftime('%Y-%m-%d %H:%M')}"
                    f"  진행 {progress_pct:.1f}%  청크 {len(all_chunks):,}개"
                )

            # 체크포인트 저장
            if call_count % CHECKPOINT_EVERY == 0 and all_chunks:
                _save_checkpoint(all_chunks, saved_chunks, ckpt)

            time.sleep(SLEEP_SEC)

        except KeyboardInterrupt:
            print("\n[Upbit] 사용자 중단 — 체크포인트 저장 후 종료")
            _save_checkpoint(all_chunks, saved_chunks, ckpt)
            raise

        except Exception as e:
            print(f"  에러: {e!r} — {SLEEP_ON_ERROR_SEC}초 후 재시도")
            time.sleep(SLEEP_ON_ERROR_SEC)

    if not all_chunks and not saved_chunks:
        raise RuntimeError("업비트 데이터 수집 실패 — 청크가 없습니다.")

    # ── 최종 병합 ─────────────────────────────────────────────────────────
    all_frames = saved_chunks + all_chunks
    df = pd.concat(all_frames).sort_index()
    df = df[~df.index.duplicated(keep="first")]

    # 체크포인트 정리
    if ckpt.exists():
        ckpt.unlink()
        print(f"  체크포인트 삭제: {ckpt}")

    print(f"[Upbit] 수집 완료: {len(df):,}행  ({df.index[0]} ~ {df.index[-1]})")
    return df


def _save_checkpoint(
    new_chunks: list[pd.DataFrame],
    saved_chunks: list[pd.DataFrame],
    ckpt: Path,
) -> None:
    """현재까지 수집된 모든 청크를 체크포인트 파일로 저장."""
    all_frames = saved_chunks + new_chunks
    if not all_frames:
        return
    df_ckpt = pd.concat(all_frames).sort_index()
    df_ckpt = df_ckpt[~df_ckpt.index.duplicated(keep="first")]
    df_ckpt.to_parquet(ckpt, index=True)
    print(f"  [Checkpoint] {len(df_ckpt):,}행 저장 → {ckpt.name}")


# ══════════════════════════════════════════════════════════════════════════════
# 2. 기술 지표 생성 (미래 참조 없음 — 학습/실시간 공용)
# ══════════════════════════════════════════════════════════════════════════════

def make_tech_features(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV → 44개 기술 지표 생성."""
    c = df["close"].copy()
    h = df["high"].copy()
    lo = df["low"].copy()
    v = df["volume"].copy()

    out = df[["open", "high", "low", "close", "volume"]].copy()

    out["ret_1m"] = c.pct_change()
    out["ret_5m"] = c.pct_change(5)
    out["ret_15m"] = c.pct_change(15)
    out["ret_60m"] = c.pct_change(60)

    for w in [5, 15, 30, 60, 120, 240]:
        out[f"ma{w}"] = c.rolling(w).mean()
        out[f"ma{w}_dist"] = (c - out[f"ma{w}"]) / out[f"ma{w}"]

    out["vol_5m"] = out["ret_1m"].rolling(5).std()
    out["vol_15m"] = out["ret_1m"].rolling(15).std()
    out["vol_60m"] = out["ret_1m"].rolling(60).std()

    out["vol_ratio_5m"] = v / v.rolling(5).mean()
    out["vol_ratio_60m"] = v / v.rolling(60).mean()
    out["vol_std_15m"] = v.rolling(15).std() / v.rolling(15).mean()

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    out["rsi14"] = 100 - (100 / (1 + gain / (loss + 1e-9)))

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    out["bb_upper"] = bb_mid + 2 * bb_std
    out["bb_lower"] = bb_mid - 2 * bb_std
    out["bb_pct"] = (c - out["bb_lower"]) / (out["bb_upper"] - out["bb_lower"] + 1e-9)
    out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / (bb_mid + 1e-9)

    tr = pd.concat(
        [h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1
    ).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    out["atr_norm"] = out["atr14"] / (c + 1e-9)

    out["channel_high_20"] = h.rolling(20).max()
    out["channel_low_20"] = lo.rolling(20).min()
    out["channel_pos"] = (c - out["channel_low_20"]) / (
        out["channel_high_20"] - out["channel_low_20"] + 1e-9
    )

    idx = pd.DatetimeIndex(out.index)
    out["hour_sin"] = np.sin(2 * np.pi * idx.hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * idx.hour / 24)
    out["dow_sin"] = np.sin(2 * np.pi * idx.dayofweek / 7)
    out["dow_cos"] = np.cos(2 * np.pi * idx.dayofweek / 7)

    return out


# ══════════════════════════════════════════════════════════════════════════════
# 3. yfinance 매크로 지표 수집
# ══════════════════════════════════════════════════════════════════════════════

def fetch_macro(start: str, end: str) -> pd.DataFrame:
    """DXY, S&P500, Gold, BTC-USD 일봉 수집 후 수익률 컬럼 추가."""
    tickers = {
        "DX-Y.NYB": "dxy",
        "^GSPC": "sp500",
        "GC=F": "gold",
        "BTC-USD": "btc_usd",
    }
    frames: dict[str, pd.Series] = {}

    for yf_ticker, col_name in tickers.items():
        for attempt in range(3):
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
                    break
                close_col = raw["Close"]
                if isinstance(close_col, pd.DataFrame):
                    close_col = close_col.iloc[:, 0]
                s = close_col.squeeze()
                s.index = pd.to_datetime(s.index).tz_localize(None)
                frames[col_name] = s
                frames[f"{col_name}_ret"] = s.pct_change()
                print(f"  [Macro] {yf_ticker}: {len(s)}행")
                break
            except Exception as e:
                if attempt < 2:
                    print(f"  [Macro] {yf_ticker} 에러(재시도 {attempt+1}/3): {e}")
                    time.sleep(3)
                else:
                    print(f"  [Macro] {yf_ticker} 최종 실패: {e}")

    if not frames:
        return pd.DataFrame()

    macro_df = pd.DataFrame(frames)
    macro_df.index = pd.to_datetime(macro_df.index).normalize()
    return macro_df


# ══════════════════════════════════════════════════════════════════════════════
# 4. 병합 및 타겟 생성
# ══════════════════════════════════════════════════════════════════════════════

def merge_macro(ohlcv: pd.DataFrame, macro: pd.DataFrame) -> pd.DataFrame:
    """1분봉에 일봉 매크로 데이터를 날짜 기준 forward-fill 병합."""
    if macro.empty:
        return ohlcv

    ohlcv_daily = ohlcv.copy()
    ohlcv_daily["_date"] = pd.to_datetime(ohlcv_daily.index).normalize()

    macro_reset = macro.reset_index().rename(columns={"index": "_date", "Date": "_date"})
    macro_reset["_date"] = pd.to_datetime(macro_reset["_date"]).dt.normalize()

    merged = (
        ohlcv_daily.reset_index()
        .merge(macro_reset, on="_date", how="left")
        .set_index(ohlcv_daily.index.name or "index")
    )
    merged = merged.drop(columns=["_date"])
    merged.index = ohlcv_daily.index

    macro_cols = list(macro.columns)
    merged[macro_cols] = merged[macro_cols].ffill()

    return merged


def make_target(
    df: pd.DataFrame,
    horizon: int = 60,
    fee_rate: float = 0.0005,
) -> pd.DataFrame:
    """horizon분 후 가격 변화율 기반 이진 분류 타겟.

    target = 1 (LONG)  if future_ret > fee_rate × 2  (왕복 수수료 초과 상승)
    target = 0 (FLAT)  otherwise
    """
    out = df.copy()
    future_close = out["close"].shift(-horizon)
    future_ret = future_close / out["close"] - 1
    out["future_ret"] = future_ret
    out["target"] = (future_ret > fee_rate * 2).astype(int)
    out.loc[out.index[-horizon:], ["future_ret", "target"]] = np.nan
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 5. 메인
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step DL-1: 업비트 1분봉 + 매크로 데이터 수집")
    p.add_argument("--days", type=int, default=TARGET_DAYS_DEFAULT,
                   help=f"수집 일수 (기본: {TARGET_DAYS_DEFAULT}일 = 2년)")
    p.add_argument("--resume", action="store_true",
                   help="체크포인트에서 재개")
    return p.parse_args()


def main():
    args = parse_args()
    target_days = args.days
    out_path = _out_path(target_days)

    print("=" * 65)
    print(f"Step DL-1: 업비트 1분봉 + 매크로 수집 ({target_days}일)")
    print("=" * 65)
    print(f"  출력 경로: {out_path}")
    print(f"  체크포인트 재개: {args.resume}")

    t_start = time.time()

    # ── 1. 업비트 1분봉 수집 ──────────────────────────────────────────────
    raw = fetch_upbit_1m(target_days, resume=args.resume)

    # ── 2. 기술 피처 생성 ─────────────────────────────────────────────────
    print("\n[Feature] 기술 지표 생성 중...")
    feat = make_tech_features(raw)
    print(f"  피처 수: {feat.shape[1]}개 (OHLCV 포함)")

    # ── 3. 매크로 수집 ────────────────────────────────────────────────────
    start_str = feat.index[0].strftime("%Y-%m-%d")
    end_str = (feat.index[-1] + timedelta(days=2)).strftime("%Y-%m-%d")
    print(f"\n[Macro] 매크로 데이터 수집 ({start_str} ~ {end_str})")
    macro = fetch_macro(start_str, end_str)

    # ── 4. 병합 ───────────────────────────────────────────────────────────
    print("\n[Merge] 1분봉 + 매크로 병합...")
    merged = merge_macro(feat, macro)
    print(f"  병합 후 shape: {merged.shape}")

    # ── 5. 타겟 생성 ──────────────────────────────────────────────────────
    print("\n[Target] 60분 후 타겟 생성 (fee=0.1%×2)...")
    final = make_target(merged, horizon=60, fee_rate=0.0005)

    # ── 6. 기초 통계 ──────────────────────────────────────────────────────
    valid = final.dropna(subset=["target"])
    print(f"\n[Stats]")
    print(f"  전체 행 수   : {len(final):,}")
    print(f"  유효 행 수   : {len(valid):,}  (target 존재)")
    print(f"  LONG(1) 비율 : {valid['target'].mean():.4f}")
    print(f"  FLAT(0) 비율 : {1 - valid['target'].mean():.4f}")
    print(f"  시작일       : {valid.index[0]}")
    print(f"  종료일       : {valid.index[-1]}")
    print(f"  피처 컬럼 수 : {len(final.columns)}개")

    # ── 7. 저장 ───────────────────────────────────────────────────────────
    final.to_parquet(out_path, index=True)
    size_mb = out_path.stat().st_size / 1024 / 1024
    elapsed_min = (time.time() - t_start) / 60

    print(f"\n[Save] 저장 완료: {out_path}")
    print(f"  파일 크기    : {size_mb:.1f} MB")
    print(f"  총 소요 시간 : {elapsed_min:.1f}분")

    # ── 8. Colab 업로드 안내 ─────────────────────────────────────────────
    print("\n[Colab 업로드 안내]")
    print(f"  1. {out_path.name} → Google Drive/MyDrive/btc_quant/ 에 업로드")
    print(f"  2. scripts/dl/colab_tcn_train.ipynb → Colab에서 열기")
    print(f"  3. DRIVE_DATA_PATH 셀에서 경로 확인 후 전체 실행 (GPU 런타임 권장)")

    print(f"\n→ 다음 단계: Colab에서 colab_tcn_train.ipynb 실행")


if __name__ == "__main__":
    main()
