"""Durable task memory and context-aware knowledge retrieval for batch agents.

This module stores compact, auditable state summaries.  It intentionally does
not persist hidden model reasoning; checkpoints contain only facts, decisions,
failed approaches, and the next executable action.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    AgentCheckpoint,
    AgentEvidence,
    AgentKnowledge,
    AgentKnowledgeUse,
    AgentLesson,
    AgentMission,
    AgentQualityGapDisposition,
    AgentRun,
    AgentTask,
    AgentWorkItem,
    Marker,
    MarkerShape,
)


_TARGET_RE = re.compile(r"(?m)^\s*-\s*#(?P<id>\d+)\s+(?P<title>.+?)(?:\s+\(현재:.*)?$")
_TOKEN_RE = re.compile(r"[0-9A-Za-z_\-]{2,}|[가-힣]{2,}|[\u3400-\u9fff]{2,}")
_STOPWORDS = {
    "현재", "장소", "자동", "품질", "보강", "실행", "대상", "정보", "작업", "agent",
    "place", "quality", "research", "검증", "사진", "운영", "존재", "있는", "없는",
}
CORRECTIVE_POLICY_GUARD_ERRORS = frozenset({
    "duplicate_data_integrity_place_read",
    "data_integrity_task_list_budget_exhausted",
    "active_agent_task_mismatch",
    "invalid_data_integrity_task_status",
    "invalid_data_integrity_task_result",
    "structured_integrity_verdict_required",
    "tool_not_allowed_for_data_integrity",
    "material_decision_required",
    "recent_duplicate_search",
    "duplicate_tool_call",
})
POLICY_GUARD_DECIDE_ERRORS = frozenset({
    "duplicate_data_integrity_place_read",
    "data_integrity_task_list_budget_exhausted",
    "material_decision_required",
    "structured_integrity_verdict_required",
})
QUALITY_GAP_KINDS = frozenset({"image", "zone", "verification", "description", "insights"})
QUALITY_GAP_DISPOSITIONS = frozenset({"blocked", "source_exhausted", "waived"})
TERMINAL_QUALITY_GAP_DISPOSITIONS = frozenset({"source_exhausted", "waived"})
QUALITY_GAPS_BY_TASK_KIND = {
    "quality_images": frozenset({"image"}),
    "quality_zones": frozenset({"zone"}),
    "quality_verification": frozenset({"verification"}),
    "quality_information": frozenset({"description", "insights"}),
    "quality_drafts": QUALITY_GAP_KINDS,
}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def quality_gaps_for_marker(marker: Marker) -> list[str]:
    """Return the physical DB gaps understood by managed quality missions."""

    shape = marker.shape.value if isinstance(marker.shape, MarkerShape) else str(marker.shape or "")
    if shape != MarkerShape.point.value or marker.merged_into_id is not None:
        return []
    gaps: list[str] = []
    if not marker.images:
        gaps.append("image")
    if marker.zone_id is None:
        gaps.append("zone")
    if marker.last_verified_at is None:
        gaps.append("verification")
    if len((marker.description or "").strip()) < 60:
        gaps.append("description")
    if len(marker.insights or []) < 2:
        gaps.append("insights")
    return gaps


def _zone_catalogue_signature(db: Session, *, city_id: int) -> list[dict[str, Any]]:
    zones = (
        db.query(Marker)
        .filter(
            Marker.city_id == city_id,
            Marker.shape == MarkerShape.polygon,
            Marker.merged_into_id.is_(None),
        )
        .order_by(Marker.id.asc())
        .all()
    )
    return [
        {
            "id": zone.id,
            "title": zone.title,
            "lat": round(float(zone.lat), 7),
            "lng": round(float(zone.lng), 7),
            "polygon": zone.polygon or "",
        }
        for zone in zones
    ]


def quality_gap_condition_fingerprint(
    db: Session,
    *,
    marker: Marker,
    gap_kind: str,
    source_revision: str = "",
) -> str:
    """Hash only conditions whose change justifies retrying an exact gap.

    ``source_revision`` is an opaque, non-secret provider/policy version such
    as ``"wikimedia:v1|manual-upload:v1"``.  Callers bump it when a new source
    becomes available; API keys themselves must never be stored here.
    """

    if gap_kind not in QUALITY_GAP_KINDS:
        raise ValueError(f"unsupported quality gap: {gap_kind}")
    common: dict[str, Any] = {
        "city_id": marker.city_id,
        "place_id": marker.id,
        "title": marker.title,
        "branch_name": marker.branch_name or "",
        "lat": round(float(marker.lat), 7),
        "lng": round(float(marker.lng), 7),
        "merged_into_id": marker.merged_into_id,
        "source_revision": str(source_revision or "")[:200],
    }
    if gap_kind == "image":
        common.update({
            "coordinate_query": marker.coordinate_query or "",
            "coordinate_external_id": marker.coordinate_external_id or "",
        })
    elif gap_kind == "zone":
        common["zones"] = _zone_catalogue_signature(db, city_id=marker.city_id)
    elif gap_kind == "verification":
        common.update({
            "coordinate_source": marker.coordinate_source or "",
            "coordinate_source_url": marker.coordinate_source_url or "",
        })
    elif gap_kind == "description":
        common["description"] = marker.description or ""
    elif gap_kind == "insights":
        common["insights"] = [
            {
                "id": insight.id,
                "kind": insight.kind,
                "title": insight.title,
                "content": insight.content,
                "source_url": insight.source_url,
            }
            for insight in marker.insights
        ]
    return hashlib.sha256(_dump(common).encode("utf-8")).hexdigest()


def record_quality_gap_disposition(
    db: Session,
    *,
    marker: Marker,
    gap_kind: str,
    disposition: str,
    reason: str,
    evidence_refs: Iterable[str] = (),
    source_revision: str = "",
    cooldown_hours: float = 24,
    now: Optional[datetime] = None,
) -> AgentQualityGapDisposition:
    """Close or cool one exact gap without pretending the marker field exists.

    Terminal decisions require auditable evidence.  ``blocked`` is temporary;
    ``source_exhausted`` and ``waived`` remain suppressed until a fingerprinted
    condition changes or the physical gap is first resolved and later returns.
    The caller owns the surrounding transaction.
    """

    if gap_kind not in QUALITY_GAP_KINDS:
        raise ValueError(f"unsupported quality gap: {gap_kind}")
    if disposition not in QUALITY_GAP_DISPOSITIONS:
        raise ValueError(f"unsupported quality gap disposition: {disposition}")
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise ValueError("quality gap disposition requires a reason")
    refs = list(dict.fromkeys(str(ref).strip() for ref in evidence_refs if str(ref).strip()))
    if disposition in TERMINAL_QUALITY_GAP_DISPOSITIONS and not refs:
        raise ValueError(f"{disposition} quality gap disposition requires evidence_refs")
    if gap_kind not in quality_gaps_for_marker(marker):
        raise ValueError(f"marker #{marker.id} no longer has the {gap_kind} quality gap")

    timestamp = now or datetime.now(timezone.utc)
    row = db.query(AgentQualityGapDisposition).filter(
        AgentQualityGapDisposition.place_id == marker.id,
        AgentQualityGapDisposition.gap_kind == gap_kind,
    ).first()
    if row is None:
        row = AgentQualityGapDisposition(
            city_id=marker.city_id,
            place_id=marker.id,
            gap_kind=gap_kind,
            condition_fingerprint="",
        )
        db.add(row)
    else:
        row.attempt_count = int(row.attempt_count or 0) + 1
    row.city_id = marker.city_id
    row.status = disposition
    row.reason = normalized_reason[:4000]
    row.evidence_refs = _dump(refs[:30])
    row.source_revision = str(source_revision or "")[:200]
    row.condition_fingerprint = quality_gap_condition_fingerprint(
        db,
        marker=marker,
        gap_kind=gap_kind,
        source_revision=row.source_revision,
    )
    row.retry_after = (
        timestamp + timedelta(hours=max(0.25, float(cooldown_hours)))
        if disposition == "blocked"
        else None
    )
    row.resolved_at = None
    row.updated_at = timestamp
    db.flush()
    return row


def evaluate_quality_gap_disposition(
    db: Session,
    *,
    marker: Marker,
    gap_kind: str,
    source_revision: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return whether an exact gap is executable, reopening only on a trigger."""

    if gap_kind not in QUALITY_GAP_KINDS:
        raise ValueError(f"unsupported quality gap: {gap_kind}")
    timestamp = now or datetime.now(timezone.utc)
    row = db.query(AgentQualityGapDisposition).filter(
        AgentQualityGapDisposition.place_id == marker.id,
        AgentQualityGapDisposition.gap_kind == gap_kind,
    ).first()
    if row is None or row.status in {"resolved", "reopened"}:
        return {"actionable": True, "trigger": "untracked" if row is None else row.status, "row": row}

    effective_revision = row.source_revision if source_revision is None else str(source_revision or "")
    current_fingerprint = quality_gap_condition_fingerprint(
        db,
        marker=marker,
        gap_kind=gap_kind,
        source_revision=effective_revision,
    )
    trigger = ""
    if current_fingerprint != row.condition_fingerprint:
        trigger = "condition_changed"
    elif row.status == "blocked" and row.retry_after is not None and _utc(timestamp) >= _utc(row.retry_after):
        trigger = "cooldown_elapsed"
    if trigger:
        row.status = "reopened"
        row.resolved_at = timestamp
        row.updated_at = timestamp
        db.flush()
        return {"actionable": True, "trigger": trigger, "row": row}
    return {
        "actionable": False,
        "trigger": row.status,
        "retry_after": row.retry_after,
        "reason": row.reason,
        "row": row,
    }


