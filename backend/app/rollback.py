"""에이전트 변경 롤백."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.events import log_place_event
from app.models import (
    Marker,
    MarkerCategory,
    PlaceEvent,
    PlaceEventAction,
    PlaceImage,
    User,
)


ROLLBACKABLE_ACTIONS = {
    PlaceEventAction.merge,
    PlaceEventAction.update,
    PlaceEventAction.context_update,
    PlaceEventAction.agent_create,
    PlaceEventAction.image_reorder,
}


def _payload(ev: PlaceEvent) -> dict[str, Any]:
    try:
        return json.loads(ev.payload or "{}")
    except json.JSONDecodeError:
        return {}


def _save_payload(ev: PlaceEvent, data: dict[str, Any]) -> None:
    ev.payload = json.dumps(data, ensure_ascii=False)


def marker_snapshot(m: Marker) -> dict[str, Any]:
    return {
        "title": m.title,
        "description": m.description or "",
        "category": m.category.value if m.category else "other",
        "agent_context": m.agent_context or "",
        "lat": m.lat,
        "lng": m.lng,
        "merged_into_id": m.merged_into_id,
        "is_agent_suggested": bool(m.is_agent_suggested),
    }


def apply_marker_snapshot(m: Marker, snap: dict[str, Any]) -> None:
    if "title" in snap:
        m.title = str(snap["title"])[:200]
    if "description" in snap:
        m.description = str(snap["description"])[:2000]
    if "agent_context" in snap:
        m.agent_context = str(snap["agent_context"])[:8000]
    if "category" in snap:
        try:
            m.category = MarkerCategory(str(snap["category"]))
        except ValueError:
            pass
    if "lat" in snap:
        m.lat = float(snap["lat"])
    if "lng" in snap:
        m.lng = float(snap["lng"])


def is_rollbackable(ev: PlaceEvent) -> bool:
    if ev.actor != "agent":
        return False
    if ev.action not in ROLLBACKABLE_ACTIONS:
        return False
    data = _payload(ev)
    if data.get("rolled_back"):
        return False
    if not data.get("before") and ev.action != PlaceEventAction.agent_create:
        # 예전 이벤트(스냅샷 없음) — merge만 source_id로 부분 복원 시도
        if ev.action == PlaceEventAction.merge and data.get("source_id"):
            return True
        return False
    if ev.action == PlaceEventAction.agent_create:
        return True
    return True


def list_agent_actions(
    db: Session, *, limit: int = 50, city_id: Optional[int] = None
) -> list[PlaceEvent]:
    query = db.query(PlaceEvent).filter(
        PlaceEvent.actor == "agent",
        PlaceEvent.action.in_(list(ROLLBACKABLE_ACTIONS)),
    )
    if city_id is not None:
        query = query.join(Marker, Marker.id == PlaceEvent.place_id).filter(
            Marker.city_id == city_id
        )
    return (
        query.order_by(PlaceEvent.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )


def rollback_event(
    db: Session,
    *,
    event_id: int,
    admin: User,
    note: str = "",
) -> PlaceEvent:
    ev = db.query(PlaceEvent).filter(PlaceEvent.id == event_id).first()
    if ev is None:
        raise ValueError("이력을 찾을 수 없습니다")
    if not is_rollbackable(ev):
        raise ValueError("이 이력은 롤백할 수 없습니다 (이미 롤백됐거나 스냅샷 없음)")

    data = _payload(ev)
    action = ev.action
    place_id = ev.place_id
    detail = ""

    if action == PlaceEventAction.merge:
        place_id, detail = _rollback_merge(db, data)
    elif action == PlaceEventAction.update:
        place_id, detail = _rollback_update(db, data, place_id)
    elif action == PlaceEventAction.context_update:
        place_id, detail = _rollback_context(db, data, place_id)
    elif action == PlaceEventAction.agent_create:
        place_id, detail = _rollback_agent_create(db, data, place_id)
    elif action == PlaceEventAction.image_reorder:
        place_id, detail = _rollback_image_reorder(db, data, place_id)
    else:
        raise ValueError("지원하지 않는 롤백 유형입니다")

    data["rolled_back"] = True
    data["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
    data["rolled_back_by"] = admin.id
    _save_payload(ev, data)

    rb = log_place_event(
        db,
        place_id=place_id,
        user=admin,
        action=PlaceEventAction.rollback,
        summary=f"관리자 롤백: {ev.summary}"[:500],
        payload={
            "rolled_back_event_id": ev.id,
            "original_action": action.value,
            "detail": detail,
            "admin_note": (note or "").strip()[:1000],
            "lesson": (
                f"이전 에이전트 조치({action.value})가 관리자에 의해 취소됨. "
                "같은 방식의 병합/수정/추가를 반복하지 말 것."
            ),
        },
        actor="user",
    )
    # 롤백은 에이전트가 다음 주기에 꼭 읽도록 미읽음 유지 (groq_read_at=None)
    db.commit()
    db.refresh(rb)
    return rb


def _rollback_merge(db: Session, data: dict[str, Any]) -> tuple[Optional[int], str]:
    source_id = int(data["source_id"])
    target_id = int(data["target_id"])
    source = db.query(Marker).filter(Marker.id == source_id).first()
    target = db.query(Marker).filter(Marker.id == target_id).first()
    if not source or not target:
        raise ValueError("병합 대상 장소를 찾을 수 없습니다")

    before_target = data.get("before", {}).get("target")
    if before_target:
        apply_marker_snapshot(target, before_target)

    # 이미지 원복
    moved = data.get("moved_image_ids") or []
    if moved:
        for img in db.query(PlaceImage).filter(PlaceImage.id.in_(moved)).all():
            img.place_id = source_id
            prev = (data.get("before", {}).get("source_images") or {}).get(str(img.id))
            if prev is not None:
                img.sort_order = int(prev.get("sort_order", img.sort_order))
                img.group_key = prev.get("group_key")

    source.merged_into_id = None
    return target_id, f"병합 해제 #{source_id} ← #{target_id}"


def _rollback_update(
    db: Session, data: dict[str, Any], place_id: Optional[int]
) -> tuple[Optional[int], str]:
    before = data.get("before") or {}
    pid = int(place_id or before.get("place_id") or 0)
    m = db.query(Marker).filter(Marker.id == pid, Marker.merged_into_id.is_(None)).first()
    if not m:
        raise ValueError("장소를 찾을 수 없습니다")
    apply_marker_snapshot(m, before)
    return pid, "필드 복원"


def _rollback_context(
    db: Session, data: dict[str, Any], place_id: Optional[int]
) -> tuple[Optional[int], str]:
    before = data.get("before") or {}
    pid = int(place_id or 0)
    m = db.query(Marker).filter(Marker.id == pid, Marker.merged_into_id.is_(None)).first()
    if not m:
        raise ValueError("장소를 찾을 수 없습니다")
    if "agent_context" in before:
        m.agent_context = str(before["agent_context"])[:8000]
    return pid, "컨텍스트 복원"


def _rollback_agent_create(
    db: Session, data: dict[str, Any], place_id: Optional[int]
) -> tuple[Optional[int], str]:
    pid = int(data.get("place_id") or place_id or 0)
    m = db.query(Marker).filter(Marker.id == pid).first()
    if not m:
        raise ValueError("추천 장소를 찾을 수 없습니다 (이미 삭제됨)")
    title = m.title
    db.delete(m)
    return None, f"추천 장소 삭제: {title}"


def _rollback_image_reorder(
    db: Session, data: dict[str, Any], place_id: Optional[int]
) -> tuple[Optional[int], str]:
    pid = int(place_id or 0)
    before_orders = data.get("before", {}).get("image_orders") or {}
    if not before_orders:
        raise ValueError("이전 이미지 순서 스냅샷이 없습니다")
    images = db.query(PlaceImage).filter(PlaceImage.place_id == pid).all()
    by_id = {i.id: i for i in images}
    for sid, meta in before_orders.items():
        iid = int(sid)
        if iid in by_id:
            by_id[iid].sort_order = int(meta.get("sort_order", 0))
            if "group_key" in meta:
                by_id[iid].group_key = meta.get("group_key")
    return pid, "이미지 순서 복원"
