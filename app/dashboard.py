import json

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from sqlalchemy import text

from app.config import load_settings
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
                    "u_exec, d_exec, h_sec, entry_r_t, entry_ev_rate, entry_p_none, updated_at "
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
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Status", pp["status"])
        p1.metric("Cash (KRW)", f"{pp['cash_krw']:,.0f}")
        p2.metric("Qty", f"{pp['qty']:.8f}")
        p2.metric("Entry Price", f"{pp['entry_price']:,.0f}" if pd.notna(pp["entry_price"]) else "N/A")
        p3.metric("u_exec", f"{pp['u_exec']:,.0f}" if pd.notna(pp["u_exec"]) else "N/A")
        p3.metric("d_exec", f"{pp['d_exec']:,.0f}" if pd.notna(pp["d_exec"]) else "N/A")
        p4.metric("entry_r_t", f"{pp['entry_r_t']:.6f}" if pd.notna(pp["entry_r_t"]) else "N/A")
        st.caption(f"Updated at: {pp['updated_at']}")
    else:
        st.info("No paper position yet.")

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
                    "spread_bps, lag_sec, cost_roundtrip_est, r_t "
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


if __name__ == "__main__":
    main()
