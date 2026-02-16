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
    settings = load_settings()
    engine = get_engine(settings)

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("Boot OK")
        print("DB OK")
    except Exception as e:
        err = str(e)
        if "failed to resolve host" in err or "could not translate host name" in err:
            print(f"DB connection failed: {e}\n{DB_RESOLVE_HINT}")
        else:
            print(f"DB connection failed: {e}")
        raise


if __name__ == "__main__":
    main()
