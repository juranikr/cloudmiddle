from __future__ import annotations

import json
import re
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.runner import (
    CANDIDATE_DISCOVERY_COOLDOWN_HOURS,
    CANDIDATE_DISCOVERY_INTERVAL,
    CANDIDATE_DISCOVERY_KIND,
    CANDIDATE_ROLE_TARGETS,
    CANDIDATE_DISCOVERY_TOOLS,
    DATA_INTEGRITY_TOOLS,
    RECOVERY_TOOLS_BY_TASK,
    _active_agent_task_mismatch,
    _data_integrity_evidence_refs,
    _data_integrity_corrective_tools,
    _halt_stalled_mission,
    _project_data_integrity_task_result_args,
    _project_structured_integrity_result,
    _compact_react_messages,
    _ensure_gap_tasks,
    _ensure_candidate_discovery_task,
    _is_material_change,
    _performance_delta,
    _performance_score,
    _performance_snapshot,
    _page_supports_coordinate_evidence,
    _new_evidence_keys,
    _normalize_research_query,
    _active_target_mismatch,
    _mission_has_no_executable_target,
    _model_output_failure_kind,
    _model_recovery_plan,
    _research_gaps,
    _run_outcome_status,
    _canonical_quality_task,
    _candidate_discovery_research_refs,
    _candidate_discovery_due,
    _candidate_mission_role,
    _candidate_task_role,
    _fair_non_discovery_task,
    _persistable_tool_result,
    _select_autonomous_task,
    _scoped_quality_block_evidence_refs,
    _scoped_quality_tools,
    _sync_quality_tasks,
    _should_rotate_exhausted_image_target,
    _step_detail_json,
    _tool_signature,
    count_unread,
    run_agent,
)
from app.agent.tools import TOOLS, run_tool
from app.agent.tools import is_useful_fetched_page, _image_relevance, _matching_existing_place
from app.db import Base
from app.knowledge import rebuild_knowledge_base, upsert_knowledge
from app.agent.memory import (
    CORRECTIVE_POLICY_GUARD_ERRORS,
    checkpoint_after_tool,
    evaluate_knowledge_uses,
    ensure_mission_for_task,
    finalize_mission,
    learn_from_recent_runs,
    retrieve_contextual_knowledge,
    rotate_blocked_work_item,
    reconcile_work_items,
    observe_lesson,
    finish_model_recovery_attempt,
    record_model_recovery_attempt,
    active_work_item_for_mission,
)
from app.rollback import list_agent_actions
from app.models import (
    AgentCheckpoint,
    AgentKnowledge,
    AgentKnowledgeUse,
    AgentKnowledgeArchive,
    AgentProposal,
    AgentQualityGapDisposition,
    AgentRun,
    AgentRunStep,
    AgentSearchLog,
    AgentTask,
    AgentWebVisit,
    AgentEvidence,
    AgentLesson,
    AgentMission,
    AgentWorkItem,
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


def _structured_integrity_args(
    request: dict,
    *,
    task_id: int,
    verdict: str = "unresolved",
    reason: str = "Server-owned observation supports this terminal audit result",
) -> dict:
    schema = next(
        tool["function"]["parameters"]
        for tool in request["tools"]
        if tool["function"]["name"] == "upsert_agent_task"
    )
    refs = schema["properties"]["evidence_refs"]["items"]["enum"]
    return {
        "task_id": task_id,
        "status": "completed",
        "verdict": verdict,
        "reason": reason,
        "marker_changes": 0,
        "evidence_refs": refs[:1],
    }


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

    def test_nearby_coordinate_from_different_page_is_not_exact_poi_evidence(self) -> None:
        coordinate = {
            "display_name": "沈阳故宫角楼咖啡",
            "address": "沈河区沈阳路171号",
            "lat": 41.7963,
            "lng": 123.4552,
        }
        unrelated_page = {
            "title": "老边饺子中街店",
            "text": "老边饺子位于沈河区中街路，提供东北菜和饺子。" * 10,
            "coordinate_candidates": [
                {
                    "lat": 41.7971,
                    "lng": 123.4560,
                    "source": "ctrip_embedded_gdcoord",
                    "storage_allowed": True,
                }
            ],
        }
        matching_page = {
            **unrelated_page,
            "title": "沈阳故宫角楼咖啡",
            "text": "沈阳故宫角楼咖啡位于沈河区沈阳路171号。" * 10,
        }

        self.assertFalse(
            _page_supports_coordinate_evidence(unrelated_page, coordinate)
        )
        self.assertTrue(
            _page_supports_coordinate_evidence(matching_page, coordinate)
        )

    def _run_scripted_agent(
        self,
        calls: list[tuple[str, dict]],
        *,
        max_steps: int,
    ) -> tuple[dict, list[dict]]:
        requests: list[dict] = []
        scripted_index = 0

        class Completions:
            def create(inner_self, **kwargs):
                nonlocal scripted_index
                requests.append(kwargs)
                if scripted_index < len(calls):
                    name, args = calls[scripted_index]
                    args = dict(args)
                    upsert_schema = next(
                        (
                            tool["function"]["parameters"]
                            for tool in kwargs.get("tools", [])
                            if tool["function"]["name"] == "upsert_agent_task"
                        ),
                        {},
                    )
                    is_structured_schema = "verdict" in (
                        upsert_schema.get("properties") or {}
                    )
                    terminal_verdict = re.search(
                        r"verdict\s*=\s*(confirmed|conflict|unresolved)",
                        str(args.get("result") or ""),
                        re.IGNORECASE,
                    )
                    if name == "upsert_agent_task" and terminal_verdict:
                        if is_structured_schema:
                            refs = (
                                upsert_schema["properties"]["evidence_refs"]
                                ["items"]["enum"]
                            )
                            args = {
                                "task_id": args["task_id"],
                                "status": "completed",
                                "verdict": terminal_verdict.group(1).lower(),
                                "reason": str(args.get("result") or "")[:500],
                                "marker_changes": 0,
                                "evidence_refs": refs[:1],
                            }
                            scripted_index += 1
                    else:
                        scripted_index += 1
                    call = SimpleNamespace(
                        id=f"scripted-{len(requests)}",
                        function=SimpleNamespace(
                            name=name,
                            arguments=json.dumps(args),
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="script complete", tool_calls=[],
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=max_steps,
                autonomous_research=True,
            )
        return result, requests

    def test_city_queue_and_place_listing_do_not_mix(self) -> None:
        self.assertEqual(count_unread(self.db, 1), 1)
        self.assertEqual(count_unread(self.db, 2), 1)
        rows = run_tool(self.db, "list_places", {}, city_id=2)
        self.assertEqual([row["city_id"] for row in rows], [2])

    def test_unread_queue_preempts_integrity_mission_then_next_run_resumes_target(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title=f"[integrity:queue-preemption:#{place.id}] exact target",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="read only the exact target and record a grounded result",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        self.db.commit()
        event_ids = [
            row.id
            for row in self.db.query(PlaceEvent)
            .join(Marker, Marker.id == PlaceEvent.place_id)
            .filter(Marker.city_id == 2, PlaceEvent.groq_read_at.is_(None))
            .all()
        ]
        self.assertTrue(event_ids)
        attempts_before = (task.attempts, item.attempts)
        checkpoints_before = self.db.query(AgentCheckpoint).count()
        queue_requests: list[dict] = []

        class QueueCompletions:
            def create(inner_self, **kwargs):
                queue_requests.append(kwargs)
                calls = [
                    SimpleNamespace(
                        id="drain-user-queue",
                        function=SimpleNamespace(
                            name="mark_events_read",
                            arguments=json.dumps({"event_ids": event_ids}),
                        ),
                    ),
                    # A model may optimistically append autonomous work to the
                    # same response. It must be acknowledged but not executed.
                    SimpleNamespace(
                        id="must-not-transition-to-research",
                        function=SimpleNamespace(
                            name="web_search",
                            arguments=json.dumps({"query": "unrelated city research"}),
                        ),
                    ),
                ]
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=calls,
                ))])

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=QueueCompletions())
        )
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]) as sync,
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            queue_result = run_agent(
                self.db,
                city_id=2,
                max_steps=5,
                autonomous_research=True,
            )

        self.assertTrue(queue_result["ok"], queue_result)
        self.assertEqual(len(queue_requests), 1)
        sync.assert_not_called()
        self.db.refresh(task)
        self.db.refresh(mission)
        self.db.refresh(item)
        self.assertEqual((task.attempts, item.attempts), attempts_before)
        self.assertEqual(mission.status, "active")
        self.assertEqual(item.status, "active")
        self.assertEqual(self.db.query(AgentCheckpoint).count(), checkpoints_before)
        self.assertEqual(
            self.db.query(AgentRunStep).filter(
                AgentRunStep.run_id == queue_result["run_id"],
                AgentRunStep.tool == "web_search",
            ).count(),
            0,
        )
        queue_run = self.db.get(AgentRun, queue_result["run_id"])
        self.assertEqual(queue_run.mode, "queue")
        self.assertIsNone(queue_run.mission_id)
        self.assertIsNone(queue_run.work_item_id)
        self.assertEqual(count_unread(self.db, 2), 0)

        research_result, research_requests = self._run_scripted_agent(
            [("get_place", {"place_id": place.id})],
            max_steps=1,
        )

        self.assertTrue(research_result["ok"], research_result)
        self.assertEqual(len(research_requests), 1)
        research_run = self.db.get(AgentRun, research_result["run_id"])
        self.assertEqual(research_run.mode, "research")
        self.assertEqual(research_run.mission_id, mission.id)
        self.assertEqual(research_run.work_item_id, item.id)
        self.db.refresh(task)
        self.assertEqual(task.attempts, attempts_before[0] + 1)
        target_step = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == research_run.id,
            AgentRunStep.tool == "get_place",
        ).one()
        self.assertEqual(json.loads(target_step.detail)["result"]["id"], place.id)
        target_checkpoint = self.db.query(AgentCheckpoint).filter(
            AgentCheckpoint.run_id == research_run.id,
            AgentCheckpoint.mission_id == mission.id,
            AgentCheckpoint.work_item_id == item.id,
        ).one()
        self.assertEqual(target_checkpoint.outcome, "ok")

    def test_new_unread_event_during_integrity_run_is_deferred_to_next_queue_run(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title=f"[integrity:new-unread:#{place.id}] keep focus",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="finish this target without absorbing newly arrived queue work",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        requests: list[dict] = []

        class IntegrityCompletions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                if len(requests) == 1:
                    call = SimpleNamespace(
                        id="read-current-target",
                        function=SimpleNamespace(
                            name="get_place",
                            arguments=json.dumps({"place_id": place.id}),
                        ),
                    )
                elif len(requests) == 2:
                    self.db.add(PlaceEvent(
                        place_id=place.id,
                        actor="user",
                        action=PlaceEventAction.update,
                        summary="arrived while integrity mission was active",
                    ))
                    self.db.commit()
                    call = SimpleNamespace(
                        id="request-terminal-schema",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps({
                                "task_id": task.id,
                                "status": "completed",
                                "result": "legacy free-form terminal attempt",
                            }),
                        ),
                    )
                else:
                    call = SimpleNamespace(
                        id="grounded-terminal-result",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps(_structured_integrity_args(
                                kwargs,
                                task_id=task.id,
                                reason=f"Active place #{place.id} was observed before completion",
                            )),
                        ),
                    )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=IntegrityCompletions())
        )
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=4,
                autonomous_research=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["unread_after"], 1)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(
            "미처리 큐" not in json.dumps(request["messages"], ensure_ascii=False)
            for request in requests[1:]
        ))
        self.db.refresh(task)
        self.assertEqual(task.status, "completed")
        mission = self.db.query(AgentMission).filter(
            AgentMission.task_id == task.id
        ).one()
        attempts_after_integrity = task.attempts

        event_id = self.db.query(PlaceEvent.id).filter(
            PlaceEvent.summary == "arrived while integrity mission was active",
            PlaceEvent.groq_read_at.is_(None),
        ).scalar()
        self.assertIsNotNone(event_id)
        queue_requests: list[dict] = []

        class QueueCompletions:
            def create(inner_self, **kwargs):
                queue_requests.append(kwargs)
                call = SimpleNamespace(
                    id="process-deferred-event",
                    function=SimpleNamespace(
                        name="mark_events_read",
                        arguments=json.dumps({"event_ids": [event_id]}),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_client.chat.completions = QueueCompletions()
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]) as sync,
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            queue_result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        self.assertTrue(queue_result["ok"], queue_result)
        sync.assert_not_called()
        queue_run = self.db.get(AgentRun, queue_result["run_id"])
        self.assertEqual(queue_run.mode, "queue")
        self.assertIsNone(queue_run.mission_id)
        self.assertIsNone(queue_run.work_item_id)
        self.db.refresh(task)
        self.db.refresh(mission)
        self.assertEqual(task.attempts, attempts_after_integrity)
        self.assertEqual(mission.status, "completed")

    def test_durable_mission_splits_quality_task_into_resumable_place_work_items(self) -> None:
        places = self.db.query(Marker).filter(Marker.city_id == 2).all()
        second = Marker(
            city_id=2,
            category=MarkerCategory.restaurant,
            shape=MarkerShape.point,
            title="诚意小厨(皇姑店)",
            description="한국어 설명",
            lat=41.82,
            lng=123.41,
        )
        self.db.add(second)
        self.db.flush()
        task = AgentTask(
            city_id=2,
            kind="quality_verification",
            title="운영 검증",
            detail=(
                "대상:\n"
                f"- #{places[0].id} {places[0].title} (현재: 미검증)\n"
                f"- #{second.id} {second.title} (현재: 미검증)"
            ),
            success_metric="각 장소의 last_verified_at 기록",
            priority=78,
        )
        self.db.add(task)
        self.db.commit()

        mission, active = ensure_mission_for_task(self.db, task)
        items = self.db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission.id).order_by(AgentWorkItem.id).all()
        self.assertEqual(len(items), 2)
        self.assertEqual(active.place_id, places[0].id)
        self.assertEqual(json.loads(active.next_action)["tool"], "get_place")

        mission_again, active_again = ensure_mission_for_task(self.db, task)
        self.assertEqual(mission_again.id, mission.id)
        self.assertEqual(active_again.id, active.id)
        self.assertEqual(self.db.query(AgentMission).count(), 1)

    def test_resuming_blocked_item_starts_a_fresh_consecutive_failure_window(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="quality_information",
            title="information quality",
            detail=f"targets:\n- #{place.id} {place.title} (current: thin)",
            success_metric="two structured insights",
            priority=78,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        mission.status = "paused"
        item.status = "blocked"
        item.failed_approaches = json.dumps(["source A failed", "source B failed", "source C failed"])
        item.blocked_reason = "three source paths failed"
        item.retry_condition = "cooldown"
        self.db.commit()

        resumed_mission, resumed = ensure_mission_for_task(self.db, task)

        self.assertEqual(resumed_mission.id, mission.id)
        self.assertEqual(resumed.status, "active")
        self.assertEqual(json.loads(resumed.failed_approaches), [])
        self.assertEqual(resumed.blocked_reason, "")
        self.assertEqual(resumed.retry_condition, "")

    def test_checkpoint_persists_new_evidence_and_exact_next_action(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2, kind="quality_verification", title="운영 검증",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 미검증)",
            success_metric="last_verified_at 기록", priority=78,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        run = AgentRun(city_id=2, mission_id=mission.id, work_item_id=item.id, status="running")
        self.db.add(run)
        self.db.commit()

        updated, continuity = checkpoint_after_tool(
            self.db, mission=mission, work_item=item, run_id=run.id, sequence=1,
            tool="web_search", args={"query": place.title},
            result={"results": [{"href": "https://example.test/place", "title": "공식 안내", "body": "운영 정보", "seen": False}]},
            outcome="ok", new_evidence_count=1, material_change=False,
        )
        self.db.commit()
        self.assertEqual(json.loads(updated.next_action)["tool"], "fetch_page")
        self.assertEqual(continuity["next_action"]["args"]["url"], "https://example.test/place")
        evidence = self.db.query(AgentEvidence).one()
        self.assertEqual(evidence.source_status, "discovered")

        _, seen_continuity = checkpoint_after_tool(
            self.db, mission=mission, work_item=updated, run_id=run.id, sequence=2,
            tool="web_search", args={"query": place.title + " 재검색"},
            result={"results": [{"href": "https://example.test/old", "title": "기존 결과", "body": "이미 확인", "seen": True}]},
            outcome="ok", new_evidence_count=0, material_change=False,
        )
        self.assertEqual(seen_continuity["next_action"]["tool"], "choose_alternative_source")

    def test_candidate_checkpoint_keeps_storable_exact_dossier_after_later_error(self) -> None:
        task = AgentTask(
            city_id=2,
            kind=CANDIDATE_DISCOVERY_KIND,
            title="휴식 역할 후보",
            detail="target_role: rest\n정확한 휴식 장소 후보 발굴",
            success_metric="travel_role=rest 제안",
            priority=88,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        run = AgentRun(
            city_id=2,
            mission_id=mission.id,
            work_item_id=item.id,
            status="running",
        )
        self.db.add(run)
        self.db.commit()

        item, continuity = checkpoint_after_tool(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            sequence=1,
            tool="geocode_place",
            args={"query": "沈阳 独立茶馆"},
            result={"results": [{
                "display_name": "独立茶馆, 沈河区中街路88号",
                "address": "沈河区中街路88号",
                "lat": 41.8012,
                "lng": 123.4521,
                "source": "nominatim",
                "external_id": "node:123",
                "source_url": "https://www.openstreetmap.org/node/123",
                "storage_allowed": True,
            }]},
            outcome="ok",
            new_evidence_count=1,
            material_change=False,
        )
        dossier = continuity["next_action"]
        self.assertEqual(dossier["handoff_version"], "candidate_dossier_v1")
        self.assertEqual(dossier["tool"], "web_search")
        self.assertEqual(dossier["candidate"]["external_id"], "node:123")
        self.assertIn("独立茶馆", dossier["next_exact_query"])

        item, continuity = checkpoint_after_tool(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            sequence=2,
            tool="web_search",
            args=dossier["args"],
            result={
                "error": "recent_duplicate_search",
                "detail": "source axis already used",
            },
            outcome="error",
            new_evidence_count=0,
            material_change=False,
        )

        retained = continuity["next_action"]
        self.assertEqual(retained["handoff_version"], "candidate_dossier_v1")
        self.assertEqual(retained["candidate"]["external_id"], "node:123")
        self.assertEqual(retained["last_error"], "recent_duplicate_search")
        self.assertNotEqual(retained["tool"], "choose_alternative_source")

    def test_candidate_fetch_failure_advances_to_unread_source_then_exact_alternative(self) -> None:
        task = AgentTask(
            city_id=2,
            kind=CANDIDATE_DISCOVERY_KIND,
            title="휴식 역할 후보",
            detail="target_role: rest\n정확한 휴식 장소 후보 발굴",
            success_metric="travel_role=rest 제안",
            priority=88,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        run = AgentRun(
            city_id=2,
            mission_id=mission.id,
            work_item_id=item.id,
            status="running",
        )
        self.db.add(run)
        self.db.commit()

        first_url = "https://first.example.test/tea-house"
        second_url = "https://second.example.test/tea-house"
        item, continuity = checkpoint_after_tool(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            sequence=1,
            tool="web_search",
            args={"query": '"独立茶馆" "沈河区中街路88号"'},
            result={
                "results": [
                    {
                        "href": first_url,
                        "title": "独立茶馆 공식 안내",
                        "body": "沈河区中街路88号",
                        "seen": False,
                    },
                    {
                        "href": second_url,
                        "title": "独立茶馆 예약 안내",
                        "body": "沈河区中街路88号",
                        "seen": False,
                    },
                ]
            },
            outcome="ok",
            new_evidence_count=2,
            material_change=False,
        )
        dossier = continuity["next_action"]
        self.assertEqual(dossier["args"]["url"], first_url)

        item, continuity = checkpoint_after_tool(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            sequence=2,
            tool="fetch_page",
            args={"url": first_url},
            result={"error": "fetch_failed: HTTP 403"},
            outcome="error",
            new_evidence_count=0,
            material_change=False,
        )
        advanced = continuity["next_action"]
        self.assertEqual(advanced["handoff_version"], "candidate_source_v1")
        self.assertEqual(advanced["candidate"], dossier["candidate"])
        self.assertEqual(advanced["failed_source"]["url"], first_url)
        self.assertEqual(advanced["tool"], "fetch_page")
        self.assertEqual(advanced["args"]["url"], second_url)
        self.assertNotEqual(advanced["args"]["url"], first_url)

        _item, continuity = checkpoint_after_tool(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            sequence=3,
            tool="fetch_page",
            args={"url": second_url},
            result={"error": "page_not_useful_evidence"},
            outcome="error",
            new_evidence_count=0,
            material_change=False,
        )
        alternative = continuity["next_action"]
        self.assertEqual(alternative["candidate"], dossier["candidate"])
        self.assertEqual(alternative["failed_source"]["url"], second_url)
        self.assertEqual(alternative["tool"], "web_search")
        self.assertNotIn(first_url, alternative["args"]["query"])
        self.assertNotIn(second_url, alternative["args"]["query"])
        self.assertIn("-site:first.example.test", alternative["args"]["query"])
        self.assertIn("-site:second.example.test", alternative["args"]["query"])
        self.assertEqual(
            {entry["url"] for entry in alternative["failed_sources"]},
            {first_url, second_url},
        )

    def test_integrity_policy_guards_do_not_consume_investigation_failure_budget(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="policy correction",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="record an honest unresolved verdict",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        run = AgentRun(
            city_id=2,
            mission_id=mission.id,
            work_item_id=item.id,
            status="running",
        )
        self.db.add(run)
        self.db.commit()
        tools_by_error = {
            "duplicate_data_integrity_place_read": "get_place",
            "data_integrity_task_list_budget_exhausted": "list_agent_tasks",
            "active_agent_task_mismatch": "upsert_agent_task",
            "invalid_data_integrity_task_status": "upsert_agent_task",
            "invalid_data_integrity_task_result": "upsert_agent_task",
            "tool_not_allowed_for_data_integrity": "verify_place",
            "material_decision_required": "fetch_page",
            "recent_duplicate_search": "web_search",
            "duplicate_tool_call": "fetch_page",
            "structured_integrity_verdict_required": "upsert_agent_task",
        }
        decide_errors = {
            "duplicate_data_integrity_place_read",
            "data_integrity_task_list_budget_exhausted",
            "material_decision_required",
            "structured_integrity_verdict_required",
        }

        for sequence, error in enumerate(sorted(CORRECTIVE_POLICY_GUARD_ERRORS), start=1):
            item, continuity = checkpoint_after_tool(
                self.db,
                mission=mission,
                work_item=item,
                run_id=run.id,
                sequence=sequence,
                tool=tools_by_error[error],
                args={"place_id": place.id},
                result={"error": error, "detail": "correct the action choice"},
                outcome="error",
                new_evidence_count=0,
                material_change=False,
            )
            action = json.loads(item.next_action)
            if error in decide_errors:
                self.assertEqual(
                    action["phase"], "data_integrity_terminal_verdict_v1"
                )
                self.assertEqual(action["tool"], "upsert_agent_task")
                self.assertEqual(action["task_id"], task.id)
                self.assertEqual(action["status"], "completed")
                self.assertEqual(action["guard_disposition"], "decide")
                self.assertNotIn("args", action)
                self.assertIn("evidence_refs", action["required_fields"])
            else:
                self.assertEqual(action["tool"], "continue")
                self.assertEqual(action["args"]["task_id"], task.id)
            self.assertEqual(continuity["failed_approaches"], [])

        self.assertEqual(json.loads(item.failed_approaches), [])
        checkpoints = (
            self.db.query(AgentCheckpoint)
            .filter(AgentCheckpoint.run_id == run.id)
            .order_by(AgentCheckpoint.sequence)
            .all()
        )
        self.assertEqual(len(checkpoints), len(CORRECTIVE_POLICY_GUARD_ERRORS))
        self.assertTrue(
            all(json.loads(checkpoint.rejected_claims) == [] for checkpoint in checkpoints)
        )

    def test_policy_guard_does_not_terminally_close_non_integrity_mission(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="research",
            title="ordinary research duplicate",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="continue research without premature completion",
            priority=50,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        run = AgentRun(
            city_id=2,
            mission_id=mission.id,
            work_item_id=item.id,
            status="running",
        )
        self.db.add(run)
        self.db.commit()

        item, continuity = checkpoint_after_tool(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            sequence=1,
            tool="web_search",
            args={"query": "same search"},
            result={
                "error": "recent_duplicate_search",
                "error_class": "policy_guard",
                "guard_disposition": "retry",
                "detail": "reuse existing results",
            },
            outcome="error",
            new_evidence_count=0,
            material_change=False,
        )

        action = json.loads(item.next_action)
        self.assertNotEqual(action["tool"], "upsert_agent_task")
        self.assertNotEqual(item.stage, "decide")
        self.assertTrue(
            any(
                failure.startswith("web_search: recent_duplicate_search")
                for failure in continuity["failed_approaches"]
            )
        )
        self.assertEqual(task.status, "pending")
        self.assertEqual(mission.status, "active")

    def test_checkpoint_strips_nul_from_untrusted_web_evidence(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="quality_verification",
            title="verify web text",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="validated source",
            priority=78,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        run = AgentRun(city_id=2, mission_id=mission.id, work_item_id=item.id, status="running")
        self.db.add(run)
        self.db.commit()

        checkpoint_after_tool(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            sequence=1,
            tool="fetch_page",
            args={"url": "https://example.test/hotel"},
            result={
                "url": "https://example.test/hotel",
                "title": "hotel\x00 title",
                "text": "usable\x00 hotel evidence",
            },
            outcome="ok",
            new_evidence_count=1,
            material_change=False,
        )
        self.db.commit()

        evidence = self.db.query(AgentEvidence).one()
        self.assertEqual(evidence.title, "hotel title")
        self.assertEqual(evidence.excerpt, "usable hotel evidence")

    def test_material_progress_resets_consecutive_failure_rotation_count(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="quality_information",
            title="information quality",
            detail=f"targets:\n- #{place.id} {place.title} (current: thin)",
            success_metric="two structured insights",
            priority=78,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        item.failed_approaches = json.dumps(["old source failed", "old write failed"])
        run = AgentRun(city_id=2, mission_id=mission.id, work_item_id=item.id, status="running")
        self.db.add(run)
        self.db.commit()

        updated, continuity = checkpoint_after_tool(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            sequence=1,
            tool="upsert_place_insights",
            args={"place_id": place.id},
            result={"ok": True, "changed": 1, "place_id": place.id},
            outcome="changed",
            new_evidence_count=0,
            material_change=True,
        )

        self.assertEqual(json.loads(updated.failed_approaches), [])
        self.assertEqual(continuity["failed_approaches"], [])

    def test_contextual_knowledge_prefers_exact_city_and_source_strategy(self) -> None:
        upsert_knowledge(
            self.db, topic="dianping_source", title="Dianping 인증 화면 대응",
            content="로그인 화면은 검증 근거가 아니며 다른 출처를 선택한다.",
            scope="city", city_id=2, category="source", quality_score=0.95,
            keywords=["dianping", "로그인", "검증"],
            applicability={"task_kinds": ["quality_verification"], "domains": ["dianping.com"]},
        )
        upsert_knowledge(
            self.db, topic="jinan_springs", title="지난 샘물",
            content="지난의 샘물 조사 지식", scope="city", city_id=1, category="city",
        )
        self.db.commit()
        task = AgentTask(
            city_id=2, kind="quality_verification", title="운영 검증",
            detail="Dianping 로그인 화면 이후 다른 출처 검증", priority=78,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        item.failed_approaches = json.dumps(["Dianping 로그인 화면"])
        self.db.commit()
        found = retrieve_contextual_knowledge(
            self.db, city_id=2, mission=mission, work_item=item,
            query="Dianping 로그인 검증", limit=5,
        )
        topics = [entry["topic"] for entry in found["knowledge"]]
        self.assertIn("city:2:dianping_source", topics)
        self.assertNotIn("city:1:jinan_springs", topics)

    def test_dianping_login_shell_is_not_useful_evidence(self) -> None:
        shell = {
            "url": "https://www.dianping.com/shop/1",
            "title": "大众点评网",
            "text": "登录 APP扫码，享七天免登录 二维码已失效 账号登录/注册 " * 15,
        }
        self.assertFalse(is_useful_fetched_page(shell))

    def test_image_relevance_handles_nullable_provider_metadata(self) -> None:
        score = _image_relevance(
            {"provider": None, "title": "喜茶沈阳大悦城店", "page_url": "", "width": 1200, "height": 800},
            "喜茶沈阳大悦城店",
        )
        self.assertGreater(score, 0)

    def test_new_agent_task_handles_defaults_before_first_flush(self) -> None:
        result = run_tool(
            self.db,
            "upsert_agent_task",
            {
                "title": "공식 교통 페이지를 다른 출처로 재검증",
                "detail": "다음 실행에서 확인",
                "success_metric": "본문 근거 1건",
                "status": "pending",
            },
            city_id=2,
        )
        self.assertTrue(result["created"])
        self.assertTrue(result["changed"])
        task = self.db.get(AgentTask, result["task_id"])
        self.assertEqual(task.kind, "research")

    def test_optional_agent_task_id_accepts_explicit_null(self) -> None:
        schema = next(
            tool["function"]["parameters"]
            for tool in TOOLS
            if tool["function"]["name"] == "upsert_agent_task"
        )
        self.assertEqual(schema["properties"]["task_id"]["type"], ["integer", "null"])
        result = run_tool(
            self.db,
            "upsert_agent_task",
            {"task_id": None, "title": "근거 재수집", "status": "blocked"},
            city_id=2,
        )
        self.assertTrue(result["created"])

    def test_explicit_place_call_cannot_drift_from_active_target(self) -> None:
        work_item = AgentWorkItem(
            mission_id=1,
            city_id=2,
            place_id=83,
            target_key="place:83",
            title="현재 장소",
        )
        mismatch = _active_target_mismatch(
            "search_place_images", {"place_id": 90, "query": "다른 장소"}, work_item
        )
        self.assertEqual(mismatch["error"], "active_work_item_mismatch")
        self.assertEqual(mismatch["active_place_id"], 83)
        self.assertIsNone(
            _active_target_mismatch("search_place_images", {"place_id": 83}, work_item)
        )
        integrity_read_mismatch = _active_target_mismatch(
            "get_place",
            {"place_id": 101},
            work_item,
            mission_kind="data_integrity",
        )
        self.assertEqual(
            integrity_read_mismatch["error"], "active_work_item_mismatch"
        )
        integrity_write_mismatch = _active_target_mismatch(
            "search_place_images",
            {"place_id": 101},
            work_item,
            mission_kind="data_integrity",
        )
        self.assertEqual(integrity_write_mismatch["error"], "active_work_item_mismatch")

    def test_paused_mission_stops_remaining_parallel_tool_calls(self) -> None:
        mission = AgentMission(city_id=2, title="사진 보강", status="paused")
        self.assertTrue(_mission_has_no_executable_target(mission, None))
        mission.status = "active"
        self.assertFalse(_mission_has_no_executable_target(mission, None))
        self.assertFalse(
            _mission_has_no_executable_target(
                mission,
                AgentWorkItem(
                    mission_id=1,
                    city_id=2,
                    target_key="place:83",
                    title="장소",
                    status="active",
                ),
            )
        )

    def test_blocked_target_rotates_and_pauses_only_when_every_target_is_blocked(self) -> None:
        places = self.db.query(Marker).filter(Marker.city_id == 2).all()
        second = Marker(
            city_id=2, category=MarkerCategory.restaurant, shape=MarkerShape.point,
            title="두 번째 대상", description="한국어 설명", lat=41.82, lng=123.42,
        )
        self.db.add(second)
        self.db.flush()
        task = AgentTask(
            city_id=2, kind="quality_verification", title="순환 검증",
            detail=f"대상:\n- #{places[0].id} {places[0].title}\n- #{second.id} {second.title}",
            success_metric="검증", priority=78,
        )
        self.db.add(task)
        self.db.commit()
        mission, first = ensure_mission_for_task(self.db, task)
        run = AgentRun(
            city_id=2,
            mission_id=mission.id,
            work_item_id=first.id,
            status="running",
        )
        self.db.add(run)
        self.db.commit()
        next_item = rotate_blocked_work_item(
            self.db, mission=mission, current=first, run_id=run.id, reason="세 경로 실패",
        )
        self.assertIsNotNone(next_item)
        self.assertNotEqual(next_item.id, first.id)
        self.assertEqual(first.status, "blocked")
        self.assertEqual(next_item.status, "active")
        self.assertEqual(run.work_item_id, next_item.id)

        final = rotate_blocked_work_item(
            self.db, mission=mission, current=next_item, run_id=run.id, reason="세 경로 실패",
        )
        self.assertIsNone(final)
        self.assertEqual(mission.status, "paused")
        self.assertEqual(run.work_item_id, next_item.id)

    def test_stalled_mission_transition_rolls_back_every_row_if_commit_fails(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        sibling = Marker(
            city_id=2,
            category=MarkerCategory.restaurant,
            shape=MarkerShape.point,
            title="atomic sibling",
            description="ready sibling",
            lat=41.82,
            lng=123.39,
        )
        self.db.add(sibling)
        self.db.flush()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="atomic stall",
            detail=(
                f"targets:\n- #{place.id} {place.title}\n"
                f"- #{sibling.id} {sibling.title}"
            ),
            success_metric="atomic terminal checkpoint",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        mission, current = ensure_mission_for_task(self.db, task)
        ready = (
            self.db.query(AgentWorkItem)
            .filter(
                AgentWorkItem.mission_id == mission.id,
                AgentWorkItem.status == "ready",
            )
            .one()
        )
        run = AgentRun(
            city_id=2,
            mission_id=mission.id,
            work_item_id=current.id,
            status="running",
        )
        self.db.add(run)
        self.db.commit()
        original_ready_attempts = ready.attempts
        original_ready_last_run_id = ready.last_run_id

        with patch.object(
            self.db,
            "commit",
            side_effect=RuntimeError("simulated final commit failure"),
        ) as commit:
            with self.assertRaisesRegex(RuntimeError, "simulated final commit failure"):
                _halt_stalled_mission(
                    self.db,
                    mission=mission,
                    work_item=current,
                    run_id=run.id,
                    sequence=0,
                    reason="atomic rollback test",
                )
        self.assertEqual(commit.call_count, 1)

        self.db.expire_all()
        persisted_mission = self.db.get(AgentMission, mission.id)
        persisted_current = self.db.get(AgentWorkItem, current.id)
        persisted_ready = self.db.get(AgentWorkItem, ready.id)
        persisted_run = self.db.get(AgentRun, run.id)
        self.assertEqual(persisted_mission.status, "active")
        self.assertEqual(persisted_current.status, "active")
        self.assertEqual(persisted_current.blocked_reason, "")
        self.assertEqual(persisted_ready.status, "ready")
        self.assertEqual(persisted_ready.attempts, original_ready_attempts)
        self.assertEqual(persisted_ready.last_run_id, original_ready_last_run_id)
        self.assertEqual(persisted_run.work_item_id, current.id)
        self.assertEqual(
            self.db.query(AgentRunStep).filter(AgentRunStep.run_id == run.id).count(),
            0,
        )
        self.assertEqual(
            self.db.query(AgentCheckpoint).filter(AgentCheckpoint.run_id == run.id).count(),
            0,
        )

    def test_stalled_mission_returns_ready_resume_cursor_without_rewriting_run_target(self) -> None:
        first = self.db.query(Marker).filter(Marker.city_id == 2).one()
        second = Marker(
            city_id=2,
            category=MarkerCategory.restaurant,
            shape=MarkerShape.point,
            title="resume sibling",
            description="ready sibling",
            lat=41.82,
            lng=123.39,
        )
        self.db.add(second)
        self.db.flush()
        task = AgentTask(
            city_id=2,
            kind="quality_information",
            title="stalled cursor handoff",
            detail=(
                f"targets:\n- #{first.id} {first.title}\n"
                f"- #{second.id} {second.title}"
            ),
            success_metric="resume the unprocessed sibling",
            priority=90,
        )
        self.db.add(task)
        self.db.commit()
        mission, current = ensure_mission_for_task(self.db, task)
        ready = self.db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.status == "ready",
        ).one()
        run = AgentRun(
            city_id=2,
            mission_id=mission.id,
            work_item_id=current.id,
            status="running",
        )
        self.db.add(run)
        self.db.commit()

        resume = _halt_stalled_mission(
            self.db,
            mission=mission,
            work_item=current,
            run_id=run.id,
            sequence=0,
            reason="no progress",
        )

        self.assertEqual(resume.id, ready.id)
        self.assertEqual(resume.status, "ready")
        self.assertEqual(current.status, "blocked")
        self.assertEqual(mission.status, "paused")
        self.assertEqual(json.loads(mission.progress)["resume_work_item_id"], ready.id)
        self.assertEqual(run.work_item_id, current.id)

    def test_first_lesson_observation_handles_database_defaults_before_commit(self) -> None:
        lesson = observe_lesson(
            self.db,
            key="first_observation",
            city_id=2,
            category="workflow",
            trigger="처음 관찰",
            action="다음 실행에 적용",
            expected_effect="반복 방지",
            evidence_ref="run:1:step:1",
            successful=False,
        )
        self.db.commit()
        self.assertEqual(lesson.observation_count, 1)
        self.assertEqual(lesson.failure_count, 1)
        self.assertLess(lesson.confidence, 0.5)

    def test_model_output_recovery_is_persisted_and_measured_as_a_lesson(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="quality_images",
            title="image recovery",
            detail=f"targets\n- #{place.id} {place.title}",
            success_metric="image_count >= 1",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        item.failed_approaches = json.dumps(["fetch_page: fetch_failed - HTTP 403"])
        run = AgentRun(city_id=2, mission_id=mission.id, work_item_id=item.id, status="running")
        self.db.add(run)
        self.db.commit()
        strategy = _model_recovery_plan(
            failure_kind="output_parse_failed",
            attempt=1,
            model="openai/gpt-oss-120b",
            mission=mission,
            work_item=item,
            prompt_chars=80_000,
        )

        ref = record_model_recovery_attempt(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            sequence=1,
            failure_kind="output_parse_failed",
            error="Parsing failed",
            attempt=1,
            strategy=strategy,
        )
        lesson = finish_model_recovery_attempt(
            self.db,
            mission=mission,
            work_item=item,
            run_id=run.id,
            evidence_ref=ref,
            failure_kind="output_parse_failed",
            strategy=strategy,
            successful=True,
        )
        self.db.commit()

        history = json.loads(mission.strategy)["recovery_history"]
        self.assertEqual(history[-1]["outcome"], "recovered")
        self.assertEqual(json.loads(mission.progress)["last_recovery"]["outcome"], "recovered")
        self.assertEqual(lesson.success_count, 1)
        self.assertEqual(lesson.failure_count, 0)
        self.assertIn("output_parse_failed", item.state_summary)
        self.assertEqual(
            json.loads(item.failed_approaches),
            ["fetch_page: fetch_failed - HTTP 403"],
        )
        recovery_checkpoint = (
            self.db.query(AgentCheckpoint)
            .filter(
                AgentCheckpoint.run_id == run.id,
                AgentCheckpoint.outcome == "recovery_retry",
            )
            .one()
        )
        self.assertTrue(
            any(
                failure.startswith("model_output:output_parse_failed:")
                for failure in json.loads(recovery_checkpoint.failed_approaches)
            )
        )

    def test_recovery_lesson_is_evaluated_only_when_its_trigger_occurs(self) -> None:
        lesson = AgentLesson(
            lesson_key="model_output_recovery:tool_schema_failed:focused_retry",
            scope="global",
            category="model_runtime",
            trigger="tool schema failure",
            action="focused retry",
            expected_effect="parseable output",
            applicability=json.dumps({"failure_kinds": ["tool_schema_failed"]}),
        )
        run_without_trigger = AgentRun(
            city_id=2,
            status="completed",
            metrics=json.dumps({"model_recovery_history": []}),
        )
        self.db.add_all([lesson, run_without_trigger])
        self.db.flush()
        use_without_trigger = AgentKnowledgeUse(
            lesson_id=lesson.id,
            run_id=run_without_trigger.id,
        )
        self.db.add(use_without_trigger)
        self.db.commit()

        evaluate_knowledge_uses(
            self.db,
            run_id=run_without_trigger.id,
            material_change_count=0,
        )
        self.assertEqual(use_without_trigger.outcome, "not_triggered")

        run_with_trigger = AgentRun(
            city_id=2,
            status="partial",
            metrics=json.dumps({
                "model_recovery_history": [{
                    "failure_kind": "tool_schema_failed",
                    "outcome": "recovered",
                }],
            }),
        )
        self.db.add(run_with_trigger)
        self.db.flush()
        use_with_trigger = AgentKnowledgeUse(lesson_id=lesson.id, run_id=run_with_trigger.id)
        self.db.add(use_with_trigger)
        self.db.commit()

        evaluate_knowledge_uses(
            self.db,
            run_id=run_with_trigger.id,
            material_change_count=0,
        )
        self.assertEqual(use_with_trigger.outcome, "recovery_succeeded")

    def test_model_parse_recovery_restricts_tools_and_escalates_strategy(self) -> None:
        self.assertEqual(
            _model_output_failure_kind("code='output_parse_failed': Parsing failed"),
            "output_parse_failed",
        )
        mission = type("Mission", (), {"kind": "quality_images", "strategy": "{}"})()
        work = type("Work", (), {"next_action": json.dumps({"tool": "get_place"})})()
        focused = _model_recovery_plan(
            failure_kind="output_parse_failed", attempt=1, model="test", mission=mission,
            work_item=work, prompt_chars=90_000,
        )
        minimal = _model_recovery_plan(
            failure_kind="output_parse_failed", attempt=3, model="test", mission=mission,
            work_item=work, prompt_chars=90_000,
        )
        self.assertEqual(focused["reasoning_effort"], "medium")
        self.assertEqual(minimal["reasoning_effort"], "low")
        self.assertTrue(minimal["force_compaction"])
        self.assertLessEqual(len(minimal["tool_names"]), 4)
        self.assertIn("get_place", minimal["tool_names"])

        mission.strategy = json.dumps({"recovery_history": [{
            "failure_kind": "output_parse_failed",
            "strategy": {"mode": "focused_retry"},
            "outcome": "failed",
        }]})
        learned = _model_recovery_plan(
            failure_kind="output_parse_failed", attempt=1, model="test", mission=mission,
            work_item=work, prompt_chars=90_000,
        )
        self.assertEqual(learned["mode"], "compact_retry")
        self.assertTrue(learned["adapted_from_history"])

    def test_data_integrity_tool_scope_contains_no_place_mutations(self) -> None:
        expected = {
            "get_place",
            "web_search",
            "fetch_page",
            "geocode_place",
            "upsert_agent_task",
        }
        forbidden = {
            "verify_place",
            "update_place_fields",
            "update_place_context",
            "upsert_place_insights",
            "create_place",
            "propose_place",
            "merge_places",
            "assign_place_zone",
            "assign_place_chain",
        }

        self.assertEqual(set(DATA_INTEGRITY_TOOLS), expected)
        self.assertEqual(set(RECOVERY_TOOLS_BY_TASK["data_integrity"]), expected)
        self.assertTrue(expected.isdisjoint(forbidden))

        mission = type("Mission", (), {"kind": "data_integrity", "strategy": "{}"})()
        work = type("Work", (), {"next_action": json.dumps({"tool": "get_place"})})()
        recovery = _model_recovery_plan(
            failure_kind="output_parse_failed",
            attempt=1,
            model="test",
            mission=mission,
            work_item=work,
            prompt_chars=90_000,
        )
        self.assertTrue(set(recovery["tool_names"]).issubset(expected))
        self.assertTrue(set(recovery["tool_names"]).isdisjoint(forbidden))

    def test_data_integrity_task_result_requires_exact_active_task_id(self) -> None:
        mission = AgentMission(city_id=2, task_id=41, kind="data_integrity")
        self.assertIsNone(
            _active_agent_task_mismatch(
                "upsert_agent_task", {"task_id": 41}, mission
            )
        )
        for args in ({}, {"task_id": None}, {"task_id": 42}, {"task_id": "41"}):
            mismatch = _active_agent_task_mismatch(
                "upsert_agent_task", args, mission
            )
            self.assertEqual(mismatch["error"], "active_agent_task_mismatch")
        self.assertIsNone(
            _active_agent_task_mismatch("get_place", {"place_id": 42}, mission)
        )

    def test_data_integrity_task_result_projection_allows_only_terminal_verdict(self) -> None:
        mission = AgentMission(city_id=2, task_id=41, kind="data_integrity")
        task = AgentTask(id=41, city_id=2, kind="data_integrity")
        projected, error = _project_data_integrity_task_result_args(
            "upsert_agent_task",
            {
                "task_id": 41,
                "kind": "quality_information",
                "title": "hijacked title",
                "detail": "hijacked detail",
                "success_metric": "hijacked metric",
                "priority": 1,
                "status": "completed",
                "result": "audit verdict",
            },
            mission,
            task,
        )
        self.assertIsNone(error)
        self.assertEqual(projected, {
            "task_id": 41,
            "status": "completed",
            "result": "audit verdict",
        })
        for status in ("pending", "active", "", None):
            _, status_error = _project_data_integrity_task_result_args(
                "upsert_agent_task",
                {"task_id": 41, "status": status, "result": "not terminal"},
                mission,
                task,
            )
            self.assertEqual(
                status_error["error"],
                "invalid_data_integrity_task_status",
            )
        _, missing_error = _project_data_integrity_task_result_args(
            "upsert_agent_task",
            {"task_id": 41, "status": "completed", "result": "verdict"},
            mission,
            None,
        )
        self.assertEqual(missing_error["error"], "active_agent_task_not_writable")

    def test_data_integrity_corrective_tool_schema_matches_projected_terminal_write(self) -> None:
        tools = _data_integrity_corrective_tools(
            task_id=41,
            evidence_refs=["checkpoint:7", "evidence:9"],
        )
        self.assertEqual(len(tools), 1)
        function = tools[0]["function"]
        self.assertEqual(function["name"], "upsert_agent_task")
        parameters = function["parameters"]
        self.assertEqual(
            parameters["required"],
            [
                "task_id", "status", "verdict", "reason",
                "marker_changes", "evidence_refs",
            ],
        )
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(
            set(parameters["properties"]),
            {
                "task_id", "status", "verdict", "reason",
                "marker_changes", "evidence_refs",
            },
        )
        self.assertEqual(parameters["properties"]["task_id"]["type"], "integer")
        self.assertEqual(parameters["properties"]["task_id"]["enum"], [41])
        self.assertEqual(parameters["properties"]["status"]["enum"], ["completed"])
        self.assertEqual(parameters["properties"]["verdict"]["enum"], ["unresolved"])
        self.assertNotIn("minLength", parameters["properties"]["reason"])
        self.assertNotIn(
            "uniqueItems", parameters["properties"]["evidence_refs"]
        )
        self.assertEqual(
            parameters["properties"]["evidence_refs"]["items"]["enum"],
            ["checkpoint:7", "evidence:9"],
        )
        _, invalid = _project_structured_integrity_result(
            {
                "task_id": 41,
                "status": "completed",
                "verdict": "conflict",
                "reason": "checkpoint comparison conflicts",
                "marker_changes": 0,
                "evidence_refs": ["checkpoint:7"],
            },
            allowed_refs={
                "checkpoint:7": "target_observed",
                "evidence:9": "validated|Middle Street hotel official address",
            },
        )
        self.assertEqual(invalid["error"], "invalid_data_integrity_task_result")
        _, related_conflict_error = _project_structured_integrity_result(
            {
                "task_id": 41,
                "status": "completed",
                "verdict": "conflict",
                "reason": "Middle Street hotel has a conflicting official address",
                "marker_changes": 0,
                "evidence_refs": ["evidence:9"],
            },
            allowed_refs={
                "checkpoint:7": "target_observed",
                "evidence:9": "validated|Middle Street hotel official address",
            },
        )
        self.assertEqual(
            related_conflict_error["error"],
            "invalid_data_integrity_task_result",
        )
        projected, error = _project_structured_integrity_result(
            {
                "task_id": 41,
                "status": "completed",
                "verdict": "unresolved",
                "reason": "Middle Street hotel evidence remains inconclusive",
                "marker_changes": 0,
                "evidence_refs": ["checkpoint:7", "evidence:9", "evidence:9"],
            },
            allowed_refs={
                "checkpoint:7": "target_observed",
                "evidence:9": "validated|Middle Street hotel official address",
            },
        )
        self.assertIsNone(error)
        self.assertEqual(projected["status"], "completed")
        self.assertIn("verdict=unresolved", projected["result"])
        self.assertEqual(projected["result"].count("evidence:9"), 1)
        _, unrelated = _project_structured_integrity_result(
            {
                "task_id": 41,
                "status": "completed",
                "verdict": "conflict",
                "reason": "Middle Street hotel has a conflicting official address",
                "marker_changes": 0,
                "evidence_refs": ["evidence:10"],
            },
            allowed_refs={
                "evidence:10": "validated|Beiling Park opening hours and tickets",
            },
        )
        self.assertEqual(unrelated["error"], "invalid_data_integrity_task_result")
        _, korean_error = _project_structured_integrity_result(
            {
                "task_id": 41,
                "status": "completed",
                "verdict": "confirmed",
                "reason": "중제 만신호텔 지점 주소가 공식 자료와 일치합니다",
                "marker_changes": 0,
                "evidence_refs": ["evidence:11"],
            },
            allowed_refs={
                "evidence:11": "validated|중제 만신호텔 공식 지점 주소",
            },
        )
        self.assertEqual(korean_error["error"], "invalid_data_integrity_task_result")

    def test_integrity_evidence_refs_are_owned_bounded_and_exclude_policy_steps(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="owned evidence refs",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="cite only this work item's observations",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        run = AgentRun(
            city_id=2,
            mission_id=mission.id,
            work_item_id=item.id,
            status="running",
        )
        self.db.add(run)
        self.db.flush()
        observed_step = AgentRunStep(
            run_id=run.id,
            sequence=1,
            phase="observe",
            tool="get_place",
            outcome="ok",
            detail=json.dumps({
                "args": {"place_id": place.id},
                "result": {"id": place.id, "title": place.title},
            }),
        )
        policy_step = AgentRunStep(
            run_id=run.id,
            sequence=2,
            phase="observe",
            tool="get_place",
            outcome="error",
            detail=json.dumps({"result": {"error": "duplicate_data_integrity_place_read"}}),
        )
        self.db.add_all([observed_step, policy_step])
        self.db.flush()
        observed_checkpoint = AgentCheckpoint(
            mission_id=mission.id,
            work_item_id=item.id,
            run_id=run.id,
            sequence=1,
            outcome="ok",
        )
        policy_checkpoint = AgentCheckpoint(
            mission_id=mission.id,
            work_item_id=item.id,
            run_id=run.id,
            sequence=2,
            outcome="error",
        )
        owned = AgentEvidence(
            city_id=2,
            mission_id=mission.id,
            work_item_id=item.id,
            place_id=place.id,
            run_id=run.id,
            source_type="fetch_page",
            url="https://example.test/hotel",
            title="Middle Street hotel",
            claim="official address",
            excerpt="Middle Street hotel official address",
            source_status="validated",
            confidence=0.9,
            fingerprint="a" * 64,
        )
        wrong_work_item = AgentEvidence(
            city_id=2,
            mission_id=mission.id,
            work_item_id=None,
            place_id=place.id,
            run_id=run.id,
            source_type="fetch_page",
            url="https://example.test/unowned",
            title="unowned",
            claim="must not be cited",
            excerpt="must not be cited",
            source_status="validated",
            confidence=0.9,
            fingerprint="b" * 64,
        )
        self.db.add_all([
            observed_checkpoint,
            policy_checkpoint,
            owned,
            wrong_work_item,
        ])
        self.db.commit()

        refs = _data_integrity_evidence_refs(self.db, mission, item)

        self.assertLessEqual(len(refs), 20)
        self.assertIn(f"checkpoint:{observed_checkpoint.id}", refs)
        self.assertEqual(
            refs[f"checkpoint:{observed_checkpoint.id}"], "target_observed"
        )
        self.assertNotIn(f"checkpoint:{policy_checkpoint.id}", refs)
        self.assertIn(f"evidence:{owned.id}", refs)
        self.assertNotIn(f"evidence:{wrong_work_item.id}", refs)
        self.assertTrue(refs[f"evidence:{owned.id}"].startswith("validated|"))

    def test_data_integrity_runtime_rejects_hallucinated_write_tool(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        comparison = Marker(
            city_id=2,
            category=MarkerCategory.restaurant,
            shape=MarkerShape.point,
            title="비교 대상 #101",
            description="무결성 감사에서 읽기만 할 비교 장소",
            lat=41.81,
            lng=123.36,
        )
        self.db.add(comparison)
        self.db.flush()
        original_verified_at = place.last_verified_at
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title=f"[integrity:test:#{place.id}] read-only audit",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 좌표 근거 확인 필요)",
            success_metric="장소 원본 변경 없이 과제 result에 판정 기록",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        requests = []

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                if len(requests) == 1:
                    call = SimpleNamespace(
                        id="read-active-target",
                        function=SimpleNamespace(
                            name="get_place",
                            arguments=json.dumps({"place_id": place.id}),
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                if len(requests) == 2:
                    call = SimpleNamespace(
                        id="read-foreign-target",
                        function=SimpleNamespace(
                            name="get_place",
                            arguments=json.dumps({"place_id": comparison.id}),
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                if len(requests) == 3:
                    # Simulate a provider returning a tool that was not in the
                    # advertised schema. The runner must still reject it.
                    call = SimpleNamespace(
                        id="forbidden-write",
                        function=SimpleNamespace(
                            name="verify_place",
                            arguments=json.dumps({
                                "place_id": place.id,
                                "status": "uncertain",
                                "note": "should never execute",
                            }),
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                if len(requests) == 4:
                    call = SimpleNamespace(
                        id="record-audit",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps({
                                "task_id": task.id,
                                "kind": "data_integrity",
                                "title": task.title,
                                "status": "completed",
                                "result": (
                                    "verdict=unresolved; marker_changes=0; "
                                    f"observed_facts=active place #{place.id} read"
                                ),
                            }),
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                if len(requests) == 5:
                    call = SimpleNamespace(
                        id="record-structured-audit",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps(_structured_integrity_args(
                                kwargs,
                                task_id=task.id,
                                reason=(
                                    f"Active place #{place.id} was read before "
                                    "the foreign-target and forbidden-write attempts"
                                ),
                            )),
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="감사 결과를 과제에 기록했습니다.", tool_calls=[],
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=5,
                autonomous_research=True,
            )

        self.assertTrue(result["ok"], result)
        self.db.refresh(place)
        self.db.refresh(task)
        self.assertEqual(place.last_verified_at, original_verified_at)
        self.assertEqual(task.status, "completed")
        self.assertIn("verdict=unresolved", task.result)
        advertised = [
            {tool["function"]["name"] for tool in request.get("tools", [])}
            for request in requests
            if request.get("tools")
        ]
        self.assertTrue(advertised)
        self.assertTrue(
            all(
                tool_names in (set(DATA_INTEGRITY_TOOLS), {"upsert_agent_task"})
                for tool_names in advertised
            )
        )
        self.assertEqual(advertised[-1], {"upsert_agent_task"})
        place_steps = (
            self.db.query(AgentRunStep)
            .filter(AgentRunStep.tool == "get_place")
            .order_by(AgentRunStep.id)
            .all()
        )
        self.assertEqual([step.outcome for step in place_steps], ["ok", "error"])
        self.assertEqual(
            json.loads(place_steps[0].detail)["result"]["id"], place.id
        )
        foreign_result = json.loads(place_steps[1].detail)["result"]
        self.assertEqual(foreign_result["error"], "active_work_item_mismatch")
        self.assertEqual(foreign_result["active_place_id"], place.id)
        self.assertEqual(foreign_result["requested_place_id"], comparison.id)
        forbidden_step = (
            self.db.query(AgentRunStep)
            .filter(AgentRunStep.tool == "verify_place")
            .order_by(AgentRunStep.id.desc())
            .first()
        )
        self.assertIsNotNone(forbidden_step)
        self.assertEqual(forbidden_step.outcome, "error")
        step_detail = json.loads(forbidden_step.detail)
        self.assertEqual(
            step_detail["result"]["error"],
            "tool_not_allowed_for_data_integrity",
        )

    def test_data_integrity_requires_active_target_read_before_terminal_phase(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="target observation is mandatory",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="cite a successful get_place observation for this target",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()

        result, requests = self._run_scripted_agent(
            [
                ("upsert_agent_task", {
                    "task_id": task.id,
                    "status": "completed",
                    "result": "premature completion before target observation",
                }),
                ("get_place", {"place_id": place.id}),
                ("upsert_agent_task", {
                    "task_id": task.id,
                    "status": "completed",
                    "result": "request structured close after target observation",
                }),
                ("upsert_agent_task", {
                    "task_id": task.id,
                    "status": "completed",
                    "result": (
                        "verdict=unresolved; marker_changes=0; "
                        f"observed_facts=active place #{place.id} was read"
                    ),
                }),
            ],
            max_steps=5,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(requests), 4)
        guard_steps = (
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool == "upsert_agent_task",
                AgentRunStep.outcome == "error",
            )
            .order_by(AgentRunStep.sequence)
            .all()
        )
        self.assertEqual(len(guard_steps), 2)
        first_guard = json.loads(guard_steps[0].detail)["result"]
        second_guard = json.loads(guard_steps[1].detail)["result"]
        self.assertEqual(first_guard["error"], "structured_integrity_verdict_required")
        self.assertEqual(first_guard["guard_disposition"], "retry")
        self.assertEqual(first_guard["allowed_evidence_refs"], [])
        self.assertEqual(second_guard["error"], "structured_integrity_verdict_required")
        self.assertEqual(second_guard["guard_disposition"], "decide")
        self.assertTrue(
            any(ref.startswith("checkpoint:") for ref in second_guard["allowed_evidence_refs"])
        )
        self.assertEqual(requests[-1]["tool_choice"], "required")
        self.assertEqual(
            {tool["function"]["name"] for tool in requests[-1]["tools"]},
            {"upsert_agent_task"},
        )
        self.db.refresh(task)
        self.assertEqual(task.status, "completed")
        self.assertIn("evidence_refs=[", task.result)

    def test_data_integrity_blocks_missing_and_foreign_task_writes_then_completes_own(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        active_task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="active integrity task",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="record the audit result on this task only",
            priority=100,
        )
        foreign_task = AgentTask(
            city_id=2,
            kind="research",
            title="unrelated task",
            status="pending",
            priority=1,
        )
        self.db.add_all([active_task, foreign_task])
        self.db.commit()

        result, requests = self._run_scripted_agent(
            [
                ("upsert_agent_task", {
                    "status": "completed",
                    "title": "must not be created",
                    "result": "missing task id",
                }),
                ("upsert_agent_task", {
                    "task_id": foreign_task.id,
                    "status": "completed",
                    "result": "must not edit another task",
                }),
                ("get_place", {"place_id": place.id}),
                ("upsert_agent_task", {
                    "task_id": active_task.id,
                    "status": "completed",
                    "result": (
                        "verdict=unresolved; marker_changes=0; "
                        f"observed_facts=place #{place.id} read successfully; sources=none"
                    ),
                }),
            ],
            max_steps=5,
        )

        self.assertTrue(result["ok"], result)
        self.assertLessEqual(len(requests), 5)
        self.db.refresh(active_task)
        self.db.refresh(foreign_task)
        self.assertEqual(active_task.status, "completed")
        self.assertIn("verdict=unresolved", active_task.result)
        self.assertIn("evidence_refs=[", active_task.result)
        self.assertIn(f"place #{place.id} read successfully", active_task.result)
        self.assertEqual(foreign_task.status, "pending")
        self.assertEqual(
            self.db.query(AgentTask).filter(AgentTask.title == "must not be created").count(),
            0,
        )
        steps = (
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool == "upsert_agent_task",
            )
            .order_by(AgentRunStep.sequence)
            .all()
        )
        self.assertEqual(
            [step.outcome for step in steps],
            ["error", "error", "error", "ok"],
        )
        self.assertEqual(
            [json.loads(step.detail)["result"].get("error") for step in steps],
            [
                "active_agent_task_mismatch",
                "active_agent_task_mismatch",
                "structured_integrity_verdict_required",
                None,
            ],
        )

    def test_retry_guard_corrects_action_before_evidence_backed_completion(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="retry then decide",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="do not complete from a task-id typo alone",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()

        result, requests = self._run_scripted_agent(
            [
                ("upsert_agent_task", {
                    "status": "completed",
                    "result": (
                        "verdict=unresolved; marker_changes=0; "
                        "observed_facts=none; sources=none"
                    ),
                }),
                ("get_place", {"place_id": place.id}),
                ("upsert_agent_task", {
                    "task_id": task.id,
                    "status": "completed",
                    "result": (
                        "verdict=unresolved; marker_changes=0; "
                        f"observed_facts=place #{place.id} identity read successfully"
                    ),
                }),
                ("upsert_agent_task", {
                    "task_id": task.id,
                    "status": "completed",
                    "result": (
                        "verdict=unresolved; marker_changes=0; "
                        f"observed_facts=place #{place.id} identity read successfully"
                    ),
                }),
            ],
            max_steps=6,
        )

        self.assertTrue(result["ok"], result)
        self.assertLessEqual(len(requests), 6)
        self.assertTrue(
            all(
                request["tool_choice"] == "auto"
                for request in requests[:3]
            )
        )
        self.assertTrue(
            all(
                {tool["function"]["name"] for tool in request["tools"]}
                in (set(DATA_INTEGRITY_TOOLS), {"upsert_agent_task"})
                for request in requests
            )
        )
        self.db.refresh(task)
        self.assertEqual(task.status, "pending")
        mission = self.db.query(AgentMission).filter(AgentMission.task_id == task.id).one()
        item = self.db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission.id).one()
        self.assertEqual(json.loads(item.failed_approaches), [])

    def test_completed_integrity_result_skips_parallel_and_later_blocked_overwrite(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="completed verdict is terminal",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="preserve the first evidence-backed terminal verdict",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        final_reason = (
            f"Place #{place.id} was observed and the terminal result must not be overwritten"
        )
        requests: list[dict] = []

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                if len(requests) == 1:
                    calls = [SimpleNamespace(
                        id="terminal-read",
                        function=SimpleNamespace(
                            name="get_place",
                            arguments=json.dumps({"place_id": place.id}),
                        ),
                    )]
                elif len(requests) == 2:
                    calls = [SimpleNamespace(
                        id="duplicate-terminal-read",
                        function=SimpleNamespace(
                            name="get_place",
                            arguments=json.dumps({"place_id": place.id}),
                        ),
                    )]
                elif len(requests) == 3:
                    calls = [
                        SimpleNamespace(
                            id="terminal-completed",
                            function=SimpleNamespace(
                                name="upsert_agent_task",
                                arguments=json.dumps(_structured_integrity_args(
                                    kwargs,
                                    task_id=task.id,
                                    reason=final_reason,
                                )),
                            ),
                        ),
                        SimpleNamespace(
                            id="parallel-blocked-overwrite",
                            function=SimpleNamespace(
                                name="upsert_agent_task",
                                arguments=json.dumps({
                                    "task_id": task.id,
                                    "status": "blocked",
                                    "result": "must not overwrite completed verdict",
                                }),
                            ),
                        ),
                    ]
                else:
                    # This later overwrite is a second safety net: terminal
                    # completion must prevent the provider from being called
                    # for another round at all.
                    calls = [SimpleNamespace(
                        id="later-blocked-overwrite",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps({
                                "task_id": task.id,
                                "status": "blocked",
                                "result": "must never run",
                            }),
                        ),
                    )]
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=calls,
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=5,
                autonomous_research=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(requests), 3)
        self.db.refresh(task)
        self.assertEqual(task.status, "completed")
        self.assertIn("verdict=unresolved", task.result)
        self.assertIn(final_reason, task.result)
        self.assertNotIn("must not overwrite", task.result.split("reason=", 1)[-1].replace(final_reason, ""))
        mission = self.db.query(AgentMission).filter(AgentMission.task_id == task.id).one()
        item = self.db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission.id).one()
        self.assertEqual(mission.status, "completed")
        self.assertEqual(item.status, "done")
        writes = (
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool == "upsert_agent_task",
            )
            .all()
        )
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].outcome, "ok")

    def test_legacy_corrective_schema_recovery_keeps_upsert_only_contract(self) -> None:
        """Reproduce run 58: old decide cursor + provider tool-schema failure."""

        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="legacy run 56 decide cursor",
            detail=(
                f"Target:\n- #{place.id} {place.title}\n"
                "Finish with obsolete fields: qunar_identity, ctrip_identity, "
                "branch_comparison, conflicting_fields, sources, "
                "recommended_remediation. LEGACY_OUTPUT_FIELD_TRAP"
            ),
            success_metric=(
                "Return qunar_identity and ctrip_identity in separate fields. "
                "LEGACY_SUCCESS_METRIC_TRAP"
            ),
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        observed, _ = self._run_scripted_agent(
            [("get_place", {"place_id": place.id})],
            max_steps=1,
        )
        self.assertTrue(observed["ok"], observed)
        mission = self.db.query(AgentMission).filter(
            AgentMission.task_id == task.id
        ).one()
        item = self.db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id
        ).one()
        legacy_action = {
            "tool": "upsert_agent_task",
            "args": {
                "task_id": task.id,
                "status": "completed",
                "result": (
                    "verdict=unresolved; policy_guard=duplicate_data_integrity_place_read; "
                    "guard_disposition=decide; marker_changes=0; evidence_refs=<server-owned refs>"
                ),
            },
            "purpose": "legacy production corrective cursor",
        }
        item.stage = "decide"
        item.status = "active"
        item.next_action = json.dumps(legacy_action)
        progress = json.loads(mission.progress or "{}")
        progress["next_action"] = legacy_action
        mission.progress = json.dumps(progress)
        self.db.commit()
        requests: list[dict] = []

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                if len(requests) == 1:
                    # Groq run 58 rejected a get_place generation because only
                    # the structured terminal tool was advertised.
                    raise RuntimeError(
                        "Error code: 400 - tool_use_failed: additionalProperties "
                        "qunar_identity, ctrip_identity, branch_comparison, "
                        "conflicting_fields, sources, recommended_remediation not allowed"
                    )
                call = SimpleNamespace(
                    id="recovered-structured-close",
                    function=SimpleNamespace(
                        name="upsert_agent_task",
                        arguments=json.dumps(_structured_integrity_args(
                            kwargs,
                            task_id=task.id,
                            reason=(
                                f"Exact active place #{place.id} was already observed "
                                "in the cited server checkpoint"
                            ),
                        )),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            completed = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        self.assertTrue(completed["ok"], completed)
        self.assertEqual(completed["model_recovery_attempts"], 1)
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request["tool_choice"], "required")
            self.assertEqual(
                {tool["function"]["name"] for tool in request["tools"]},
                {"upsert_agent_task"},
            )
        initial_prompt = json.dumps(requests[0]["messages"], ensure_ascii=False)
        recovery_prompt = json.dumps(requests[1]["messages"], ensure_ascii=False)
        self.assertIn("already validated a successful get_place checkpoint", initial_prompt)
        self.assertIn("Do not call get_place", initial_prompt)
        self.assertIn("get_place나 조사 도구를 다시 호출하지 마세요", initial_prompt)
        self.assertIn("Do not call get_place or any research tool", recovery_prompt)
        for prompt in (initial_prompt, recovery_prompt):
            self.assertIn(
                "task_id, status, verdict, reason, marker_changes, evidence_refs",
                prompt,
            )
            self.assertNotIn("LEGACY_OUTPUT_FIELD_TRAP", prompt)
            self.assertNotIn("LEGACY_SUCCESS_METRIC_TRAP", prompt)
            for obsolete_field in (
                "qunar_identity", "ctrip_identity", "branch_comparison",
                "conflicting_fields", "recommended_remediation",
            ):
                self.assertNotIn(obsolete_field, prompt)
        recovery_steps = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == completed["run_id"],
            AgentRunStep.tool == "model_output",
        ).all()
        self.assertEqual(len(recovery_steps), 1)
        strategy = json.loads(recovery_steps[0].detail)["strategy"]
        self.assertEqual(strategy["tool_names"], ["upsert_agent_task"])
        self.assertTrue(strategy["corrective_result_only"])
        self.db.refresh(task)
        self.db.refresh(mission)
        self.db.refresh(item)
        self.assertEqual(task.status, "completed")
        self.assertEqual(mission.status, "completed")
        self.assertEqual(item.status, "done")

    def test_integrity_decide_phase_resumes_as_required_upsert_only_next_run(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="durable corrective phase",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="resume only the terminal result write",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()

        first, first_requests = self._run_scripted_agent(
            [
                ("get_place", {"place_id": place.id}),
                ("get_place", {"place_id": place.id}),
            ],
            max_steps=2,
        )

        self.assertTrue(first["ok"], first)
        self.assertEqual(len(first_requests), 2)
        self.db.refresh(task)
        self.assertEqual(task.status, "pending")
        mission = self.db.query(AgentMission).filter(AgentMission.task_id == task.id).one()
        item = self.db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission.id).one()
        action = json.loads(item.next_action)
        self.assertEqual(item.stage, "decide")
        self.assertEqual(action["phase"], "data_integrity_terminal_verdict_v1")
        self.assertEqual(action["tool"], "upsert_agent_task")
        self.assertEqual(action["task_id"], task.id)
        self.assertEqual(action["status"], "completed")
        self.assertEqual(action["guard_disposition"], "decide")
        self.assertNotIn("args", action)

        final_result = (
            "verdict=unresolved; marker_changes=0; "
            f"observed_facts=place #{place.id} was read before duplicate detection"
        )
        second, second_requests = self._run_scripted_agent(
            [("upsert_agent_task", {
                "task_id": task.id,
                "status": "completed",
                "result": final_result,
            })],
            max_steps=2,
        )

        self.assertTrue(second["ok"], second)
        self.assertGreaterEqual(len(second_requests), 1)
        first_resume_request = second_requests[0]
        self.assertEqual(first_resume_request["tool_choice"], "required")
        self.assertEqual(
            {
                tool["function"]["name"]
                for tool in first_resume_request["tools"]
            },
            {"upsert_agent_task"},
        )
        self.db.refresh(task)
        self.db.refresh(mission)
        self.db.refresh(item)
        self.assertEqual(task.status, "completed")
        self.assertIn("verdict=unresolved", task.result)
        self.assertIn("evidence_refs=[", task.result)
        self.assertEqual(mission.status, "completed")
        self.assertEqual(item.status, "done")
        self.assertEqual(item.stage, "complete")
        self.assertEqual(json.loads(item.next_action), {})

    def test_legacy_integrity_cursor_resumes_without_exposing_old_result_example(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="legacy production cursor",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="resume through the current structured provider schema",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        observed, _ = self._run_scripted_agent(
            [("get_place", {"place_id": place.id})],
            max_steps=1,
        )
        self.assertTrue(observed["ok"], observed)
        mission = self.db.query(AgentMission).filter(
            AgentMission.task_id == task.id
        ).one()
        item = self.db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id
        ).one()
        legacy_action = {
            "tool": "upsert_agent_task",
            "args": {
                "task_id": task.id,
                "status": "completed",
                "result": (
                    "verdict=unresolved; policy_guard=duplicate_data_integrity_place_read; "
                    "guard_disposition=decide; marker_changes=0; LEGACY_COPY_TRAP"
                ),
            },
        }
        item.stage = "decide"
        item.status = "active"
        item.next_action = json.dumps(legacy_action)
        task.result = "LEGACY_TASK_RESULT_TRAP result=<obsolete free-form payload>"
        progress = json.loads(mission.progress or "{}")
        progress["active_work_item_id"] = item.id
        progress["next_action"] = legacy_action
        mission.progress = json.dumps(progress)
        self.db.commit()

        completed, requests = self._run_scripted_agent(
            [("upsert_agent_task", {
                "task_id": task.id,
                "status": "completed",
                "result": (
                    "verdict=unresolved; marker_changes=0; "
                    f"observed_facts=active place #{place.id} was read"
                ),
            })],
            max_steps=1,
        )

        self.assertTrue(completed["ok"], completed)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["tool_choice"], "required")
        self.assertEqual(
            {tool["function"]["name"] for tool in request["tools"]},
            {"upsert_agent_task"},
        )
        parameters = request["tools"][0]["function"]["parameters"]
        self.assertNotIn("result", parameters["properties"])
        self.assertEqual(parameters["properties"]["verdict"]["enum"], ["unresolved"])
        self.assertNotIn(
            "LEGACY_COPY_TRAP",
            json.dumps(request["messages"], ensure_ascii=False),
        )
        self.assertNotIn(
            "LEGACY_TASK_RESULT_TRAP",
            json.dumps(request["messages"], ensure_ascii=False),
        )
        self.db.refresh(task)
        self.db.refresh(mission)
        self.db.refresh(item)
        self.assertEqual(task.status, "completed")
        self.assertEqual(mission.status, "completed")
        self.assertEqual(item.status, "done")

    def test_resumed_corrective_malformed_json_retries_before_status_guard(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="corrective malformed retry",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="recover malformed terminal tool arguments",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        first, _ = self._run_scripted_agent(
            [
                ("get_place", {"place_id": place.id}),
                ("get_place", {"place_id": place.id}),
            ],
            max_steps=2,
        )
        self.assertTrue(first["ok"], first)
        requests: list[dict] = []
        final_result = (
            "verdict=unresolved; marker_changes=0; "
            f"observed_facts=place #{place.id} was read before duplicate detection"
        )

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                if len(requests) == 1:
                    call = SimpleNamespace(
                        id="malformed-corrective",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments="{",
                        ),
                    )
                else:
                    call = SimpleNamespace(
                        id="recovered-corrective",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps(_structured_integrity_args(
                                kwargs,
                                task_id=task.id,
                                reason=final_result,
                            )),
                        ),
                    )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            second = run_agent(
                self.db,
                city_id=2,
                max_steps=2,
                autonomous_research=True,
            )

        self.assertTrue(second["ok"], second)
        self.assertEqual(len(requests), 2)
        self.assertTrue(
            all(request["tool_choice"] == "required" for request in requests)
        )
        malformed_steps = (
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == second["run_id"],
                AgentRunStep.tool == "upsert_agent_task",
                AgentRunStep.outcome == "error",
            )
            .all()
        )
        malformed_step = next(
            step for step in malformed_steps
            if json.loads(step.detail)["result"].get("error")
            == "malformed_tool_arguments"
        )
        malformed_result = json.loads(malformed_step.detail)["result"]
        self.assertEqual(malformed_result["error"], "malformed_tool_arguments")
        self.db.refresh(task)
        self.assertEqual(task.status, "completed")
        self.assertIn("verdict=unresolved", task.result)
        self.assertIn("evidence_refs=[", task.result)

    def test_invalid_corrective_ref_repins_required_upsert_across_runs(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="durable invalid evidence correction",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="close only with a server-owned evidence reference",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        first, _ = self._run_scripted_agent(
            [
                ("get_place", {"place_id": place.id}),
                ("get_place", {"place_id": place.id}),
            ],
            max_steps=2,
        )
        self.assertTrue(first["ok"], first)

        invalid_requests: list[dict] = []

        class InvalidRefCompletions:
            def create(inner_self, **kwargs):
                invalid_requests.append(kwargs)
                call = SimpleNamespace(
                    id="invalid-server-ref",
                    function=SimpleNamespace(
                        name="upsert_agent_task",
                        arguments=json.dumps({
                            "task_id": task.id,
                            "status": "completed",
                            "verdict": "unresolved",
                            "reason": "The observation exists but this reference is not owned",
                            "marker_changes": 0,
                            "evidence_refs": ["checkpoint:999999"],
                        }),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=InvalidRefCompletions())
        )
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            second = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        self.assertTrue(second["ok"], second)
        self.assertEqual(len(invalid_requests), 1)
        self.assertEqual(invalid_requests[0]["tool_choice"], "required")
        self.assertEqual(
            {tool["function"]["name"] for tool in invalid_requests[0]["tools"]},
            {"upsert_agent_task"},
        )
        self.db.refresh(task)
        self.assertEqual(task.status, "pending")
        mission = self.db.query(AgentMission).filter(
            AgentMission.task_id == task.id
        ).one()
        item = self.db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id
        ).one()
        self.assertEqual(item.status, "active")
        self.assertEqual(item.stage, "decide")
        action = json.loads(item.next_action)
        self.assertEqual(action["phase"], "data_integrity_terminal_verdict_v1")
        self.assertEqual(action["tool"], "upsert_agent_task")
        self.assertEqual(action["task_id"], task.id)
        self.assertEqual(action["status"], "completed")
        self.assertEqual(action["guard_disposition"], "decide")
        self.assertNotIn("args", action)
        progress = json.loads(mission.progress)
        self.assertEqual(progress["next_action"]["tool"], "upsert_agent_task")
        failed_step = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == second["run_id"],
            AgentRunStep.outcome == "error",
        ).one()
        self.assertEqual(
            json.loads(failed_step.detail)["result"]["error"],
            "invalid_data_integrity_task_result",
        )

        final_result = (
            "verdict=unresolved; marker_changes=0; "
            f"observed_facts=place #{place.id} was read before duplicate detection"
        )
        third, final_requests = self._run_scripted_agent(
            [("upsert_agent_task", {
                "task_id": task.id,
                "status": "completed",
                "result": final_result,
            })],
            max_steps=1,
        )
        self.assertTrue(third["ok"], third)
        self.assertEqual(final_requests[0]["tool_choice"], "required")
        self.assertEqual(
            {tool["function"]["name"] for tool in final_requests[0]["tools"]},
            {"upsert_agent_task"},
        )
        self.db.refresh(task)
        self.db.refresh(mission)
        self.db.refresh(item)
        self.assertEqual(task.status, "completed")
        self.assertIn("verdict=unresolved", task.result)
        self.assertEqual(mission.status, "completed")
        self.assertEqual(item.status, "done")

    def test_integrity_completion_rolls_back_task_when_checkpoint_fails(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="atomic integrity completion",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="task and durable cursor commit together",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        first, _ = self._run_scripted_agent(
            [
                ("get_place", {"place_id": place.id}),
                ("get_place", {"place_id": place.id}),
            ],
            max_steps=2,
        )
        self.assertTrue(first["ok"], first)
        self.db.refresh(task)
        result_before = task.result
        requests: list[dict] = []

        class Completion:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                call = SimpleNamespace(
                    id="atomic-terminal-write",
                    function=SimpleNamespace(
                        name="upsert_agent_task",
                        arguments=json.dumps(_structured_integrity_args(
                            kwargs,
                            task_id=task.id,
                            reason="The owned checkpoint supports an unresolved audit result",
                        )),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completion()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch(
                "app.agent.runner.checkpoint_after_tool",
                side_effect=RuntimeError("forced checkpoint failure"),
            ),
        ):
            failed = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        self.assertFalse(failed["ok"], failed)
        self.assertIn("forced checkpoint failure", failed["message"])
        self.db.expire_all()
        persisted_task = self.db.get(AgentTask, task.id)
        self.assertEqual(persisted_task.status, "pending")
        self.assertEqual(persisted_task.result, result_before)
        mission = self.db.query(AgentMission).filter(
            AgentMission.task_id == task.id
        ).one()
        item = self.db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id
        ).one()
        self.assertEqual(mission.status, "active")
        self.assertEqual(item.status, "active")
        self.assertEqual(item.stage, "decide")

        retry, _ = self._run_scripted_agent(
            [("upsert_agent_task", {
                "task_id": task.id,
                "status": "completed",
                "result": "verdict=unresolved; marker_changes=0; retry after rollback",
            })],
            max_steps=1,
        )
        self.assertTrue(retry["ok"], retry)
        self.db.refresh(persisted_task)
        self.db.refresh(mission)
        self.db.refresh(item)
        self.assertEqual(persisted_task.status, "completed")
        self.assertEqual(mission.status, "completed")
        self.assertEqual(item.status, "done")

    def test_legacy_completed_integrity_task_reconciles_without_model_or_overwrite(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="legacy split completion",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="repair a task-first legacy commit",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        original_result = "verdict=unresolved; immutable legacy terminal result"
        task.status = "completed"
        task.result = original_result
        task.completed_at = datetime.now(timezone.utc)
        item.stage = "decide"
        item.next_action = json.dumps({
            "tool": "upsert_agent_task",
            "args": {
                "task_id": task.id,
                "status": "completed",
                "result": "must not replay",
            },
        })
        self.db.commit()

        immutable = run_tool(
            self.db,
            "upsert_agent_task",
            {
                "task_id": task.id,
                "status": "completed",
                "result": "attempted overwrite",
            },
            city_id=2,
            server_defer_commit=True,
        )
        self.assertTrue(immutable["already_completed"])
        self.assertTrue(immutable["immutable"])
        self.assertEqual(immutable["result"], original_result)
        self.db.refresh(task)
        self.assertEqual(task.result, original_result)

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: self.fail("model must not be called")
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            repaired = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        self.assertTrue(repaired["ok"], repaired)
        self.assertEqual(repaired["steps"], 0)
        self.db.refresh(task)
        self.db.refresh(mission)
        self.db.refresh(item)
        self.assertEqual(task.result, original_result)
        self.assertEqual(mission.status, "completed")
        self.assertEqual(item.status, "done")
        self.assertEqual(item.stage, "complete")
        self.assertEqual(json.loads(item.next_action), {})

    def test_completed_integrity_result_is_immutable_across_stale_sessions(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "integrity-concurrency.db"
            engine = create_engine(f"sqlite:///{db_path.as_posix()}")
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(bind=engine)
            seed = session_factory()
            first = session_factory()
            stale = session_factory()
            check = session_factory()
            try:
                seed.add(City(
                    id=2,
                    slug="shenyang-concurrency",
                    name_ko="선양",
                    name_local="沈阳",
                    center_lat=41.8,
                    center_lng=123.4,
                    search_viewbox="123.0,42.0,123.8,41.5",
                ))
                seed.add(AgentTask(
                    id=36,
                    city_id=2,
                    kind="data_integrity",
                    title="stale session terminal audit",
                    status="pending",
                ))
                seed.commit()

                # Session B deliberately retains the old pending identity-map
                # snapshot while session A commits the terminal verdict.
                self.assertEqual(stale.get(AgentTask, 36).status, "pending")
                first_result = run_tool(
                    first,
                    "upsert_agent_task",
                    {"task_id": 36, "status": "completed", "result": "first verdict"},
                    city_id=2,
                )
                self.assertTrue(first_result["changed"])

                stale_result = run_tool(
                    stale,
                    "upsert_agent_task",
                    {"task_id": 36, "status": "completed", "result": "second overwrite"},
                    city_id=2,
                )
                self.assertTrue(stale_result["already_completed"])
                self.assertTrue(stale_result["immutable"])
                self.assertEqual(stale_result["result"], "first verdict")
                persisted = check.get(AgentTask, 36)
                self.assertEqual(persisted.status, "completed")
                self.assertEqual(persisted.result, "first verdict")
            finally:
                for session in (check, stale, first, seed):
                    session.close()
                engine.dispose()

    def test_malformed_corrective_last_step_repins_next_run(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="last-step malformed correction",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="retain correction cursor after malformed JSON",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        first, _ = self._run_scripted_agent(
            [
                ("get_place", {"place_id": place.id}),
                ("get_place", {"place_id": place.id}),
            ],
            max_steps=2,
        )
        self.assertTrue(first["ok"], first)

        class Malformed:
            def create(inner_self, **kwargs):
                call = SimpleNamespace(
                    id="last-step-malformed",
                    function=SimpleNamespace(
                        name="upsert_agent_task",
                        arguments="{",
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Malformed()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            malformed = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )
        self.assertTrue(malformed["ok"], malformed)
        mission = self.db.query(AgentMission).filter(
            AgentMission.task_id == task.id
        ).one()
        item = self.db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id
        ).one()
        self.assertEqual(item.status, "active")
        self.assertEqual(item.stage, "decide")
        self.assertEqual(json.loads(item.next_action)["tool"], "upsert_agent_task")

        final, requests = self._run_scripted_agent(
            [("upsert_agent_task", {
                "task_id": task.id,
                "status": "completed",
                "result": "verdict=unresolved; marker_changes=0; malformed retry",
            })],
            max_steps=1,
        )
        self.assertTrue(final["ok"], final)
        self.assertEqual(requests[0]["tool_choice"], "required")
        self.assertEqual(
            {tool["function"]["name"] for tool in requests[0]["tools"]},
            {"upsert_agent_task"},
        )

    def test_data_integrity_task_definition_cannot_be_reclassified_and_next_run_stays_clamped(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="immutable integrity definition",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="record a read-only verdict",
            priority=97,
        )
        self.db.add(task)
        self.db.commit()
        original_definition = (
            task.kind,
            task.title,
            task.detail,
            task.success_metric,
            task.priority,
        )

        first_result, _ = self._run_scripted_agent(
            [("upsert_agent_task", {
                "task_id": task.id,
                "kind": "quality_information",
                "title": "mutated title",
                "detail": "mutated detail",
                "success_metric": "mutated metric",
                "priority": 1,
                "status": "blocked",
                "result": "audit needs a new independent source",
            })],
            max_steps=2,
        )

        self.assertTrue(first_result["ok"], first_result)
        self.db.refresh(task)
        mission = self.db.query(AgentMission).filter(AgentMission.task_id == task.id).one()
        self.assertEqual(
            (
                task.kind,
                task.title,
                task.detail,
                task.success_metric,
                task.priority,
            ),
            original_definition,
        )
        self.assertEqual(task.status, "pending")
        self.assertNotEqual(task.result, "audit needs a new independent source")
        self.assertEqual(mission.kind, "data_integrity")

        second_result, requests = self._run_scripted_agent(
            [
                ("get_place", {"place_id": place.id}),
                ("upsert_agent_task", {
                    "task_id": task.id,
                    "status": "completed",
                    "result": (
                        "verdict=unresolved; marker_changes=0; "
                        f"observed_facts=place #{place.id} read successfully"
                    ),
                }),
            ],
            max_steps=3,
        )

        self.assertTrue(second_result["ok"], second_result)
        advertised = [
            {tool["function"]["name"] for tool in request.get("tools", [])}
            for request in requests
            if request.get("tools")
        ]
        self.assertTrue(advertised)
        self.assertTrue(
            all(
                tool_names in (set(DATA_INTEGRITY_TOOLS), {"upsert_agent_task"})
                for tool_names in advertised
            )
        )
        self.db.refresh(task)
        self.db.refresh(mission)
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.kind, "data_integrity")
        self.assertEqual(mission.kind, "data_integrity")

    def test_data_integrity_duplicate_place_read_is_blocked_before_loop_limit(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="deduplicate reads",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="reuse one observation and record the verdict",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()

        result, requests = self._run_scripted_agent(
            [
                ("get_place", {"place_id": place.id}),
                ("get_place", {"place_id": place.id}),
                ("upsert_agent_task", {
                    "task_id": task.id,
                    "status": "completed",
                    "result": (
                        "verdict=unresolved; marker_changes=0; "
                        f"observed_facts=place #{place.id} read once; sources=none"
                    ),
                }),
            ],
            max_steps=4,
        )

        self.assertTrue(result["ok"], result)
        self.assertLess(len(requests), 18)
        self.assertLess(result["steps"], 18)
        self.db.refresh(task)
        self.assertEqual(task.status, "completed")
        reads = (
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool == "get_place",
            )
            .order_by(AgentRunStep.sequence)
            .all()
        )
        self.assertEqual([step.outcome for step in reads], ["ok", "error"])
        duplicate = json.loads(reads[1].detail)["result"]
        self.assertEqual(duplicate["error"], "duplicate_data_integrity_place_read")
        self.assertEqual(duplicate["error_class"], "policy_guard")
        self.assertEqual(duplicate["guard_disposition"], "decide")
        self.assertIn(f"task_id={task.id}", duplicate["detail"])

    def test_recovered_schema_403_and_duplicate_guard_finish_without_three_path_rotation(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="run 54 recovery budget",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="finish with an honest unresolved verdict",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        requests: list[dict] = []

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                round_number = len(requests)
                if round_number == 1:
                    raise RuntimeError(
                        "Error code: 400 - output_parse_failed: Parsing failed"
                    )
                if round_number == 2:
                    name, args = "fetch_page", {"url": "https://blocked.test/place"}
                elif round_number in {3, 4}:
                    name, args = "get_place", {"place_id": place.id}
                elif round_number == 5:
                    name, args = "upsert_agent_task", _structured_integrity_args(
                        kwargs,
                        task_id=task.id,
                        reason=(
                            f"Place #{place.id} was read; one source returned HTTP 403"
                        ),
                    )
                else:
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="unresolved audit result recorded", tool_calls=[],
                    ))])
                call = SimpleNamespace(
                    id=f"recovery-{round_number}",
                    function=SimpleNamespace(name=name, arguments=json.dumps(args)),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        def run_tool_with_403(db, name, args, **kwargs):
            if name == "fetch_page":
                return {
                    "error": "fetch_failed",
                    "detail": "HTTP 403 forbidden",
                    "url": args.get("url"),
                }
            return run_tool(db, name, args, **kwargs)

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=run_tool_with_403),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=6,
                autonomous_research=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["model_recovery_attempts"], 1)
        self.assertEqual(len(requests), 5)
        self.assertEqual(requests[4]["tool_choice"], "required")
        corrective_tools = {
            tool["function"]["name"] for tool in requests[4].get("tools", [])
        }
        self.assertEqual(corrective_tools, {"upsert_agent_task"})
        corrective_parameters = requests[4]["tools"][0]["function"]["parameters"]
        self.assertEqual(
            corrective_parameters["required"],
            [
                "task_id", "status", "verdict", "reason",
                "marker_changes", "evidence_refs",
            ],
        )
        self.assertFalse(corrective_parameters["additionalProperties"])
        self.db.refresh(task)
        self.assertEqual(task.status, "completed")
        self.assertIn("verdict=unresolved", task.result)
        mission = self.db.query(AgentMission).filter(AgentMission.task_id == task.id).one()
        item = self.db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission.id).one()
        self.assertEqual(mission.status, "completed")
        self.assertEqual(item.status, "done")
        failures = json.loads(item.failed_approaches)
        self.assertEqual(len(failures), 1)
        self.assertIn("fetch_page: fetch_failed", failures[0])
        self.assertFalse(any("model_output:" in failure for failure in failures))
        self.assertFalse(
            any("duplicate_data_integrity_place_read" in failure for failure in failures)
        )
        duplicate_step = (
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool == "get_place",
                AgentRunStep.outcome == "error",
            )
            .one()
        )
        self.assertEqual(
            json.loads(duplicate_step.detail)["result"]["error"],
            "duplicate_data_integrity_place_read",
        )
        duplicate_checkpoint = (
            self.db.query(AgentCheckpoint)
            .filter(
                AgentCheckpoint.run_id == result["run_id"],
                AgentCheckpoint.sequence == duplicate_step.sequence,
            )
            .one()
        )
        self.assertEqual(json.loads(duplicate_checkpoint.rejected_claims), [])
        recovery_checkpoint = (
            self.db.query(AgentCheckpoint)
            .filter(
                AgentCheckpoint.run_id == result["run_id"],
                AgentCheckpoint.outcome == "recovery_retry",
            )
            .one()
        )
        self.assertTrue(
            any(
                failure.startswith("model_output:output_parse_failed:")
                for failure in json.loads(recovery_checkpoint.failed_approaches)
            )
        )

    def test_corrective_mode_survives_recovery_and_blocks_hallucinated_or_parallel_tools(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="hard corrective boundary",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="close with a grounded unresolved verdict",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        requests: list[dict] = []

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                round_number = len(requests)
                if round_number == 3:
                    raise RuntimeError(
                        "Error code: 400 - output_parse_failed: Parsing failed"
                    )
                if round_number == 1:
                    calls = [SimpleNamespace(
                        id="first-read",
                        function=SimpleNamespace(
                            name="get_place",
                            arguments=json.dumps({"place_id": place.id}),
                        ),
                    )]
                elif round_number == 2:
                    calls = [
                        SimpleNamespace(
                            id="duplicate-read",
                            function=SimpleNamespace(
                                name="get_place",
                                arguments=json.dumps({"place_id": place.id}),
                            ),
                        ),
                        SimpleNamespace(
                            id="parallel-write",
                            function=SimpleNamespace(
                                name="verify_place",
                                arguments=json.dumps({
                                    "place_id": place.id,
                                    "status": "valid",
                                    "note": "must be skipped",
                                }),
                            ),
                        ),
                    ]
                elif round_number == 4:
                    # Simulate a provider ignoring the one-tool corrective
                    # schema after recovery. Runtime enforcement must still win.
                    calls = [SimpleNamespace(
                        id="hallucinated-read",
                        function=SimpleNamespace(
                            name="get_place",
                            arguments=json.dumps({"place_id": place.id}),
                        ),
                    )]
                elif round_number == 5:
                    calls = [SimpleNamespace(
                        id="grounded-close",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps(_structured_integrity_args(
                                kwargs,
                                task_id=task.id,
                                reason=f"Place #{place.id} was read once before correction",
                            )),
                        ),
                    )]
                else:
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="done", tool_calls=[],
                    ))])
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=calls,
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=6,
                autonomous_research=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(requests), 5)
        for request in requests[2:]:
            self.assertEqual(request["tool_choice"], "required")
            self.assertEqual(
                {tool["function"]["name"] for tool in request["tools"]},
                {"upsert_agent_task"},
            )
        self.assertEqual(
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool == "verify_place",
            )
            .count(),
            0,
        )
        hallucinated = (
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool == "get_place",
                AgentRunStep.outcome == "error",
            )
            .order_by(AgentRunStep.sequence.desc())
            .first()
        )
        self.assertEqual(
            json.loads(hallucinated.detail)["result"]["error"],
            "tool_not_allowed_for_data_integrity",
        )
        mission = self.db.query(AgentMission).filter(AgentMission.task_id == task.id).one()
        item = self.db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission.id).one()
        self.db.refresh(task)
        self.assertEqual(task.status, "completed")
        self.assertEqual(mission.status, "completed")
        self.assertEqual(item.status, "done")
        self.assertEqual(json.loads(item.failed_approaches), [])

    def test_data_integrity_rejects_list_tools_outside_narrow_scope(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="bounded task lookup",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="task lookup is bounded",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        result, requests = self._run_scripted_agent(
            [
                ("list_agent_tasks", {"limit": 12}),
                ("list_places", {"q": place.title, "limit": 12}),
                ("get_place", {"place_id": place.id}),
                ("get_place", {"place_id": place.id}),
                ("upsert_agent_task", {
                    "task_id": task.id,
                    "status": "completed",
                    "result": "verdict=unresolved; marker_changes=0; target read",
                }),
            ],
            max_steps=6,
        )

        self.assertTrue(result["ok"], result)
        list_steps = (
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool.in_(("list_agent_tasks", "list_places")),
            )
            .order_by(AgentRunStep.sequence)
            .all()
        )
        self.assertEqual(len(list_steps), 2)
        self.assertTrue(all(step.outcome == "error" for step in list_steps))
        self.assertTrue(all(
            json.loads(step.detail)["result"]["error"]
            == "tool_not_allowed_for_data_integrity"
            for step in list_steps
        ))
        self.assertTrue(all(
            {tool["function"]["name"] for tool in request["tools"]}
            in (set(DATA_INTEGRITY_TOOLS), {"upsert_agent_task"})
            for request in requests
        ))
        self.db.refresh(task)
        self.assertEqual(task.status, "completed")

    def test_list_agent_tasks_server_pure_read_has_no_reconcile_or_commit_side_effect(self) -> None:
        legacy = AgentTask(
            city_id=2,
            kind="research",
            title="승인 제안: legacy candidate",
            detail="legacy pending task",
            status="pending",
            priority=5,
        )
        self.db.add(legacy)
        self.db.commit()

        with patch.object(self.db, "commit", wraps=self.db.commit) as commit:
            rows = run_tool(
                self.db,
                "list_agent_tasks",
                {"limit": 20},
                city_id=2,
                server_pure_read=True,
            )

        commit.assert_not_called()
        self.assertIn(legacy.id, [row["id"] for row in rows])
        self.assertEqual(legacy.title, "승인 제안: legacy candidate")
        self.assertEqual(legacy.kind, "research")
        self.assertEqual(legacy.status, "pending")

        # A model-controlled lookalike argument has no provenance. The ordinary
        # path must retain its reconciliation behavior and commit the change.
        with patch.object(self.db, "commit", wraps=self.db.commit) as commit:
            run_tool(
                self.db,
                "list_agent_tasks",
                {"limit": 20, "server_pure_read": True},
                city_id=2,
            )

        self.assertGreaterEqual(commit.call_count, 1)
        self.assertEqual(legacy.title, "후보 검증: legacy candidate")
        self.assertEqual(legacy.kind, "candidate_research")

    def test_list_agent_tasks_pure_read_does_not_autoflush_unrelated_pending_change(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = (Path(temp_dir) / "pure-read.sqlite3").as_posix()
            engine = create_engine(f"sqlite:///{database_path}")
            Base.metadata.create_all(engine)
            isolated_session = sessionmaker(bind=engine)
            writer = isolated_session()
            reader = isolated_session()
            try:
                unrelated = AgentTask(
                    city_id=2,
                    kind="research",
                    title="committed original title",
                    status="pending",
                    priority=5,
                )
                writer.add(unrelated)
                writer.commit()
                unrelated_id = unrelated.id
                unrelated.title = "uncommitted unrelated mutation"

                with (
                    patch.object(writer, "flush", wraps=writer.flush) as flush,
                    patch.object(writer, "commit", wraps=writer.commit) as commit,
                ):
                    run_tool(
                        writer,
                        "list_agent_tasks",
                        {"limit": 20},
                        city_id=2,
                        server_pure_read=True,
                    )

                flush.assert_not_called()
                commit.assert_not_called()
                persisted = reader.get(AgentTask, unrelated_id)
                self.assertEqual(persisted.title, "committed original title")
            finally:
                writer.rollback()
                writer.close()
                reader.close()
                engine.dispose()

    def test_data_integrity_list_agent_tasks_does_not_modify_other_pending_tasks(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        legacy = AgentTask(
            city_id=2,
            kind="research",
            title="승인 제안: must remain untouched",
            detail="another task outside the integrity audit",
            status="pending",
            priority=1,
        )
        task = AgentTask(
            city_id=2,
            kind="data_integrity",
            title="pure backlog inspection",
            detail=f"targets:\n- #{place.id} {place.title}",
            success_metric="read backlog without reconciling it",
            priority=100,
        )
        self.db.add_all([legacy, task])
        self.db.commit()

        result, _ = self._run_scripted_agent(
            [
                ("list_agent_tasks", {
                    "limit": 20,
                    # This JSON field is untrusted and irrelevant; purity comes
                    # only from the runner's server-only keyword provenance.
                    "server_pure_read": False,
                }),
                ("upsert_agent_task", {
                    "task_id": task.id,
                    "status": "completed",
                    "result": "backlog observed without side effects",
                }),
            ],
            max_steps=3,
        )

        self.assertTrue(result["ok"], result)
        self.db.refresh(legacy)
        self.assertEqual(legacy.title, "승인 제안: must remain untouched")
        self.assertEqual(legacy.kind, "research")
        self.assertEqual(legacy.status, "pending")

    def test_no_tool_no_progress_exit_pauses_and_checkpoints_before_next_run(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        sibling = Marker(
            city_id=2,
            category=MarkerCategory.restaurant,
            shape=MarkerShape.point,
            title="next integrity target",
            description="must wait for mission cooldown",
            lat=41.82,
            lng=123.37,
        )
        self.db.add(sibling)
        self.db.flush()
        task = AgentTask(
            city_id=2,
            kind="research",
            title="stall then pause",
            detail=(
                f"targets:\n- #{place.id} {place.title}\n"
                f"- #{sibling.id} {sibling.title}"
            ),
            success_metric="record a verdict",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        repeated_reads = [("get_place", {"place_id": place.id}) for _ in range(18)]

        result, requests = self._run_scripted_agent(repeated_reads, max_steps=20)

        self.assertEqual(result["steps"], 19)
        self.assertEqual(len(requests), 19)
        mission = self.db.query(AgentMission).filter(AgentMission.task_id == task.id).one()
        items = (
            self.db.query(AgentWorkItem)
            .filter(AgentWorkItem.mission_id == mission.id)
            .order_by(AgentWorkItem.priority.desc())
            .all()
        )
        self.assertEqual(mission.status, "paused")
        self.assertEqual([item.status for item in items], ["blocked", "ready"])
        self.assertIn("추가 행동 없이 종료", items[0].blocked_reason)
        self.assertEqual(items[1].attempts, 0)
        self.assertIsNone(items[1].last_run_id)
        self.assertEqual(json.loads(mission.progress)["resume_work_item_id"], items[1].id)
        checkpoint = (
            self.db.query(AgentCheckpoint)
            .filter(
                AgentCheckpoint.mission_id == mission.id,
                AgentCheckpoint.outcome == "blocked",
            )
            .order_by(AgentCheckpoint.id.desc())
            .first()
        )
        self.assertIsNotNone(checkpoint)
        self.assertIn("orchestrator_no_progress", checkpoint.state_summary)
        progress = json.loads(mission.progress)
        self.assertEqual(progress["last_checkpoint_sequence"], checkpoint.sequence)
        self.assertEqual(progress["last_outcome"], "blocked")
        attempts_before = task.attempts
        class DiscoveryCompletions:
            def create(inner_self, **_kwargs):
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="이번 발굴 조각은 다음 체크포인트로 인계", tool_calls=[],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=DiscoveryCompletions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
        ):
            next_result = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )
        self.db.refresh(task)
        self.assertEqual(next_result["steps"], 1)
        self.assertEqual(task.attempts, attempts_before)
        next_run = self.db.get(AgentRun, next_result["run_id"])
        self.assertIsNotNone(next_run.mission_id)
        self.assertEqual(
            self.db.get(AgentMission, next_run.mission_id).kind,
            "candidate_discovery",
        )

    def test_tool_round_no_progress_exit_pauses_and_checkpoints(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        sibling = Marker(
            city_id=2,
            category=MarkerCategory.restaurant,
            shape=MarkerShape.point,
            title="later integrity target",
            description="must remain ready while mission is paused",
            lat=41.83,
            lng=123.38,
        )
        self.db.add(sibling)
        self.db.flush()
        task = AgentTask(
            city_id=2,
            kind="research",
            title="tool round stall",
            detail=(
                f"targets:\n- #{place.id} {place.title}\n"
                f"- #{sibling.id} {sibling.title}"
            ),
            success_metric="record a verdict",
            priority=100,
        )
        self.db.add(task)
        self.db.commit()
        repeated_reads = [("get_place", {"place_id": place.id}) for _ in range(20)]

        result, requests = self._run_scripted_agent(repeated_reads, max_steps=24)

        self.assertEqual(result["steps"], 20)
        self.assertEqual(len(requests), 20)
        mission = self.db.query(AgentMission).filter(AgentMission.task_id == task.id).one()
        items = (
            self.db.query(AgentWorkItem)
            .filter(AgentWorkItem.mission_id == mission.id)
            .order_by(AgentWorkItem.priority.desc())
            .all()
        )
        self.assertEqual(mission.status, "paused")
        self.assertEqual([item.status for item in items], ["blocked", "ready"])
        self.assertIn("새 근거·데이터·정제가 생기지 않음", items[0].blocked_reason)
        self.assertEqual(items[1].attempts, 0)
        self.assertIsNone(items[1].last_run_id)
        halt_step = (
            self.db.query(AgentRunStep)
            .filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool == "orchestrator_no_progress",
            )
            .one()
        )
        self.assertEqual(halt_step.outcome, "blocked")

    def test_batch_malformed_tool_arguments_are_not_executed_as_empty_object(self) -> None:
        event = (
            self.db.query(PlaceEvent)
            .join(Marker, Marker.id == PlaceEvent.place_id)
            .filter(Marker.city_id == 2)
            .one()
        )
        requests = []

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                round_number = len(requests)
                if round_number == 1:
                    call = SimpleNamespace(
                        id="bad-json",
                        function=SimpleNamespace(
                            name="mark_events_read",
                            arguments='{"event_ids":[',
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                if round_number == 2:
                    # The malformed call must not have reached run_tool.
                    self.db.refresh(event)
                    self.assertIsNone(event.groq_read_at)
                    call = SimpleNamespace(
                        id="valid-json",
                        function=SimpleNamespace(
                            name="mark_events_read",
                            arguments=json.dumps({"event_ids": [event.id]}),
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="처리를 완료했습니다.", tool_calls=[],
                ))])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=4,
                autonomous_research=False,
            )

        self.assertTrue(result["ok"], result)
        self.db.refresh(event)
        self.assertIsNotNone(event.groq_read_at)
        recovery_tools = {
            item["function"]["name"] for item in requests[1].get("tools", [])
        }
        self.assertEqual(recovery_tools, {"mark_events_read"})
        malformed_step = (
            self.db.query(AgentRunStep)
            .filter(AgentRunStep.tool == "mark_events_read", AgentRunStep.outcome == "error")
            .order_by(AgentRunStep.id.asc())
            .first()
        )
        self.assertIsNotNone(malformed_step)
        detail = json.loads(malformed_step.detail)
        self.assertEqual(detail["result"]["error"], "malformed_tool_arguments")
        self.assertTrue(detail["result"]["signature"])

    def test_exhausted_image_target_rotates_when_model_tries_next_target(self) -> None:
        mission = type("Mission", (), {"kind": "quality_images"})()
        work = type("Work", (), {"place_id": 103})()
        self.assertTrue(_should_rotate_exhausted_image_target(
            mission=mission,
            work_item=work,
            target_mismatch={"error": "active_work_item_mismatch"},
            image_searches_by_place={103: 3},
        ))
        self.assertFalse(_should_rotate_exhausted_image_target(
            mission=mission,
            work_item=work,
            target_mismatch={"error": "active_work_item_mismatch"},
            image_searches_by_place={103: 2},
        ))

    def test_model_call_cannot_move_active_cursor_back_to_ready_sibling(self) -> None:
        places = self.db.query(Marker).filter(Marker.city_id == 2).all()
        second = Marker(
            city_id=2, category=MarkerCategory.restaurant, shape=MarkerShape.point,
            title="두 번째 활성 대상", description="한국어 설명", lat=41.83, lng=123.43,
        )
        self.db.add(second)
        self.db.flush()
        task = AgentTask(
            city_id=2, kind="quality_images", title="이미지",
            detail=f"대상:\n- #{places[0].id} {places[0].title}\n- #{second.id} {second.title}",
            success_metric="사진", priority=100,
        )
        self.db.add(task)
        self.db.commit()
        mission, first = ensure_mission_for_task(self.db, task)
        second_item = rotate_blocked_work_item(
            self.db, mission=mission, current=first, run_id=2, reason="첫 대상 차단",
        )
        self.assertEqual(active_work_item_for_mission(self.db, mission).id, second_item.id)
        run = AgentRun(city_id=2, mission_id=mission.id, work_item_id=second_item.id, status="running")
        self.db.add(run)
        self.db.commit()
        updated, _ = checkpoint_after_tool(
            self.db, mission=mission, work_item=second_item, run_id=run.id, sequence=1,
            tool="get_place", args={"place_id": first.place_id}, result={"id": first.place_id},
            outcome="ok", new_evidence_count=0, material_change=False,
        )
        self.assertEqual(updated.id, second_item.id)
        self.assertEqual(active_work_item_for_mission(self.db, mission).id, second_item.id)

    def test_batch_progress_ignores_noop_mutations_and_repeated_evidence(self) -> None:
        self.assertFalse(_is_material_change("upsert_agent_task", {"ok": True, "changed": False}))
        self.assertFalse(_is_material_change("upsert_agent_task", {"ok": True, "created": True}))
        self.assertFalse(_is_material_change(
            "propose_place",
            {"ok": True, "proposal_created": False, "proposal_id": 91},
        ))
        self.assertFalse(_is_material_change(
            "create_place",
            {"ok": True, "proposal_created": False, "proposal_id": 91},
        ))
        self.assertTrue(_is_material_change(
            "propose_place",
            {"ok": True, "proposal_created": True, "proposal_id": 91},
        ))
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

        large_place_detail = _step_detail_json(
            {"place_id": 104},
            {"id": 104, "title": "large target", "images": ["x" * 2000] * 8},
            {"new_evidence": 0},
            max_chars=1000,
        )
        parsed_place = json.loads(large_place_detail)
        self.assertTrue(parsed_place["truncated"])
        self.assertEqual(parsed_place["args"]["place_id"], 104)
        self.assertEqual(parsed_place["result"]["id"], 104)

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

    def test_recovery_can_force_compaction_before_context_limit(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "objective"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "latest state"},
        ]
        compacted, changed = _compact_react_messages(
            messages,
            tool_counts={},
            material_changes=[],
            current_score=0,
            max_chars=50_000,
            force=True,
            recent_round_limit=1,
        )
        self.assertTrue(changed)
        self.assertEqual(compacted[0]["content"], "system")
        self.assertEqual(compacted[1]["content"], "objective")
        self.assertIn("latest state", [item.get("content") for item in compacted])

    def test_candidate_discovery_is_periodic_and_city_scoped(self) -> None:
        self.assertEqual(CANDIDATE_DISCOVERY_INTERVAL, 3)
        self.assertTrue(_candidate_discovery_due(self.db, city_id=2))

        discovery_task = AgentTask(
            city_id=2,
            kind=CANDIDATE_DISCOVERY_KIND,
            title="선양 발굴",
            status="completed",
        )
        self.db.add(discovery_task)
        self.db.flush()
        discovery_mission = AgentMission(
            city_id=2,
            task_id=discovery_task.id,
            kind=CANDIDATE_DISCOVERY_KIND,
            title=discovery_task.title,
            status="completed",
        )
        self.db.add(discovery_mission)
        self.db.flush()
        self.db.add(AgentRun(
            city_id=2,
            mission_id=discovery_mission.id,
            mode="research",
            status="completed",
        ))
        self.db.commit()

        self.assertFalse(_candidate_discovery_due(self.db, city_id=2))
        self.assertTrue(_candidate_discovery_due(self.db, city_id=1))
        for index in range(CANDIDATE_DISCOVERY_INTERVAL - 1):
            quality_task = AgentTask(
                city_id=2,
                kind="quality_information",
                title=f"품질 {index}",
                status="completed",
            )
            self.db.add(quality_task)
            self.db.flush()
            mission = AgentMission(
                city_id=2,
                task_id=quality_task.id,
                kind=quality_task.kind,
                title=quality_task.title,
                status="completed",
            )
            self.db.add(mission)
            self.db.flush()
            self.db.add(AgentRun(
                city_id=2,
                mission_id=mission.id,
                mode="research",
                status="completed",
            ))
            self.db.commit()
        self.assertTrue(_candidate_discovery_due(self.db, city_id=2))

    def test_blocked_discovery_keeps_exact_cursor_and_rotates_role_during_cooldown(self) -> None:
        self.assertEqual(CANDIDATE_DISCOVERY_COOLDOWN_HOURS, 12)
        task = _ensure_candidate_discovery_task(self.db, city=self.db.get(City, 2))
        self.assertIsNotNone(task)
        mission, item = ensure_mission_for_task(self.db, task)
        from app.agent.runner import _pin_candidate_mission_role
        original_role = _pin_candidate_mission_role(mission, task)
        self.db.commit()
        original = (task.id, mission.id, item.id)
        task.status = "blocked"
        task.result = "provider temporarily blocked"
        finalize_mission(self.db, mission=mission, task=task, run_id=77)

        self.assertEqual(mission.status, "paused")
        self.assertEqual(item.status, "blocked")
        self.assertIn("12시간 냉각", json.loads(mission.progress)["retry_condition"])
        rotated_task = _ensure_candidate_discovery_task(self.db, city=self.db.get(City, 2))
        self.assertIsNotNone(rotated_task)

        self.assertNotEqual(rotated_task.id, original[0])
        self.assertNotEqual(_candidate_task_role(rotated_task), original_role)
        self.assertEqual((task.id, mission.id, item.id), original)
        self.assertEqual(task.status, "blocked")
        self.assertEqual(mission.status, "paused")
        self.assertEqual(item.status, "blocked")

    def test_discovery_returns_none_when_every_missing_role_frontier_is_cooling(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)
        for role in CANDIDATE_ROLE_TARGETS:
            task = AgentTask(
                city_id=2,
                kind=CANDIDATE_DISCOVERY_KIND,
                title=f"cooling {role}",
                detail=f"target_role: {role}\nexact role frontier",
                status="blocked",
            )
            self.db.add(task)
            self.db.flush()
            mission = AgentMission(
                city_id=2,
                task_id=task.id,
                kind=CANDIDATE_DISCOVERY_KIND,
                title=task.title,
                objective=task.detail,
                status="paused",
                strategy=json.dumps({"target_role": role}),
                updated_at=now,
            )
            self.db.add(mission)
            self.db.flush()
            self.db.add(AgentWorkItem(
                mission_id=mission.id,
                city_id=2,
                target_type="task",
                target_key=f"task:{task.id}",
                title=task.title,
                status="blocked",
            ))
        self.db.commit()

        selected = _ensure_candidate_discovery_task(self.db, city=self.db.get(City, 2))

        self.assertIsNone(selected)
        self.assertTrue(all(
            mission.status == "paused"
            for mission in self.db.query(AgentMission).filter(
                AgentMission.kind == CANDIDATE_DISCOVERY_KIND,
            )
        ))

        with (
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        self.assertEqual(result["steps"], 0)
        self.assertEqual(result["outcome"], "deferred")
        run = self.db.get(AgentRun, result["run_id"])
        self.assertEqual(run.mode, "idle")
        self.assertEqual(json.loads(run.metrics)["lane"], "discovery_deferred")

    def test_role_frontier_is_fixed_in_task_and_mission_prompt(self) -> None:
        from app.agent.runner import (
            _candidate_discovery_system,
            _candidate_proposal_role_error,
            _pin_candidate_mission_role,
            _scoped_mission_user_message,
        )

        task = _ensure_candidate_discovery_task(self.db, city=self.db.get(City, 2))
        self.assertIsNotNone(task)
        mission, item = ensure_mission_for_task(self.db, task)
        role = _pin_candidate_mission_role(mission, task)
        self.db.commit()

        self.assertIn(role, CANDIDATE_ROLE_TARGETS)
        self.assertEqual(_candidate_mission_role(mission), role)
        self.assertIn(f"target_role={role}", _candidate_discovery_system(
            self.db.get(City, 2), mission, item,
        ))
        self.assertIn(f"target_role={role}", _scoped_mission_user_message(
            self.db.get(City, 2), mission, item, continuity_hint="{}",
        ))
        other_role = next(candidate for candidate in CANDIDATE_ROLE_TARGETS if candidate != role)
        mismatch = _candidate_proposal_role_error(
            "propose_place", {"travel_role": other_role}, mission,
        )
        self.assertEqual(mismatch["error"], "candidate_target_role_mismatch")

    def test_expired_role_frontier_resumes_exact_cursor_after_other_roles_cool(self) -> None:
        now = datetime.now(timezone.utc)
        original: tuple[AgentTask, AgentMission, AgentWorkItem] | None = None
        for index, role in enumerate(CANDIDATE_ROLE_TARGETS):
            task = AgentTask(
                city_id=2,
                kind=CANDIDATE_DISCOVERY_KIND,
                title=f"frontier {role}",
                detail=f"target_role: {role}\nexact role frontier",
                status="blocked",
            )
            self.db.add(task)
            self.db.flush()
            mission = AgentMission(
                city_id=2,
                task_id=task.id,
                kind=CANDIDATE_DISCOVERY_KIND,
                title=task.title,
                objective=task.detail,
                status="paused",
                strategy=json.dumps({"target_role": role}),
                updated_at=(
                    now - timedelta(hours=CANDIDATE_DISCOVERY_COOLDOWN_HOURS + 1)
                    if index == 0
                    else now
                ),
            )
            self.db.add(mission)
            self.db.flush()
            item = AgentWorkItem(
                mission_id=mission.id,
                city_id=2,
                target_type="task",
                target_key=f"task:{task.id}",
                title=task.title,
                status="blocked",
                next_action=json.dumps({
                    "handoff_version": "candidate_dossier_v1",
                    "candidate": {"external_id": "keep-me"},
                    "tool": "web_search",
                }),
            )
            self.db.add(item)
            if index == 0:
                original = (task, mission, item)
        self.db.commit()
        self.assertIsNotNone(original)
        original_task, original_mission, original_item = original

        selected = _ensure_candidate_discovery_task(self.db, city=self.db.get(City, 2))
        self.assertEqual(selected.id, original_task.id)
        resumed_mission, resumed_item = ensure_mission_for_task(self.db, selected)

        self.assertEqual(resumed_mission.id, original_mission.id)
        self.assertEqual(resumed_item.id, original_item.id)
        self.assertEqual(resumed_item.status, "active")
        self.assertEqual(
            json.loads(resumed_item.next_action)["candidate"]["external_id"],
            "keep-me",
        )

    def test_scoped_blocked_discovery_is_completed_for_this_run_despite_city_gaps(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        self.db.commit()

        rounds = 0

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                rounds += 1
                task = self.db.query(AgentTask).filter(
                    AgentTask.city_id == 2,
                    AgentTask.kind == CANDIDATE_DISCOVERY_KIND,
                    AgentTask.status == "pending",
                ).one()
                if rounds == 1:
                    call = SimpleNamespace(
                        id="discovery-search",
                        function=SimpleNamespace(
                            name="web_search",
                            arguments=json.dumps({"query": "沈阳 新地点 独立验证"}),
                        ),
                    )
                elif rounds == 2:
                    call = SimpleNamespace(
                        id="discovery-fetch",
                        function=SimpleNamespace(
                            name="fetch_page",
                            arguments=json.dumps({"url": "https://example.test/shenyang-place"}),
                        ),
                    )
                else:
                    call = SimpleNamespace(
                        id="blocked-discovery",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps({
                                "task_id": task.id,
                                "status": "blocked",
                                "result": "독립 검증 가능한 후보가 없음",
                            }),
                        ),
                    )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        def fake_tool(db, name, args, **kwargs):
            if name == "web_search":
                return {"results": [{
                    "title": "선양 신규 장소 공식 안내",
                    "href": "https://example.test/shenyang-place",
                    "body": "공식 장소 안내",
                }]}
            if name == "fetch_page":
                return {
                    "url": args["url"],
                    "title": "선양 신규 장소 공식 안내",
                    "text": "선양의 신규 장소를 소개하는 독립 출처 본문입니다. " * 500,
                }
            return run_tool(db, name, args, **kwargs)

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        self.assertTrue(result["remaining_gaps"])
        self.assertEqual(result["status"], "completed")
        run = self.db.get(AgentRun, result["run_id"])
        task = self.db.get(AgentTask, json.loads(run.metrics)["primary_task_id"])
        mission = self.db.get(AgentMission, run.mission_id)
        self.assertEqual(task.status, "blocked")
        self.assertEqual(mission.status, "paused")
        self.assertIsNone(active_work_item_for_mission(self.db, mission))
        terminal = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == run.id,
            AgentRunStep.tool == "upsert_agent_task",
        ).one()
        terminal_args = json.loads(terminal.detail)["args"]
        self.assertEqual(set(terminal_args), {"task_id", "status", "result"})
        self.assertEqual(terminal_args["task_id"], task.id)
        self.assertEqual(terminal_args["status"], "blocked")
        self.assertNotIn("독립 검증 가능한 후보가 없음", terminal_args["result"])

    def test_discovery_terminal_claims_without_research_or_proposal_are_rejected(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        self.db.commit()

        class Completions:
            def create(inner_self, **_kwargs):
                task = self.db.query(AgentTask).filter(
                    AgentTask.city_id == 2,
                    AgentTask.kind == CANDIDATE_DISCOVERY_KIND,
                    AgentTask.status == "pending",
                ).one()
                calls = [
                    SimpleNamespace(
                        id="false-completed",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps({
                                "task_id": task.id,
                                "status": "completed",
                                "result": "말로만 완료",
                            }),
                        ),
                    ),
                    SimpleNamespace(
                        id="unsupported-blocked",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps({
                                "task_id": task.id,
                                "status": "blocked",
                                "result": "조사 없이 차단",
                            }),
                        ),
                    ),
                ]
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=calls,
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool") as tool,
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        tool.assert_not_called()
        errors = {
            json.loads(step.detail)["result"]["error"]
            for step in self.db.query(AgentRunStep).filter(
                AgentRunStep.run_id == result["run_id"],
                AgentRunStep.tool == "upsert_agent_task",
            )
        }
        self.assertEqual(errors, {
            "candidate_discovery_completion_server_controlled",
            "candidate_discovery_research_evidence_required",
        })
        run = self.db.get(AgentRun, result["run_id"])
        task = self.db.get(AgentTask, json.loads(run.metrics)["primary_task_id"])
        mission = self.db.get(AgentMission, run.mission_id)
        self.assertEqual(task.status, "pending")
        self.assertEqual(mission.status, "active")

    def test_discovery_cannot_block_after_successful_search_without_independent_verification(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        self.db.commit()
        rounds = 0

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                rounds += 1
                task = self.db.query(AgentTask).filter(
                    AgentTask.city_id == 2,
                    AgentTask.kind == CANDIDATE_DISCOVERY_KIND,
                    AgentTask.status == "pending",
                ).one()
                if rounds == 1:
                    call = SimpleNamespace(
                        id="search-only",
                        function=SimpleNamespace(
                            name="web_search",
                            arguments=json.dumps({"query": "沈阳 新地点 搜索-only"}),
                        ),
                    )
                else:
                    call = SimpleNamespace(
                        id="premature-block",
                        function=SimpleNamespace(
                            name="upsert_agent_task",
                            arguments=json.dumps({
                                "task_id": task.id,
                                "status": "blocked",
                                "result": "검색만 하고 차단",
                            }),
                        ),
                    )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )

        def fake_tool(_db, name, _args, **_kwargs):
            self.assertEqual(name, "web_search")
            return {"results": []}

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=2,
                autonomous_research=True,
            )

        terminal = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == result["run_id"],
            AgentRunStep.tool == "upsert_agent_task",
        ).one()
        self.assertEqual(
            json.loads(terminal.detail)["result"]["error"],
            "candidate_discovery_research_evidence_required",
        )
        run = self.db.get(AgentRun, result["run_id"])
        task = self.db.get(AgentTask, json.loads(run.metrics)["primary_task_id"])
        mission = self.db.get(AgentMission, run.mission_id)
        self.assertEqual(task.status, "pending")
        self.assertEqual(mission.status, "active")

    def test_discovery_research_evidence_accepts_only_explicit_storable_non_brave_geocode(self) -> None:
        run = AgentRun(city_id=2, mode="research", status="running")
        self.db.add(run)
        self.db.flush()
        self.db.add(AgentRunStep(
            run_id=run.id,
            sequence=1,
            tool="web_search",
            detail=json.dumps({"args": {}, "result": {"results": []}}),
        ))
        self.db.add(AgentRunStep(
            run_id=run.id,
            sequence=2,
            tool="geocode_place",
            detail=json.dumps({
                "args": {},
                "result": {"results": [{
                    "source": "brave_place",
                    "storage_allowed": False,
                    "lat": 41.8,
                    "lng": 123.4,
                }]},
            }),
        ))
        self.db.commit()
        self.assertEqual(_candidate_discovery_research_refs(self.db, run_id=run.id), [])

        brave_step = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == run.id,
            AgentRunStep.sequence == 2,
        ).one()
        brave_step.detail = json.dumps({
            "args": {},
            "result": {"results": [{
                "source": "arcgis",
                "storage_allowed": True,
                "lat": 41.8,
                "lng": 123.4,
            }]},
        })
        self.db.commit()
        self.assertEqual(
            _candidate_discovery_research_refs(self.db, run_id=run.id),
            [f"run:{run.id}:step:1", f"run:{run.id}:step:2"],
        )

        brave_step.detail = json.dumps({
            "args": {},
            "result": {"status": "independent_verification_retained_as_marker"},
            "progress": {"discovery_verification_ok": True},
        })
        self.db.commit()
        self.assertEqual(
            _candidate_discovery_research_refs(self.db, run_id=run.id),
            [f"run:{run.id}:step:1", f"run:{run.id}:step:2"],
        )

    def test_successful_discovery_proposal_auto_completes_task_and_mission(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        self.db.commit()
        rounds = 0
        title = "测试咖啡馆 (테스트 카페)"
        address = "沈阳市测试路1号"

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                rounds += 1
                if rounds == 1:
                    call = SimpleNamespace(
                        id="canonical-page",
                        function=SimpleNamespace(
                            name="fetch_page",
                            arguments=json.dumps({"url": "https://example.test/cafe"}),
                        ),
                    )
                elif rounds == 2:
                    call = SimpleNamespace(
                        id="canonical-geocode",
                        function=SimpleNamespace(
                            name="geocode_place",
                            arguments=json.dumps({"query": title}),
                        ),
                    )
                else:
                    call = SimpleNamespace(
                        id="proposal",
                        function=SimpleNamespace(
                            name="propose_place",
                            arguments=json.dumps({
                                "title": title,
                                "description": (
                                    "선양 테스트로에 있는 카페로 독립 공개 본문과 저장 가능한 좌표에서 "
                                    "같은 상호와 주소를 확인했습니다. 여행 중 차와 음료를 마시며 잠시 "
                                    "쉬어 갈 수 있는 휴식 후보로 검토할 만합니다."
                                ),
                                "address": address,
                                "category": "drink",
                                "travel_role": "rest",
                                "lat": 41.81,
                                "lng": 123.45,
                                "evidence": "독립 좌표와 본문을 확인했습니다.",
                                "source_urls": ["https://example.test/cafe"],
                                "confidence": 0.9,
                                "insights": [],
                            }),
                        ),
                    )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        geocode_result = {"results": [{
            "display_name": title,
            "address": address,
            "lat": 41.81,
            "lng": 123.45,
            "source": "arcgis",
            "source_url": "https://example.test/geo",
            "external_id": "canonical-1",
            "confidence": 0.9,
            "storage_allowed": True,
        }]}

        def fake_tool(_db, name, _args, **_kwargs):
            if name == "fetch_page":
                return {
                    "url": "https://example.test/cafe",
                    "title": "测试咖啡馆公开页面",
                    "text": (f"测试咖啡馆位于{address}，提供茶饮和休息空间。" * 20),
                }
            if name == "geocode_place":
                return geocode_result
            if name == "propose_place":
                return {
                    "ok": True,
                    "proposal_created": True,
                    "proposal_id": 501,
                }
            return {"ok": True}

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        role_task = AgentTask(
            city_id=2,
            kind=CANDIDATE_DISCOVERY_KIND,
            title="자동 신규 장소 발굴 [휴식]: 선양",
            detail="target_role: rest\n선양에서 휴식 역할 후보만 탐색",
            success_metric="travel_role=rest인 검증 후보 제안",
            priority=88,
            status="pending",
        )
        self.db.add(role_task)
        self.db.commit()
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch(
                "app.agent.runner._select_autonomous_task",
                return_value=(role_task, CANDIDATE_DISCOVERY_KIND, True),
            ),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        run = self.db.get(AgentRun, result["run_id"])
        task = self.db.get(AgentTask, json.loads(run.metrics)["primary_task_id"])
        mission = self.db.get(AgentMission, run.mission_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(task.status, "completed")
        self.assertEqual(
            task.result,
            f"실행 #{run.id}: 검증된 신규 장소 승인 제안 #501 생성",
        )
        self.assertEqual(mission.status, "completed")
        self.assertIsNone(active_work_item_for_mission(self.db, mission))

    def test_duplicate_discovery_proposal_does_not_complete_task(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        self.db.commit()
        rounds = 0
        title = "重复候选咖啡馆 (중복 후보 카페)"
        address = "沈阳市重复路1号"

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                rounds += 1
                if rounds == 1:
                    call = SimpleNamespace(
                        id="duplicate-geocode",
                        function=SimpleNamespace(
                            name="geocode_place",
                            arguments=json.dumps({"query": title}),
                        ),
                    )
                else:
                    call = SimpleNamespace(
                        id="duplicate-proposal",
                        function=SimpleNamespace(
                            name="propose_place",
                            arguments=json.dumps({
                                "title": title,
                                "description": "이미 승인 대기에 있는 후보입니다.",
                                "address": address,
                                "category": "drink",
                                "travel_role": "rest",
                                "lat": 41.81,
                                "lng": 123.45,
                                "evidence": "독립 좌표를 확인했습니다.",
                                "source_urls": ["https://example.test/duplicate"],
                                "insights": [],
                            }),
                        ),
                    )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        geocode_result = {"results": [{
            "display_name": title,
            "address": address,
            "lat": 41.81,
            "lng": 123.45,
            "source": "arcgis",
            "source_url": "https://example.test/geo-duplicate",
            "external_id": "duplicate-1",
            "confidence": 0.9,
            "storage_allowed": True,
        }]}

        def fake_tool(_db, name, _args, **_kwargs):
            if name == "geocode_place":
                return geocode_result
            if name == "propose_place":
                return {
                    "ok": True,
                    "proposal_created": False,
                    "duplicate": True,
                    "proposal_id": 77,
                }
            return {"ok": True}

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        role_task = AgentTask(
            city_id=2,
            kind=CANDIDATE_DISCOVERY_KIND,
            title="자동 신규 장소 발굴 [휴식]: 선양",
            detail="target_role: rest\n선양에서 휴식 역할 후보만 탐색",
            success_metric="travel_role=rest인 검증 후보 제안",
            priority=88,
            status="pending",
        )
        self.db.add(role_task)
        self.db.commit()
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch(
                "app.agent.runner._select_autonomous_task",
                return_value=(role_task, CANDIDATE_DISCOVERY_KIND, True),
            ),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=2,
                autonomous_research=True,
            )

        run = self.db.get(AgentRun, result["run_id"])
        task = self.db.get(AgentTask, json.loads(run.metrics)["primary_task_id"])
        mission = self.db.get(AgentMission, run.mission_id)
        self.assertEqual(task.status, "pending")
        self.assertNotIn("승인 제안 #77 생성", task.result or "")
        self.assertEqual(mission.status, "active")
        self.assertIsNotNone(active_work_item_for_mission(self.db, mission))

    def test_non_transient_proposal_receives_verified_page_docs_but_history_drops_them(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        title = "独立茶馆 (독립 차관)"
        address = "沈河区中街路88号"
        source_url = "https://example.test/independent-tea"
        calls = [
            ("fetch_page", {"url": source_url}),
            ("geocode_place", {"query": f"{title} {address}"}),
            ("propose_place", {
                "title": title,
                "description": "공개 본문으로 확인한 선양의 독립 찻집입니다.",
                "address": address,
                "category": "drink",
                "travel_role": "rest",
                "lat": 41.8012,
                "lng": 123.4521,
                "evidence": "공개 본문과 저장 가능한 좌표가 같은 지점을 확인합니다.",
                "source_urls": [source_url],
                "insights": [],
            }),
        ]
        rounds = 0

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                name, arguments = calls[rounds]
                rounds += 1
                call = SimpleNamespace(
                    id=f"grounded-{rounds}",
                    function=SimpleNamespace(
                        name=name,
                        arguments=json.dumps(arguments, ensure_ascii=False),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        received_documents: list[dict] = []

        def fake_tool(_db, name, args, **_kwargs):
            if name == "fetch_page":
                return {
                    "url": source_url,
                    "title": "独立茶馆公开页面",
                    "text": (f"独立茶馆位于{address}，提供茶饮和到访信息。" * 20),
                }
            if name == "geocode_place":
                return {"results": [{
                    "display_name": title,
                    "address": address,
                    "lat": 41.8012,
                    "lng": 123.4521,
                    "source": "nominatim",
                    "source_url": "https://www.openstreetmap.org/node/123",
                    "external_id": "node/123",
                    "confidence": 0.9,
                    "storage_allowed": True,
                }]}
            if name == "propose_place":
                received_documents.extend(args.get("_validated_source_documents") or [])
                return {"ok": True, "proposal_created": True, "proposal_id": 700}
            return {"ok": True}

        role_task = AgentTask(
            city_id=2,
            kind=CANDIDATE_DISCOVERY_KIND,
            title="자동 신규 장소 발굴 [휴식]: 선양",
            detail="target_role: rest\n선양에서 휴식 역할 후보만 탐색",
            success_metric="travel_role=rest인 검증 후보 제안",
            priority=88,
            status="pending",
        )
        self.db.add(role_task)
        self.db.commit()
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch(
                "app.agent.runner._select_autonomous_task",
                return_value=(role_task, CANDIDATE_DISCOVERY_KIND, True),
            ),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        self.assertEqual(rounds, 3)
        self.assertEqual(received_documents[0]["url"], source_url)
        proposal_step = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == result["run_id"],
            AgentRunStep.tool == "propose_place",
        ).one()
        persisted_args = json.loads(proposal_step.detail)["args"]
        self.assertNotIn("_validated_source_documents", persisted_args)

    def test_non_transient_proposal_rejects_a_fetched_page_for_another_poi(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        target_title = "独立茶馆 (독립 차관)"
        target_address = "沈河区中街路88号"
        wrong_url = "https://example.test/another-restaurant"
        calls = [
            ("fetch_page", {"url": wrong_url}),
            ("geocode_place", {"query": f"{target_title} {target_address}"}),
            ("propose_place", {
                "title": target_title,
                "description": (
                    "선양 중제에서 차를 마시며 잠시 쉬기 좋은 독립 찻집 후보입니다. "
                    "정확한 상호와 지점 주소, 좌표 및 공개 상세 페이지를 함께 확인해 "
                    "여행자의 휴식 동선에 넣을 수 있도록 검토합니다."
                ),
                "address": target_address,
                "category": "drink",
                "travel_role": "rest",
                "lat": 41.8012,
                "lng": 123.4521,
                "evidence": "본문과 저장 가능한 좌표가 같은 지점인지 확인합니다.",
                "source_urls": [wrong_url],
                "confidence": 0.9,
                "insights": [],
            }),
        ]
        rounds = 0

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                name, arguments = calls[rounds]
                rounds += 1
                call = SimpleNamespace(
                    id=f"wrong-page-{rounds}",
                    function=SimpleNamespace(
                        name=name,
                        arguments=json.dumps(arguments, ensure_ascii=False),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        proposal_executed = False

        def fake_tool(_db, name, _args, **_kwargs):
            nonlocal proposal_executed
            if name == "fetch_page":
                return {
                    "url": wrong_url,
                    "title": "完全不同餐厅公开页面",
                    "text": ("另一家餐厅位于和平区青年大街999号，提供正餐。" * 20),
                }
            if name == "geocode_place":
                return {"results": [{
                    "display_name": target_title,
                    "address": target_address,
                    "lat": 41.8012,
                    "lng": 123.4521,
                    "source": "nominatim",
                    "source_url": "https://www.openstreetmap.org/node/123",
                    "external_id": "node/123",
                    "confidence": 0.9,
                    "storage_allowed": True,
                }]}
            if name == "propose_place":
                proposal_executed = True
                return {"ok": True, "proposal_created": True, "proposal_id": 999}
            return {"ok": True}

        role_task = AgentTask(
            city_id=2,
            kind=CANDIDATE_DISCOVERY_KIND,
            title="자동 신규 장소 발굴 [휴식]: 선양",
            detail="target_role: rest\n선양에서 휴식 역할 후보만 탐색",
            success_metric="travel_role=rest인 검증 후보 제안",
            priority=88,
            status="pending",
        )
        self.db.add(role_task)
        self.db.commit()
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch(
                "app.agent.runner._select_autonomous_task",
                return_value=(role_task, CANDIDATE_DISCOVERY_KIND, True),
            ),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        self.assertEqual(rounds, 3)
        self.assertFalse(proposal_executed)
        proposal_step = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == result["run_id"],
            AgentRunStep.tool == "propose_place",
        ).one()
        self.assertEqual(
            json.loads(proposal_step.detail)["result"]["error"],
            "proposal_source_target_not_verified",
        )

    def test_scoped_quality_schema_pins_exact_task_and_blocked_terminal_shape(self) -> None:
        tools = _scoped_quality_tools(
            task_id=731,
            tool_names=RECOVERY_TOOLS_BY_TASK["quality_information"],
        )
        task_tool = next(
            tool for tool in tools
            if tool["function"]["name"] == "upsert_agent_task"
        )
        schema = task_tool["function"]["parameters"]

        self.assertEqual(schema["required"], ["task_id", "status", "result"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["task_id"]["enum"], [731])
        self.assertEqual(schema["properties"]["status"]["enum"], ["blocked"])
        self.assertNotIn("minLength", schema["properties"]["result"])

    def test_information_blocker_requires_three_exact_subject_research_axes(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="quality_information",
            title="정보 차단 근거 테스트",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 구조화 정보 부족)",
            success_metric="정확 장소의 정보 보완 또는 감사 가능한 차단",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        mission, item = ensure_mission_for_task(self.db, task)
        run = AgentRun(city_id=2, mission_id=mission.id, work_item_id=item.id)
        self.db.add(run)
        self.db.flush()

        def add_step(sequence: int, tool: str, args: dict, result: dict) -> None:
            self.db.add(AgentRunStep(
                run_id=run.id,
                sequence=sequence,
                tool=tool,
                detail=json.dumps({"args": args, "result": result}),
            ))

        add_step(1, "get_place", {"place_id": place.id}, {"id": place.id})
        # Three empty searches for another attraction must not block this one.
        for sequence in range(2, 5):
            add_step(
                sequence,
                "web_search",
                {"query": f"完全不同景点 source axis {sequence}"},
                {"results": []},
            )
        self.db.commit()
        self.assertEqual(
            _scoped_quality_block_evidence_refs(
                self.db,
                run_id=run.id,
                mission=mission,
                work_item=item,
            ),
            [],
        )

        # One matching search is still insufficient. Only three distinct,
        # exact-subject axes authorize a temporary blocker.
        for sequence in range(5, 8):
            add_step(
                sequence,
                "web_search",
                {"query": f"{place.title} official source axis {sequence}"},
                {"results": []},
            )
            self.db.commit()
            refs = _scoped_quality_block_evidence_refs(
                self.db,
                run_id=run.id,
                mission=mission,
                work_item=item,
            )
            if sequence < 7:
                self.assertEqual(refs, [])
        self.assertEqual(
            refs,
            [
                f"run:{run.id}:step:1",
                f"run:{run.id}:step:5",
                f"run:{run.id}:step:6",
                f"run:{run.id}:step:7",
            ],
        )

    def test_first_scoped_material_change_skips_stale_parallel_calls_and_keeps_run_target(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        first = self.db.query(Marker).filter(Marker.city_id == 2).one()
        second = Marker(
            city_id=2,
            category=MarkerCategory.restaurant,
            shape=MarkerShape.point,
            title="第二目标餐厅 (두 번째 대상 식당)",
            description="한국어 설명",
            lat=41.81,
            lng=123.44,
        )
        self.db.add(second)
        self.db.flush()
        task = AgentTask(
            city_id=2,
            kind="quality_information",
            title="정보 결손 커서 전환 테스트",
            detail=(
                "대상:\n"
                f"- #{first.id} {first.title} (현재: 정보 부족)\n"
                f"- #{second.id} {second.title} (현재: 정보 부족)"
            ),
            success_metric="장소별 설명 60자와 인사이트 2개",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        mission, initial_item = ensure_mission_for_task(self.db, task)
        sibling = self.db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.id != initial_item.id,
        ).one()
        source_url = "https://example.test/exact-first-place"
        requests: list[dict] = []
        rounds = 0

        class Completions:
            def create(inner_self, **kwargs):
                nonlocal rounds
                rounds += 1
                requests.append(kwargs)
                if rounds == 1:
                    calls = [SimpleNamespace(
                        id="read-source",
                        function=SimpleNamespace(
                            name="fetch_page",
                            arguments=json.dumps({"url": source_url}),
                        ),
                    )]
                elif rounds == 2:
                    calls = [
                        SimpleNamespace(
                            id="write-current",
                            function=SimpleNamespace(
                                name="upsert_place_insights",
                                arguments=json.dumps({
                                    "place_id": first.id,
                                    "insights": [{
                                        "kind": "visit",
                                        "title": "방문 정보",
                                        "content": "공식 안내에서 운영 정보를 확인했습니다.",
                                        "source_url": source_url,
                                        "source_title": "공식 안내",
                                        "confidence": 0.9,
                                    }],
                                }),
                            ),
                        ),
                        SimpleNamespace(
                            id="stale-next-target",
                            function=SimpleNamespace(
                                name="upsert_place_insights",
                                arguments=json.dumps({
                                    "place_id": second.id,
                                    "insights": [{
                                        "kind": "visit",
                                        "title": "실행되면 안 됨",
                                        "content": "오래된 프롬프트의 병렬 호출",
                                        "source_url": source_url,
                                    }],
                                }),
                            ),
                        ),
                    ]
                else:
                    self.fail("a scoped material change must halt before another provider round")
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=calls,
                ))])

        executed_writes: list[int] = []
        injected_documents: list[dict] = []

        def fake_tool(db, name, args, **kwargs):
            if name == "fetch_page":
                return {
                    "url": source_url,
                    "title": "공식 안내",
                    "text": (f"{first.title}의 위치와 방문 정보를 안내합니다. " * 120),
                }
            if name == "upsert_place_insights":
                executed_writes.append(int(args["place_id"]))
                injected_documents.extend(args.get("_validated_source_documents") or [])
                marker = db.get(Marker, int(args["place_id"]))
                marker.description = (
                    f"{marker.title}은 선양 여행자가 방문할 수 있는 장소이며 위치, 역사, "
                    "운영 정보와 동선을 함께 확인할 수 있도록 충분히 정리된 한국어 설명입니다."
                )
                db.add_all([
                    PlaceInsight(
                        place_id=marker.id,
                        kind="location",
                        title="위치",
                        content="중심가에서 접근할 수 있습니다.",
                        source_url=source_url,
                    ),
                    PlaceInsight(
                        place_id=marker.id,
                        kind="visit",
                        title="방문",
                        content="방문 전 공식 안내를 확인합니다.",
                        source_url=source_url,
                    ),
                ])
                db.flush()
                db.expire(marker, ["insights"])
                return {"ok": True, "changed": 2, "place_id": marker.id}
            return run_tool(db, name, args, **kwargs)

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._select_autonomous_task", return_value=(task, "quality_or_backlog", False)),
            patch("app.agent.runner._sync_quality_tasks", return_value=[task.id]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=8,
                autonomous_research=True,
            )

        self.assertEqual(rounds, 2)
        self.assertEqual(executed_writes, [first.id])
        self.assertEqual(injected_documents[0]["url"], source_url)
        run = self.db.get(AgentRun, result["run_id"])
        metrics = json.loads(run.metrics)
        self.assertEqual(run.work_item_id, initial_item.id)
        self.assertEqual(metrics["work_item_id"], initial_item.id)
        self.assertEqual(metrics["next_work_item_id"], sibling.id)
        self.assertEqual(metrics["continuity"]["work_item_id"], sibling.id)
        self.assertEqual(active_work_item_for_mission(self.db, mission).id, sibling.id)
        write_step = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == run.id,
            AgentRunStep.tool == "upsert_place_insights",
        ).one()
        persisted_args = json.loads(write_step.detail)["args"]
        self.assertNotIn("_validated_source_documents", persisted_args)
        self.assertEqual(result["status"], "completed")

    def test_one_image_search_and_parallel_blocker_cannot_close_target(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        first = self.db.query(Marker).filter(Marker.city_id == 2).one()
        second = Marker(
            city_id=2,
            category=MarkerCategory.tourist,
            shape=MarkerShape.point,
            title="第二无图地点 (두 번째 무사진 장소)",
            description="한국어 설명",
            lat=41.82,
            lng=123.42,
        )
        self.db.add(second)
        self.db.flush()
        task = AgentTask(
            city_id=2,
            kind="quality_images",
            title="이미지 차단 커서 전환 테스트",
            detail=(
                "대상:\n"
                f"- #{first.id} {first.title} (현재: 사진 없음)\n"
                f"- #{second.id} {second.title} (현재: 사진 없음)"
            ),
            success_metric="장소별 정확한 사진 또는 감사 가능한 냉각 상태",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        mission, initial_item = ensure_mission_for_task(self.db, task)
        sibling = self.db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.id != initial_item.id,
        ).one()
        rounds = 0
        executed: list[tuple[str, int | None]] = []

        class Completions:
            def create(inner_self, **kwargs):
                nonlocal rounds
                rounds += 1
                task_schema = next(
                    tool["function"]["parameters"]
                    for tool in kwargs["tools"]
                    if tool["function"]["name"] == "upsert_agent_task"
                )
                self.assertEqual(task_schema["properties"]["task_id"]["enum"], [task.id])
                self.assertEqual(task_schema["properties"]["status"]["enum"], ["blocked"])
                if rounds == 1:
                    calls = [
                        SimpleNamespace(
                            id="exact-image-search-1",
                            function=SimpleNamespace(
                                name="search_place_images",
                                arguments=json.dumps({
                                    "place_id": first.id,
                                    "query": f"{first.title} exact exterior photo axis 1",
                                }),
                            ),
                        ),
                        SimpleNamespace(
                            id="premature-image-block",
                            function=SimpleNamespace(
                                name="upsert_agent_task",
                                arguments=json.dumps({
                                    "task_id": task.id,
                                    "status": "blocked",
                                    "result": "검색 한 번만 보고 자료가 없다고 성급히 판단",
                                }),
                            ),
                        ),
                    ]
                elif rounds in {2, 3}:
                    calls = [SimpleNamespace(
                        id=f"exact-image-search-{rounds}",
                        function=SimpleNamespace(
                            name="search_place_images",
                            arguments=json.dumps({
                                "place_id": first.id,
                                "query": f"{first.title} exact exterior photo axis {rounds}",
                            }),
                        ),
                    )]
                else:
                    self.fail("three clean exact searches must terminate without another provider round")
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=calls,
                ))])

        def fake_tool(db, name, args, **kwargs):
            executed.append((name, int(args["place_id"]) if args.get("place_id") is not None else None))
            if name == "search_place_images":
                return {"results": []}
            return run_tool(db, name, args, **kwargs)

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._select_autonomous_task", return_value=(task, "quality_or_backlog", False)),
            patch("app.agent.runner._sync_quality_tasks", return_value=[task.id]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=8,
                autonomous_research=True,
            )

        self.assertEqual(rounds, 3)
        self.assertEqual(
            [name for name, _place_id in executed],
            ["search_place_images", "search_place_images", "search_place_images"],
        )
        run = self.db.get(AgentRun, result["run_id"])
        metrics = json.loads(run.metrics)
        disposition = self.db.query(AgentQualityGapDisposition).filter(
            AgentQualityGapDisposition.place_id == first.id,
            AgentQualityGapDisposition.gap_kind == "image",
        ).one()
        self.assertEqual(disposition.status, "source_exhausted")
        self.assertEqual(
            json.loads(disposition.evidence_refs),
            [
                "run:%s:step:1" % run.id,
                "run:%s:step:3" % run.id,
                "run:%s:step:4" % run.id,
            ],
        )
        self.assertEqual(run.work_item_id, initial_item.id)
        self.assertEqual(metrics["work_item_id"], initial_item.id)
        self.assertEqual(metrics["next_work_item_id"], sibling.id)
        self.assertEqual(metrics["continuity"]["work_item_id"], sibling.id)
        self.assertEqual(active_work_item_for_mission(self.db, mission).id, sibling.id)
        self.assertEqual(
            self.db.query(AgentRunStep).filter(
                AgentRunStep.run_id == run.id,
                AgentRunStep.tool == "upsert_agent_task",
            ).count(),
            1,
        )
        rejected_block = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == run.id,
            AgentRunStep.tool == "upsert_agent_task",
        ).one()
        self.assertEqual(
            json.loads(rejected_block.detail)["result"]["error"],
            "quality_task_block_evidence_required",
        )
        self.assertEqual(result["status"], "completed")

    def test_third_clean_empty_image_search_records_source_exhausted_immediately(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="quality_images",
            title="사진 결손 전용 테스트",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 사진 없음)",
            success_metric="사진 후보 소진을 감사 가능하게 기록",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        rounds = 0

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                rounds += 1
                call = SimpleNamespace(
                    id=f"image-{rounds}",
                    function=SimpleNamespace(
                        name="search_place_images",
                        arguments=json.dumps({
                            "place_id": place.id,
                            "query": f"{place.title} exact photo source {rounds}",
                        }),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._select_autonomous_task", return_value=(task, "quality_or_backlog", False)),
            patch("app.agent.runner._sync_quality_tasks", return_value=[task.id]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", return_value={"results": []}),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        disposition = self.db.query(AgentQualityGapDisposition).filter(
            AgentQualityGapDisposition.place_id == place.id,
            AgentQualityGapDisposition.gap_kind == "image",
        ).one()
        self.assertEqual(disposition.status, "source_exhausted")
        self.assertEqual(len(json.loads(disposition.evidence_refs)), 3)
        self.assertEqual(result["status"], "completed")

    def test_image_warning_in_three_attempt_sample_records_blocked_not_exhausted(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="quality_images",
            title="사진 공급자 실패 전용 테스트",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 사진 없음)",
            success_metric="공급자 실패를 소진으로 오판하지 않음",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        rounds = 0

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                rounds += 1
                call = SimpleNamespace(
                    id=f"warning-image-{rounds}",
                    function=SimpleNamespace(
                        name="search_place_images",
                        arguments=json.dumps({
                            "place_id": place.id,
                            "query": f"{place.title} provider axis {rounds}",
                        }),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        image_results = [
            {"results": []},
            {"results": []},
            {"results": [], "warnings": ["openverse: timeout"]},
        ]

        def fake_tool(_db, name, _args, **_kwargs):
            self.assertEqual(name, "search_place_images")
            return image_results.pop(0)

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._select_autonomous_task", return_value=(task, "quality_or_backlog", False)),
            patch("app.agent.runner._sync_quality_tasks", return_value=[task.id]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        disposition = self.db.query(AgentQualityGapDisposition).filter(
            AgentQualityGapDisposition.place_id == place.id,
            AgentQualityGapDisposition.gap_kind == "image",
        ).one()
        self.assertEqual(disposition.status, "blocked")
        self.assertIsNotNone(disposition.retry_after)
        self.assertEqual(len(json.loads(disposition.evidence_refs)), 3)
        self.assertEqual(result["status"], "completed")

    def test_image_provider_errors_record_blocked_not_source_exhausted(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="quality_images",
            title="사진 공급자 오류 전용 테스트",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 사진 없음)",
            success_metric="공급자 오류를 소진으로 오판하지 않음",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        rounds = 0

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                rounds += 1
                call = SimpleNamespace(
                    id=f"failed-image-{rounds}",
                    function=SimpleNamespace(
                        name="search_place_images",
                        arguments=json.dumps({
                            "place_id": place.id,
                            "query": f"{place.title} failed provider {rounds}",
                        }),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._select_autonomous_task", return_value=(task, "quality_or_backlog", False)),
            patch("app.agent.runner._sync_quality_tasks", return_value=[task.id]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", return_value={"results": [], "error": "provider_timeout"}),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        disposition = self.db.query(AgentQualityGapDisposition).filter(
            AgentQualityGapDisposition.place_id == place.id,
            AgentQualityGapDisposition.gap_kind == "image",
        ).one()
        self.assertEqual(disposition.status, "blocked")
        self.assertIsNotNone(disposition.retry_after)
        self.assertNotEqual(disposition.status, "source_exhausted")
        self.assertEqual(result["status"], "completed")

    def test_image_candidates_keep_quality_target_open_without_terminal_disposition(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task = AgentTask(
            city_id=2,
            kind="quality_images",
            title="사진 후보 검토 전용 테스트",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 사진 없음)",
            success_metric="후보가 있으면 소진 또는 차단으로 닫지 않음",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        rounds = 0

        class Completions:
            def create(inner_self, **_kwargs):
                nonlocal rounds
                rounds += 1
                call = SimpleNamespace(
                    id=f"candidate-image-{rounds}",
                    function=SimpleNamespace(
                        name="search_place_images",
                        arguments=json.dumps({
                            "place_id": place.id,
                            "query": f"{place.title} candidate source {rounds}",
                        }),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._select_autonomous_task", return_value=(task, "quality_or_backlog", False)),
            patch("app.agent.runner._sync_quality_tasks", return_value=[task.id]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", return_value={
                "results": [{
                    "url": "https://images.example.test/candidate.jpg",
                    "source": "openverse",
                }],
            }),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        self.assertEqual(
            self.db.query(AgentQualityGapDisposition).filter(
                AgentQualityGapDisposition.place_id == place.id,
                AgentQualityGapDisposition.gap_kind == "image",
            ).count(),
            0,
        )
        self.db.refresh(task)
        self.assertEqual(task.status, "pending")
        mission = self.db.get(AgentMission, self.db.get(AgentRun, result["run_id"]).mission_id)
        self.assertEqual(mission.status, "active")

    def test_valid_zone_catalog_without_containing_polygon_records_waiver(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        outside_zone = Marker(
            city_id=2,
            category=MarkerCategory.other,
            shape=MarkerShape.polygon,
            title="유효하지만 멀리 있는 구역",
            description="테스트 구역",
            lat=40.0,
            lng=122.0,
            polygon=json.dumps([
                {"lat": 40.0, "lng": 122.0},
                {"lat": 40.0, "lng": 122.1},
                {"lat": 40.1, "lng": 122.1},
                {"lat": 40.1, "lng": 122.0},
            ]),
        )
        self.db.add(outside_zone)
        task = AgentTask(
            city_id=2,
            kind="quality_zones",
            title="구역 결손 전용 테스트",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 구역 미배정)",
            success_metric="현재 구역 전체에 불포함임을 감사 가능하게 기록",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()

        class Completions:
            def create(inner_self, **_kwargs):
                call = SimpleNamespace(
                    id="zones",
                    function=SimpleNamespace(name="list_zones", arguments="{}"),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )

        def fake_tool(db, name, args, **kwargs):
            return run_tool(db, name, args, **kwargs)

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._select_autonomous_task", return_value=(task, "quality_or_backlog", False)),
            patch("app.agent.runner._sync_quality_tasks", return_value=[task.id]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        disposition = self.db.query(AgentQualityGapDisposition).filter(
            AgentQualityGapDisposition.place_id == place.id,
            AgentQualityGapDisposition.gap_kind == "zone",
        ).one()
        self.assertEqual(disposition.status, "waived")
        self.assertEqual(len(json.loads(disposition.evidence_refs)), 1)
        self.assertEqual(result["status"], "completed")

    def test_malformed_zone_catalog_never_records_outside_waiver(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        malformed_zone = Marker(
            city_id=2,
            category=MarkerCategory.other,
            shape=MarkerShape.polygon,
            title="손상된 구역",
            description="테스트 구역",
            lat=41.7,
            lng=123.3,
            polygon="{broken-json",
        )
        self.db.add(malformed_zone)
        task = AgentTask(
            city_id=2,
            kind="quality_zones",
            title="손상 구역 결손 테스트",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 구역 미배정)",
            success_metric="손상된 구역으로 면제하지 않음",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        observed = run_tool(self.db, "list_zones", {}, city_id=2)
        malformed_row = next(row for row in observed if row["id"] == malformed_zone.id)
        self.assertIsNone(malformed_row["polygon"])
        self.assertEqual(malformed_row["polygon_status"], "invalid")
        self.assertEqual(malformed_row["polygon_error"], "malformed_polygon_json")

        class Completions:
            def create(inner_self, **_kwargs):
                call = SimpleNamespace(
                    id="malformed-zones",
                    function=SimpleNamespace(name="list_zones", arguments="{}"),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )

        def fake_tool(db, name, args, **kwargs):
            return run_tool(db, name, args, **kwargs)

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._select_autonomous_task", return_value=(task, "quality_or_backlog", False)),
            patch("app.agent.runner._sync_quality_tasks", return_value=[task.id]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        disposition = self.db.query(AgentQualityGapDisposition).filter(
            AgentQualityGapDisposition.place_id == place.id,
            AgentQualityGapDisposition.gap_kind == "zone",
        ).one()
        self.assertEqual(disposition.status, "blocked")
        self.assertIsNotNone(disposition.retry_after)
        self.assertEqual(result["status"], "completed")

    def test_assigning_against_malformed_zone_catalog_records_blocked_cooldown(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        malformed_zone = Marker(
            city_id=2,
            category=MarkerCategory.other,
            shape=MarkerShape.polygon,
            title="퇴화된 구역",
            description="테스트 구역",
            lat=place.lat,
            lng=place.lng,
            polygon=json.dumps([
                {"lat": place.lat, "lng": place.lng},
                {"lat": place.lat, "lng": place.lng},
                {"lat": place.lat, "lng": place.lng},
            ]),
        )
        self.db.add(malformed_zone)
        self.db.flush()
        task = AgentTask(
            city_id=2,
            kind="quality_zones",
            title="손상 구역 배정 경로 테스트",
            detail=f"대상:\n- #{place.id} {place.title} (현재: 구역 미배정)",
            success_metric="손상 카탈로그에서 배정을 추측하지 않음",
            priority=100,
            status="pending",
        )
        self.db.add(task)
        self.db.commit()

        class Completions:
            def create(inner_self, **_kwargs):
                call = SimpleNamespace(
                    id="assign-malformed-zone",
                    function=SimpleNamespace(
                        name="assign_place_zone",
                        arguments=json.dumps({
                            "place_id": place.id,
                            "zone_id": malformed_zone.id,
                            "reason": "좌표 포함 여부 확인",
                        }),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )

        def fake_tool(db, name, args, **kwargs):
            return run_tool(db, name, args, **kwargs)

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._select_autonomous_task", return_value=(task, "quality_or_backlog", False)),
            patch("app.agent.runner._sync_quality_tasks", return_value=[task.id]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        disposition = self.db.query(AgentQualityGapDisposition).filter(
            AgentQualityGapDisposition.place_id == place.id,
            AgentQualityGapDisposition.gap_kind == "zone",
        ).one()
        self.assertEqual(disposition.status, "blocked")
        self.assertIsNotNone(disposition.retry_after)
        self.assertEqual(result["status"], "completed")

    def test_discovery_contract_matches_the_actual_provider_tool_list(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        self.db.commit()
        requests: list[dict] = []

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="후보 검증을 다음 조각으로 인계", tool_calls=[],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(requests), 1)
        advertised = {
            item["function"]["name"] for item in requests[0]["tools"]
        }
        self.assertEqual(advertised, set(CANDIDATE_DISCOVERY_TOOLS))
        self.assertTrue({
            "list_places", "list_research_history", "web_search", "fetch_page",
            "geocode_place", "propose_place",
        }.issubset(advertised))
        task_tool = next(
            item for item in requests[0]["tools"]
            if item["function"]["name"] == "upsert_agent_task"
        )
        task_schema = task_tool["function"]["parameters"]
        self.assertEqual(task_schema["required"], ["task_id", "status", "result"])
        run = self.db.get(AgentRun, result["run_id"])
        task_id = json.loads(run.metrics)["primary_task_id"]
        self.assertEqual(task_schema["properties"]["task_id"]["enum"], [task_id])
        self.assertEqual(task_schema["properties"]["status"]["enum"], ["blocked"])
        self.assertNotIn("title", task_schema["properties"])
        self.assertFalse(task_schema["additionalProperties"])
        prompt = json.dumps(requests[0]["messages"], ensure_ascii=False)
        for unavailable in ("create_place", "merge_places", "attach_image_from_url"):
            self.assertNotIn(unavailable, prompt)

    def test_propose_place_schema_advertises_its_runtime_required_fields(self) -> None:
        proposal = next(
            item for item in TOOLS
            if item["function"]["name"] == "propose_place"
        )
        schema = proposal["function"]["parameters"]
        required = set(schema["required"])

        self.assertTrue({
            "title", "description", "lat", "lng", "travel_role",
            "evidence", "source_urls", "confidence", "insights",
        }.issubset(required))
        self.assertEqual(schema["properties"]["description"]["type"], "string")
        self.assertEqual(schema["properties"]["description"]["minLength"], 60)
        self.assertEqual(schema["properties"]["travel_role"]["type"], "string")
        self.assertEqual(schema["properties"]["source_urls"]["minItems"], 1)

        # The compatibility/direct-create contract remains permissive.
        create = next(
            item for item in TOOLS
            if item["function"]["name"] == "create_place"
        )
        self.assertEqual(
            create["function"]["parameters"]["properties"]["description"]["type"],
            ["string", "null"],
        )

    def test_brave_place_candidates_are_transient_in_run_history(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        self.db.commit()
        raw_result = {
            "results": [{
                "title": "보존 가능한 일반 웹 문서",
                "href": "https://example.test/regular-source",
                "body": "일반 웹 검색 본문",
                "seen": False,
            }],
            "place_candidates": [{
                "display_name": "TRANSIENT_SECRET_PLACE",
                "address": "TRANSIENT_SECRET_ADDRESS",
                "lat": 41.81234,
                "lng": 123.45678,
                "transient_id": "TRANSIENT_SECRET_ID",
                "source": "brave_place",
                "storage_allowed": False,
                "requires_cross_verification": True,
            }],
            "provider_attempts": [{
                "provider": "brave_place",
                "status": "ok",
                "result_count": 1,
            }],
        }

        class Completions:
            def create(inner_self, **_kwargs):
                call = SimpleNamespace(
                    id="transient-brave-search",
                    function=SimpleNamespace(
                        name="web_search",
                        arguments=json.dumps({"query": "沈阳 新咖啡店 transient test"}),
                    ),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", return_value=raw_result),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        step = self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == result["run_id"],
            AgentRunStep.tool == "web_search",
        ).one()
        persisted = step.detail
        self.assertIn("https://example.test/regular-source", persisted)
        self.assertIn('"status": "transient_discarded"', persisted)
        self.assertNotIn('"result_count": 1', persisted)
        for forbidden in (
            "TRANSIENT_SECRET_PLACE", "TRANSIENT_SECRET_ADDRESS",
            "TRANSIENT_SECRET_ID", "41.81234", "123.45678",
        ):
            self.assertNotIn(forbidden, persisted)
        evidence_blob = " ".join(
            str(value or "")
            for row in self.db.query(AgentEvidence).filter(
                AgentEvidence.run_id == result["run_id"]
            ).all()
            for value in (row.url, row.title, row.claim, row.excerpt)
        )
        self.assertIn("https://example.test/regular-source", evidence_blob)
        self.assertNotIn("TRANSIENT_SECRET", evidence_blob)

    def test_brave_lead_echo_is_live_but_not_persisted_across_two_rounds(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        self.db.commit()
        secret = "BRAVE_ONLY_MOYO_SECRET_ADDRESS"
        requests: list[dict] = []

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                round_no = len(requests)
                if round_no == 1:
                    call = SimpleNamespace(
                        id="search",
                        function=SimpleNamespace(
                            name="web_search",
                            arguments=json.dumps({"query": "沈阳 饮料 新店"}),
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                if round_no == 2:
                    call = SimpleNamespace(
                        id="verify",
                        function=SimpleNamespace(
                            name="geocode_place",
                            arguments=json.dumps({"query": secret}),
                        ),
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="", tool_calls=[call],
                    ))])
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content=f"검증 실패: {secret}", tool_calls=[],
                ))])

        raw_search = {
            "results": [],
            "place_candidates": [{
                "display_name": "BRAVE_ONLY_MOYO_SECRET",
                "address": secret,
                "lat": 41.81234,
                "lng": 123.45678,
                "source": "brave_place",
                "storage_allowed": False,
            }],
            "provider_attempts": [{
                "provider": "brave_place", "status": "ok", "result_count": 1,
            }],
        }

        def fake_tool(_db, name, _args, **_kwargs):
            if name == "web_search":
                return raw_search
            if name == "geocode_place":
                return {"results": []}
            return {"ok": True}

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=3,
                autonomous_research=True,
            )

        self.assertIn(secret, json.dumps(requests[1]["messages"], ensure_ascii=False))
        durable = []
        durable.extend(row.detail or "" for row in self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == result["run_id"]
        ))
        durable.extend(
            " ".join((
                row.state_summary or "",
                row.decision or "",
                row.new_facts or "",
                row.rejected_claims or "",
                row.failed_approaches or "",
                row.next_action or "",
            ))
            for row in self.db.query(AgentCheckpoint).filter(
                AgentCheckpoint.run_id == result["run_id"]
            )
        )
        run = self.db.get(AgentRun, result["run_id"])
        durable.extend([run.summary or "", run.metrics or ""])
        task = self.db.get(AgentTask, json.loads(run.metrics)["primary_task_id"])
        durable.append(task.result or "")
        durable_blob = "\n".join(durable)
        self.assertNotIn(secret, durable_blob)
        self.assertNotIn("BRAVE_ONLY_MOYO_SECRET", durable_blob)
        self.assertIn("BRAVE_TRANSIENT_QUERY_DISCARDED", durable_blob)

    def test_transient_candidate_proposal_uses_only_server_canonical_evidence(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        self.db.commit()
        brave_url = "https://brave-only.example/ephemeral-poi"
        independent_url = "https://independent.example/shenyang-tea-house"
        forbidden = (
            "BRAVE_EPHEMERAL_ID",
            brave_url,
            "MODEL_ONLY_TITLE",
            "MODEL_ONLY_ADDRESS",
            "MODEL_PARAPHRASED_BRAVE_FACT",
            "MODEL_ONLY_SOURCE",
        )
        calls = [
            ("web_search", {"query": "沈阳 新茶馆"}),
            ("fetch_page", {"url": brave_url}),
            ("web_search", {"query": "沈阳 中街 独立茶馆 官方 地址"}),
            ("fetch_page", {"url": independent_url}),
            ("geocode_place", {"query": "沈阳 独立茶馆 中街路88号"}),
            ("propose_place", {
                "title": "独立茶馆 (MODEL_ONLY_TITLE 모델명)",
                "description": "MODEL_PARAPHRASED_BRAVE_FACT를 모델이 다시 쓴 설명",
                "address": "MODEL_ONLY_ADDRESS",
                "category": "lodging",
                "travel_role": "rest",
                "lat": 41.80125,
                "lng": 123.45215,
                "coordinate_source": "MODEL_ONLY_SOURCE",
                "coordinate_source_url": brave_url,
                "source_urls": [brave_url, "https://model-only.example/source"],
                "evidence": "MODEL_PARAPHRASED_BRAVE_FACT 근거",
                "confidence": 0.99,
                "insights": [
                    {
                        "kind": "tip",
                        "title": "모델 임의 팁",
                        "content": "MODEL_PARAPHRASED_BRAVE_FACT 팁입니다.",
                        "source_url": brave_url,
                    },
                    {
                        "kind": "visit",
                        "title": "모델 임의 방문법",
                        "content": "MODEL_PARAPHRASED_BRAVE_FACT 방문법입니다.",
                        "source_url": brave_url,
                    },
                ],
            }),
        ]
        requests: list[dict] = []
        request_flags: list[tuple[str, dict]] = []

        class Completions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                index = len(requests) - 1
                if index >= len(calls):
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="서버 검증 제안을 완료했습니다.", tool_calls=[],
                    ))])
                name, arguments = calls[index]
                call = SimpleNamespace(
                    id=f"canonical-{index}",
                    function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="", tool_calls=[call],
                ))])

        initial_search = {
            "results": [],
            "place_candidates": [{
                "display_name": "独立茶馆",
                "address": "沈河区中街路88号",
                "lat": 41.8012,
                "lng": 123.4521,
                "source_url": brave_url,
                "transient_id": "BRAVE_EPHEMERAL_ID",
                "source": "brave_place",
                "storage_allowed": False,
            }],
            "provider_attempts": [{
                "provider": "brave_place", "status": "ok", "result_count": 1,
            }],
        }
        independent_search = {
            "results": [{
                "title": "独立茶馆地址与到访信息",
                "href": independent_url,
                "body": "沈阳独立茶馆位于沈河区中街路88号",
                "seen": False,
            }],
            "place_candidates": [],
            "provider_attempts": [],
        }
        search_count = 0

        def fake_extract(url):
            return {
                "title": "独立茶馆公开页面",
                "text": (
                    "独立茶馆位于沈河区中街路88号，页面提供茶饮门店地址与到访信息。"
                    * 10
                ),
                "coordinate_candidates": [],
            }

        def fake_tool(db, name, args, **kwargs):
            nonlocal search_count
            request_flags.append((name, dict(kwargs)))
            if name == "web_search":
                search_count += 1
                return initial_search if search_count == 1 else independent_search
            if name == "geocode_place":
                return {"results": [{
                    "display_name": "独立茶馆, 沈河区中街路88号",
                    "address": "沈河区中街路88号",
                    "lat": 41.8012,
                    "lng": 123.4521,
                    "type": "cafe",
                    "source": "nominatim",
                    "source_url": "https://www.openstreetmap.org/node/12345",
                    "external_id": "node/12345",
                    "confidence": 0.9,
                    "storage_allowed": True,
                }]}
            return run_tool(db, name, args, **kwargs)

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        curated_args = {
            "title": "独立茶馆 (두리 차관)",
            "description": (
                "중제에서 차와 가벼운 휴식을 즐길 수 있는 독립 찻집으로, 공개 본문과 "
                "저장 가능한 지오코딩에서 같은 상호와 지점을 확인했습니다."
            ),
            "address": "沈河区中街路88号",
            "category": "drink",
            "travel_role": "rest",
            "lat": 41.8012,
            "lng": 123.4521,
            "evidence": "독립 공개 본문과 좌표 근거가 같은 찻집을 가리킵니다.",
            "source_urls": [independent_url],
            "confidence": 0.9,
            "insights": [
                {
                    "kind": "location",
                    "title": "위치",
                    "content": "沈河区中街路88号에 있는 독립 찻집입니다.",
                    "source_url": independent_url,
                    "confidence": 0.9,
                },
                {
                    "kind": "visit",
                    "title": "방문 성격",
                    "content": "중제 동선에서 차를 마시며 쉬어 가기 좋은 곳입니다.",
                    "source_url": independent_url,
                    "confidence": 0.8,
                },
            ],
        }
        role_task = AgentTask(
            city_id=2,
            kind=CANDIDATE_DISCOVERY_KIND,
            title="자동 신규 장소 발굴 [휴식]: 선양",
            detail="target_role: rest\n선양에서 휴식 역할 후보만 탐색",
            success_metric="travel_role=rest인 검증 후보 제안",
            priority=88,
            status="pending",
        )
        self.db.add(role_task)
        self.db.commit()
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch("app.agent.runner.settings.agent_allow_auto_create", False),
            patch("app.agent.runner._sync_quality_tasks", return_value=[]),
            patch(
                "app.agent.runner._select_autonomous_task",
                return_value=(role_task, CANDIDATE_DISCOVERY_KIND, True),
            ),
            patch("app.agent.runner._ensure_gap_tasks", return_value=[]),
            patch("app.agent.runner.run_tool", side_effect=fake_tool),
            patch("app.agent.tools._extract_page_text", side_effect=fake_extract),
            patch(
                "app.agent.runner.curate_grounded_candidate",
                return_value={"ok": True, "args": curated_args},
            ) as curator,
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=len(calls),
                autonomous_research=True,
            )

        self.assertTrue(result["ok"], result)
        proposal = self.db.query(AgentProposal).one()
        payload = json.loads(proposal.payload)
        self.assertEqual(proposal.title, "独立茶馆 (두리 차관)")
        self.assertEqual(payload["address"], "沈河区中街路88号")
        self.assertEqual((payload["lat"], payload["lng"]), (41.8012, 123.4521))
        self.assertEqual(payload["category"], "drink")
        self.assertEqual(payload["travel_role"], "rest")
        self.assertEqual(curator.call_args.kwargs["target_role"], "rest")
        self.assertEqual(payload["coordinate_source"], "nominatim")
        self.assertEqual(json.loads(proposal.source_urls), [independent_url])
        self.assertEqual(self.db.query(AgentWebVisit).count(), 0)
        fetch_flags = [flags for name, flags in request_flags if name == "fetch_page"]
        self.assertEqual(len(fetch_flags), 2)
        self.assertTrue(all(flags.get("server_record_web_visit") is False for flags in fetch_flags))

        durable_parts = [proposal.title, proposal.payload, proposal.evidence, proposal.source_urls]
        durable_parts.extend(row.detail or "" for row in self.db.query(AgentRunStep).filter(
            AgentRunStep.run_id == result["run_id"]
        ))
        durable_parts.extend(row.query or "" for row in self.db.query(AgentSearchLog).all())
        run = self.db.get(AgentRun, result["run_id"])
        durable_parts.extend((run.summary or "", run.metrics or ""))
        durable_blob = "\n".join(durable_parts)
        for value in forbidden:
            self.assertNotIn(value, durable_blob)
        self.assertIn(independent_url, durable_blob)

    def test_cooled_paused_quality_mission_rejoins_fair_schedule(self) -> None:
        recent_discovery_task = AgentTask(
            city_id=2,
            kind=CANDIDATE_DISCOVERY_KIND,
            title="직전 발굴",
            status="completed",
        )
        active_task = AgentTask(
            city_id=2,
            kind="quality_images",
            title="자주 실행된 사진 과제",
            attempts=5,
            priority=100,
            status="pending",
        )
        cooled_task = AgentTask(
            city_id=2,
            kind="quality_information",
            title="냉각을 마친 정보 과제",
            attempts=1,
            priority=80,
            status="pending",
        )
        self.db.add_all([recent_discovery_task, active_task, cooled_task])
        self.db.flush()
        discovery_mission = AgentMission(
            city_id=2,
            task_id=recent_discovery_task.id,
            kind=CANDIDATE_DISCOVERY_KIND,
            title=recent_discovery_task.title,
            status="completed",
        )
        active_mission = AgentMission(
            city_id=2,
            task_id=active_task.id,
            kind=active_task.kind,
            title=active_task.title,
            status="active",
        )
        cooled_mission = AgentMission(
            city_id=2,
            task_id=cooled_task.id,
            kind=cooled_task.kind,
            title=cooled_task.title,
            status="paused",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=13),
        )
        self.db.add_all([discovery_mission, active_mission, cooled_mission])
        self.db.flush()
        self.db.add(AgentRun(
            city_id=2,
            mission_id=discovery_mission.id,
            mode="research",
            status="completed",
        ))
        self.db.commit()

        self.assertEqual(_fair_non_discovery_task(self.db, city_id=2).id, cooled_task.id)
        chosen, lane, reserved = _select_autonomous_task(
            self.db,
            city=self.db.get(City, 2),
        )
        self.assertEqual(chosen.id, cooled_task.id)
        self.assertEqual(lane, "quality_or_backlog")
        self.assertFalse(reserved)

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
        self.assertTrue(duplicate["ok"])
        self.assertEqual(duplicate["task_id"], image_task.id)
        self.assertIn("자유 라이선스", image_task.result)
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

    def test_batch_uses_discovery_when_every_quality_task_is_cooling_down(self) -> None:
        for event in self.db.query(PlaceEvent).all():
            event.groq_read_at = datetime.now(timezone.utc)
        task_ids = _sync_quality_tasks(self.db, city_id=2, run_id=1)
        for task_id in task_ids:
            task = self.db.get(AgentTask, task_id)
            self.db.add(AgentMission(
                city_id=2,
                task_id=task.id,
                kind=task.kind,
                title=task.title,
                status="paused",
                progress=json.dumps({"retry_condition": "cooldown"}),
            ))
        self.db.commit()

        class DiscoveryCompletions:
            def create(inner_self, **_kwargs):
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="발굴 후보를 다음 실행에서 계속 검증", tool_calls=[],
                ))])

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=DiscoveryCompletions())
        )
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
        ):
            result = run_agent(
                self.db,
                city_id=2,
                max_steps=1,
                autonomous_research=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"], 1)
        run = self.db.query(AgentRun).order_by(AgentRun.id.desc()).first()
        self.assertEqual(run.mode, "research")
        self.assertEqual(json.loads(run.metrics)["lane"], "candidate_discovery")
        self.assertEqual(self.db.get(AgentMission, run.mission_id).kind, "candidate_discovery")

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
        duplicate_fact = dict(insight)
        duplicate_fact.update({
            "title": "연락처",
            "content": "문의 024-96833, 주차 024-89398383",
        })
        first_phone_fact = dict(insight)
        first_phone_fact.update({
            "title": "전화번호",
            "content": "주차 024-89398383, 문의 024-96833",
        })
        run_tool(
            self.db,
            "upsert_place_insights",
            {
                "place_id": place.id,
                "insights": [first_phone_fact, duplicate_fact],
                "_validated_source_urls": ["https://example.test/place-guide"],
            },
            city_id=2,
        )
        phone_rows = self.db.query(PlaceInsight).filter(
            PlaceInsight.place_id == place.id,
            PlaceInsight.source_url == "https://example.test/place-guide",
            PlaceInsight.content.like("%024-%"),
        ).all()
        self.assertEqual(len(phone_rows), 1)

    def test_place_insights_reject_placeholder_and_unsupported_precise_facts(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        place.title = "鸣记脆皮烤鱼 (밍지 바삭 구이생선)"
        place.branch_name = "辽大店"
        place.description = "辽大路8号에 있는 지점"
        place.coordinate_query = "鸣记脆皮烤鱼 辽大店 辽大路8号"
        self.db.commit()
        source_url = "https://example.test/liaoda-branch"
        source_document = {
            "url": source_url,
            "title": "鸣记脆皮烤鱼(辽大店)",
            "text": "鸣记脆皮烤鱼辽大店位于辽大路8号，每日11:00营业。",
        }
        base = {
            "place_id": place.id,
            "_validated_source_urls": [source_url],
            "_validated_source_documents": [source_document],
        }

        placeholder = run_tool(
            self.db,
            "upsert_place_insights",
            {
                **base,
                "insights": [{
                    "kind": "location",
                    "title": "연락처",
                    "content": "주소는 辽大路8号이며 전화는 024-xxxxxxx입니다.",
                    "source_url": source_url,
                    "confidence": 0.9,
                }],
            },
            city_id=2,
        )
        self.assertEqual(placeholder["error"], "insight_placeholder_forbidden")

        unsupported = run_tool(
            self.db,
            "upsert_place_insights",
            {
                **base,
                "insights": [{
                    "kind": "visit",
                    "title": "다른 지점 정보",
                    "content": "辽宁省沈阳市大悦城A馆4楼에 있으며 22시까지 영업합니다.",
                    "source_url": source_url,
                    "confidence": 0.9,
                }],
            },
            city_id=2,
        )
        self.assertEqual(unsupported["error"], "insight_claim_not_supported")

        supported = run_tool(
            self.db,
            "upsert_place_insights",
            {
                **base,
                "insights": [{
                    "kind": "visit",
                    "title": "확인된 방문 정보",
                    "content": "辽大路8号에 있으며 11:00부터 영업합니다.",
                    "source_url": source_url,
                    "source_title": "모델이 임의로 쓴 출처 제목",
                    "confidence": 0.9,
                }],
            },
            city_id=2,
        )
        self.assertEqual(supported["changed"], 1)
        stored = self.db.query(PlaceInsight).filter(
            PlaceInsight.place_id == place.id,
            PlaceInsight.title == "확인된 방문 정보",
        ).one()
        self.assertEqual(stored.source_title, source_document["title"])

    def test_branch_claim_cannot_spoof_source_title_or_borrow_matching_hours(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        place.title = "鸣记脆皮烤鱼 (밍지 바삭 구이생선)"
        place.branch_name = "沈阳大悦城店"
        place.description = "大悦城A馆4楼에 있는 지점"
        place.coordinate_query = "鸣记脆皮烤鱼 沈阳大悦城店 大悦城A馆4楼"
        self.db.commit()
        source_url = "https://example.test/liaoda-branch"
        base = {
            "place_id": place.id,
            "_validated_source_urls": [source_url],
            "_validated_source_documents": [{
                "url": source_url,
                "title": "鸣记脆皮烤鱼(辽大店)",
                "text": "鸣记脆皮烤鱼辽大店位于辽大路8号，每日11:00营业。",
            }],
        }

        borrowed_hours = run_tool(
            self.db,
            "upsert_place_insights",
            {
                **base,
                "insights": [{
                    "kind": "visit",
                    "title": "영업시간",
                    "content": "이 지점은 매일 11:00부터 영업합니다.",
                    "source_url": source_url,
                    # Model-authored metadata must not override the fetched title.
                    "source_title": "鸣记脆皮烤鱼(沈阳大悦城店)",
                    "confidence": 0.9,
                }],
            },
            city_id=2,
        )
        self.assertEqual(borrowed_hours["error"], "insight_claim_context_mismatch")

        translated_wrong_location = run_tool(
            self.db,
            "upsert_place_insights",
            {
                **{
                    **base,
                    "_validated_source_documents": [{
                        "url": source_url,
                        "title": "鸣记脆皮烤鱼(辽大店)",
                        "text": (
                            "鸣记脆皮烤鱼辽大店位于辽大商场A馆4层，"
                            "每日11:00营业。"
                        ),
                    }],
                },
                "insights": [{
                    "kind": "location",
                    "title": "위치",
                    "content": "다웨청 A관 4층에 있으며 11:00부터 영업합니다.",
                    "source_url": source_url,
                    "source_title": "鸣记脆皮烤鱼(沈阳大悦城店)",
                    "confidence": 0.9,
                }],
            },
            city_id=2,
        )
        self.assertEqual(translated_wrong_location["error"], "insight_claim_context_mismatch")

    def test_supported_korean_building_translation_matches_chinese_structure(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        place.title = "鸣记脆皮烤鱼 (밍지 바삭 구이생선)"
        place.branch_name = "沈阳大悦城店"
        place.description = "大悦城A馆4楼에 있는 지점"
        place.coordinate_query = "鸣记脆皮烤鱼 沈阳大悦城店 大悦城A馆4楼"
        self.db.commit()
        for index, source_floor in enumerate(("4层", "4楼"), start=1):
            with self.subTest(source_floor=source_floor):
                source_url = f"https://example.test/dayuecheng-branch-{index}"
                result = run_tool(
                    self.db,
                    "upsert_place_insights",
                    {
                        "place_id": place.id,
                        "insights": [{
                            "kind": "location",
                            "title": f"매장 위치 {index}",
                            "content": "다웨청 A관 4층에 있으며 매일 11:00부터 영업합니다.",
                            "source_url": source_url,
                            "confidence": 0.9,
                        }],
                        "_validated_source_urls": [source_url],
                        "_validated_source_documents": [{
                            "url": source_url,
                            "title": "鸣记脆皮烤鱼(沈阳大悦城店)",
                            "text": (
                                "鸣记脆皮烤鱼沈阳大悦城店位于大悦城A馆"
                                f"{source_floor}，每日11:00营业。"
                            ),
                        }],
                    },
                    city_id=2,
                )

                self.assertEqual(result["changed"], 1)

    def test_official_hotel_name_and_exact_address_can_replace_missing_branch_token(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        place.title = "瀋陽中街故宮漫心酒店 (선양 중제 고궁 만신 호텔)"
        place.branch_name = "中街故宫店"
        place.description = "주소: 北中街路118号에 있는 숙소"
        place.coordinate_query = "瀋陽中街故宮漫心酒店 北中街路118号"
        self.db.commit()
        source_url = "https://hotel.example.test/manxin"

        result = run_tool(
            self.db,
            "upsert_place_insights",
            {
                "place_id": place.id,
                "insights": [{
                    "kind": "visit",
                    "title": "체크인과 위치",
                    "content": "北中街路118号에 있으며 체크인은 14:00부터입니다.",
                    "source_url": source_url,
                    "confidence": 0.9,
                }],
                "_validated_source_urls": [source_url],
                "_validated_source_documents": [{
                    "url": source_url,
                    "title": "沈阳中街故宫漫心酒店",
                    "text": "沈阳中街故宫漫心酒店位于北中街路118号，入住时间为14:00。",
                }],
            },
            city_id=2,
        )

        self.assertEqual(result["changed"], 1)

    def test_exact_name_large_public_feature_matches_across_representative_points(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        place.title = "民生大街 (민생대가)"
        place.category = MarkerCategory.tourist
        place.lat = 41.8000
        place.lng = 123.4500
        place.branch_name = ""
        self.db.commit()

        duplicate = _matching_existing_place(
            self.db,
            city_id=2,
            title="民生大街 (산책 거리)",
            lat=41.8028,
            lng=123.4500,
            category="tourist",
        )
        self.assertEqual(duplicate.id, place.id)

        branch_sensitive = _matching_existing_place(
            self.db,
            city_id=2,
            title="民生大街 (산책 거리)",
            lat=41.8028,
            lng=123.4500,
            category="restaurant",
        )
        self.assertIsNone(branch_sensitive)

    def test_branch_insight_rejects_brand_general_source(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        place.branch_name = "沈阳大悦城店"
        self.db.commit()
        source_url = "https://baike.example.test/item/brand"
        result = run_tool(
            self.db,
            "upsert_place_insights",
            {
                "place_id": place.id,
                "insights": [{
                    "kind": "tip",
                    "title": "영업시간",
                    "content": "매일 24시간 영업한다고 안내되어 있습니다.",
                    "source_url": source_url,
                    "source_title": "브랜드 일반 소개",
                    "confidence": 0.8,
                }],
                "_validated_source_urls": [source_url],
            },
            city_id=2,
        )
        self.assertEqual(result["error"], "insight_branch_source_mismatch")

    def test_image_attachment_rejects_nearby_subject(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        with patch("app.agent.tools.storage.s3_enabled", return_value=True):
            result = run_tool(
                self.db,
                "attach_image_from_url",
                {
                    "place_id": place.id,
                    "image_url": "https://upload.wikimedia.org/nearby-building.jpg",
                    "source": "Wikimedia Commons - nearby parking garage",
                },
                city_id=2,
            )
        self.assertEqual(result["error"], "image_source_subject_mismatch")

    def test_managed_quality_task_cannot_be_reclassified_by_agent(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        task_id = _sync_quality_tasks(self.db, city_id=2)[0]
        task = self.db.get(AgentTask, task_id)
        original = (task.kind, task.title, task.detail, task.success_metric, task.priority)
        result = run_tool(
            self.db,
            "upsert_agent_task",
            {
                "task_id": task.id,
                "kind": "research",
                "title": "renamed",
                "detail": f"장소 #{place.id}의 출처가 막혀 다음 실행에서 재검증 필요",
                "success_metric": "changed metric",
                "priority": 1,
                "status": "pending",
            },
            city_id=2,
        )
        self.db.refresh(task)
        self.assertTrue(result["changed"])
        self.assertEqual(
            (task.kind, task.title, task.detail, task.success_metric, task.priority),
            original,
        )
        self.assertIn("다음 실행", task.result)

    def test_managed_quality_completion_cannot_sever_or_duplicate_active_mission(self) -> None:
        image_task_id = next(
            task_id
            for task_id in _sync_quality_tasks(self.db, city_id=2)
            if self.db.get(AgentTask, task_id).kind == "quality_images"
        )
        image_task = self.db.get(AgentTask, image_task_id)
        mission, item = ensure_mission_for_task(self.db, image_task)

        completion = run_tool(
            self.db,
            "upsert_agent_task",
            {
                "task_id": image_task.id,
                "title": image_task.title,
                "status": "completed",
                "result": "사진을 찾지 못했으므로 완료",
            },
            city_id=2,
        )
        self.db.refresh(image_task)
        self.assertEqual(image_task.status, "pending")
        self.assertTrue(completion["requested_status_ignored"])

        # Reproduce the historical bad state and prove synchronization chooses
        # the task that owns the durable cursor instead of the newer duplicate.
        image_task.status = "completed"
        self.db.commit()
        deduplicated = run_tool(
            self.db,
            "upsert_agent_task",
            {
                "kind": "quality_images",
                "title": "another image task",
                "status": "pending",
            },
            city_id=2,
        )
        self.assertEqual(deduplicated["error"], "quality_gap_already_tracked")
        self.assertEqual(deduplicated["task_id"], image_task.id)

        duplicate = AgentTask(
            city_id=2,
            kind="quality_images",
            title="duplicate image task",
            status="pending",
        )
        self.db.add(duplicate)
        self.db.commit()
        canonical = _canonical_quality_task(
            self.db,
            city_id=2,
            kind="quality_images",
            now=datetime.now(timezone.utc),
        )
        self.db.commit()

        self.assertEqual(canonical.id, image_task.id)
        self.assertEqual(duplicate.status, "completed")
        self.assertEqual(mission.status, "active")
        self.assertEqual(item.status, "active")

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
                "coordinate_source": "official_detail",
                "coordinate_source_url": "https://example.org/official",
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
                "description": (
                    "청 황실 능원과 넓은 공원을 한 동선에서 둘러볼 수 있는 선양 북부의 역사 명소로, "
                    "도심 관광 사이에 산책과 청대 문화 이해를 함께 하기 좋은 장소입니다."
                ),
                "category": "tourist",
                "lat": 41.85,
                "lng": 123.43,
                "coordinate_source": "official_detail",
                "coordinate_source_url": "https://example.org/beiling",
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

    def test_ordinary_proposal_rejects_generic_korean_place_type(self) -> None:
        result = run_tool(
            self.db,
            "propose_place",
            {
                "title": "辽宁省博物馆 (박물관)",
                "description": (
                    "랴오닝 지역의 역사와 문화를 살펴볼 수 있는 전시 공간으로, 선양 여행에서 시대별 "
                    "유물을 비교하고 지역 배경을 이해하기 위해 방문할 수 있는 장소입니다."
                ),
                "category": "tourist",
                "lat": 41.68,
                "lng": 123.46,
                "source_urls": ["https://example.test/museum"],
                "insights": [],
            },
            city_id=2,
        )

        self.assertEqual(result["error"], "specific_korean_name_required")

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
            "description": (
                "청 황실 능원과 넓은 공원을 한 동선에서 둘러볼 수 있는 선양 북부의 역사 명소로, "
                "도심 관광 사이에 산책과 청대 문화 이해를 함께 하기 좋은 장소입니다."
            ),
            "category": "tourist",
            "lat": 41.85,
            "lng": 123.43,
            "coordinate_source": "official_detail",
            "coordinate_source_url": "https://example.org/beiling",
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

    def test_existing_marker_blocks_seo_title_duplicate_proposal(self) -> None:
        existing = Marker(
            city_id=2,
            category=MarkerCategory.restaurant,
            shape=MarkerShape.point,
            title="必吃！鸣记脆皮烤鱼，香辣酸甜一网打尽",
            description="기존 대화 후보입니다.",
            lat=41.8007,
            lng=123.4637,
        )
        self.db.add(existing)
        self.db.commit()

        result = run_tool(
            self.db,
            "propose_place",
            {
                "title": "鸣记脆皮烤鱼 (밍지 바삭 구이생선)",
                # Duplicate identity must short-circuit before proposal prose
                # quality gates; no new row will store this thin description.
                "description": "주소: 辽宁省沈阳市大东区小东路6号. 구이 생선 전문점입니다.",
                "category": "restaurant",
                "lat": 41.8007,
                "lng": 123.4637,
                "evidence": "동일 지점의 상호와 주소를 확인했습니다.",
                "source_urls": ["https://example.org/mingji"],
                "confidence": 0.9,
                "insights": [
                    {
                        "kind": "location",
                        "title": "위치",
                        "content": "다웨청 A관에 있는 지점입니다.",
                        "source_url": "https://example.org/mingji",
                    },
                    {
                        "kind": "tip",
                        "title": "메뉴",
                        "content": "구이 생선을 중심으로 주문하는 식당입니다.",
                        "source_url": "https://example.org/mingji",
                    },
                ],
            },
            city_id=2,
        )

        self.assertTrue(result["duplicate"])
        self.assertEqual(result["existing_place_id"], existing.id)
        self.assertEqual(self.db.query(AgentProposal).count(), 0)

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

    def test_failed_model_output_summary_becomes_a_deduplicated_diagnostic_lesson(self) -> None:
        run = AgentRun(
            city_id=2,
            status="failed",
            summary="Error code 400: output_parse_failed; Parsing failed",
        )
        self.db.add(run)
        self.db.commit()

        first = learn_from_recent_runs(self.db, city_id=2)
        second = learn_from_recent_runs(self.db, city_id=2)

        lesson = self.db.query(AgentLesson).filter(
            AgentLesson.lesson_key == "model_output_failure:output_parse_failed"
        ).one()
        self.assertEqual(first, 1)
        self.assertEqual(second, 1)
        self.assertEqual(lesson.observation_count, 1)
        self.assertEqual(lesson.failure_count, 1)

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

    def test_place_update_preserves_existing_korean_name(self) -> None:
        marker = self.db.query(Marker).filter(Marker.city_id == 2).one()
        marker.title = "선양 타오셴 국제공항"
        self.db.commit()
        result = run_tool(
            self.db,
            "update_place_fields",
            {
                "place_id": marker.id,
                "expected_title": marker.title,
                "replace_title": "沈阳桃仙国际机场 (선양 타오셉 국제공항)",
            },
            city_id=2,
        )
        self.assertEqual(result["error"], "existing_korean_name_must_be_preserved")

    def test_place_insight_rejects_other_district_branch(self) -> None:
        marker = self.db.query(Marker).filter(Marker.city_id == 2).one()
        marker.description = "Shenhe Middle Street, Shenyang"
        self.db.commit()
        result = run_tool(
            self.db,
            "upsert_place_insights",
            {
                "place_id": marker.id,
                "insights": [{
                    "kind": "location",
                    "title": "주소",
                    "content": "Tiexi Jingxing North Street 3에 위치합니다.",
                    "source_url": "https://example.test/tiexi",
                    "source_title": "Liaomingyan Tiexi branch",
                    "confidence": 0.9,
                }],
                "_validated_source_urls": ["https://example.test/tiexi"],
            },
            city_id=2,
        )
        self.assertEqual(result["error"], "insight_branch_mismatch")

    def test_place_insight_rejects_fixed_currency_conversion(self) -> None:
        marker = self.db.query(Marker).filter(Marker.city_id == 2).one()
        result = run_tool(
            self.db,
            "upsert_place_insights",
            {
                "place_id": marker.id,
                "insights": [{
                    "kind": "tip",
                    "title": "price",
                    "content": "평균 가격은 61위안(약 5,000원)입니다.",
                    "source_url": "https://example.test/price",
                    "source_title": marker.title,
                    "confidence": 0.9,
                }],
                "_validated_source_urls": ["https://example.test/price"],
            },
            city_id=2,
        )
        self.assertEqual(result["error"], "derived_currency_conversion_forbidden")

    def test_reconcile_pauses_instead_of_reactivating_blocked_item(self) -> None:
        place = self.db.query(Marker).filter(Marker.city_id == 2).one()
        place.description = (
            "충분히 긴 한국어 장소 설명입니다. 여행자가 방문 전에 이해할 수 있도록 "
            "핵심 위치와 특징을 자세히 정리했습니다."
        )
        place.insights.extend([
            PlaceInsight(kind="location", title="위치", content="위치 정보", source_url="https://example.test/1"),
            PlaceInsight(kind="tip", title="팁", content="방문 정보", source_url="https://example.test/2"),
        ])
        task = AgentTask(city_id=2, kind="quality_information", title="정보 보강", status="pending")
        self.db.add(task)
        self.db.flush()
        mission = AgentMission(city_id=2, task_id=task.id, kind=task.kind, title=task.title, status="active")
        self.db.add(mission)
        self.db.flush()
        blocked = AgentWorkItem(
            mission_id=mission.id, city_id=2, target_key="task:blocked", title="차단 장소",
            status="blocked", priority=90,
        )
        current = AgentWorkItem(
            mission_id=mission.id, city_id=2, place_id=place.id, target_key=f"place:{place.id}",
            title=place.title, status="active", priority=80,
        )
        self.db.add_all([blocked, current])
        self.db.commit()
        active = reconcile_work_items(self.db, mission=mission)
        self.assertIsNone(active)
        self.assertEqual(current.status, "done")
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(mission.status, "paused")

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
                "coordinate_source": "manual",
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
