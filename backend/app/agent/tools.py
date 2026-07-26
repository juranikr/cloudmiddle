"""Groq tool-calling용 도구 정의 + 실행."""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from app import storage

from app.events import (
    diff_marker_fields,
    ensure_contributor,
    log_place_event,
    mark_events_read,
    marker_field_snapshot,
    summary_for_changes,
)
from app.geocode import search_address
from app.knowledge import list_knowledge, upsert_knowledge
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
from app.rollback import marker_snapshot

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_unread_events",
            "description": (
                "아직 처리하지 않은 장소 이력(생성·수정·병합·이의 기록 등)을 오래된 순으로 가져온다. "
                "작업 큐의 핵심. 반환된 id를 빠짐없이 검토·조치한 뒤 mark_events_read 할 것."
            ),
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
            "description": (
                "검토·조치를 끝낸 이벤트 ID만 읽음 처리한다. "
                "아직 안 본 ID를 한꺼번에 표시하지 말 것. 잔여 미읽음이 0이 될 때까지 반복."
            ),
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
            "description": (
                "활성 장소(병합되지 않은 것)를 조건으로 조회한다. "
                "병합 후보를 찾을 때 미읽음/이의 대상뿐 아니라 수정되지 않은 기존 장소까지 "
                "이 툴로 전체 지도에서 쿼리한다. "
                "q·category·near_lat/near_lng/radius_m를 조합해 필요한 범위만 가져올 것. "
                "동명 중복 탐색은 q(이름 부분일치)로, 한글/한자 표기가 다르면 "
                "전체 목록을 가져와 직접 비교한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": "제목·설명·agent_context 부분 일치 검색(한국어/중국어/영문)",
                    },
                    "category": {
                        "type": "string",
                        "description": "tourist|lodging|restaurant|transport|shopping|drink|convenience|other",
                    },
                    "near_lat": {"type": "number", "description": "이 좌표 근처만 (WGS84)"},
                    "near_lng": {"type": "number", "description": "이 좌표 근처만 (WGS84)"},
                    "radius_m": {
                        "type": "number",
                        "default": 150,
                        "description": "near_lat/lng와 함께 쓸 반경(미터). 기본 150",
                    },
                    "exclude_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "결과에서 제외할 place_id (자기 자신 등)",
                    },
                    "limit": {"type": "integer", "default": 80},
                },
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
            "description": (
                "기준 place_id 주변의 활성 장소를 거리순으로 반환한다. "
                "수정/이의가 없는 기존 핀도 모두 포함되므로, 병합 후보 탐색의 1순위 툴이다. "
                "거리는 참고 신호일 뿐이다. 산·공원·호수 등 넓은 명소는 radius_m을 "
                "1000~5000까지 넓혀서 검색하고, 이름이 같으면 거리가 멀어도 병합 후보다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer", "description": "미읽음/이의로 주목 중인 기준 장소"},
                    "radius_m": {
                        "type": "number",
                        "default": 120,
                        "description": "미터. 넓은 명소는 1000~5000 권장. 고정 기준 아님",
                    },
                },
                "required": ["place_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_places",
            "description": (
                "source_place_id를 target_place_id로 병합한다. 설명/기여자/이미지를 합친다. "
                "같은 실체라는 근거(동일·동의 명칭, 이의제기 주장, 웹 확인)가 있으면 "
                "거리가 150m를 넘어도 병합한다. 정보가 풍부한 쪽을 target으로."
            ),
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
            "description": "웹 조사·지오코딩 후 지도에 없는 유용한 지난 장소를 추천 추가한다. 매 사이클 소수를 적극 등록. 제목에 현지 명칭 병기.",
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
            "description": (
                "미종결(open) 이의신청 목록. 각 id마다 resolve_appeal로 반드시 종결해야 한다. "
                "읽음 표시만으로 넘기면 안 된다."
            ),
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
                "이의신청을 검토한 뒤 반영(resolved)/기각(dismissed)하고 신청자에게 결과 메시지를 보낸다. "
                "open 이의를 끝내는 유일한 정상 경로. "
                "잘못된 병합이면 설명 보완·별도 create_place 등으로 조치한 뒤 호출."
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
            "description": (
                "이미 resolve_appeal로 종결된 이의만 읽음 처리할 때 사용. "
                "status=open 이의 ID는 거부된다 — 반드시 resolve_appeal을 먼저 호출."
            ),
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
            "name": "list_recent_rollbacks",
            "description": (
                "관리자가 에이전트 조치를 롤백한 미읽음 이력. "
                "같은 실수를 반복하지 않도록 반드시 확인한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "list_knowledge",
            "description": "【필수·시작】에이전트 장기 지식/교훈. 다른 툴보다 먼저 호출한다.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_knowledge",
            "description": (
                "【필수】교훈·조사·병합 정책을 주제(topic)별로 저장/병합한다. "
                "매 사이클 종료 전 최소 1회 호출. 기존 content와 모순되면 완성본으로 재작성."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "영문/숫자 slug, 예: appeal_lessons, jinan_food"},
                    "title": {"type": "string"},
                    "content": {"type": "string", "description": "한국어로 정리된 지식 본문"},
                    "place_id": {
                        "type": ["integer", "null"],
                        "description": "특정 장소와 연관된 지식일 때만. 없으면 생략하거나 null",
                    },
                    "merge": {"type": "boolean", "default": True},
                },
                "required": ["topic", "title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "geocode_place",
            "description": "지난(济南) 중심 주소/장소명 지오코딩. create_place 전에 사용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
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
    {
        "type": "function",
        "function": {
            "name": "search_place_images",
            "description": (
                "위키미디어 커먼즈에서 자유 라이선스 장소 사진을 검색한다. "
                "image_count가 0인 장소의 사진 보강용. "
                "중국어 명칭(예: 千佛山, 大明湖)으로 검색하면 결과가 좋다. "
                "결과의 image_url을 attach_image_from_url에 넘겨 업로드한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "장소 명칭 (중국어 권장)"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attach_image_from_url",
            "description": (
                "이미지 URL을 다운로드해 해당 장소의 사진으로 업로드한다(S3). "
                "search_place_images 결과의 image_url만 사용할 것(자유 라이선스 보장). "
                "source에는 출처 페이지와 라이선스를 기록한다. 장소당 1~2장이면 충분."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "image_url": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": "출처·라이선스 메모 (예: Wikimedia Commons, CC BY-SA 4.0, page_url)",
                    },
                },
                "required": ["place_id", "image_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stale_places",
            "description": (
                "추가/수정/재검증된 지 오래된(기본 30일 이상) 활성 장소를 오래된 순으로 반환한다. "
                "이 장소들은 폐업·이전 가능성이 있으므로 web_search로 유효성을 재확인하고 "
                "결과를 verify_place로 기록할 것. 사이클당 3~5곳이면 충분하다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30, "description": "이 일수 이상 미확인 장소만"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_place",
            "description": (
                "장소 재검증 결과를 기록한다(last_verified_at 갱신 → 재검증 목록에서 제외). "
                "status: valid(영업·존재 확인) | closed(폐업·소멸 추정) | moved(같은 지점의 이전 확인) "
                "| uncertain(판단 불가). "
                "주의 — moved로 판정하기 전에 반드시 한 번 더 검토: 웹에서 찾은 다른 주소가 "
                "'같은 지점의 이전(搬迁)'인지 '다른 지점(분점)'인지 구분할 것. "
                "체인점이면 지점명·구(区)·도로명을 대조하고, 다른 지점이면 moved가 아니라 "
                "valid + note로 기록하고 좌표를 옮기지 말 것. "
                "같은 지점의 이전이 확실할 때만 geocode_place→update_place_fields로 좌표를 "
                "갱신한 뒤 moved로 기록한다. closed는 삭제하지 말고 기록만 남긴다. "
                "closed/moved/uncertain의 note는 agent_context에 자동 병합된다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["valid", "closed", "moved", "uncertain"],
                    },
                    "note": {
                        "type": "string",
                        "description": "판단 근거·출처. closed/moved/uncertain은 필수 권장",
                    },
                },
                "required": ["place_id", "status"],
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
        "image_count": len(m.images or []),
    }


_WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
_IMAGE_UA = "JinanTravelMap/0.1 (shared travel map; image enrichment)"
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_IMAGE_MAX_BYTES = 5 * 1024 * 1024


def _wikimedia_image_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": max(1, min(limit, 10)),
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": 1280,
        }
    )
    req = urllib.request.Request(
        f"{_WIKIMEDIA_API}?{params}", headers={"User-Agent": _IMAGE_UA}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out: list[dict[str, Any]] = []
    for page in (data.get("query", {}).get("pages") or {}).values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        ii = infos[0]
        thumb = ii.get("thumburl") or ii.get("url") or ""
        if not str(thumb).lower().rsplit("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        meta = ii.get("extmetadata") or {}
        license_name = ((meta.get("LicenseShortName") or {}).get("value") or "")[:100]
        out.append(
            {
                "title": page.get("title", ""),
                "image_url": thumb,
                "width": ii.get("thumbwidth") or ii.get("width"),
                "height": ii.get("thumbheight") or ii.get("height"),
                "license": license_name,
                "page_url": ii.get("descriptionurl") or "",
            }
        )
    return out


def run_tool(db: Session, name: str, args: dict[str, Any]) -> Any:
    if name == "list_unread_events":
        limit = int(args.get("limit") or 30)
        rows = (
            db.query(PlaceEvent)
            .filter(PlaceEvent.groq_read_at.is_(None), PlaceEvent.actor != "agent")
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
        q = str(args.get("q") or "").strip()
        cat_raw = str(args.get("category") or "").strip()
        exclude = args.get("exclude_ids") or []
        try:
            exclude_ids = {int(x) for x in exclude}
        except (TypeError, ValueError):
            exclude_ids = set()
        near_lat = args.get("near_lat")
        near_lng = args.get("near_lng")
        radius = float(args.get("radius_m") or 150)

        query = db.query(Marker).filter(Marker.merged_into_id.is_(None))
        if cat_raw:
            try:
                query = query.filter(Marker.category == MarkerCategory(cat_raw))
            except ValueError:
                pass
        if q:
            like = f"%{q}%"
            query = query.filter(
                (Marker.title.ilike(like))
                | (Marker.description.ilike(like))
                | (Marker.agent_context.ilike(like))
            )
        if exclude_ids:
            query = query.filter(~Marker.id.in_(exclude_ids))

        # Geo filter needs all candidates then haversine (dataset is small)
        if near_lat is not None and near_lng is not None:
            nlat, nlng = float(near_lat), float(near_lng)
            rows = query.all()
            scored = []
            for m in rows:
                dist = _haversine_m(nlat, nlng, m.lat, m.lng)
                if dist <= radius:
                    scored.append((dist, m))
            scored.sort(key=lambda x: x[0])
            return [
                {**_place_brief(m), "distance_m": round(dist, 1)}
                for dist, m in scored[: max(1, min(limit, 200))]
            ]

        rows = (
            query.order_by(Marker.updated_at.desc())
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
        radius = float(args.get("radius_m") or 120)
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
        before = {
            "target": marker_snapshot(target),
            "source": marker_snapshot(source),
            "source_images": {
                str(img.id): {"sort_order": img.sort_order, "group_key": img.group_key}
                for img in source.images
            },
        }
        moved_image_ids = [img.id for img in list(source.images)]
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
        contributor_ids: set[int] = {c.user_id for c in list(source.contributors)}
        if source.user_id:
            contributor_ids.add(source.user_id)
        for uid in sorted(contributor_ids):
            ensure_contributor(db, target.id, uid)
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
            payload={
                "source_id": source_id,
                "target_id": target_id,
                "reason": reason,
                "before": before,
                "moved_image_ids": moved_image_ids,
            },
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
        before = marker_field_snapshot(m)
        # 덮어쓰기보다 병합 선호
        if m.agent_context and ctx and ctx not in m.agent_context:
            m.agent_context = (m.agent_context.rstrip() + "\n\n" + ctx).strip()[:8000]
        else:
            m.agent_context = ctx or m.agent_context
        after = marker_field_snapshot(m)
        changes = diff_marker_fields(before, after, keys=["agent_context"])
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.context_update,
            summary=summary_for_changes("에이전트 컨텍스트 보완", changes),
            payload={
                "chars": len(m.agent_context or ""),
                "before": before,
                "after": after,
                "changes": changes,
                "fields": [c["field"] for c in changes],
            },
            actor="agent",
        )
        db.commit()
        return {"ok": True}

    if name == "update_place_fields":
        pid = int(args["place_id"])
        m = db.query(Marker).filter(Marker.id == pid, Marker.merged_into_id.is_(None)).first()
        if not m:
            return {"error": "not_found"}
        before = marker_field_snapshot(m)
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
        after = marker_field_snapshot(m)
        changes = diff_marker_fields(before, after)
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.update,
            summary=summary_for_changes("에이전트 정보 보완", changes),
            payload={
                **changed,
                "before": before,
                "after": after,
                "changes": changes,
                "fields": [c["field"] for c in changes],
            },
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
            payload={"lat": m.lat, "lng": m.lng, "place_id": m.id, "before": {}},
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
        if not ids:
            return {"marked": 0}
        open_ids = [
            a.id
            for a in db.query(PlaceAppeal)
            .filter(PlaceAppeal.id.in_(ids), PlaceAppeal.status == PlaceAppealStatus.open)
            .all()
        ]
        if open_ids:
            return {
                "error": "open_appeals_must_resolve",
                "open_ids": open_ids,
                "hint": "resolve_appeal(resolved|dismissed)로 종결한 뒤 읽음 처리하세요.",
            }
        n = mark_appeals_read(db, ids)
        db.commit()
        return {"marked": n}

    if name == "reorder_images":
        pid = int(args["place_id"])
        ordered = [int(x) for x in (args.get("ordered_ids") or [])]
        groups = args.get("group_keys") or {}
        images = db.query(PlaceImage).filter(PlaceImage.place_id == pid).all()
        before_orders = {
            str(i.id): {"sort_order": i.sort_order, "group_key": i.group_key} for i in images
        }
        by_id = {i.id: i for i in images}
        for idx, iid in enumerate(ordered):
            if iid in by_id:
                by_id[iid].sort_order = idx
                gk = groups.get(str(iid)) or groups.get(iid)
                if gk is not None:
                    by_id[iid].group_key = str(gk)[:100]
        before_ids = [i.id for i in sorted(images, key=lambda x: x.sort_order)]
        changes = [{"field": "image_ids", "before": before_ids, "after": ordered}]
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.image_reorder,
            summary="에이전트 이미지 순서 조정",
            payload={
                "ordered_ids": ordered,
                "before": {"image_orders": before_orders},
                "after": {"image_ids": ordered},
                "changes": changes,
                "fields": ["image_ids"],
            },
            actor="agent",
        )
        db.commit()
        return {"ok": True}

    if name == "list_recent_rollbacks":
        limit = int(args.get("limit") or 20)
        rows = (
            db.query(PlaceEvent)
            .filter(
                PlaceEvent.action == PlaceEventAction.rollback,
                PlaceEvent.groq_read_at.is_(None),
            )
            .order_by(PlaceEvent.created_at.desc())
            .limit(max(1, min(limit, 50)))
            .all()
        )
        return [
            {
                "id": e.id,
                "place_id": e.place_id,
                "summary": e.summary,
                "payload": json.loads(e.payload or "{}"),
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]


    if name == "list_knowledge":
        limit = int(args.get("limit") or 30)
        rows = list_knowledge(db, limit=limit)
        return [
            {
                "id": r.id,
                "topic": r.topic,
                "title": r.title,
                "content": r.content,
                "place_id": r.place_id,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    if name == "upsert_knowledge":
        row = upsert_knowledge(
            db,
            topic=str(args.get("topic") or "general"),
            title=str(args.get("title") or "교훈"),
            content=str(args.get("content") or ""),
            place_id=int(args["place_id"]) if args.get("place_id") is not None else None,
            merge=bool(args.get("merge", True)),
        )
        db.commit()
        return {"ok": True, "topic": row.topic, "id": row.id, "chars": len(row.content or "")}

    if name == "geocode_place":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"results": []}
        try:
            hits = search_address(query, limit=int(args.get("limit") or 5))
        except Exception as exc:
            return {"results": [], "error": str(exc)}
        return {"results": hits}

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

    if name == "search_place_images":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"results": []}
        try:
            return {"results": _wikimedia_image_search(query, limit=int(args.get("limit") or 5))}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:300], "results": []}

    if name == "attach_image_from_url":
        pid = int(args["place_id"])
        url = str(args.get("image_url") or "").strip()
        source = str(args.get("source") or "")[:500]
        if not url.lower().startswith("https://"):
            return {"error": "bad_url"}
        if not storage.s3_enabled():
            return {"error": "s3_disabled"}
        m = (
            db.query(Marker)
            .options(joinedload(Marker.images))
            .filter(Marker.id == pid, Marker.merged_into_id.is_(None))
            .first()
        )
        if not m:
            return {"error": "not_found"}
        if len(m.images) >= 8:
            return {"error": "too_many_images"}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _IMAGE_UA})
            with urllib.request.urlopen(req, timeout=25) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in _IMAGE_TYPES:
                    return {"error": f"unsupported_type:{ctype}"}
                data = resp.read(_IMAGE_MAX_BYTES + 1)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"download_failed: {exc}"[:300]}
        if len(data) > _IMAGE_MAX_BYTES:
            return {"error": "too_large"}
        if len(data) < 1024:
            return {"error": "too_small"}
        key = storage.build_object_key(pid, "web-image", ctype)
        try:
            storage.put_object_bytes(key, data, ctype)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"upload_failed: {exc}"[:300]}
        max_order = max([i.sort_order for i in m.images], default=-1)
        img = PlaceImage(
            place_id=pid,
            s3_key=key,
            content_type=ctype,
            sort_order=max_order + 1,
            uploaded_by_user_id=None,
        )
        db.add(img)
        db.flush()
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.image_add,
            summary=f"웹 이미지 추가: {m.title}",
            payload={
                "image_id": img.id,
                "s3_key": key,
                "source_url": url[:500],
                "source": source,
                "changes": [{"field": "image_id", "before": None, "after": img.id}],
                "fields": ["image_id"],
            },
            actor="agent",
        )
        db.commit()
        return {"ok": True, "image_id": img.id, "url": storage.public_url(key)}

    if name == "list_stale_places":
        days = max(7, int(args.get("days") or 30))
        limit = max(1, min(int(args.get("limit") or 10), 50))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        rows = db.query(Marker).filter(Marker.merged_into_id.is_(None)).all()
        stale: list[dict[str, Any]] = []
        for m in rows:
            candidates = [t for t in (m.updated_at, m.last_verified_at, m.created_at) if t]
            if not candidates:
                continue
            last_checked = max(
                t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in candidates
            )
            if last_checked >= cutoff:
                continue
            stale.append(
                {
                    **_place_brief(m),
                    "last_checked_at": last_checked.isoformat(),
                    "days_since_check": (now - last_checked).days,
                }
            )
        stale.sort(key=lambda x: x["days_since_check"], reverse=True)
        return stale[:limit]

    if name == "verify_place":
        pid = int(args["place_id"])
        status = str(args.get("status") or "").strip()
        note = str(args.get("note") or "").strip()[:1000]
        if status not in ("valid", "closed", "moved", "uncertain"):
            return {"error": "bad_status"}
        m = db.query(Marker).filter(Marker.id == pid, Marker.merged_into_id.is_(None)).first()
        if not m:
            return {"error": "not_found"}
        before = marker_field_snapshot(m)
        now = datetime.now(timezone.utc)
        m.last_verified_at = now
        tag = {
            "valid": "정보 유효",
            "closed": "폐업 추정",
            "moved": "이전 확인",
            "uncertain": "확인 필요",
        }[status]
        if status != "valid" and note:
            line = f"[재검증 {now.strftime('%Y-%m-%d')} · {tag}] {note}"
            if line not in (m.agent_context or ""):
                m.agent_context = ((m.agent_context or "").rstrip() + "\n\n" + line).strip()[
                    :8000
                ]
        after = marker_field_snapshot(m)
        changes = diff_marker_fields(before, after, keys=["agent_context"])
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.context_update,
            summary=f"재검증({tag}): {m.title}",
            payload={
                "verify_status": status,
                "note": note,
                "before": before,
                "after": after,
                "changes": changes,
                "fields": [c["field"] for c in changes],
            },
            actor="agent",
        )
        db.commit()
        return {"ok": True, "status": status, "last_verified_at": now.isoformat()}

    return {"error": f"unknown_tool:{name}"}
