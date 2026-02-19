from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
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

    # v1.1: cost-based r_t floor
    spread_bps_med = Column(Double, nullable=True)
    cost_roundtrip_est = Column(Double, nullable=True)
    r_min_eff = Column(Double, nullable=True)

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


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    symbol = Column(Text, primary_key=True)
    status = Column(Text, nullable=False)  # FLAT | LONG
    cash_krw = Column(Double, nullable=False)
    qty = Column(Double, nullable=False)
    entry_time = Column(DateTime(timezone=True), nullable=True)
    entry_price = Column(Double, nullable=True)
    entry_fee_krw = Column(Double, nullable=True)
    u_exec = Column(Double, nullable=True)
    d_exec = Column(Double, nullable=True)
    h_sec = Column(Integer, nullable=True)
    entry_pred_t0 = Column(DateTime(timezone=True), nullable=True)
    entry_model_version = Column(Text, nullable=True)
    entry_r_t = Column(Double, nullable=True)
    entry_z_barrier = Column(Double, nullable=True)
    entry_ev_rate = Column(Double, nullable=True)
    entry_p_none = Column(Double, nullable=True)

    # v1.2: risk management + equity tracking
    initial_krw = Column(Double, nullable=True)
    equity_high = Column(Double, nullable=True)
    day_start_date = Column(Date, nullable=True)
    day_start_equity = Column(Double, nullable=True)
    halted = Column(Boolean, nullable=True)
    halt_reason = Column(Text, nullable=True)
    halted_at = Column(DateTime(timezone=True), nullable=True)

    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<PaperPosition {self.symbol} status={self.status} cash={self.cash_krw}>"


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    t = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    reason = Column(Text, nullable=False)
    price = Column(Double, nullable=False)
    qty = Column(Double, nullable=False)
    fee_krw = Column(Double, nullable=False)
    cash_after = Column(Double, nullable=False)
    pnl_krw = Column(Double, nullable=True)
    pnl_rate = Column(Double, nullable=True)
    hold_sec = Column(Double, nullable=True)
    pred_t0 = Column(DateTime(timezone=True), nullable=True)
    model_version = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_paper_trades_symbol_t", "symbol", "t"),
    )

    def __repr__(self) -> str:
        return f"<PaperTrade {self.symbol} {self.t} {self.action} {self.reason}>"


class PaperDecision(Base):
    __tablename__ = "paper_decisions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(Text, nullable=False)
    pos_status = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    reason = Column(Text, nullable=False)
    ev_rate = Column(Double, nullable=True)
    ev = Column(Double, nullable=True)
    p_up = Column(Double, nullable=True)
    p_down = Column(Double, nullable=True)
    p_none = Column(Double, nullable=True)
    r_t = Column(Double, nullable=True)
    z_barrier = Column(Double, nullable=True)
    spread_bps = Column(Double, nullable=True)
    lag_sec = Column(Double, nullable=True)
    cost_roundtrip_est = Column(Double, nullable=True)
    model_version = Column(Text, nullable=True)
    pred_t0 = Column(DateTime(timezone=True), nullable=True)
    reason_flags = Column(Text, nullable=True)

    # v1.2: equity tracking
    cash_krw = Column(Double, nullable=True)
    qty = Column(Double, nullable=True)
    equity_est = Column(Double, nullable=True)
    drawdown_pct = Column(Double, nullable=True)
    policy_profile = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_paper_decisions_symbol_ts", "symbol", "ts"),
    )

    def __repr__(self) -> str:
        return f"<PaperDecision {self.symbol} {self.ts} {self.action} {self.reason}>"


class UpbitAccountSnapshot(Base):
    __tablename__ = "upbit_account_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    symbol = Column(Text, nullable=False)
    currency = Column(Text, nullable=False)
    balance = Column(Double, nullable=False)
    locked = Column(Double, nullable=False)
    avg_buy_price = Column(Double, nullable=True)
    avg_buy_price_modified = Column(Boolean, nullable=True)
    unit_currency = Column(Text, nullable=True)
    raw_json = Column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_upbit_account_snapshots_ts", "ts"),
        Index("ix_upbit_account_snapshots_symbol_currency", "symbol", "currency"),
    )

    def __repr__(self) -> str:
        return f"<UpbitAccountSnapshot {self.currency} balance={self.balance} ts={self.ts}>"


class UpbitOrderAttempt(Base):
    __tablename__ = "upbit_order_attempts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    symbol = Column(Text, nullable=False)
    action = Column(Text, nullable=False)      # ENTER_LONG | EXIT_LONG
    mode = Column(Text, nullable=False)        # shadow | test | live
    side = Column(Text, nullable=False)        # bid | ask
    ord_type = Column(Text, nullable=False)    # market | limit | price
    price = Column(Double, nullable=True)
    volume = Column(Double, nullable=True)
    paper_trade_id = Column(BigInteger, nullable=True)
    response_json = Column(JSONB, nullable=True)
    status = Column(Text, nullable=False)      # logged | test_ok | submitted | error | done | cancel
    error_msg = Column(Text, nullable=True)
    # Step 8 extended columns
    uuid = Column(Text, nullable=True)
    identifier = Column(Text, nullable=True)
    request_json = Column(JSONB, nullable=True)
    http_status = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    remaining_req = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=True, default=0)
    final_state = Column(Text, nullable=True)
    executed_volume = Column(Double, nullable=True)
    paid_fee = Column(Double, nullable=True)
    avg_price = Column(Double, nullable=True)

    __table_args__ = (
        Index("ix_upbit_order_attempts_ts", "ts"),
        Index("ix_upbit_order_attempts_symbol_ts", "symbol", "ts"),
    )

    def __repr__(self) -> str:
        return f"<UpbitOrderAttempt {self.symbol} {self.action} mode={self.mode} status={self.status}>"


class UpbitOrderSnapshot(Base):
    """Live 주문 상태 스냅샷 (uuid 폴링)."""

    __tablename__ = "upbit_order_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(Text, nullable=False)
    uuid = Column(Text, nullable=False)
    state = Column(Text, nullable=True)
    side = Column(Text, nullable=True)
    ord_type = Column(Text, nullable=True)
    price = Column(Double, nullable=True)
    volume = Column(Double, nullable=True)
    remaining_volume = Column(Double, nullable=True)
    executed_volume = Column(Double, nullable=True)
    paid_fee = Column(Double, nullable=True)
    raw_json = Column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("uuid", "ts", name="uq_upbit_order_snapshots_uuid_ts"),
        Index("ix_upbit_order_snapshots_symbol_ts", "symbol", "ts"),
        Index("ix_upbit_order_snapshots_uuid_ts", "uuid", "ts"),
    )

    def __repr__(self) -> str:
        return f"<UpbitOrderSnapshot {self.uuid} state={self.state} ts={self.ts}>"


class LivePosition(Base):
    """실계좌 기반 포지션 스냅샷 (UpbitAccountRunner가 갱신)."""

    __tablename__ = "live_positions"

    symbol = Column(Text, primary_key=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    krw_balance = Column(Double, nullable=True)
    btc_balance = Column(Double, nullable=True)
    btc_avg_buy_price = Column(Double, nullable=True)
    position_status = Column(Text, nullable=True)   # FLAT | LONG
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<LivePosition {self.symbol} status={self.position_status} btc={self.btc_balance}>"
