from pydantic_settings import BaseSettings, SettingsConfigDict


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
    H_SEC: int = 120
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

    MODEL_LOOKBACK_SEC: int = 120
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

    DB_URL: str = "postgresql+psycopg://postgres:postgres@db:5432/quant"


def load_settings() -> Settings:
    return Settings()
