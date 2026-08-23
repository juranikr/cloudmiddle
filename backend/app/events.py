from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models import Marker, PlaceContributor, PlaceEvent, PlaceEventAction, User

FIELD_LABELS_KO: dict[str, str] = {
    "title": "제목",
    "description": "설명",
    "category": "분류",
    "lat": "위도",
    "lng": "경도",
    "polygon": "구역",
    "agent_context": "정리 메모",
    "image_ids": "사진 순서",
    "image_id": "사진",
    "local_name": "현지 명칭",
    "append_note": "설명 보완",
    "replace_title": "제목",
    "zone_id": "소속 구역",
    "chain_id": "체인",
    "branch_name": "지점명",
    "travel_role": "여행 역할",
}


def ensure_contributor(db: Session, place_id: int, user_id: int) -> None:
    """참여자 명단(유니크)에 1회만 추가. 세션 pending 중복 INSERT 방지."""
    for obj in db.new:
        if (
            isinstance(obj, PlaceContributor)
            and obj.place_id == place_id
            and obj.user_id == user_id
        ):
            return
    exists = (
        db.query(PlaceContributor)
        .filter(PlaceContributor.place_id == place_id, PlaceContributor.user_id == user_id)
        .first()
    )
    if exists is None:
        db.add(PlaceContributor(place_id=place_id, user_id=user_id))


def marker_field_snapshot(m: Marker) -> dict[str, Any]:
    return {
        "city_id": m.city_id,
        "title": m.title,
        "description": m.description or "",
        "category": m.category.value if m.category else "other",
        "lat": m.lat,
        "lng": m.lng,
        "polygon": m.polygon or "",
        "agent_context": m.agent_context or "",
        "zone_id": m.zone_id,
        "chain_id": m.chain_id,
        "branch_name": m.branch_name or "",
        "travel_role": m.travel_role or "general",
    }


def diff_marker_fields(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    keys: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """필드별 before/after 목록. 값이 같은 필드는 제외."""
    use_keys = keys if keys is not None else sorted(set(before) | set(after))
    changes: list[dict[str, Any]] = []
    for key in use_keys:
        b = before.get(key)
        a = after.get(key)
        if b != a:
            changes.append({"field": key, "before": b, "after": a})
    return changes


def summary_for_changes(prefix: str, changes: list[dict[str, Any]]) -> str:
    if not changes:
        return prefix[:500]
    labels = [FIELD_LABELS_KO.get(str(c["field"]), str(c["field"])) for c in changes]
    # 중복 라벨 제거(순서 유지)
    seen: set[str] = set()
    uniq: list[str] = []
    for lab in labels:
        if lab not in seen:
            seen.add(lab)
            uniq.append(lab)
    return f"{prefix}: {', '.join(uniq)}"[:500]


def _is_flat_snapshot(data: dict[str, Any]) -> bool:
    return not any(isinstance(v, (dict, list)) for v in data.values())


def changes_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """API용: payload에서 changes를 추출·정규화. 전체 내부 스냅샷은 노출하지 않음."""
    raw = payload.get("changes")
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or "field" not in item:
                continue
            out.append(
                {
                    "field": str(item["field"]),
                    "before": item.get("before"),
                    "after": item.get("after"),
                }
            )
        if out:
            return out

    before = payload.get("before")
    after = payload.get("after")
    if (
        isinstance(before, dict)
        and isinstance(after, dict)
        and _is_flat_snapshot(before)
        and _is_flat_snapshot(after)
    ):
        return diff_marker_fields(before, after)
    return []


def log_place_event(
    db: Session,
    *,
    place_id: Optional[int],
    city_id: Optional[int] = None,
    user: Optional[User],
    action: PlaceEventAction,
    summary: str,
    payload: Optional[dict[str, Any]] = None,
    actor: str = "user",
) -> PlaceEvent:
    now = datetime.now(timezone.utc)
    resolved_city_id = city_id
    if resolved_city_id is None and place_id is not None:
        resolved_city_id = (
            db.query(Marker.city_id).filter(Marker.id == place_id).scalar()
        )

    payload_data = dict(payload or {})
    if place_id is not None:
        # The FK is SET NULL when a marker is deleted. Keep the immutable
        # identity inside the audit payload so create/delete pairs can be
        # correlated without guessing from a chain title.
        payload_data.setdefault("place_id", place_id)
    if resolved_city_id is None:
        raw_payload_city_id = payload_data.get("city_id")
        try:
            resolved_city_id = (
                int(raw_payload_city_id) if raw_payload_city_id is not None else None
            )
        except (TypeError, ValueError):
            resolved_city_id = None
    if resolved_city_id is not None:
        # A redundant payload copy keeps exported/raw audit records
        # self-describing even outside this database schema.
        payload_data.setdefault("city_id", resolved_city_id)

    # Agent-authored events are already "known" to the agent — mark read immediately.
    event = PlaceEvent(
        city_id=resolved_city_id,
        place_id=place_id,
        user_id=user.id if user else None,
        actor=actor,
        action=action,
        summary=summary[:500],
        payload=json.dumps(payload_data, ensure_ascii=False),
        groq_read_at=now if actor == "agent" else None,
    )
    db.add(event)
    return event


def place_event_city_clause(city_id: int):
    """City scope with a compatibility path for pre-migration rows.

    New and migrated rows use ``PlaceEvent.city_id``.  The marker relationship
    is consulted only while a legacy row is still unattributed.
    """

    return or_(
        PlaceEvent.city_id == city_id,
        and_(
            PlaceEvent.city_id.is_(None),
            PlaceEvent.place.has(Marker.city_id == city_id),
        ),
    )


def mark_events_read(
    db: Session,
    event_ids: list[int],
    *,
    city_id: Optional[int] = None,
) -> int:
    if not event_ids:
        return 0
    now = datetime.now(timezone.utc)
    q = db.query(PlaceEvent).filter(PlaceEvent.id.in_(event_ids), PlaceEvent.groq_read_at.is_(None))
    if city_id is not None:
        q = q.filter(place_event_city_clause(city_id))
    count = 0
    for ev in q.all():
        ev.groq_read_at = now
        count += 1
    return count
