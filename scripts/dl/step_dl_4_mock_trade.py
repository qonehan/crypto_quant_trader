"""
scripts/dl/step_dl_4_mock_trade.py

Step DL-4: 1D-CNN + LSTM 스나이퍼 모델 실시간 모의투자 루프

흐름:
  1. 모델 / 스케일러 / 피처 목록 로드
  2. 매분 정각 직후 업비트 1분봉 320개 수집 (ma240 계산에 충분한 버퍼)
  3. make_tech_features() + 매크로 피처 ffill → 마지막 60행(SEQ_LEN) 추출
  4. transform_realtime() 로 스케일링 → 텐서 → 모델 추론
  5. prob ≥ THRESHOLD → [MOCK BUY], 보유 중 EXIT 조건 → [MOCK SELL]

부가 기능:
  - 로그 파일: logs/mock_trade_YYYYMMDD.log (터미널+파일 동시 출력)
  - 거래 이력: artifacts/trade_history.csv (BUY/SELL 이벤트마다 추가)
  - 재시도:   pyupbit/yfinance 네트워크 오류 시 5초 대기 후 최대 3회 재시도
  - 하트비트: 매 10분마다 정상 작동 확인 로그 출력

실행:
    poetry run python scripts/dl/step_dl_4_mock_trade.py
"""

from __future__ import annotations

import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyupbit
import torch
import yfinance as yf
from dotenv import load_dotenv

# ── 프로젝트 루트 ───────────────────────────────────────────────────────────
_PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJ_ROOT))

from app.predictor.dl_dataset import SEQ_LEN, transform_realtime
from app.predictor.dl_model import LSTMClassifier

load_dotenv(_PROJ_ROOT / ".env")

# ── 상수 ────────────────────────────────────────────────────────────────────
TICKER = "KRW-BTC"
THRESHOLD = 0.55
STOP_LOSS_PCT = -0.005      # -0.5%
EXIT_PROB_FLOOR = 0.45
FETCH_COUNT = 320
MACRO_REFRESH_HOURS = 12
HEARTBEAT_SEC = 600         # 10분
RETRY_MAX = 3
RETRY_WAIT_SEC = 5

ARTIFACT_DIR = _PROJ_ROOT / "artifacts" / "dl_prod"
MODEL_PATH = ARTIFACT_DIR / "lstm_model.pt"
MODEL_META_PATH = ARTIFACT_DIR / "dl_model_meta.json"
SCALER_PATH = ARTIFACT_DIR / "scaler.joblib"
FEATURE_COLS_PATH = ARTIFACT_DIR / "feature_cols.json"
TRADE_CSV_PATH = _PROJ_ROOT / "artifacts" / "trade_history.csv"

LOG_DIR = _PROJ_ROOT / "logs"
DEVICE = "cpu"

# ── 모듈 레벨 logger (setup_logger() 호출 전까지 basicConfig 수준) ──────────
logger: logging.Logger = logging.getLogger("mock_trade")


# ══════════════════════════════════════════════════════════════════════════════
# 1. 로거 설정
# ══════════════════════════════════════════════════════════════════════════════

