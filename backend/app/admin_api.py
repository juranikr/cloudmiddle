"""관리자 API — 성주한 등 ADMIN_EMAILS만 접근."""

from __future__ import annotations

import json
import threading
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
    AgentProposal,
    AgentRun,
    AgentRunStep,
    AgentTask,
    City,
    Marker,
    PlaceAppeal,
    PlaceAppealStatus,
    PlaceEvent,
    User,
)
from app.knowledge import list_knowledge, rebuild_knowledge_base
from app.rollback import is_rollbackable, list_agent_actions, rollback_event
from app.schemas import AgentKnowledgeOut, AgentRunResponse, UserOut

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminStatusOut(BaseModel):
    admin_email: str
    groq_configured: bool
    groq_model: str
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
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional[AgentRunResponse] = None


# 게이트웨이(≈60초) 타임아웃 회피: 실행은 백그라운드 스레드, 관리자 UI는 상태 폴링
_agent_run_lock = threading.Lock()
_agent_run_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
}


def _agent_run_status() -> AgentRunStatusOut:
    result = _agent_run_state["result"]
    return AgentRunStatusOut(
        running=bool(_agent_run_state["running"]),
        started_at=_agent_run_state["started_at"],
        finished_at=_agent_run_state["finished_at"],
        result=AgentRunResponse(**result) if result else None,
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


def _run_agent_background(city_id: int, research: bool) -> None:
    db = SessionLocal()
    try:
        result = run_agent(db, city_id=city_id, autonomous_research=research)
    except Exception as exc:
        result = {
            "ok": False,
            "steps": 0,
            "message": str(exc)[:1500],
            "unread_before": 0,
            "unread_after": 0,
            "city_id": city_id,
        }
    finally:
        db.close()
    _agent_run_state["result"] = result
    _agent_run_state["finished_at"] = datetime.now(timezone.utc)
    _agent_run_state["running"] = False


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
        if not _agent_run_state["running"]:
            _agent_run_state.update(
                {
                    "running": True,
                    "started_at": datetime.now(timezone.utc),
                    "finished_at": None,
                    "result": None,
                }
            )
            threading.Thread(
                target=_run_agent_background,
                args=(body.city_id, body.research),
                daemon=True,
            ).start()
    return _agent_run_status()


@router.get("/agent/run/status", response_model=AgentRunStatusOut)
def admin_agent_run_status(
    admin: User = Depends(get_admin_user),
) -> AgentRunStatusOut:
    _ = admin
    return _agent_run_status()


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
        output.append(AgentRunHistoryOut(
            id=row.id, city_id=row.city_id, mode=row.mode, status=row.status,
            objective=row.objective, score=row.score, metrics=metrics, summary=row.summary,
            step_count=len(row.steps or []), started_at=row.started_at, finished_at=row.finished_at,
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
            evidence_count=r.evidence_count or 0,
            quality_score=r.quality_score or 0,
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
    row = db.query(AgentProposal).filter(AgentProposal.id == proposal_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="제안을 찾을 수 없습니다")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="이미 처리된 제안입니다")
    try:
        payload = json.loads(row.payload or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="제안 데이터가 손상되었습니다") from exc
    result = run_tool(db, row.action, payload, city_id=row.city_id, approved=True)
    if not isinstance(result, dict) or result.get("error") or not result.get("ok"):
        db.rollback()
        detail = (
            result.get("detail") or result.get("error")
            if isinstance(result, dict)
            else "apply_failed"
        )
        raise HTTPException(status_code=400, detail=f"제안을 적용하지 못했습니다: {detail}")
    row = db.query(AgentProposal).filter(AgentProposal.id == proposal_id).first()
    assert row is not None
    row.status = "approved"
    row.decision_note = body.note.strip()
    row.decided_by_user_id = admin.id
    row.decided_at = datetime.now(timezone.utc)
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
    row = db.query(AgentProposal).filter(AgentProposal.id == proposal_id).first()
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
) -> list[AdminAgentActionOut]:
    _ = admin
    rows = list_agent_actions(db, limit=limit)
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
