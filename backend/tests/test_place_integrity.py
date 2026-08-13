from __future__ import annotations

import unittest

from app.place_integrity import (
    assess_new_place,
    compare_china_addresses,
    compare_place_identity,
    normalize_china_address,
)


SHENYANG_VIEWBOX = "122.85,42.15,123.85,41.45"


class ChinaAddressIntegrityTests(unittest.TestCase):
    def test_equivalent_mall_address_variants_do_not_conflict(self) -> None:
        result = compare_china_addresses(
            "辽宁省沈阳市大东区小东路6号沈阳大悦城A馆4楼",
            "沈阳大悦城A馆4层，小东路6号",
        )

        self.assertTrue(result.ok, result.details)
        self.assertEqual(result.error, "")

    def test_different_explicit_roads_conflict_even_without_districts(self) -> None:
        result = compare_china_addresses(
            "沈阳市文官街(中地名都向西300米)",
            "辽宁省沈阳市北四东路44-1号御览茗居7号门",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "address_road_mismatch")

    def test_different_districts_conflict(self) -> None:
        result = compare_china_addresses(
            "沈河区文艺路32号",
            "铁西区文艺路32号",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "address_district_mismatch")

    def test_missing_address_parts_are_not_treated_as_conflicts(self) -> None:
        result = compare_china_addresses("小东路6号", "辽宁省沈阳市大东区小东路6号")

        self.assertTrue(result.ok, result.details)

    def test_traditional_address_suffixes_are_normalized(self) -> None:
        tokens = normalize_china_address("瀋陽市大東區小東路6號大悅城A館4樓")

        self.assertIn("大东区", tokens.districts)
        self.assertIn("小东路", tokens.roads)
        self.assertIn("6号", tokens.house_numbers)


class PlaceIdentityIntegrityTests(unittest.TestCase):
    def test_different_business_names_are_rejected(self) -> None:
        result = compare_place_identity(
            "麦德香 (맥데샹·문관거리점)",
            "麦香铁锅焖面(沈阳总店)",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "place_identity_mismatch")

    def test_same_brand_different_explicit_chinese_branch_is_rejected(self) -> None:
        result = compare_place_identity("喜茶(中街店)", "喜茶(大悦城店)")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "branch_identity_mismatch")

    def test_same_business_with_minor_suffix_difference_is_accepted(self) -> None:
        result = compare_place_identity("老边饺子馆", "老边饺子(中街店)")

        self.assertTrue(result.ok, result.details)

    def test_cross_script_branch_is_unresolved_not_rejected(self) -> None:
        result = compare_place_identity("喜茶(중제점)", "喜茶(中街店)")

        self.assertTrue(result.ok)
        self.assertIn("branch_identity_unresolved", result.warnings)