def setup_logger() -> logging.Logger:
    """터미널 + 날짜별 파일에 동시 기록하는 로거 초기화."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    log_file = LOG_DIR / f"mock_trade_{today}.log"

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    lg = logging.getLogger("mock_trade")
    lg.setLevel(logging.DEBUG)
    lg.handlers.clear()

    # 콘솔
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    lg.addHandler(sh)

    # 파일 (append 모드 — 재시작해도 누적)
    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    lg.addHandler(fh)

    lg.info(f"로그 파일: {log_file}")
    return lg


# ══════════════════════════════════════════════════════════════════════════════
# 2. 거래 이력 CSV
# ══════════════════════════════════════════════════════════════════════════════

_TRADE_CSV_HEADER = [
    "timestamp", "action",
    "entry_time", "entry_price", "entry_prob",
    "exit_time", "exit_price", "exit_prob",
    "ret_pct", "held_min", "reason",
    "cum_trade_count", "cum_pnl_pct",
]


def _ensure_trade_csv() -> None:
    """CSV 파일이 없으면 헤더 행만 생성."""
    TRADE_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TRADE_CSV_PATH.exists():
        with open(TRADE_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_TRADE_CSV_HEADER)
        logger.info(f"거래 이력 CSV 생성: {TRADE_CSV_PATH}")


def append_buy_csv(
    ts: datetime,
    price: float,
    prob: float,
) -> None:
    """BUY 이벤트 기록 (청산 정보는 SELL 시 채워짐 — 별도 행으로 관리)."""
    with open(TRADE_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            ts.strftime("%Y-%m-%d %H:%M:%S"), "BUY",
            ts.strftime("%Y-%m-%d %H:%M:%S"), f"{price:.0f}", f"{prob:.4f}",
            "", "", "",
            "", "", "",
            "", "",
        ])


def append_sell_csv(
    ts: datetime,
    entry_time: datetime,
    entry_price: float,
    entry_prob: float,
    exit_price: float,
    exit_prob: float,
    ret_pct: float,
    held_min: int,
    reason: str,
    cum_count: int,
    cum_pnl: float,
) -> None:
    """SELL 이벤트 기록 (왕복 정보 한 줄)."""
    with open(TRADE_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            ts.strftime("%Y-%m-%d %H:%M:%S"), "SELL",
            entry_time.strftime("%Y-%m-%d %H:%M:%S"), f"{entry_price:.0f}", f"{entry_prob:.4f}",
            ts.strftime("%Y-%m-%d %H:%M:%S"), f"{exit_price:.0f}", f"{exit_prob:.4f}",
            f"{ret_pct:+.4f}", held_min, reason,
            cum_count, f"{cum_pnl:+.4f}",
        ])


# ══════════════════════════════════════════════════════════════════════════════
# 3. 재시도 래퍼
# ══════════════════════════════════════════════════════════════════════════════

def call_with_retry(fn, *args, label: str = "", **kwargs):
    """fn(*args, **kwargs) 를 최대 RETRY_MAX 회 재시도.

    각 실패마다 RETRY_WAIT_SEC 초 대기. 모두 실패하면 None 반환.
    """
    for attempt in range(1, RETRY_MAX + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.warning(
                f"[Retry {attempt}/{RETRY_MAX}] {label} 오류: {e!r}"
                + (f" — {RETRY_WAIT_SEC}초 후 재시도" if attempt < RETRY_MAX else " — 재시도 포기")
            )
            if attempt < RETRY_MAX:
                time.sleep(RETRY_WAIT_SEC)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 4. 기술 지표 생성 (step_dl_1의 make_tech_features 동일 로직)
# ══════════════════════════════════════════════════════════════════════════════

def make_tech_features(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV → 44개 기술 지표 생성 (매크로 피처 제외)."""
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
    rs = gain / (loss + 1e-9)
    out["rsi14"] = 100 - (100 / (1 + rs))

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
# 5. 매크로 데이터 조회
# ══════════════════════════════════════════════════════════════════════════════

MACRO_TICKERS = {
    "DX-Y.NYB": ("dxy", "dxy_ret"),
    "^GSPC":    ("sp500", "sp500_ret"),
    "GC=F":     ("gold", "gold_ret"),
    "BTC-USD":  ("btc_usd", "btc_usd_ret"),
}


