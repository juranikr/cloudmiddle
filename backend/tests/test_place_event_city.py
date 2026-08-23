from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.events import log_place_event, mark_events_read
from app.main import create_marker, delete_marker
from app.migrate import _install_postgres_place_event_city_trigger, ensure_schema
from app.models import (
    City,
    Marker,
    MarkerCategory,
    MarkerShape,
    PlaceEvent,
    PlaceEventAction,
    User,
)
from app.rollback import rollback_event
from app.schemas import MarkerCreate


def _city(city_id: int, *, slug: str, lat: float, lng: float, viewbox: str) -> City:
    return City(
        id=city_id,
        slug=slug,
        name_ko=slug,
        name_local=slug,
        center_lat=lat,
        center_lng=lng,
        search_viewbox=viewbox,
        search_context=slug,
    )


class PlaceEventCityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all(
            [
                _city(
                    1,
                    slug="jinan",
                    lat=36.65,
                    lng=117.12,
                    viewbox="116.70,36.95,117.55,36.35",
                ),
                _city(
                    2,
                    slug="shenyang",
                    lat=41.80,
                    lng=123.43,
                    viewbox="122.85,42.15,123.85,41.45",
                ),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _marker(self, city_id: int, title: str) -> Marker:
        marker = Marker(
            city_id=city_id,
            category=MarkerCategory.tourist,
            shape=MarkerShape.point,
            title=title,
            description="",
            lat=41.8 if city_id == 2 else 36.65,
            lng=123.43 if city_id == 2 else 117.12,
        )
        self.db.add(marker)
        self.db.flush()
        return marker

    def test_log_event_keeps_city_after_marker_is_deleted(self) -> None:
        marker = self._marker(2, "temporary hotel")
        event = log_place_event(
            self.db,
            place_id=marker.id,
            user=None,
            action=PlaceEventAction.delete,
            summary="장소 삭제: temporary hotel",
            payload={"title": marker.title},
        )
        self.db.flush()

        self.assertEqual(event.city_id, 2)
        payload = json.loads(event.payload)
        self.assertEqual(payload["city_id"], 2)
        self.assertEqual(payload["place_id"], marker.id)

        self.db.delete(marker)
        self.db.commit()
        self.db.refresh(event)

        self.assertIsNone(event.place_id)
        self.assertEqual(event.city_id, 2)

    def test_city_scoped_mark_does_not_acknowledge_another_city(self) -> None:
        city_one = PlaceEvent(
            city_id=1,
            actor="user",
            action=PlaceEventAction.update,
            summary="city one",
        )
        city_two = PlaceEvent(
            city_id=2,
            actor="user",
            action=PlaceEventAction.update,
            summary="city two",
        )
        legacy_marker = self._marker(1, "legacy")
        legacy = PlaceEvent(
            city_id=None,
            place_id=legacy_marker.id,
            actor="user",
            action=PlaceEventAction.update,
            summary="legacy city one",
        )
        self.db.add_all([city_one, city_two, legacy])
        self.db.commit()

        count = mark_events_read(
            self.db,
            [city_one.id, city_two.id, legacy.id],
            city_id=1,
        )
        self.db.commit()

        self.assertEqual(count, 2)
        self.assertIsNotNone(city_one.groq_read_at)
        self.assertIsNone(city_two.groq_read_at)
        self.assertIsNotNone(legacy.groq_read_at)

    def test_agent_create_rollback_preserves_city_after_deleting_marker(self) -> None:
        admin = User(
            email="admin@example.com",
            display_name="admin",
            password_hash="unused-in-unit-test",
        )
        self.db.add(admin)
        marker = self._marker(2, "agent suggestion")
        event = log_place_event(
            self.db,
            place_id=marker.id,
            user=None,
            action=PlaceEventAction.agent_create,
            summary="에이전트 장소 추가: agent suggestion",
            payload={"place_id": marker.id, "before": {}},
            actor="agent",
        )
        self.db.commit()

        rollback = rollback_event(
            self.db,
            event_id=event.id,
            admin=admin,
            note="wrong location",
        )

        self.assertIsNone(rollback.place_id)
        self.assertEqual(rollback.city_id, 2)
        self.assertEqual(json.loads(rollback.payload)["city_id"], 2)
        self.assertIsNone(self.db.get(Marker, marker.id))

    def test_user_create_and_delete_payloads_share_durable_marker_identity(self) -> None:
        user = User(
            email="traveler@example.com",
            display_name="traveler",
            password_hash="unused-in-unit-test",
        )
        self.db.add(user)
        self.db.commit()

        created = create_marker(
            MarkerCreate(
                city_id=2,
                category=MarkerCategory.lodging,
                title="瀋陽中街故宮漫心酒店",
                description="hotel",
                lat=41.8025,
                lng=123.4280556,
            ),
            self.db,
            user,
        )
        create_event = (
            self.db.query(PlaceEvent)
            .filter(PlaceEvent.action == PlaceEventAction.create)
            .one()
        )
        create_payload = json.loads(create_event.payload)

        delete_marker(created.id, self.db, user)
        delete_event = (
            self.db.query(PlaceEvent)
            .filter(PlaceEvent.action == PlaceEventAction.delete)
            .one()
        )
        delete_payload = json.loads(delete_event.payload)

        self.assertEqual(create_payload["place_id"], created.id)
        self.assertEqual(delete_payload["place_id"], created.id)
        self.assertEqual(create_payload["after"]["title"], "瀋陽中街故宮漫心酒店")
        self.assertEqual(delete_payload["before"]["title"], "瀋陽中街故宮漫心酒店")
        self.assertEqual(delete_payload["before"]["city_id"], 2)
        self.assertIsNone(delete_event.place_id)


class PlaceEventCityMigrationTests(unittest.TestCase):
    def test_migration_uses_only_durable_evidence_and_quarantines_ambiguity(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{tmp}/legacy.db")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        CREATE TABLE cities (
                          id INTEGER PRIMARY KEY,
                          slug VARCHAR(50) UNIQUE NOT NULL,
                          name_ko VARCHAR(100) NOT NULL,
                          name_local VARCHAR(100) NOT NULL,
                          country_code VARCHAR(2) DEFAULT 'CN' NOT NULL,
                          center_lat FLOAT NOT NULL,
                          center_lng FLOAT NOT NULL,
                          default_zoom INTEGER DEFAULT 12 NOT NULL,
                          search_viewbox VARCHAR(100) DEFAULT '' NOT NULL,
                          search_context VARCHAR(200) DEFAULT '' NOT NULL,
                          status VARCHAR(20) DEFAULT 'active' NOT NULL,
                          sort_order INTEGER DEFAULT 0 NOT NULL,
                          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE TABLE markers (
                          id INTEGER PRIMARY KEY,
                          user_id INTEGER,
                          city_id INTEGER NOT NULL,
                          category VARCHAR(30) NOT NULL,
                          title VARCHAR(200) NOT NULL,
                          description TEXT DEFAULT '' NOT NULL,
                          lat FLOAT NOT NULL,
                          lng FLOAT NOT NULL,
                          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE TABLE place_events (
                          id INTEGER PRIMARY KEY,
                          place_id INTEGER,
                          user_id INTEGER,
                          actor VARCHAR(20) NOT NULL,
                          action VARCHAR(30) NOT NULL,
                          summary VARCHAR(500) DEFAULT '' NOT NULL,
                          payload TEXT DEFAULT '{}' NOT NULL,
                          groq_read_at TIMESTAMP,
                          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO cities
                          (id, slug, name_ko, name_local, center_lat, center_lng,
                           search_viewbox, search_context, sort_order)
                        VALUES
                          (1, 'jinan', '지난', '济南', 36.6512, 117.1201,
                           '116.70,36.95,117.55,36.35', '济南市 山东省 中国', 10),
                          (2, 'shenyang', '선양', '沈阳', 41.8057, 123.4315,
                           '122.85,42.15,123.85,41.45', '沈阳市 辽宁省 中国', 20),
                          (3, 'overlap-test', '중첩 테스트', 'Overlap', 36.66, 117.01,
                           '116.90,36.80,117.20,36.50', 'test overlap', 30)
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO markers
                          (id, user_id, city_id, category, title, description, lat, lng)
                        VALUES
                          (10, NULL, 1, 'tourist', '趵突泉', '', 36.66, 117.01),
                          (20, NULL, 2, 'drink', 'HEYTEA', '', 41.80, 123.43)
                        """
                    )
                )
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    text(
                        """
                        INSERT INTO place_events
                          (id, place_id, user_id, actor, action, summary, payload, created_at)
                        VALUES
                          (360, NULL, 7, 'user', 'create',
                           '장소 추가: Shenyang Middle Street Palace Museum Manxin Hotel',
                           :create_payload, :created),
                          (361, NULL, 7, 'user', 'delete',
                           '장소 삭제: Shenyang Middle Street Palace Museum Manxin Hotel',
                           :delete_payload, :created),
                          (433, NULL, NULL, 'agent', 'merge', 'legacy merge',
                           :merge_payload, :created),
                          (439, NULL, 7, 'user', 'rollback', 'legacy rollback',
                           :rollback_payload, :created),
                          (500, 10, 7, 'user', 'create', '장소 추가: Starbucks',
                           :starbucks_payload, :created),
                          (501, 20, 7, 'user', 'create', '장소 추가: Starbucks',
                           :starbucks_payload, :created),
                          (502, NULL, 7, 'user', 'delete', '장소 삭제: Starbucks',
                           :starbucks_payload, :created),
                          (503, 10, 7, 'user', 'create', '장소 추가: HEYTEA',
                           :heytea_payload, :created),
                          (504, 20, 7, 'user', 'create', '장소 추가: HEYTEA',
                           :heytea_payload, :created),
                          (505, NULL, 7, 'user', 'delete', '장소 삭제: HEYTEA',
                           :heytea_payload, :created),
                          (506, NULL, 7, 'user', 'create', 'ambiguous coordinate',
                           :ambiguous_coordinate_payload, :created),
                          (507, NULL, 7, 'user', 'update', 'explicit city',
                           :explicit_city_payload, :created)
                        """
                    ),
                    {
                        "created": now,
                        "create_payload": json.dumps(
                            {
                                "category": "lodging",
                                "lat": 41.8025,
                                "lng": 123.4280556,
                            }
                        ),
                        "delete_payload": json.dumps(
                            {
                                "title": "Shenyang Middle Street Palace Museum Manxin Hotel"
                            }
                        ),
                        "merge_payload": json.dumps({"target_id": 10}),
                        "rollback_payload": json.dumps(
                            {"rolled_back_event_id": 433}
                        ),
                        "starbucks_payload": json.dumps({"title": "Starbucks"}),
                        "heytea_payload": json.dumps({"title": "HEYTEA"}),
                        "ambiguous_coordinate_payload": json.dumps(
                            {"lat": 36.66, "lng": 117.01}
                        ),
                        "explicit_city_payload": json.dumps({"city_id": 2}),
                    },
                )

            with patch("app.migrate.engine", engine):
                ensure_schema()
                ensure_schema()

            self.assertIn(
                "city_id",
                {column["name"] for column in inspect(engine).get_columns("place_events")},
            )
            with engine.connect() as conn:
                rows = dict(
                    conn.execute(
                        text(
                            "SELECT id, city_id FROM place_events "
                            "WHERE id IN (360, 361, 433, 439, 500, 501, 502, "
                            "503, 504, 505, 506, 507) ORDER BY id"
                        )
                    ).all()
                )
            self.assertEqual(
                rows,
                {
                    360: 2,
                    361: None,
                    433: 1,
                    439: 1,
                    500: 1,
                    501: 2,
                    502: None,
                    503: 1,
                    504: 2,
                    505: None,
                    506: None,
                    507: 2,
                },
            )
            engine.dispose()

    def test_postgres_trigger_fills_live_place_then_rejects_unresolved_insert(self) -> None:
        class RecordingConnection:
            def __init__(self) -> None:
                self.statements: list[str] = []

            def execute(self, statement):
                self.statements.append(str(statement))

        connection = RecordingConnection()
        _install_postgres_place_event_city_trigger(connection)  # type: ignore[arg-type]
        ddl = "\n".join(connection.statements)

        self.assertIn("BEFORE INSERT ON place_events", ddl)
        self.assertIn("NEW.place_id IS NOT NULL", ddl)
        self.assertIn("SELECT city_id INTO NEW.city_id", ddl)
        self.assertIn("IF NEW.city_id IS NULL THEN", ddl)
        self.assertIn("ERRCODE = '23502'", ddl)
        self.assertNotIn("BEFORE UPDATE", ddl)


if __name__ == "__main__":
    unittest.main()
