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


if __name__ == "__main__":
    main()
