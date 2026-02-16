from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from app.config import Settings


def get_engine(settings: Settings) -> Engine:
    return create_engine(settings.DB_URL, echo=False)


def get_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)
