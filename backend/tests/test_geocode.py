from __future__ import annotations

import json
import unittest

from app.agent.tools import _ctrip_food_coordinate_url, _extract_embedded_coordinates
from app.gcj02 import gcj02_to_wgs84
from app.geocode import _local_hits, _meaningful_entity_match, _merge_hits, parse_viewbox


def hit(
    source: str,
    lat: float,
    lng: float,
    confidence: float,
    *,
    storage_allowed: bool,
    name: str = "沈阳故宫",
    marker_id: int | None = None,
) -> dict:
    return {
        "query": "沈阳故宫",
        "display_name": name,
        "lat": lat,
        "lng": lng,
        "type": "POI",
        "source": source,
        "sources": [source],
        "confidence": confidence,
        "storage_allowed": storage_allowed,
        "existing_marker_id": marker_id,
        "external_id": "Q1" if source == "wikidata" else "",
        "source_url": "",
    }


class GeocodeMergeTests(unittest.TestCase):
    def test_city_only_fallback_does_not_verify_a_business(self) -> None:
        self.assertFalse(_meaningful_entity_match(
            "沈阳 必吃！鸣记脆皮烤鱼，香辣酸甜一网打尽",
            "沈阳市 铁西区",
            "沈阳",
        ))
        self.assertTrue(_meaningful_entity_match(
            "沈阳 鸣记脆皮烤鱼 大悦城A馆",
            "鸣记脆皮烤鱼, 小东路6号大悦城A馆",
            "沈阳",
        ))

    def test_ctrip_food_detail_coordinate_is_converted_for_storage(self) -> None:
        html = '<script>{"GDCoord":{"Lat":41.7890215,"Lng":123.4169422}}</script>'

        rows = _extract_embedded_coordinates(
            html,
            "https://gs.ctrip.com/html5/you/foods/fooddetail/155/5382272.html",
        )
        expected_lat, expected_lng = gcj02_to_wgs84(41.7890215, 123.4169422)

        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["lat"], expected_lat, places=6)
        self.assertAlmostEqual(rows[0]["lng"], expected_lng, places=6)
        self.assertEqual(rows[0]["source_crs"], "GCJ-02")
        self.assertEqual(rows[0]["storage_crs"], "WGS84")
        self.assertTrue(rows[0]["storage_allowed"])

    def test_ctrip_poi_page_has_coordinate_bearing_companion(self) -> None:
        self.assertEqual(
            _ctrip_food_coordinate_url(
                "https://you.ctrip.com/food/shenyang155/15729804-dianping153077651.html"
            ),
            "https://gs.ctrip.com/html5/you/foods/fooddetail/155/15729804.html",
        )
        self.assertEqual(
            _ctrip_food_coordinate_url(
                "https://you.ctrip.com/food/shenyang155/22551502.html"
            ),
            "https://gs.ctrip.com/html5/you/foods/fooddetail/155/22551502.html",
        )
        self.assertEqual(
            _ctrip_food_coordinate_url(
                "https://you.ctrip.com/food/shenyang155/364207-food.html"
            ),
            "",
        )

    def test_embedded_coordinate_ignores_unsupported_pages(self) -> None:
        html = '<script>{"GDCoord":{"Lat":41.7890215,"Lng":123.4169422}}</script>'

        rows = _extract_embedded_coordinates(html, "https://example.com/food/place")

        self.assertEqual(rows, [])

    def test_qunar_poi_coordinate_is_converted_for_storage(self) -> None:
        html = "<script>var POI_LAT=41.8012176000;var POI_LNG=123.4632683000;</script>"

        rows = _extract_embedded_coordinates(html, "https://touch.go.qunar.com/poi/16834135")
        expected_lat, expected_lng = gcj02_to_wgs84(41.8012176, 123.4632683)

        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["lat"], expected_lat, places=6)
        self.assertAlmostEqual(rows[0]["lng"], expected_lng, places=6)
        self.assertEqual(rows[0]["source"], "qunar_embedded_poi")
        self.assertTrue(rows[0]["storage_allowed"])

    def test_qunar_dist_poi_coordinate_is_supported(self) -> None:
        html = "<script>var POI_LAT=41.801503;var POI_LNG=123.4606888;</script>"

        rows = _extract_embedded_coordinates(
            html,
            "https://touch.travel.qunar.com/dist/poi/3332184",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "qunar_embedded_poi")

    def test_360_map_detail_coordinate_and_identity_are_supported(self) -> None:
        state = {
            "searchDetailByPguid": {
                "data": {
                    "poi": {
                        "name": "孙丽丽烤猪蹄(总店)",
                        "address": "沈阳市沈河区中央里118号",
                        "x": 123.458152,
                        "y": 41.801674,
                        "primaryid": "2aa190301fafe141",
                        "tags": "餐饮|小吃快餐|小吃|猪蹄",
                    }
                }
            }
        }
        html = f"<script>window.__STATE__ = {json.dumps(state, ensure_ascii=False)};try{{}}</script>"

        rows = _extract_embedded_coordinates(
            html,
            "https://m.map.360.cn/m/search/detail/pid=2aa190301fafe141",
        )
        expected_lat, expected_lng = gcj02_to_wgs84(41.801674, 123.458152)

        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["lat"], expected_lat, places=6)
        self.assertAlmostEqual(rows[0]["lng"], expected_lng, places=6)
        self.assertEqual(rows[0]["source"], "360map_embedded_poi")
        self.assertEqual(rows[0]["external_id"], "2aa190301fafe141")
        self.assertIn("孙丽丽烤猪蹄", rows[0]["display_name"])
        self.assertTrue(rows[0]["storage_allowed"])

    def test_paused_ctrip_restaurant_is_not_storable(self) -> None:
        html = (
            "营业提示：暂停营业"
            '<script>{"GDCoord":{"Lat":41.7890215,"Lng":123.4169422}}</script>'
        )

        rows = _extract_embedded_coordinates(
            html,
            "https://gs.ctrip.com/html5/you/foods/fooddetail/155/5382272.html",
        )

        self.assertEqual(rows, [])

    def test_nominatim_viewbox_order_is_normalized(self) -> None:
        bounds = parse_viewbox("122.85,42.15,123.85,41.45")
        self.assertIsNotNone(bounds)
        assert bounds is not None
        self.assertTrue(bounds.contains(41.80, 123.43))
        self.assertFalse(bounds.contains(40.0, 123.43))
        self.assertEqual(bounds.arcgis_extent, "122.85,41.45,123.85,42.15")

    def test_open_data_coordinate_wins_over_anonymous_arcgis(self) -> None:
        rows = _merge_hits(
            [
                hit("arcgis", 41.79617, 123.44950, 0.88, storage_allowed=False),
                hit("wikidata", 41.79583, 123.45000, 0.86, storage_allowed=True),
            ],
            5,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sources"], ["arcgis", "wikidata"])
        self.assertTrue(rows[0]["storage_allowed"])
        self.assertAlmostEqual(rows[0]["lat"], 41.79583)
        self.assertEqual(rows[0]["confidence_label"], "교차 확인")

    def test_anonymous_arcgis_alone_cannot_be_stored(self) -> None:
        rows = _merge_hits(
            [hit("arcgis", 41.79905, 123.45157, 0.86, storage_allowed=False, name="中街")],
            5,
        )
        self.assertFalse(rows[0]["storage_allowed"])
        self.assertEqual(rows[0]["confidence_label"], "유력")

    def test_existing_db_place_is_first_and_marked(self) -> None:
        local = _local_hits(
            "선양고궁",
            [
                {
                    "id": 42,
                    "title": "선양고궁",
                    "description": "沈阳故宫",
                    "lat": 41.7958,
                    "lng": 123.45,
                    "type": "tourist",
                }
            ],
            5,
        )
        rows = _merge_hits(local, 5)
        self.assertEqual(rows[0]["existing_marker_id"], 42)
        self.assertEqual(rows[0]["confidence_label"], "내 지도에 등록됨")


if __name__ == "__main__":
    unittest.main()
