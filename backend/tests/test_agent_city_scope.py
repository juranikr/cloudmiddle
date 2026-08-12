from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.runner import (
    _compact_react_messages,
    _ensure_gap_tasks,
    _is_material_change,
    _performance_delta,
    _performance_score,
    _performance_snapshot,
    _new_evidence_keys,
    _normalize_research_query,
    _research_gaps,
    _run_outcome_status,
    _sync_quality_tasks,
    _step_detail_json,
    _tool_signature,
    count_unread,
)
from app.agent.tools import TOOLS, run_tool
from app.agent.tools import is_useful_fetched_page
from app.db import Base
from app.knowledge import rebuild_knowledge_base, upsert_knowledge
from app.rollback import list_agent_actions
from app.models import (
    AgentKnowledge,
    AgentKnowledgeArchive,
    AgentProposal,
    AgentTask,
    City,
    Marker,
    MarkerCategory,
    MarkerShape,
    PlaceEvent,
    PlaceEventAction,
    PlaceInsight,
    PlaceImage,
    PlaceChain,
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

    def test_batch_progress_ignores_noop_mutations_and_repeated_evidence(self) -> None:
        self.assertFalse(_is_material_change("upsert_agent_task", {"ok": True, "changed": False}))
        self.assertFalse(_is_material_change("upsert_agent_task", {"ok": True, "created": True}))
        self.assertTrue(_is_material_change("propose_place", {"proposal_id": 91}))

        result = {"results": [{"seen": False, "href": "https://example.test/place"}]}
        keys = _new_evidence_keys("web_search", result, set())
        self.assertEqual(keys, {"url:https://example.test/place"})
        self.assertEqual(_new_evidence_keys("web_search", result, keys), set())
        image_keys = _new_evidence_keys(
            "search_place_images",
            {"results": [{"image_url": "https://images.example.test/place.jpg"}]},
            set(),
        )
        self.assertEqual(image_keys, {"image:https://images.example.test/place.jpg"})

        challenge = {
            "url": "https://example.test/login",
            "title": "验证中心",
            "text": "请登录后查看" * 40,
            "already_visited": False,
        }
        self.assertEqual(_new_evidence_keys("fetch_page", challenge, set()), set())
        useful_page = {
            "url": "https://example.test/guide",
            "title": "沈阳交通指南",
            "text": "선양 공항과 도심 교통에 관한 구체적인 안내입니다. " * 12,
            "already_visited": False,
        }
        self.assertEqual(
            _new_evidence_keys("fetch_page", useful_page, set()),
            {"page:https://example.test/guide"},
        )
        self.assertFalse(is_useful_fetched_page(challenge))
        self.assertTrue(is_useful_fetched_page(useful_page))

        self.assertEqual(
            _tool_signature("web_search", {"limit": 5, "query": "沈阳"}),
            _tool_signature("web_search", {"query": "沈阳", "limit": 5}),
        )
        self.assertEqual(
            _normalize_research_query("  诚意小厨   皇姑店  "),
            _normalize_research_query("诚意小厨 皇姑店"),
        )

        detail = _step_detail_json(
            {"query": "沈阳"},
            {"results": [{"href": f"https://example.test/{index}", "text": "가" * 5000} for index in range(8)]},
            {"new_evidence": 8},
            max_chars=3000,
        )
        parsed = json.loads(detail)
        self.assertTrue(parsed["truncated"])
        self.assertLessEqual(len(detail), 3000)

    def test_long_react_context_is_compacted_by_complete_tool_round(self) -> None:
        messages: list[dict] = [
            {"role": "system", "content": "stable system prompt"},
            {"role": "user", "content": "stable batch objective"},
        ]
        for index in range(8):
            call_id = f"call-{index}"
            messages.extend([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "web_search", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": call_id, "content": "x" * 4000},
            ])
        compacted, changed = _compact_react_messages(
            messages,
            tool_counts={"web_search": 8},
            material_changes=[{"tool": "update_place"}],
            current_score=4.0,
            max_chars=18_000,
        )
        self.assertTrue(changed)
        self.assertEqual(compacted[0]["content"], "stable system prompt")
        self.assertEqual(compacted[1]["content"], "stable batch objective")
        self.assertIn("자동 압축", compacted[2]["content"])
        assistant_ids = {
            message["tool_calls"][0]["id"]
            for message in compacted
            if message.get("role") == "assistant" and message.get("tool_calls")
        }
        tool_ids = {
            message["tool_call_id"]
            for message in compacted
            if message.get("role") == "tool"
        }
        self.assertEqual(assistant_ids, tool_ids)
        self.assertIn("call-7", tool_ids)
        self.assertLessEqual(len(json.dumps(compacted, ensure_ascii=False)), 18_000)

    def test_quality_snapshot_and_backlog_measure_real_points(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        place.is_agent_suggested = True
        zone = Marker(
            city_id=2,
            category=MarkerCategory.tourist,
            shape=MarkerShape.polygon,
            title="중제권",
            description="구역 폴리곤은 사진 결손 장소로 계산하면 안 됩니다.",
            lat=41.8,
            lng=123.45,
            polygon="[]",
        )
        self.db.add(zone)
        self.db.commit()

        before = _performance_snapshot(self.db, 2)
        self.assertEqual(before["active_places"], 1)
        self.assertEqual(before["imageless_places"], 1)
        self.assertEqual(before["suggested_drafts"], 1)
        task_ids = _sync_quality_tasks(self.db, city_id=2, run_id=21)
        self.assertGreaterEqual(len(task_ids), 4)
        image_task = self.db.query(AgentTask).filter(AgentTask.kind == "quality_images").one()
        self.assertIn(f"#{place.id}", image_task.detail)
        self.assertNotIn(f"#{zone.id}", image_task.detail)
        duplicate = run_tool(
            self.db,
            "upsert_agent_task",
            {
                "title": f"이미지 보강 실패: {place.title}",
                "detail": "자유 라이선스 사진 부재",
                "status": "pending",
            },
            city_id=2,
        )
        self.assertEqual(duplicate["error"], "quality_gap_already_tracked")
        self.assertEqual(duplicate["task_id"], image_task.id)
        image_task.attempts = 1
        place.description = "x" * 61
        self.db.commit()
        _sync_quality_tasks(self.db, city_id=2, run_id=21)
        self.db.refresh(image_task)
        self.assertEqual(image_task.attempts, 1)
        self.assertLess(image_task.priority, 96)

        self.db.add(PlaceImage(place_id=place.id, s3_key="places/test.jpg"))
        self.db.commit()
        after = _performance_snapshot(self.db, 2)
        delta = _performance_delta(before, after)
        self.assertEqual(delta["imageless_places"], -1)
        self.assertGreaterEqual(_performance_score(delta, {}), 8)
        gaps = _research_gaps(delta, {"list_agent_tasks": 1, "list_zones": 1}, after)
        self.assertFalse(any(gap.startswith("사진 없는 실제 장소") for gap in gaps))

        _sync_quality_tasks(self.db, city_id=2, run_id=22)
        self.db.refresh(image_task)
        self.assertEqual(image_task.status, "completed")

    def test_batch_gap_tasks_are_measurable_and_deduplicated(self) -> None:
        first = _ensure_gap_tasks(
            self.db,
            city_id=2,
            run_id=6,
            gaps=["여행 역할 균형: 음식 2/3", "근거 페이지 4개 이상 확보(현재 2)"],
        )
        second = _ensure_gap_tasks(
            self.db,
            city_id=2,
            run_id=7,
            gaps=["여행 역할 균형: 음식 2/3", "근거 페이지 4개 이상 확보(현재 2)"],
        )
        self.assertEqual(first, second)
        rows = self.db.query(AgentTask).filter(AgentTask.city_id == 2).all()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.success_metric and "remaining_gaps" in row.success_metric for row in rows))
        self.assertGreater(rows[0].priority, rows[1].priority)

        _ensure_gap_tasks(
            self.db,
            city_id=2,
            run_id=8,
            gaps=["근거 페이지 4개 이상 확보(현재 2)"],
        )
        self.db.refresh(rows[0])
        self.assertEqual(rows[0].status, "completed")
        self.assertIn("자동 완료", rows[0].result)

    def test_batch_status_tracks_this_run_not_the_entire_city_backlog(self) -> None:
        gaps = ["사진 없는 실제 장소 14/27"]
        self.assertEqual(
            _run_outcome_status(unread_after=0, gaps=gaps, material_change_count=3),
            "completed",
        )
        self.assertEqual(
            _run_outcome_status(unread_after=0, gaps=gaps, material_change_count=0),
            "partial",
        )
        self.assertEqual(
            _run_outcome_status(unread_after=2, gaps=[], material_change_count=3),
            "partial",
        )

    def test_procedural_checklists_are_not_persisted_as_performance_gaps(self) -> None:
        gaps = _research_gaps({}, {}, {"active_places": 0})
        self.assertNotIn("이전 조사 백로그 확인", gaps)
        self.assertNotIn("구역 현황 확인", gaps)
        ids = _ensure_gap_tasks(
            self.db,
            city_id=2,
            run_id=9,
            gaps=["이전 조사 백로그 확인", "구역 현황 확인"],
        )
        self.assertEqual(ids, [])

    def test_verification_requires_read_source_and_rejects_other_district_branch(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        place.title = "辽铭宴 (沈河店)"
        place.description = "沈河区中街"
        self.db.commit()

        no_read = run_tool(
            self.db,
            "verify_place",
            {
                "place_id": place.id,
                "status": "valid",
                "note": "https://example.test/tiexi 에서 영업 확인",
            },
            city_id=2,
        )
        self.assertEqual(no_read["error"], "verification_source_not_validated")
        other_branch = run_tool(
            self.db,
            "verify_place",
            {
                "place_id": place.id,
                "status": "valid",
                "note": "铁西店 景星北街3号 https://example.test/tiexi",
                "_validated_source_urls": ["https://example.test/tiexi"],
            },
            city_id=2,
        )
        self.assertEqual(other_branch["error"], "verification_branch_mismatch")
        uncertain = run_tool(
            self.db,
            "verify_place",
            {
                "place_id": place.id,
                "status": "uncertain",
                "note": "현재 지점을 뒷받침할 유효 본문을 찾지 못함",
            },
            city_id=2,
        )
        self.assertTrue(uncertain["ok"])

    def test_place_insights_require_a_fetched_source(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        insight = {
            "kind": "visit",
            "title": "방문 팁",
            "content": "실제 본문에서 확인한 한국어 방문 팁입니다.",
            "source_url": "https://example.test/place-guide",
            "confidence": 0.8,
        }
        rejected = run_tool(
            self.db,
            "upsert_place_insights",
            {"place_id": place.id, "insights": [insight]},
            city_id=2,
        )
        self.assertEqual(rejected["error"], "insight_source_not_validated")
        accepted = run_tool(
            self.db,
            "upsert_place_insights",
            {
                "place_id": place.id,
                "insights": [insight],
                "_validated_source_urls": ["https://example.test/place-guide"],
            },
            city_id=2,
        )
        self.assertEqual(accepted["changed"], 1)

    def test_admin_agent_history_can_be_scoped_to_city(self) -> None:
        for city_id in (1, 2):
            marker = self.db.query(Marker).filter(Marker.city_id == city_id).one()
            self.db.add(PlaceEvent(
                place_id=marker.id,
                actor="agent",
                action=PlaceEventAction.update,
                summary=f"city {city_id}",
                payload=json.dumps({"before": {"title": marker.title}}),
            ))
        self.db.commit()
        rows = list_agent_actions(self.db, city_id=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].summary, "city 2")

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

    def test_propose_place_alias_creates_reviewable_proposal(self) -> None:
        tool_names = {tool["function"]["name"] for tool in TOOLS}
        self.assertIn("propose_place", tool_names)
        result = run_tool(
            self.db,
            "propose_place",
            {
                "title": "北陵公园 (베이링 공원)",
                "description": "청 황실 능원과 공원을 함께 보는 장소입니다.",
                "category": "tourist",
                "lat": 41.85,
                "lng": 123.43,
                "evidence": "공식 안내와 위치 자료를 교차 확인했습니다.",
                "source_urls": ["https://example.org/beiling"],
                "confidence": 0.9,
                "insights": [
                    {
                        "kind": "location",
                        "title": "북릉권 중심",
                        "content": "선양 북부의 대표적인 역사 관광 구역입니다.",
                        "source_url": "https://example.org/beiling",
                    },
                    {
                        "kind": "history",
                        "title": "청 황실 능원",
                        "content": "청 초기 황실사를 이해할 수 있는 능원입니다.",
                        "source_url": "https://example.org/beiling",
                    },
                ],
            },
            city_id=2,
        )
        self.assertTrue(result["proposal_created"])
        self.assertEqual(self.db.query(AgentProposal).count(), 1)

    def test_same_pending_place_title_is_deduplicated_and_completes_legacy_task(self) -> None:
        legacy_task = AgentTask(
            city_id=2,
            title="승인 제안: '北陵公园 (Beiling Park)' 신규 장소 추가",
            status="pending",
        )
        self.db.add(legacy_task)
        self.db.commit()
        base = {
            "title": "北陵公园 (베이링 공원)",
            "description": "청 황실 능원과 공원을 함께 보는 장소입니다.",
            "category": "tourist",
            "lat": 41.85,
            "lng": 123.43,
            "evidence": "공식 안내와 위치 자료를 교차 확인했습니다.",
            "source_urls": ["https://example.org/beiling"],
            "confidence": 0.9,
            "insights": [
                {
                    "kind": "location",
                    "title": "북릉권 중심",
                    "content": "선양 북부의 대표적인 역사 관광 구역입니다.",
                    "source_url": "https://example.org/beiling",
                },
                {
                    "kind": "history",
                    "title": "청 황실 능원",
                    "content": "청 초기 황실사를 이해할 수 있는 능원입니다.",
                    "source_url": "https://example.org/beiling",
                },
            ],
        }
        first = run_tool(self.db, "propose_place", base, city_id=2)
        second = run_tool(
            self.db,
            "propose_place",
            {**base, "lat": 41.851, "evidence": "다른 출처에서도 재확인했습니다."},
            city_id=2,
        )
        self.db.refresh(legacy_task)
        self.assertTrue(first["proposal_created"])
        self.assertFalse(second["proposal_created"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.db.query(AgentProposal).count(), 1)
        self.assertEqual(legacy_task.status, "completed")

    def test_place_proposal_cannot_masquerade_as_backlog_task(self) -> None:
        result = run_tool(
            self.db,
            "upsert_agent_task",
            {
                "kind": "research",
                "title": "승인 제안: 신규 장소 추가",
                "detail": "후보를 나중에 지도에 등록",
                "status": "pending",
            },
            city_id=2,
        )
        self.assertEqual(result["error"], "proposal_masquerading_as_task")
        self.assertEqual(self.db.query(AgentTask).count(), 0)

    def test_run_summary_cannot_pollute_knowledge(self) -> None:
        result = run_tool(
            self.db,
            "upsert_knowledge",
            {
                "topic": "cycle_summary",
                "title": "선양 지도 정리 사이클 요약",
                "content": "7개의 신규 장소 승인 제안 생성 완료",
                "category": "workflow",
            },
            city_id=2,
        )
        self.assertEqual(result["error"], "run_history_forbidden_in_knowledge")
        self.assertEqual(self.db.query(AgentKnowledge).count(), 0)

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

    def test_repeating_same_place_update_is_idempotent(self) -> None:
        marker = self.db.query(Marker).filter(Marker.city_id == 2).one()
        event_count = self.db.query(PlaceEvent).count()
        result = run_tool(
            self.db,
            "update_place_fields",
            {
                "place_id": marker.id,
                "expected_title": marker.title,
                "replace_title": marker.title,
                "replace_description": marker.description,
                "category": marker.category.value,
            },
            city_id=2,
        )
        self.assertEqual(result, {"ok": True, "changed": {}})
        self.assertEqual(self.db.query(PlaceEvent).count(), event_count)

    def test_place_update_rejects_cross_subject_description(self) -> None:
        marker = self.db.query(Marker).filter(Marker.city_id == 2).one()
        marker.is_agent_suggested = True
        original = marker.description

        result = run_tool(
            self.db,
            "update_place_fields",
            {
                "place_id": marker.id,
                "expected_title": marker.title,
                "replace_description": "청년공원은 1958년에 완공된 선허구의 도시공원입니다.",
            },
            city_id=2,
        )

        self.assertEqual(result["error"], "description_subject_mismatch")
        self.assertEqual(marker.description, original)

    def test_place_update_requires_current_expected_title(self) -> None:
        marker = self.db.query(Marker).filter(Marker.city_id == 2).one()

        result = run_tool(
            self.db,
            "update_place_fields",
            {
                "place_id": marker.id,
                "expected_title": "다른 장소",
                "travel_role": "nature",
            },
            city_id=2,
        )

        self.assertEqual(result["error"], "target_confirmation_required")
        self.assertNotEqual(marker.travel_role, "nature")

    def test_zone_and_chain_are_relationships_not_merges(self) -> None:
        zone = Marker(
            city_id=2,
            category=MarkerCategory.tourist,
            shape=MarkerShape.polygon,
            title="중제·고궁권",
            description="도보 관광 구역",
            lat=41.79,
            lng=123.45,
            polygon=json.dumps([
                {"lat": 41.78, "lng": 123.43},
                {"lat": 41.81, "lng": 123.43},
                {"lat": 41.81, "lng": 123.47},
            ]),
        )
        self.db.add(zone)
        self.db.flush()
        place = self.db.query(Marker).filter(Marker.city_id == 2, Marker.shape == MarkerShape.point).one()
        zoned = run_tool(
            self.db,
            "assign_place_zone",
            {"place_id": place.id, "zone_id": zone.id, "reason": "중제 도보권"},
            city_id=2,
        )
        self.assertTrue(zoned["changed"])
        chained = run_tool(
            self.db,
            "assign_place_chain",
            {
                "place_id": place.id,
                "chain_name_local": "测试品牌",
                "chain_name_ko": "테스트 브랜드",
                "branch_name": "중제점",
                "reason": "동일 브랜드의 독립 지점",
            },
            city_id=2,
        )
        self.assertTrue(chained["changed"])
        self.db.refresh(place)
        self.assertEqual(place.zone_id, zone.id)
        self.assertIsNotNone(place.chain_id)
        self.assertIsNone(place.merged_into_id)
        self.assertEqual(self.db.query(PlaceChain).count(), 1)

    def test_zone_assignment_rejects_a_polygon_that_does_not_contain_place(self) -> None:
        wrong_zone = Marker(
            city_id=2,
            category=MarkerCategory.tourist,
            shape=MarkerShape.polygon,
            title="훈난권",
            description="멀리 떨어진 구역",
            lat=41.65,
            lng=123.45,
            polygon=json.dumps([
                {"lat": 41.63, "lng": 123.40},
                {"lat": 41.63, "lng": 123.50},
                {"lat": 41.70, "lng": 123.50},
                {"lat": 41.70, "lng": 123.40},
            ]),
        )
        self.db.add(wrong_zone)
        self.db.commit()
        place = self.db.query(Marker).filter(Marker.city_id == 2).first()
        result = run_tool(
            self.db,
            "assign_place_zone",
            {"place_id": place.id, "zone_id": wrong_zone.id, "reason": "잘못 기억한 ID"},
            city_id=2,
        )
        self.assertEqual(result["error"], "place_outside_zone_polygon")
        self.assertIsNone(place.zone_id)

    def test_approved_place_without_zone_is_inferred_from_polygon(self) -> None:
        zone = Marker(
            city_id=2,
            category=MarkerCategory.tourist,
            shape=MarkerShape.polygon,
            title="중제·고궁권",
            description="도보 관광 구역",
            lat=41.795,
            lng=123.45,
            polygon=json.dumps([
                {"lat": 41.78, "lng": 123.43},
                {"lat": 41.78, "lng": 123.47},
                {"lat": 41.81, "lng": 123.47},
                {"lat": 41.81, "lng": 123.43},
            ]),
        )
        self.db.add(zone)
        self.db.commit()
        applied = run_tool(
            self.db,
            "create_place",
            {
                "title": "张氏帅府 (장씨수부)",
                "description": "근대 동북 역사를 이해하는 장소입니다.",
                "category": "tourist",
                "lat": 41.793,
                "lng": 123.449,
            },
            city_id=2,
            approved=True,
        )
        created = self.db.get(Marker, applied["place_id"])
        self.assertEqual(created.zone_id, zone.id)

    def test_description_append_is_rejected_in_favor_of_insights(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        before = place.description
        result = run_tool(
            self.db,
            "update_place_fields",
            {"place_id": place.id, "append_note": "운영시간을 덧붙입니다."},
            city_id=2,
        )
        self.assertEqual(result["error"], "structured_insight_required")
        self.assertEqual(place.description, before)

    def test_knowledge_rebuild_archives_journals_and_seeds_playbooks(self) -> None:
        upsert_knowledge(
            self.db,
            topic="research_strategy",
            title="누적 일지",
            content="다음 사이클에는 계속 조사한다.\n---\n오래된 실행 로그",
            city_id=2,
        )
        self.db.commit()
        result = rebuild_knowledge_base(self.db)
        self.assertGreaterEqual(result["archived"], 1)
        self.assertEqual(result["active"], 5)
        self.assertEqual(self.db.query(AgentKnowledgeArchive).count(), result["archived"])
        self.assertEqual(self.db.query(AgentKnowledge).count(), 5)


if __name__ == "__main__":
    unittest.main()