def filter_actionable_quality_gaps(
    db: Session,
    *,
    markers: Iterable[Marker],
    gaps_by_id: dict[int, list[str]],
    source_revisions: Optional[dict[str, str]] = None,
    now: Optional[datetime] = None,
) -> dict[int, list[str]]:
    """Remove terminal/cooling gaps before managed quality tasks are built.

    Physical resolution retires the disposition.  If the same physical gap is
    introduced later (for example an image is deleted), it is actionable again.
    """

    timestamp = now or datetime.now(timezone.utc)
    marker_rows = list(markers)
    marker_by_id = {marker.id: marker for marker in marker_rows}
    if not marker_by_id:
        return {}
    dispositions = db.query(AgentQualityGapDisposition).filter(
        AgentQualityGapDisposition.place_id.in_(marker_by_id),
    ).all()
    current_gap_sets = {
        marker_id: set(gaps_by_id.get(marker_id, []))
        for marker_id in marker_by_id
    }
    for row in dispositions:
        if row.gap_kind not in current_gap_sets.get(row.place_id, set()) and row.status != "resolved":
            row.status = "resolved"
            row.resolved_at = timestamp
            row.updated_at = timestamp

    revisions = source_revisions or {}
    actionable: dict[int, list[str]] = {}
    for marker_id, gaps in gaps_by_id.items():
        marker = marker_by_id.get(marker_id)
        if marker is None:
            continue
        kept: list[str] = []
        for gap_kind in gaps:
            if gap_kind not in QUALITY_GAP_KINDS:
                kept.append(gap_kind)
                continue
            evaluation = evaluate_quality_gap_disposition(
                db,
                marker=marker,
                gap_kind=gap_kind,
                source_revision=revisions.get(gap_kind) if gap_kind in revisions else None,
                now=timestamp,
            )
            if evaluation["actionable"]:
                kept.append(gap_kind)
        actionable[marker_id] = kept
    db.flush()
    return actionable


def _json_value(raw: str, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return fallback
    return value


def _json_list(raw: str) -> list[str]:
    value = _json_value(raw, [])
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _json_dict(raw: str) -> dict[str, Any]:
    value = _json_value(raw, {})
    return value if isinstance(value, dict) else {}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _db_text(value: Any, limit: int) -> str:
    """Normalize untrusted web text before storing it in PostgreSQL text."""

    return str(value or "").replace("\x00", "")[:limit]


def _tokens(*values: Any) -> set[str]:
    text = " ".join(str(value or "") for value in values).casefold()
    return {
        token
        for token in _TOKEN_RE.findall(text)
        if token not in _STOPWORDS and len(token) >= 2
    }


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def _place_targets(db: Session, task: AgentTask) -> list[tuple[Marker, str]]:
    matches = [(int(m.group("id")), m.group("title").strip()) for m in _TARGET_RE.finditer(task.detail or "")]
    if not matches:
        return []
    rows = {
        row.id: row
        for row in db.query(Marker).filter(
            Marker.city_id == task.city_id,
            Marker.id.in_([place_id for place_id, _ in matches]),
        )
    }
    return [(rows[place_id], title) for place_id, title in matches if place_id in rows]


def ensure_mission_for_task(db: Session, task: AgentTask) -> tuple[AgentMission, AgentWorkItem]:
    """Create or resume the durable mission and its exact target work items."""

    mission = (
        db.query(AgentMission)
        .filter(
            AgentMission.city_id == task.city_id,
            AgentMission.task_id == task.id,
            AgentMission.status.in_(("active", "paused")),
        )
        .order_by(AgentMission.id.desc())
        .first()
    )
    if mission is None:
        mission = AgentMission(
            city_id=task.city_id,
            task_id=task.id,
            kind=task.kind,
            title=task.title,
            objective=task.detail,
            success_metric=task.success_metric,
            status="active",
            priority=task.priority,
            strategy=_dump({"resume_policy": "checkpoint_first", "target_rotation": "blocked_then_next"}),
            progress="{}",
        )
        db.add(mission)
        db.flush()
    else:
        mission.kind = task.kind
        mission.title = task.title
        mission.objective = task.detail
        mission.success_metric = task.success_metric
        mission.priority = task.priority
        mission.status = "active"

    targets = _place_targets(db, task)
    live_keys: set[str] = set()
    for index, (place, parsed_title) in enumerate(targets):
        target_key = f"place:{place.id}"
        live_keys.add(target_key)
        item = db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.target_key == target_key,
        ).first()
        if item is None:
            item = AgentWorkItem(
                mission_id=mission.id,
                city_id=task.city_id,
                place_id=place.id,
                target_type="place",
                target_key=target_key,
                title=parsed_title or place.title,
                goal=task.title,
                definition_of_done=task.success_metric,
                stage="observe",
                status="ready",
                priority=max(1, task.priority - index),
                next_action=_dump({"tool": "get_place", "args": {"place_id": place.id}, "purpose": "현재 상태 확인"}),
            )
            db.add(item)
        else:
            item.title = parsed_title or place.title
            item.goal = task.title
            item.definition_of_done = task.success_metric
            item.priority = max(1, task.priority - index)
            if item.status == "superseded":
                item.status = "ready"

    if not targets:
        target_key = f"task:{task.id}"
        live_keys.add(target_key)
        item = db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.target_key == target_key,
        ).first()
        if item is None:
            item = AgentWorkItem(
                mission_id=mission.id,
                city_id=task.city_id,
                target_type="task",
                target_key=target_key,
                title=task.title,
                goal=task.detail,
                definition_of_done=task.success_metric,
                stage="observe",
                status="ready",
                priority=task.priority,
                next_action=_dump({"tool": "list_agent_tasks", "purpose": "과제 현황 확인"}),
            )
            db.add(item)

    db.flush()
    for stale in db.query(AgentWorkItem).filter(
        AgentWorkItem.mission_id == mission.id,
        AgentWorkItem.status.in_(("ready", "active")),
    ).all():
        if stale.target_key not in live_keys:
            stale.status = "superseded"
            stale.state_summary = "현재 운영 DB의 과제 대상 목록에서 제외됨"

    active = db.query(AgentWorkItem).filter(
        AgentWorkItem.mission_id == mission.id,
        AgentWorkItem.status == "active",
    ).order_by(AgentWorkItem.updated_at.desc()).first()
    if active is None:
        active = db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.status == "ready",
        ).order_by(AgentWorkItem.priority.desc(), AgentWorkItem.id.asc()).first()
    if active is None:
        active = db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.status == "blocked",
        ).order_by(AgentWorkItem.updated_at.asc()).first()
    if active is None:
        # The live target set may have been completed between task synchronization and this call.
        active = db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission.id).first()
    if active is None:
        raise RuntimeError(f"mission {mission.id} has no work item")
    if active.status == "blocked":
        # A paused mission is selected again only after its retry condition or
        # cooldown has elapsed. Keep the auditable checkpoints/lessons, while
        # giving the new attempt a fresh consecutive-failure budget.
        active.failed_approaches = "[]"
        active.retry_condition = ""
        active.status = "active"
        active.blocked_reason = ""
    elif active.status == "ready":
        active.status = "active"
        active.blocked_reason = ""
    active.attempts += 1
    db.commit()
    return mission, active


def mission_context(mission: AgentMission, item: AgentWorkItem) -> dict[str, Any]:
    return {
        "mission_id": mission.id,
        "mission": mission.title,
        "objective": mission.objective,
        "success_metric": mission.success_metric,
        "progress": _json_dict(mission.progress),
        "strategy": _json_dict(mission.strategy),
        "work_item_id": item.id,
        "target": {"type": item.target_type, "key": item.target_key, "place_id": item.place_id, "title": item.title},
        "stage": item.stage,
        "status": item.status,
        "state_summary": item.state_summary,
        "current_hypothesis": item.current_hypothesis,
        "failed_approaches": _json_list(item.failed_approaches)[-8:],
        "blocked_reason": item.blocked_reason,
        "retry_condition": item.retry_condition,
        "evidence_summary": item.evidence_summary,
        "next_action": _json_dict(item.next_action),
    }


