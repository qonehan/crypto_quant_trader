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

    MODE: str = "paper"

    DB_URL: str = "postgresql+psycopg://postgres:postgres@db:5432/quant"


def load_settings() -> Settings:
    return Settings()
