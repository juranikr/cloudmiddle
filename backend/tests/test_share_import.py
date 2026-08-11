from __future__ import annotations

import unittest

from app.share_import import _parse_share_body_lines, _prefer_name


class ShareImportParsingTests(unittest.TestCase):
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
