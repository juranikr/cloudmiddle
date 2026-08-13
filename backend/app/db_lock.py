"""Small cross-process transaction lock used for idempotent agent writes."""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.orm import Session


def transaction_lock(db: Session, scope: str) -> None:
    """Serialize one logical write scope on PostgreSQL; SQLite is already serialized."""

    if db.get_bind().dialect.name != "postgresql":
        return
    raw = hashlib.sha256(scope.encode("utf-8")).digest()[:8]
    key = int.from_bytes(raw, byteorder="big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


__all__ = ["transaction_lock"]
