"""관리자 API — 성주한 등 ADMIN_EMAILS만 접근."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.agent.runner import count_unread, run_agent
from app.auth import get_admin_user, hash_password
from app.config import settings
from app.db import get_db
from app.models import Marker, PlaceAppeal, PlaceAppealStatus, PlaceEvent, User
from app.schemas import AgentRunResponse, UserOut

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
    )


@router.post("/agent/run", response_model=AgentRunResponse)
def admin_run_agent(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> AgentRunResponse:
    _ = admin
    return AgentRunResponse(**run_agent(db))


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

@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> None:
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="자기 자신은 삭제할 수 없습니다")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    if user.email.lower() in settings.admin_email_list:
        raise HTTPException(status_code=400, detail="관리자 계정은 삭제할 수 없습니다")
    db.delete(user)
    db.commit()
