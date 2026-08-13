from __future__ import annotations

import unittest

from app.place_identity import (
    PlaceIdentityInput,
    canonical_place_identity,
    same_place_candidate,
)


class PlaceIdentityTests(unittest.TestCase):
    def test_same_chain_different_named_branches_stay_separate(self) -> None:
        dayuecheng = PlaceIdentityInput(
            city="沈阳",
            title="喜茶(沈阳大悦城店)",
            chain_name="喜茶",
            branch_name="沈阳大悦城店",
            address="辽宁省沈阳市大东区小东路6号",
            lat=41.8007,
            lng=123.4637,
        )
        zhongjie = PlaceIdentityInput(
            city="沈阳",
            title="喜茶(中街益田假日世界店)",
            chain_name="喜茶",
            branch_name="中街益田假日世界店",
            address="辽宁省沈阳市沈河区中街路268号",
            lat=41.8014,
            lng=123.4520,
        )

        decision = same_place_candidate(dayuecheng, zhongjie)

        self.assertFalse(decision.same)
        self.assertEqual(decision.reason, "branch_mismatch")
        self.assertNotEqual(
            canonical_place_identity(dayuecheng).canonical_key,
            canonical_place_identity(zhongjie).canonical_key,
        )

    def test_same_mall_different_sections_are_not_collapsed_by_proximity(self) -> None:
        hall_a = PlaceIdentityInput(
            city="沈阳",
            title="鸣记脆皮烤鱼(大悦城A馆店)",
            chain_name="鸣记脆皮烤鱼",
            branch_name="大悦城A馆店",
            address="小东路6号大悦城A馆",
            lat=41.80070,
            lng=123.46370,
        )
        hall_c = PlaceIdentityInput(
            city="沈阳",
            title="鸣记脆皮烤鱼(大悦城C馆店)",
            chain_name="鸣记脆皮烤鱼",
            branch_name="大悦城C馆店",
            address="小东路6号大悦城C馆",
            lat=41.80085,
            lng=123.46382,
        )

        decision = same_place_candidate(hall_a, hall_c)

        self.assertLess(decision.distance_m or 999, 80)
        self.assertFalse(decision.same)
        self.assertEqual(decision.reason, "branch_mismatch")

    def test_seo_title_and_clean_title_at_same_address_are_one_place(self) -> None:
        seo = PlaceIdentityInput(
            city="沈阳",
            title="必吃！鸣记脆皮烤鱼，香辣酸甜一网打尽",
            address="辽宁省沈阳市大东区小东路6号",
            lat=41.80070,
            lng=123.46370,
        )
        clean = PlaceIdentityInput(
            city="沈阳",
            title="鸣记脆皮烤鱼 (밍지 바삭 구이생선)",
            address="小东路6号 沈阳大悦城A馆",
            lat=41.80074,
            lng=123.46373,
        )

        decision = same_place_candidate(seo, clean)

        self.assertTrue(decision.same)
        self.assertEqual(decision.reason, "same_business_address")
        self.assertEqual(
            canonical_place_identity(seo).canonical_key,
            canonical_place_identity(clean).canonical_key,
        )

    def test_same_branch_tolerates_admin_prefix_and_small_coordinate_drift(self) -> None:
        first = PlaceIdentityInput(
            city="沈阳",
            title="茉酸奶(沈阳大悦城旗舰店)",
            chain_name="茉酸奶",
            branch_name="沈阳大悦城旗舰店",
            address="辽宁省沈阳市大东区小东路6号",
            lat=41.80072,
            lng=123.46365,
        )
        second = PlaceIdentityInput(
            city="沈阳",
            title="茉酸奶（大悦城店） (모어요거트 다웨청점)",
            chain_name="茉酸奶",
            branch_name="大悦城店",
            address="小东路6号",
            lat=41.80080,
            lng=123.46371,
        )

        decision = same_place_candidate(first, second)

        self.assertTrue(decision.same)
        self.assertEqual(decision.reason, "same_branch")

    def test_same_brand_without_branch_is_not_merged_across_city(self) -> None:
        first = PlaceIdentityInput(
            city="沈阳",
            title="喜茶",
            chain_name="喜茶",
            lat=41.8007,
            lng=123.4637,
        )
        far = PlaceIdentityInput(
            city="沈阳",
            title="喜茶 HEYTEA",
            chain_name="喜茶",
            lat=41.8607,
            lng=123.4637,
        )

        decision = same_place_candidate(first, far)

        self.assertFalse(decision.same)
        self.assertEqual(decision.reason, "coordinate_conflict")

    def test_same_name_and_coordinates_in_different_cities_are_separate(self) -> None:
        shenyang = PlaceIdentityInput("沈阳", "万象城", lat=41.8, lng=123.4)
        jinan = PlaceIdentityInput("济南", "万象城", lat=41.8, lng=123.4)

        decision = same_place_candidate(shenyang, jinan)

        self.assertFalse(decision.same)
        self.assertEqual(decision.reason, "city_mismatch")


if __name__ == "__main__":
    unittest.main()
