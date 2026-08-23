from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.runner import (
    _acknowledge_authoritative_queue_events,
    count_unread,
    run_agent,
)
from app.agent.tools import run_tool
from app.db import Base
from app.models import (
    AgentKnowledge,
    AgentLesson,
    AgentRun,
    City,
    PlaceEvent,
    PlaceEventAction,
    User,
)


class AgentQueuePreprocessingTests(unittest.TestCase):
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
                User(
                    id=7,
                    email="traveler@example.com",
                    display_name="traveler",
                    password_hash="test",
                ),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_run_floor_query_failure_still_releases_city_lock(self) -> None:
        db = Mock()
        db.query.side_effect = RuntimeError("run floor unavailable")
        lock_connection = object()
        with (
            patch(
                "app.agent.runner._acquire_agent_city_lock",
                return_value=(lock_connection, "acquired"),
            ),
            patch("app.agent.runner._release_agent_city_lock") as release,
            self.assertRaisesRegex(RuntimeError, "run floor unavailable"),
        ):
            run_agent(db, city_id=2, autonomous_research=True)

        release.assert_called_once_with(lock_connection, city_id=2)

    def _event(
        self,
        *,
        action: PlaceEventAction,
        summary: str,
        payload: dict | None = None,
        actor: str = "system",
        user_id: int | None = None,
        city_id: int = 2,
        created_at: datetime | None = None,
    ) -> PlaceEvent:
        event = PlaceEvent(
            city_id=city_id,
            actor=actor,
            user_id=user_id,
            action=action,
            summary=summary,
            payload=json.dumps(payload or {}, ensure_ascii=False),
        )
        if created_at is not None:
            event.created_at = created_at
        self.db.add(event)
        self.db.flush()
        return event

    def test_only_authoritative_events_create_audited_model_free_queue_run(self) -> None:
        correction_with_lesson = self._event(
            action=PlaceEventAction.update,
            summary="서타 정보 교정",
            payload={
                "cleanup_version": "prod-cleanup-v1",
                "lesson": "서타의 검증되지 않은 상세 주소와 일률적 영업시간을 다시 쓰지 않는다.",
                "source_urls": ["https://example.org/source"],
            },
        )
        correction_without_lesson = self._event(
            action=PlaceEventAction.update,
            summary="공원 이름과 역할 교정",
            payload={
                "cleanup_version": "prod-cleanup-v1",
                "after": {
                    "title": "沈水湾公园 (선수이완 공원)",
                    "travel_role": "nature",
                },
                "source_urls": ["https://example.org/park"],
            },
        )
        archive = self._event(
            action=PlaceEventAction.context_update,
            summary="정리 전 지식 스냅샷 보존",
            payload={
                "cleanup_version": "prod-cleanup-v1",
                "archive_hash": "abc123",
                "snapshot": {"large": "value" * 100},
            },
        )
        self.db.commit()

        with patch("app.agent.runner.settings.groq_api_key", ""):
            result = run_agent(self.db, city_id=2, autonomous_research=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "queue_acknowledged")
        self.assertEqual(result["steps"], 0)
        self.assertEqual(result["unread_before"], 3)
        self.assertEqual(result["unread_after"], 0)
        self.assertEqual(result["acknowledged_event_count"], 3)
        self.assertEqual(result["score"], 24.0)
        self.assertEqual(
            result["queue_preprocessing"]["archive_metadata_event_ids"],
            [archive.id],
        )
        self.assertEqual(self.db.query(AgentLesson).count(), 2)
        self.assertEqual(self.db.query(AgentKnowledge).count(), 2)
        self.assertTrue(
            all(row.status == "validated" for row in self.db.query(AgentLesson).all())
        )
        self.assertTrue(
            all(
                row.groq_read_at is not None
                for row in (correction_with_lesson, correction_without_lesson, archive)
            )
        )
        run = self.db.query(AgentRun).one()
        metrics = json.loads(run.metrics)
        self.assertEqual(metrics["lane"], "deterministic_queue_ack")
        self.assertEqual(metrics["outcome"], "queue_acknowledged")
        self.assertEqual(metrics["queue_events_cleared"], 3)

    def test_partial_preack_stays_in_queue_lane_until_ordinary_event_is_cleared(self) -> None:
        correction = self._event(
            action=PlaceEventAction.update,
            summary="시스템 교정",
            payload={
                "cleanup_version": "prod-cleanup-v2",
                "lesson": "확정된 교정을 반복 조사하지 않는다.",
            },
        )
        ordinary = self._event(
            action=PlaceEventAction.update,
            summary="사용자가 직접 보완 요청",
            actor="user",
            user_id=7,
        )
        self.db.commit()
        requests: list[dict] = []

        class QueueCompletions:
            def create(inner_self, **kwargs):
                requests.append(kwargs)
                if len(requests) == 1:
                    call = SimpleNamespace(
                        id="ack-ordinary",
                        function=SimpleNamespace(
                            name="mark_events_read",
                            arguments=json.dumps({"event_ids": [ordinary.id]}),
                        ),
                    )
                    message = SimpleNamespace(content="", tool_calls=[call])
                else:
                    message = SimpleNamespace(content="사용자 큐 처리 완료", tool_calls=[])
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=QueueCompletions())
        )
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
        ):
            result = run_agent(self.db, city_id=2, autonomous_research=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["unread_before"], 2)
        self.assertEqual(result["unread_after"], 0)
        self.assertEqual(result["acknowledged_event_count"], 1)
        self.assertIsNotNone(correction.groq_read_at)
        self.assertIsNotNone(ordinary.groq_read_at)
        self.assertEqual(self.db.query(AgentRun).one().mode, "queue")
        first_user_message = requests[0]["messages"][1]["content"]
        self.assertIn("미읽음 작업 1건", first_user_message)
        self.assertIn(str(ordinary.id), first_user_message)

    def test_admin_rollback_is_promoted_but_ordinary_user_update_is_preserved(self) -> None:
        ordinary = self._event(
            action=PlaceEventAction.update,
            summary="사용자가 설명을 직접 수정",
            actor="user",
            user_id=7,
        )
        rollback = self._event(
            action=PlaceEventAction.rollback,
            summary="관리자 롤백: 잘못된 병합",
            actor="user",
            user_id=7,
            payload={
                "rolled_back_event_id": 123,
                "admin_note": "서로 다른 지점",
                "lesson": "서로 다른 지점을 이름 유사성만으로 병합하지 않는다.",
            },
        )
        self.db.commit()

        result = _acknowledge_authoritative_queue_events(self.db, city_id=2)
        self.db.commit()

        self.assertEqual(result["acknowledged_event_ids"], [rollback.id])
        self.assertIsNone(ordinary.groq_read_at)
        self.assertIsNotNone(rollback.groq_read_at)
        self.assertEqual(count_unread(self.db, 2), 1)
        lesson = self.db.query(AgentLesson).one()
        self.assertEqual(lesson.status, "validated")
        self.assertIn("병합하지 않는다", lesson.action)

    def test_adjacent_exact_create_delete_pair_is_net_zero_but_unmatched_create_remains(self) -> None:
        base = datetime.now(timezone.utc)
        title = "Shenyang Middle Street Palace Museum Manxin Hotel"
        snapshot = {
            "title": title,
            "address": "沈河区北中街路118号",
            "lat": 41.80651,
            "lng": 123.45618,
        }
        created = self._event(
            action=PlaceEventAction.create,
            summary=f"장소 추가: {title}",
            payload={"after": snapshot},
            actor="user",
            user_id=7,
            created_at=base,
        )
        deleted = self._event(
            action=PlaceEventAction.delete,
            summary=f"장소 삭제: {title}",
            payload={"before": snapshot},
            actor="user",
            user_id=7,
            created_at=base + timedelta(seconds=29),
        )
        unmatched = self._event(
            action=PlaceEventAction.create,
            summary="장소 추가: another place",
            actor="user",
            user_id=7,
            created_at=base + timedelta(minutes=2),
        )
        self.db.commit()

        result = _acknowledge_authoritative_queue_events(self.db, city_id=2)
        self.db.commit()

        self.assertEqual(result["acknowledged_event_ids"], [created.id, deleted.id])
        self.assertEqual(
            result["net_zero_create_delete_pairs"],
            [{
                "create_event_id": created.id,
                "delete_event_id": deleted.id,
                "title": title,
                "same_entity_proof": "matching_title_and_coordinates",
            }],
        )
        self.assertIsNotNone(created.groq_read_at)
        self.assertIsNotNone(deleted.groq_read_at)
        self.assertIsNone(unmatched.groq_read_at)
        self.assertEqual(count_unread(self.db, 2), 1)
        knowledge = self.db.query(AgentKnowledge).one()
        self.assertIn("자동으로 되살리지", knowledge.content)

    def test_same_title_nearby_heytea_branches_are_never_net_zero_without_entity_proof(self) -> None:
        base = datetime.now(timezone.utc)
        title = "喜茶 (헤이티)"
        created = self._event(
            action=PlaceEventAction.create,
            summary=f"장소 추가: {title}",
            payload={
                "after": {
                    "title": title,
                    "address": "沈河区中街路128号",
                    "lat": 41.80111,
                    "lng": 123.44910,
                }
            },
            actor="user",
            user_id=7,
            created_at=base,
        )
        deleted = self._event(
            action=PlaceEventAction.delete,
            summary=f"장소 삭제: {title}",
            payload={
                "before": {
                    "title": title,
                    "address": "大东区小东路10号",
                    "lat": 41.80842,
                    "lng": 123.46931,
                }
            },
            actor="user",
            user_id=7,
            created_at=base + timedelta(seconds=20),
        )
        self.db.commit()

        result = _acknowledge_authoritative_queue_events(self.db, city_id=2)

        self.assertEqual(result["acknowledged_event_ids"], [])
        self.assertEqual(result["net_zero_create_delete_pairs"], [])
        self.assertIsNone(created.groq_read_at)
        self.assertIsNone(deleted.groq_read_at)
        self.assertEqual(count_unread(self.db, 2), 2)
        self.assertEqual(self.db.query(AgentKnowledge).count(), 0)

    def test_title_and_timing_alone_never_ack_legacy_create_delete_pair(self) -> None:
        base = datetime.now(timezone.utc)
        title = "Shenyang Middle Street Palace Museum Manxin Hotel"
        created = self._event(
            action=PlaceEventAction.create,
            summary=f"장소 추가: {title}",
            payload={"lat": 41.8025, "lng": 123.4280556},
            actor="user",
            user_id=7,
            created_at=base,
        )
        deleted = self._event(
            action=PlaceEventAction.delete,
            summary=f"장소 삭제: {title}",
            payload={"title": title},
            actor="user",
            user_id=7,
            created_at=base + timedelta(seconds=29),
        )
        self.db.commit()

        result = _acknowledge_authoritative_queue_events(self.db, city_id=2)

        self.assertEqual(result["acknowledged_event_ids"], [])
        self.assertIsNone(created.groq_read_at)
        self.assertIsNone(deleted.groq_read_at)

    def test_all_ack_commit_failure_leaves_queue_and_knowledge_untouched(self) -> None:
        correction = self._event(
            action=PlaceEventAction.update,
            summary="원자적 교정",
            payload={
                "cleanup_version": "atomic-v1",
                "lesson": "교정 승인과 실행 이력은 같은 트랜잭션에 있어야 한다.",
            },
        )
        self.db.commit()

        with (
            patch("app.agent.runner.settings.groq_api_key", ""),
            patch.object(self.db, "commit", side_effect=RuntimeError("commit failed")),
            self.assertRaisesRegex(RuntimeError, "commit failed"),
        ):
            run_agent(self.db, city_id=2, autonomous_research=True)
        self.db.rollback()

        correction = self.db.get(PlaceEvent, correction.id)
        self.assertIsNone(correction.groq_read_at)
        self.assertEqual(self.db.query(AgentLesson).count(), 0)
        self.assertEqual(self.db.query(AgentKnowledge).count(), 0)
        self.assertEqual(self.db.query(AgentRun).count(), 0)

    def test_partial_preack_without_model_key_rolls_back_every_preprocessing_write(self) -> None:
        correction = self._event(
            action=PlaceEventAction.update,
            summary="부분 전처리 교정",
            payload={
                "cleanup_version": "atomic-v2",
                "lesson": "남은 큐를 실행할 수 없으면 이 교정도 소비하지 않는다.",
            },
        )
        ordinary = self._event(
            action=PlaceEventAction.update,
            summary="사용자 판단 필요",
            actor="user",
            user_id=7,
        )
        self.db.commit()

        with patch("app.agent.runner.settings.groq_api_key", ""):
            result = run_agent(self.db, city_id=2, autonomous_research=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["acknowledged_event_count"], 0)
        self.assertTrue(result["queue_preprocessing"]["rolled_back"])
        self.assertEqual(result["unread_before"], 2)
        self.assertEqual(result["unread_after"], 2)
        self.db.refresh(correction)
        self.db.refresh(ordinary)
        self.assertIsNone(correction.groq_read_at)
        self.assertIsNone(ordinary.groq_read_at)
        self.assertEqual(self.db.query(AgentLesson).count(), 0)
        self.assertEqual(self.db.query(AgentKnowledge).count(), 0)
        self.assertEqual(self.db.query(AgentRun).count(), 0)

    def test_partial_preack_preparation_failure_finalizes_same_audit_run(self) -> None:
        correction = self._event(
            action=PlaceEventAction.update,
            summary="준비 실패 전 시스템 교정",
            payload={
                "cleanup_version": "atomic-preparation-v1",
                "lesson": "승인된 교정과 실패 실행 이력은 같은 감사 흐름에 남긴다.",
            },
        )
        ordinary = self._event(
            action=PlaceEventAction.update,
            summary="모델 판단이 필요한 사용자 요청",
            actor="user",
            user_id=7,
        )
        self.db.commit()

        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: SimpleNamespace()
        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch("app.agent.runner.settings.groq_api_key", "test-key"),
            patch(
                "app.agent.runner.normalize_knowledge_metadata",
                side_effect=RuntimeError("preparation exploded"),
            ),
        ):
            result = run_agent(self.db, city_id=2, autonomous_research=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["acknowledged_event_count"], 1)
        self.assertEqual(result["unread_before"], 2)
        self.assertEqual(result["unread_after"], 1)
        run = self.db.query(AgentRun).one()
        self.assertEqual(run.id, result["run_id"])
        self.assertEqual(run.status, "failed")
        self.assertIsNotNone(run.finished_at)
        metrics = json.loads(run.metrics)
        self.assertEqual(metrics["failure_phase"], "preparation")
        self.assertEqual(metrics["lifecycle_phase"], "failed")
        self.assertEqual(metrics["failure_type"], "RuntimeError")
        self.assertEqual(
            metrics["queue_preprocessing"]["acknowledged_event_ids"],
            [correction.id],
        )
        self.db.refresh(correction)
        self.db.refresh(ordinary)
        self.assertIsNotNone(correction.groq_read_at)
        self.assertIsNone(ordinary.groq_read_at)
        self.assertEqual(self.db.query(AgentLesson).count(), 1)
        self.assertEqual(self.db.query(AgentKnowledge).count(), 1)

    def test_deleted_place_events_are_listed_and_marked_by_persisted_city(self) -> None:
        city_one = self._event(
            action=PlaceEventAction.update,
            summary="city one orphan",
            actor="user",
            user_id=7,
            city_id=1,
        )
        city_two = self._event(
            action=PlaceEventAction.update,
            summary="city two orphan",
            actor="user",
            user_id=7,
            city_id=2,
        )
        self.db.commit()

        listed = run_tool(self.db, "list_unread_events", {}, city_id=2)
        self.assertEqual([row["id"] for row in listed], [city_two.id])
        marked = run_tool(
            self.db,
            "mark_events_read",
            {"event_ids": [city_one.id, city_two.id]},
            city_id=2,
        )
        self.assertEqual(marked, {"marked": 1})
        self.assertIsNone(city_one.groq_read_at)
        self.assertIsNotNone(city_two.groq_read_at)


if __name__ == "__main__":
    unittest.main()
