from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.codex_mcp import ToolContext, TravelToolBridge, serve
from app.agent.tools import run_tool as business_run_tool
from app.config import settings
from app.db import Base
from app.models import (
    AgentProposal, AgentRun, AgentRunStep, City, Marker, MarkerCategory,
    PlaceEvent, PlaceEventAction, TravelChatMessage, TravelChatWork, User,
)
from app.travel_chat import answer_travel_chat
from app.agent.runner import run_agent


class CodexTravelIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.db = self.factory()
        self.db.add_all([
            City(id=1, slug="shenyang", name_ko="선양", name_local="沈阳",
                 center_lat=41.8, center_lng=123.43,
                 search_viewbox="122.85,42.15,123.85,41.45"),
            City(id=2, slug="jinan", name_ko="지난", name_local="济南",
                 center_lat=36.65, center_lng=117.12),
            User(id=7, email="travel@example.com", display_name="여행자", password_hash="test"),
        ])
        self.db.commit()
        self.db.add_all([
            Marker(id=5, city_id=1, user_id=7, title="沈阳故宫 (선양 고궁)",
                   category=MarkerCategory.tourist, lat=41.796, lng=123.455,
                   description="현재 운영 DB에서 확인한 장소입니다."),
            Marker(id=6, city_id=2, user_id=7, title="지난의 장소", category=MarkerCategory.tourist,
                   lat=36.65, lng=117.12),
        ])
        self.db.commit()
        self.backend = patch.object(settings, "agent_backend", "codex")
        self.backend.start()

    def tearDown(self):
        self.backend.stop()
        self.db.close()
        self.engine.dispose()

    def bridge(self, run_id, *, mode="chat", city_id=1, user_id=7, max_calls=100):
        return TravelToolBridge(ToolContext(city_id, mode, run_id, user_id, max_calls), self.factory)

    def work(self):
        work = TravelChatWork(user_id=7, city_id=1, status="active", goal="여행 조사", state="{}")
        self.db.add(work)
        self.db.commit()
        return work

    @staticmethod
    def proposal():
        title = "喜茶(大悦城店) (헤이티 다웨청점)"
        url = "https://m.map.360.cn/m/search/detail/pguid=joy"
        return {
            "title": title, "branch_name": "大悦城店", "category": "drink", "travel_role": "food",
            "description": f"{title}은 선양 도심에서 음료나 간식을 고를 수 있는 실제 지점입니다. "
                           "상세 페이지의 상호와 지점 위치를 확인했으며 여행 도중 잠시 쉬거나 "
                           "음료를 포장하기 좋은 후보로 검토할 수 있습니다.",
            "lat": 1.0, "lng": 2.0, "confidence": 0.95, "evidence": "상세 페이지 상호·지점 확인",
            "source_urls": [url],
            "insights": [
                {"kind": "location", "title": "위치", "content": "상세 페이지에서 지점 위치를 확인했습니다.", "source_url": url},
                {"kind": "tip", "title": "방문", "content": "여행 중 음료를 사기 좋은 지점입니다.", "source_url": url},
            ],
        }

    def observations(self, bridge):
        args = self.proposal()
        coordinate = {"display_name": args["title"], "branch_name": "大悦城店",
                      "lat": 41.8007, "lng": 123.4637, "source": "360map_embedded_poi",
                      "source_url": args["source_urls"][0], "external_id": "poi-joy",
                      "confidence": 0.9, "storage_allowed": True}
        def tool(db, name, arguments, **kwargs):
            if name == "geocode_place":
                return {"results": [coordinate]}
            if name == "fetch_page":
                return {"url": args["source_urls"][0], "title": args["title"],
                        "text": (args["title"] + " 位于沈阳市。这是该店的公开详情页面。 ") * 12,
                        "coordinate_candidates": [coordinate]}
            return business_run_tool(db, name, arguments, **kwargs)
        return tool

    def test_chat_dispatch_preserves_response_and_persists_both_roles(self):
        def harness(prompt, instructions, **scope):
            self.assertEqual(scope["mode"], "chat")
            result = self.bridge(scope["run_id"]).execute("get_place", {"place_id": 5, "city_id": 2})
            self.assertEqual(result["id"], 5)
            return {"text": "운영 지도에 있는 장소 #5를 추천합니다. https://invented.example/fake", "model": "test-codex"}
        with patch("app.codex_travel.run_codex", side_effect=harness):
            result = answer_travel_chat(self.db, user_id=7, city_id=1, message="가까운 곳을 알려줘")
        self.assertEqual(result["model"], "test-codex")
        self.assertEqual(result["place_ids"], [5])
        self.assertNotIn("invented.example", result["row"].content)
        self.assertEqual([item.role for item in self.db.query(TravelChatMessage).order_by(TravelChatMessage.id)], ["user", "assistant"])

    def test_tool_scope_cannot_be_changed_or_approved_by_model(self):
        work = self.work()
        bridge = self.bridge(work.id)
        self.assertEqual(bridge.execute("get_place", {"place_id": 6, "city_id": 2})["error"], "not_found")
        for tool in ("create_place", "merge_places", "undo_merge", "upsert_agent_task"):
            self.assertEqual(bridge.execute(tool, {"approved": True})["error"], "tool_not_allowed")
        self.assertNotIn("create_place", {item["name"] for item in bridge.tools()})

    def test_forged_coordinate_evidence_never_reaches_business_tool(self):
        bridge = self.bridge(self.work().id)
        args = {**self.proposal(), "_coordinate_evidence": {"lat": 41.8, "lng": 123.4, "storage_allowed": True}}
        with patch("app.agent.codex_mcp.run_tool") as tool:
            result = bridge.execute("propose_place", args)
        self.assertEqual(result["error"], "coordinate_target_not_verified")
        tool.assert_not_called()

    def test_server_observations_lock_coordinates_and_actual_proposal_survives_transport_failure(self):
        def harness(prompt, instructions, **scope):
            bridge = self.bridge(scope["run_id"])
            with patch("app.agent.codex_mcp.run_tool", side_effect=self.observations(bridge)):
                bridge.execute("geocode_place", {"query": "喜茶 大悦城店"})
                bridge.execute("fetch_page", {"url": self.proposal()["source_urls"][0]})
                result = bridge.execute("propose_place", self.proposal())
                self.assertTrue(result.get("proposal_created"), result)
            raise RuntimeError("transport disconnected after business commit")
        with patch("app.codex_travel.run_codex", side_effect=harness):
            result = answer_travel_chat(self.db, user_id=7, city_id=1, message="이 지점을 지도 승인 대기에 저장해줘")
        proposal = self.db.query(AgentProposal).one()
        payload = json.loads(proposal.payload)
        self.assertEqual((payload["lat"], payload["lng"]), (41.8007, 123.4637))
        self.assertEqual(proposal.status, "pending")
        self.assertIn("실제 저장", result["row"].content)
        self.assertEqual(result["candidates"][0]["proposal_id"], proposal.id)
        self.assertEqual(self.db.query(Marker).count(), 2)
        self.assertEqual(self.db.query(TravelChatMessage).count(), 2)

    def test_chat_next_turn_resets_safety_ceiling_and_keeps_grounded_evidence(self):
        captured = []
        def harness(prompt, instructions, **scope):
            captured.append(json.loads(prompt))
            bridge = self.bridge(scope["run_id"], max_calls=1)
            self.assertTrue(bridge.execute("checkpoint_travel_work", {"state_summary": "고궁 후보를 확인", "next_action": "주변 간식 조사"})["ok"])
            return {"text": "장소 #5를 확인했습니다.", "model": "codex-test"}
        with patch("app.codex_travel.run_codex", side_effect=harness):
            answer_travel_chat(self.db, user_id=7, city_id=1, message="고궁을 알려줘")
            answer_travel_chat(self.db, user_id=7, city_id=1, message="그 주변도 계속 찾아줘")
        self.assertIn("고궁을 알려줘", captured[1]["recent_user_requests"])
        self.assertEqual(captured[1]["handoff"]["next_action"], "주변 간식 조사")
        self.assertEqual(self.db.query(TravelChatWork).count(), 2)

    def test_batch_no_work_does_not_start_harness(self):
        with patch("app.codex_travel.run_codex") as harness:
            result = run_agent(self.db, city_id=1, autonomous_research=False)
        harness.assert_not_called()
        self.assertEqual(result["outcome"], "no_work")

    def test_batch_unread_work_prevents_false_completed_and_logs_each_call(self):
        self.db.add(PlaceEvent(city_id=1, place_id=5, user_id=7, actor="user",
                               action=PlaceEventAction.update, summary="사용자 수정", payload="{}"))
        self.db.commit()
        def harness(prompt, instructions, **scope):
            self.assertFalse(json.loads(prompt)["autonomous_research"])
            bridge = self.bridge(scope["run_id"], mode="batch")
            self.assertEqual(len(bridge.execute("list_unread_events", {})), 1)
            bridge.execute("checkpoint_travel_work", {"state_summary": "사용자 수정을 읽음", "next_action": "원문 검증 후 처리"})
            return {"text": "모든 작업을 마쳤습니다.", "model": "codex-test"}
        with patch("app.codex_travel.run_codex", side_effect=harness):
            result = run_agent(self.db, city_id=1, autonomous_research=True)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unread_after"], 1)
        self.assertEqual(self.db.query(AgentRunStep).count(), 2)
        ledger = json.loads(self.db.query(AgentRun).one().metrics)["codex"]
        self.assertEqual(ledger["handoff"]["next_action"], "원문 검증 후 처리")

    def test_batch_cannot_acknowledge_unseen_event(self):
        run = AgentRun(city_id=1, status="running", objective="검토", metrics="{}")
        self.db.add(run)
        self.db.commit()
        result = self.bridge(run.id, mode="batch").execute("mark_events_read", {"event_ids": [999]})
        self.assertEqual(result["error"], "event_not_observed")
        self.assertEqual(self.db.query(AgentRunStep).one().outcome, "error")

    def test_budget_is_server_owned_and_pending_ledger_is_not_destroyed(self):
        bridge = self.bridge(self.work().id, max_calls=1)
        bridge.execute("get_place", {"place_id": 5})
        result = bridge.execute("get_place", {"place_id": 5})
        self.assertEqual(result["error"], "tool_safety_ceiling")

    def test_batch_safety_limit_is_partial_and_final_handoff_survives_ceiling(self):
        def harness(prompt, instructions, **scope):
            bridge = self.bridge(scope["run_id"], mode="batch", max_calls=1)
            bridge.execute("get_place", {"place_id": 5})
            self.assertEqual(bridge.execute("get_place", {"place_id": 5})["error"], "tool_safety_ceiling")
            result = bridge.execute("checkpoint_travel_work", {"state_summary": "고궁을 읽었으나 근거 확인 미완료", "next_action": "공식 페이지를 읽고 검증"})
            self.assertTrue(result["ok"])
            return {"text": "조사를 끝냈습니다.", "model": "codex-test"}
        with patch("app.codex_travel.run_codex", side_effect=harness):
            result = run_agent(self.db, city_id=1, autonomous_research=True, max_steps=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["outcome"], "safety_limit")
        self.assertIn("공식 페이지를 읽고 검증", result["remaining_gaps"])
        self.assertEqual(self.db.query(AgentRunStep).count(), 2)

    def test_stale_running_chat_is_recoverable_and_old_subprocess_is_fenced(self):
        from app.codex_travel import _chat_work
        work = self.work()
        work.state = json.dumps({"candidates": [{"key": "exact", "title": "기존 정확한 후보"}], "codex": {
            "status": "running", "started_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "handoff": {"next_action": "정확한 기존 후보를 검증"},
        }})
        self.db.commit()
        old_id = work.id
        fresh = _chat_work(self.db, user_id=7, city_id=1, message="계속")
        self.assertNotEqual(fresh.id, old_id)
        self.assertEqual(json.loads(fresh.state)["candidates"][0]["key"], "exact")
        self.assertEqual(self.bridge(old_id).execute("get_place", {"place_id": 5})["error"], "inactive_or_foreign_execution")

    def test_durable_json_is_never_sliced_mid_document(self):
        from app.codex_travel import _dump
        value = {"codex": {"tool_trace": [{"result": {"text": "문" * 600000}}]}}
        self.assertEqual(json.loads(_dump(value, 500000)), value)

    def test_transient_provider_payload_is_not_logged_or_returned(self):
        bridge = self.bridge(self.work().id)
        with patch("app.agent.codex_mcp.run_tool", return_value={"results": [], "place_candidates": [{"title": "SECRET TRANSIENT"}], "provider_attempts": ["SECRET TRANSIENT"]}) as tool:
            result = bridge.execute("web_search", {"query": "선양"})
        self.assertFalse(tool.call_args.kwargs["server_allow_brave_places"])
        self.assertNotIn("SECRET TRANSIENT", json.dumps(result))
        self.db.expire_all()
        self.assertNotIn("SECRET TRANSIENT", self.db.query(TravelChatWork).one().state)

    def test_jsonl_mcp_protocol_initializes_and_lists_scoped_tools(self):
        bridge = self.bridge(self.work().id)
        incoming = io.StringIO('\n'.join(json.dumps(item) for item in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_place", "arguments": {"place_id": 5}}},
        ]))
        outgoing = io.StringIO()
        with patch("sys.stdin", incoming), patch("sys.stdout", outgoing):
            serve(bridge)
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "cloudmiddle-travel")
        self.assertFalse(responses[2]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
