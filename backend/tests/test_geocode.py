from __future__ import annotations

import unittest

from app.geocode import _local_hits, _merge_hits, parse_viewbox


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