def _fetch_macro_once() -> dict[str, float]:
    """yfinance로 최근 10일 일봉의 마지막 유효값 반환 (재시도 없이 1회)."""
    end = datetime.utcnow() + timedelta(days=1)
    start = end - timedelta(days=10)
    result: dict[str, float] = {}

    for yf_ticker, (col, ret_col) in MACRO_TICKERS.items():
        try:
            raw = yf.download(
                yf_ticker,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=True,
                progress=False,
            )
            if raw.empty:
                result[col] = float("nan")
                result[ret_col] = float("nan")
                continue
            close_series = raw["Close"]
            if isinstance(close_series, pd.DataFrame):
                close_series = close_series.iloc[:, 0]
            vals = close_series.dropna()
            if len(vals) < 2:
                result[col] = float("nan")
                result[ret_col] = float("nan")
                continue
            last_val = float(vals.iloc[-1])
            prev_val = float(vals.iloc[-2])
            result[col] = last_val
            result[ret_col] = (last_val / prev_val - 1) if prev_val != 0 else 0.0
        except Exception as e:
            logger.warning(f"[Macro] {yf_ticker} 개별 오류: {e!r}")
            result[col] = float("nan")
            result[ret_col] = float("nan")

    return result


def fetch_macro_latest() -> dict[str, float] | None:
    """재시도 포함 매크로 조회. 실패 시 None."""
    result = call_with_retry(_fetch_macro_once, label="Macro(yfinance)")
    if result is not None:
        logger.info(
            f"[Macro] DXY={result.get('dxy', float('nan')):.2f}  "
            f"SP500={result.get('sp500', float('nan')):.1f}  "
            f"Gold={result.get('gold', float('nan')):.1f}  "
            f"BTC-USD={result.get('btc_usd', float('nan')):.0f}"
        )
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 6. 업비트 1분봉 수집
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_ohlcv_once(count: int) -> pd.DataFrame:
    """업비트 KRW-BTC 1분봉 조회 (예외를 그대로 raise)."""
    df = pyupbit.get_ohlcv(TICKER, interval="minute1", count=count)
    if df is None or df.empty:
        raise ValueError("업비트 빈 응답")
    return df.sort_index()


def fetch_ohlcv(count: int = FETCH_COUNT) -> pd.DataFrame | None:
    """재시도 포함 1분봉 조회. 실패 시 None."""
    return call_with_retry(_fetch_ohlcv_once, count, label="Upbit OHLCV")


# ══════════════════════════════════════════════════════════════════════════════
# 7. 피처 윈도우 생성
# ══════════════════════════════════════════════════════════════════════════════

def build_window(
    ohlcv: pd.DataFrame,
    macro: dict[str, float],
    feature_cols: list[str],
) -> pd.DataFrame | None:
    feat = make_tech_features(ohlcv)

    for col in ["dxy", "dxy_ret", "sp500", "sp500_ret", "gold", "gold_ret", "btc_usd", "btc_usd_ret"]:
        feat[col] = macro.get(col, float("nan"))

    feat[feature_cols] = feat[feature_cols].ffill().bfill()
    window = feat[feature_cols].iloc[-SEQ_LEN:]

    if len(window) < SEQ_LEN:
        logger.warning(f"[Window] 행 수 부족: {len(window)} < {SEQ_LEN}")
        return None
    if window.isnull().any().any():
        nan_cols = window.columns[window.isnull().any()].tolist()
        logger.warning(f"[Window] NaN 잔존 컬럼: {nan_cols[:5]}")
        return None

    return window


# ══════════════════════════════════════════════════════════════════════════════
# 8. 모의 포지션
# ══════════════════════════════════════════════════════════════════════════════

