from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from unittest.mock import patch

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.agent.memory import (
    evaluate_quality_gap_disposition,
    filter_actionable_quality_gaps,
    quality_gaps_for_marker,
    record_quality_gap_disposition,
    rotate_blocked_work_item,
)
from app.db import Base
from app.migrate import ensure_schema
from app.models import (
    AgentQualityGapDisposition,
    AgentMission,
    AgentRun,
    AgentTask,
    AgentWorkItem,
    City,
    Marker,
    MarkerCategory,
    MarkerShape,
    PlaceImage,
)


class QualityGapDispositionTests(unittest.TestCase):
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
        self.place = Marker(
            city_id=2,
            category=MarkerCategory.lodging,
            shape=MarkerShape.point,
            title="瀋陽中街故宮漫心酒店",
            description="짧은 설명",
            lat=41.811,
            lng=123.449,
            coordinate_query="Beizhongjie Road No.118",
        )
        self.db.add(self.place)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _filter(self, *, now: datetime, revisions: dict[str, str] | None = None) -> list[str]:
        gaps = quality_gaps_for_marker(self.place)
        result = filter_actionable_quality_gaps(
            self.db,
            markers=[self.place],
            gaps_by_id={self.place.id: gaps},
            source_revisions=revisions,
            now=now,
        )
        return result[self.place.id]

    def test_source_exhausted_image_stays_closed_until_condition_changes(self) -> None:
        now = datetime(2026, 8, 23, 3, tzinfo=timezone.utc)
        row = record_quality_gap_disposition(
            self.db,
            marker=self.place,
            gap_kind="image",
            disposition="source_exhausted",
            reason="Wikimedia의 정확한 업장 실사진 후보를 모두 검토했으나 일치 항목이 없습니다.",
            evidence_refs=["run:120:step:14", "search:wikimedia:瀋陽中街故宮漫心酒店"],
            source_revision="wikimedia:v1|manual-upload:v1",
            now=now,
        )
        self.db.commit()

        self.assertNotIn(
            "image",
            self._filter(
                now=now + timedelta(days=30),
                revisions={"image": "wikimedia:v1|manual-upload:v1"},
            ),
        )
        self.assertEqual(row.status, "source_exhausted")

        # Enabling another legal source is a real retry condition; passage of
        # time by itself is not.
        self.assertIn(
            "image",
            self._filter(
                now=now + timedelta(days=30),
                revisions={"image": "wikimedia:v1|manual-upload:v1|new-provider:v1"},
            ),
        )
        self.assertEqual(row.status, "reopened")

    def test_waived_out_of_zone_reopens_when_zone_catalogue_changes(self) -> None:
        now = datetime(2026, 8, 23, 3, tzinfo=timezone.utc)
        row = record_quality_gap_disposition(
            self.db,
            marker=self.place,
            gap_kind="zone",
            disposition="waived",
            reason="현재 등록된 모든 구역 폴리곤을 확인했으나 호텔 좌표를 포함하는 구역이 없습니다.",
            evidence_refs=["run:120:step:20", "zones:city:2:v1"],
            now=now,
        )
        self.db.commit()
        self.assertNotIn("zone", self._filter(now=now + timedelta(days=60)))

        self.db.add(Marker(
            city_id=2,
            category=MarkerCategory.tourist,
            shape=MarkerShape.polygon,
            title="중제·고궁권",
            description="도보 관광 구역",
            lat=41.81,
            lng=123.45,
            polygon=(
                '[{"lat":41.80,"lng":123.44},{"lat":41.82,"lng":123.44},'
                '{"lat":41.82,"lng":123.46},{"lat":41.80,"lng":123.46}]'
            ),
        ))
        self.db.commit()

        self.assertIn("zone", self._filter(now=now + timedelta(days=60)))
        self.assertEqual(row.status, "reopened")

    def test_blocked_gap_is_cooled_then_reopened_once(self) -> None:
        now = datetime(2026, 8, 23, 3, tzinfo=timezone.utc)
        row = record_quality_gap_disposition(
            self.db,
            marker=self.place,
            gap_kind="verification",
            disposition="blocked",
            reason="현재 공급자 요청이 403으로 차단되어 다른 네트워크 조건을 기다립니다.",
            evidence_refs=["run:120:step:8"],
            cooldown_hours=48,
            now=now,
        )
        self.db.commit()

        early = evaluate_quality_gap_disposition(
            self.db,
            marker=self.place,
            gap_kind="verification",
            now=now + timedelta(hours=47),
        )
        self.assertFalse(early["actionable"])
        self.assertEqual(row.status, "blocked")

        due = evaluate_quality_gap_disposition(
            self.db,
            marker=self.place,
            gap_kind="verification",
            now=now + timedelta(hours=48),
        )
        self.assertTrue(due["actionable"])
        self.assertEqual(due["trigger"], "cooldown_elapsed")
        self.assertEqual(row.status, "reopened")

    def test_physical_resolution_retires_terminal_state_and_later_regression_is_actionable(self) -> None:
        now = datetime(2026, 8, 23, 3, tzinfo=timezone.utc)
        row = record_quality_gap_disposition(
            self.db,
            marker=self.place,
            gap_kind="image",
            disposition="source_exhausted",
            reason="현재 자유 라이선스 공급자에서 정확한 사진을 찾지 못했습니다.",
            evidence_refs=["run:120:step:14"],
            source_revision="wikimedia:v1",
            now=now,
        )
        self.place.images.append(PlaceImage(s3_key="places/2/hotel.jpg"))
        self.db.commit()

        self.assertNotIn("image", quality_gaps_for_marker(self.place))
        self._filter(now=now + timedelta(days=1), revisions={"image": "wikimedia:v1"})
        self.assertEqual(row.status, "resolved")

        self.place.images.clear()
        self.db.commit()
        self.assertIn(
            "image",
            self._filter(now=now + timedelta(days=2), revisions={"image": "wikimedia:v1"}),
        )

    def test_terminal_disposition_requires_auditable_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires evidence_refs"):
            record_quality_gap_disposition(
                self.db,
                marker=self.place,
                gap_kind="zone",
                disposition="waived",
                reason="구역 밖으로 판단했습니다.",
            )
        self.assertEqual(self.db.query(AgentQualityGapDisposition).count(), 0)

    def test_rotation_can_atomically_record_an_exact_terminal_gap(self) -> None:
        task = AgentTask(
            city_id=2,
            kind="quality_images",
            title="사진 보강",
            status="pending",
        )
        run = AgentRun(city_id=2, status="running")
        self.db.add_all([task, run])
        self.db.flush()
        mission = AgentMission(
            city_id=2,
            task_id=task.id,
            kind=task.kind,
            title=task.title,
            status="active",
        )
        self.db.add(mission)
        self.db.flush()
        item = AgentWorkItem(
            mission_id=mission.id,
            city_id=2,
            place_id=self.place.id,
            target_type="place",
            target_key=f"place:{self.place.id}",
            title=self.place.title,
            status="active",
        )
        self.db.add(item)
        self.db.commit()

        rotate_blocked_work_item(
            self.db,
            mission=mission,
            current=item,
            run_id=run.id,
            reason="정확한 업장 사진 후보 3개가 모두 다른 장소이거나 자유 라이선스가 아닙니다.",
            quality_disposition="source_exhausted",
            quality_gap_kinds=["image"],
            quality_evidence_refs=[f"run:{run.id}:step:12"],
            quality_source_revision="wikimedia:v1",
        )

        disposition = self.db.query(AgentQualityGapDisposition).one()
        self.assertEqual(disposition.gap_kind, "image")
        self.assertEqual(disposition.status, "source_exhausted")
        self.assertEqual(item.status, "blocked")
        self.assertIn("조건 지문", item.retry_condition)
        self.assertNotIn(
            "image",
            self._filter(
                now=datetime.now(timezone.utc) + timedelta(days=365),
                revisions={"image": "wikimedia:v1"},
            ),
        )

    def test_custom_migration_bootstraps_disposition_table_idempotently(self) -> None:
        AgentQualityGapDisposition.__table__.drop(bind=self.engine)
        self.assertNotIn(
            "agent_quality_gap_dispositions",
            inspect(self.engine).get_table_names(),
        )

        with patch("app.migrate.engine", self.engine):
            ensure_schema()
            ensure_schema()

        self.assertIn(
            "agent_quality_gap_dispositions",
            inspect(self.engine).get_table_names(),
        )


if __name__ == "__main__":
    unittest.main()
