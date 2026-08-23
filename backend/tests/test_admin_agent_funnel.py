import json
import unittest

from app.admin_api import _agent_discovery_funnel
from app.models import AgentRunStep


def _step(tool: str, result: dict, *, sequence: int) -> AgentRunStep:
    return AgentRunStep(
        run_id=1,
        sequence=sequence,
        tool=tool,
        detail=json.dumps({"args": {}, "result": result}),
    )


class AgentDiscoveryFunnelTests(unittest.TestCase):
    def test_reports_each_conversion_stage_separately(self) -> None:
        steps = [
            _step(
                "web_search",
                {
                    "raw_results_count": 20,
                    "results": [{"href": "https://one.example"}, {"href": "https://two.example"}],
                    "provider_attempts": [
                        {"provider": "brave_place", "status": "transient_discarded"},
                    ],
                },
                sequence=1,
            ),
            _step("fetch_page", {"text": "validated body"}, sequence=2),
            _step("fetch_page", {"error": "HTTP 403"}, sequence=3),
            _step("geocode_place", {"results": [{"lat": 1}, {"lat": 2}]}, sequence=4),
            _step("propose_place", {"proposal_created": True, "proposal_id": 9}, sequence=5),
        ]

        self.assertEqual(
            _agent_discovery_funnel(steps),
            {
                "search_calls": 1,
                "place_discovery_calls": 1,
                "raw_hits": 20,
                "exposed_hits": 2,
                "validated_pages": 1,
                "geocode_calls": 1,
                "geocode_candidates": 2,
                "proposal_attempts": 1,
                "proposals_created": 1,
            },
        )

    def test_malformed_step_does_not_break_history(self) -> None:
        step = AgentRunStep(run_id=1, sequence=1, tool="web_search", detail="not-json")
        funnel = _agent_discovery_funnel([step])
        self.assertEqual(funnel["search_calls"], 0)
        self.assertEqual(funnel["proposals_created"], 0)


if __name__ == "__main__":
    unittest.main()
