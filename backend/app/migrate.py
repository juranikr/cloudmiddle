from sqlalchemy import inspect, text

from app.db import engine


def ensure_schema() -> None:
    """기존 SQLite 테이블에 shape/polygon 컬럼이 없으면 추가한다."""
    insp = inspect(engine)
    if "markers" not in insp.get_table_names():
        return

    cols = {c["name"] for c in insp.get_columns("markers")}
    with engine.begin() as conn:
        if "shape" not in cols:
            conn.execute(text("ALTER TABLE markers ADD COLUMN shape VARCHAR(20) DEFAULT 'point' NOT NULL"))
        if "polygon" not in cols:
            conn.execute(text("ALTER TABLE markers ADD COLUMN polygon TEXT"))


def clear_all_markers() -> int:
    with engine.begin() as conn:
        if "markers" not in inspect(engine).get_table_names():
            return 0
        result = conn.execute(text("DELETE FROM markers"))
        return result.rowcount or 0
