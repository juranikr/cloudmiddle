from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from app.db import engine


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mapping_nodes(value: Any) -> Iterable[dict[str, Any]]:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _utc_datetime(value: Any) -> datetime | None:
    """Parse DB/JSON timestamps without allowing naive local-time semantics."""

    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _backfill_agent_mission_cooldowns(conn: Connection) -> int:
    """Promote legacy candidate cooldown hints to durable scheduler columns.

    Older deployments used ``updated_at`` as the scheduler clock.  We use it
    only once during migration (or the explicit JSON ``retry_after`` when
    present), after which unrelated mission updates cannot extend the pause.
    Invalid/missing JSON is deliberately harmless.
    """

    rows = conn.execute(text("""
        SELECT id, progress, updated_at, blocked_at, retry_after
        FROM agent_missions
        WHERE kind = 'candidate_discovery'
          AND status = 'paused'
          AND (blocked_at IS NULL OR retry_after IS NULL)
    """)).mappings().all()
    updated = 0
    for row in rows:
        progress = _json_object(row.get("progress"))
        existing_retry = _utc_datetime(row.get("retry_after"))
        progress_retry = _utc_datetime(progress.get("retry_after"))
        existing_blocked = _utc_datetime(row.get("blocked_at"))
        progress_blocked = _utc_datetime(progress.get("blocked_at"))
        legacy_updated = _utc_datetime(row.get("updated_at"))
        retry_hint = existing_retry or progress_retry

        blocked_at = (
            existing_blocked
            or progress_blocked
            or (retry_hint - timedelta(hours=12) if retry_hint is not None else None)
            or legacy_updated
        )
        if blocked_at is None:
            # ``updated_at`` is non-null in the model, but fail closed if a
            # hand-built legacy table contains an invalid value.
            continue
        retry_after = retry_hint or (blocked_at + timedelta(hours=12))
        conn.execute(
            text("""
                UPDATE agent_missions
                SET blocked_at = :blocked_at,
                    retry_after = :retry_after
                WHERE id = :mission_id
            """),
            {
                "blocked_at": blocked_at,
                "retry_after": retry_after,
                "mission_id": row["id"],
            },
        )
        updated += 1
    return updated


_PLACE_EVENT_CITY_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION cloudmiddle_fill_place_event_city_id()
RETURNS trigger AS $$
BEGIN
  IF NEW.city_id IS NULL AND NEW.place_id IS NOT NULL THEN
    SELECT city_id INTO NEW.city_id
    FROM markers
    WHERE id = NEW.place_id;
  END IF;

  IF NEW.city_id IS NULL THEN
    RAISE EXCEPTION
      'place_events.city_id is required for every new event; pass city_id when no live place exists'
      USING ERRCODE = '23502', TABLE = 'place_events', COLUMN = 'city_id';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_PLACE_EVENT_CITY_TRIGGER_SQL = """
CREATE TRIGGER trg_place_events_fill_city_id
BEFORE INSERT ON place_events
FOR EACH ROW
EXECUTE FUNCTION cloudmiddle_fill_place_event_city_id()
"""


def _install_postgres_place_event_city_trigger(conn: Connection) -> None:
    """Bridge rolling deployments, then fail closed for unattributed events.

    This is intentionally INSERT-only. Existing legacy NULL rows stay available
    for quarantine/manual repair, while old application tasks that still omit
    ``city_id`` can be attributed from their live ``place_id``.
    """

    conn.execute(text(_PLACE_EVENT_CITY_TRIGGER_FUNCTION_SQL))
    conn.execute(
        text(
            "DROP TRIGGER IF EXISTS trg_place_events_fill_city_id ON place_events"
        )
    )
    conn.execute(text(_PLACE_EVENT_CITY_TRIGGER_SQL))


