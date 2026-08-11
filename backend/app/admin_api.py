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
from app.auth import get_admin_user, hash_password
from app.config import settings
from app.db import SessionLocal, get_db
from app.models import AgentKnowledge, Marker, PlaceAppeal, PlaceAppealStatus, PlaceEvent, User
from app.knowledge import list_knowledge
from app.rollback import is_rollbackable, list_agent_actions, rollback_event
from app.schemas import AgentKnowledgeOut, AgentRunResponse, UserOut

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminStatusOut(BaseModel):
    admin_email: str
    groq_configured: bool
    groq_model: str
    markers_active: int
    events_total: int
    events_unread: int
    appeals_open: int
    users_total: int
    unread_work_items: int
    knowledge_topics: int = 0
    agent_suggested_places: int = 0


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
        markers_active=db.query(Marker).filter(Marker.merged_into_id.is_(None)).count(),
        events_total=db.query(PlaceEvent).count(),
        events_unread=events_unread,
        appeals_open=appeals_open,
        users_total=db.query(User).count(),
        unread_work_items=count_unread(db),
        knowledge_topics=db.query(AgentKnowledge).count(),
        agent_suggested_places=db.query(Marker).filter(
            Marker.is_agent_suggested.is_(True), Marker.merged_into_id.is_(None)
        ).count(),
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


def _run_agent_background() -> None:
    db = SessionLocal()
    try:
        result = run_agent(db)
    except Exception as exc:
        result = {
            "ok": False,
            "steps": 0,
            "message": str(exc)[:1500],
            "unread_before": 0,
            "unread_after": 0,
        }
    finally:
        db.close()
    _agent_run_state["result"] = result
    _agent_run_state["finished_at"] = datetime.now(timezone.utc)
    _agent_run_state["running"] = False


@router.post("/agent/run", response_model=AgentRunStatusOut)
def admin_run_agent(
    admin: User = Depends(get_admin_user),
) -> AgentRunStatusOut:
    _ = admin
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
            threading.Thread(target=_run_agent_background, daemon=True).start()
    return _agent_run_status()


@router.get("/agent/run/status", response_model=AgentRunStatusOut)
def admin_agent_run_status(
    admin: User = Depends(get_admin_user),
) -> AgentRunStatusOut:
    _ = admin
    return _agent_run_status()


@router.get("/knowledge", response_model=list[AgentKnowledgeOut])
def admin_knowledge(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
    limit: int = 50,
) -> list[AgentKnowledgeOut]:
    _ = admin
    rows = list_knowledge(db, limit=limit)
    return [
        AgentKnowledgeOut(
            id=r.id,
            topic=r.topic,
            title=r.title,
            content=r.content or "",
            scope=r.scope,
            city_id=r.city_id,
            place_id=r.place_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


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
