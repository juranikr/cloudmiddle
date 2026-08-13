from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import Settings, settings


def engine_kwargs_for(config: Settings) -> dict[str, Any]:
    """Build engine options, including a DB-level read-only backstop."""

    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if config.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    elif config.is_production_readonly:
        # libpq applies this before SQLAlchemy can start its first transaction.
        # It therefore protects callers outside FastAPI as well as HTTP routes.
        kwargs["connect_args"] = {
            "options": "-c default_transaction_read_only=on",
        }
    return kwargs


engine = create_engine(settings.database_url, **engine_kwargs_for(settings))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator:
    db = SessionLocal()
    try:
        if settings.is_production_readonly:
            # Keep an explicit transaction-local guard in addition to libpq's
            # session default. A read-only DB role remains required in production.
            db.execute(text("SET TRANSACTION READ ONLY"))
        yield db
    finally:
        db.close()
