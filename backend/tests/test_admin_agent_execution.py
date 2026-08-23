import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app import admin_api


STATE_MACHINE_ARN = (
    "arn:aws:states:ap-northeast-2:123456789012:stateMachine:tourmiddle-test-agent"
)
EXECUTION_ARN = (
    "arn:aws:states:ap-northeast-2:123456789012:execution:tourmiddle-test-agent:admin-test"
)


class _CityQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return (2,)


class _Db:
    def query(self, *_args, **_kwargs):
        return _CityQuery()


class AdminAgentExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        with admin_api._agent_run_lock:
            admin_api._agent_run_state.update(
                {
                    "running": False,
                    "started_at": None,
                    "finished_at": None,
                    "result": None,
                    "execution_arn": None,
                    "backend": "local",
                    "city_id": None,
                    "city_ids": [],
                }
            )

    def _request(self):
        return admin_api.AgentRunRequest(city_id=2, research=True)

    def test_configured_production_run_starts_step_functions_once(self) -> None:
        client = Mock()
        client.list_executions.return_value = {"executions": []}
        client.start_execution.return_value = {
            "executionArn": EXECUTION_ARN,
            "startDate": datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc),
        }
        client.describe_execution.return_value = {
            "status": "RUNNING",
            "startDate": datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc),
        }

        with (
            patch.object(admin_api.settings, "agent_state_machine_arn", STATE_MACHINE_ARN),
            patch.object(admin_api, "_step_functions_client", return_value=client),
            patch.object(admin_api.threading, "Thread") as thread_cls,
        ):
            first = admin_api.admin_run_agent(
                self._request(), db=_Db(), admin=SimpleNamespace(id=1)
            )
            second = admin_api.admin_run_agent(
                self._request(), db=_Db(), admin=SimpleNamespace(id=1)
            )

        self.assertTrue(first.running)
        self.assertTrue(second.running)
        self.assertEqual(first.backend, "step_functions")
        self.assertEqual(first.execution_arn, EXECUTION_ARN)
        client.start_execution.assert_called_once()
        kwargs = client.start_execution.call_args.kwargs
        self.assertEqual(kwargs["stateMachineArn"], STATE_MACHINE_ARN)
        self.assertEqual(
            json.loads(kwargs["input"]),
            {"city_ids": [2], "autonomous_research": True},
        )
        self.assertTrue(kwargs["name"].startswith("admin-2-"))
        thread_cls.assert_not_called()

    def test_status_maps_nested_map_output_to_existing_response(self) -> None:
        started = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
        stopped = datetime(2026, 8, 23, 8, 3, tzinfo=timezone.utc)
        with admin_api._agent_run_lock:
            admin_api._agent_run_state.update(
                {
                    "running": True,
                    "started_at": started,
                    "finished_at": None,
                    "result": None,
                    "execution_arn": EXECUTION_ARN,
                    "backend": "step_functions",
                    "city_id": 2,
                }
            )
        client = Mock()
        client.describe_execution.return_value = {
            "status": "SUCCEEDED",
            "startDate": started,
            "stopDate": stopped,
            "output": json.dumps(
                [
                    {
                        "cities": [
                            {
                                "city_id": 2,
                                "run_id": 140,
                                "ok": True,
                                "status": "partial",
                                "steps": 17,
                                "score": 3.5,
                                "message": "checkpoint saved",
                            }
                        ]
                    }
                ]
            ),
        }

        with patch.object(admin_api, "_step_functions_client", return_value=client):
            status = admin_api.admin_agent_run_status(admin=SimpleNamespace(id=1))

        self.assertFalse(status.running)
        self.assertEqual(status.finished_at, stopped)
        self.assertIsNotNone(status.result)
        self.assertEqual(status.result.run_id, 140)
        self.assertEqual(status.result.status, "partial")
        self.assertEqual(status.result.steps, 17)
        self.assertEqual(status.result.city_id, 2)

    def test_status_never_substitutes_another_city_result(self) -> None:
        with admin_api._agent_run_lock:
            admin_api._agent_run_state.update(
                {
                    "running": True,
                    "started_at": datetime.now(timezone.utc),
                    "finished_at": None,
                    "result": None,
                    "execution_arn": EXECUTION_ARN,
                    "backend": "step_functions",
                    "city_id": 2,
                }
            )
        client = Mock()
        client.describe_execution.return_value = {
            "status": "SUCCEEDED",
            "stopDate": datetime.now(timezone.utc),
            "output": json.dumps([
                {
                    "cities": [{
                        "city_id": 1,
                        "run_id": 141,
                        "ok": True,
                        "status": "completed",
                        "steps": 9,
                        "message": "지난 결과",
                    }],
                }
            ]),
        }

        with patch.object(admin_api, "_step_functions_client", return_value=client):
            status = admin_api.admin_agent_run_status(admin=SimpleNamespace(id=1))

        self.assertFalse(status.running)
        self.assertIsNotNone(status.result)
        self.assertFalse(status.result.ok)
        self.assertEqual(status.result.city_id, 2)
        self.assertIsNone(status.result.run_id)
        self.assertIn("요청 도시 #2의 결과가 없습니다", status.result.message)

    def test_unset_arn_keeps_local_background_fallback(self) -> None:
        with (
            patch.object(admin_api.settings, "agent_state_machine_arn", ""),
            patch.object(admin_api.threading, "Thread") as thread_cls,
            patch.object(admin_api, "_step_functions_client") as client_factory,
        ):
            status = admin_api.admin_run_agent(
                self._request(), db=_Db(), admin=SimpleNamespace(id=1)
            )

        self.assertTrue(status.running)
        self.assertEqual(status.backend, "local")
        self.assertIsNone(status.execution_arn)
        thread_cls.assert_called_once()
        thread_cls.return_value.start.assert_called_once()
        client_factory.assert_not_called()

    def test_start_error_does_not_launch_ambiguous_local_fallback(self) -> None:
        client = Mock()
        client.list_executions.return_value = {"executions": []}
        client.start_execution.side_effect = RuntimeError("temporary AWS error")
        with (
            patch.object(admin_api.settings, "agent_state_machine_arn", STATE_MACHINE_ARN),
            patch.object(admin_api, "_step_functions_client", return_value=client),
            patch.object(admin_api.threading, "Thread") as thread_cls,
        ):
            with self.assertRaises(HTTPException) as raised:
                admin_api.admin_run_agent(
                    self._request(), db=_Db(), admin=SimpleNamespace(id=1)
                )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertFalse(admin_api._agent_run_state["running"])
        thread_cls.assert_not_called()

    def test_api_restart_adopts_running_execution_that_contains_city(self) -> None:
        started = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
        client = Mock()
        client.list_executions.return_value = {
            "executions": [{"executionArn": EXECUTION_ARN, "startDate": started}]
        }
        client.describe_execution.return_value = {
            "executionArn": EXECUTION_ARN,
            "status": "RUNNING",
            "startDate": started,
            "input": json.dumps({"city_ids": [1, 2], "autonomous_research": True}),
        }

        with (
            patch.object(admin_api.settings, "agent_state_machine_arn", STATE_MACHINE_ARN),
            patch.object(admin_api, "_step_functions_client", return_value=client),
            patch.object(admin_api.threading, "Thread") as thread_cls,
        ):
            status = admin_api.admin_run_agent(
                self._request(), db=_Db(), admin=SimpleNamespace(id=1)
            )

        self.assertTrue(status.running)
        self.assertEqual(status.execution_arn, EXECUTION_ARN)
        client.start_execution.assert_not_called()
        thread_cls.assert_not_called()

    def test_status_get_recovers_running_execution_after_api_restart(self) -> None:
        started = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
        client = Mock()
        client.list_executions.return_value = {
            "executions": [{"executionArn": EXECUTION_ARN, "startDate": started}]
        }
        client.describe_execution.return_value = {
            "executionArn": EXECUTION_ARN,
            "status": "RUNNING",
            "startDate": started,
            "input": json.dumps({"city_ids": [1, 2], "autonomous_research": True}),
        }

        with (
            patch.object(admin_api.settings, "agent_state_machine_arn", STATE_MACHINE_ARN),
            patch.object(admin_api, "_step_functions_client", return_value=client),
        ):
            status = admin_api.admin_agent_run_status(
                city_id=2,
                admin=SimpleNamespace(id=1),
            )

        self.assertTrue(status.running)
        self.assertEqual(status.city_id, 2)
        self.assertEqual(status.execution_arn, EXECUTION_ARN)
        client.start_execution.assert_not_called()

    def test_cached_running_other_city_rejects_new_manual_run(self) -> None:
        client = Mock()
        client.describe_execution.return_value = {
            "status": "RUNNING",
            "input": json.dumps({"city_ids": [1], "autonomous_research": True}),
        }
        with admin_api._agent_run_lock:
            admin_api._agent_run_state.update(
                {
                    "running": True,
                    "started_at": datetime.now(timezone.utc),
                    "finished_at": None,
                    "result": None,
                    "execution_arn": EXECUTION_ARN,
                    "backend": "step_functions",
                    "city_id": 1,
                    "city_ids": [1],
                }
            )

        with (
            patch.object(admin_api.settings, "agent_state_machine_arn", STATE_MACHINE_ARN),
            patch.object(admin_api, "_step_functions_client", return_value=client),
        ):
            with self.assertRaises(HTTPException) as raised:
                admin_api.admin_run_agent(
                    self._request(), db=_Db(), admin=SimpleNamespace(id=1)
                )

        self.assertEqual(raised.exception.status_code, 409)
        client.start_execution.assert_not_called()

    def test_running_unrelated_workflow_returns_conflict_without_duplicate(self) -> None:
        client = Mock()
        client.list_executions.return_value = {
            "executions": [{"executionArn": EXECUTION_ARN}]
        }
        client.describe_execution.return_value = {
            "status": "RUNNING",
            "input": json.dumps({"city_ids": [1], "autonomous_research": True}),
        }

        with (
            patch.object(admin_api.settings, "agent_state_machine_arn", STATE_MACHINE_ARN),
            patch.object(admin_api, "_step_functions_client", return_value=client),
            patch.object(admin_api.threading, "Thread") as thread_cls,
        ):
            with self.assertRaises(HTTPException) as raised:
                admin_api.admin_run_agent(
                    self._request(), db=_Db(), admin=SimpleNamespace(id=1)
                )

        self.assertEqual(raised.exception.status_code, 409)
        client.start_execution.assert_not_called()
        thread_cls.assert_not_called()

    def test_describe_error_preserves_running_execution_and_prevents_duplicate(self) -> None:
        with admin_api._agent_run_lock:
            admin_api._agent_run_state.update(
                {
                    "running": True,
                    "started_at": datetime.now(timezone.utc),
                    "finished_at": None,
                    "result": None,
                    "execution_arn": EXECUTION_ARN,
                    "backend": "step_functions",
                    "city_id": 2,
                }
            )
        client = Mock()
        client.describe_execution.side_effect = RuntimeError("status temporarily unavailable")

        with (
            patch.object(admin_api.settings, "agent_state_machine_arn", STATE_MACHINE_ARN),
            patch.object(admin_api, "_step_functions_client", return_value=client),
            patch.object(admin_api.threading, "Thread") as thread_cls,
        ):
            status = admin_api.admin_run_agent(
                self._request(), db=_Db(), admin=SimpleNamespace(id=1)
            )

        self.assertTrue(status.running)
        self.assertEqual(status.execution_arn, EXECUTION_ARN)
        client.start_execution.assert_not_called()
        thread_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
