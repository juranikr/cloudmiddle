from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock, patch

from app.agent.__main__ import (
    is_retryable_model_output_failure,
    is_retryable_network_block,
    report_step_function_result,
)


class AgentEntrypointTests(unittest.TestCase):
    def test_classifies_groq_network_403_as_retryable(self) -> None:
        result = {
            "status": "failed",
            "message": "Error code: 403 - {'error': {'message': 'Access denied. Please check your network settings.'}}",
        }

        self.assertTrue(is_retryable_network_block(result))

    def test_does_not_retry_unrelated_failure_or_partial_result(self) -> None:
        self.assertFalse(is_retryable_network_block({"status": "failed", "message": "database unavailable"}))
        self.assertFalse(
            is_retryable_network_block(
                {"status": "partial", "message": "Access denied. Please check your network settings."}
            )
        )

    def test_classifies_model_output_parse_failure_as_retryable(self) -> None:
        result = {
            "status": "failed",
            "message": "Error code: 400 - code='output_parse_failed' Parsing failed",
        }
        self.assertTrue(is_retryable_model_output_failure(result))
        self.assertFalse(is_retryable_model_output_failure({
            "status": "partial",
            "message": "output_parse_failed",
        }))

    @patch("app.agent.__main__.boto3.client")
    def test_reports_retryable_failure_to_step_functions(self, boto_client: Mock) -> None:
        client = boto_client.return_value
        results = [
            {
                "city_id": 2,
                "status": "failed",
                "message": "Error code: 403 - Access denied. Please check your network settings.",
            }
        ]

        with patch.dict(os.environ, {"AWS_REGION": "ap-northeast-2"}, clear=False):
            outcome = report_step_function_result("secret-task-token", results)

        self.assertEqual(outcome, "retryable_network_block")
        boto_client.assert_called_once_with("stepfunctions", region_name="ap-northeast-2")
        kwargs = client.send_task_failure.call_args.kwargs
        self.assertEqual(kwargs["taskToken"], "secret-task-token")
        self.assertEqual(kwargs["error"], "RetryableNetworkBlock")
        self.assertEqual(json.loads(kwargs["cause"])["cities"][0]["city_id"], 2)
        client.send_task_success.assert_not_called()

    @patch("app.agent.__main__.boto3.client")
    def test_reports_partial_result_as_success(self, boto_client: Mock) -> None:
        client = boto_client.return_value
        results = [{"city_id": 2, "status": "partial", "ok": False, "message": "more research remains"}]

        outcome = report_step_function_result("secret-task-token", results)

        self.assertEqual(outcome, "success")
        self.assertEqual(client.send_task_success.call_args.kwargs["taskToken"], "secret-task-token")
        client.send_task_failure.assert_not_called()

    @patch("app.agent.__main__.boto3.client")
    def test_reports_exhausted_model_output_failure_for_fresh_task_retry(self, boto_client: Mock) -> None:
        client = boto_client.return_value
        results = [{
            "city_id": 2,
            "status": "failed",
            "message": "output_parse_failed after three adaptive retries",
        }]

        outcome = report_step_function_result("secret-task-token", results)

        self.assertEqual(outcome, "retryable_model_output")
        self.assertEqual(client.send_task_failure.call_args.kwargs["error"], "RetryableModelOutput")

    @patch("app.agent.__main__.boto3.client")
    def test_reports_non_retryable_agent_failure(self, boto_client: Mock) -> None:
        client = boto_client.return_value
        results = [{"city_id": 1, "status": "failed", "message": "database unavailable"}]

        outcome = report_step_function_result("secret-task-token", results)

        self.assertEqual(outcome, "failed")
        self.assertEqual(client.send_task_failure.call_args.kwargs["error"], "AgentRunFailed")


if __name__ == "__main__":
    unittest.main()