def _knowledge_score(row: AgentKnowledge, context_tokens: set[str], *, city_id: int, place_id: Optional[int], categories: set[str]) -> tuple[float, str]:
    if row.place_id is not None and row.place_id != place_id:
        return -1000.0, "다른 장소 전용 지식"
    if row.scope == "city" and row.city_id != city_id:
        return -1000.0, "다른 도시 전용 지식"
    row_tokens = _tokens(
        row.topic, row.title, row.summary, row.content, row.principles,
        row.next_actions, row.keywords, row.applicability,
    )
    overlap = context_tokens & row_tokens
    scope_score = 7.0 if row.place_id == place_id and place_id is not None else 4.0 if row.city_id == city_id else 1.5
    category_score = 2.5 if row.category in categories else 0.0
    score = scope_score + category_score + min(7.0, len(overlap) * 1.4) + float(row.quality_score or 0) * 2.0
    reason_parts = [f"scope={row.scope}"]
    if row.category in categories:
        reason_parts.append(f"category={row.category}")
    if overlap:
        reason_parts.append("terms=" + ",".join(sorted(overlap)[:6]))
    return score, "; ".join(reason_parts)


def retrieve_contextual_knowledge(
    db: Session,
    *,
    city_id: int,
    mission: Optional[AgentMission] = None,
    work_item: Optional[AgentWorkItem] = None,
    query: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Retrieve only knowledge and lessons applicable to the current state."""

    place_id = work_item.place_id if work_item else None
    context_tokens = _tokens(
        query,
        mission.kind if mission else "",
        mission.title if mission else "",
        mission.objective if mission else "",
        mission.success_metric if mission else "",
        mission.strategy if mission else "",
        work_item.title if work_item else "",
        work_item.goal if work_item else "",
        work_item.stage if work_item else "",
        work_item.state_summary if work_item else "",
        work_item.current_hypothesis if work_item else "",
        work_item.failed_approaches if work_item else "",
        work_item.blocked_reason if work_item else "",
        work_item.next_action if work_item else "",
    )
    context_text = " ".join(
        str(value or "")
        for value in (
            query,
            mission.kind if mission else "",
            mission.objective if mission else "",
            mission.strategy if mission else "",
            work_item.stage if work_item else "",
            work_item.failed_approaches if work_item else "",
            work_item.blocked_reason if work_item else "",
            work_item.next_action if work_item else "",
        )
    ).casefold()
    task_kind = mission.kind if mission else ""
    categories = {"playbook", "quality"}
    if "verification" in task_kind:
        categories.update(("source", "workflow"))
    if "image" in task_kind:
        categories.update(("source", "quality"))
    if "zone" in task_kind:
        categories.update(("data_model", "city"))
    if "draft" in task_kind or "information" in task_kind:
        categories.update(("city", "data_model"))

    rows = db.query(AgentKnowledge).filter(
        AgentKnowledge.status == "active",
        or_(
            AgentKnowledge.scope == "global",
            AgentKnowledge.city_id == city_id,
            AgentKnowledge.place_id == place_id if place_id is not None else False,
        ),
    ).all()
    scored: list[tuple[float, str, AgentKnowledge]] = []
    for row in rows:
        applicability = _json_dict(row.applicability)
        domains = [str(item).casefold() for item in applicability.get("domains", [])]
        task_kinds = [str(item).casefold() for item in applicability.get("task_kinds", [])]
        stages = [str(item).casefold() for item in applicability.get("stages", [])]
        if domains and not any(
            domain in context_text or domain.split(".")[0] in context_text
            for domain in domains
        ):
            continue
        if task_kinds and not any(kind in (mission.kind.casefold() if mission else "") for kind in task_kinds):
            continue
        if stages and (work_item is None or work_item.stage.casefold() not in stages):
            continue
        score, reason = _knowledge_score(
            row, context_tokens, city_id=city_id, place_id=place_id, categories=categories
        )
        if score > 0:
            scored.append((score, reason, row))
    scored.sort(key=lambda entry: (-entry[0], -(entry[2].quality_score or 0), entry[2].id))

    lesson_rows = db.query(AgentLesson).filter(
        AgentLesson.status.in_(("validated", "testing", "candidate")),
        or_(AgentLesson.scope == "global", AgentLesson.city_id == city_id, AgentLesson.place_id == place_id),
    ).all()
    lesson_scored: list[tuple[float, str, AgentLesson]] = []
    for lesson in lesson_rows:
        lesson_tokens = _tokens(lesson.lesson_key, lesson.category, lesson.trigger, lesson.action, lesson.applicability)
        overlap = context_tokens & lesson_tokens
        scope_score = 6.0 if lesson.place_id == place_id and place_id else 3.5 if lesson.city_id == city_id else 1.5
        status_score = 3.0 if lesson.status == "validated" else 1.0 if lesson.status == "testing" else 0.0
        score = scope_score + status_score + min(7.0, len(overlap) * 1.5) + lesson.confidence * 2
        applicability = _json_dict(lesson.applicability)
        domains = [str(item).casefold() for item in applicability.get("domains", [])]
        task_kinds = [str(item).casefold() for item in applicability.get("task_kinds", [])]
        stages = [str(item).casefold() for item in applicability.get("stages", [])]
        if domains and not any(
            domain in context_text or domain.split(".")[0] in context_text
            for domain in domains
        ):
            continue
        if task_kinds and not any(kind in (mission.kind.casefold() if mission else "") for kind in task_kinds):
            continue
        if stages and (work_item is None or work_item.stage.casefold() not in stages):
            continue
        if lesson.status == "candidate" and not overlap and not (domains or task_kinds or stages):
            continue
        reason = f"status={lesson.status}; scope={lesson.scope}"
        if overlap:
            reason += "; terms=" + ",".join(sorted(overlap)[:6])
        lesson_scored.append((score, reason, lesson))
    lesson_scored.sort(key=lambda entry: (-entry[0], -entry[2].confidence, entry[2].id))

    return {
        "context_terms": sorted(context_tokens)[:30],
        "knowledge": [
            {
                "id": row.id,
                "topic": row.topic,
                "title": row.title,
                "category": row.category,
                "scope": row.scope,
                "summary": (row.summary or row.content or "")[:900],
                "principles": _json_list(row.principles)[:8],
                "next_actions": _json_list(row.next_actions)[:6],
                "applicability": _json_dict(row.applicability),
                "score": round(score, 2),
                "reason": reason,
            }
            for score, reason, row in scored[: max(1, limit)]
        ],
        "lessons": [
            {
                "id": row.id,
                "key": row.lesson_key,
                "status": row.status,
                "trigger": row.trigger,
                "action": row.action,
                "expected_effect": row.expected_effect,
                "confidence": row.confidence,
                "score": round(score, 2),
                "reason": reason,
            }
            for score, reason, row in lesson_scored[:6]
        ],
    }


def record_knowledge_uses(
    db: Session,
    *,
    run: AgentRun,
    mission: Optional[AgentMission],
    work_item: Optional[AgentWorkItem],
    retrieved: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc)
    for item in retrieved.get("knowledge") or []:
        row = db.get(AgentKnowledge, int(item["id"]))
        if row is None:
            continue
        row.retrieval_count = int(row.retrieval_count or 0) + 1
        row.last_retrieved_at = now
        db.add(AgentKnowledgeUse(
            knowledge_id=row.id,
            run_id=run.id,
            mission_id=mission.id if mission else None,
            work_item_id=work_item.id if work_item else None,
            relevance_score=float(item.get("score") or 0),
            retrieval_reason=str(item.get("reason") or "")[:2000],
        ))
    for item in retrieved.get("lessons") or []:
        row = db.get(AgentLesson, int(item["id"]))
        if row is None:
            continue
        row.applied_count = int(row.applied_count or 0) + 1
        row.last_applied_run_id = run.id
        db.add(AgentKnowledgeUse(
            lesson_id=row.id,
            run_id=run.id,
            mission_id=mission.id if mission else None,
            work_item_id=work_item.id if work_item else None,
            relevance_score=float(item.get("score") or 0),
            retrieval_reason=str(item.get("reason") or "")[:2000],
        ))
    db.commit()


def _result_urls(tool: str, args: dict[str, Any], result: Any) -> Iterable[dict[str, str]]:
    if not isinstance(result, dict):
        return []
    if tool in {"web_search", "search_place_images"}:
        output = []
        for item in result.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("href") or item.get("url") or item.get("image_url") or "")
            if url:
                output.append({
                    "url": _db_text(url, 1000),
                    "title": _db_text(item.get("title"), 300),
                    "excerpt": _db_text(item.get("body") or item.get("text") or item.get("snippet"), 3000),
                    "source_status": "seen" if item.get("seen") else "discovered",
                })
        return output
    if tool == "fetch_page":
        url = _db_text(result.get("url") or args.get("url"), 1000)
        return [{
            "url": url,
            "title": _db_text(result.get("title"), 300),
            "excerpt": _db_text(result.get("text"), 5000),
            "source_status": "validated",
        }] if url else []
    return []


def _source_status(tool: str, result: Any, outcome: str) -> tuple[str, str]:
    error = str(result.get("error") or "") if isinstance(result, dict) else ""
    detail = str(result.get("detail") or "") if isinstance(result, dict) else ""
    if error:
        if error in {"page_not_useful_evidence", "unsafe_source_content"}:
            return "rejected", detail or error
        if error in {"fetch_failed", "material_decision_required"} or error.startswith("fetch_failed"):
            return "blocked", detail or error
        return "failed", detail or error
    if tool == "fetch_page":
        return "validated", ""
    return ("discovered", "") if outcome != "no_new_evidence" else ("seen", "")


def _resolve_work_item(db: Session, mission: AgentMission, current: AgentWorkItem, tool: str, args: dict[str, Any]) -> AgentWorkItem:
    try:
        place_id = int(args["place_id"]) if args.get("place_id") is not None else None
    except (TypeError, ValueError):
        place_id = None
    target: Optional[AgentWorkItem] = None
    if place_id is not None:
        target = db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.place_id == place_id,
            AgentWorkItem.status.in_(("ready", "active", "blocked")),
        ).first()
    if target is None and args.get("query"):
        query_tokens = _tokens(args.get("query"))
        candidates = db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.place_id.is_not(None),
            AgentWorkItem.status.in_(("ready", "active", "blocked")),
        ).all()
        target = max(
            candidates,
            key=lambda row: len(query_tokens & _tokens(row.title)),
            default=None,
        )
        if target is not None and not (query_tokens & _tokens(target.title)):
            target = None
    if target is None and args.get("url"):
        url = str(args.get("url") or "")
        evidence = db.query(AgentEvidence).filter(
            AgentEvidence.mission_id == mission.id,
            AgentEvidence.url == url,
        ).order_by(AgentEvidence.id.desc()).first()
        if evidence is not None and evidence.work_item_id is not None:
            target = db.get(AgentWorkItem, evidence.work_item_id)
    if target is None or target.id == current.id:
        return current
    # The orchestrator, not a drifting model call, owns target transitions.
    # A ready/blocked sibling may receive evidence, but cannot steal the active
    # cursor until the current item is completed or explicitly rotated.
    if current.status == "active":
        return current
    target.status = "active"
    target.blocked_reason = ""
    return target


def observe_lesson(
    db: Session,
    *,
    key: str,
    city_id: Optional[int],
    category: str,
    trigger: str,
    action: str,
    expected_effect: str,
    evidence_ref: str,
    applicability: Optional[dict[str, Any]] = None,
    successful: bool = False,
) -> AgentLesson:
    lesson = db.query(AgentLesson).filter(AgentLesson.lesson_key == key[:160]).first()
    refs: list[str]
    if lesson is None:
        lesson = AgentLesson(
            lesson_key=key[:160],
            scope="city" if city_id is not None else "global",
            city_id=city_id,
            category=category[:40],
            trigger=trigger,
            action=action,
            expected_effect=expected_effect,
            applicability=_dump(applicability or {}),
            evidence_refs="[]",
            status="candidate",
            confidence=0.5,
            observation_count=0,
        )
        db.add(lesson)
        refs = []
    else:
        refs = _json_list(lesson.evidence_refs)
        if applicability:
            lesson.applicability = _dump(applicability)
    is_new = not evidence_ref or evidence_ref not in refs
    if is_new:
        lesson.observation_count = int(lesson.observation_count or 0) + 1
        if successful:
            lesson.success_count = int(lesson.success_count or 0) + 1
        else:
            lesson.failure_count = int(lesson.failure_count or 0) + 1
    if evidence_ref and evidence_ref not in refs:
        refs.append(evidence_ref[:500])
    lesson.evidence_refs = _dump(refs[-20:])
    observation_count = int(lesson.observation_count or 0)
    success_count = int(lesson.success_count or 0)
    failure_count = int(lesson.failure_count or 0)
    lesson.confidence = max(
        0.12,
        min(0.98, 0.45 + observation_count * 0.05 + success_count * 0.12 - failure_count * 0.08),
    )
    if success_count >= 2 and observation_count >= 3:
        lesson.status = "validated"
    elif success_count >= 1 and observation_count >= 2:
        lesson.status = "testing"
    elif failure_count >= 3 and success_count == 0:
        lesson.status = "deprecated"
    lesson.updated_at = datetime.now(timezone.utc)
    db.flush()
    return lesson


def record_model_recovery_attempt(
    db: Session,
    *,
    mission: Optional[AgentMission],
    work_item: Optional[AgentWorkItem],
    run_id: int,
    sequence: int,
    failure_kind: str,
    error: str,
    attempt: int,
    strategy: dict[str, Any],
) -> str:
    """Persist an auditable strategy change before retrying a model call."""

    evidence_ref = f"run:{run_id}:model-recovery:{attempt}"
    if mission is None or work_item is None:
        return evidence_ref

    now = datetime.now(timezone.utc)
    mission_strategy = _json_dict(mission.strategy)
    history = [
        dict(item) for item in mission_strategy.get("recovery_history", [])
        if isinstance(item, dict)
    ]
    failure_summary = (
        f"model_output:{failure_kind}: attempt={attempt}; "
        f"mode={strategy.get('mode', 'focused_retry')}; error={error[:240]}"
    )
    entry = {
        "evidence_ref": evidence_ref,
        "failure_kind": failure_kind,
        "attempt": attempt,
        "strategy": strategy,
        "failure_summary": failure_summary,
        "outcome": "retrying",
        "started_at": now.isoformat(),
    }
    history.append(entry)
    mission_strategy["recovery_history"] = history[-8:]
    mission_strategy["last_recovery"] = entry
    mission.strategy = _dump(mission_strategy)

    failures = [
        failure for failure in _json_list(work_item.failed_approaches)
        if not failure.startswith("model_output:")
    ]
    work_item.failed_approaches = _dump(failures[-12:])
    checkpoint_failures = [*failures]
    if failure_summary not in checkpoint_failures:
        checkpoint_failures.append(failure_summary)
    # Provider/schema failures belong to mission recovery history, not the
    # investigation-path budget that rotates a place after three failed source
    # strategies. The recovery checkpoint still preserves the incident.
    work_item.state_summary = (
        f"Run #{run_id} model output failed ({failure_kind}); retry {attempt} changed "
        f"strategy to {strategy.get('mode', 'focused_retry')}."
    )[:3000]
    work_item.last_run_id = run_id
    work_item.updated_at = now

    next_action = _json_dict(work_item.next_action)
    db.add(AgentCheckpoint(
        mission_id=mission.id,
        work_item_id=work_item.id,
        run_id=run_id,
        sequence=sequence,
        state_summary=work_item.state_summary,
        decision=f"Retry model output with changed strategy: {strategy.get('mode', 'focused_retry')}",
        new_facts="[]",
        rejected_claims=_dump([error[:1000]]),
        failed_approaches=_dump(checkpoint_failures[-6:]),
        next_action=_dump(next_action),
        outcome="recovery_retry",
    ))
    progress = _json_dict(mission.progress)
    progress["last_recovery"] = entry
    mission.progress = _dump(progress)
    mission.updated_at = now
    db.flush()
    return evidence_ref


def finish_model_recovery_attempt(
    db: Session,
    *,
    mission: Optional[AgentMission],
    work_item: Optional[AgentWorkItem],
    run_id: int,
    evidence_ref: str,
    failure_kind: str,
    strategy: dict[str, Any],
    successful: bool,
) -> Optional[AgentLesson]:
    """Measure a recovery attempt and promote only outcome-backed lessons."""

    now = datetime.now(timezone.utc)
    if mission is not None:
        mission_strategy = _json_dict(mission.strategy)
        history = [
            dict(item) for item in mission_strategy.get("recovery_history", [])
            if isinstance(item, dict)
        ]
        for item in history:
            if item.get("evidence_ref") == evidence_ref:
                item["outcome"] = "recovered" if successful else "failed"
                item["finished_at"] = now.isoformat()
        mission_strategy["recovery_history"] = history[-8:]
        if history:
            mission_strategy["last_recovery"] = history[-1]
        mission.strategy = _dump(mission_strategy)
        progress = _json_dict(mission.progress)
        progress["last_recovery"] = history[-1] if history else {
            "evidence_ref": evidence_ref,
            "failure_kind": failure_kind,
            "strategy": strategy,
            "outcome": "recovered" if successful else "failed",
            "finished_at": now.isoformat(),
        }
        mission.progress = _dump(progress)
        mission.updated_at = now
    if work_item is not None:
        # Model/provider failures are mission recovery history, never source
        # investigation paths. Also clean legacy entries written by older runs,
        # regardless of whether this particular recovery succeeded.
        failures = [
            failure for failure in _json_list(work_item.failed_approaches)
            if not failure.startswith("model_output:")
        ]
        work_item.failed_approaches = _dump(failures[-12:])
        work_item.state_summary = (
            f"Run #{run_id} model-output recovery "
            f"{'succeeded' if successful else 'failed'} for {failure_kind} using "
            f"{strategy.get('mode', 'focused_retry')}."
        )[:3000]
        work_item.updated_at = now

    lesson = observe_lesson(
        db,
        key=f"model_output_recovery:{failure_kind}:{strategy.get('mode', 'focused_retry')}",
        city_id=None,
        category="model_runtime",
        trigger=f"Provider rejected model output as {failure_kind}",
        action=(
            f"Retry with mode={strategy.get('mode')}, reasoning={strategy.get('reasoning_effort')}, "
            f"force_compaction={bool(strategy.get('force_compaction'))}, "
            f"tool_count={len(strategy.get('tool_names') or [])}"
        ),
        expected_effect="Produce parseable tool output while preserving the same durable task and committed progress",
        evidence_ref=evidence_ref,
        applicability={
            "task_kinds": [mission.kind] if mission is not None else [],
            "failure_kinds": [failure_kind],
            "models": [str(strategy.get("model") or "")],
        },
        successful=successful,
    )
    db.flush()
    return lesson


def _candidate_structured_handoff(
    tool: str,
    args: dict[str, Any],
    result: Any,
    evidence_rows: list[AgentEvidence],
) -> dict[str, Any] | None:
    """Build a resumable action only from independently retainable evidence."""

    if not isinstance(result, dict) or result.get("error"):
        return None
    if tool == "geocode_place":
        for row in result.get("results") or []:
            if (
                not isinstance(row, dict)
                or row.get("storage_allowed") is not True
                or str(row.get("source") or "").casefold().startswith("brave")
                or row.get("lat") is None
                or row.get("lng") is None
            ):
                continue
            identity = str(row.get("title") or row.get("display_name") or row.get("name") or "").strip()
            address = str(row.get("address") or "").strip()
            if not identity:
                continue
            exact_query = " ".join(
                value for value in (f'"{identity[:160]}"', f'"{address[:180]}"' if address else "", "官方 地址 营业时间")
                if value
            )[:500]
            return {
                "handoff_version": "candidate_dossier_v1",
                "tool": "web_search",
                "args": {"query": exact_query},
                "candidate": {
                    "display_name": identity[:240],
                    "address": address[:300],
                    "lat": row.get("lat"),
                    "lng": row.get("lng"),
                    "source": str(row.get("source") or "")[:80],
                    "external_id": str(row.get("external_id") or "")[:240],
                    "source_url": str(row.get("source_url") or "")[:1000],
                },
                "source_axis": {
                    "kind": "storable_geocoder",
                    "provider": str(row.get("source") or "")[:80],
                },
                "next_exact_query": exact_query,
                "purpose": "독립 좌표로 고정한 같은 지점의 공개 본문을 정확 검색",
            }
    if tool == "fetch_page" and evidence_rows:
        source = next(
            (row for row in reversed(evidence_rows) if row.source_status == "validated"),
            evidence_rows[-1],
        )
        if not str(source.url or "").startswith(("http://", "https://")):
            return None
        identity = str(source.title or result.get("title") or "").strip()
        if not identity:
            return None
        exact_query = identity[:300]
        return {
            "handoff_version": "candidate_source_v1",
            "tool": "geocode_place",
            "args": {"query": exact_query},
            "candidate": {"display_name": identity[:240]},
            "source_axis": {
                "kind": "validated_public_page",
                "host": _host(source.url),
                "url": source.url[:1000],
            },
            "next_exact_query": exact_query,
            "purpose": "검증한 공개 본문의 정확한 장소명을 저장 가능한 좌표 공급자로 확인",
        }
    if tool == "web_search" and evidence_rows:
        source = next(
            (row for row in evidence_rows if row.source_status == "discovered"),
            None,
        )
        if source is None or not str(source.url or "").startswith(("http://", "https://")):
            return None
        return {
            "handoff_version": "candidate_source_v1",
            "tool": "fetch_page",
            "args": {"url": source.url[:1000]},
            "candidate": {"display_name": str(source.title or "")[:240]},
            "source_axis": {
                "kind": "search_result",
                "host": _host(source.url),
                "url": source.url[:1000],
            },
            "next_exact_query": str(args.get("query") or "")[:500],
            "purpose": "검색 요약이 아닌 새 출처 본문 검증",
        }
    return None


def _candidate_fetch_failure_handoff(
    db: Session,
    *,
    mission: AgentMission,
    item: AgentWorkItem,
    prior_next_action: dict[str, Any],
    args: dict[str, Any],
    error: str,
) -> dict[str, Any] | None:
    """Advance a retained candidate dossier past one failed source URL.

    Keeping the candidate is useful; keeping the exact failed ``fetch_page``
    action is not.  Prefer another discovered-but-unread URL from this work
    item.  When none remains, retain the identity and switch to an exact search
    axis that excludes the failed hosts.
    """

    if (
        not str(prior_next_action.get("handoff_version") or "").startswith("candidate_")
        or prior_next_action.get("tool") != "fetch_page"
    ):
        return None
    prior_args = (
        prior_next_action.get("args")
        if isinstance(prior_next_action.get("args"), dict)
        else {}
    )
    failed_url = str(args.get("url") or prior_args.get("url") or "").strip()
    if not failed_url.startswith(("http://", "https://")):
        return None

    evidence_rows = (
        db.query(AgentEvidence)
        .filter(
            AgentEvidence.mission_id == mission.id,
            AgentEvidence.work_item_id == item.id,
        )
        .order_by(AgentEvidence.id.asc())
        .all()
    )
    unavailable_urls = {
        str(row.url or "").strip()
        for row in evidence_rows
        if row.source_status in {"validated", "rejected", "blocked", "failed"}
        and str(row.url or "").strip()
    }
    unavailable_urls.add(failed_url)

    remaining_source: AgentEvidence | None = None
    seen_urls: set[str] = set()
    for row in evidence_rows:
        source_url = str(row.url or "").strip()
        if (
            row.source_status != "discovered"
            or not source_url.startswith(("http://", "https://"))
            or source_url in unavailable_urls
            or source_url in seen_urls
        ):
            continue
        seen_urls.add(source_url)
        remaining_source = row
        break

    failed_sources = [
        dict(value)
        for value in (prior_next_action.get("failed_sources") or [])
        if isinstance(value, dict) and str(value.get("url") or "").strip()
    ]
    failed_record = {
        "url": failed_url[:1000],
        "host": _host(failed_url),
        "error": str(error or "fetch_failed")[:160],
    }
    if not any(str(value.get("url") or "") == failed_url for value in failed_sources):
        failed_sources.append(failed_record)
    failed_sources = failed_sources[-8:]

    next_action = dict(prior_next_action)
    next_action.update({
        "failed_source": failed_record,
        "failed_sources": failed_sources,
        "last_error": str(error or "fetch_failed")[:160],
    })
    if remaining_source is not None:
        next_url = str(remaining_source.url)
        next_action.update({
            "tool": "fetch_page",
            "args": {"url": next_url[:1000]},
            "source_axis": {
                "kind": "search_result",
                "host": _host(next_url),
                "url": next_url[:1000],
            },
            "purpose": "실패한 출처를 제외하고 같은 검색에서 발견한 미열람 본문 검증",
        })
        return next_action

    candidate = (
        prior_next_action.get("candidate")
        if isinstance(prior_next_action.get("candidate"), dict)
        else {}
    )
    identity = str(candidate.get("display_name") or "").strip()
    address = str(candidate.get("address") or "").strip()
    if not address and "," in identity:
        _identity, _separator, display_address = identity.partition(",")
        address = display_address.strip()
    base_query = str(prior_next_action.get("next_exact_query") or "").strip()
    query_parts = [
        f'"{identity[:160]}"' if identity else base_query[:300],
        f'"{address[:180]}"' if address and address.casefold() not in identity.casefold() else "",
        "官方 地址 营业时间",
    ]
    failed_hosts = sorted({
        str(value.get("host") or _host(str(value.get("url") or ""))).strip()
        for value in failed_sources
        if str(value.get("host") or _host(str(value.get("url") or ""))).strip()
    })
    query_parts.extend(f"-site:{host}" for host in failed_hosts[:3])
    alternative_query = " ".join(value for value in query_parts if value).strip()[:500]
    if not alternative_query:
        return None
    next_action.update({
        "tool": "web_search",
        "args": {"query": alternative_query},
        "source_axis": {
            "kind": "alternative_exact_search",
            "excluded_hosts": failed_hosts[:3],
        },
        "next_exact_query": alternative_query,
        "purpose": "보존된 정확 후보를 실패한 호스트가 아닌 독립 출처 축에서 재검증",
    })
    return next_action


def checkpoint_after_tool(
    db: Session,
    *,
    mission: Optional[AgentMission],
    work_item: Optional[AgentWorkItem],
    run_id: int,
    sequence: int,
    tool: str,
    args: dict[str, Any],
    result: Any,
    outcome: str,
    new_evidence_count: int,
    material_change: bool,
) -> tuple[Optional[AgentWorkItem], dict[str, Any]]:
    """Persist a compact checkpoint and return continuity metadata for the model."""

    if mission is None or work_item is None:
        return work_item, {}
    item = _resolve_work_item(db, mission, work_item, tool, args)
    now = datetime.now(timezone.utc)
    error = str(result.get("error") or "") if isinstance(result, dict) else ""
    detail = str(result.get("detail") or "") if isinstance(result, dict) else ""
    is_policy_guard = bool(
        isinstance(result, dict)
        and mission.kind == "data_integrity"
        and (
            result.get("error_class") == "policy_guard"
            or error in CORRECTIVE_POLICY_GUARD_ERRORS
        )
    )
    guard_disposition = (
        str(result.get("guard_disposition") or "")
        if isinstance(result, dict)
        else ""
    )
    if guard_disposition not in {"decide", "retry"}:
        guard_disposition = (
            "decide" if error in POLICY_GUARD_DECIDE_ERRORS else "retry"
        )
    facts: list[str] = []
    rejected: list[str] = []
    failures = _json_list(item.failed_approaches)
    prior_next_action = _json_dict(item.next_action)
    next_action: dict[str, Any]

    source_status, rejection_reason = _source_status(tool, result, outcome)
    evidence_rows = []
    for source in _result_urls(tool, args, result):
        item_source_status = (
            source_status
            if source_status in {"rejected", "blocked", "failed", "validated"}
            else str(source.get("source_status") or source_status)
        )
        fingerprint = hashlib.sha256(
            f"{mission.city_id}|{item.id}|{source['url']}|{item_source_status}|{source['excerpt'][:500]}".encode("utf-8")
        ).hexdigest()
        row = db.query(AgentEvidence).filter(AgentEvidence.fingerprint == fingerprint).first()
        if row is None:
            row = AgentEvidence(
                city_id=mission.city_id,
                mission_id=mission.id,
                work_item_id=item.id,
                place_id=item.place_id,
                run_id=run_id,
                source_type=tool,
                url=source["url"][:1000],
                title=source["title"][:300],
                claim=source["excerpt"][:2000],
                excerpt=source["excerpt"][:5000],
                source_status=item_source_status,
                rejection_reason=rejection_reason[:2000],
                confidence=0.85 if item_source_status == "validated" else 0.45 if item_source_status == "discovered" else 0.0,
                fingerprint=fingerprint,
            )
            db.add(row)
            db.flush()
        evidence_rows.append(row)

    candidate_handoff = (
        _candidate_structured_handoff(tool, args, result, evidence_rows)
        if mission.kind == "candidate_discovery"
        else None
    )

    if material_change:
        # Checkpoints and promoted lessons retain the historical failures, but
        # deliberate rotation is based on consecutive failures since the most
        # recent material success. Carrying older failures forward makes a
        # resumed verification attempt pause after only one new source error.
        failures = []
        facts.append(f"{tool}로 운영 DB 변화가 생성됨")
        item.stage = "verify"
        next_action = {"tool": "get_place" if item.place_id else "list_agent_tasks", "args": {"place_id": item.place_id} if item.place_id else {}, "purpose": "성공조건 재측정"}
    elif tool == "web_search" and evidence_rows:
        unseen = next((row for row in evidence_rows if row.source_status == "discovered"), None)
        if unseen is not None:
            facts.append(f"새 출처 후보 {len([row for row in evidence_rows if row.source_status == 'discovered'])}건 발견")
            item.stage = "read"
            next_action = candidate_handoff or {
                "tool": "fetch_page",
                "args": {"url": unseen.url},
                "purpose": "검색 요약이 아닌 본문 검증",
            }
        else:
            item.stage = "research"
            next_action = {"tool": "choose_alternative_source", "purpose": "모든 결과가 기존 열람 항목이므로 다른 출처 축 선택"}
    elif tool == "fetch_page" and source_status == "validated" and evidence_rows:
        facts.append("출처 본문을 읽어 저장 판단이 가능함")
        item.stage = "decide"
        next_action = candidate_handoff or {
            "tool": "decide",
            "source_url": evidence_rows[0].url,
            "purpose": "주장-대상 일치 확인 후 안전한 저장 또는 기각",
        }
    elif candidate_handoff is not None:
        facts.append("독립 보존 가능한 후보 식별자와 다음 정확 질의를 인계함")
        item.stage = "research"
        next_action = candidate_handoff
    elif is_policy_guard and guard_disposition == "decide":
        # Policy guards describe an invalid action choice, not a failed source
        # or investigation path. Keep the actual failure budget untouched and
        # direct the model to close its own integrity task honestly.
        item.stage = "decide"
        # These guards are locally resolved orchestration corrections rather
        # than external blockers. Close the audit as an honest unresolved
        # verdict so an active mission is not resumed in the next batch.
        terminal_status = "completed"
        next_action = {
            "phase": "data_integrity_terminal_verdict_v1",
            "tool": "upsert_agent_task",
            "task_id": mission.task_id,
            "status": terminal_status,
            "guard_error": error,
            "guard_disposition": "decide",
            "required_fields": [
                "task_id", "status", "verdict", "reason",
                "marker_changes", "evidence_refs",
            ],
            "purpose": (
                "현재 provider가 광고한 upsert_agent_task 스키마만 따라 "
                "근거 기반 terminal verdict를 기록"
            ),
        }
    elif is_policy_guard:
        # Retry guards correct an action choice without consuming the failed
        # investigation-path budget or prematurely closing an unobserved task.
        item.stage = "research"
        next_action = {
            "tool": "continue",
            "args": {"task_id": mission.task_id},
            "purpose": (
                f"Correct policy guard {error}, reuse existing observations, and "
                "continue with an allowed non-repeated action. Do not close the "
                "task until an auditable observation supports the verdict."
            ),
        }
    elif error == "active_work_item_mismatch":
        # Model drift is not evidence that the active place is blocked.  Keep
        # the cursor and definition of done intact without poisoning the
        # target's failed-approach count (which drives deliberate rotation).
        rejected.append(detail or error)
        next_action = {
            "tool": "get_place" if item.place_id else "continue",
            "args": {"place_id": item.place_id} if item.place_id else {},
            "purpose": "현재 활성 대상에 다시 집중",
        }
    elif error:
        failure = f"{tool}: {error}" + (f" - {detail[:400]}" if detail else "")
        if failure not in failures:
            failures.append(failure)
        rejected.append(failure)
        item.stage = "research"
        advanced_candidate_handoff = (
            _candidate_fetch_failure_handoff(
                db,
                mission=mission,
                item=item,
                prior_next_action=prior_next_action,
                args=args,
                error=error,
            )
            if mission.kind == "candidate_discovery" and tool == "fetch_page"
            else None
        )
        if advanced_candidate_handoff is not None:
            next_action = advanced_candidate_handoff
        elif (
            mission.kind == "candidate_discovery"
            and str(prior_next_action.get("handoff_version") or "").startswith("candidate_")
        ):
            # A later source/guard failure must not erase a retainable exact
            # candidate dossier and turn the next run back into a broad search.
            next_action = dict(prior_next_action)
            next_action["last_error"] = error[:160]
            next_action["purpose"] = (
                "보존된 정확 후보를 유지하고 실패한 출처가 아닌 독립 출처 축으로 계속 검증"
            )
        else:
            next_action = {"tool": "choose_alternative_source", "purpose": "같은 호출을 반복하지 않고 다른 출처 축 선택"}
    else:
        item.stage = "research" if tool in {"web_search", "fetch_page"} else item.stage
        next_action = _json_dict(item.next_action) or {"tool": "continue", "purpose": "현재 성공조건에 가장 가까운 행동"}

    item.state_summary = (
        f"실행 #{run_id} 행동 {sequence}: {tool} → {outcome}. "
        f"새 근거 {new_evidence_count}건, 실제 변경 {'있음' if material_change else '없음'}."
    )[:3000]
    item.failed_approaches = _dump(failures[-12:])
    item.next_action = _dump(next_action)
    item.evidence_summary = "; ".join(
        f"{row.source_status}:{row.title or row.url}" for row in evidence_rows[-5:]
    )[:4000] or item.evidence_summary
    item.last_run_id = run_id
    item.updated_at = now
    if item.status in {"ready", "blocked"}:
        item.status = "active"

    checkpoint = AgentCheckpoint(
        mission_id=mission.id,
        work_item_id=item.id,
        run_id=run_id,
        sequence=sequence,
        state_summary=item.state_summary,
        decision=(
            "DB 변경 후 성공조건 확인"
            if material_change
            else "정책 가드 후 현재 과제 결과 기록"
            if is_policy_guard
            else "근거를 더 확인"
            if next_action.get("tool") == "fetch_page"
            else "대체 행동 선택"
            if error
            else "현재 전략 유지"
        ),
        new_facts=_dump(facts),
        rejected_claims=_dump(rejected),
        failed_approaches=_dump(failures[-6:]),
        next_action=_dump(next_action),
        outcome=outcome,
    )
    db.add(checkpoint)
    mission.last_run_id = run_id
    mission.progress = _dump({
        "active_work_item_id": item.id,
        "last_checkpoint_sequence": sequence,
        "last_outcome": outcome,
        "next_action": next_action,
    })
    mission.updated_at = now

    host = _host(str(args.get("url") or (evidence_rows[0].url if evidence_rows else "")))
    if error == "page_not_useful_evidence" and host:
        observe_lesson(
            db,
            key=f"reject_login_shell:{host}",
            city_id=mission.city_id,
            category="source",
            trigger=f"{host} 페이지가 로그인·인증 셸만 반환",
            action="검증 근거로 등록하지 말고 실패 출처로 기록한 뒤 다른 출처 축으로 전환",
            expected_effect="본문 미확인 정보의 저장 방지",
            evidence_ref=f"run:{run_id}:step:{sequence}",
            applicability={"domains": [host], "task_kinds": [mission.kind]},
            successful=True,
        )
    if error == "recent_duplicate_search":
        observe_lesson(
            db,
            key="avoid_recent_duplicate_search",
            city_id=mission.city_id,
            category="workflow",
            trigger="최근 검색어와 의미상 같은 검색을 다시 시도",
            action="저장된 검색 결과와 체크포인트를 재사용하고 대상 또는 출처 축을 변경",
            expected_effect="무성과 반복 감소",
            evidence_ref=f"run:{run_id}:step:{sequence}",
            applicability={"task_kinds": [mission.kind]},
            successful=True,
        )
    db.flush()
    continuity = {
        "mission_id": mission.id,
        "work_item_id": item.id,
        "target": item.target_key,
        "stage": item.stage,
        "next_action": next_action,
        "failed_approaches": failures[-4:],
    }
    return item, continuity


def reconcile_work_items(db: Session, *, mission: Optional[AgentMission]) -> Optional[AgentWorkItem]:
    """Measure exact place gaps and move completed targets out of the active queue."""

    if mission is None:
        return None
    task = db.get(AgentTask, mission.task_id) if mission.task_id else None
    items = db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission.id).all()
    now = datetime.now(timezone.utc)
    for item in items:
        if item.place_id is None or item.status in {"done", "superseded"}:
            continue
        place = db.get(Marker, item.place_id)
        if place is None:
            item.status = "superseded"
            continue
        done = False
        if mission.kind == "quality_images":
            done = len(place.images or []) > 0
        elif mission.kind == "quality_verification":
            done = place.last_verified_at is not None
        elif mission.kind == "quality_zones":
            done = place.zone_id is not None
        elif mission.kind == "quality_information":
            done = len((place.description or "").strip()) >= 60 and len(place.insights or []) >= 2
        elif mission.kind == "quality_drafts":
            done = (
                len(place.images or []) > 0
                and place.zone_id is not None
                and place.last_verified_at is not None
                and len((place.description or "").strip()) >= 60
                and len(place.insights or []) >= 2
            )
        if done:
            item.status = "done"
            item.stage = "complete"
            item.state_summary = "운영 DB 성공조건 재측정 완료"
            item.completed_at = now

    active = next((item for item in items if item.status == "active"), None)
    if active is None:
        active = sorted(
            (item for item in items if item.status == "ready"),
            key=lambda item: (-item.priority, item.id),
        )[0] if any(item.status == "ready" for item in items) else None
        if active is not None:
            active.status = "active"
    executable = [item for item in items if item.status in {"ready", "active"}]
    blocked = [item for item in items if item.status == "blocked"]
    remaining = [*executable, *blocked]
    if not executable and blocked:
        mission.status = "paused"
        mission.progress = _dump({
            "active_work_item_id": None,
            "done": len([item for item in items if item.status == "done"]),
            "ready": 0,
            "blocked": len(blocked),
            "total": len([item for item in items if item.status != "superseded"]),
            "retry_condition": "새 출처 또는 냉각 시간 경과 후 재평가",
        })
    elif not remaining:
        mission.status = "completed"
        mission.completed_at = now
        if task is not None:
            task.status = "completed"
            task.completed_at = now
            if mission.kind != "candidate_discovery" or not (task.result or "").strip():
                task.result = "모든 세부 대상의 운영 DB 성공조건을 재측정해 완료했습니다."
    if mission.status not in {"paused", "completed"}:
        mission.progress = _dump({
            "active_work_item_id": active.id if active else None,
            "done": len([item for item in items if item.status == "done"]),
            "ready": len([item for item in items if item.status == "ready"]),
            "blocked": len(blocked),
            "total": len([item for item in items if item.status != "superseded"]),
            "next_action": _json_dict(active.next_action) if active else {},
        })
    db.commit()
    return active


def rotate_blocked_work_item(
    db: Session,
    *,
    mission: Optional[AgentMission],
    current: Optional[AgentWorkItem],
    run_id: int,
    reason: str,
    activate_next: bool = True,
    commit: bool = True,
    quality_disposition: Optional[str] = None,
    quality_gap_kinds: Optional[Iterable[str]] = None,
    quality_evidence_refs: Iterable[str] = (),
    quality_source_revision: str = "",
    quality_cooldown_hours: float = 24,
) -> Optional[AgentWorkItem]:
    """Block the current cursor and optionally activate its ready successor.

    ``commit=False`` lets a caller include the rotation in a larger atomic
    checkpoint transition. ``activate_next=False`` records the successor as a
    resume cursor without charging an attempt before it is actually executed.
    """

    if mission is None or current is None:
        return current
    disposition_rows: list[AgentQualityGapDisposition] = []
    if quality_disposition is not None:
        marker = db.get(Marker, current.place_id) if current.place_id is not None else None
        if marker is None:
            raise ValueError("quality disposition requires an exact place work item")
        requested_gaps = list(dict.fromkeys(str(item) for item in (quality_gap_kinds or ())))
        if not requested_gaps:
            raise ValueError("quality disposition requires exact quality_gap_kinds")
        allowed_gaps = QUALITY_GAPS_BY_TASK_KIND.get(mission.kind, frozenset())
        invalid_gaps = [gap for gap in requested_gaps if gap not in allowed_gaps]
        if invalid_gaps:
            raise ValueError(
                f"{mission.kind} cannot dispose quality gaps: {', '.join(invalid_gaps)}"
            )
        for gap_kind in requested_gaps:
            disposition_rows.append(record_quality_gap_disposition(
                db,
                marker=marker,
                gap_kind=gap_kind,
                disposition=quality_disposition,
                reason=reason,
                evidence_refs=quality_evidence_refs,
                source_revision=quality_source_revision,
                cooldown_hours=quality_cooldown_hours,
            ))
    current.status = "blocked"
    current.blocked_reason = reason[:3000]
    current.retry_condition = (
        "기록된 장소·구역·공급자 조건 지문이 바뀔 때만 재개"
        if quality_disposition in TERMINAL_QUALITY_GAP_DISPOSITIONS
        else "기록된 냉각 시간이 지나거나 장소·구역·공급자 조건 지문이 바뀔 때 재개"
        if quality_disposition == "blocked"
        else "새 출처·인증 가능한 접근·사용자 근거 중 하나가 생길 때 재개"
    )
    current.updated_at = datetime.now(timezone.utc)
    next_item = db.query(AgentWorkItem).filter(
        AgentWorkItem.mission_id == mission.id,
        AgentWorkItem.status == "ready",
    ).order_by(AgentWorkItem.priority.desc(), AgentWorkItem.id.asc()).first()
    progress = _json_dict(mission.progress)
    if next_item is not None and activate_next:
        next_item.status = "active"
        next_item.attempts += 1
        next_item.last_run_id = run_id
        progress.update({
            "active_work_item_id": next_item.id,
            "rotation_reason": reason[:1000],
            "next_action": _json_dict(next_item.next_action),
        })
        mission.progress = _dump(progress)
    else:
        mission.status = "paused"
        progress.update({
            "active_work_item_id": None,
            "resume_work_item_id": next_item.id if next_item is not None else None,
            "rotation_reason": reason[:1000],
            "blocked_work_items": db.query(AgentWorkItem).filter(
                AgentWorkItem.mission_id == mission.id,
                AgentWorkItem.status == "blocked",
            ).count(),
            "retry_condition": "새 출처 또는 12시간 냉각 후 재평가",
        })
        mission.progress = _dump(progress)
    if disposition_rows:
        progress = _json_dict(mission.progress)
        progress["quality_dispositions"] = [
            {
                "id": row.id,
                "place_id": row.place_id,
                "gap_kind": row.gap_kind,
                "status": row.status,
                "retry_after": row.retry_after,
            }
            for row in disposition_rows
        ]
        mission.progress = _dump(progress)
    run = db.get(AgentRun, run_id)
    if run is not None:
        # Keep the run summary cursor aligned with the durable mission cursor;
        # otherwise the admin history shows the blocked item even though the
        # next run will correctly resume its ready successor.
        run.work_item_id = (
            next_item.id
            if next_item is not None and activate_next
            else current.id
        )
    if commit:
        db.commit()
    return next_item


def active_work_item_for_mission(db: Session, mission: Optional[AgentMission]) -> Optional[AgentWorkItem]:
    if mission is None:
        return None
    return db.query(AgentWorkItem).filter(
        AgentWorkItem.mission_id == mission.id,
        AgentWorkItem.status == "active",
    ).order_by(AgentWorkItem.updated_at.desc(), AgentWorkItem.id.asc()).first()


def finalize_mission(
    db: Session,
    *,
    mission: Optional[AgentMission],
    task: Optional[AgentTask],
    run_id: int,
    commit: bool = True,
) -> None:
    if mission is None:
        return
    mission.last_run_id = run_id
    if task is not None and task.status in {"completed", "blocked"}:
        now = datetime.now(timezone.utc)
        terminal_items = db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.status.in_(("ready", "active", "blocked")),
        ).all()
        if task.status == "completed":
            mission.status = "completed"
            mission.completed_at = now
            for item in terminal_items:
                item.status = "done"
                item.completed_at = now
        else:
            # A blocked discovery slice is a durable pause, not an abandoned
            # active cursor. The periodic discovery scheduler reopens the same
            # task and ``ensure_mission_for_task`` resumes this exact item with
            # its checkpoint/evidence history intact.
            mission.status = "paused"
            mission.completed_at = None
            mission.updated_at = now
            candidate_retry_after = (
                now + timedelta(hours=12)
                if mission.kind == "candidate_discovery"
                else None
            )
            for item in terminal_items:
                item.status = "blocked"
                item.blocked_reason = (task.result or "과제가 차단 상태로 종료됨")[:3000]
                item.retry_condition = (
                    f"{candidate_retry_after.isoformat()} 이후 같은 체크포인트를 재평가"
                    if candidate_retry_after is not None
                    else "새 출처 조건이 생긴 뒤 재평가"
                )
                item.updated_at = now
            progress = _json_dict(mission.progress)
            progress.update({
                "active_work_item_id": None,
                "resume_work_item_id": terminal_items[0].id if terminal_items else None,
                "terminal_task_status": "blocked",
                "retry_condition": (
                    "12시간 냉각 뒤 같은 체크포인트를 재개"
                    if candidate_retry_after is not None
                    else "새 출처 조건이 생긴 뒤 같은 체크포인트를 재개"
                ),
                "retry_after": (
                    candidate_retry_after.isoformat()
                    if candidate_retry_after is not None
                    else None
                ),
            })
            mission.progress = _dump(progress)
    if commit:
        db.commit()


def learn_from_recent_runs(db: Session, *, city_id: int, limit: int = 12) -> int:
    """Convert repeated, auditable run failures into deduplicated lesson candidates."""

    runs = db.query(AgentRun).filter(AgentRun.city_id == city_id).order_by(AgentRun.id.desc()).limit(limit).all()
    learned_ids: set[int] = set()
    for run in runs:
        summary = str(run.summary or "").casefold()
        model_failure_kind = (
            "output_parse_failed" if "output_parse_failed" in summary or "parsing failed" in summary
            else "tool_schema_failed" if "tool_use_failed" in summary or "tool call validation failed" in summary
            else ""
        )
        if model_failure_kind:
            learned_ids.add(observe_lesson(
                db,
                key=f"model_output_failure:{model_failure_kind}",
                city_id=None,
                category="model_runtime",
                trigger=f"Autonomous run terminated on {model_failure_kind}",
                action=(
                    "Preserve the checkpoint, narrow tools to the active task, lower reasoning effort, "
                    "compact context, and retry before failing the run"
                ),
                expected_effect="One malformed model output does not discard committed batch progress",
                evidence_ref=f"run:{run.id}:summary",
                applicability={"failure_kinds": [model_failure_kind]},
                successful=False,
            ).id)
        discovered_urls: set[str] = set()
        for step in run.steps:
            detail = _json_dict(step.detail)
            args = detail.get("args") if isinstance(detail.get("args"), dict) else {}
            result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
            error = str(result.get("error") or "")
            if step.tool == "web_search":
                for item in result.get("results") or []:
                    if isinstance(item, dict) and not item.get("seen") and item.get("href"):
                        discovered_urls.add(str(item["href"]))
            ref = f"run:{run.id}:step:{step.sequence}"
            if error == "recent_duplicate_search":
                learned_ids.add(observe_lesson(
                    db,
                    key="avoid_recent_duplicate_search",
                    city_id=city_id,
                    category="workflow",
                    trigger="최근 검색어와 의미상 같은 검색을 다시 시도",
                    action="검색 이력·체크포인트의 결과를 재사용하고 대상 또는 출처 축을 변경",
                    expected_effect="무성과 검색 반복 감소",
                    evidence_ref=ref,
                    applicability={},
                    successful=True,
                ).id)
            if error == "page_not_useful_evidence":
                host = _host(str(args.get("url") or ""))
                if host:
                    learned_ids.add(observe_lesson(
                        db,
                        key=f"reject_login_shell:{host}",
                        city_id=city_id,
                        category="source",
                        trigger=f"{host}가 로그인·인증 또는 정보 없는 셸을 반환",
                        action="근거로 사용하지 않고 실패 출처로 기록한 뒤 다른 출처 축으로 이동",
                        expected_effect="본문 미확인 정보 저장 방지",
                        evidence_ref=ref,
                        applicability={"domains": [host]},
                        successful=True,
                    ).id)
            if error == "material_decision_required" and step.tool == "fetch_page" and str(args.get("url") or "") in discovered_urls:
                learned_ids.add(observe_lesson(
                    db,
                    key="allow_new_evidence_followup",
                    city_id=city_id,
                    category="workflow",
                    trigger="이번 실행에서 방금 발견한 신규 URL의 본문을 확인하려는 경우",
                    action="일반 조사 예산과 분리해 fetch_page 후속 검증을 허용",
                    expected_effect="유일한 신규 근거를 확인하지 못하고 종료하는 문제 방지",
                    evidence_ref=ref,
                    applicability={"stages": ["read"], "task_kinds": ["quality_verification", "quality_information"]},
                    successful=False,
                ).id)
    db.commit()
    return len(learned_ids)


def evaluate_knowledge_uses(db: Session, *, run_id: int, material_change_count: int) -> None:
    """Record whether a retrieved context accompanied a productive run.

    This is deliberately not treated as causal proof for a lesson.  A lesson is
    promoted only by its own repeated trigger/outcome observations.
    """

    helpful = material_change_count > 0
    run = db.get(AgentRun, run_id)
    run_metrics = _json_dict(run.metrics) if run is not None else {}
    recovery_history = [
        item for item in run_metrics.get("model_recovery_history", [])
        if isinstance(item, dict)
    ]
    uses = db.query(AgentKnowledgeUse).filter(AgentKnowledgeUse.run_id == run_id).all()
    for use in uses:
        if use.lesson_id is not None:
            lesson = db.get(AgentLesson, use.lesson_id)
            applicability = _json_dict(lesson.applicability) if lesson is not None else {}
            failure_kinds = {
                str(item) for item in applicability.get("failure_kinds", []) if str(item)
            }
            if failure_kinds:
                triggered = [
                    item for item in recovery_history
                    if str(item.get("failure_kind") or "") in failure_kinds
                ]
                if not triggered:
                    # Absence of the lesson's trigger is not evidence against it.
                    # This avoids degrading a parser-recovery lesson merely
                    # because the next run produced valid JSON throughout.
                    use.outcome = "not_triggered"
                elif any(item.get("outcome") == "recovered" for item in triggered):
                    use.outcome = "recovery_succeeded"
                else:
                    use.outcome = "recovery_failed"
                continue
        use.outcome = "productive_run" if helpful else "no_material_change"
    db.commit()
