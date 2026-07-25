from sqlalchemy import inspect, text

from app.db import engine


def ensure_schema() -> None:
    """기존 DB에 컬럼/테이블을 점진 추가."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    with engine.begin() as conn:
        if "markers" in tables:
            cols = {c["name"] for c in insp.get_columns("markers")}
            if "shape" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN shape VARCHAR(20) DEFAULT 'point' NOT NULL"))
            if "polygon" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN polygon TEXT"))
            if "agent_context" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN agent_context TEXT DEFAULT '' NOT NULL"))
            if "merged_into_id" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN merged_into_id INTEGER"))
            if "is_agent_suggested" not in cols:
                # Postgres는 boolean 기본값에 FALSE 필요 (0은 integer로 거부됨)
                default = "FALSE" if engine.dialect.name == "postgresql" else "0"
                conn.execute(
                    text(
                        f"ALTER TABLE markers ADD COLUMN is_agent_suggested BOOLEAN DEFAULT {default} NOT NULL"
                    )
                )

        # 기여자 백필: 기존 markers.user_id → place_contributors
        tables_now = set(inspect(engine).get_table_names())
        if "markers" in tables_now and "place_contributors" in tables_now:
            conn.execute(
                text(
                    """
                    INSERT INTO place_contributors (place_id, user_id)
                    SELECT m.id, m.user_id FROM markers m
                    WHERE m.user_id IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM place_contributors pc
                        WHERE pc.place_id = m.id AND pc.user_id = m.user_id
                      )
                    """
                )
            )

        # Postgres: markers.user_id를 nullable로 (에이전트 제안 장소)
        if "markers" in tables_now and engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE markers ALTER COLUMN user_id DROP NOT NULL"))

        # 기존 마커에 이력이 없으면 create 이벤트를 미읽음으로 백필 (에이전트가 아직 안 본 상태)
        if "markers" in tables_now and "place_events" in tables_now:
            conn.execute(
                text(
                    """
                    INSERT INTO place_events (place_id, user_id, actor, action, summary, payload, groq_read_at, created_at)
                    SELECT
                      m.id,
                      m.user_id,
                      'system',
                      'create',
                      '기존 장소 백필: ' || m.title,
                      '{}',
                      NULL,
                      m.created_at
                    FROM markers m
                    WHERE NOT EXISTS (
                      SELECT 1 FROM place_events pe WHERE pe.place_id = m.id
                    )
                    """
                )
            )



        # 에이전트가 남긴 이력은 미읽음에서 제외
        if "place_events" in tables_now:
            conn.execute(
                text(
                    """
                    UPDATE place_events
                    SET groq_read_at = COALESCE(groq_read_at, created_at, CURRENT_TIMESTAMP)
                    WHERE actor = 'agent' AND groq_read_at IS NULL
                    """
                )
            )

def clear_all_markers() -> int:
    with engine.begin() as conn:
        names = set(inspect(engine).get_table_names())
        if "place_events" in names:
            conn.execute(text("DELETE FROM place_events"))
        if "place_images" in names:
            conn.execute(text("DELETE FROM place_images"))
        if "place_contributors" in names:
            conn.execute(text("DELETE FROM place_contributors"))
        if "markers" not in names:
            return 0
        result = conn.execute(text("DELETE FROM markers"))
        return result.rowcount or 0
