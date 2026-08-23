from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.agent.runner import (
    _acquire_agent_city_lock,
    _release_agent_city_lock,
    run_agent,
)


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar(self) -> bool:
        return self.value


class _FakeConnection:
    def __init__(self, acquired: bool = True, fail_on: str = "") -> None:
        self.acquired = acquired
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self.closed = False
        self.invalidated = False

    def execute(self, statement, params):
        sql = str(statement)
        self.calls.append((sql, dict(params)))
        if self.fail_on and self.fail_on in sql:
            raise ConnectionError("ambiguous connection failure")
        if "pg_try_advisory_lock" in sql:
            return _ScalarResult(self.acquired)
        return _ScalarResult(True)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True

    def invalidate(self) -> None:
        self.invalidated = True


class AgentRunLockTests(unittest.TestCase):
    def test_postgres_lock_uses_dedicated_connection_until_release(self) -> None:
        connection = _FakeConnection(acquired=True)
        engine = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            connect=lambda: connection,
        )
        db = SimpleNamespace(get_bind=lambda: engine)

        acquired, status = _acquire_agent_city_lock(db, city_id=2)

        self.assertIs(acquired, connection)
        self.assertEqual(status, "acquired")
        self.assertFalse(connection.closed)
        self.assertIn("pg_try_advisory_lock", connection.calls[0][0])
        self.assertEqual(connection.calls[0][1]["city_id"], 2)

        _release_agent_city_lock(connection, city_id=2)
        self.assertTrue(connection.closed)
        self.assertIn("pg_advisory_unlock", connection.calls[1][0])

    def test_busy_city_returns_without_running_agent(self) -> None:
        db = Mock()
        with (
            patch("app.agent.runner._acquire_agent_city_lock", return_value=(None, "busy")),
            patch("app.agent.runner.count_unread", return_value=3),
            patch("app.agent.runner._run_agent_impl") as implementation,
        ):
            result = run_agent(db, city_id=2, autonomous_research=True)

        implementation.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "already_running")
        self.assertEqual(result["steps"], 0)

    def test_ambiguous_acquire_failure_invalidates_physical_session(self) -> None:
        connection = _FakeConnection(fail_on="pg_try_advisory_lock")
        engine = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            connect=lambda: connection,
        )
        db = SimpleNamespace(get_bind=lambda: engine)

        with self.assertRaises(ConnectionError):
            _acquire_agent_city_lock(db, city_id=2)

        self.assertTrue(connection.invalidated)
        self.assertTrue(connection.closed)

    def test_unlock_failure_invalidates_session_without_raising(self) -> None:
        connection = _FakeConnection(fail_on="pg_advisory_unlock")

        _release_agent_city_lock(connection, city_id=2)

        self.assertTrue(connection.invalidated)
        self.assertTrue(connection.closed)

    def test_acquired_lock_is_released_after_agent_failure(self) -> None:
        db = Mock()
        connection = _FakeConnection(acquired=True)
        with (
            patch(
                "app.agent.runner._acquire_agent_city_lock",
                return_value=(connection, "acquired"),
            ),
            patch(
                "app.agent.runner._run_agent_impl",
                side_effect=RuntimeError("boom"),
            ),
            patch("app.agent.runner._release_agent_city_lock") as release,
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                run_agent(db, city_id=2)

        release.assert_called_once_with(connection, city_id=2)


if __name__ == "__main__":
    unittest.main()
