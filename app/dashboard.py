import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from sqlalchemy import text

from app.config import load_settings
from app.db.session import get_engine

DB_RESOLVE_HINT = (
    "DB host 'db'를 찾지 못했습니다. "
    "Codespaces에서 Dev Containers: Rebuild and Reopen in Container를 실행해 "
    "docker-compose devcontainer로 들어가 있는지 확인하세요. "
    "또한 db 컨테이너가 정상 실행 중인지 확인하세요."
)


def main() -> None:
    st.set_page_config(page_title="BTC Quant Bot", layout="wide")
    st.title("BTC Quant Bot - Prototype v0")

    settings = load_settings()

    st.subheader("Settings")
    st.write(f"**SYMBOL:** {settings.SYMBOL}")
    st.write(f"**MODE:** {settings.MODE}")

    st.subheader("DB Connection Test")
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
            # last 60 rows
            df60 = pd.read_sql_query(
                text(
                    "SELECT ts, symbol, mid, bid, ask, spread, trade_count_1s, "
                    "trade_volume_1s, imbalance_top5, last_trade_price, last_trade_side "
                    "FROM market_1s ORDER BY ts DESC LIMIT 60"
                ),
                conn,
            )
            # last 300 rows for chart
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

    # lag
    last_ts = pd.to_datetime(df60["ts"].iloc[0], utc=True)
    now_utc = datetime.now(timezone.utc)
    lag_sec = (now_utc - last_ts).total_seconds()
    st.metric("Lag (sec)", f"{lag_sec:.1f}")

    st.dataframe(df60, use_container_width=True, height=400)

    # mid chart (5 min)
    if not df300.empty:
        st.subheader("Mid Price — Last 5 min")
        chart_df = df300.sort_values("ts")
        chart_df = chart_df.set_index("ts")
        st.line_chart(chart_df["mid"])

    # ── Barrier State ──────────────────────────────────────
    st.subheader("Barrier State")

    try:
        with engine.connect() as conn:
            bs_latest = pd.read_sql_query(
                text(
                    "SELECT ts, symbol, r_t, sigma_1s, sigma_h, status, sample_n, "
                    "h_sec, vol_window_sec, r_min, k_vol, error "
                    "FROM barrier_state ORDER BY ts DESC LIMIT 1"
                ),
                conn,
            )
            bs_recent = pd.read_sql_query(
                text(
                    "SELECT ts, symbol, r_t, sigma_1s, sigma_h, status, sample_n "
                    "FROM barrier_state ORDER BY ts DESC LIMIT 20"
                ),
                conn,
            )
            bs_chart = pd.read_sql_query(
                text(
                    "SELECT ts, r_t, sigma_h "
                    "FROM barrier_state ORDER BY ts DESC LIMIT 360"
                ),
                conn,
            )
    except Exception as e:
        st.warning(f"barrier_state table not available yet: {e}")
        bs_latest = pd.DataFrame()
        bs_recent = pd.DataFrame()
        bs_chart = pd.DataFrame()

    if bs_latest.empty:
        st.info("No barrier_state rows yet. Wait for the first decision tick.")
    else:
        row = bs_latest.iloc[0]
        barrier_ts = pd.to_datetime(row["ts"], utc=True)
        barrier_lag = (now_utc - barrier_ts).total_seconds()

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("r_t", f"{row['r_t']:.6f}")
        col2.metric("sigma_h", f"{row['sigma_h']:.8f}" if pd.notna(row["sigma_h"]) else "N/A")
        col3.metric("Status", row["status"])
        col4.metric("Barrier Lag (sec)", f"{barrier_lag:.1f}")

        st.write(
            f"**sample_n:** {row['sample_n']}  |  "
            f"**h_sec:** {row['h_sec']}  |  "
            f"**vol_window_sec:** {row['vol_window_sec']}  |  "
            f"**sigma_1s:** {row['sigma_1s']:.8f}" if pd.notna(row.get("sigma_1s")) else
            f"**sample_n:** {row['sample_n']}  |  "
            f"**h_sec:** {row['h_sec']}  |  "
            f"**vol_window_sec:** {row['vol_window_sec']}  |  "
            f"**sigma_1s:** N/A"
        )

    if not bs_chart.empty:
        st.subheader("r_t — Last 30 min")
        c1 = bs_chart.sort_values("ts").set_index("ts")
        st.line_chart(c1["r_t"])

        st.subheader("sigma_h — Last 30 min")
        c2 = c1.dropna(subset=["sigma_h"])
        if not c2.empty:
            st.line_chart(c2["sigma_h"])

    if not bs_recent.empty:
        st.subheader("barrier_state — Recent 20 rows")
        st.dataframe(bs_recent, use_container_width=True, height=400)


if __name__ == "__main__":
    main()
