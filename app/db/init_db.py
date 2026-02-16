from sqlalchemy.engine import Engine

from app.db.models import Base


def ensure_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)
