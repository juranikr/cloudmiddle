from __future__ import annotations

import unittest
from unittest.mock import patch

from app.share_import import (
    _parse_amap_final,
    _parse_share_body_lines,
    _prefer_name,
    import_share_text,
)


class ShareImportParsingTests(unittest.TestCase):
    @patch(
        "app.share_import._follow_redirects",
        return_value=(
            "https://wb.amap.com/?p=B0K6YA3YSO%2C41.80354530323765%2C"
            "123.4565408527851%2C%2C"
        ),
    )
    def test_hotel_name_containing_street_is_not_treated_as_address(self, _redirect) -> None:
        result = import_share_text(
            "Shenyang Middle Street Palace Museum Manxin Hotel\n"
            "Beizhongjie Road No.118\n"
            "https://surl.amap.com/cFUgvfMS3ee",
            preferred_source="amap",
            city_name="Shenyang",
            city_context="Shenyang China",
        )

        self.assertEqual(result.title, "Shenyang Middle Street Palace Museum Manxin Hotel")
        self.assertEqual(result.address, "Beizhongjie Road No.118")
        self.assertEqual(result.category_hint, "lodging")
        self.assertFalse(result.needs_map_pick)

    def test_chinese_hotel_name_with_neighbourhood_word_is_a_title(self) -> None:
        body = (
            "\u6c88\u9633\u4e2d\u8857\u6545\u5bab\u6f2b\u5fc3\u9152\u5e97\n"
            "\u8fbd\u5b81\u7701\u6c88\u9633\u5e02\u6c88\u6cb3\u533a\u5317\u4e2d\u8857\u8def118\u53f7\u4e00\u5c42"
        )

        title, address, _, _ = _parse_share_body_lines(body, "")

        self.assertEqual(title, "\u6c88\u9633\u4e2d\u8857\u6545\u5bab\u6f2b\u5fc3\u9152\u5e97")
        self.assertIn("118\u53f7", address)

    @patch("app.share_import.search_address")
    @patch("app.share_import._follow_redirects", side_effect=RuntimeError("offline"))
    def test_unresolved_amap_link_never_accepts_generic_geocoder_hit(
        self,
        _follow_redirects,
        search_address,
    ) -> None:
        result = import_share_text(
            "Manxin Hotel\nBeizhongjie Road No.118\nhttps://surl.amap.com/example",
            preferred_source="amap",
            city_name="Shenyang",
            city_context="Shenyang China",
        )

        self.assertTrue(result.needs_map_pick)
        self.assertIsNone(result.lat)
        self.assertIsNone(result.lng)
        search_address.assert_not_called()

    def test_current_amap_top_level_p_parameter_is_parsed(self) -> None:
        final_url = (
            "https://wb.amap.com/?p=B0K6YA3YSO%2C41.80354530323765%2C"
            "123.4565408527851%2CShenyang+Middle+Street+Palace+Museum+"
            "Manxin+Hotel%2CBeizhongjie+Road+No.118"
        )

        parsed = _parse_amap_final(final_url)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[0], 41.80354530323765)
        self.assertEqual(parsed[1], 123.4565408527851)
        self.assertIn("Manxin Hotel", parsed[2])
        self.assertEqual(parsed[3], "Beizhongjie Road No.118")

    def test_rating_line_never_becomes_place_title(self) -> None:
        body = """5.0分
ZHONGJIE CBD HOTEL中心大酒店(沈阳中街故宫大悦城店)
Shenhe Wanquan Street No.3
https://surl.amap.com/example
"""
        title, address, _, _ = _parse_share_body_lines(
            body, "https://surl.amap.com/example"
        )
        self.assertEqual(title, "ZHONGJIE CBD HOTEL中心大酒店(沈阳中街故宫大悦城店)")
        self.assertIn("Street", address)

    def test_amap_url_title_wins_over_rating_like_text(self) -> None:
        chosen = _prefer_name(
            "5.0分",
            "ZHONGJIE CBD HOTEL中心大酒店(沈阳中街故宫大悦城店)",
        )
        self.assertIn("中心大酒店", chosen)

    def test_square_name_is_not_misclassified_as_address(self) -> None:
        title, address, _, _ = _parse_share_body_lines("泉城广场", "")
        self.assertEqual(title, "泉城广场")
        self.assertEqual(address, "")


if __name__ == "__main__":
    unittest.main()
