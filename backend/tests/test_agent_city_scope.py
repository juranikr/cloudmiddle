from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.runner import count_unread
from app.agent.tools import run_tool
from app.db import Base
from app.knowledge import upsert_knowledge
from app.models import (
    AgentProposal,
    City,
    Marker,
    MarkerCategory,
    MarkerShape,
    PlaceEvent,
    PlaceEventAction,
    PlaceInsight,
)


class AgentCityScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all(
            [
                City(
                    id=1,
                    slug="jinan",
                    name_ko="지난",
                    name_local="济南",
                    center_lat=36.65,
                    center_lng=117.12,
                    search_viewbox="116.7,36.95,117.55,36.35",
                    search_context="济南市 山东省 中国",
                ),
                City(
                    id=2,
                    slug="shenyang",
                    name_ko="선양",
                    name_local="沈阳",
                    center_lat=41.80,
                    center_lng=123.43,
                    search_viewbox="122.85,42.15,123.85,41.45",
                    search_context="沈阳市 辽宁省 中国",
                ),
            ]
        )
        for city_id, title, lat, lng in [
            (1, "趵突泉 (바오투취안)", 36.66, 117.01),
            (2, "沈阳故宫 (선양고궁)", 41.796, 123.45),
        ]:
            marker = Marker(
                city_id=city_id,
                category=MarkerCategory.tourist,
                shape=MarkerShape.point,
                title=title,
                description="한국어 설명",
                lat=lat,
                lng=lng,
            )
            self.db.add(marker)
            self.db.flush()
            self.db.add(
                PlaceEvent(
                    place_id=marker.id,
                    actor="user",
                    action=PlaceEventAction.create,
                    summary=title,
                )
            )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_city_queue_and_place_listing_do_not_mix(self) -> None:
        self.assertEqual(count_unread(self.db, 1), 1)
        self.assertEqual(count_unread(self.db, 2), 1)
        rows = run_tool(self.db, "list_places", {}, city_id=2)
        self.assertEqual([row["city_id"] for row in rows], [2])

    def test_disabled_auto_create_becomes_evidence_proposal(self) -> None:
        result = run_tool(
            self.db,
            "create_place",
            {
                "title": "张氏帅府 (장씨수부)",
                "description": "근대 동북 역사를 이해하는 장소입니다.",
                "category": "tourist",
                "lat": 41.793,
                "lng": 123.449,
                "evidence": "공식 박물관 안내와 좌표를 확인했습니다.",
                "source_urls": ["https://example.org/official"],
                "confidence": 0.9,
                "insights": [
                    {
                        "kind": "location",
                        "title": "공간의 역할",
                        "content": "근대 선양의 정치 중심을 보여주는 공간입니다.",
                        "source_url": "https://example.org/official",
                        "confidence": 0.9,
                    },
                    {
                        "kind": "history",
                        "title": "장씨 가문의 시대",
                        "content": "20세기 전반 동북 지역사의 주요 무대였습니다.",
                        "year_label": "20세기 전반",
                        "source_url": "https://example.org/official",
                        "confidence": 0.85,
                    },
                ],
            },
            city_id=2,
        )
        self.assertTrue(result["proposal_created"])
        proposal = self.db.query(AgentProposal).one()
        self.assertEqual(proposal.city_id, 2)
        self.assertEqual(proposal.status, "pending")
        applied = run_tool(
            self.db,
            proposal.action,
            json.loads(proposal.payload),
            city_id=proposal.city_id,
            approved=True,
        )
        self.assertTrue(applied["ok"])
        created = self.db.query(Marker).filter(Marker.id == applied["place_id"]).one()
        self.assertEqual(created.city_id, 2)
        self.assertEqual(self.db.query(PlaceInsight).filter(PlaceInsight.place_id == created.id).count(), 2)

    def test_same_topic_is_namespaced_per_city(self) -> None:
        first = upsert_knowledge(
            self.db,
            topic="research_strategy",
            title="지난 전략",
            content="지난 자료",
            city_id=1,
        )
        second = upsert_knowledge(
            self.db,
            topic="research_strategy",
            title="선양 전략",
            content="선양 자료",
            city_id=2,
        )
        self.db.commit()
        self.assertNotEqual(first.topic, second.topic)
        self.assertEqual({first.topic, second.topic}, {"city:1:research_strategy", "city:2:research_strategy"})


if __name__ == "__main__":
    unittest.main()
