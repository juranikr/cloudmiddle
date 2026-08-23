import unittest

from app.admin_api import _agent_run_reporting


class AgentRunOutcomeReportingTests(unittest.TestCase):
    def test_traveler_visible_change_wins_over_proposal(self) -> None:
        report = _agent_run_reporting(
            status="completed",
            metrics={
                "material_change_count": 2,
                "material_changes": [
                    {"tool": "propose_place", "proposal_id": 31},
                    {"tool": "upsert_place_insights", "place_id": 96},
                ],
            },
        )

        self.assertEqual(report["outcome_category"], "traveler_visible_changed")
        self.assertEqual(report["material_change_count"], 2)

    def test_proposal_is_distinct_from_traveler_visible_change(self) -> None:
        report = _agent_run_reporting(
            status="completed",
            metrics={
                "delta": {"proposals": 1},
                "material_changes": [
                    {"tool": "propose_place", "proposal_id": 33},
                ],
            },
        )

        self.assertEqual(report["outcome_category"], "proposal_created")
        self.assertEqual(report["material_change_count"], 1)

    def test_audited_waiver_is_not_reported_as_no_yield(self) -> None:
        report = _agent_run_reporting(
            status="completed",
            metrics={
                "material_change_count": 0,
                "continuity": {
                    "mission_id": 12,
                    "status": "active",
                    "progress": {
                        "quality_dispositions": [
                            {"gap_kind": "zone", "status": "waived"},
                        ],
                    },
                },
            },
        )

        self.assertEqual(
            report["outcome_category"],
            "verified_or_waived_no_change",
        )

    def test_deferred_run_exposes_resume_cursor(self) -> None:
        report = _agent_run_reporting(
            status="completed",
            metrics={
                "outcome": "deferred",
                "lane": "discovery_deferred",
                "deferred_reason": "all_frontiers_cooling",
                "next_work_item_id": 124,
                "continuity": {
                    "mission_id": 29,
                    "work_item_id": 124,
                    "status": "paused",
                    "target": {"title": "선양 야시장 후보"},
                    "next_action": {"tool": "choose_alternative_source"},
                },
            },
        )

        self.assertEqual(report["outcome_category"], "deferred_or_blocked")
        self.assertEqual(report["next_work_item_id"], 124)
        self.assertEqual(report["next_cursor"]["mission_id"], 29)
        self.assertEqual(report["next_cursor"]["target"], "선양 야시장 후보")
        self.assertEqual(
            report["next_cursor"]["next_tool"],
            "choose_alternative_source",
        )

    def test_deterministic_queue_ack_has_its_own_outcome(self) -> None:
        for metrics in (
            {"outcome": "queue_acknowledged"},
            {"lane": "deterministic_queue_ack", "delta": {"unread_cleared": 3}},
        ):
            with self.subTest(metrics=metrics):
                report = _agent_run_reporting(status="completed", metrics=metrics)

                self.assertEqual(report["outcome_category"], "queue_acknowledged")
                self.assertEqual(report["material_change_count"], 0)

    def test_no_progress_counter_alone_does_not_claim_a_durable_block(self) -> None:
        report = _agent_run_reporting(
            status="completed",
            metrics={"no_progress_actions": 32},
        )

        self.assertEqual(report["outcome_category"], "no_yield")

    def test_internal_context_write_is_not_labeled_traveler_visible(self) -> None:
        report = _agent_run_reporting(
            status="completed",
            metrics={"material_changes": [{"tool": "update_place_context", "place_id": 96}]},
        )

        self.assertEqual(report["outcome_category"], "no_yield")

    def test_verification_timestamp_is_reported_as_audit_not_visible_change(self) -> None:
        report = _agent_run_reporting(
            status="completed",
            metrics={
                "material_changes": [{"tool": "verify_place", "place_id": 96}],
                "successful_tool_counts": {"verify_place": 1},
            },
        )

        self.assertEqual(
            report["outcome_category"],
            "verified_or_waived_no_change",
        )

    def test_normal_completion_without_audited_result_is_no_yield(self) -> None:
        report = _agent_run_reporting(
            status="completed",
            metrics={
                "material_change_count": 0,
                "material_changes": [],
                "continuity": {
                    "mission_id": 30,
                    "work_item_id": 125,
                    "status": "completed",
                },
            },
        )

        self.assertEqual(report["outcome_category"], "no_yield")
        self.assertIsNone(report["next_work_item_id"])
        self.assertEqual(report["next_cursor"], {})

    def test_overlapping_worker_is_reported_as_deferred(self) -> None:
        report = _agent_run_reporting(
            status="completed",
            metrics={"outcome": "already_running"},
        )

        self.assertEqual(report["outcome_category"], "deferred_or_blocked")

    def test_failed_process_is_failed_even_with_stale_metrics(self) -> None:
        report = _agent_run_reporting(
            status="failed",
            metrics={
                "material_changes": [
                    {"tool": "upsert_place_insights", "place_id": 96},
                ],
            },
        )

        self.assertEqual(report["outcome_category"], "failed")


if __name__ == "__main__":
    unittest.main()
