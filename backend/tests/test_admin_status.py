import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_api import admin_status
from app.db import Base
from app.models import City, PlaceEvent, PlaceEventAction


class AdminStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add(
            City(
                id=1,
                slug="jinan",
                name_ko="지난",
                name_local="济南",
                center_lat=36.65,
                center_lng=117.12,
                search_viewbox="116.7,36.95,117.55,36.35",
                search_context="济南市 山东省 中国",
            )
        )
        self.db.add_all(
            [
                PlaceEvent(
                    city_id=1,
                    actor="system",
                    action=PlaceEventAction.update,
                    summary="attributed unread",
                ),
                PlaceEvent(
                    city_id=None,
                    actor="system",
                    action=PlaceEventAction.update,
                    summary="quarantined unread",
                ),
                PlaceEvent(
                    city_id=None,
                    actor="system",
                    action=PlaceEventAction.update,
                    summary="already acknowledged legacy",
                    groq_read_at=datetime.now(timezone.utc),
                ),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_reports_only_unread_cityless_events_as_unattributed(self) -> None:
        status = admin_status(
            db=self.db,
            admin=SimpleNamespace(email="admin@example.com"),
        )

        self.assertEqual(status.events_total, 3)
        self.assertEqual(status.events_unread, 2)
        self.assertEqual(status.events_unattributed, 1)


if __name__ == "__main__":
    unittest.main()
