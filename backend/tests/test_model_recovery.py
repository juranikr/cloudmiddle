import unittest

from app.agent.model_recovery import (
    allowed_tools_after_failure,
    classify_failure,
    make_recovery_plan,
    recovery_mode,
)


class ModelFailureClassificationTests(unittest.TestCase):
    def test_classifies_provider_output_and_tool_schema_failures(self) -> None:
        self.assertEqual(
            classify_failure({
                "error": {
                    "message": "Parsing failed. The model generated output that could not be parsed.",
                    "code": "output_parse_failed",
                    "failed_generation": "",
                }
            }),
            "output_parse_failed",
        )
        self.assertEqual(
            classify_failure("Error code: 400 - tool_use_failed: tool call validation failed"),
            "tool_schema_failed",
        )

    def test_classifies_local_malformed_arguments(self) -> None:
        self.assertEqual(
            classify_failure("malformed_tool_arguments: Expecting property name enclosed in double quotes"),
            "malformed_tool_arguments",
        )

    def test_classifies_transient_and_infrastructure_failures(self) -> None:
        cases = {
            "Error code: 429 - rate_limit_exceeded": "rate_limit",
            "429 Too Many Requests": "rate_limit",
            "httpx.ReadTimeout: request timed out": "timeout",
            "Error code: 403 - request blocked for this public IP": "network_block",
            "403 Forbidden": "network_block",
            "Error code: 503 - service unavailable": "provider_unavailable",
            "context_length_exceeded: maximum context length": "context_limit",
            "Error code: 401 - invalid API key": "authorization_failed",
        }
        for error, expected in cases.items():
            with self.subTest(error=error):
                self.assertEqual(classify_failure(error), expected)

    def test_unknown_failure_remains_explicit(self) -> None:
        self.assertEqual(classify_failure("database invariant failed"), "unknown")


class RecoveryPolicyTests(unittest.TestCase):
    def test_recovery_modes_escalate_and_remain_minimal(self) -> None:
        self.assertEqual(recovery_mode(1), "focused_retry")
        self.assertEqual(recovery_mode(2), "compact_retry")
        self.assertEqual(recovery_mode(3), "minimal_retry")
        self.assertEqual(recovery_mode(8), "minimal_retry")
        with self.assertRaises(ValueError):
            recovery_mode(0)

    def test_focused_schema_recovery_retries_only_the_failed_tool(self) -> None:
        allowed = allowed_tools_after_failure(
            "tool_schema_failed",
            attempt=1,
            phase="write",
            available_tools={"web_search", "fetch_page", "propose_place"},
            last_tool="propose_place",
        )
        self.assertEqual(allowed, frozenset({"propose_place"}))

    def test_tool_precondition_error_routes_to_the_tool_that_can_fix_it(self) -> None:
        self.assertEqual(
            allowed_tools_after_failure(
                "fact_source_not_fetched",
                available_tools={"web_search", "fetch_page", "geocode_place", "propose_place"},
            ),
            frozenset({"fetch_page"}),
        )
        self.assertEqual(
            allowed_tools_after_failure(
                "coordinate_target_not_verified",
                available_tools={"web_search", "fetch_page", "geocode_place", "propose_place"},
            ),
            frozenset({"web_search", "fetch_page", "geocode_place"}),
        )
        self.assertEqual(
            allowed_tools_after_failure(
                "candidate_target_changed",
                available_tools={"web_search", "propose_place"},
            ),
            frozenset({"propose_place"}),
        )

    def test_available_tools_are_a_hard_capability_boundary(self) -> None:
        self.assertEqual(
            allowed_tools_after_failure(
                "coordinate_not_grounded",
                available_tools={"fetch_page"},
            ),
            frozenset({"fetch_page"}),
        )
        self.assertEqual(
            allowed_tools_after_failure(
                "fact_source_not_fetched",
                available_tools=set(),
            ),
            frozenset(),
        )

    def test_duplicate_failure_removes_the_repeated_tool(self) -> None:
        allowed = allowed_tools_after_failure(
            "recent_duplicate_search",
            phase="research",
            available_tools={"web_search", "fetch_page", "geocode_place"},
            last_tool="web_search",
        )
        self.assertEqual(allowed, frozenset({"fetch_page", "geocode_place"}))

    def test_minimal_retry_exposes_one_deterministic_tool(self) -> None:
        allowed = allowed_tools_after_failure(
            "output_parse_failed",
            attempt=3,
            phase="locate",
            available_tools={"web_search", "fetch_page", "geocode_place"},
            next_tool="geocode_place",
        )
        self.assertEqual(allowed, frozenset({"geocode_place"}))

    def test_minimal_transient_retry_uses_stable_phase_order(self) -> None:
        allowed = allowed_tools_after_failure(
            "timeout",
            attempt=3,
            phase="locate",
            available_tools={"web_search", "fetch_page", "geocode_place"},
        )
        self.assertEqual(allowed, frozenset({"geocode_place"}))

    def test_composed_plan_matches_batch_recovery_limits(self) -> None:
        plan = make_recovery_plan(
            "Error code: 400 - output_parse_failed",
            attempt=2,
            phase="write",
            available_tools={"web_search", "fetch_page", "propose_place"},
        )
        self.assertEqual(plan.failure_kind, "output_parse_failed")
        self.assertEqual(plan.mode, "compact_retry")
        self.assertEqual(plan.reasoning_effort, "low")
        self.assertTrue(plan.force_compaction)
        self.assertEqual(plan.recent_round_limit, 3)
        self.assertEqual(plan.max_context_chars, 42_000)
        self.assertEqual(plan.allowed_tools, frozenset({"propose_place"}))


if __name__ == "__main__":
    unittest.main()