def _backfill_place_event_city_ids(
    conn: Connection,
    *,
    tables: set[str],
) -> None:
    """Recover durable city ownership for legacy place events.

    Most rows still point at a marker and are handled in SQL.  Deleted-place
    rows require audit-data recovery: explicit payload city, a referenced live
    marker/event, or coordinates that fall inside exactly one city viewbox.
    Titles and users are deliberately excluded because chain names recur across
    cities. Ambiguous rows stay NULL as a quarantine signal.
    """

    if "place_events" not in tables or "cities" not in tables:
        return

    if "markers" in tables:
        conn.execute(
            text(
                """
                UPDATE place_events
                SET city_id = (
                  SELECT m.city_id FROM markers m WHERE m.id = place_events.place_id
                )
                WHERE city_id IS NULL
                  AND place_id IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM markers m WHERE m.id = place_events.place_id
                  )
                """
            )
        )

    if conn.execute(
        text("SELECT 1 FROM place_events WHERE city_id IS NULL LIMIT 1")
    ).first() is None:
        return

    city_rows = conn.execute(
        text("SELECT id, search_viewbox FROM cities")
    ).mappings().all()
    valid_city_ids = {int(row["id"]) for row in city_rows}
    city_bounds: list[tuple[int, float, float, float, float]] = []
    for row in city_rows:
        try:
            parts = [float(item.strip()) for item in str(row["search_viewbox"] or "").split(",")]
            if len(parts) != 4:
                continue
            west, first_lat, east, second_lat = parts
            city_bounds.append(
                (
                    int(row["id"]),
                    min(west, east),
                    max(west, east),
                    min(first_lat, second_lat),
                    max(first_lat, second_lat),
                )
            )
        except (TypeError, ValueError):
            continue

    marker_cities: dict[int, int] = {}
    if "markers" in tables:
        marker_cities = {
            int(row["id"]): int(row["city_id"])
            for row in conn.execute(
                text("SELECT id, city_id FROM markers WHERE city_id IS NOT NULL")
            ).mappings()
        }

    rows = list(
        conn.execute(
            text(
                """
                SELECT id, city_id, place_id, user_id, summary, payload, created_at
                FROM place_events
                ORDER BY created_at ASC, id ASC
                """
            )
        ).mappings()
    )
    event_cities = {
        int(row["id"]): int(row["city_id"])
        for row in rows
        if row["city_id"] is not None
    }
    parsed_payloads = {int(row["id"]): _json_object(row["payload"]) for row in rows}

    def coordinate_city(payload: dict[str, Any]) -> int | None:
        matches: set[int] = set()
        for node in _mapping_nodes(payload):
            if "lat" not in node or "lng" not in node:
                continue
            try:
                lat = float(node["lat"])
                lng = float(node["lng"])
            except (TypeError, ValueError):
                continue
            for city_id, west, east, south, north in city_bounds:
                if west <= lng <= east and south <= lat <= north:
                    matches.add(city_id)
        return next(iter(matches)) if len(matches) == 1 else None

    def infer_city(row: Any) -> int | None:
        payload = parsed_payloads[int(row["id"])]
        for node in _mapping_nodes(payload):
            city_id = _positive_int(node.get("city_id"))
            if city_id in valid_city_ids:
                return city_id

        place_id = _positive_int(row["place_id"])
        if place_id in marker_cities:
            return marker_cities[place_id]
        for node in _mapping_nodes(payload):
            for key in ("place_id", "source_id", "target_id"):
                marker_id = _positive_int(node.get(key))
                if marker_id in marker_cities:
                    return marker_cities[marker_id]

        for node in _mapping_nodes(payload):
            referenced_event_id = _positive_int(node.get("rolled_back_event_id"))
            if referenced_event_id in event_cities:
                return event_cities[referenced_event_id]

        from_coordinates = coordinate_city(payload)
        if from_coordinates is not None:
            return from_coordinates
        return None

    unresolved = [row for row in rows if row["city_id"] is None]
    # References can form short chains (rollback -> merge -> marker), so repeat
    # until a pass cannot attribute anything else.
    while unresolved:
        changed = False
        still_unresolved = []
        for row in unresolved:
            city_id = infer_city(row)
            if city_id is None:
                still_unresolved.append(row)
                continue
            event_id = int(row["id"])
            conn.execute(
                text("UPDATE place_events SET city_id = :city_id WHERE id = :event_id"),
                {"city_id": city_id, "event_id": event_id},
            )
            event_cities[event_id] = city_id
            changed = True
        if not changed:
            break
        unresolved = still_unresolved