class NewPlaceAssessmentTests(unittest.TestCase):
    def test_ungrounded_agent_research_coordinate_is_blocked(self) -> None:
        result = assess_new_place(
            {
                "title": "普喜·养身调理 (푸시 양신조정)",
                "lat": 41.795,
                "lng": 123.438,
                "coordinate_source": "agent_research",
                "coordinate_source_url": "",
                "coordinate_external_id": "",
            },
            city_viewbox=SHENYANG_VIEWBOX,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "coordinate_evidence_required")

    def test_amap_share_url_is_accepted_but_identity_remains_unchecked(self) -> None:
        result = assess_new_place(
            {
                "title": "沈阳中街故宫漫心酒店 (선양 중제고궁 만신호텔)",
                "lat": 41.8011621,
                "lng": 123.4504561,
                "coordinate_source": "amap_share",
                "coordinate_source_url": "https://surl.amap.com/cFUgvfMS3ee",
            },
            city_viewbox=SHENYANG_VIEWBOX,
        )

        self.assertTrue(result.ok, result.details)
        self.assertIn("coordinate_identity_unchecked", result.warnings)

    def test_coordinate_outside_city_is_blocked(self) -> None:
        result = assess_new_place(
            {
                "title": "测试地点 (테스트 장소)",
                "lat": 40.0,
                "lng": 123.45,
                "coordinate_source": "nominatim",
                "coordinate_external_id": "node/1",
            },
            city_viewbox=SHENYANG_VIEWBOX,
        )

        self.assertFalse(result.ok)
        self.assertIn("coordinate_outside_city", result.details["errors"])

    def test_coordinate_must_match_server_evidence(self) -> None:
        result = assess_new_place(
            {
                "title": "鹿鸣春饭店 (루밍춘 식당)",
                "lat": 41.80,
                "lng": 123.46,
                "coordinate_source": "ctrip_embedded_gdcoord",
                "coordinate_source_url": "https://gs.ctrip.com/html5/you/foods/fooddetail/155/1.html",
            },
            city_viewbox=SHENYANG_VIEWBOX,
            coordinate_evidence={
                "title": "鹿鸣春饭店",
                "lat": 41.78658,
                "lng": 123.41080,
                "source_url": "https://gs.ctrip.com/html5/you/foods/fooddetail/155/1.html",
                "storage_allowed": True,
            },
        )

        self.assertFalse(result.ok)
        self.assertIn("coordinate_not_grounded", result.details["errors"])

    def test_bad_branch_address_is_rejected_against_coordinate_evidence(self) -> None:
        result = assess_new_place(
            {
                "title": "麦德香 (맥데샹·문관거리점)",
                "description": "주소: 辽宁省沈阳市北四东路44-1号御览茗居7号门",
                "lat": 41.8750759,
                "lng": 123.4827428,
                "coordinate_source": "qunar_embedded_poi",
                "coordinate_source_url": "https://touch.go.qunar.com/poi/6899842",
            },
            city_viewbox=SHENYANG_VIEWBOX,
            coordinate_evidence={
                "title": "麦德香(文官街店)",
                "address": "沈阳市文官街(中地名都向西300米)",
                "lat": 41.8750759,
                "lng": 123.4827428,
                "source_url": "https://touch.go.qunar.com/poi/6899842",
                "storage_allowed": True,
            },
        )

        self.assertFalse(result.ok)
        self.assertIn("address_road_mismatch", result.details["errors"])

    def test_existing_landmark_anchor_catches_joy_city_coordinate_error(self) -> None:
        result = assess_new_place(
            {
                "title": "鸣记脆皮烤鱼 (밍지 크리스피 구이 생선)",
                "description": "주소: 辽宁省沈阳市大东区小东路6号沈阳大悦城A馆4楼",
                "lat": 41.8120956,
                "lng": 123.3638935,
                "coordinate_source": "agent_research",
                "coordinate_source_url": "https://www.dianping.com/discovery/1578561502",
            },
            city_viewbox=SHENYANG_VIEWBOX,
            anchors=[
                {
                    "id": 81,
                    "title": "沈阳大悦城 (선양 다웨청)",
                    "lat": 41.8007032,
                    "lng": 123.463722,
                }
            ],
        )

        self.assertFalse(result.ok)
        self.assertIn("landmark_anchor_mismatch", result.details["errors"])
        self.assertGreater(result.details["anchor_checks"][0]["distance_m"], 8_000)

    def test_nearby_claimed_landmark_is_allowed(self) -> None:
        result = assess_new_place(
            {
                "title": "鸣记脆皮烤鱼 (밍지 크리스피 구이 생선)",
                "description": "주소: 辽宁省沈阳市大东区小东路6号沈阳大悦城A馆4楼",
                "lat": 41.8008,
                "lng": 123.4638,
                "coordinate_source": "360map_embedded_poi",
                "coordinate_external_id": "poi-1",
            },
            city_viewbox=SHENYANG_VIEWBOX,
            anchors=[
                {
                    "id": 81,
                    "title": "沈阳大悦城 (선양 다웨청)",
                    "lat": 41.8007032,
                    "lng": 123.463722,
                }
            ],
        )

        self.assertTrue(result.ok, result.details)


if __name__ == "__main__":
    unittest.main()
