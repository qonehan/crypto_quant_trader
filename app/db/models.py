from sqlalchemy import (
    BigInteger,
    Boolean,
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

    # v1: bid/ask OHLC
    bid_open_1s = Column(Double, nullable=True)
    bid_high_1s = Column(Double, nullable=True)
    bid_low_1s = Column(Double, nullable=True)
    bid_close_1s = Column(Double, nullable=True)
    ask_open_1s = Column(Double, nullable=True)
    ask_high_1s = Column(Double, nullable=True)
    ask_low_1s = Column(Double, nullable=True)
    ask_close_1s = Column(Double, nullable=True)
    spread_bps = Column(Double, nullable=True)
    imb_notional_top5 = Column(Double, nullable=True)
    mid_close_1s = Column(Double, nullable=True)

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

    # v1: feedback / state tracking
    k_vol_eff = Column(Double, nullable=True)
    none_ewma = Column(Double, nullable=True)
    target_none = Column(Double, nullable=True)
    ewma_alpha = Column(Double, nullable=True)
    ewma_eta = Column(Double, nullable=True)
    vol_dt_sec = Column(Integer, nullable=True)

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

    # v1: probability / EV fields
    z_barrier = Column(Double, nullable=True)
    p_hit_base = Column(Double, nullable=True)
    ev_rate = Column(Double, nullable=True)
    r_none_pred = Column(Double, nullable=True)
    t_up_cond_pred = Column(Double, nullable=True)
    t_down_cond_pred = Column(Double, nullable=True)
    spread_bps = Column(Double, nullable=True)
    mom_z = Column(Double, nullable=True)
    imb_notional_top5 = Column(Double, nullable=True)
    action_hat = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("symbol", "t0", name="uq_predictions_symbol_t0"),
        Index("ix_predictions_status", "status"),
        Index("ix_predictions_t0", "t0"),
        Index("ix_predictions_symbol_t0_desc", "symbol", t0.desc()),
    )

    def __repr__(self) -> str:
        return f"<Prediction {self.symbol} {self.t0} hat={self.direction_hat} ev={self.ev}>"


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(Text, nullable=False)
    t0 = Column(DateTime(timezone=True), nullable=False)
    r_t = Column(Double, nullable=False)
    p_up = Column(Double, nullable=False)
    p_down = Column(Double, nullable=False)
    p_none = Column(Double, nullable=False)
    ev = Column(Double, nullable=False)
    slope_pred = Column(Double, nullable=False)
    direction_hat = Column(Text, nullable=False)
    actual_direction = Column(Text, nullable=False)
    actual_r_t = Column(Double, nullable=False)
    touch_time_sec = Column(Double, nullable=True)
    status = Column(Text, nullable=False)
    error = Column(Text, nullable=True)

    # v1: exec_v1 label / probability assessment
    label_version = Column(Text, nullable=True)
    entry_price = Column(Double, nullable=True)
    u_exec = Column(Double, nullable=True)
    d_exec = Column(Double, nullable=True)
    ambig_touch = Column(Boolean, nullable=True)
    r_h = Column(Double, nullable=True)
    brier = Column(Double, nullable=True)
    logloss = Column(Double, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("symbol", "t0", name="uq_evaluation_results_symbol_t0"),
        Index("ix_evaluation_results_t0", "t0"),
        Index("ix_evaluation_results_status", "status"),
        Index("ix_evaluation_results_symbol_t0_desc", "symbol", t0.desc()),
    )

    def __repr__(self) -> str:
        return f"<EvaluationResult {self.symbol} {self.t0} hat={self.direction_hat} actual={self.actual_direction}>"


class BarrierParams(Base):
    __tablename__ = "barrier_params"

    symbol = Column(Text, primary_key=True)
    k_vol_eff = Column(Double, nullable=False)
    none_ewma = Column(Double, nullable=False)
    target_none = Column(Double, nullable=False)
    ewma_alpha = Column(Double, nullable=False)
    ewma_eta = Column(Double, nullable=False)
    last_eval_t0 = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<BarrierParams {self.symbol} k_vol_eff={self.k_vol_eff}>"
