"""관리자 API — 성주한 등 ADMIN_EMAILS만 접근."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.agent.runner import count_unread, run_agent
from app.agent.tools import run_tool
from app.auth import get_admin_user, hash_password
from app.config import settings
from app.db import SessionLocal, get_db
from app.models import (
    AgentKnowledge,
    AgentMission,
    AgentProposal,
    AgentQualityGapDisposition,
    AgentRun,
    AgentRunStep,
    AgentTask,
    AgentWorkItem,
    City,
    Marker,
    PlaceAppeal,
    PlaceAppealStatus,
    PlaceEvent,
    User,
)
from app.knowledge import list_knowledge, rebuild_knowledge_base
from app.db_lock import transaction_lock
from app.rollback import is_rollbackable, list_agent_actions, rollback_event
from app.schemas import AgentKnowledgeOut, AgentRunResponse, UserOut

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _json_dict(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _restore_applying_proposal(
    db: Session,
    *,
    proposal_id: int,
    original_status: str,
    admin_id: int,
) -> None:
    """Release an approval claim only when this request still owns it."""

    db.rollback()
    failed_row = db.query(AgentProposal).filter(AgentProposal.id == proposal_id).first()
    if (
        failed_row is not None
        and failed_row.status == "applying"
        and failed_row.decided_by_user_id == admin_id
    ):
        failed_row.status = original_status
        failed_row.decision_note = ""
        failed_row.decided_by_user_id = None
        failed_row.decided_at = None
        db.commit()


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


class AdminStatusOut(BaseModel):
    admin_email: str
    groq_configured: bool
    groq_model: str
    brave_place_configured: bool = False
    brave_storage_rights: bool = False
    quality_gaps_suppressed: int = 0
    markers_active: int
    zones_active: int = 0
    events_total: int
    events_unread: int
    appeals_open: int
    users_total: int
    unread_work_items: int
    knowledge_topics: int = 0
    agent_suggested_places: int = 0
    proposals_pending: int = 0


class AdminUserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=4, max_length=200)


class AdminUserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    password: Optional[str] = Field(default=None, min_length=4, max_length=200)


@router.get("/status", response_model=AdminStatusOut)
def admin_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> AdminStatusOut:
    events_unread = db.query(PlaceEvent).filter(PlaceEvent.groq_read_at.is_(None)).count()
    appeals_open = (
        db.query(PlaceAppeal)
        .filter(PlaceAppeal.status == PlaceAppealStatus.open, PlaceAppeal.groq_read_at.is_(None))
        .count()
    )
    return AdminStatusOut(
        admin_email=admin.email,
        groq_configured=bool(settings.groq_api_key),
        groq_model=settings.groq_model,
        brave_place_configured=bool(
            settings.brave_place_enabled and settings.brave_search_api_key
        ),
        brave_storage_rights=bool(settings.brave_search_storage_rights),
        quality_gaps_suppressed=db.query(AgentQualityGapDisposition).filter(
            AgentQualityGapDisposition.status.in_(("blocked", "source_exhausted", "waived"))
        ).count(),
        markers_active=db.query(Marker).filter(Marker.merged_into_id.is_(None), Marker.shape == "point").count(),
        zones_active=db.query(Marker).filter(Marker.merged_into_id.is_(None), Marker.shape == "polygon").count(),
        events_total=db.query(PlaceEvent).count(),
        events_unread=events_unread,
        appeals_open=appeals_open,
        users_total=db.query(User).count(),
        unread_work_items=count_unread(db),
        knowledge_topics=db.query(AgentKnowledge).count(),
        agent_suggested_places=db.query(Marker).filter(
            Marker.is_agent_suggested.is_(True), Marker.merged_into_id.is_(None)
        ).count(),
        proposals_pending=db.query(AgentProposal).filter(AgentProposal.status == "pending").count(),
    )


class AgentRunStatusOut(BaseModel):
    running: bool
    city_id: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional[AgentRunResponse] = None
    execution_arn: Optional[str] = None
    backend: str = "local"
    outcome_category: Optional[str] = None
    material_change_count: int = 0
    next_work_item_id: Optional[int] = None
    next_cursor: dict[str, Any] = Field(default_factory=dict)


# Gateway timeout avoidance: production delegates to Step Functions and the UI
# polls its status. Local development keeps the background-thread path.
_agent_run_lock = threading.RLock()
_agent_run_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "execution_arn": None,
    "backend": "local",
    "city_id": None,
    "city_ids": [],
}


def _step_functions_client() -> Any:
    # Lazy client creation means an unset local configuration never contacts
    # AWS and can run with the existing SQLite/background-thread setup.
    import boto3

    return boto3.client("stepfunctions", region_name=settings.aws_region)


class _AgentWorkflowBusy(RuntimeError):
    """Another workflow is running but does not include the requested city."""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _find_sfn_city_result(value: Any, city_id: int) -> Optional[dict[str, Any]]:
    """Find the compact city result inside Map/callback output shapes."""

    if isinstance(value, str):
        try:
            return _find_sfn_city_result(json.loads(value), city_id)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(value, list):
        for item in value:
            found = _find_sfn_city_result(item, city_id)
            if found is not None:
                return found
        return None
    if not isinstance(value, dict):
        return None
    cities = value.get("cities")
    if isinstance(cities, list):
        candidates = [item for item in cities if isinstance(item, dict)]
        return next(
            (item for item in candidates if _safe_int(item.get("city_id")) == city_id),
            None,
        )
    if "status" in value and ("city_id" in value or "run_id" in value):
        return value if _safe_int(value.get("city_id")) == city_id else None
    for nested in value.values():
        found = _find_sfn_city_result(nested, city_id)
        if found is not None:
            return found
    return None


def _sfn_result_response(
    description: dict[str, Any],
    *,
    city_id: int,
) -> AgentRunResponse:
    execution_status = str(description.get("status") or "FAILED")
    city_result = _find_sfn_city_result(
        description.get("output") or description.get("cause"), city_id
    )
    if city_result is not None:
        run_id = _safe_int(city_result.get("run_id"))
        return AgentRunResponse(
            ok=bool(city_result.get("ok")),
            status=str(city_result.get("status") or "failed"),
            steps=max(0, _safe_int(city_result.get("steps"))),
            message=str(city_result.get("message") or "")[:4000],
            unread_before=max(0, _safe_int(city_result.get("unread_before"))),
            unread_after=max(0, _safe_int(city_result.get("unread_after"))),
            city_id=_safe_int(city_result.get("city_id"), city_id),
            score=_safe_float(city_result.get("score")),
            performance=(
                city_result.get("performance")
                if isinstance(city_result.get("performance"), dict)
                else {}
            ),
            remaining_gaps=(
                city_result.get("remaining_gaps")
                if isinstance(city_result.get("remaining_gaps"), list)
                else []
            ),
            run_id=run_id or None,
            outcome=str(city_result.get("outcome") or "") or None,
        )

    error = str(description.get("error") or execution_status)
    if execution_status == "SUCCEEDED":
        message = (
            f"Step Functions 실행은 끝났지만 요청 도시 #{city_id}의 결과가 없습니다. "
            "해당 도시가 실행되지 않았거나 도시 처리 전에 중단되었는지 시스템 이력을 확인해 주세요."
        )
    else:
        message = f"Step Functions 실행이 {execution_status.lower()} 상태로 종료되었습니다 ({error[:160]})."
    return AgentRunResponse(
        ok=False,
        status="failed",
        steps=0,
        message=message,
        unread_before=0,
        unread_after=0,
        city_id=city_id,
    )


def _sync_sfn_execution_locked(client: Any | None = None) -> None:
    """Refresh the cached execution without ever starting a fallback run."""

    execution_arn = _agent_run_state.get("execution_arn")
    if not execution_arn or _agent_run_state.get("backend") != "step_functions":
        return
    if not _agent_run_state.get("running") and _agent_run_state.get("result") is not None:
        return
    try:
        description = (client or _step_functions_client()).describe_execution(
            executionArn=execution_arn
        )
    except Exception:
        # A status-call/network failure is ambiguous. Preserve the active ARN
        # so a later poll can retry; never launch a duplicate local execution.
        return

    _agent_run_state["started_at"] = description.get("startDate") or _agent_run_state.get(
        "started_at"
    )
    execution_cities = sorted(_execution_city_ids(description))
    if execution_cities:
        _agent_run_state["city_ids"] = execution_cities
    if description.get("status") == "RUNNING":
        _agent_run_state["running"] = True
        return

    city_id = int(_agent_run_state.get("city_id") or 1)
    _agent_run_state.update(
        {
            "running": False,
            "finished_at": description.get("stopDate") or datetime.now(timezone.utc),
            "result": _sfn_result_response(description, city_id=city_id).model_dump(),
        }
    )


def _execution_city_ids(description: dict[str, Any]) -> set[int]:
    try:
        payload = json.loads(str(description.get("input") or "{}"))
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(payload, dict) or not isinstance(payload.get("city_ids"), list):
        return set()
    return {
        parsed
        for value in payload["city_ids"]
        if (parsed := _safe_int(value)) > 0
    }


def _adopt_running_sfn_execution_locked(
    client: Any,
    *,
    city_id: Optional[int] = None,
) -> bool:
    """Recover an in-flight workflow after an API restart or rolling deploy.

    The old in-memory guard disappeared whenever ECS replaced the API task.
    Querying Step Functions before every start also lets a manual request join
    the scheduled all-city workflow instead of running the same city twice.
    """

    response = client.list_executions(
        stateMachineArn=settings.agent_state_machine_arn.strip(),
        statusFilter="RUNNING",
        maxResults=20,
    )
    if not isinstance(response, dict):
        raise RuntimeError("Step Functions returned an invalid execution list")
    running = [
        item for item in response.get("executions") or []
        if isinstance(item, dict) and str(item.get("executionArn") or "").strip()
    ]
    unrelated_running = False
    for item in running:
        execution_arn = str(item["executionArn"]).strip()
        description = client.describe_execution(executionArn=execution_arn)
        if not isinstance(description, dict) or description.get("status") != "RUNNING":
            continue
        execution_cities = _execution_city_ids(description)
        if city_id is not None and city_id not in execution_cities:
            unrelated_running = True
            continue
        selected_city_id = city_id or min(execution_cities or {1})
        _agent_run_state.update(
            {
                "running": True,
                "started_at": description.get("startDate") or item.get("startDate") or datetime.now(timezone.utc),
                "finished_at": None,
                "result": None,
                "execution_arn": execution_arn,
                "backend": "step_functions",
                "city_id": selected_city_id,
                "city_ids": sorted(execution_cities or {selected_city_id}),
            }
        )
        return True
    if unrelated_running:
        raise _AgentWorkflowBusy(
            "다른 도시 에이전트 워크플로가 실행 중입니다. 종료 후 다시 시도해 주세요."
        )
    return False


def _agent_run_status(
    *,
    refresh: bool = True,
    city_id: Optional[int] = None,
) -> AgentRunStatusOut:
    with _agent_run_lock:
        if refresh:
            if settings.agent_state_machine_arn:
                client = _step_functions_client()
                try:
                    # The API task can be replaced while a Fargate agent keeps
                    # running. Rebuild the lost in-memory pointer from SFN so a
                    # GET poll alone is sufficient after a rolling restart.
                    if not _agent_run_state.get("running"):
                        _adopt_running_sfn_execution_locked(client, city_id=city_id)
                    if (
                        city_id is not None
                        and city_id in set(_agent_run_state.get("city_ids") or [])
                    ):
                        _agent_run_state["city_id"] = city_id
                    _sync_sfn_execution_locked(client)
                except _AgentWorkflowBusy:
                    # Status is global for backward compatibility. If another
                    # city is active, keep reporting that real execution rather
                    # than manufacturing an idle result for the selected city.
                    try:
                        _adopt_running_sfn_execution_locked(client)
                        _sync_sfn_execution_locked(client)
                    except Exception:
                        pass
                except Exception:
                    # A read failure is ambiguous. Preserve the last known ARN
                    # and let the next poll retry without starting anything.
                    pass
            else:
                _sync_sfn_execution_locked()
        result = _agent_run_state["result"]
        reporting: dict[str, Any] = {}
        if isinstance(result, dict):
            run_id = _safe_int(result.get("run_id"))
            reporting = _load_agent_run_reporting(run_id) if run_id > 0 else {}
            reporting = reporting or _agent_run_reporting(
                status=str(result.get("status") or "failed"),
                metrics={"outcome": result.get("outcome")},
            )
        return AgentRunStatusOut(
            running=bool(_agent_run_state["running"]),
            city_id=_safe_int(_agent_run_state.get("city_id")) or None,
            started_at=_agent_run_state["started_at"],
            finished_at=_agent_run_state["finished_at"],
            result=AgentRunResponse(**result) if result else None,
            execution_arn=_agent_run_state.get("execution_arn"),
            backend=str(_agent_run_state.get("backend") or "local"),
            **reporting,
        )


class AgentRunRequest(BaseModel):
    city_id: int = Field(default=2, gt=0)
    research: bool = False


class AgentRunHistoryOut(BaseModel):
    id: int
    city_id: int
    mode: str
    status: str
    objective: str
    score: float
    metrics: dict[str, Any]
    summary: str
    step_count: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    outcome_category: str = "no_yield"
    material_change_count: int = 0
    next_work_item_id: Optional[int] = None
    next_cursor: dict[str, Any] = Field(default_factory=dict)


class AgentRunStepOut(BaseModel):
    sequence: int
    phase: str
    tool: str
    outcome: str
    score_delta: float
    detail: dict[str, Any]
    created_at: datetime


_TRAVELER_VISIBLE_CHANGE_TOOLS = frozenset({
    "create_place",
    "merge_places",
    "undo_merge",
    "update_place_fields",
    "update_place_context",
    "upsert_place_insights",
    "verify_place",
    "attach_image_from_url",
    "assign_place_zone",
    "assign_place_chain",
})
_AUDITED_NO_CHANGE_DISPOSITIONS = frozenset({"waived", "source_exhausted", "resolved"})


def _positive_int_or_none(value: Any) -> Optional[int]:
    parsed = _safe_int(value)
    return parsed if parsed > 0 else None


def _agent_next_cursor(metrics: dict[str, Any]) -> dict[str, Any]:
    """Expose the durable future cursor without leaking the whole checkpoint."""

    continuity = metrics.get("continuity")
    if not isinstance(continuity, dict):
        continuity = {}
    progress = continuity.get("progress")
    if not isinstance(progress, dict):
        progress = {}
    target = continuity.get("target")
    if not isinstance(target, dict):
        target = {}
    next_action = continuity.get("next_action")
    if not isinstance(next_action, dict) or not next_action:
        next_action = progress.get("next_action")
    if not isinstance(next_action, dict):
        next_action = {}

    cursor_status = str(continuity.get("status") or "")[:40]
    explicit_next_id = _positive_int_or_none(metrics.get("next_work_item_id"))
    resume_work_item_id = _positive_int_or_none(progress.get("resume_work_item_id"))
    continuity_work_item_id = (
        _positive_int_or_none(continuity.get("work_item_id"))
        if cursor_status in {"active", "ready", "blocked", "paused"}
        else None
    )
    work_item_id = (
        explicit_next_id
        or resume_work_item_id
        or continuity_work_item_id
    )
    wait_reason = str(
        continuity.get("retry_condition")
        or progress.get("retry_condition")
        or metrics.get("deferred_reason")
        or ""
    )[:500]
    if (
        cursor_status in {"done", "completed", "cancelled"}
        and work_item_id is None
        and not wait_reason
    ):
        return {}
    cursor = {
        "mission_id": _positive_int_or_none(continuity.get("mission_id")),
        "work_item_id": work_item_id,
        "target": str(target.get("title") or target.get("key") or "")[:240],
        "stage": str(continuity.get("stage") or "")[:40],
        "status": cursor_status,
        "next_tool": str(next_action.get("tool") or "")[:80],
        "wait_reason": wait_reason,
    }
    return {
        key: value
        for key, value in cursor.items()
        if value not in (None, "")
    }


def _agent_run_reporting(*, status: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """Classify the persisted outcome separately from process completion.

    A green ``completed`` process state only says the worker exited normally.
    This report tells an operator whether that execution changed a traveler-
    visible record, created an approval proposal, proved that no change was
    appropriate, deferred a cursor, or produced no useful result.
    """

    if not isinstance(metrics, dict):
        metrics = {}
    raw_changes = metrics.get("material_changes")
    changes = [
        item for item in raw_changes
        if isinstance(item, dict)
    ] if isinstance(raw_changes, list) else []
    reported_count = _safe_int(metrics.get("material_change_count"), len(changes))
    material_change_count = max(len(changes), reported_count, 0)
    delta = metrics.get("delta")
    if not isinstance(delta, dict):
        delta = {}

    proposal_created = bool(
        _safe_int(delta.get("proposals")) > 0
        or any(
            item.get("proposal_id") is not None
            or str(item.get("tool") or "") == "propose_place"
            for item in changes
        )
    )
    traveler_visible_changed = any(
        str(item.get("tool") or "") in _TRAVELER_VISIBLE_CHANGE_TOOLS
        and item.get("proposal_id") is None
        for item in changes
    )

    continuity = metrics.get("continuity")
    if not isinstance(continuity, dict):
        continuity = {}
    progress = continuity.get("progress")
    if not isinstance(progress, dict):
        progress = {}
    dispositions = progress.get("quality_dispositions")
    disposition_statuses = {
        str(item.get("status") or "")
        for item in dispositions
        if isinstance(item, dict)
    } if isinstance(dispositions, list) else set()
    deferred_or_blocked = bool(
        metrics.get("outcome") in {"deferred", "already_running"}
        or metrics.get("lane") == "discovery_deferred"
        or str(continuity.get("status") or "") in {"paused", "blocked"}
        or bool(continuity.get("blocked_reason"))
        or "blocked" in disposition_statuses
        or _safe_int(metrics.get("no_progress_actions")) > 0
    )
    audited_no_change = bool(
        disposition_statuses & _AUDITED_NO_CHANGE_DISPOSITIONS
        or metrics.get("lane") == "integrity_repair"
        or metrics.get("outcome") in {"verified", "waived"}
        or (
            _safe_int(delta.get("completed_tasks")) > 0
            and material_change_count == 0
            and metrics.get("lane") in {"quality_or_backlog", "data_integrity"}
        )
        or (
            _safe_int((metrics.get("successful_tool_counts") or {}).get("verify_place")) > 0
            if isinstance(metrics.get("successful_tool_counts"), dict)
            else False
        )
    )

    normalized_status = str(status or "").lower()
    if normalized_status == "failed":
        outcome_category = "failed"
    elif traveler_visible_changed:
        outcome_category = "traveler_visible_changed"
    elif proposal_created:
        outcome_category = "proposal_created"
    elif audited_no_change:
        outcome_category = "verified_or_waived_no_change"
    elif deferred_or_blocked:
        outcome_category = "deferred_or_blocked"
    else:
        outcome_category = "no_yield"

    next_cursor = _agent_next_cursor(metrics)
    return {
        "outcome_category": outcome_category,
        "material_change_count": material_change_count,
        "next_work_item_id": _positive_int_or_none(next_cursor.get("work_item_id")),
        "next_cursor": next_cursor,
    }


def _load_agent_run_reporting(run_id: int) -> dict[str, Any]:
    """Read the final report from the run row after a local/SFN execution."""

    db = SessionLocal()
    try:
        row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if row is None:
            return {}
        return _agent_run_reporting(status=row.status, metrics=_json_dict(row.metrics))
    except Exception:
        # Reporting must never turn a completed agent call into a status error.
        return {}
    finally:
        db.close()


def _agent_discovery_funnel(steps: list[AgentRunStep]) -> dict[str, int]:
    """Summarize the traveler-visible discovery funnel from inspectable steps.

    Search volume alone previously made an unproductive run look busy.  Keep the
    stages separate so the admin can see exactly where a candidate stopped.
    """

    funnel = {
        "search_calls": 0,
        "place_discovery_calls": 0,
        "raw_hits": 0,
        "exposed_hits": 0,
        "validated_pages": 0,
        "geocode_calls": 0,
        "geocode_candidates": 0,
        "proposal_attempts": 0,
        "proposals_created": 0,
    }
    for step in steps:
        try:
            detail = json.loads(step.detail or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(detail, dict):
            continue
        result = detail.get("result")
        if not isinstance(result, dict):
            result = {}
        if step.tool == "web_search":
            funnel["search_calls"] += 1
            # A call-level flag is enough to diagnose whether the discovery
            # lane ran.  Never reconstruct or retain Brave result counts: the
            # standard Search plan permits only transient response handling.
            if any(
                isinstance(attempt, dict)
                and attempt.get("provider") == "brave_place"
                and attempt.get("status") == "transient_discarded"
                for attempt in (result.get("provider_attempts") or [])
            ):
                funnel["place_discovery_calls"] += 1
            try:
                funnel["raw_hits"] += int(result.get("raw_results_count") or 0)
            except (TypeError, ValueError):
                pass
            funnel["exposed_hits"] += len(result.get("results") or [])
        elif step.tool == "fetch_page":
            if not result.get("error") and (result.get("text") or result.get("coordinate_candidates")):
                funnel["validated_pages"] += 1
        elif step.tool == "geocode_place":
            funnel["geocode_calls"] += 1
            funnel["geocode_candidates"] += len(result.get("results") or [])
        elif step.tool == "propose_place":
            funnel["proposal_attempts"] += 1
            if result.get("proposal_created"):
                funnel["proposals_created"] += 1
    return funnel


class AgentTaskOut(BaseModel):
    id: int
    city_id: int
    kind: str
    title: str
    detail: str
    success_metric: str
    priority: int
    status: str
    attempts: int
    result: str
    created_at: datetime
    updated_at: datetime


class AgentMissionOut(BaseModel):
    id: int
    city_id: int
    task_id: Optional[int] = None
    kind: str
    title: str
    objective: str
    success_metric: str
    status: str
    priority: int
    progress: dict[str, Any]
    last_run_id: Optional[int] = None
    updated_at: datetime


class AgentWorkItemOut(BaseModel):
    id: int
    mission_id: int
    place_id: Optional[int] = None
    target_key: str
    title: str
    stage: str
    status: str
    state_summary: str
    next_action: dict[str, Any]
    failed_approaches: list[str]
    blocked_reason: str
    retry_condition: str
    last_run_id: Optional[int] = None
    updated_at: datetime


def _run_agent_background(city_id: int, research: bool) -> None:
    db = SessionLocal()
    try:
        result = run_agent(db, city_id=city_id, autonomous_research=research)
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "steps": 0,
            "message": str(exc)[:1500],
            "unread_before": 0,
            "unread_after": 0,
            "city_id": city_id,
        }
    finally:
        db.close()
    with _agent_run_lock:
        _agent_run_state["result"] = result
        _agent_run_state["finished_at"] = datetime.now(timezone.utc)
        _agent_run_state["running"] = False


def _start_sfn_execution_locked(city_id: int, *, research: bool) -> None:
    now = datetime.now(timezone.utc)
    execution_name = f"admin-{city_id}-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:12]}"
    try:
        client = _step_functions_client()
        if _adopt_running_sfn_execution_locked(client, city_id=city_id):
            return
        response = client.start_execution(
            stateMachineArn=settings.agent_state_machine_arn.strip(),
            name=execution_name,
            input=json.dumps(
                {"city_ids": [city_id], "autonomous_research": bool(research)},
                separators=(",", ":"),
            ),
        )
        execution_arn = str(response.get("executionArn") or "").strip()
        if not execution_arn:
            raise RuntimeError("Step Functions returned no execution ARN")
    except Exception:
        # A failed AWS request can be ambiguous (the workflow may have accepted
        # it before the connection broke). Never start the in-process fallback
        # here, because that could run the same city twice.
        _agent_run_state.update(
            {
                "running": False,
                "started_at": now,
                "finished_at": datetime.now(timezone.utc),
                "result": None,
                "execution_arn": None,
                "backend": "step_functions",
                "city_id": city_id,
                "city_ids": [city_id],
            }
        )
        raise

    _agent_run_state.update(
        {
            "running": True,
            "started_at": response.get("startDate") or now,
            "finished_at": None,
            "result": None,
            "execution_arn": execution_arn,
            "backend": "step_functions",
            "city_id": city_id,
            "city_ids": [city_id],
        }
    )


@router.post("/agent/run", response_model=AgentRunStatusOut)
def admin_run_agent(
    body: AgentRunRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> AgentRunStatusOut:
    _ = admin
    if db.query(City.id).filter(City.id == body.city_id, City.status == "active").first() is None:
        raise HTTPException(status_code=404, detail="도시를 찾을 수 없습니다")
    with _agent_run_lock:
        if settings.agent_state_machine_arn:
            _sync_sfn_execution_locked()
        if _agent_run_state["running"]:
            running_cities = set(_agent_run_state.get("city_ids") or [])
            cached_city_id = _safe_int(_agent_run_state.get("city_id"))
            if not running_cities and cached_city_id > 0:
                running_cities.add(cached_city_id)
            if running_cities and body.city_id not in running_cities:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"도시 #{sorted(running_cities)[0]} 에이전트가 이미 실행 중입니다. "
                        "종료 후 다시 시도해 주세요."
                    ),
                )
        if settings.agent_state_machine_arn:
            if not _agent_run_state["running"]:
                try:
                    _start_sfn_execution_locked(body.city_id, research=body.research)
                except _AgentWorkflowBusy as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                except Exception as exc:
                    raise HTTPException(
                        status_code=502,
                        detail="에이전트 워크플로를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                    ) from exc
            return _agent_run_status(refresh=False)

        if not _agent_run_state["running"]:
            _agent_run_state.update(
                {
                    "running": True,
                    "started_at": datetime.now(timezone.utc),
                    "finished_at": None,
                    "result": None,
                    "execution_arn": None,
                    "backend": "local",
                    "city_id": body.city_id,
                    "city_ids": [body.city_id],
                }
            )
            threading.Thread(
                target=_run_agent_background,
                args=(body.city_id, body.research),
                daemon=True,
            ).start()
    return _agent_run_status(refresh=False)


@router.get("/agent/run/status", response_model=AgentRunStatusOut)
def admin_agent_run_status(
    city_id: Optional[int] = None,
    admin: User = Depends(get_admin_user),
) -> AgentRunStatusOut:
    _ = admin
    return _agent_run_status(city_id=city_id)


@router.get("/agent/runs", response_model=list[AgentRunHistoryOut])
def admin_agent_runs(
    city_id: Optional[int] = None,
    limit: int = 30,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> list[AgentRunHistoryOut]:
    _ = admin
    query = db.query(AgentRun)
    if city_id is not None:
        query = query.filter(AgentRun.city_id == city_id)
    rows = query.order_by(AgentRun.started_at.desc()).limit(max(1, min(limit, 100))).all()
    output: list[AgentRunHistoryOut] = []
    for row in rows:
        try:
            metrics = json.loads(row.metrics or "{}")
        except json.JSONDecodeError:
            metrics = {}
        if not isinstance(metrics, dict):
            metrics = {}
        metrics["discovery_funnel"] = _agent_discovery_funnel(list(row.steps or []))
        reporting = _agent_run_reporting(status=row.status, metrics=metrics)
        output.append(AgentRunHistoryOut(
            id=row.id, city_id=row.city_id, mode=row.mode, status=row.status,
            objective=row.objective, score=row.score, metrics=metrics, summary=row.summary,
            step_count=len(row.steps or []), started_at=row.started_at, finished_at=row.finished_at,
            **reporting,
        ))
    return output


@router.get("/agent/runs/{run_id}/steps", response_model=list[AgentRunStepOut])
def admin_agent_run_steps(
    run_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> list[AgentRunStepOut]:
    _ = admin
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="에이전트 실행 이력을 찾을 수 없습니다.")
    output: list[AgentRunStepOut] = []
    for row in run.steps:
        try:
            detail = json.loads(row.detail or "{}")
        except json.JSONDecodeError:
            detail = {"raw": row.detail or ""}
        output.append(AgentRunStepOut(
            sequence=row.sequence,
            phase=row.phase,
            tool=row.tool,
            outcome=row.outcome,
            score_delta=row.score_delta,
            detail=detail if isinstance(detail, dict) else {"value": detail},
            created_at=row.created_at,
        ))
    return output


@router.get("/agent/tasks", response_model=list[AgentTaskOut])
def admin_agent_tasks(
    city_id: Optional[int] = None,
    task_status: str = "pending",
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> list[AgentTaskOut]:
    _ = admin
    query = db.query(AgentTask)
    if city_id is not None:
        query = query.filter(AgentTask.city_id == city_id)
    if task_status != "all":
        query = query.filter(AgentTask.status == task_status)
    rows = query.order_by(AgentTask.priority.desc(), AgentTask.created_at.asc()).limit(max(1, min(limit, 200))).all()
    return [AgentTaskOut(
        id=row.id, city_id=row.city_id, kind=row.kind, title=row.title, detail=row.detail,
        success_metric=row.success_metric, priority=row.priority, status=row.status,
        attempts=row.attempts, result=row.result, created_at=row.created_at, updated_at=row.updated_at,
    ) for row in rows]


@router.get("/agent/missions", response_model=list[AgentMissionOut])
def admin_agent_missions(
    city_id: Optional[int] = None,
    mission_status: str = "active",
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> list[AgentMissionOut]:
    _ = admin
    query = db.query(AgentMission)
    if city_id is not None:
        query = query.filter(AgentMission.city_id == city_id)
    if mission_status != "all":
        query = query.filter(AgentMission.status == mission_status)
    rows = query.order_by(AgentMission.priority.desc(), AgentMission.updated_at.desc()).limit(max(1, min(limit, 200))).all()
    return [AgentMissionOut(
        id=row.id, city_id=row.city_id, task_id=row.task_id, kind=row.kind,
        title=row.title, objective=row.objective, success_metric=row.success_metric,
        status=row.status, priority=row.priority,
        progress=_json_dict(row.progress), last_run_id=row.last_run_id, updated_at=row.updated_at,
    ) for row in rows]


@router.get("/agent/missions/{mission_id}/work-items", response_model=list[AgentWorkItemOut])
def admin_agent_work_items(
    mission_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> list[AgentWorkItemOut]:
    _ = admin
    if db.get(AgentMission, mission_id) is None:
        raise HTTPException(status_code=404, detail="에이전트 미션을 찾을 수 없습니다.")
    rows = db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission_id).order_by(
        AgentWorkItem.priority.desc(), AgentWorkItem.id.asc()
    ).all()
    return [AgentWorkItemOut(
        id=row.id, mission_id=row.mission_id, place_id=row.place_id,
        target_key=row.target_key, title=row.title, stage=row.stage, status=row.status,
        state_summary=row.state_summary, next_action=_json_dict(row.next_action),
        failed_approaches=_json_list(row.failed_approaches), blocked_reason=row.blocked_reason,
        retry_condition=row.retry_condition, last_run_id=row.last_run_id, updated_at=row.updated_at,
    ) for row in rows]


@router.get("/knowledge", response_model=list[AgentKnowledgeOut])
def admin_knowledge(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
    limit: int = 50,
) -> list[AgentKnowledgeOut]:
    _ = admin
    rows = list_knowledge(db, limit=limit)
    def as_list(raw: str) -> list[str]:
        try:
            value = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []
    return [
        AgentKnowledgeOut(
            id=r.id,
            topic=r.topic,
            title=r.title,
            content=r.content or "",
            scope=r.scope,
            city_id=r.city_id,
            place_id=r.place_id,
            category=r.category or "playbook",
            summary=r.summary or "",
            principles=as_list(r.principles),
            next_actions=as_list(r.next_actions),
            keywords=as_list(r.keywords),
            applicability=_json_dict(r.applicability),
            source_refs=as_list(r.source_refs),
            evidence_count=r.evidence_count or 0,
            quality_score=r.quality_score or 0,
            retrieval_count=r.retrieval_count or 0,
            last_retrieved_at=r.last_retrieved_at,
            status=r.status or "active",
            version=r.version or 1,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.post("/knowledge/rebuild")
def admin_rebuild_knowledge(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> dict[str, int]:
    _ = admin
    return rebuild_knowledge_base(db)


class AgentProposalOut(BaseModel):
    id: int
    city_id: int
    place_id: Optional[int] = None
    result_place_id: Optional[int] = None
    action: str
    title: str
    payload: dict[str, Any]
    evidence: str
    source_urls: list[str]
    confidence: float
    status: str
    decision_note: str
    created_at: datetime
    decided_at: Optional[datetime] = None


class AgentProposalDecision(BaseModel):
    note: str = Field(default="", max_length=2000)


def _proposal_out(row: AgentProposal) -> AgentProposalOut:
    try:
        payload = json.loads(row.payload or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        urls = json.loads(row.source_urls or "[]")
    except json.JSONDecodeError:
        urls = []
    return AgentProposalOut(
        id=row.id,
        city_id=row.city_id,
        place_id=row.place_id,
        result_place_id=row.result_place_id,
        action=row.action,
        title=row.title,
        payload=payload if isinstance(payload, dict) else {},
        evidence=row.evidence or "",
        source_urls=urls if isinstance(urls, list) else [],
        confidence=float(row.confidence or 0),
        status=row.status,
        decision_note=row.decision_note or "",
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


@router.get("/agent/proposals", response_model=list[AgentProposalOut])
def admin_agent_proposals(
    city_id: Optional[int] = None,
    proposal_status: str = "pending",
    limit: int = 100,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> list[AgentProposalOut]:
    _ = admin
    query = db.query(AgentProposal)
    if city_id is not None:
        query = query.filter(AgentProposal.city_id == city_id)
    if proposal_status and proposal_status != "all":
        query = query.filter(AgentProposal.status == proposal_status)
    rows = query.order_by(AgentProposal.created_at.desc()).limit(max(1, min(limit, 300))).all()
    return [_proposal_out(row) for row in rows]


@router.post("/agent/proposals/{proposal_id}/approve", response_model=AgentProposalOut)
def approve_agent_proposal(
    proposal_id: int,
    body: AgentProposalDecision,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> AgentProposalOut:
    transaction_lock(db, f"agent-proposal-decision:{proposal_id}")
    row = (
        db.query(AgentProposal)
        .filter(AgentProposal.id == proposal_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="제안을 찾을 수 없습니다")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="이미 처리된 제안입니다")
    try:
        payload = json.loads(row.payload or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="제안 데이터가 손상되었습니다") from exc
    original_status = row.status
    if row.action not in {"create_place", "merge_places"}:
        raise HTTPException(status_code=400, detail="지원하지 않는 제안 작업입니다")
    # Claim the decision before the applying tool performs its own commit. This
    # closes the post-apply/pre-status race for both another approval and reject.
    row.status = "applying"
    row.decision_note = body.note.strip()
    row.decided_by_user_id = admin.id
    row.decided_at = datetime.now(timezone.utc)
    db.commit()
    try:
        result = run_tool(db, row.action, payload, city_id=row.city_id, approved=True)
    except Exception as exc:  # noqa: BLE001 - approval claim must never remain orphaned
        _restore_applying_proposal(
            db,
            proposal_id=proposal_id,
            original_status=original_status,
            admin_id=admin.id,
        )
        raise HTTPException(
            status_code=500,
            detail="제안 적용 중 오류가 발생해 승인 상태를 원복했습니다. 다시 시도해 주세요.",
        ) from exc
    if not isinstance(result, dict) or result.get("error") or not result.get("ok"):
        _restore_applying_proposal(
            db,
            proposal_id=proposal_id,
            original_status=original_status,
            admin_id=admin.id,
        )
        detail = (
            result.get("detail") or result.get("error")
            if isinstance(result, dict)
            else "apply_failed"
        )
        raise HTTPException(status_code=400, detail=f"제안을 적용하지 못했습니다: {detail}")
    row = db.query(AgentProposal).filter(AgentProposal.id == proposal_id).first()
    assert row is not None
    if row.status != "applying" or row.decided_by_user_id != admin.id:
        raise HTTPException(status_code=409, detail="제안 결정 상태가 변경되었습니다")
    row.status = "approved"
    row.result_place_id = int(result.get("place_id") or result.get("target_id") or 0) or None
    db.commit()
    db.refresh(row)
    return _proposal_out(row)


@router.post("/agent/proposals/{proposal_id}/reject", response_model=AgentProposalOut)
def reject_agent_proposal(
    proposal_id: int,
    body: AgentProposalDecision,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> AgentProposalOut:
    transaction_lock(db, f"agent-proposal-decision:{proposal_id}")
    row = (
        db.query(AgentProposal)
        .filter(AgentProposal.id == proposal_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="제안을 찾을 수 없습니다")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="이미 처리된 제안입니다")
    row.status = "rejected"
    row.decision_note = body.note.strip()
    row.decided_by_user_id = admin.id
    row.decided_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _proposal_out(row)


class AdminAgentActionOut(BaseModel):
    id: int
    place_id: Optional[int] = None
    place_title: str = ""
    action: str
    summary: str
    rolled_back: bool = False
    can_rollback: bool = False
    created_at: datetime


class AdminRollbackRequest(BaseModel):
    note: str = Field(default="", max_length=1000)


class AdminRollbackOut(BaseModel):
    ok: bool
    rollback_event_id: int
    message: str


@router.get("/agent/actions", response_model=list[AdminAgentActionOut])
def admin_agent_actions(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
    limit: int = 40,
    city_id: Optional[int] = None,
) -> list[AdminAgentActionOut]:
    _ = admin
    rows = list_agent_actions(db, limit=limit, city_id=city_id)
    place_ids = {e.place_id for e in rows if e.place_id}
    titles: dict[int, str] = {}
    if place_ids:
        for m in db.query(Marker).filter(Marker.id.in_(place_ids)).all():
            titles[m.id] = m.title
    out: list[AdminAgentActionOut] = []
    for e in rows:
        try:
            payload = json.loads(e.payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        out.append(
            AdminAgentActionOut(
                id=e.id,
                place_id=e.place_id,
                place_title=titles.get(e.place_id or -1, ""),
                action=e.action.value if e.action else "",
                summary=e.summary or "",
                rolled_back=bool(payload.get("rolled_back")),
                can_rollback=is_rollbackable(e),
                created_at=e.created_at,
            )
        )
    return out


@router.post("/agent/actions/{event_id}/rollback", response_model=AdminRollbackOut)
def admin_rollback_action(
    event_id: int,
    body: AdminRollbackRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> AdminRollbackOut:
    try:
        rb = rollback_event(db, event_id=event_id, admin=admin, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AdminRollbackOut(
        ok=True,
        rollback_event_id=rb.id,
        message=rb.summary,
    )


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        is_admin=user.email.lower() in settings.admin_email_list,
    )


@router.get("/users", response_model=list[UserOut])
def admin_list_users(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> list[UserOut]:
    _ = admin
    return [_user_out(u) for u in db.query(User).order_by(User.id.asc()).all()]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def admin_create_user(
    body: AdminUserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> UserOut:
    _ = admin
    email = body.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="이미 있는 이메일입니다")
    user = User(
        email=email,
        display_name=body.display_name.strip(),
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.patch("/users/{user_id}", response_model=UserOut)
def admin_update_user(
    user_id: int,
    body: AdminUserUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> UserOut:
    _ = admin
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    data = body.model_dump(exclude_unset=True)
    if "email" in data and data["email"]:
        email = str(data["email"]).lower().strip()
        exists = db.query(User).filter(User.email == email, User.id != user_id).first()
        if exists:
            raise HTTPException(status_code=400, detail="이미 있는 이메일입니다")
        user.email = email
    if "display_name" in data and data["display_name"] is not None:
        user.display_name = data["display_name"].strip()
    if data.get("password"):
        user.password_hash = hash_password(data["password"])
    db.commit()
    db.refresh(user)
    return _user_out(user)

@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> Response:
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="자기 자신은 삭제할 수 없습니다")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    if user.email.lower() in settings.admin_email_list:
        raise HTTPException(status_code=400, detail="관리자 계정은 삭제할 수 없습니다")
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
