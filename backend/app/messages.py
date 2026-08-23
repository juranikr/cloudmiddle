"""인앱 메시지·이의신청 헬퍼."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models import (
    Marker,
    PlaceAppeal,
    PlaceAppealStatus,
    PlaceContributor,
    PlaceEvent,
    PlaceEventAction,
    User,
    UserMessage,
    UserMessageKind,
)
from app.events import log_place_event


def contributor_user_ids(db: Session, place_ids: Iterable[int]) -> set[int]:
    ids = {int(x) for x in place_ids}
    if not ids:
        return set()
    rows = (
        db.query(PlaceContributor.user_id)
        .filter(PlaceContributor.place_id.in_(ids))
        .distinct()
        .all()
    )
    out = {r[0] for r in rows}
    creators = db.query(Marker.user_id).filter(Marker.id.in_(ids), Marker.user_id.isnot(None)).all()
    out.update(r[0] for r in creators if r[0])
    return out


def notify_users(
    db: Session,
    *,
    user_ids: Iterable[int],
    kind: UserMessageKind,
    title: str,
    body: str,
    place_id: Optional[int] = None,
    related_event_id: Optional[int] = None,
) -> list[UserMessage]:
    created: list[UserMessage] = []
    seen: set[int] = set()
    for uid in user_ids:
        if not uid or uid in seen:
            continue
        seen.add(uid)
        msg = UserMessage(
            user_id=uid,
            place_id=place_id,
            kind=kind,
            title=title[:200],
            body=body[:4000],
            related_event_id=related_event_id,
        )
        db.add(msg)
        created.append(msg)
    return created


def notify_place_contributors(
    db: Session,
    *,
    place_ids: list[int],
    kind: UserMessageKind,
    title: str,
    body: str,
    place_id: Optional[int] = None,
    related_event_id: Optional[int] = None,
) -> list[UserMessage]:
    return notify_users(
        db,
        user_ids=contributor_user_ids(db, place_ids),
        kind=kind,
        title=title,
        body=body,
        place_id=place_id,
        related_event_id=related_event_id,
    )


def notify_all_users(
    db: Session,
    *,
    kind: UserMessageKind,
    title: str,
    body: str,
    place_id: Optional[int] = None,
    related_event_id: Optional[int] = None,
) -> list[UserMessage]:
    ids = [u.id for u in db.query(User.id).all()]
    return notify_users(
        db,
        user_ids=ids,
        kind=kind,
        title=title,
        body=body,
        place_id=place_id,
        related_event_id=related_event_id,
    )


def create_appeal(
    db: Session,
    *,
    user: User,
    place_id: int,
    body: str,
    message_id: Optional[int] = None,
) -> PlaceAppeal:
    place = db.query(Marker).filter(Marker.id == place_id).first()
    if place is None:
        raise ValueError("장소를 찾을 수 없습니다")
    appeal = PlaceAppeal(
        place_id=place_id,
        user_id=user.id,
        message_id=message_id,
        body=body.strip()[:4000],
        status=PlaceAppealStatus.open,
    )
    db.add(appeal)
    db.flush()
    log_place_event(
        db,
        place_id=place_id,
        city_id=place.city_id,
        user=user,
        action=PlaceEventAction.appeal,
        summary=f"이의신청: {body.strip()[:80]}",
        payload={"appeal_id": appeal.id, "message_id": message_id},
    )
    return appeal


def list_open_appeals(db: Session, limit: int = 40) -> list[PlaceAppeal]:
    return (
        db.query(PlaceAppeal)
        .filter(
            PlaceAppeal.status == PlaceAppealStatus.open,
            PlaceAppeal.groq_read_at.is_(None),
        )
        .order_by(PlaceAppeal.created_at.asc())
        .limit(max(1, min(limit, 100)))
        .all()
    )


def mark_appeals_read(db: Session, appeal_ids: list[int]) -> int:
    if not appeal_ids:
        return 0
    now = datetime.now(timezone.utc)
    count = 0
    for a in db.query(PlaceAppeal).filter(PlaceAppeal.id.in_(appeal_ids)).all():
        if a.groq_read_at is None:
            a.groq_read_at = now
            count += 1
    return count
