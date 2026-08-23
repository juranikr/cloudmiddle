from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.agent.memory import ensure_mission_for_task, finalize_mission
from app.db import Base
from app.migrate import ensure_schema
from app.models import AgentMission, AgentTask, AgentWorkItem, City


class AgentMissionCooldownTests(unittest.TestCase):
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
        self.task = AgentTask(
            city_id=2,
            kind="candidate_discovery",
            title="음료 후보 발굴",
            detail="target_role: food",
            status="pending",
        )
        self.db.add(self.task)
        self.db.flush()
        self.mission = AgentMission(
            city_id=2,
            task_id=self.task.id,
            kind=self.task.kind,
            title=self.task.title,
            objective=self.task.detail,
            status="active",
        )
        self.db.add(self.mission)
        self.db.flush()
        self.item = AgentWorkItem(
            mission_id=self.mission.id,
            city_id=2,
            target_type="task",
            target_key=f"task:{self.task.id}",
            title=self.task.title,
            status="active",
        )
        self.db.add(self.item)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_candidate_pause_uses_explicit_timezone_safe_clock(self) -> None:
        blocked_at_kst = datetime(2026, 8, 23, 12, 30, tzinfo=timezone(timedelta(hours=9)))
        self.task.status = "blocked"
        self.task.result = "provider temporarily blocked"

        finalize_mission(
            self.db,
            mission=self.mission,
            task=self.task,
            run_id=77,
            now=blocked_at_kst,
        )

        blocked_at_utc = datetime(2026, 8, 23, 3, 30, tzinfo=timezone.utc)
        retry_after_utc = blocked_at_utc + timedelta(hours=12)
        self.assertEqual(self.mission.status, "paused")
        self.assertEqual(self.mission.blocked_at.replace(tzinfo=timezone.utc), blocked_at_utc)
        self.assertEqual(self.mission.retry_after.replace(tzinfo=timezone.utc), retry_after_utc)
        progress = json.loads(self.mission.progress)
        self.assertEqual(progress["blocked_at"], blocked_at_utc.isoformat())
        self.assertEqual(progress["retry_after"], retry_after_utc.isoformat())

        # Checkpoint/recovery bookkeeping may still update this generic field;
        # it must not move the explicit deadline.
        explicit_retry = self.mission.retry_after
        self.mission.updated_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.db.commit()
        self.assertEqual(self.mission.retry_after, explicit_retry)

    def test_resuming_due_mission_clears_only_active_pause_columns(self) -> None:
        blocked_at = datetime(2026, 8, 23, 3, tzinfo=timezone.utc)
        self.task.status = "blocked"
        finalize_mission(
            self.db,
            mission=self.mission,
            task=self.task,
            run_id=77,
            now=blocked_at,
        )
        historical_progress = self.mission.progress
        self.task.status = "pending"

        resumed_mission, resumed_item = ensure_mission_for_task(self.db, self.task)

        self.assertEqual(resumed_mission.id, self.mission.id)
        self.assertEqual(resumed_item.id, self.item.id)
        self.assertEqual(resumed_mission.status, "active")
        self.assertIsNone(resumed_mission.blocked_at)
        self.assertIsNone(resumed_mission.retry_after)
        self.assertEqual(resumed_mission.progress, historical_progress)


class AgentMissionCooldownMigrationTests(unittest.TestCase):
    def test_legacy_candidate_pause_is_backfilled_once_and_idempotently(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        # The explicit progress deadline is older/more authoritative than a
        # generic checkpoint update two hours later.
        legacy_updated = "2026-08-23 05:30:00"
        explicit_retry = "2026-08-24T00:30:00+09:00"
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE agent_missions"))
            conn.execute(text("""
                CREATE TABLE agent_missions (
                    id INTEGER PRIMARY KEY,
                    kind VARCHAR(40) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    progress TEXT NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
            """))
            conn.execute(text("""
                INSERT INTO agent_missions (id, kind, status, progress, updated_at)
                VALUES
                  (1, 'candidate_discovery', 'paused', :progress, :updated_at),
                  (2, 'quality_images', 'paused', '{}', :updated_at),
                  (3, 'candidate_discovery', 'paused', '{}', :updated_at),
                  (4, 'candidate_discovery', 'paused', 'not-json', 'not-a-timestamp')
            """), {
                "progress": json.dumps({"retry_after": explicit_retry}),
                "updated_at": legacy_updated,
            })

        with patch("app.migrate.engine", engine):
            with engine.connect() as conn:
                legacy_row = conn.execute(text(
                    "SELECT kind, status, progress, updated_at FROM agent_missions WHERE id = 1"
                )).mappings().one()
            self.assertEqual(legacy_row["kind"], "candidate_discovery")
            self.assertEqual(legacy_row["status"], "paused")
            ensure_schema()
            with engine.connect() as conn:
                first_pass = conn.execute(text(
                    "SELECT blocked_at, retry_after FROM agent_missions WHERE id = 1"
                )).mappings().one()
            self.assertIsNotNone(first_pass["blocked_at"])
            self.assertIsNotNone(first_pass["retry_after"])
            # A later generic update cannot be mistaken for a new cooldown on
            # the second idempotent migration pass.
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE agent_missions SET updated_at = '2030-01-01 00:00:00' WHERE id = 1"
                ))
            ensure_schema()

        columns = {column["name"] for column in inspect(engine).get_columns("agent_missions")}
        self.assertIn("blocked_at", columns)
        self.assertIn("retry_after", columns)
        with engine.connect() as conn:
            candidate = conn.execute(text(
                "SELECT blocked_at, retry_after FROM agent_missions WHERE id = 1"
            )).mappings().one()
            unrelated = conn.execute(text(
                "SELECT blocked_at, retry_after FROM agent_missions WHERE id = 2"
            )).mappings().one()
            fallback = conn.execute(text(
                "SELECT blocked_at, retry_after FROM agent_missions WHERE id = 3"
            )).mappings().one()
            malformed = conn.execute(text(
                "SELECT blocked_at, retry_after FROM agent_missions WHERE id = 4"
            )).mappings().one()

        self.assertEqual(str(candidate["blocked_at"])[:19], "2026-08-23 03:30:00")
        self.assertTrue(str(candidate["retry_after"]).startswith("2026-08-23 15:30:00"))
        self.assertIsNone(unrelated["blocked_at"])
        self.assertIsNone(unrelated["retry_after"])
        self.assertEqual(str(fallback["blocked_at"])[:19], "2026-08-23 05:30:00")
        self.assertEqual(str(fallback["retry_after"])[:19], "2026-08-23 17:30:00")
        self.assertIsNone(malformed["blocked_at"])
        self.assertIsNone(malformed["retry_after"])
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
