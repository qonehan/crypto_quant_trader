from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ACTIVE_MODEL 별 자동 동기화 호흡(초) — ml_model._REGISTRY와 일치 유지
_ACTIVE_MODEL_HORIZON: dict[str, int] = {
    "ridge_h3600": 3600,
    "ridge_h600":  600,
    "ridge_h120":  120,
    "hgbr_h600":   600,
    "hgbr_h120":   120,
    "baseline_v1": 120,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    UPBIT_WS_URL: str = "wss://api.upbit.com/websocket/v1"
    SYMBOL: str = "KRW-BTC"
    UPBIT_WS_FORMAT: str = "DEFAULT"
    UPBIT_ORDERBOOK_UNIT: int = 5
    UPBIT_PING_INTERVAL_SEC: int = 20
    UPBIT_RECONNECT_MIN_SEC: float = 1
    UPBIT_RECONNECT_MAX_SEC: float = 30
    UPBIT_NO_MESSAGE_TIMEOUT_SEC: float = 30

    DECISION_INTERVAL_SEC: int = 5
    H_SEC: int = 3600
    VOL_WINDOW_SEC: int = 600

    R_MIN: float = 0.0010
    R_MAX: float = 0.03
    K_VOL: float = 1.0

    # EWMA feedback control
    TARGET_NONE: float = 0.55
    EWMA_ALPHA: float = 0.98
    EWMA_ETA: float = 0.15
    K_VOL_MIN: float = 0.50
    K_VOL_MAX: float = 2.00
    VOL_DT_SEC: int = 5

    MODEL_LOOKBACK_SEC: int = 3600
    FEE_RATE: float = 0.0005
    SLIPPAGE_BPS: float = 2
    EV_COST_MULT: float = 1.0
    P_NONE_MAX_FOR_SIGNAL: float = 0.7

    # p_none(z) function parameter
    P_HIT_CZ: float = 0.25

    # score coefficients (standardized inputs)
    SCORE_A_MOMZ: float = 1.5
    SCORE_B_IMB: float = 1.0
    SCORE_C_SPREAD: float = 1.0

    # action_hat thresholds
    ENTER_EV_RATE_TH: float = 0.0
    ENTER_PNONE_MAX: float = 0.70
    ENTER_PDIR_MARGIN: float = 0.05
    ENTER_SPREAD_BPS_MAX: float = 20.0

    # Evaluator / Dashboard windows
    EVAL_WINDOW_N: int = 500
    DASH_PRED_WINDOW_N: int = 200

    # Cost-based r_t floor
    R_MIN_COST_MULT: float = 1.10
    COST_SPREAD_LOOKBACK_SEC: int = 60

    # Paper trading
    PAPER_TRADING_ENABLED: bool = True
    PAPER_INITIAL_KRW: float = 1_000_000
    MAX_POSITION_FRAC: float = 0.20
    MIN_ORDER_KRW: float = 5000
    EXIT_EV_RATE_TH: float = -0.00002
    DATA_LAG_SEC_MAX: float = 5.0
    COST_RMIN_MULT: float = 1.10

    # Risk stops
    PAPER_MAX_DRAWDOWN_PCT: float = 0.05
    PAPER_DAILY_LOSS_LIMIT_PCT: float = 0.03
    PAPER_HALT_COOLDOWN_MIN: int = 1440

    # Equity logging
    PAPER_EQUITY_LOG_ENABLED: bool = True

    # Policy profile: strict | test
    PAPER_POLICY_PROFILE: str = "strict"
    TEST_ENTER_EV_RATE_TH: float = -0.00003
    TEST_ENTER_PNONE_MAX: float = 0.99
    TEST_ENTER_PDIR_MARGIN: float = -1.0
    TEST_COST_RMIN_MULT: float = 0.95
    TEST_MAX_POSITION_FRAC: float = 0.05
    TEST_MAX_ENTRIES_PER_HOUR: int = 2
    TEST_COOLDOWN_SEC: int = 300

    MODE: str = "paper"

    # Upbit REST API
    UPBIT_ACCESS_KEY: str = ""
    UPBIT_SECRET_KEY: str = ""
    UPBIT_API_BASE: str = "https://api.upbit.com"
    UPBIT_ACCOUNT_POLL_SEC: int = 30
    UPBIT_REST_TIMEOUT_SEC: float = 10.0
    UPBIT_REST_MAX_RETRY: int = 3

    # Shadow / Live trading safety (3-layer guard)
    UPBIT_SHADOW_ENABLED: bool = True
    UPBIT_ORDER_TEST_ENABLED: bool = False
    LIVE_TRADING_ENABLED: bool = False
    UPBIT_TRADE_MODE: str = "shadow"  # shadow | live
    LIVE_GUARD_PHRASE: str = ""  # must be "I_CONFIRM_LIVE_TRADING" to enable live

    # Live order polling (Step 8)
    LIVE_ORDER_POLL_INTERVAL_SEC: int = 5
    LIVE_ORDER_MAX_POLLS: int = 24  # up to 120s total

    # E2E test order parameters (Step 10)
    UPBIT_E2E_TEST_ORDER_KRW: int = 10000   # BUY order_test KRW amount
    UPBIT_E2E_TEST_SELL_BTC: float = 0.0001  # SELL order_test BTC volume (skip if balance insufficient)

    # Step 11: paper_trades → Upbit TEST auto-link
    UPBIT_TEST_ON_PAPER_TRADES: bool = True        # auto-call order_test on each paper_trade event
    UPBIT_TEST_BUY_KRW: int = 10000                # BUY order_test price (KRW)
    UPBIT_TEST_SELL_BTC: float = 0.0001            # SELL order_test volume fallback (BTC)
    UPBIT_TEST_REQUIRE_PAPER_PROFILE: str = "test" # must match PAPER_POLICY_PROFILE to allow test

    DB_URL: str = "postgresql+psycopg://postgres:postgres@db:5432/quant"
    GCP_DB_URL: Optional[str] = None

    # ── Predictor ──────────────────────────────────────────────────
    # ACTIVE_MODEL 하나만 바꾸면 모델·호흡·gamma가 자동 결정됩니다.
    ACTIVE_MODEL: str = "ridge_h3600"  # 레지스트리 키 (ml_model._REGISTRY 참조)

    # 하위 호환용 — ACTIVE_MODEL 미설정 환경에서만 사용
    PREDICTOR_TYPE: str = "ridge"
    RIDGE_MODEL_PATH: str = "artifacts/ml_prod/h3600/historical_dataset_ridge/ridge_model.joblib"
    RIDGE_GAMMA: float = 1.5

    @model_validator(mode="after")
    def _sync_horizon_from_active_model(self) -> "Settings":
        """ACTIVE_MODEL에 매핑된 h_sec을 H_SEC / MODEL_LOOKBACK_SEC에 자동 반영."""
        h = _ACTIVE_MODEL_HORIZON.get(self.ACTIVE_MODEL)
        if h is not None:
            object.__setattr__(self, "H_SEC", h)
            object.__setattr__(self, "MODEL_LOOKBACK_SEC", h)
        return self

    # ── Alt Data (Binance / Coinglass) ─────────────────────────────
    ALT_DATA_ENABLED: bool = False
    ALT_SYMBOL_BINANCE: str = "BTCUSDT"
    ALT_SYMBOL_COINGLASS: str = "BTC"

    # Binance Futures WS
    BINANCE_FUTURES_WS_BASE: str = "wss://fstream.binance.com/ws"
    BINANCE_FUTURES_REST_BASE: str = "https://fapi.binance.com"
    BINANCE_MARK_PRICE_STREAM: str = "!markPrice@arr@1s"
    BINANCE_FORCE_ORDER_STREAM: str = "!forceOrder@arr"
    BINANCE_POLL_SEC: int = 60
    BINANCE_METRIC_PERIOD: str = "5m"

    # Coinglass
    COINGLASS_ENABLED: bool = False
    COINGLASS_API_KEY: str = ""
    COINGLASS_BASE: str = "https://open-api.coinglass.com"
    COINGLASS_POLL_SEC: int = 300


def load_settings() -> Settings:
    return Settings()
