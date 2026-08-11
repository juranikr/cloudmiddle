import json
import re
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.agent.tools import TOOLS, run_tool
from app.config import settings
from app.models import City, Marker, MarkerShape, TravelChatMessage, TravelPlanItem


READ_TOOLS = {"list_places", "list_zones", "web_search", "fetch_page", "geocode_place"}
WRITE_TOOLS = {"propose_place", "upsert_place_insights"}
URL_RE = re.compile(r"https?://[^\s<>\])}]+")
PLACE_ID_RE = re.compile(r"(?:장소|place)[_ ]?(?:id)?\s*[:#]?\s*(\d+)", re.IGNORECASE)


def _tool_subset(names: set[str]) -> list[dict[str, Any]]:
    return [tool for tool in TOOLS if tool.get("function", {}).get("name") in names]


def _marker_context(db: Session, city_id: int) -> tuple[list[Marker], str]:
    rows = (
        db.query(Marker)
        .options(joinedload(Marker.insights), joinedload(Marker.images), joinedload(Marker.zone))
        .filter(
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
            Marker.shape == MarkerShape.point,
        )
        .order_by(Marker.travel_role.asc(), Marker.title.asc())
        .limit(180)
        .all()
    )
    compact = [
        {
            "id": row.id,
            "title": row.title,
            "category": row.category.value,
            "travel_role": row.travel_role or "general",
            "zone": row.zone.title if row.zone else "",
            "description": (row.description or "")[:280],
            "insights": [
                {"kind": item.kind, "title": item.title, "content": (item.content or "")[:220]}
                for item in (row.insights or [])[:5]
            ],
            "image_count": len(row.images or []),
        }
        for row in rows
    ]
    return rows, json.dumps(compact, ensure_ascii=False)


def _plan_context(db: Session, *, user_id: int, city_id: int) -> str:
    rows = (
        db.query(TravelPlanItem)
        .options(joinedload(TravelPlanItem.place))
        .filter(TravelPlanItem.user_id == user_id, TravelPlanItem.city_id == city_id)
        .order_by(TravelPlanItem.day, TravelPlanItem.sort_order, TravelPlanItem.id)
        .all()
    )
    return json.dumps(
        [
            {
                "day": row.day,
                "slot": row.slot,
                "place_id": row.place_id,
                "title": row.place.title if row.place else "",
                "note": row.note,
            }
            for row in rows
        ],
        ensure_ascii=False,
    )


def _collect_sources(value: Any, out: set[str]) -> None:
    if isinstance(value, str):
        out.update(URL_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            _collect_sources(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_sources(item, out)


def answer_travel_chat(
    db: Session,
    *,
    user_id: int,
    city_id: int,
    message: str,
    selected_place_id: int | None = None,
) -> dict[str, Any]:
    city = db.query(City).filter(City.id == city_id, City.status == "active").first()
    if city is None:
        raise ValueError("활성 도시를 찾을 수 없습니다")
    if not settings.groq_api_key:
        raise RuntimeError("여행 에이전트 모델이 설정되지 않았습니다")

    markers, map_context = _marker_context(db, city_id)
    existing_ids = {row.id for row in markers}
    write_intent = bool(re.search(r"(지도|장소).{0,12}(넣|추가|저장|등록|보강)|정보.{0,8}(추가|갱신)", message))
    allowed = set(READ_TOOLS)
    if write_intent:
        allowed |= WRITE_TOOLS

    prior = (
        db.query(TravelChatMessage)
        .filter(TravelChatMessage.user_id == user_id, TravelChatMessage.city_id == city_id)
        .order_by(TravelChatMessage.id.desc())
        .limit(8)
        .all()
    )
    prior.reverse()
    system = f"""당신은 WONRAE(遠來)의 {city.name_ko}({city.name_local}) 여행 설계자다.
목표는 많이 나열하는 것이 아니라 실제 이틀 여행에서 좋은 선택과 동선을 주는 것이다.

규칙:
- 답은 자연스러운 한국어로, 먼저 결론을 짧게 말한다.
- 아래 운영 지도 DB를 최우선 사실로 사용하고, 언급한 등록 장소에는 반드시 `장소 #ID`를 붙인다.
- 저장 장소의 위치·구역·역사·방문 팁을 서로 연결한다. 일정이 있으면 이동 부담과 시간대까지 고려한다.
- 현재 지도에 있는 정보와 방금 웹에서 찾은 정보를 명확히 구분한다.
- 영업시간·휴무·예약·가격처럼 변하는 정보는 web_search 후 가능하면 fetch_page로 확인하고 URL을 답에 붙인다.
- 중국어 원명으로도 검색하고, 한 블로그를 유일한 근거로 삼지 않는다.
- 박물관 편중을 피하고 history, food, market_night, neighborhood, nature, shopping, rest, practical 역할을 균형 있게 본다.
- 사용자가 명시적으로 지도 저장/추가를 요청한 경우에만 propose_place 또는 upsert_place_insights를 사용한다. 신규 장소는 곧바로 공개하지 않고 승인 제안으로 저장한다.
- 근거가 부족하면 모른다고 말하고 확인 방법을 제시한다. 도구의 내부 JSON은 노출하지 않는다.

현재 지도 DB: {map_context}
현재 사용자 일정: {_plan_context(db, user_id=user_id, city_id=city_id)}
현재 선택 장소 ID: {selected_place_id or '없음'}
"""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend({"role": row.role, "content": row.content} for row in prior)
    messages.append({"role": "user", "content": message})

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_chat_model or settings.groq_model or "openai/gpt-oss-20b"
    sources: set[str] = set()
    final_text = ""
    for _ in range(8):
        kwargs: dict[str, Any] = {}
        if "gpt-oss" in model:
            kwargs["extra_body"] = {"reasoning_effort": "medium"}
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=_tool_subset(allowed),
            tool_choice="auto",
            temperature=0.25,
            **kwargs,
        )
        reply = response.choices[0].message
        calls = reply.tool_calls or []
        if not calls:
            final_text = (reply.content or "답변을 만들지 못했습니다.").strip()
            break
        messages.append(
            {
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in calls
                ],
            }
        )
        for call in calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name not in allowed:
                result: Any = {"error": "이 대화에서 허용되지 않은 작업입니다"}
            else:
                result = run_tool(db, name, args, city_id=city_id)
            _collect_sources(result, sources)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str)[:10000],
                }
            )
    if not final_text:
        final_text = "조사가 길어져 여기서 멈췄습니다. 질문을 한 장소나 한 주제로 좁혀 다시 물어봐 주세요."

    sources.update(URL_RE.findall(final_text))
    place_ids = {
        int(match)
        for match in PLACE_ID_RE.findall(final_text)
        if int(match) in existing_ids
    }
    user_row = TravelChatMessage(
        user_id=user_id,
        city_id=city_id,
        role="user",
        content=message,
        sources="[]",
        place_ids=json.dumps([selected_place_id] if selected_place_id else []),
    )
    assistant_row = TravelChatMessage(
        user_id=user_id,
        city_id=city_id,
        role="assistant",
        content=final_text,
        sources=json.dumps(sorted(sources)[:12], ensure_ascii=False),
        place_ids=json.dumps(sorted(place_ids)),
    )
    db.add_all([user_row, assistant_row])
    db.commit()
    db.refresh(assistant_row)
    return {
        "row": assistant_row,
        "model": model,
        "place_ids": sorted(place_ids),
        "sources": sorted(sources)[:12],
    }
