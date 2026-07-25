"""Groq tool-calling용 도구 정의 + 실행."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from app.events import ensure_contributor, log_place_event, mark_events_read
from app.models import Marker, MarkerCategory, MarkerShape, PlaceEvent, PlaceEventAction, PlaceImage

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_unread_events",
            "description": "Groq가 아직 읽지 않은 장소 이력 이벤트를 최신순으로 가져온다.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_events_read",
            "description": "처리 완료한 이벤트 ID들을 읽음 처리한다.",
            "parameters": {
                "type": "object",
                "properties": {"event_ids": {"type": "array", "items": {"type": "integer"}}},
                "required": ["event_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_places",
            "description": "활성 장소 목록(병합되지 않은 것만).",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 80}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_place",
            "description": "장소 상세 + 이미지 + 최근 이벤트.",
            "parameters": {
                "type": "object",
                "properties": {"place_id": {"type": "integer"}},
                "required": ["place_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_nearby_candidates",
            "description": "같은 장소로 의심되는 가까운 핀들을 찾는다 (미터 단위).",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "radius_m": {"type": "number", "default": 80},
                },
                "required": ["place_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_places",
            "description": "source_place_id를 target_place_id로 병합한다. 설명/기여자/이미지를 합친다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_place_id": {"type": "integer"},
                    "source_place_id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["target_place_id", "source_place_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_place_context",
            "description": "장소의 내부 컨텍스트(agent_context)를 갱신한다. 사용자 description과 별개.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "context": {"type": "string"},
                },
                "required": ["place_id", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_place_fields",
            "description": "장소 제목/설명/카테고리를 정리한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["place_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_place",
            "description": "꼭 필요해 보이는 장소를 에이전트가 추가한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                    "lat": {"type": "number"},
                    "lng": {"type": "number"},
                    "context": {"type": "string"},
                },
                "required": ["title", "lat", "lng"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reorder_images",
            "description": "장소 이미지 표시 순서와 그룹 키를 조정한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "ordered_ids": {"type": "array", "items": {"type": "integer"}},
                    "group_keys": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "image_id(string) -> group_key",
                    },
                },
                "required": ["place_id", "ordered_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "웹에서 장소 관련 정보를 간단히 검색한다 (DuckDuckGo).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _place_brief(m: Marker) -> dict[str, Any]:
    return {
        "id": m.id,
        "title": m.title,
        "category": m.category.value if m.category else None,
        "shape": m.shape.value if m.shape else None,
        "lat": m.lat,
        "lng": m.lng,
        "description": (m.description or "")[:400],
        "agent_context": (m.agent_context or "")[:800],
        "merged_into_id": m.merged_into_id,
        "is_agent_suggested": m.is_agent_suggested,
    }


def run_tool(db: Session, name: str, args: dict[str, Any]) -> Any:
    if name == "list_unread_events":
        limit = int(args.get("limit") or 30)
        rows = (
            db.query(PlaceEvent)
            .filter(PlaceEvent.groq_read_at.is_(None))
            .order_by(PlaceEvent.created_at.asc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        return [
            {
                "id": e.id,
                "place_id": e.place_id,
                "user_id": e.user_id,
                "actor": e.actor,
                "action": e.action.value,
                "summary": e.summary,
                "payload": json.loads(e.payload or "{}"),
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]

    if name == "mark_events_read":
        ids = [int(x) for x in (args.get("event_ids") or [])]
        n = mark_events_read(db, ids)
        db.commit()
        return {"marked": n}

    if name == "list_places":
        limit = int(args.get("limit") or 80)
        rows = (
            db.query(Marker)
            .filter(Marker.merged_into_id.is_(None))
            .order_by(Marker.updated_at.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return [_place_brief(m) for m in rows]

    if name == "get_place":
        pid = int(args["place_id"])
        m = (
            db.query(Marker)
            .options(joinedload(Marker.images), joinedload(Marker.contributors))
            .filter(Marker.id == pid)
            .first()
        )
        if not m:
            return {"error": "not_found"}
        events = (
            db.query(PlaceEvent)
            .filter(PlaceEvent.place_id == pid)
            .order_by(PlaceEvent.created_at.desc())
            .limit(20)
            .all()
        )
        brief = _place_brief(m)
        brief["images"] = [
            {"id": i.id, "sort_order": i.sort_order, "group_key": i.group_key, "s3_key": i.s3_key}
            for i in sorted(m.images, key=lambda x: x.sort_order)
        ]
        brief["contributor_user_ids"] = [c.user_id for c in m.contributors]
        brief["recent_events"] = [
            {"id": e.id, "action": e.action.value, "summary": e.summary, "groq_read": e.groq_read_at is not None}
            for e in events
        ]
        return brief

    if name == "find_nearby_candidates":
        pid = int(args["place_id"])
        radius = float(args.get("radius_m") or 80)
        m = db.query(Marker).filter(Marker.id == pid, Marker.merged_into_id.is_(None)).first()
        if not m:
            return {"error": "not_found"}
        others = db.query(Marker).filter(Marker.merged_into_id.is_(None), Marker.id != pid).all()
        hits = []
        for o in others:
            dist = _haversine_m(m.lat, m.lng, o.lat, o.lng)
            if dist <= radius:
                hits.append({**_place_brief(o), "distance_m": round(dist, 1)})
        hits.sort(key=lambda x: x["distance_m"])
        return hits

    if name == "merge_places":
        target_id = int(args["target_place_id"])
        source_id = int(args["source_place_id"])
        reason = str(args.get("reason") or "same place")
        if target_id == source_id:
            return {"error": "same_id"}
        target = db.query(Marker).filter(Marker.id == target_id, Marker.merged_into_id.is_(None)).first()
        source = db.query(Marker).filter(Marker.id == source_id, Marker.merged_into_id.is_(None)).first()
        if not target or not source:
            return {"error": "not_found"}
        # merge text
        chunks = [target.description or ""]
        if source.description and source.description not in (target.description or ""):
            chunks.append(f"[병합:{source.title}] {source.description}")
        target.description = "\n\n".join(c for c in chunks if c).strip()[:2000]
        if source.agent_context:
            target.agent_context = ((target.agent_context or "") + "\n" + source.agent_context).strip()[:8000]
        for c in list(source.contributors):
            ensure_contributor(db, target.id, c.user_id)
        for img in list(source.images):
            img.place_id = target.id
            img.sort_order = 1000 + img.sort_order
        source.merged_into_id = target.id
        log_place_event(
            db,
            place_id=target.id,
            user=None,
            action=PlaceEventAction.merge,
            summary=f"병합: #{source.id} → #{target.id} ({reason})",
            payload={"source_id": source_id, "target_id": target_id, "reason": reason},
            actor="agent",
        )
        db.commit()
        return {"ok": True, "target_id": target_id, "source_id": source_id}

    if name == "update_place_context":
        pid = int(args["place_id"])
        ctx = str(args.get("context") or "")[:8000]
        m = db.query(Marker).filter(Marker.id == pid, Marker.merged_into_id.is_(None)).first()
        if not m:
            return {"error": "not_found"}
        m.agent_context = ctx
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.context_update,
            summary="에이전트 컨텍스트 갱신",
            payload={"chars": len(ctx)},
            actor="agent",
        )
        db.commit()
        return {"ok": True}

    if name == "update_place_fields":
        pid = int(args["place_id"])
        m = db.query(Marker).filter(Marker.id == pid, Marker.merged_into_id.is_(None)).first()
        if not m:
            return {"error": "not_found"}
        changed = {}
        if args.get("title"):
            m.title = str(args["title"])[:200]
            changed["title"] = m.title
        if args.get("description") is not None:
            m.description = str(args["description"])[:2000]
            changed["description"] = True
        if args.get("category"):
            try:
                m.category = MarkerCategory(str(args["category"]))
                changed["category"] = m.category.value
            except ValueError:
                pass
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.update,
            summary="에이전트 필드 정리",
            payload=changed,
            actor="agent",
        )
        db.commit()
        return {"ok": True, "changed": changed}

    if name == "create_place":
        title = str(args.get("title") or "추천 장소")[:200]
        cat_raw = str(args.get("category") or "other")
        try:
            cat = MarkerCategory(cat_raw)
        except ValueError:
            cat = MarkerCategory.other
        m = Marker(
            user_id=None,
            category=cat,
            shape=MarkerShape.point,
            title=title,
            description=str(args.get("description") or "")[:2000],
            lat=float(args["lat"]),
            lng=float(args["lng"]),
            agent_context=str(args.get("context") or "")[:8000],
            is_agent_suggested=True,
        )
        db.add(m)
        db.flush()
        log_place_event(
            db,
            place_id=m.id,
            user=None,
            action=PlaceEventAction.agent_create,
            summary=f"에이전트 장소 추가: {title}",
            payload={"lat": m.lat, "lng": m.lng},
            actor="agent",
        )
        db.commit()
        return {"ok": True, "place_id": m.id}

    if name == "reorder_images":
        pid = int(args["place_id"])
        ordered = [int(x) for x in (args.get("ordered_ids") or [])]
        groups = args.get("group_keys") or {}
        images = db.query(PlaceImage).filter(PlaceImage.place_id == pid).all()
        by_id = {i.id: i for i in images}
        for idx, iid in enumerate(ordered):
            if iid in by_id:
                by_id[iid].sort_order = idx
                gk = groups.get(str(iid)) or groups.get(iid)
                if gk is not None:
                    by_id[iid].group_key = str(gk)[:100]
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.image_reorder,
            summary="에이전트 이미지 순서 조정",
            payload={"ordered_ids": ordered},
            actor="agent",
        )
        db.commit()
        return {"ok": True}

    if name == "web_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"results": []}
        try:
            from duckduckgo_search import DDGS

            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
            return {
                "results": [
                    {"title": r.get("title"), "href": r.get("href"), "body": (r.get("body") or "")[:300]}
                    for r in results
                ]
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "results": []}

    return {"error": f"unknown_tool:{name}"}
