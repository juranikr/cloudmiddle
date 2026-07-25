"""Groq tool-calling용 도구 정의 + 실행."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from app.events import ensure_contributor, log_place_event, mark_events_read
from app.messages import (
    list_open_appeals,
    mark_appeals_read,
    notify_all_users,
    notify_place_contributors,
)
from app.models import (
    Marker,
    MarkerCategory,
    MarkerShape,
    PlaceAppeal,
    PlaceAppealStatus,
    PlaceEvent,
    PlaceEventAction,
    PlaceImage,
    UserMessageKind,
)

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
            "description": (
                "기존 기록을 최대한 보존하며 보완한다. "
                "설명은 append_note로만 추가. 제목은 local_name(현지 명칭)을 병기하거나, "
                "정말 필요할 때만 replace_title로 교체."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "append_note": {"type": "string", "description": "설명 끝에 덧붙일 한국어 보완 정보"},
                    "local_name": {"type": "string", "description": "현지(중국어 등) 공식 명칭·주소 병기"},
                    "replace_title": {"type": "string", "description": "기존 제목을 꼭 바꿔야 할 때만"},
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
            "description": "꼭 필요해 보이는 장소를 에이전트가 추가한다. 제목에 현지 명칭을 함께 넣는다.",
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
            "name": "list_open_appeals",
            "description": "사용자가 낸 미처리 이의신청 목록. 다음 주기 재고려 대상.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_appeal",
            "description": (
                "이의신청을 검토한 뒤 반영/기각하고 신청자에게 결과 메시지를 보낸다. "
                "잘못된 병합이면 설명을 보완하거나 별도 장소를 create_place로 복원하는 식으로 조치."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appeal_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["resolved", "dismissed"]},
                    "agent_note": {"type": "string", "description": "한국어로 조치 설명"},
                },
                "required": ["appeal_id", "status", "agent_note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_appeals_read",
            "description": "검토를 마친 이의신청 ID를 읽음 처리한다.",
            "parameters": {
                "type": "object",
                "properties": {"appeal_ids": {"type": "array", "items": {"type": "integer"}}},
                "required": ["appeal_ids"],
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
        source_title = source.title
        # 기존 기록 보존: 설명·제목 정보를 덧붙임
        chunks = [target.description or ""]
        if source.description and source.description not in (target.description or ""):
            chunks.append(f"[병합 보존:{source.title}] {source.description}")
        elif source.title and source.title not in (target.title or ""):
            chunks.append(f"[병합 보존 별칭] {source.title}")
        target.description = "\n\n".join(c for c in chunks if c).strip()[:2000]
        if source.title and source.title not in target.title:
            combined = f"{target.title} / {source.title}"
            target.title = combined[:200]
        if source.agent_context:
            target.agent_context = ((target.agent_context or "") + "\n" + source.agent_context).strip()[:8000]
        for c in list(source.contributors):
            ensure_contributor(db, target.id, c.user_id)
        if source.user_id:
            ensure_contributor(db, target.id, source.user_id)
        for img in list(source.images):
            img.place_id = target.id
            img.sort_order = 1000 + img.sort_order
        source.merged_into_id = target.id
        ev = log_place_event(
            db,
            place_id=target.id,
            user=None,
            action=PlaceEventAction.merge,
            summary=f"병합: #{source.id} → #{target.id} ({reason})",
            payload={"source_id": source_id, "target_id": target_id, "reason": reason},
            actor="agent",
        )
        db.flush()
        notify_place_contributors(
            db,
            place_ids=[target_id, source_id],
            kind=UserMessageKind.agent_merge,
            title=f"장소가 병합되었습니다: {target.title}",
            body=(
                f"에이전트가 「{source_title}」(#{source_id})를 "
                f"「{target.title}」(#{target_id})로 합쳤습니다.\n"
                f"사유: {reason}\n\n"
                "잘못되었다고 생각되면 메시지에서 이의신청을 남겨 주세요. "
                "다음 새벽 정리 주기에 다시 검토합니다."
            ),
            place_id=target_id,
            related_event_id=ev.id,
        )
        db.commit()
        return {"ok": True, "target_id": target_id, "source_id": source_id}

    if name == "update_place_context":
        pid = int(args["place_id"])
        ctx = str(args.get("context") or "")[:8000]
        m = db.query(Marker).filter(Marker.id == pid, Marker.merged_into_id.is_(None)).first()
        if not m:
            return {"error": "not_found"}
        # 덮어쓰기보다 병합 선호
        if m.agent_context and ctx and ctx not in m.agent_context:
            m.agent_context = (m.agent_context.rstrip() + "\n\n" + ctx).strip()[:8000]
        else:
            m.agent_context = ctx or m.agent_context
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.context_update,
            summary="에이전트 컨텍스트 보완",
            payload={"chars": len(m.agent_context or "")},
            actor="agent",
        )
        db.commit()
        return {"ok": True}

    if name == "update_place_fields":
        pid = int(args["place_id"])
        m = db.query(Marker).filter(Marker.id == pid, Marker.merged_into_id.is_(None)).first()
        if not m:
            return {"error": "not_found"}
        changed: dict[str, Any] = {}
        local_name = str(args.get("local_name") or "").strip()
        if local_name and local_name not in m.title:
            m.title = f"{m.title} ({local_name})"[:200]
            changed["local_name"] = local_name
        replace_title = str(args.get("replace_title") or "").strip()
        if replace_title:
            # 기존 제목은 설명에 보존
            if m.title and m.title not in (m.description or ""):
                note = f"[이전 제목 보존] {m.title}"
                m.description = ((m.description or "") + "\n" + note).strip()[:2000]
            m.title = replace_title[:200]
            changed["replace_title"] = m.title
        append_note = str(args.get("append_note") or "").strip()
        if append_note:
            if append_note not in (m.description or ""):
                m.description = ((m.description or "").rstrip() + "\n\n" + append_note).strip()[:2000]
            changed["append_note"] = True
        if args.get("category"):
            try:
                m.category = MarkerCategory(str(args["category"]))
                changed["category"] = m.category.value
            except ValueError:
                pass
        if not changed:
            return {"ok": True, "changed": {}}
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.update,
            summary="에이전트 정보 보완",
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
        ev = log_place_event(
            db,
            place_id=m.id,
            user=None,
            action=PlaceEventAction.agent_create,
            summary=f"에이전트 장소 추가: {title}",
            payload={"lat": m.lat, "lng": m.lng},
            actor="agent",
        )
        db.flush()
        notify_all_users(
            db,
            kind=UserMessageKind.agent_create,
            title=f"새 추천 장소: {title}",
            body=(
                f"에이전트가 장소를 추가했습니다.\n"
                f"이름: {title}\n좌표: {m.lat:.5f}, {m.lng:.5f}\n\n"
                "필요 없거나 잘못되었으면 해당 장소 상세 또는 이 메시지에서 이의신청을 남겨 주세요. "
                "다음 새벽 정리 주기에 다시 검토합니다."
            ),
            place_id=m.id,
            related_event_id=ev.id,
        )
        db.commit()
        return {"ok": True, "place_id": m.id}

    if name == "list_open_appeals":
        limit = int(args.get("limit") or 30)
        rows = list_open_appeals(db, limit=limit)
        return [
            {
                "id": a.id,
                "place_id": a.place_id,
                "user_id": a.user_id,
                "body": a.body,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ]

    if name == "resolve_appeal":
        aid = int(args["appeal_id"])
        status_raw = str(args.get("status") or "resolved")
        note = str(args.get("agent_note") or "").strip()[:2000]
        try:
            status = PlaceAppealStatus(status_raw)
        except ValueError:
            return {"error": "bad_status"}
        if status not in (PlaceAppealStatus.resolved, PlaceAppealStatus.dismissed):
            return {"error": "bad_status"}
        appeal = db.query(PlaceAppeal).filter(PlaceAppeal.id == aid).first()
        if not appeal:
            return {"error": "not_found"}
        appeal.status = status
        appeal.agent_note = note
        appeal.resolved_at = datetime.now(timezone.utc)
        if appeal.groq_read_at is None:
            appeal.groq_read_at = appeal.resolved_at
        label = "반영" if status == PlaceAppealStatus.resolved else "기각"
        from app.messages import contributor_user_ids, notify_users

        recipients = contributor_user_ids(db, [appeal.place_id])
        recipients.add(appeal.user_id)
        notify_users(
            db,
            user_ids=recipients,
            kind=UserMessageKind.appeal_result,
            title=f"이의신청 {label} (장소 #{appeal.place_id})",
            body=f"이의 내용: {appeal.body[:500]}\n\n에이전트 조치: {note}",
            place_id=appeal.place_id,
        )
        db.commit()
        return {"ok": True, "status": status.value}

    if name == "mark_appeals_read":
        ids = [int(x) for x in (args.get("appeal_ids") or [])]
        n = mark_appeals_read(db, ids)
        db.commit()
        return {"marked": n}

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
