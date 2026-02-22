import os
import sys

# sys.path 보정: 어떤 경로에서 실행해도 프로젝트 루트를 찾을 수 있게 한다.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import json

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from sqlalchemy import text

from app.config import is_real_key, load_settings
from app.db.session import get_engine
from app.evaluator.evaluator import compute_calibration

DB_RESOLVE_HINT = (
    "DB host 'db'를 찾지 못했습니다. "
    "Codespaces에서 Dev Containers: Rebuild and Reopen in Container를 실행해 "
    "docker-compose devcontainer로 들어가 있는지 확인하세요. "
    "또한 db 컨테이너가 정상 실행 중인지 확인하세요."
)


def main() -> None:
    st.set_page_config(page_title="BTC Quant Bot v1", layout="wide")
    st.title("BTC Quant Bot - v1 Dashboard")

    settings = load_settings()
    now_utc = datetime.now(timezone.utc)

    st.subheader("Settings")
    st.write(f"**SYMBOL:** {settings.SYMBOL}  |  **MODE:** {settings.MODE}")

    try:
        engine = get_engine(settings)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        st.success("DB connection OK")
    except Exception as e:
        err = str(e)
        if "failed to resolve host" in err or "could not translate host name" in err:
            st.error(f"DB connection failed: {e}")
            st.warning(DB_RESOLVE_HINT)
        else:
            st.error(f"DB connection failed: {e}")
        return

    # ── market_1s data ──────────────────────────────────────
    st.subheader("market_1s — Recent Data")

    try:
        with engine.connect() as conn:
            df60 = pd.read_sql_query(
                text(
                    "SELECT ts, symbol, mid, bid, ask, spread, trade_count_1s, "
                    "trade_volume_1s, imbalance_top5, last_trade_price, last_trade_side "
                    "FROM market_1s ORDER BY ts DESC LIMIT 60"
                ),
                conn,
            )
            df300 = pd.read_sql_query(
                text("SELECT ts, mid FROM market_1s ORDER BY ts DESC LIMIT 300"),
                conn,
            )
    except Exception as e:
        st.warning(f"market_1s table not available yet: {e}")
        return

    if df60.empty:
        st.info("No market_1s rows yet. Start the bot first.")
        return

    last_ts = pd.to_datetime(df60["ts"].iloc[0], utc=True)
    lag_sec = (now_utc - last_ts).total_seconds()
    st.metric("Lag (sec)", f"{lag_sec:.1f}")

    st.dataframe(df60, use_container_width=True, height=400)

    if not df300.empty:
        st.subheader("Mid Price — Last 5 min")
        chart_df = df300.sort_values("ts").set_index("ts")
        st.line_chart(chart_df["mid"])

    # ══════════════════════════════════════════════════════════
    # [A] Barrier Feedback
    # ══════════════════════════════════════════════════════════
    st.header("[A] Barrier Feedback")

    try:
        with engine.connect() as conn:
            bp_df = pd.read_sql_query(
                text(
                    "SELECT symbol, k_vol_eff, none_ewma, target_none, "
                    "ewma_alpha, ewma_eta, updated_at "
                    "FROM barrier_params WHERE symbol = :sym"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
            bs_latest = pd.read_sql_query(
                text(
                    "SELECT ts, symbol, r_t, sigma_1s, sigma_h, status, sample_n, "
                    "h_sec, vol_window_sec, r_min, k_vol, k_vol_eff, none_ewma, "
                    "r_min_eff, cost_roundtrip_est, spread_bps_med "
                    "FROM barrier_state WHERE symbol = :sym ORDER BY ts DESC LIMIT 1"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
            bs_chart = pd.read_sql_query(
                text(
                    "SELECT ts, r_t, sigma_h, k_vol_eff, none_ewma, "
                    "r_min_eff, cost_roundtrip_est, spread_bps_med "
                    "FROM barrier_state WHERE symbol = :sym "
                    "ORDER BY ts DESC LIMIT 720"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"barrier data not available: {e}")
        bp_df = pd.DataFrame()
        bs_latest = pd.DataFrame()
        bs_chart = pd.DataFrame()

    if not bp_df.empty:
        bp = bp_df.iloc[0]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("k_vol_eff", f"{bp['k_vol_eff']:.4f}")
        c2.metric("none_ewma", f"{bp['none_ewma']:.4f}")
        c3.metric("target_none", f"{bp['target_none']:.2f}")
        c4.metric("ewma_alpha", f"{bp['ewma_alpha']:.2f}")
        c5.metric("ewma_eta", f"{bp['ewma_eta']:.2f}")
        st.caption(f"Updated at: {bp['updated_at']}")

    if not bs_latest.empty:
        row = bs_latest.iloc[0]
        bc1, bc2, bc3, bc4, bc5, bc6 = st.columns(6)
        bc1.metric("r_t", f"{row['r_t']:.6f}")
        bc2.metric("sigma_h", f"{row['sigma_h']:.8f}" if pd.notna(row["sigma_h"]) else "N/A")
        bc3.metric("Status", row["status"])
        bc4.metric("sample_n", int(row["sample_n"]) if pd.notna(row["sample_n"]) else 0)
        bc5.metric("r_min_eff", f"{row['r_min_eff']:.6f}" if pd.notna(row.get("r_min_eff")) else "N/A")
        bc6.metric("cost_roundtrip", f"{row['cost_roundtrip_est']:.6f}" if pd.notna(row.get("cost_roundtrip_est")) else "N/A")

    if not bs_chart.empty:
        bsc = bs_chart.sort_values("ts").set_index("ts")

        st.subheader("r_t vs r_min_eff vs cost_roundtrip — Time Series")
        cost_cols = ["r_t", "r_min_eff", "cost_roundtrip_est"]
        cost_data = bsc[cost_cols].dropna(how="all")
        if not cost_data.empty:
            st.line_chart(cost_data)

        st.subheader("r_t / k_vol_eff / none_ewma — Time Series")
        chart_sel = st.selectbox("Select chart", ["r_t", "k_vol_eff", "none_ewma", "sigma_h", "spread_bps_med"])
        col_data = bsc[chart_sel].dropna() if chart_sel in bsc.columns else pd.Series(dtype=float)
        if not col_data.empty:
            st.line_chart(col_data)

    # ══════════════════════════════════════════════════════════
    # [B] Probabilistic Metrics
    # ══════════════════════════════════════════════════════════
    st.header("[B] Probabilistic Metrics")

    eval_n = settings.EVAL_WINDOW_N
    try:
        with engine.connect() as conn:
            eval_agg = pd.read_sql_query(
                text("""
                    SELECT count(*) as n,
                           avg(brier) as mean_brier,
                           avg(logloss) as mean_logloss,
                           avg(case when actual_direction='NONE' then 1 else 0 end) as none_rate,
                           avg(case when actual_direction='UP' then 1 else 0 end) as up_rate,
                           avg(case when actual_direction='DOWN' then 1 else 0 end) as down_rate,
                           avg(case when direction_hat = actual_direction then 1 else 0 end) as accuracy,
                           avg(case when touch_time_sec is not null then 1 else 0 end) as hit_rate
                    FROM (
                        SELECT * FROM evaluation_results
                        WHERE symbol = :sym AND label_version='exec_v1'
                          AND brier IS NOT NULL AND logloss IS NOT NULL
                        ORDER BY t0 DESC LIMIT :n
                    ) sub
                """),
                conn,
                params={"sym": settings.SYMBOL, "n": eval_n},
            )
    except Exception as e:
        st.warning(f"evaluation_results not available: {e}")
        eval_agg = pd.DataFrame()

    if not eval_agg.empty and eval_agg.iloc[0]["n"] > 0:
        ea = eval_agg.iloc[0]
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("N", int(ea["n"]))
        m2.metric("Accuracy", f"{ea['accuracy']:.3f}")
        m3.metric("Hit Rate", f"{ea['hit_rate']:.3f}")
        m4.metric("None Rate", f"{ea['none_rate']:.3f}")
        m5.metric("Mean Brier", f"{ea['mean_brier']:.4f}")
        m6.metric("Mean LogLoss", f"{ea['mean_logloss']:.4f}")

        st.caption(f"Actual distribution: UP={ea['up_rate']:.3f} DOWN={ea['down_rate']:.3f} NONE={ea['none_rate']:.3f}")
    else:
        st.info("No exec_v1 evaluation results yet.")

    # ══════════════════════════════════════════════════════════
    # [C] Calibration Tables
    # ══════════════════════════════════════════════════════════
    st.header("[C] Calibration Tables")

    try:
        with engine.connect() as conn:
            calib_rows = conn.execute(
                text("""
                    SELECT p_up, p_down, p_none, actual_direction
                    FROM evaluation_results
                    WHERE symbol = :sym AND label_version='exec_v1'
                      AND brier IS NOT NULL AND logloss IS NOT NULL
                    ORDER BY t0 DESC LIMIT :n
                """),
                {"sym": settings.SYMBOL, "n": eval_n},
            ).fetchall()
    except Exception as e:
        st.warning(f"calibration data not available: {e}")
        calib_rows = []

    if calib_rows:
        for cls in ("UP", "DOWN", "NONE"):
            calib = compute_calibration(calib_rows, cls)
            calib_df = pd.DataFrame(calib)

            # ECE
            total_count = calib_df["count"].sum()
            if total_count > 0:
                ece = (calib_df["abs_gap"] * calib_df["count"]).sum() / total_count
            else:
                ece = 0.0

            st.subheader(f"Calibration: {cls}  (ECE = {ece:.4f})")
            non_empty = calib_df[calib_df["count"] > 0]
            if not non_empty.empty:
                st.dataframe(non_empty, use_container_width=True)
            else:
                st.info(f"No samples for {cls}")
    else:
        st.info("No calibration data yet.")

    # ══════════════════════════════════════════════════════════
    # [D] EV/Cost Diagnostic Panel
    # ══════════════════════════════════════════════════════════
    st.header("[D] EV/Cost Diagnostic Panel")

    pred_n = settings.DASH_PRED_WINDOW_N
    try:
        with engine.connect() as conn:
            pred_diag = pd.read_sql_query(
                text("""
                    SELECT ev, ev_rate, p_none, spread_bps, action_hat
                    FROM predictions
                    WHERE symbol = :sym AND ev IS NOT NULL
                    ORDER BY t0 DESC LIMIT :n
                """),
                conn,
                params={"sym": settings.SYMBOL, "n": pred_n},
            )
    except Exception as e:
        st.warning(f"predictions data not available: {e}")
        pred_diag = pd.DataFrame()

    if not pred_diag.empty:
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("EV mean", f"{pred_diag['ev'].mean():.8f}")
        d1.metric("EV median", f"{pred_diag['ev'].median():.8f}")
        d2.metric("EV_rate mean", f"{pred_diag['ev_rate'].mean():.2e}" if pred_diag['ev_rate'].notna().any() else "N/A")
        d2.metric("EV_rate median", f"{pred_diag['ev_rate'].median():.2e}" if pred_diag['ev_rate'].notna().any() else "N/A")
        d3.metric("p_none mean", f"{pred_diag['p_none'].mean():.4f}")
        d3.metric("p_none median", f"{pred_diag['p_none'].median():.4f}")

        spd = pred_diag['spread_bps'].dropna()
        d4.metric("spread_bps mean", f"{spd.mean():.2f}" if not spd.empty else "N/A")
        d4.metric("spread_bps median", f"{spd.median():.2f}" if not spd.empty else "N/A")

        # action_hat distribution
        if "action_hat" in pred_diag.columns:
            action_counts = pred_diag["action_hat"].value_counts()
            st.subheader("action_hat Distribution")
            st.bar_chart(action_counts)

        # Cost breakdown
        st.subheader("Cost Breakdown (estimated)")
        fee_round = 2 * settings.FEE_RATE
        slip_round = 2 * (settings.SLIPPAGE_BPS / 10000.0)
        spread_median = spd.median() / 10000.0 if not spd.empty else 0.0
        cost_est = settings.EV_COST_MULT * (fee_round + slip_round + spread_median)

        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("fee_round (2*FEE_RATE)", f"{fee_round:.6f}")
        cc2.metric("slip_round (2*SLIP/1e4)", f"{slip_round:.6f}")
        cc3.metric("spread_round (median)", f"{spread_median:.6f}")
        cc4.metric("cost_roundtrip_est", f"{cost_est:.6f}")

        st.info(
            f"cost_roundtrip = EV_COST_MULT({settings.EV_COST_MULT}) * "
            f"(fee_round({fee_round:.6f}) + slip_round({slip_round:.6f}) + "
            f"spread_round({spread_median:.6f})) = **{cost_est:.6f}**"
        )
    else:
        st.info("No prediction data for EV/Cost diagnostics yet.")

    # ── Predictions Table ──────────────────────────────────────
    st.header("Predictions — Recent 20")

    try:
        with engine.connect() as conn:
            pred_recent = pd.read_sql_query(
                text(
                    "SELECT t0, r_t, p_up, p_down, p_none, z_barrier, "
                    "ev, ev_rate, action_hat, model_version, status "
                    "FROM predictions WHERE symbol = :sym ORDER BY t0 DESC LIMIT 20"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"predictions table not available: {e}")
        pred_recent = pd.DataFrame()

    if not pred_recent.empty:
        pr = pred_recent.iloc[0]
        pc1, pc2, pc3, pc4 = st.columns(4)
        pc1.metric("action_hat", pr.get("action_hat", "N/A"))
        pc2.metric("EV", f"{pr['ev']:.8f}")
        pc3.metric("ev_rate", f"{pr['ev_rate']:.2e}" if pd.notna(pr.get("ev_rate")) else "N/A")
        pc4.metric("model_version", pr.get("model_version", "N/A"))

        st.dataframe(pred_recent, use_container_width=True, height=400)

    # ── Evaluation Results ──────────────────────────────────────
    st.header("Evaluation Results — Recent 20")

    try:
        with engine.connect() as conn:
            eval_recent = pd.read_sql_query(
                text(
                    "SELECT t0, r_t, direction_hat, actual_direction, actual_r_t, "
                    "touch_time_sec, brier, logloss, status "
                    "FROM evaluation_results WHERE symbol = :sym ORDER BY t0 DESC LIMIT 20"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"evaluation_results not available: {e}")
        eval_recent = pd.DataFrame()

    if not eval_recent.empty:
        st.dataframe(eval_recent, use_container_width=True, height=400)
    else:
        st.info("No evaluation results yet.")

    # ══════════════════════════════════════════════════════════
    # [E] Paper Trading
    # ══════════════════════════════════════════════════════════
    st.header("[E] Paper Trading")

    try:
        with engine.connect() as conn:
            pp_df = pd.read_sql_query(
                text(
                    "SELECT symbol, status, cash_krw, qty, entry_time, entry_price, "
                    "u_exec, d_exec, h_sec, entry_r_t, entry_ev_rate, entry_p_none, "
                    "initial_krw, equity_high, day_start_date, day_start_equity, "
                    "halted, halt_reason, halted_at, updated_at "
                    "FROM paper_positions WHERE symbol = :sym"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"paper_positions not available: {e}")
        pp_df = pd.DataFrame()

    if not pp_df.empty:
        pp = pp_df.iloc[0]
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Status", pp["status"])
        p1.metric("Cash (KRW)", f"{pp['cash_krw']:,.0f}")
        p2.metric("Qty", f"{pp['qty']:.8f}")
        p2.metric("Entry Price", f"{pp['entry_price']:,.0f}" if pd.notna(pp["entry_price"]) else "N/A")
        p3.metric("equity_high", f"{pp['equity_high']:,.0f}" if pd.notna(pp.get("equity_high")) else "N/A")
        p3.metric("initial_krw", f"{pp['initial_krw']:,.0f}" if pd.notna(pp.get("initial_krw")) else "N/A")
        halted_val = pp.get("halted")
        p4.metric("Halted", str(halted_val) if halted_val is not None else "false")
        p4.metric("Halt Reason", pp.get("halt_reason") or "N/A")
        p5.metric("entry_r_t", f"{pp['entry_r_t']:.6f}" if pd.notna(pp["entry_r_t"]) else "N/A")
        p5.metric("Profile", getattr(settings, "PAPER_POLICY_PROFILE", "strict"))
        st.caption(f"Updated at: {pp['updated_at']}")
    else:
        st.info("No paper position yet.")

    # Equity Curve + Drawdown
    st.subheader("Equity Curve — Last 6h")
    try:
        with engine.connect() as conn:
            eq_df = pd.read_sql_query(
                text("""
                    SELECT ts, equity_est, drawdown_pct, policy_profile
                    FROM paper_decisions
                    WHERE symbol = :sym AND equity_est IS NOT NULL
                      AND ts >= now() - interval '6 hours'
                    ORDER BY ts ASC
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"equity data not available: {e}")
        eq_df = pd.DataFrame()

    if not eq_df.empty:
        eq_chart = eq_df.set_index("ts")
        st.line_chart(eq_chart["equity_est"])
        st.subheader("Drawdown (%) — Last 6h")
        st.line_chart(eq_chart["drawdown_pct"] * 100)
    else:
        st.info("No equity data yet.")

    # Trade Stats
    st.subheader("Trade Stats (EXIT_LONG, last 200)")
    try:
        with engine.connect() as conn:
            exit_stats = pd.read_sql_query(
                text("""
                    SELECT count(*) as trades,
                           avg(case when pnl_krw > 0 then 1.0 else 0.0 end) as win_rate,
                           avg(pnl_krw) as avg_pnl_krw,
                           avg(pnl_rate) as avg_pnl_rate,
                           avg(hold_sec) as avg_hold_sec,
                           sum(fee_krw) as total_fee_krw
                    FROM (
                        SELECT * FROM paper_trades
                        WHERE symbol = :sym AND action = 'EXIT_LONG'
                        ORDER BY t DESC LIMIT 200
                    ) sub
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
            exit_reasons = pd.read_sql_query(
                text("""
                    SELECT reason, count(*) as cnt
                    FROM (
                        SELECT reason FROM paper_trades
                        WHERE symbol = :sym AND action = 'EXIT_LONG'
                        ORDER BY t DESC LIMIT 200
                    ) sub
                    GROUP BY reason ORDER BY cnt DESC
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"trade stats not available: {e}")
        exit_stats = pd.DataFrame()
        exit_reasons = pd.DataFrame()

    if not exit_stats.empty and exit_stats.iloc[0]["trades"] > 0:
        es = exit_stats.iloc[0]
        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("Trades", int(es["trades"]))
        s2.metric("Win Rate", f"{es['win_rate']:.2%}")
        s3.metric("Avg PnL (KRW)", f"{es['avg_pnl_krw']:,.0f}")
        s4.metric("Avg Hold (sec)", f"{es['avg_hold_sec']:.0f}" if pd.notna(es["avg_hold_sec"]) else "N/A")
        s5.metric("Total Fees (KRW)", f"{es['total_fee_krw']:,.0f}")

        if not exit_reasons.empty:
            st.caption("Exit Reason Distribution:")
            st.dataframe(exit_reasons, use_container_width=True)
    else:
        st.info("No EXIT_LONG trades yet.")

    # Paper Trades
    st.subheader("Paper Trades — Recent 30")
    try:
        with engine.connect() as conn:
            pt_df = pd.read_sql_query(
                text(
                    "SELECT t, action, reason, price, qty, fee_krw, cash_after, "
                    "pnl_krw, pnl_rate, hold_sec, model_version "
                    "FROM paper_trades WHERE symbol = :sym ORDER BY t DESC LIMIT 30"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"paper_trades not available: {e}")
        pt_df = pd.DataFrame()

    if not pt_df.empty:
        st.dataframe(pt_df, use_container_width=True, height=300)
    else:
        st.info("No paper trades yet (expected if cost > r_t).")

    # Paper Decisions
    st.subheader("Paper Decisions — Recent 60")
    try:
        with engine.connect() as conn:
            pd_df = pd.read_sql_query(
                text(
                    "SELECT ts, pos_status, action, reason, reason_flags, ev_rate, p_none, "
                    "spread_bps, lag_sec, cost_roundtrip_est, r_t, "
                    "equity_est, drawdown_pct, policy_profile "
                    "FROM paper_decisions WHERE symbol = :sym ORDER BY ts DESC LIMIT 60"
                ),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"paper_decisions not available: {e}")
        pd_df = pd.DataFrame()

    if not pd_df.empty:
        st.dataframe(pd_df, use_container_width=True, height=400)

    # Why no trades? — primary reason distribution
    st.subheader("Primary Reason Distribution (last 500)")
    try:
        with engine.connect() as conn:
            reason_dist = pd.read_sql_query(
                text("""
                    SELECT reason, count(*) as cnt
                    FROM (
                        SELECT reason FROM paper_decisions
                        WHERE symbol = :sym
                        ORDER BY ts DESC LIMIT 500
                    ) sub
                    GROUP BY reason ORDER BY cnt DESC LIMIT 8
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"reason distribution not available: {e}")
        reason_dist = pd.DataFrame()

    if not reason_dist.empty:
        st.dataframe(reason_dist, use_container_width=True)
        st.bar_chart(reason_dist.set_index("reason")["cnt"])
    else:
        st.info("No decision data yet.")

    # Flag-level distribution (reason_flags JSON)
    st.subheader("Reason Flags Distribution — All flags (last 500)")
    try:
        with engine.connect() as conn:
            flags_raw = conn.execute(
                text("""
                    SELECT reason_flags FROM paper_decisions
                    WHERE symbol = :sym AND reason_flags IS NOT NULL
                    ORDER BY ts DESC LIMIT 500
                """),
                {"sym": settings.SYMBOL},
            ).fetchall()
    except Exception as e:
        st.warning(f"reason_flags not available: {e}")
        flags_raw = []

    if flags_raw:
        flag_counts: dict[str, int] = {}
        for row in flags_raw:
            try:
                flags_list = json.loads(row.reason_flags)
                for f in flags_list:
                    flag_counts[f] = flag_counts.get(f, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass
        if flag_counts:
            fc_df = pd.DataFrame(
                sorted(flag_counts.items(), key=lambda x: -x[1]),
                columns=["flag", "count"],
            )
            st.dataframe(fc_df, use_container_width=True)
            st.bar_chart(fc_df.set_index("flag")["count"])
        else:
            st.info("No flags parsed yet.")
    else:
        st.info("No reason_flags data yet.")


    # ══════════════════════════════════════════════════════════
    # [F] Upbit Exchange (Step 8)
    # ══════════════════════════════════════════════════════════
    st.header("[F] Upbit Exchange")

    # (1) Mode / Guard status
    st.subheader("모드 / 가드 상태")
    live_guard = (
        settings.LIVE_TRADING_ENABLED
        and settings.UPBIT_TRADE_MODE == "live"
        and settings.LIVE_GUARD_PHRASE == "I_CONFIRM_LIVE_TRADING"
        and settings.PAPER_POLICY_PROFILE != "test"
    )
    has_key = bool(settings.UPBIT_ACCESS_KEY and settings.UPBIT_SECRET_KEY)
    g1, g2, g3, g4, g5 = st.columns(5)
    g1.metric("LIVE_TRADING_ENABLED", str(settings.LIVE_TRADING_ENABLED))
    g2.metric("UPBIT_TRADE_MODE", settings.UPBIT_TRADE_MODE)
    g3.metric("ORDER_TEST_ENABLED", str(settings.UPBIT_ORDER_TEST_ENABLED))
    g4.metric("SHADOW_ENABLED", str(settings.UPBIT_SHADOW_ENABLED))
    g5.metric("API Keys", "✅ set" if has_key else "❌ not set")
    live_label = "🔴 LIVE ACTIVE" if live_guard else "🟢 SAFE (no live)"
    policy_note = f"  |  POLICY_PROFILE={settings.PAPER_POLICY_PROFILE}"
    st.info(f"Live Guard: {live_label}{policy_note}")

    # (2) Account snapshots — latest balances + live_positions
    st.subheader("계좌 잔액 (최신 스냅샷)")
    try:
        with engine.connect() as conn:
            acct_df = pd.read_sql_query(
                text("""
                    SELECT DISTINCT ON (currency)
                        ts, currency, balance, locked, avg_buy_price, unit_currency
                    FROM upbit_account_snapshots
                    WHERE symbol = :sym
                    ORDER BY currency, ts DESC
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"upbit_account_snapshots not available: {e}")
        acct_df = pd.DataFrame()

    if not acct_df.empty:
        st.dataframe(acct_df, use_container_width=True)
        st.caption(f"스냅샷 기준: {acct_df['ts'].max()}")
    else:
        st.info("계좌 스냅샷 없음 (UPBIT_ACCESS_KEY 설정 및 UpbitAccountRunner 실행 필요)")

    # live_positions summary
    try:
        with engine.connect() as conn:
            lp_df = pd.read_sql_query(
                text("""
                    SELECT ts, krw_balance, btc_balance, btc_avg_buy_price,
                           position_status, updated_at
                    FROM live_positions
                    WHERE symbol = :sym
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        lp_df = pd.DataFrame()

    if not lp_df.empty:
        st.caption("실계좌 포지션 요약 (live_positions)")
        st.dataframe(lp_df, use_container_width=True)

    # (3) Order attempts — recent 50 rows with Step 8 columns
    st.subheader("주문 시도 로그 (upbit_order_attempts, 최근 50건)")
    try:
        with engine.connect() as conn:
            oa_df = pd.read_sql_query(
                text("""
                    SELECT ts, action, mode, status, uuid, identifier,
                           side, ord_type, price, volume,
                           http_status, latency_ms, remaining_req,
                           retry_count, final_state, error_msg,
                           paper_trade_id
                    FROM upbit_order_attempts
                    WHERE symbol = :sym
                    ORDER BY ts DESC LIMIT 50
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"upbit_order_attempts not available: {e}")
        oa_df = pd.DataFrame()

    if not oa_df.empty:
        total = len(oa_df)
        shadow_n = int((oa_df["mode"] == "shadow").sum())
        test_n = int((oa_df["mode"] == "test").sum())
        live_n = int((oa_df["mode"] == "live").sum())
        error_n = int((oa_df["status"] == "error").sum())
        throttled_n = int((oa_df["status"] == "throttled").sum())
        f1, f2, f3, f4, f5, f6 = st.columns(6)
        f1.metric("Total", total)
        f2.metric("Shadow", shadow_n)
        f3.metric("Test", test_n)
        f4.metric("Live", live_n)
        f5.metric("Errors", error_n)
        f6.metric("Throttled", throttled_n)
        st.dataframe(oa_df, use_container_width=True, height=350)

        # Step 9: remaining-req 마지막 값 표시
        last_rr = oa_df[oa_df["remaining_req"].notna()]["remaining_req"].iloc[0] if oa_df["remaining_req"].notna().any() else None
        if last_rr:
            st.caption(f"최근 remaining-req: `{last_rr}`")
    else:
        st.info("주문 시도 기록 없음 (ShadowExecutionRunner가 paper_trades를 감지하면 자동 생성)")

    # Step 9/11: 24h 상태 분포 집계 (test_ok 강조)
    st.subheader("주문 상태 분포 (최근 24h)")
    try:
        with engine.connect() as conn:
            dist_df = pd.read_sql_query(
                text("""
                    SELECT status, mode, count(*) AS cnt
                    FROM upbit_order_attempts
                    WHERE symbol = :sym
                      AND ts >= now() - interval '24 hours'
                    GROUP BY status, mode
                    ORDER BY cnt DESC
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        dist_df = pd.DataFrame()

    if not dist_df.empty:
        test_ok_total = int(dist_df[dist_df["status"] == "test_ok"]["cnt"].sum()) if "status" in dist_df.columns else 0
        blocked_total = int(dist_df[dist_df["status"] == "blocked"]["cnt"].sum()) if "status" in dist_df.columns else 0
        throttled_total = int(dist_df[dist_df["status"] == "throttled"]["cnt"].sum()) if "status" in dist_df.columns else 0
        ds1, ds2, ds3 = st.columns(3)
        ds1.metric("test_ok (24h)", test_ok_total)
        ds2.metric("blocked (24h)", blocked_total)
        ds3.metric("throttled (24h)", throttled_total)
        st.dataframe(dist_df, use_container_width=True)
    else:
        st.info("24h 데이터 없음")

    # Step 11: blocked_reasons top N
    st.subheader("blocked_reasons 분포 (최근 24h, 상위 8개)")
    try:
        with engine.connect() as conn:
            br_df = pd.read_sql_query(
                text("""
                    SELECT reason, count(*) AS cnt
                    FROM (
                        SELECT jsonb_array_elements_text(blocked_reasons) AS reason
                        FROM upbit_order_attempts
                        WHERE symbol = :sym
                          AND blocked_reasons IS NOT NULL
                          AND ts >= now() - interval '24 hours'
                    ) sub
                    GROUP BY reason
                    ORDER BY cnt DESC
                    LIMIT 8
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        br_df = pd.DataFrame()

    if not br_df.empty:
        st.dataframe(br_df, use_container_width=True)
        st.bar_chart(br_df.set_index("reason")["cnt"])
    else:
        st.info("blocked_reasons 없음 (정상: shadow 모드이거나 test_ok 진행 중)")

    # Step 9: Duplicate identifier 체크 (0건이어야 정상)
    st.subheader("중복 identifier 검사 (0건이어야 정상)")
    try:
        with engine.connect() as conn:
            dup_df = pd.read_sql_query(
                text("""
                    SELECT identifier, mode, count(*) AS cnt
                    FROM upbit_order_attempts
                    WHERE symbol = :sym
                      AND identifier IS NOT NULL
                    GROUP BY identifier, mode
                    HAVING count(*) > 1
                    ORDER BY cnt DESC
                    LIMIT 20
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception:
        dup_df = pd.DataFrame()

    if dup_df.empty:
        st.success("✅ identifier 중복 없음 — DB unique 제약 정상 동작")
    else:
        st.error(f"⚠️ identifier 중복 {len(dup_df)}건 발견!")
        st.dataframe(dup_df, use_container_width=True)

    # (3b) Step 10: Upbit Ready 상태
    st.subheader("Upbit Ready 상태 (Step 10)")
    not_ready_reasons: list[str] = []
    if not has_key:
        not_ready_reasons.append("KEYS_MISSING")

    # Account snapshot freshness
    acct_fresh = False
    snap_lag_sec: float | None = None
    snap_ts_str = "N/A"
    try:
        with engine.connect() as conn:
            snap_row = conn.execute(
                text("""
                    SELECT ts FROM upbit_account_snapshots
                    WHERE symbol = :sym ORDER BY ts DESC LIMIT 1
                """),
                {"sym": settings.SYMBOL},
            ).fetchone()
        if snap_row is not None:
            snap_ts = snap_row.ts
            if snap_ts.tzinfo is None:
                snap_ts = snap_ts.replace(tzinfo=timezone.utc)
            snap_lag_sec = (now_utc - snap_ts).total_seconds()
            threshold_sec = settings.UPBIT_ACCOUNT_POLL_SEC * 3
            acct_fresh = snap_lag_sec <= threshold_sec
            snap_ts_str = str(snap_ts)[:19]
        else:
            snap_lag_sec = None
    except Exception:
        pass

    if not acct_fresh:
        not_ready_reasons.append("ACCOUNT_STALE")

    # remaining-req throttle check (from last order attempt)
    rr_throttled = False
    try:
        with engine.connect() as conn:
            rr_row = conn.execute(
                text("""
                    SELECT remaining_req FROM upbit_order_attempts
                    WHERE symbol = :sym AND remaining_req IS NOT NULL
                    ORDER BY ts DESC LIMIT 1
                """),
                {"sym": settings.SYMBOL},
            ).fetchone()
        if rr_row is not None:
            from app.exchange.upbit_rest import parse_remaining_req as _parse_rr
            parsed_rr = _parse_rr(rr_row.remaining_req)
            sec_val = parsed_rr.get("sec")
            if sec_val is not None and sec_val <= 1:
                rr_throttled = True
                not_ready_reasons.append("THROTTLED")
    except Exception:
        pass

    # test_ok count
    test_ok_cnt = 0
    try:
        with engine.connect() as conn:
            test_ok_cnt = conn.execute(
                text("""
                    SELECT count(*) FROM upbit_order_attempts
                    WHERE symbol = :sym AND mode = 'test' AND status = 'test_ok'
                """),
                {"sym": settings.SYMBOL},
            ).scalar() or 0
    except Exception:
        pass

    # Determine ready label
    if not not_ready_reasons:
        if settings.LIVE_TRADING_ENABLED:
            ready_label = "✅ LIVE READY"
        else:
            ready_label = "✅ TEST READY (실거래 비활성)"
        st.success(ready_label)
    else:
        st.error(f"❌ NOT READY — {', '.join(not_ready_reasons)}")

    r1, r2, r3, r4 = st.columns(4)
    r1.metric("API Keys", "✅ set" if has_key else "❌ not set")
    r2.metric(
        "Account Fresh",
        f"{'✅' if acct_fresh else '❌'} lag={snap_lag_sec:.0f}s" if snap_lag_sec is not None else "❌ no data"
    )
    r3.metric("Throttled", f"{'⚠️ YES' if rr_throttled else '✅ NO'}")
    r4.metric("test_ok 건수", test_ok_cnt)
    st.caption(
        f"마지막 account snapshot: {snap_ts_str}  "
        f"| ACCOUNT_POLL_SEC={settings.UPBIT_ACCOUNT_POLL_SEC}  "
        f"| freshness_threshold={settings.UPBIT_ACCOUNT_POLL_SEC * 3}s"
    )
    if not_ready_reasons:
        st.caption(f"Not ready 사유: {not_ready_reasons}")

    # (4) Order snapshots (live mode only)
    st.subheader("주문 상태 스냅샷 (upbit_order_snapshots, 최근 50건 — live 모드 전용)")
    try:
        with engine.connect() as conn:
            os_df = pd.read_sql_query(
                text("""
                    SELECT ts, uuid, state, side, ord_type, price, volume,
                           remaining_volume, executed_volume, paid_fee
                    FROM upbit_order_snapshots
                    WHERE symbol = :sym
                    ORDER BY ts DESC LIMIT 50
                """),
                conn,
                params={"sym": settings.SYMBOL},
            )
    except Exception as e:
        st.warning(f"upbit_order_snapshots not available: {e}")
        os_df = pd.DataFrame()

    if not os_df.empty:
        st.dataframe(os_df, use_container_width=True, height=300)
    else:
        st.info("주문 스냅샷 없음 (live 모드에서 실주문 시 uuid 폴링으로 자동 생성)")

    # ══════════════════════════════════════════════════════════
    # [G] Alt Data (Binance / Coinglass)
    # ══════════════════════════════════════════════════════════
    st.header("[G] Alt Data (Binance Futures / Coinglass)")

    alt_sym = settings.ALT_SYMBOL_BINANCE
    cg_sym = settings.ALT_SYMBOL_COINGLASS
    poll_sec = settings.BINANCE_POLL_SEC

    # ── G1: Binance WS Health ─────────────────────────────────
    st.subheader("G1 — Binance WS Health (mark price)")
    try:
        with engine.connect() as conn:
            mp_row = conn.execute(
                text("""
                    SELECT max(ts) as last_ts,
                           count(*) FILTER (
                               WHERE ts >= now() AT TIME ZONE 'UTC' - interval '300 seconds'
                           ) as cnt_5m
                    FROM binance_mark_price_1s
                    WHERE symbol = :sym
                """),
                {"sym": alt_sym},
            ).fetchone()
    except Exception as e:
        st.warning(f"binance_mark_price_1s not available: {e}")
        mp_row = None

    if mp_row is not None:
        last_ts_mp = mp_row.last_ts
        cnt_5m = mp_row.cnt_5m or 0
        fill_5m = cnt_5m / 300 if cnt_5m is not None else 0
        if last_ts_mp is not None:
            if last_ts_mp.tzinfo is None:
                last_ts_mp = last_ts_mp.replace(tzinfo=__import__("datetime").timezone.utc)
            lag_mp = (now_utc - last_ts_mp).total_seconds()
        else:
            lag_mp = None

        g1c1, g1c2, g1c3 = st.columns(3)
        g1c1.metric("Last Insert", str(last_ts_mp)[:19] if last_ts_mp else "N/A")
        g1c2.metric("Lag (sec)", f"{lag_mp:.1f}" if lag_mp is not None else "N/A")
        g1c3.metric("Fill Rate 5min", f"{fill_5m*100:.1f}% ({cnt_5m}/300)")
    else:
        st.info("mark price 데이터 없음 (bot 실행 후 대기)")

    # ── G1b: Force Orders ─────────────────────────────────────
    st.subheader("G1b — Binance Force Orders (24h)")
    try:
        with engine.connect() as conn:
            fo_cnt = conn.execute(
                text("""
                    SELECT count(*) FROM binance_force_orders
                    WHERE symbol=:sym
                      AND ts >= now() AT TIME ZONE 'UTC' - interval '86400 seconds'
                """),
                {"sym": alt_sym},
            ).scalar() or 0
    except Exception:
        fo_cnt = 0
    st.metric("Liquidation Events (24h)", fo_cnt)
    st.caption("이벤트 0건도 정상 — 청산이 없을 수 있음. WS 연결 상태 기준.")

    # ── G2: Binance Futures Metrics ───────────────────────────
    st.subheader("G2 — Binance Futures Metrics (최근 6h)")
    try:
        with engine.connect() as conn:
            bfm_df = pd.read_sql_query(
                text("""
                    SELECT ts, metric, value, value2, period
                    FROM binance_futures_metrics
                    WHERE symbol=:sym
                      AND ts >= now() AT TIME ZONE 'UTC' - interval '21600 seconds'
                    ORDER BY ts DESC LIMIT 200
                """),
                conn,
                params={"sym": alt_sym},
            )
    except Exception as e:
        st.warning(f"binance_futures_metrics not available: {e}")
        bfm_df = pd.DataFrame()

    if not bfm_df.empty:
        metrics_to_show = ["open_interest", "global_ls_ratio", "taker_ls_ratio", "basis"]
        for m in metrics_to_show:
            sub = bfm_df[bfm_df["metric"] == m].sort_values("ts")
            if sub.empty:
                st.caption(f"{m}: no data")
                continue
            latest = sub.iloc[-1]
            lag_m = (now_utc - pd.to_datetime(latest["ts"], utc=True)).total_seconds()
            st.metric(
                m,
                f"{latest['value']:.6g}" if pd.notna(latest["value"]) else "N/A",
                delta=f"lag={lag_m:.0f}s",
            )
        st.dataframe(bfm_df, use_container_width=True, height=250)
    else:
        st.info(f"Binance metrics 없음 (poll 주기={poll_sec}s, 첫 데이터 대기 중)")

    # ── G3: Coinglass ─────────────────────────────────────────
    st.subheader("G3 — Coinglass Liquidation Map")
    cg_key_set = is_real_key(settings.COINGLASS_API_KEY)
    cg_enabled = getattr(settings, "COINGLASS_ENABLED", False)

    # 상태 배너
    if cg_enabled and not cg_key_set:
        st.error("❌ 설정 오류: COINGLASS_ENABLED=True 이나 API 키 비정상 — .env에서 실제 키 입력 필요")
    elif cg_enabled and cg_key_set:
        st.success("✅ Coinglass 활성 (COINGLASS_ENABLED=True, 키 설정됨)")
    else:
        st.info(f"COINGLASS_API_KEY: {'✅ 실키 설정됨' if cg_key_set else '❌ 미설정/비정상'}  |  COINGLASS_ENABLED=False → SKIP")

    # call_status 패널
    try:
        with engine.connect() as conn:
            cgs_last = conn.execute(
                text("""
                    SELECT ok, ts, http_status, error_msg, latency_ms, poll_count
                    FROM coinglass_call_status
                    ORDER BY ts DESC LIMIT 1
                """)
            ).fetchone()
            cgs_ok24 = conn.execute(
                text("""
                    SELECT
                        count(*) FILTER (WHERE ok=true) AS ok_cnt,
                        count(*) AS total
                    FROM coinglass_call_status
                    WHERE ts >= now() - interval '24 hours'
                """)
            ).fetchone()
    except Exception:
        cgs_last = None
        cgs_ok24 = None

    if cgs_last:
        ca, cb, cc = st.columns(3)
        ca.metric("Last Call OK", "✅ YES" if cgs_last.ok else "❌ NO")
        cb.metric("Last Call TS", str(cgs_last.ts)[:19] if cgs_last.ts else "N/A")
        cc.metric("24h Success", f"{cgs_ok24.ok_cnt}/{cgs_ok24.total}" if cgs_ok24 else "N/A")
        if not cgs_last.ok and cgs_last.error_msg:
            st.caption(f"마지막 실패 원인: http={cgs_last.http_status} {cgs_last.error_msg[:150]}")

    try:
        with engine.connect() as conn:
            cg_df = pd.read_sql_query(
                text("""
                    SELECT ts, symbol, exchange, timeframe, summary_json
                    FROM coinglass_liquidation_map
                    WHERE symbol=:sym
                    ORDER BY ts DESC LIMIT 5
                """),
                conn,
                params={"sym": cg_sym},
            )
    except Exception as e:
        st.warning(f"coinglass_liquidation_map not available: {e}")
        cg_df = pd.DataFrame()

    if not cg_df.empty:
        cg_last = cg_df.iloc[0]
        cg_ts = pd.to_datetime(cg_last["ts"], utc=True)
        cg_lag = max(0.0, (now_utc - cg_ts).total_seconds())
        st.metric("Last Poll", str(cg_ts)[:19], delta=f"lag={cg_lag:.0f}s")
        st.dataframe(cg_df, use_container_width=True, height=200)
    else:
        if cg_enabled and cg_key_set:
            st.info("Coinglass 데이터 없음 (첫 poll 대기 중)")
        else:
            st.info("COINGLASS_ENABLED=False 또는 키 미설정 — 수집하려면 .env에서 설정 필요")


    # ══════════════════════════════════════════════════════════
    # [G4] Feature Snapshots
    # ══════════════════════════════════════════════════════════
    st.subheader("G4 — Feature Snapshots (학습/모델 입력용)")

    fs_sym = settings.SYMBOL
    interval_sec = settings.DECISION_INTERVAL_SEC

    # (1) Metrics: lag, fill_rate, null_rates
    try:
        with engine.connect() as conn:
            fs_meta = conn.execute(
                text("""
                    SELECT max(ts) AS last_ts,
                           count(*) FILTER (
                               WHERE ts >= now() AT TIME ZONE 'UTC' - interval '300 seconds'
                           ) AS cnt_5min,
                           count(*) FILTER (
                               WHERE mid_krw IS NULL
                                 AND ts >= now() AT TIME ZONE 'UTC' - interval '300 seconds'
                           ) AS null_mid,
                           count(*) FILTER (
                               WHERE p_none IS NULL
                                 AND ts >= now() AT TIME ZONE 'UTC' - interval '300 seconds'
                           ) AS null_p_none,
                           count(*) FILTER (
                               WHERE bin_funding_rate IS NULL
                                 AND ts >= now() AT TIME ZONE 'UTC' - interval '300 seconds'
                           ) AS null_funding,
                           count(*) FILTER (
                               WHERE oi_value IS NULL
                                 AND ts >= now() AT TIME ZONE 'UTC' - interval '300 seconds'
                           ) AS null_oi
                    FROM feature_snapshots
                    WHERE symbol = :sym
                """),
                {"sym": fs_sym},
            ).fetchone()
    except Exception as e:
        st.warning(f"feature_snapshots not available: {e}")
        fs_meta = None

    if fs_meta is not None:
        last_ts_fs = fs_meta.last_ts
        cnt_5min = fs_meta.cnt_5min or 0
        expected_5min = 300 // interval_sec  # e.g. 60
        fill_5m = cnt_5min / expected_5min if expected_5min > 0 else 0.0

        if last_ts_fs is not None:
            if last_ts_fs.tzinfo is None:
                last_ts_fs = last_ts_fs.replace(tzinfo=__import__("datetime").timezone.utc)
            lag_fs = max(0.0, (now_utc - last_ts_fs).total_seconds())
        else:
            lag_fs = None

        total_5m = cnt_5min or 1
        null_mid = (fs_meta.null_mid or 0) / total_5m
        null_p = (fs_meta.null_p_none or 0) / total_5m
        null_fund = (fs_meta.null_funding or 0) / total_5m
        null_oi = (fs_meta.null_oi or 0) / total_5m

        g4c1, g4c2, g4c3, g4c4 = st.columns(4)
        g4c1.metric("Last TS", str(last_ts_fs)[:19] if last_ts_fs else "N/A")
        g4c2.metric("Lag (sec)", f"{lag_fs:.1f}" if lag_fs is not None else "N/A")
        g4c3.metric("Fill Rate 5min", f"{fill_5m*100:.1f}% ({cnt_5min}/{expected_5min})")
        g4c4.metric("null mid_krw", f"{null_mid*100:.1f}%")

        n1, n2, n3, n4 = st.columns(4)
        n1.metric("null p_none", f"{null_p*100:.1f}%")
        n2.metric("null bin_funding_rate", f"{null_fund*100:.1f}%")
        n3.metric("null oi_value", f"{null_oi*100:.1f}%")

        # source_ts 누수/신선도 지표
        st.subheader("G4 — Source TS 누수/신선도 지표")
        try:
            with engine.connect() as conn:
                leak_row = conn.execute(
                    text("""
                        SELECT
                            count(*) FILTER (WHERE bin_mark_ts IS NOT NULL AND bin_mark_ts > ts) AS mark_leaks,
                            count(*) FILTER (WHERE oi_ts IS NOT NULL AND oi_ts > ts) AS oi_leaks,
                            count(*) FILTER (WHERE liq_last_ts IS NOT NULL AND liq_last_ts > ts) AS liq_leaks,
                            avg(EXTRACT(EPOCH FROM (ts - bin_mark_ts))) FILTER (WHERE bin_mark_ts IS NOT NULL) AS avg_mark_age_sec,
                            avg(EXTRACT(EPOCH FROM (ts - oi_ts))) FILTER (WHERE oi_ts IS NOT NULL) AS avg_oi_age_sec
                        FROM feature_snapshots
                        WHERE symbol = :sym
                          AND ts >= now() AT TIME ZONE 'UTC' - interval '300 seconds'
                    """),
                    {"sym": fs_sym},
                ).fetchone()
        except Exception as e:
            leak_row = None
            st.warning(f"source_ts 지표 조회 오류: {e}")

        if leak_row:
            total_leaks = (leak_row.mark_leaks or 0) + (leak_row.oi_leaks or 0) + (leak_row.liq_leaks or 0)
            if total_leaks > 0:
                st.error(f"❌ 누수 위반 {total_leaks}건! mark={leak_row.mark_leaks} oi={leak_row.oi_leaks} liq={leak_row.liq_leaks}")
            else:
                st.success("✅ 누수 위반 없음 (source_ts <= snapshot_ts)")
            l1, l2 = st.columns(2)
            l1.metric("avg bin_mark_age (sec)", f"{leak_row.avg_mark_age_sec:.1f}" if leak_row.avg_mark_age_sec is not None else "N/A")
            l2.metric("avg oi_age (sec)", f"{leak_row.avg_oi_age_sec:.1f}" if leak_row.avg_oi_age_sec is not None else "N/A")
    else:
        st.info("feature_snapshots 데이터 없음 (bot 실행 후 대기)")

    # (2) Charts: 최근 6h 시계열
    st.subheader("G4 Charts — 최근 6h")
    try:
        with engine.connect() as conn:
            fs_chart_df = pd.read_sql_query(
                text("""
                    SELECT ts, bin_funding_rate, oi_value, liq_5m_notional, ev_rate
                    FROM feature_snapshots
                    WHERE symbol = :sym
                      AND ts >= now() AT TIME ZONE 'UTC' - interval '21600 seconds'
                    ORDER BY ts ASC
                """),
                conn,
                params={"sym": fs_sym},
            )
    except Exception as e:
        st.warning(f"feature_snapshots chart data not available: {e}")
        fs_chart_df = pd.DataFrame()

    if not fs_chart_df.empty:
        fs_chart_df = fs_chart_df.set_index("ts")
        col_a, col_b = st.columns(2)
        with col_a:
            st.caption("bin_funding_rate")
            if fs_chart_df["bin_funding_rate"].notna().any():
                st.line_chart(fs_chart_df["bin_funding_rate"].dropna())
            else:
                st.info("funding_rate 데이터 없음")
            st.caption("oi_value (open interest)")
            if fs_chart_df["oi_value"].notna().any():
                st.line_chart(fs_chart_df["oi_value"].dropna())
            else:
                st.info("oi_value 데이터 없음")
        with col_b:
            st.caption("liq_5m_notional (5분 청산)")
            if fs_chart_df["liq_5m_notional"].notna().any():
                st.line_chart(fs_chart_df["liq_5m_notional"].dropna())
            else:
                st.info("liq_5m_notional 데이터 없음")
            st.caption("ev_rate (기대 수익률)")
            if fs_chart_df["ev_rate"].notna().any():
                st.line_chart(fs_chart_df["ev_rate"].dropna())
            else:
                st.info("ev_rate 데이터 없음")
    else:
        st.info("6h 차트 데이터 없음 (bot 실행 후 대기)")

    # (3) Table: 최근 50행
    st.subheader("G4 Table — 최근 50행")
    try:
        with engine.connect() as conn:
            fs_df = pd.read_sql_query(
                text("""
                    SELECT ts, p_none, ev_rate, action_hat,
                           bin_mark_price, bin_funding_rate,
                           oi_value, liq_5m_notional,
                           mid_krw, spread_bps, barrier_status
                    FROM feature_snapshots
                    WHERE symbol = :sym
                    ORDER BY ts DESC LIMIT 50
                """),
                conn,
                params={"sym": fs_sym},
            )
    except Exception as e:
        st.warning(f"feature_snapshots table not available: {e}")
        fs_df = pd.DataFrame()

    if not fs_df.empty:
        st.dataframe(fs_df, use_container_width=True, height=400)
    else:
        st.info("feature_snapshots 데이터 없음")


if __name__ == "__main__":
    main()
