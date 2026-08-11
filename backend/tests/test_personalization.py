import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    City,
    Marker,
    MarkerCategory,
    PlaceAppeal,
    PlaceFavorite,
    TravelChatMessage,
    TravelPlanItem,
    User,
)
from app.personalization import build_user_travel_profile


class PersonalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all([
            City(id=1, slug="shenyang", name_ko="선양", name_local="沈阳", center_lat=41.80, center_lng=123.43),
            User(id=1, email="traveler@example.com", display_name="여행자", password_hash="test"),
        ])
        self.db.flush()
        hotel = Marker(id=1, user_id=1, city_id=1, category=MarkerCategory.lodging, title="선양 중제 호텔", lat=41.80, lng=123.45)
        more_yogurt = Marker(
            id=2,
            user_id=1,
            city_id=1,
            category=MarkerCategory.drink,
            travel_role="rest",
            title="茉酸奶大悦城店 (모어요거트 다웨청점)",
            lat=41.801,
            lng=123.452,
            coordinate_source="amap_share",
            coordinate_source_url="https://amap.com/example",
        )
        heytea = Marker(id=3, city_id=1, category=MarkerCategory.drink, travel_role="rest", title="喜茶中街店 (헤이티 중제점)", lat=41.802, lng=123.454)
        restaurant = Marker(id=4, city_id=1, category=MarkerCategory.restaurant, travel_role="food", title="老边饺子馆 (라오볜 자오쯔관)", lat=41.803, lng=123.455)
        far_place = Marker(id=5, city_id=1, category=MarkerCategory.tourist, travel_role="history", title="먼 박물관", lat=42.2, lng=124.0)
        self.db.add_all([hotel, more_yogurt, heytea, restaurant, far_place])
        self.db.flush()
        self.db.add_all([
            PlaceFavorite(user_id=1, place_id=1),
            TravelPlanItem(user_id=1, city_id=1, place_id=1, day=1, slot="afternoon"),
            PlaceAppeal(user_id=1, place_id=2, body="이 지점은 다른 지점이니 합치면 안 돼요"),
            TravelChatMessage(user_id=1, city_id=1, role="user", content="헤이티와 모어요거트 다른 지점도 추가해줘"),
            TravelChatMessage(user_id=1, city_id=1, role="user", content="헤이티와 모어요거트를 꼭 찾아줘"),
        ])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_profile_uses_repeated_brands_hotel_anchor_and_corrections(self) -> None:
        profile = build_user_travel_profile(self.db, user_id=1, city_id=1)

        self.assertGreater(profile["brand_scores"]["헤이티"], 0)
        self.assertGreater(profile["brand_scores"]["모어요거트"], 0)
        self.assertEqual(profile["anchors"][0]["place_id"], 1)
        self.assertIn("share_import", profile["direct_source_counts"])
        self.assertEqual(profile["corrections"][0]["place_id"], 2)
        by_id = {item["place_id"]: item for item in profile["recommendations"]}
        self.assertIn(3, by_id)
        self.assertIn("반복 요청한 헤이티", by_id[3]["reason"])
        self.assertIn(4, by_id)
        self.assertIn("거점", by_id[4]["reason"])
        self.assertNotIn(2, by_id)


if __name__ == "__main__":
    unittest.main()