class MockPosition:
    """가상 롱 포지션 추적 + 로그/CSV 기록."""

    def __init__(self) -> None:
        self.in_position: bool = False
        self.entry_price: float = 0.0
        self.entry_time: datetime | None = None
        self.entry_prob: float = 0.0
        self.trade_count: int = 0
        self.total_pnl_pct: float = 0.0

    def open(self, price: float, prob: float, ts: datetime) -> None:
        self.in_position = True
        self.entry_price = price
        self.entry_prob = prob
        self.entry_time = ts
        logger.info(
            f"██ [MOCK BUY]  가격={price:,.0f}원  확률={prob:.4f}"
        )
        append_buy_csv(ts, price, prob)

    def close(self, price: float, prob: float, ts: datetime, reason: str) -> None:
        ret_pct = (price - self.entry_price) / self.entry_price * 100
        self.total_pnl_pct += ret_pct
        self.trade_count += 1
        held_min = int((ts - self.entry_time).total_seconds() // 60) if self.entry_time else 0

        logger.info(
            f"▼  [MOCK SELL] 가격={price:,.0f}원  확률={prob:.4f}  "
            f"수익률={ret_pct:+.3f}%  보유={held_min}분  사유={reason}"
        )
        logger.info(
            f"   누적 거래수={self.trade_count}  누적 PnL={self.total_pnl_pct:+.3f}%"
        )

        append_sell_csv(
            ts=ts,
            entry_time=self.entry_time,
            entry_price=self.entry_price,
            entry_prob=self.entry_prob,
            exit_price=price,
            exit_prob=prob,
            ret_pct=ret_pct,
            held_min=held_min,
            reason=reason,
            cum_count=self.trade_count,
            cum_pnl=self.total_pnl_pct,
        )

        self.in_position = False
        self.entry_price = 0.0
        self.entry_time = None
        self.entry_prob = 0.0

    def unrealized_pct(self, current_price: float) -> float:
        if not self.in_position or self.entry_price == 0:
            return 0.0
        return (current_price - self.entry_price) / self.entry_price * 100


# ══════════════════════════════════════════════════════════════════════════════
# 9. 대기 유틸
# ══════════════════════════════════════════════════════════════════════════════

def wait_for_next_minute(buffer_sec: float = 3.0) -> None:
    """다음 분 정각 + buffer_sec 까지 대기."""
    now = datetime.now()
    target = now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=buffer_sec)
    sleep_sec = (target - now).total_seconds()
    if sleep_sec > 0:
        time.sleep(sleep_sec)


