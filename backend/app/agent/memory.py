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
from datetime import datetime, timezone
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
    AgentRun,
    AgentTask,
    AgentWorkItem,
    Marker,
)


_TARGET_RE = re.compile(r"(?m)^\s*-\s*#(?P<id>\d+)\s+(?P<title>.+?)(?:\s+\(현재:.*)?$")
_TOKEN_RE = re.compile(r"[0-9A-Za-z_\-]{2,}|[가-힣]{2,}|[\u3400-\u9fff]{2,}")
_STOPWORDS = {
    "현재", "장소", "자동", "품질", "보강", "실행", "대상", "정보", "작업", "agent",
    "place", "quality", "research", "검증", "사진", "운영", "존재", "있는", "없는",
}


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
    if active.status in {"ready", "blocked"}:
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
                    "url": url,
                    "title": str(item.get("title") or ""),
                    "excerpt": str(item.get("body") or item.get("text") or item.get("snippet") or "")[:3000],
                    "source_status": "seen" if item.get("seen") else "discovered",
                })
        return output
    if tool == "fetch_page":
        url = str(result.get("url") or args.get("url") or "")
        return [{"url": url, "title": str(result.get("title") or ""), "excerpt": str(result.get("text") or "")[:5000], "source_status": "validated"}] if url else []
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
    if current.status == "active":
        current.status = "ready"
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
    if evidence_ref and evidence_ref not in refs:
        refs.append(evidence_ref[:500])
    lesson.evidence_refs = _dump(refs[-20:])
    observation_count = int(lesson.observation_count or 0)
    success_count = int(lesson.success_count or 0)
    lesson.confidence = min(0.98, 0.45 + observation_count * 0.08 + success_count * 0.07)
    if success_count >= 2 and observation_count >= 3:
        lesson.status = "validated"
    elif observation_count >= 2:
        lesson.status = "testing"
    lesson.updated_at = datetime.now(timezone.utc)
    db.flush()
    return lesson


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
    facts: list[str] = []
    rejected: list[str] = []
    failures = _json_list(item.failed_approaches)
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

    if material_change:
        facts.append(f"{tool}로 운영 DB 변화가 생성됨")
        item.stage = "verify"
        next_action = {"tool": "get_place" if item.place_id else "list_agent_tasks", "args": {"place_id": item.place_id} if item.place_id else {}, "purpose": "성공조건 재측정"}
    elif tool == "web_search" and evidence_rows:
        unseen = next((row for row in evidence_rows if row.source_status == "discovered"), None)
        if unseen is not None:
            facts.append(f"새 출처 후보 {len([row for row in evidence_rows if row.source_status == 'discovered'])}건 발견")
            item.stage = "read"
            next_action = {"tool": "fetch_page", "args": {"url": unseen.url}, "purpose": "검색 요약이 아닌 본문 검증"}
        else:
            item.stage = "research"
            next_action = {"tool": "choose_alternative_source", "purpose": "모든 결과가 기존 열람 항목이므로 다른 출처 축 선택"}
    elif tool == "fetch_page" and source_status == "validated" and evidence_rows:
        facts.append("출처 본문을 읽어 저장 판단이 가능함")
        item.stage = "decide"
        next_action = {"tool": "decide", "source_url": evidence_rows[0].url, "purpose": "주장-대상 일치 확인 후 안전한 저장 또는 기각"}
    elif error:
        failure = f"{tool}: {error}" + (f" - {detail[:400]}" if detail else "")
        if failure not in failures:
            failures.append(failure)
        rejected.append(failure)
        item.stage = "research"
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
        decision=("DB 변경 후 성공조건 확인" if material_change else "근거를 더 확인" if next_action.get("tool") == "fetch_page" else "대체 행동 선택" if error else "현재 전략 유지"),
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
    remaining = [item for item in items if item.status in {"ready", "active", "blocked"}]
    if not remaining:
        mission.status = "completed"
        mission.completed_at = now
        if task is not None:
            task.status = "completed"
            task.completed_at = now
            task.result = "모든 세부 대상의 운영 DB 성공조건을 재측정해 완료했습니다."
    mission.progress = _dump({
        "active_work_item_id": active.id if active else None,
        "done": len([item for item in items if item.status == "done"]),
        "ready": len([item for item in items if item.status == "ready"]),
        "blocked": len([item for item in items if item.status == "blocked"]),
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
) -> Optional[AgentWorkItem]:
    if mission is None or current is None:
        return current
    current.status = "blocked"
    current.blocked_reason = reason[:3000]
    current.retry_condition = "새 출처·인증 가능한 접근·사용자 근거 중 하나가 생길 때 재개"
    current.updated_at = datetime.now(timezone.utc)
    next_item = db.query(AgentWorkItem).filter(
        AgentWorkItem.mission_id == mission.id,
        AgentWorkItem.status == "ready",
    ).order_by(AgentWorkItem.priority.desc(), AgentWorkItem.id.asc()).first()
    if next_item is not None:
        next_item.status = "active"
        next_item.attempts += 1
        next_item.last_run_id = run_id
        mission.progress = _dump({
            "active_work_item_id": next_item.id,
            "rotation_reason": reason[:1000],
            "next_action": _json_dict(next_item.next_action),
        })
    else:
        mission.status = "paused"
        mission.progress = _dump({
            "active_work_item_id": None,
            "rotation_reason": reason[:1000],
            "blocked_work_items": db.query(AgentWorkItem).filter(
                AgentWorkItem.mission_id == mission.id,
                AgentWorkItem.status == "blocked",
            ).count(),
            "retry_condition": "새 출처 또는 12시간 냉각 후 재평가",
        })
    db.commit()
    return next_item


def finalize_mission(
    db: Session,
    *,
    mission: Optional[AgentMission],
    task: Optional[AgentTask],
    run_id: int,
) -> None:
    if mission is None:
        return
    mission.last_run_id = run_id
    if task is not None and task.status == "completed":
        mission.status = "completed"
        mission.completed_at = datetime.now(timezone.utc)
        for item in db.query(AgentWorkItem).filter(
            AgentWorkItem.mission_id == mission.id,
            AgentWorkItem.status.in_(("ready", "active", "blocked")),
        ).all():
            item.status = "done"
            item.completed_at = datetime.now(timezone.utc)
    db.commit()


def learn_from_recent_runs(db: Session, *, city_id: int, limit: int = 12) -> int:
    """Convert repeated, auditable run failures into deduplicated lesson candidates."""

    runs = db.query(AgentRun).filter(AgentRun.city_id == city_id).order_by(AgentRun.id.desc()).limit(limit).all()
    learned_ids: set[int] = set()
    for run in runs:
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
    uses = db.query(AgentKnowledgeUse).filter(AgentKnowledgeUse.run_id == run_id).all()
    for use in uses:
        use.outcome = "productive_run" if helpful else "no_material_change"
    db.commit()
