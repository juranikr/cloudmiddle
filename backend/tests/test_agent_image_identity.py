import unittest

from app.agent.tools import _image_candidate_matches_place
from app.models import Marker, MarkerCategory, MarkerShape


class AgentImageIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.marker = Marker(
            city_id=2,
            category=MarkerCategory.tourist,
            shape=MarkerShape.point,
            title="沈阳故宫 (선양 고궁)",
            description="선양 고궁 안내",
            lat=41.7966,
            lng=123.4498,
        )

    def test_exact_place_title_is_eligible(self) -> None:
        self.assertTrue(_image_candidate_matches_place(self.marker, {
            "title": "File:沈阳故宫大政殿.jpg",
            "page_url": "https://commons.wikimedia.org/wiki/File:沈阳故宫大政殿.jpg",
        }))

    def test_nearby_cityscape_is_not_a_place_candidate(self) -> None:
        self.assertFalse(_image_candidate_matches_place(self.marker, {
            "title": "File:Shenyang skyline at night.jpg",
            "page_url": "https://commons.wikimedia.org/wiki/File:Shenyang_skyline.jpg",
            "nearby": True,
        }))


if __name__ == "__main__":
    unittest.main()