def ensure_schema() -> None:
    """기존 DB에 컬럼/테이블을 점진 추가."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    with engine.begin() as conn:
        transaction_inspector = inspect(conn)
        # This repository uses an idempotent bootstrap migration instead of
        # Alembic. Normal API/agent startup calls Base.metadata.create_all
        # before ensure_schema, while this explicit create also covers an
        # existing deployment that runs ensure_schema directly.
        if (
            "agent_quality_gap_dispositions" not in tables
            and {"cities", "markers"}.issubset(tables)
        ):
            from app.models import AgentQualityGapDisposition

            AgentQualityGapDisposition.__table__.create(bind=conn, checkfirst=True)
            tables.add("agent_quality_gap_dispositions")

        if "cities" in tables:
            city_cols = {c["name"] for c in transaction_inspector.get_columns("cities")}
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
            cols = {c["name"] for c in transaction_inspector.get_columns("markers")}
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
            knowledge_cols = {
                c["name"] for c in transaction_inspector.get_columns("agent_knowledge")
            }
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
                "keywords": "TEXT DEFAULT '[]' NOT NULL",
                "applicability": "TEXT DEFAULT '{}' NOT NULL",
                "source_refs": "TEXT DEFAULT '[]' NOT NULL",
                "evidence_count": "INTEGER DEFAULT 0 NOT NULL",
                "quality_score": "FLOAT DEFAULT 0.7 NOT NULL",
                "retrieval_count": "INTEGER DEFAULT 0 NOT NULL",
                "last_retrieved_at": (
                    "TIMESTAMP WITH TIME ZONE"
                    if engine.dialect.name == "postgresql"
                    else "TIMESTAMP"
                ),
                "status": "VARCHAR(20) DEFAULT 'active' NOT NULL",
                "version": "INTEGER DEFAULT 1 NOT NULL",
            }
            for column, ddl in knowledge_additions.items():
                if column not in knowledge_cols:
                    conn.execute(text(f"ALTER TABLE agent_knowledge ADD COLUMN {column} {ddl}"))

        if "agent_search_logs" in tables:
            search_cols = {
                c["name"] for c in transaction_inspector.get_columns("agent_search_logs")
            }
            if "city_id" not in search_cols:
                conn.execute(text("ALTER TABLE agent_search_logs ADD COLUMN city_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_search_logs_city_id ON agent_search_logs (city_id)"))

        if "agent_search_results" in tables:
            result_cols = {
                c["name"] for c in transaction_inspector.get_columns("agent_search_results")
            }
            if "city_id" not in result_cols:
                conn.execute(text("ALTER TABLE agent_search_results ADD COLUMN city_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_search_results_city_id ON agent_search_results (city_id)"))

        if "agent_web_visits" in tables:
            visit_cols = {
                c["name"] for c in transaction_inspector.get_columns("agent_web_visits")
            }
            if "city_id" not in visit_cols:
                conn.execute(text("ALTER TABLE agent_web_visits ADD COLUMN city_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_web_visits_city_id ON agent_web_visits (city_id)"))

        if "agent_runs" in tables:
            run_cols = {c["name"] for c in transaction_inspector.get_columns("agent_runs")}
            if "mission_id" not in run_cols:
                conn.execute(text("ALTER TABLE agent_runs ADD COLUMN mission_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_runs_mission_id ON agent_runs (mission_id)"))
            if "work_item_id" not in run_cols:
                conn.execute(text("ALTER TABLE agent_runs ADD COLUMN work_item_id INTEGER"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_runs_work_item_id ON agent_runs (work_item_id)"))

        if "agent_missions" in tables:
            mission_cols = {
                c["name"] for c in transaction_inspector.get_columns("agent_missions")
            }
            timestamp_type = (
                "TIMESTAMP WITH TIME ZONE"
                if engine.dialect.name == "postgresql"
                else "TIMESTAMP"
            )
            if "blocked_at" not in mission_cols:
                conn.execute(text(f"ALTER TABLE agent_missions ADD COLUMN blocked_at {timestamp_type}"))
            if "retry_after" not in mission_cols:
                conn.execute(text(f"ALTER TABLE agent_missions ADD COLUMN retry_after {timestamp_type}"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_agent_missions_blocked_at "
                "ON agent_missions (blocked_at)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_agent_missions_retry_after "
                "ON agent_missions (retry_after)"
            ))
            _backfill_agent_mission_cooldowns(conn)

        if "travel_chat_messages" in tables:
            chat_cols = {
                c["name"] for c in transaction_inspector.get_columns("travel_chat_messages")
            }
            if "candidates" not in chat_cols:
                conn.execute(
                    text("ALTER TABLE travel_chat_messages ADD COLUMN candidates TEXT DEFAULT '[]' NOT NULL")
                )
            if "tool_trace" not in chat_cols:
                conn.execute(
                    text("ALTER TABLE travel_chat_messages ADD COLUMN tool_trace TEXT DEFAULT '[]' NOT NULL")
                )

        if "travel_chat_work" in tables and engine.dialect.name == "postgresql":
            # Keep the newest ledger if an older deployment admitted concurrent
            # active rows, then enforce one resumable task per user/city.
            conn.execute(text("""
                WITH ranked AS (
                  SELECT id,
                         ROW_NUMBER() OVER (
                           PARTITION BY user_id, city_id ORDER BY id DESC
                         ) AS position
                  FROM travel_chat_work
                  WHERE status = 'active'
                )
                UPDATE travel_chat_work AS work
                SET status = 'superseded'
                FROM ranked
                WHERE work.id = ranked.id AND ranked.position > 1
            """))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_travel_chat_work_active_user_city
                ON travel_chat_work (user_id, city_id)
                WHERE status = 'active'
            """))

        # 기여자 백필: 기존 markers.user_id → place_contributors
        # Reuse the migration transaction. A second Engine-level inspection can
        # roll back the same SQLite in-memory connection and cannot observe
        # uncommitted PostgreSQL DDL reliably during rolling upgrades.
        tables_now = set(transaction_inspector.get_table_names())

        if "place_events" in tables_now and "cities" in tables_now:
            event_cols = {
                c["name"] for c in transaction_inspector.get_columns("place_events")
            }
            if "city_id" not in event_cols:
                conn.execute(
                    text(
                        "ALTER TABLE place_events ADD COLUMN city_id INTEGER "
                        "REFERENCES cities(id) ON DELETE RESTRICT"
                    )
                )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_place_events_city_id "
                    "ON place_events (city_id)"
                )
            )
            _backfill_place_event_city_ids(conn, tables=tables_now)
            if engine.dialect.name == "postgresql":
                _install_postgres_place_event_city_trigger(conn)

        # Legacy personal DAY/slot rows become entries in one city-shared,
        # publishable itinerary document. Existing day/slot values are retained
        # as migration hints until a collaborator chooses a real date/time.
        if "travel_plan_items" in tables_now and "travel_plans" in tables_now:
            plan_item_cols = {
                c["name"] for c in transaction_inspector.get_columns("travel_plan_items")
            }
            if "plan_id" not in plan_item_cols:
                conn.execute(
                    text(
                        "ALTER TABLE travel_plan_items ADD COLUMN plan_id INTEGER "
                        "REFERENCES travel_plans(id) ON DELETE CASCADE"
                    )
                )

            # Keep denormalized post/listing date bounds derived from the real
            # date rows, including after an older deployment removed a day.
            conn.execute(
                text(
                    """
                    UPDATE travel_plans
                    SET start_date = (
                          SELECT MIN(d.calendar_date) FROM travel_plan_days d
                          WHERE d.plan_id = travel_plans.id
                        ),
                        end_date = (
                          SELECT MAX(d.calendar_date) FROM travel_plan_days d
                          WHERE d.plan_id = travel_plans.id
                        )
                    """
                )
            )
            if "plan_day_id" not in plan_item_cols:
                conn.execute(
                    text(
                        "ALTER TABLE travel_plan_items ADD COLUMN plan_day_id INTEGER "
                        "REFERENCES travel_plan_days(id) ON DELETE SET NULL"
                    )
                )
            if "start_time" not in plan_item_cols:
                conn.execute(text("ALTER TABLE travel_plan_items ADD COLUMN start_time TIME"))
            if "end_time" not in plan_item_cols:
                conn.execute(text("ALTER TABLE travel_plan_items ADD COLUMN end_time TIME"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_travel_plan_items_plan_id ON travel_plan_items (plan_id)"))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_travel_plan_items_plan_day_id ON travel_plan_items (plan_day_id)")
            )

            # The old constraint represented a private list. It both permitted
            # cross-user duplicates and prevented valid repeat visits.
            if engine.dialect.name == "postgresql":
                conn.execute(text("ALTER TABLE travel_plan_items DROP CONSTRAINT IF EXISTS uq_plan_user_city_place"))

            conn.execute(
                text(
                    """
                    INSERT INTO travel_plans
                      (city_id, owner_user_id, title, description, visibility, status,
                       timezone, cover_image_url, published_at)
                    SELECT
                      c.id,
                      COALESCE(
                        (SELECT MIN(t.user_id) FROM travel_plan_items t WHERE t.city_id = c.id),
                        (SELECT MIN(u.id) FROM users u)
                      ),
                      c.name_ko || ' 함께 만드는 여행',
                      '이 도시를 여행하는 사용자들이 함께 편집하는 공용 일정표입니다.',
                      'city_shared',
                      'published',
                      'Asia/Shanghai',
                      '',
                      CURRENT_TIMESTAMP
                    FROM cities c
                    WHERE c.status = 'active'
                      AND NOT EXISTS (
                        SELECT 1 FROM travel_plans p
                        WHERE p.city_id = c.id
                          AND p.visibility = 'city_shared'
                          AND p.status != 'archived'
                      )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE travel_plan_items
                    SET plan_id = (
                      SELECT p.id FROM travel_plans p
                      WHERE p.city_id = travel_plan_items.city_id
                        AND p.visibility = 'city_shared'
                        AND p.status != 'archived'
                      ORDER BY p.id ASC
                      LIMIT 1
                    )
                    WHERE plan_id IS NULL
                    """
                )
            )

            if "travel_plan_members" in tables_now:
                member_insert = "INSERT OR IGNORE" if engine.dialect.name == "sqlite" else "INSERT"
                member_conflict = "" if engine.dialect.name == "sqlite" else " ON CONFLICT (plan_id, user_id) DO NOTHING"
                conn.execute(
                    text(
                        f"""
                        {member_insert} INTO travel_plan_members
                          (plan_id, user_id, role, invitation_status, invited_by_user_id)
                        SELECT p.id, p.owner_user_id, 'owner', 'accepted', p.owner_user_id
                        FROM travel_plans p
                        WHERE p.owner_user_id IS NOT NULL
                        {member_conflict}
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        {member_insert} INTO travel_plan_members
                          (plan_id, user_id, role, invitation_status, invited_by_user_id)
                        SELECT DISTINCT t.plan_id, t.user_id, 'editor', 'accepted', p.owner_user_id
                        FROM travel_plan_items t
                        JOIN travel_plans p ON p.id = t.plan_id
                        WHERE t.plan_id IS NOT NULL AND t.user_id != COALESCE(p.owner_user_id, -1)
                        {member_conflict}
                        """
                    )
                )

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
                    INSERT INTO place_events (city_id, place_id, user_id, actor, action, summary, payload, groq_read_at, created_at)
                    SELECT
                      m.city_id,
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
