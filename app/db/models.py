from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Double,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    func,
)
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
