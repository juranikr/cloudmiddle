from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

_engine_kwargs: dict = {"pool_pre_ping": True}
if settings.is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.database_url, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
