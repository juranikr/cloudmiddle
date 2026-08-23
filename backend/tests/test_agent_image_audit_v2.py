from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.memory import (
    filter_actionable_quality_gaps,
    quality_gaps_for_marker,
    record_quality_gap_disposition,
)
from app.agent.runner import (
    QUALITY_SOURCE_REVISIONS,
    _image_search_audit,
    _image_terminal_disposition,
)
from app.agent.tools import (
    _image_candidate_matches_place,
    _image_source_mentions_place,
    run_tool,
)
from app.db import Base
from app.models import (
    AgentRun,
    AgentRunStep,
    City,
    Marker,
    MarkerCategory,
    MarkerShape,
)


class AgentImageAuditV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add(City(
            id=2,
            slug="shenyang",
            name_ko="선양",
            name_local="沈阳",
            country_code="CN",
            center_lat=41.8057,
            center_lng=123.4315,
            search_viewbox="122.85,42.15,123.85,41.45",
            search_context="沈阳市 辽宁省 中国",
        ))
        self.place = Marker(
            city_id=2,
            category=MarkerCategory.lodging,
            shape=MarkerShape.point,
            title="瀋陽中街故宮漫心酒店 (선양 중제 고궁 만신 호텔)",
            description="沈河区의 숙소",
            coordinate_query="Shenyang Middle Street Palace Museum Manxin Hotel",
            branch_name="中街故宫店",
            lat=41.811,
            lng=123.449,
        )
        self.db.add(self.place)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_v2_source_revision_reopens_v1_source_exhaustion(self) -> None:
        now = datetime(2026, 8, 23, 3, tzinfo=timezone.utc)
        record_quality_gap_disposition(
            self.db,
            marker=self.place,
            gap_kind="image",
            disposition="source_exhausted",
            reason="v1 identity policy found no exact image",
            evidence_refs=["run:131:step:1"],
            source_revision="wikimedia:v1|openverse:v1|manual-upload:v1",
            now=now,
        )
        self.db.commit()

        actionable = filter_actionable_quality_gaps(
            self.db,
            markers=[self.place],
            gaps_by_id={self.place.id: quality_gaps_for_marker(self.place)},
            source_revisions=QUALITY_SOURCE_REVISIONS,
            now=now,
        )

        self.assertEqual(
            QUALITY_SOURCE_REVISIONS["image"],
            "wikimedia:v2|openverse:v2|manual-upload:v1",
        )
        self.assertIn("image", actionable[self.place.id])

    def test_exact_coordinate_alias_is_shared_by_search_and_attach_gate(self) -> None:
        exact = {
            "title": "File:Shenyang Middle Street Palace Museum Manxin Hotel.jpg",
            "image_url": "https://upload.wikimedia.org/manxin-hotel.jpg",
            "page_url": (
                "https://commons.wikimedia.org/wiki/"
                "File:Shenyang_Middle_Street_Palace_Museum_Manxin_Hotel.jpg"
            ),
            "provider": "Wikimedia Commons",
            "width": 1600,
            "height": 1000,
        }
        nearby = {
            "title": "File:Shenyang skyline at night.jpg",
            "image_url": "https://upload.wikimedia.org/shenyang-skyline.jpg",
            "page_url": "https://commons.wikimedia.org/wiki/File:Shenyang_skyline.jpg",
            "provider": "Wikimedia Commons",
            "width": 1600,
            "height": 1000,
        }

        def commons(query: str, **_kwargs):
            if query == self.place.coordinate_query:
                return [exact]
            return [nearby]

        with (
            patch("app.agent.tools._wikimedia_image_search", side_effect=commons),
            patch("app.agent.tools._openverse_image_search", return_value=[]),
            patch("app.agent.tools._wikimedia_geosearch", return_value=[nearby]),
        ):
            search = run_tool(
                self.db,
                "search_place_images",
                {"place_id": self.place.id, "query": "瀋陽中街故宮漫心酒店 实景", "limit": 8},
                city_id=2,
            )

        self.assertIn(self.place.coordinate_query, search["queries"])
        self.assertEqual([row["image_url"] for row in search["results"]], [exact["image_url"]])
        self.assertGreater(search["rejected_subject_mismatch"], 0)
        self.assertTrue(_image_candidate_matches_place(self.place, exact))
        self.assertFalse(_image_candidate_matches_place(self.place, nearby))
        self.assertTrue(_image_source_mentions_place(
            self.place,
            exact["title"],
            exact["page_url"],
        ))

        # S3 is checked only after the same exact-subject gate used by attach.
        with patch("app.agent.tools.storage.s3_enabled", return_value=False):
            accepted = run_tool(
                self.db,
                "attach_image_from_url",
                {
                    "place_id": self.place.id,
                    "image_url": exact["image_url"],
                    "source": f"Wikimedia Commons · {exact['title']} · {exact['page_url']}",
                },
                city_id=2,
            )
            rejected = run_tool(
                self.db,
                "attach_image_from_url",
                {
                    "place_id": self.place.id,
                    "image_url": nearby["image_url"],
                    "source": f"Wikimedia Commons · {nearby['title']} · {nearby['page_url']}",
                },
                city_id=2,
            )
        self.assertEqual(accepted["error"], "s3_disabled")
        self.assertEqual(rejected["error"], "image_source_subject_mismatch")

    def test_generic_english_discovery_query_is_not_an_identity_alias(self) -> None:
        self.place.coordinate_query = "best Shenyang tourist attractions photos"
        self.db.commit()
        candidate = {
            "title": "Best Shenyang tourist attractions photos",
            "page_url": "https://commons.wikimedia.org/wiki/File:Shenyang_skyline.jpg",
        }
        self.assertFalse(_image_candidate_matches_place(self.place, candidate))

    def test_rejected_subject_results_are_audit_uncertain_not_clean_empty(self) -> None:
        run = AgentRun(city_id=2, mode="quality", objective="image audit")
        self.db.add(run)
        self.db.flush()
        payloads = [
            {"results": []},
            {"results": [{"image_url": "https://images.example/exact.jpg"}]},
            {"results": [], "rejected_subject_mismatch": 15, "warnings": []},
        ]
        for sequence, result in enumerate(payloads, start=1):
            self.db.add(AgentRunStep(
                run_id=run.id,
                sequence=sequence,
                tool="search_place_images",
                detail=json.dumps({
                    "args": {"place_id": self.place.id, "query": f"axis {sequence}"},
                    "result": result,
                }),
            ))
        self.db.commit()

        audit = _image_search_audit(self.db, run_id=run.id, place_id=self.place.id)

        self.assertEqual(audit["clean_empty"], [f"run:{run.id}:step:1"])
        self.assertEqual(audit["with_candidates"], [f"run:{run.id}:step:2"])
        self.assertEqual(audit["audit_uncertain"], [f"run:{run.id}:step:3"])
        self.assertNotIn(f"run:{run.id}:step:3", audit["clean_empty"])

        mismatch_only = {
            "all": ["step:1", "step:2", "step:3"],
            "clean_empty": [],
            "provider_failure": [],
            "audit_uncertain": ["step:1", "step:2", "step:3"],
            "with_candidates": [],
        }
        self.assertEqual(_image_terminal_disposition(mismatch_only), "blocked")
        self.assertNotEqual(_image_terminal_disposition(mismatch_only), "source_exhausted")


if __name__ == "__main__":
    unittest.main()
