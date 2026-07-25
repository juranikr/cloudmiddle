from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import PlaceContributor, PlaceEvent, PlaceEventAction, User


def ensure_contributor(db: Session, place_id: int, user_id: int) -> None:
    exists = (
        db.query(PlaceContributor)
        .filter(PlaceContributor.place_id == place_id, PlaceContributor.user_id == user_id)
        .first()
    )
    if exists is None:
        db.add(PlaceContributor(place_id=place_id, user_id=user_id))


def log_place_event(
    db: Session,
    *,
    place_id: Optional[int],
    user: Optional[User],
    action: PlaceEventAction,
    summary: str,
    payload: Optional[dict[str, Any]] = None,
    actor: str = "user",
) -> PlaceEvent:
    event = PlaceEvent(
        place_id=place_id,
        user_id=user.id if user else None,
        actor=actor,
        action=action,
        summary=summary[:500],
        payload=json.dumps(payload or {}, ensure_ascii=False),
    )
    db.add(event)
    return event


def mark_events_read(db: Session, event_ids: list[int]) -> int:
    if not event_ids:
        return 0
    now = datetime.now(timezone.utc)
    q = db.query(PlaceEvent).filter(PlaceEvent.id.in_(event_ids), PlaceEvent.groq_read_at.is_(None))
    count = 0
    for ev in q.all():
        ev.groq_read_at = now
        count += 1
    return count