# ══════════════════════════════════════════════════════════════════════════════
# 10. 메인 루프
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global logger
    logger = setup_logger()
    _ensure_trade_csv()

    logger.info("=" * 60)
    logger.info("  Step DL-4: 1D-CNN+LSTM 스나이퍼 모의투자 (Mock Mode)")
    logger.info("=" * 60)

    # ── 아티팩트 로드 ──────────────────────────────────────────────────────
    logger.info("[Init] 모델 / 스케일러 / 피처 목록 로드...")
    import joblib

    model = LSTMClassifier.load(MODEL_PATH, MODEL_META_PATH, device=DEVICE)
    model.eval()
    scaler = joblib.load(SCALER_PATH)

    with open(FEATURE_COLS_PATH) as f:
        feature_cols: list[str] = json.load(f)

    logger.info(f"  모델 파라미터: {model.count_params():,}  피처: {len(feature_cols)}  SEQ_LEN: {SEQ_LEN}")
    logger.info(f"  THRESHOLD={THRESHOLD}  STOP_LOSS={STOP_LOSS_PCT*100:.1f}%  EXIT_PROB_FLOOR={EXIT_PROB_FLOOR}")

    # ── 매크로 초기 조회 ───────────────────────────────────────────────────
    logger.info("[Init] 매크로 데이터 초기 조회...")
    macro = fetch_macro_latest()
    if macro is None:
        logger.error("매크로 초기 조회 완전 실패. 0값으로 대체합니다.")
        macro = {k: 0.0 for t in MACRO_TICKERS.values() for k in t}
    macro_refreshed_at = datetime.now()

    pos = MockPosition()
    loop_count = 0
    last_heartbeat = datetime.now()

    logger.info("[Run] 실시간 루프 시작 (Ctrl+C 로 종료)")
    logger.info(f"  {'시각':<17}  {'확률':>6}  {'현재가':>14}  {'상태':<8}  {'미실현PnL':>10}")
    logger.info("  " + "-" * 63)

    current_price = 0.0  # 마지막 가격 (요약 출력용)

    try:
        while True:
            wait_for_next_minute(buffer_sec=3.0)
            now = datetime.now()
            loop_count += 1

            # ── 하트비트 ───────────────────────────────────────────────────
            if (now - last_heartbeat).total_seconds() >= HEARTBEAT_SEC:
                logger.info(
                    f"[Heartbeat] 봇 정상 작동 중 | 루프={loop_count}회 "
                    f"거래={pos.trade_count}회 누적PnL={pos.total_pnl_pct:+.3f}%"
                )
                last_heartbeat = now

            # ── 매크로 주기적 갱신 ────────────────────────────────────────
            if (now - macro_refreshed_at).total_seconds() > MACRO_REFRESH_HOURS * 3600:
                logger.info("[Macro] 주기 갱신 시작...")
                refreshed = fetch_macro_latest()
                if refreshed is not None:
                    macro = refreshed
                    macro_refreshed_at = now
                else:
                    logger.warning("[Macro] 갱신 실패 — 기존값 유지")

            # ── 1분봉 수집 ────────────────────────────────────────────────
            ohlcv = fetch_ohlcv(FETCH_COUNT)
            if ohlcv is None:
                logger.error(f"[{now:%H:%M}] OHLCV {RETRY_MAX}회 재시도 모두 실패. 이번 봉 스킵.")
                continue

            current_price = float(ohlcv["close"].iloc[-1])

            # ── 피처 윈도우 생성 ──────────────────────────────────────────
            window = build_window(ohlcv, macro, feature_cols)
            if window is None:
                logger.warning(f"[{now:%H:%M}] 피처 생성 실패. 이번 봉 스킵.")
                continue

            # ── 추론 ──────────────────────────────────────────────────────
            tensor = transform_realtime(window, feature_cols=feature_cols, scaler=scaler)
            with torch.no_grad():
                logit = model(tensor)
                prob = float(torch.sigmoid(logit).item())

            # ── 상태 로그 ─────────────────────────────────────────────────
            state_str = "보유중" if pos.in_position else "대기"
            unreal_pct = pos.unrealized_pct(current_price)
            unreal_str = f"{unreal_pct:+.3f}%" if pos.in_position else "     -"
            logger.info(
                f"  {now:%Y-%m-%d %H:%M}  {prob:>6.4f}  {current_price:>14,.0f}원"
                f"  {state_str:<8}  {unreal_str:>10}"
            )

            # ── 매매 판단 ─────────────────────────────────────────────────
            if pos.in_position:
                stop_triggered = unreal_pct / 100 <= STOP_LOSS_PCT
                prob_weak = prob < EXIT_PROB_FLOOR

                if stop_triggered:
                    pos.close(current_price, prob, now, reason="STOP_LOSS")
                elif prob_weak:
                    pos.close(current_price, prob, now, reason="PROB_WEAK")
            else:
                if prob >= THRESHOLD:
                    pos.open(current_price, prob, now)

    except KeyboardInterrupt:
        logger.info("[종료] 사용자 중단 (Ctrl+C)")

    # ── 세션 요약 ─────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("  모의투자 세션 요약")
    logger.info("=" * 60)
    logger.info(f"  루프 횟수    : {loop_count}회")
    logger.info(f"  총 거래 수   : {pos.trade_count}회")
    logger.info(f"  누적 수익률  : {pos.total_pnl_pct:+.3f}%")
    if pos.in_position and current_price > 0:
        unreal_pct = pos.unrealized_pct(current_price)
        logger.info(
            f"  미청산 포지션: 진입가={pos.entry_price:,.0f}원  미실현={unreal_pct:+.3f}%"
        )
    logger.info(f"  거래 이력 CSV: {TRADE_CSV_PATH}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
