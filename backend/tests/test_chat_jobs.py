import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.chat_jobs import (
    ChatJobConflict,
    chat_job_to_out,
    create_chat_job,
    fail_stale_chat_jobs,
    run_chat_job,
)
from app.db import Base
from app.auth import create_access_token
from app.db import get_db
from app.main import app, clear_travel_chat, get_travel_chat_job, post_travel_chat_job
from app.models import City, TravelChatJob, TravelChatMessage, User
from app.schemas import TravelChatRequest


class ChatJobTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.db = self.factory()
        self.db.add_all([
            City(id=1, slug="jinan", name_ko="지난", name_local="济南", center_lat=36.65, center_lng=117.12),
            City(id=2, slug="other", name_ko="다른 도시", name_local="Other", center_lat=1, center_lng=1),
            User(id=1, email="one@example.com", display_name="One", password_hash="fixture"),
            User(id=2, email="two@example.com", display_name="Two", password_hash="fixture"),
        ])
        self.db.commit()
        self.body = TravelChatRequest(city_id=1, message="여행 계획을 알려줘")

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def fake_answer(self, db, **kwargs):
        self.assertIsNot(db, self.db)
        row = TravelChatMessage(
            user_id=kwargs["user_id"], city_id=kwargs["city_id"],
            role="assistant", content="Grounded fixture answer", sources="[]", place_ids="[]",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"row": row, "model": "fixture-model", "place_ids": []}

    def test_worker_owns_session_and_persists_response_without_reexecution(self):
        job, created = create_chat_job(self.db, user_id=1, body=self.body)
        self.assertTrue(created)
        worker = self.factory()
        answer = Mock(side_effect=self.fake_answer)
        with patch.object(worker, "close", wraps=worker.close) as close:
            run_chat_job(job.id, session_factory=lambda: worker, answer=answer)
            close.assert_called_once()
        run_chat_job(job.id, session_factory=self.factory, answer=answer)
        answer.assert_called_once()
        self.db.expire_all()
        out = chat_job_to_out(self.db.get(TravelChatJob, job.id))
        self.assertEqual(out.status, "completed")
        self.assertEqual(out.response.model, "fixture-model")
        self.assertEqual(out.response.message.content, "Grounded fixture answer")
        self.assertIsNone(self.db.get(TravelChatJob, job.id).active_key)
        self.assertTrue(create_chat_job(self.db, user_id=1, body=self.body)[1])

    def test_duplicate_attaches_but_different_message_conflicts_and_db_enforces_unique(self):
        job, _ = create_chat_job(self.db, user_id=1, body=self.body)
        duplicate, created = create_chat_job(self.db, user_id=1, body=self.body)
        self.assertEqual(duplicate.id, job.id)
        self.assertFalse(created)
        with self.assertRaises(ChatJobConflict):
            create_chat_job(self.db, user_id=1, body=TravelChatRequest(city_id=1, message="다른 질문"))
        self.db.add(TravelChatJob(
            id="conflicting-fixture", user_id=1, city_id=1, active_key="1:1",
            message="duplicate", created_at=datetime.now(timezone.utc), expires_at=job.expires_at,
        ))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_failure_does_not_disclose_provider_exception(self):
        job, _ = create_chat_job(self.db, user_id=1, body=self.body)
        run_chat_job(job.id, session_factory=self.factory, answer=Mock(side_effect=RuntimeError("SECRET-fixture-token")))
        self.db.expire_all()
        out = chat_job_to_out(self.db.get(TravelChatJob, job.id))
        self.assertEqual(out.status, "failed")
        self.assertNotIn("SECRET-fixture-token", out.model_dump_json())
        self.assertIsNone(out.response)

    def test_restart_cleanup_expires_old_work_but_preserves_live_rolling_worker(self):
        job, _ = create_chat_job(self.db, user_id=1, body=self.body)
        self.assertEqual(fail_stale_chat_jobs(self.db), 0)
        job.status = "running"
        job.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(fail_stale_chat_jobs(self.db), 1)
        out = chat_job_to_out(self.db.get(TravelChatJob, job.id))
        self.assertEqual(out.status, "failed")
        self.assertIn("재시작", out.error)
        answer = Mock()
        run_chat_job(job.id, session_factory=self.factory, answer=answer)
        answer.assert_not_called()
        self.assertTrue(create_chat_job(self.db, user_id=1, body=self.body)[1])

    def test_ttl_preserves_longer_configured_harness_timeout(self):
        with patch("app.chat_jobs.settings.codex_timeout_seconds", 1200):
            job, _ = create_chat_job(self.db, user_id=1, body=self.body)
        self.assertGreaterEqual((job.expires_at - job.created_at).total_seconds(), 1230)

    def test_routes_scope_job_to_both_user_and_city_and_enqueue_only_once(self):
        user = self.db.get(User, 1)
        tasks = BackgroundTasks()
        out = post_travel_chat_job(self.body, tasks, self.db, user)
        self.assertEqual(out.status, "pending")
        self.assertEqual(len(tasks.tasks), 1)
        self.assertEqual(tasks.tasks[0].args, (out.id,))
        again = BackgroundTasks()
        self.assertEqual(post_travel_chat_job(self.body, again, self.db, user).id, out.id)
        self.assertEqual(len(again.tasks), 0)
        self.assertEqual(get_travel_chat_job(out.id, 1, self.db, user).id, out.id)
        for city_id, current_user in [(2, user), (1, self.db.get(User, 2))]:
            with self.assertRaises(HTTPException) as denied:
                get_travel_chat_job(out.id, city_id, self.db, current_user)
            self.assertEqual(denied.exception.status_code, 404)

    def test_explicit_migration_adds_only_missing_job_table_idempotently(self):
        from app.migrate import ensure_schema

        TravelChatJob.__table__.drop(bind=self.engine)
        with patch("app.migrate.engine", self.engine):
            ensure_schema()
            ensure_schema()
        self.assertIn("travel_chat_jobs", inspect(self.engine).get_table_names())
        self.assertEqual(self.db.query(User).count(), 2)

    def test_clear_chat_removes_terminal_job_content_only_for_that_user_and_city(self):
        jobs = []
        for user_id, city_id in [(1, 1), (2, 1), (1, 2)]:
            job, _ = create_chat_job(self.db, user_id=user_id,
                                    body=TravelChatRequest(city_id=city_id, message="private fixture"))
            job.status, job.active_key, job.error = "failed", None, "private fixture"
            self.db.commit()
            jobs.append(job.id)
        user = self.db.get(User, 1)
        clear_travel_chat(city_id=1, db=self.db, current_user=user)
        with self.assertRaises(HTTPException) as missing:
            get_travel_chat_job(jobs[0], 1, self.db, user)
        self.assertEqual(missing.exception.status_code, 404)
        self.assertIsNotNone(self.db.get(TravelChatJob, jobs[1]))
        self.assertIsNotNone(self.db.get(TravelChatJob, jobs[2]))

    def test_authenticated_http_202_then_poll_with_existing_response_contract(self):
        def request_db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = request_db
        try:
            client = TestClient(app)
            unauthenticated = client.post("/api/travel-chat/jobs", json=self.body.model_dump())
            self.assertIn(unauthenticated.status_code, (401, 403))
            headers = {"Authorization": f"Bearer {create_access_token('1')}"}

            def fake_worker(job_id):
                run_chat_job(job_id, session_factory=self.factory, answer=self.fake_answer)

            with patch("app.main.run_chat_job", side_effect=fake_worker) as worker:
                response = client.post("/api/travel-chat/jobs", json=self.body.model_dump(), headers=headers)
            self.assertEqual(response.status_code, 202)
            worker.assert_called_once()
            job_id = response.json()["id"]
            result = client.get(f"/api/travel-chat/jobs/{job_id}?city_id=1", headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["status"], "completed")
            self.assertEqual(result.json()["response"]["message"]["content"], "Grounded fixture answer")
            other_headers = {"Authorization": f"Bearer {create_access_token('2')}"}
            denied = client.get(f"/api/travel-chat/jobs/{job_id}?city_id=1", headers=other_headers)
            self.assertEqual(denied.status_code, 404)
        finally:
            app.dependency_overrides.pop(get_db, None)


if __name__ == "__main__":
    unittest.main()
