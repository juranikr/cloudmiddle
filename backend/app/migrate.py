from sqlalchemy import inspect, text

from app.db import engine


def ensure_schema() -> None:
    """기존 DB에 컬럼/테이블을 점진 추가."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    with engine.begin() as conn:
        if "cities" in tables:
            city_cols = {c["name"] for c in insp.get_columns("cities")}
            if "search_context" not in city_cols:
                conn.execute(text("ALTER TABLE cities ADD COLUMN search_context VARCHAR(200) DEFAULT '' NOT NULL"))

        if engine.dialect.name == "postgresql":
            conn.execute(text("""
                INSERT INTO cities (id, slug, name_ko, name_local, country_code, center_lat, center_lng,
                                    default_zoom, search_viewbox, search_context, status, sort_order)
                VALUES
                  (1, 'jinan', '지난', '济南', 'CN', 36.6512, 117.1201, 12,
                   '116.70,36.95,117.55,36.35', '济南市 山东省 中国', 'active', 10),
                  (2, 'shenyang', '선양', '沈阳', 'CN', 41.8057, 123.4315, 12,
                   '122.85,42.15,123.85,41.45', '沈阳市 辽宁省 中国', 'active', 20)
                ON CONFLICT (id) DO UPDATE SET
                  slug = EXCLUDED.slug, name_ko = EXCLUDED.name_ko, name_local = EXCLUDED.name_local,
                  center_lat = EXCLUDED.center_lat, center_lng = EXCLUDED.center_lng,
                  default_zoom = EXCLUDED.default_zoom, search_viewbox = EXCLUDED.search_viewbox,
                  search_context = EXCLUDED.search_context,
                  status = EXCLUDED.status, sort_order = EXCLUDED.sort_order
            """))
            conn.execute(text("""
                SELECT setval(pg_get_serial_sequence('cities', 'id'),
                              GREATEST((SELECT max(id) FROM cities), 1), true)
            """))
        else:
            conn.execute(text("""
                INSERT OR IGNORE INTO cities
                  (id, slug, name_ko, name_local, country_code, center_lat, center_lng,
                   default_zoom, search_viewbox, search_context, status, sort_order)
                VALUES
                  (1, 'jinan', '지난', '济南', 'CN', 36.6512, 117.1201, 12,
                   '116.70,36.95,117.55,36.35', '济南市 山东省 中国', 'active', 10),
                  (2, 'shenyang', '선양', '沈阳', 'CN', 41.8057, 123.4315, 12,
                   '122.85,42.15,123.85,41.45', '沈阳市 辽宁省 中国', 'active', 20)
            """))
            conn.execute(text("UPDATE cities SET search_context = '济南市 山东省 中国' WHERE id = 1"))
            conn.execute(text("UPDATE cities SET search_context = '沈阳市 辽宁省 中国' WHERE id = 2"))
        if "markers" in tables:
            cols = {c["name"] for c in insp.get_columns("markers")}
            if "city_id" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN city_id INTEGER DEFAULT 1"))
                conn.execute(text("UPDATE markers SET city_id = 1 WHERE city_id IS NULL"))
                if engine.dialect.name == "postgresql":
                    conn.execute(text("ALTER TABLE markers ALTER COLUMN city_id SET NOT NULL"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_markers_city_id ON markers (city_id)"))
            if "zone_id" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN zone_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_markers_zone_id ON markers (zone_id)"))
            if "chain_id" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN chain_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_markers_chain_id ON markers (chain_id)"))
            if "branch_name" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN branch_name VARCHAR(120) DEFAULT '' NOT NULL"))
            if "travel_role" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN travel_role VARCHAR(30) DEFAULT 'general' NOT NULL"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_markers_travel_role ON markers (travel_role)"))
                # Backfill a useful baseline without pretending category and travel role are identical.
                conn.execute(text("UPDATE markers SET travel_role = 'food' WHERE category IN ('restaurant', 'drink')"))
                conn.execute(text("UPDATE markers SET travel_role = 'rest' WHERE category = 'lodging'"))
                conn.execute(text("UPDATE markers SET travel_role = 'shopping' WHERE category = 'shopping'"))
                conn.execute(text("UPDATE markers SET travel_role = 'practical' WHERE category IN ('transport', 'convenience')"))
                conn.execute(text("UPDATE markers SET travel_role = 'history' WHERE category = 'tourist' AND (title LIKE '%博物馆%' OR title LIKE '%博物館%' OR title LIKE '%故宫%' OR title LIKE '%宫%' OR title LIKE '%역사%' OR title LIKE '%박물관%')"))
                conn.execute(text("UPDATE markers SET travel_role = 'market_night' WHERE title LIKE '%市场%' OR title LIKE '%夜市%' OR title LIKE '%시장%'"))
                conn.execute(text("UPDATE markers SET travel_role = 'nature' WHERE title LIKE '%公园%' OR title LIKE '%山%' OR title LIKE '%공원%'"))
            if "shape" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN shape VARCHAR(20) DEFAULT 'point' NOT NULL"))
            if "polygon" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN polygon TEXT"))
            marker_text_columns = {
                "coordinate_source": ("VARCHAR(50)", "manual"),
                "coordinate_external_id": ("VARCHAR(200)", ""),
                "coordinate_query": ("VARCHAR(300)", ""),
                "coordinate_source_url": ("VARCHAR(1000)", ""),
                "coordinate_crs": ("VARCHAR(20)", "WGS84"),
            }
            for column, (sql_type, default_value) in marker_text_columns.items():
                if column not in cols:
                    escaped = default_value.replace("'", "''")
                    conn.execute(
                        text(
                            f"ALTER TABLE markers ADD COLUMN {column} {sql_type} "
                            f"DEFAULT '{escaped}' NOT NULL"
                        )
                    )
            if "coordinate_confidence" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN coordinate_confidence FLOAT"))
            if "coordinate_verified_at" not in cols:
                verified_type = (
                    "TIMESTAMP WITH TIME ZONE"
                    if engine.dialect.name == "postgresql"
                    else "TIMESTAMP"
                )
                conn.execute(
                    text(f"ALTER TABLE markers ADD COLUMN coordinate_verified_at {verified_type}")
                )
            if "agent_context" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN agent_context TEXT DEFAULT '' NOT NULL"))
            if "merged_into_id" not in cols:
                conn.execute(text("ALTER TABLE markers ADD COLUMN merged_into_id INTEGER"))
            if "last_verified_at" not in cols:
                col_type = (
                    "TIMESTAMP WITH TIME ZONE"
                    if engine.dialect.name == "postgresql"
                    else "TIMESTAMP"
                )
                conn.execute(text(f"ALTER TABLE markers ADD COLUMN last_verified_at {col_type}"))
            if "is_agent_suggested" not in cols:
                # Postgres는 boolean 기본값에 FALSE 필요 (0은 integer로 거부됨)
                default = "FALSE" if engine.dialect.name == "postgresql" else "0"
                conn.execute(
                    text(
                        f"ALTER TABLE markers ADD COLUMN is_agent_suggested BOOLEAN DEFAULT {default} NOT NULL"
                    )
                )

        if "agent_knowledge" in tables:
            knowledge_cols = {c["name"] for c in insp.get_columns("agent_knowledge")}
            if "scope" not in knowledge_cols:
                conn.execute(text("ALTER TABLE agent_knowledge ADD COLUMN scope VARCHAR(20) DEFAULT 'global' NOT NULL"))
            if "city_id" not in knowledge_cols:
                conn.execute(text("ALTER TABLE agent_knowledge ADD COLUMN city_id INTEGER"))
                if engine.dialect.name == "postgresql":
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_knowledge_city_id ON agent_knowledge (city_id)"))
            knowledge_additions = {
                "category": "VARCHAR(30) DEFAULT 'playbook' NOT NULL",
                "summary": "TEXT DEFAULT '' NOT NULL",
                "principles": "TEXT DEFAULT '[]' NOT NULL",
                "next_actions": "TEXT DEFAULT '[]' NOT NULL",
                "evidence_count": "INTEGER DEFAULT 0 NOT NULL",
                "quality_score": "FLOAT DEFAULT 0.7 NOT NULL",
                "status": "VARCHAR(20) DEFAULT 'active' NOT NULL",
                "version": "INTEGER DEFAULT 1 NOT NULL",
            }
            for column, ddl in knowledge_additions.items():
                if column not in knowledge_cols:
                    conn.execute(text(f"ALTER TABLE agent_knowledge ADD COLUMN {column} {ddl}"))

        if "agent_search_logs" in tables:
            search_cols = {c["name"] for c in insp.get_columns("agent_search_logs")}
            if "city_id" not in search_cols:
                conn.execute(text("ALTER TABLE agent_search_logs ADD COLUMN city_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_search_logs_city_id ON agent_search_logs (city_id)"))

        if "agent_search_results" in tables:
            result_cols = {c["name"] for c in insp.get_columns("agent_search_results")}
            if "city_id" not in result_cols:
                conn.execute(text("ALTER TABLE agent_search_results ADD COLUMN city_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_search_results_city_id ON agent_search_results (city_id)"))

        if "agent_web_visits" in tables:
            visit_cols = {c["name"] for c in insp.get_columns("agent_web_visits")}
            if "city_id" not in visit_cols:
                conn.execute(text("ALTER TABLE agent_web_visits ADD COLUMN city_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_web_visits_city_id ON agent_web_visits (city_id)"))

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
