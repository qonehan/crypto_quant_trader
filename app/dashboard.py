import streamlit as st
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


if __name__ == "__main__":
    main()
