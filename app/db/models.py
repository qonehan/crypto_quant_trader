from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Double,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Market1s(Base):
    __tablename__ = "market_1s"

    ts = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(Text, nullable=False)

    mid = Column(Double, nullable=True)
    bid = Column(Double, nullable=True)
    ask = Column(Double, nullable=True)
    spread = Column(Double, nullable=True)

    trade_count_1s = Column(Integer, nullable=False, default=0)
    trade_volume_1s = Column(Double, nullable=False, default=0)

    imbalance_top5 = Column(Double, nullable=True)

    last_trade_price = Column(Double, nullable=True)
    last_trade_volume = Column(Double, nullable=True)
    last_trade_side = Column(Text, nullable=True)

    ticker_ts_ms = Column(BigInteger, nullable=True)
    trade_ts_ms = Column(BigInteger, nullable=True)
    orderbook_ts_ms = Column(BigInteger, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts"),
        Index("ix_market_1s_ts", "ts"),
        Index("ix_market_1s_symbol_ts_desc", "symbol", ts.desc()),
    )

    def __repr__(self) -> str:
        return f"<Market1s {self.symbol} {self.ts} mid={self.mid}>"


class BarrierState(Base):
    __tablename__ = "barrier_state"

    ts = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(Text, nullable=False)

    h_sec = Column(Integer, nullable=False)
    vol_window_sec = Column(Integer, nullable=False)

    sigma_1s = Column(Double, nullable=True)
    sigma_h = Column(Double, nullable=True)

    r_min = Column(Double, nullable=False)
    k_vol = Column(Double, nullable=False)
    r_t = Column(Double, nullable=False)

    sample_n = Column(Integer, nullable=False, default=0)
    status = Column(Text, nullable=False)
    error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts"),
        Index("ix_barrier_state_ts", "ts"),
        Index("ix_barrier_state_symbol_ts_desc", "symbol", ts.desc()),
    )

    def __repr__(self) -> str:
        return f"<BarrierState {self.symbol} {self.ts} r_t={self.r_t} status={self.status}>"


class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    t0 = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(Text, nullable=False)
    h_sec = Column(Integer, nullable=False)
    r_t = Column(Double, nullable=False)

    p_up = Column(Double, nullable=False)
    p_down = Column(Double, nullable=False)
    p_none = Column(Double, nullable=False)

    t_up = Column(Double, nullable=True)
    t_down = Column(Double, nullable=True)

    slope_pred = Column(Double, nullable=False)
    ev = Column(Double, nullable=False)
    direction_hat = Column(Text, nullable=False)

    model_version = Column(Text, nullable=False)
    status = Column(Text, nullable=False)

    sigma_1s = Column(Double, nullable=True)
    sigma_h = Column(Double, nullable=True)
    features = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("symbol", "t0", name="uq_predictions_symbol_t0"),
        Index("ix_predictions_status", "status"),
        Index("ix_predictions_t0", "t0"),
        Index("ix_predictions_symbol_t0_desc", "symbol", t0.desc()),
    )

    def __repr__(self) -> str:
        return f"<Prediction {self.symbol} {self.t0} hat={self.direction_hat} ev={self.ev}>"
