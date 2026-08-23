import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.tools import run_tool
from app.admin_api import (
    AgentProposalDecision,
    approve_agent_proposal,
    reject_agent_proposal,
)
from app.db import Base
from app.models import AgentProposal, AgentWebVisit, City, Marker, MarkerCategory, User


class AgentIntegrityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add(City(
            id=1,
            slug="shenyang",
            name_ko="선양",
            name_local="沈阳",
            center_lat=41.80,
            center_lng=123.43,
            search_viewbox="122.85,42.15,123.85,41.45",
            search_context="沈阳市 辽宁省 中国",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _proposal_args(title, url, lat, lng, *, branch=""):
        return {
            "title": title,
            "description": (
                f"{title}은 선양 도심에서 음료나 간식을 고를 수 있는 실제 지점입니다. "
                "상세 페이지의 상호·지점·좌표 근거를 함께 확인했으며 여행 동선에서 잠시 "
                "쉬어 가거나 포장 구매하기 좋은 후보로 검토할 수 있습니다."
            ),
            "category": "drink",
            "travel_role": "food",
            "branch_name": branch,
            "lat": lat + 0.05,  # model-provided location must be ignored
            "lng": lng + 0.05,
            "evidence": "상세 페이지에서 상호와 지점, 위치를 확인했습니다.",
            "source_urls": [url],
            "confidence": 0.95,
            "insights": [
                {
                    "kind": "location", "title": "위치", "content": "상세 페이지에서 지점 위치를 확인했습니다.",
                    "source_url": url,
                },
                {
                    "kind": "tip", "title": "방문", "content": "여행 중 음료를 사기 좋은 지점입니다.",
                    "source_url": url,
                },
            ],
            "_validated_source_urls": [url],
            "_coordinate_evidence": {
                "display_name": title,
                "branch_name": branch,
                "lat": lat,
                "lng": lng,
                "source": "360map_embedded_poi",
                "source_url": url,
                "external_id": f"poi-{branch}",
                "confidence": 0.87,
                "storage_allowed": True,
            },
        }

    def test_proposal_uses_server_coordinate_not_model_coordinate(self):
        url = "https://m.map.360.cn/m/search/detail/pguid=joy"
        args = self._proposal_args(
            "喜茶(大悦城店) (헤이티 다웨청점)", url, 41.8007, 123.4637, branch="大悦城店",
        )

        result = run_tool(self.db, "propose_place", args, city_id=1)

        self.assertTrue(result["proposal_created"], result)
        payload = json.loads(self.db.query(AgentProposal).one().payload)
        self.assertEqual((payload["lat"], payload["lng"]), (41.8007, 123.4637))
        self.assertEqual(payload["coordinate_external_id"], "poi-大悦城店")
        self.assertEqual(payload["coordinate_confidence"], 0.87)
        self.assertEqual(payload["_integrity_attestation"]["version"], 2)
        applied = run_tool(self.db, "create_place", payload, city_id=1, approved=True)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(self.db.query(Marker).count(), 1)

    def test_approval_rejects_tampered_signed_proposal(self):
        url = "https://m.map.360.cn/m/search/detail/pguid=joy"
        result = run_tool(
            self.db,
            "propose_place",
            self._proposal_args(
                "喜茶(大悦城店) (헤이티 다웨청점)", url,
                41.8007, 123.4637, branch="大悦城店",
            ),
            city_id=1,
        )
        self.assertTrue(result["proposal_created"])
        payload = json.loads(self.db.query(AgentProposal).one().payload)
        payload["lat"] = 41.70
        payload["lng"] = 123.20

        applied = run_tool(self.db, "create_place", payload, city_id=1, approved=True)

        self.assertEqual(applied["error"], "proposal_integrity_attestation_invalid")
        self.assertEqual(self.db.query(Marker).count(), 0)

    def test_admin_approval_rechecks_status_and_cannot_replay(self):
        admin = User(
            email="admin@example.test",
            display_name="Admin",
            password_hash="not-used",
        )
        self.db.add(admin)
        self.db.commit()
        url = "https://m.map.360.cn/m/search/detail/pguid=joy"
        created = run_tool(
            self.db,
            "propose_place",
            self._proposal_args(
                "喜茶(大悦城店) (헤이티 다웨청점)", url,
                41.8007, 123.4637, branch="大悦城店",
            ),
            city_id=1,
        )

        first = approve_agent_proposal(
            created["proposal_id"],
            AgentProposalDecision(note="verified"),
            self.db,
            admin,
        )

        self.assertEqual(first.status, "approved")
        self.assertEqual(self.db.query(Marker).count(), 1)
        with self.assertRaisesRegex(Exception, "409"):
            approve_agent_proposal(
                created["proposal_id"],
                AgentProposalDecision(note="replayed"),
                self.db,
                admin,
            )
        self.assertEqual(self.db.query(Marker).count(), 1)

    def test_reject_cannot_overwrite_applying_decision(self):
        admin = User(
            email="admin@example.test",
            display_name="Admin",
            password_hash="not-used",
        )
        proposal = AgentProposal(
            city_id=1,
            action="create_place",
            title="처리 중 제안",
            payload="{}",
            evidence="evidence",
            source_urls="[]",
            confidence=0.8,
            proposal_key="applying-proposal",
            status="applying",
        )
        self.db.add_all([admin, proposal])
        self.db.commit()

        with self.assertRaisesRegex(Exception, "409"):
            reject_agent_proposal(
                proposal.id,
                AgentProposalDecision(note="late reject"),
                self.db,
                admin,
            )

        self.db.refresh(proposal)
        self.assertEqual(proposal.status, "applying")

    def test_raised_apply_error_restores_pending_claim(self):
        admin = User(
            email="admin@example.test",
            display_name="Admin",
            password_hash="not-used",
        )
        proposal = AgentProposal(
            city_id=1,
            action="create_place",
            title="적용 예외 제안",
            payload=json.dumps({
                "title": "测试店 (테스트점)",
                "description": "예외 복구 테스트 장소입니다.",
                "category": "other",
                "lat": 41.8,
                "lng": 123.43,
            }, ensure_ascii=False),
            evidence="evidence",
            source_urls="[]",
            confidence=0.8,
            proposal_key="raising-proposal",
            status="pending",
        )
        self.db.add_all([admin, proposal])
        self.db.commit()

        with (
            patch("app.admin_api.run_tool", side_effect=RuntimeError("database unavailable")),
            self.assertRaisesRegex(Exception, "500"),
        ):
            approve_agent_proposal(
                proposal.id,
                AgentProposalDecision(note="try apply"),
                self.db,
                admin,
            )

        self.db.refresh(proposal)
        self.assertEqual(proposal.status, "pending")
        self.assertIsNone(proposal.decided_by_user_id)
        self.assertIsNone(proposal.decided_at)

    def test_approval_rechecks_legacy_ungrounded_payload(self):
        payload = {
            "title": "测试店 (테스트점)",
            "description": "근거 없이 만들어진 오래된 장소 제안입니다.",
            "category": "other",
            "lat": 41.80,
            "lng": 123.43,
            "coordinate_source": "agent_research",
            "coordinate_source_url": "",
            "coordinate_external_id": "",
        }

        applied = run_tool(self.db, "create_place", payload, city_id=1, approved=True)

        self.assertEqual(applied["error"], "place_integrity_failed")
        self.assertEqual(self.db.query(Marker).count(), 0)

    def test_ungrounded_agent_research_never_reaches_pending_proposal(self):
        url = "https://example.test/place"
        args = self._proposal_args("测试店 (테스트점)", url, 41.80, 123.43)
        args.pop("_coordinate_evidence")
        args["coordinate_source"] = "agent_research"
        args["coordinate_source_url"] = ""

        result = run_tool(self.db, "propose_place", args, city_id=1)

        self.assertEqual(result["error"], "place_integrity_failed")
        self.assertIn("coordinate_evidence_required", result["integrity_errors"])
        self.assertEqual(self.db.query(AgentProposal).count(), 0)

    def test_same_chain_different_branches_create_separate_proposals(self):
        first = self._proposal_args(
            "喜茶(大悦城店) (헤이티 다웨청점)", "https://m.map.360.cn/a",
            41.8007, 123.4637, branch="大悦城店",
        )
        second = self._proposal_args(
            "喜茶(中街益田店) (헤이티 중제 이톈점)", "https://m.map.360.cn/b",
            41.8014, 123.4520, branch="中街益田店",
        )

        one = run_tool(self.db, "propose_place", first, city_id=1)
        two = run_tool(self.db, "propose_place", second, city_id=1)

        self.assertTrue(one["proposal_created"])
        self.assertTrue(two["proposal_created"])
        self.assertEqual(self.db.query(AgentProposal).count(), 2)

    def test_uncertain_verification_stays_in_recheck_queue(self):
        marker = Marker(
            city_id=1,
            category=MarkerCategory.other,
            title="확인 필요 장소",
            description="아직 검증되지 않았습니다.",
            lat=41.80,
            lng=123.43,
        )
        self.db.add(marker)
        self.db.commit()

        result = run_tool(
            self.db,
            "verify_place",
            {"place_id": marker.id, "status": "uncertain", "note": "근거를 찾지 못했습니다."},
            city_id=1,
        )

        self.db.refresh(marker)
        self.assertTrue(result["requires_retry"])
        self.assertIsNone(result["last_verified_at"])
        self.assertIsNone(marker.last_verified_at)

    def test_description_cannot_use_another_branch_page(self):
        marker = Marker(
            city_id=1,
            category=MarkerCategory.restaurant,
            title="麦德香(文官街店) (맥데샹 문관거리점)",
            description="기존 지점 설명입니다.",
            lat=41.875,
            lng=123.482,
            is_agent_suggested=True,
        )
        wrong_url = "https://example.test/other-branch"
        self.db.add_all([
            marker,
            AgentWebVisit(
                city_id=1,
                url=wrong_url,
                title="麦香铁锅焖面(沈阳总店) 地址 菜单",
            ),
        ])
        self.db.commit()

        result = run_tool(
            self.db,
            "update_place_fields",
            {
                "place_id": marker.id,
                "expected_title": marker.title,
                "replace_description": (
                    "麦德香(文官街店) (맥데샹 문관거리점)은 북사동로의 철솥면 본점입니다."
                ),
                "source_urls": [wrong_url],
                "_validated_source_urls": [wrong_url],
            },
            city_id=1,
        )

        self.assertEqual(result["error"], "description_source_place_mismatch")
        self.assertEqual(marker.description, "기존 지점 설명입니다.")


if __name__ == "__main__":
    unittest.main()
