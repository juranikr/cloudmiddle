from __future__ import annotations

import unittest
from datetime import date, time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import (
    _get_city_shared_plan,
    _load_travel_plan,
    _travel_plan_to_out,
    create_travel_plan_day,
    create_travel_plan_item,
    delete_travel_plan_day,
    update_plan_item,
)
from app.models import City, Marker, MarkerCategory, TravelPlanItem, TravelPlanMember, User
from app.schemas import TravelPlanDayCreate, TravelPlanItemCreate, TravelPlanItemUpdate


class SharedTravelPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.owner = User(id=1, email="owner@example.com", display_name="첫 여행자", password_hash="test")
        self.editor = User(id=2, email="editor@example.com", display_name="함께 여행자", password_hash="test")
        self.city = City(
            id=1,
            slug="shenyang",
            name_ko="선양",
            name_local="沈阳",
            center_lat=41.80,
            center_lng=123.43,
        )
        self.place = Marker(
            id=1,
            user_id=1,
            city_id=1,
            category=MarkerCategory.restaurant,
            title="함께 갈 식당",
            lat=41.801,
            lng=123.431,
        )
        self.db.add_all([self.owner, self.editor, self.city, self.place])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_all_users_edit_the_same_city_plan_with_free_date_and_time(self) -> None:
        owner_plan = _get_city_shared_plan(self.db, self.city.id, self.owner)
        editor_plan = _get_city_shared_plan(self.db, self.city.id, self.editor)

        self.assertEqual(owner_plan.id, editor_plan.id)
        roles = {
            row.user_id: row.role
            for row in self.db.query(TravelPlanMember).filter(TravelPlanMember.plan_id == owner_plan.id).all()
        }
        self.assertEqual(roles, {self.owner.id: "owner", self.editor.id: "editor"})

        plan_out = create_travel_plan_day(
            plan_id=owner_plan.id,
            body=TravelPlanDayCreate(calendar_date=date(2026, 8, 15), title="시장과 야경"),
            db=self.db,
            current_user=self.editor,
        )
        day_id = plan_out.days[0].id
        item = create_travel_plan_item(
            plan_id=owner_plan.id,
            body=TravelPlanItemCreate(
                place_id=self.place.id,
                plan_day_id=day_id,
                start_time=time(10, 20),
                end_time=time(11, 45),
                note="시간을 고정 슬롯 없이 기록",
            ),
            db=self.db,
            current_user=self.editor,
        )

        viewed_by_owner = _travel_plan_to_out(
            self.db,
            _load_travel_plan(self.db, owner_plan.id),  # type: ignore[arg-type]
            self.owner,
        )
        self.assertEqual(viewed_by_owner.days[0].calendar_date, date(2026, 8, 15))
        self.assertEqual(viewed_by_owner.days[0].items[0].id, item.id)
        self.assertEqual(viewed_by_owner.days[0].items[0].creator_name, "함께 여행자")
        self.assertEqual(viewed_by_owner.days[0].items[0].start_time, time(10, 20))

        updated = update_plan_item(
            item_id=item.id,
            body=TravelPlanItemUpdate(start_time=time(12, 5), end_time=time(13, 0)),
            db=self.db,
            current_user=self.owner,
        )
        self.assertEqual(updated.start_time, time(12, 5))

    def test_deleting_a_date_keeps_its_places_unscheduled(self) -> None:
        plan = _get_city_shared_plan(self.db, self.city.id, self.owner)
        plan_out = create_travel_plan_day(
            plan_id=plan.id,
            body=TravelPlanDayCreate(calendar_date=date(2026, 8, 16)),
            db=self.db,
            current_user=self.owner,
        )
        item = create_travel_plan_item(
            plan_id=plan.id,
            body=TravelPlanItemCreate(place_id=self.place.id, plan_day_id=plan_out.days[0].id),
            db=self.db,
            current_user=self.owner,
        )

        result = delete_travel_plan_day(
            day_id=plan_out.days[0].id,
            db=self.db,
            current_user=self.editor,
        )

        self.assertEqual(result.days, [])
        self.assertEqual([row.id for row in result.unscheduled_items], [item.id])
        stored = self.db.get(TravelPlanItem, item.id)
        self.assertIsNone(stored.plan_day_id if stored else None)


if __name__ == "__main__":
    unittest.main()
