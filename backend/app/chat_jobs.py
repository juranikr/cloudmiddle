"""Durable chat jobs; HTTP requests never own the worker's database session.

This is deliberately not a retrying queue. A process restart is surfaced as an
explicit failure after the deadline, never an automatic repeat of paid model or
write-tool calls. Live jobs survive another process's rolling-deploy startup.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import TravelChatJob
from app.schemas import TravelChatMessageOut, TravelChatRequest, TravelChatResponse

ACTIVE_STATUSES = ("pending", "running")
FAILED_MESSAGE = "여행 답변을 완료하지 못했습니다. 대화 기록을 확인한 뒤 다시 요청해 주세요."
EXPIRED_MESSAGE = "서버 재시작 또는 작업 기한 만료로 답변을 완료하지 못했습니다. 대화 기록을 확인해 주세요."


class ChatJobConflict(ValueError):
    pass


class TravelChatJobOut(BaseModel):
    id: str
    city_id: int
    status: Literal["pending", "running", "completed", "failed"]
    expires_at: datetime
    response: TravelChatResponse | None = None
    error: str | None = None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def fail_stale_chat_jobs(db: Session, *, now: datetime | None = None) -> int:
    """Release abandoned work only after its deadline, including after restart."""
    now = now or datetime.now(timezone.utc)
    count = db.query(TravelChatJob).filter(
        TravelChatJob.status.in_(ACTIVE_STATUSES), TravelChatJob.expires_at <= now,
    ).update({
        TravelChatJob.status: "failed", TravelChatJob.active_key: None,
        TravelChatJob.error: EXPIRED_MESSAGE, TravelChatJob.finished_at: now,
    }, synchronize_session=False)
    db.commit()
    db.expire_all()
    return count


def create_chat_job(db: Session, *, user_id: int, body: TravelChatRequest) -> tuple[TravelChatJob, bool]:
    fail_stale_chat_jobs(db)
    active_key = f"{user_id}:{body.city_id}"

    def existing_job() -> TravelChatJob | None:
        return db.query(TravelChatJob).filter(TravelChatJob.active_key == active_key).first()

    def attach_or_conflict(existing: TravelChatJob) -> tuple[TravelChatJob, bool]:
        if existing.message == body.message.strip() and existing.selected_place_id == body.selected_place_id:
            return existing, False
        raise ChatJobConflict("같은 도시의 이전 답변을 기다린 뒤 새 메시지를 보내 주세요.")

    existing = existing_job()
    if existing is not None:
        return attach_or_conflict(existing)
    now = datetime.now(timezone.utc)
    # The harness retains its complete configured timeout. The HTTP waiter gets
    # a little more time for serialization/cleanup, never a shorter model limit.
    ttl = max(930, int(settings.codex_timeout_seconds) + 30)
    job = TravelChatJob(
        id=str(uuid4()), user_id=user_id, city_id=body.city_id, status="pending",
        active_key=active_key, message=body.message.strip(), selected_place_id=body.selected_place_id,
        created_at=now, expires_at=now + timedelta(seconds=ttl),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = existing_job()
        if existing is None:
            raise
        return attach_or_conflict(existing)
    db.refresh(job)
    return job, True


def chat_job_to_out(job: TravelChatJob) -> TravelChatJobOut:
    response = TravelChatResponse.model_validate_json(job.result) if job.status == "completed" and job.result else None
    return TravelChatJobOut(
        id=job.id, city_id=job.city_id, status=job.status,
        expires_at=_utc(job.expires_at), response=response, error=job.error,
    )


def _chat_result(result: dict[str, Any]) -> TravelChatResponse:
    row = result["row"]

    def json_list(name: str) -> list:
        try:
            value = json.loads(getattr(row, name, "[]") or "[]")
        except (TypeError, ValueError):
            value = []
        return value if isinstance(value, list) else []

    return TravelChatResponse(
        message=TravelChatMessageOut(
            id=row.id, city_id=row.city_id, role=row.role, content=row.content,
            sources=json_list("sources"), place_ids=json_list("place_ids"),
            candidates=json_list("candidates"), created_at=row.created_at,
        ),
        model=result["model"], grounded_place_ids=result["place_ids"],
    )


def run_chat_job(
    job_id: str,
    *,
    session_factory: Callable[[], Session] | None = None,
    answer: Callable[..., dict[str, Any]] | None = None,
) -> None:
    """Claim once, run once, persist once. Never disclose provider exception text."""
    if answer is None:
        from app.travel_chat import answer_travel_chat

        answer = answer_travel_chat
    db = (session_factory or SessionLocal)()
    try:
        fail_stale_chat_jobs(db)
        now = datetime.now(timezone.utc)
        claimed = db.query(TravelChatJob).filter(
            TravelChatJob.id == job_id, TravelChatJob.status == "pending",
            TravelChatJob.expires_at > now,
        ).update({TravelChatJob.status: "running", TravelChatJob.started_at: now}, synchronize_session=False)
        db.commit()
        if not claimed:
            return
        job = db.get(TravelChatJob, job_id)
        result = answer(
            db, user_id=job.user_id, city_id=job.city_id,
            message=job.message, selected_place_id=job.selected_place_id,
        )
        response_json = _chat_result(result).model_dump_json()
        db.expire_all()
        job = db.get(TravelChatJob, job_id)
        if job is not None and job.status == "running":
            now = datetime.now(timezone.utc)
            if _utc(job.expires_at) <= now:
                job.status, job.error = "failed", EXPIRED_MESSAGE
            else:
                job.status, job.result = "completed", response_json
            job.active_key, job.finished_at = None, now
            db.commit()
    except Exception:
        db.rollback()
        # Do not persist/log raw exception strings: SDK failures may contain
        # authorization headers, tool input, or upstream response bodies.
        job = db.get(TravelChatJob, job_id)
        if job is not None and job.status in ACTIVE_STATUSES:
            job.status, job.error, job.active_key = "failed", FAILED_MESSAGE, None
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()
