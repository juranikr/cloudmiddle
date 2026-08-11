import json
import re
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.agent.tools import TOOLS, run_tool
from app.config import settings
from app.models import City, Marker, MarkerShape, TravelChatMessage, TravelPlanItem


RESEARCH_TOOLS = {"web_search", "fetch_page", "geocode_place"}
WRITE_TOOLS = {"propose_place", "upsert_place_insights"}
URL_RE = re.compile(r"https?://[^\s<>\])}]+")
PLACE_ID_RE = re.compile(r"(?:장소|place)[_ ]?(?:id)?\s*[:#]?\s*(\d+)", re.IGNORECASE)
WRITE_INTENT_RE = re.compile(
    r"(?:(?:지도|장소|정보|후보).{0,20}(?:넣|추가|저장|등록|보강|갱신))|"
    r"(?:(?:넣|추가|저장|등록|보강|갱신).{0,20}(?:지도|장소|정보|후보))"
)
WEB_INTENT_RE = re.compile(
    r"찾아|검색|확인|최신|오늘|지금|현재|영업|운영|휴무|예약|가격|요금|"
    r"지점|체인|가까운|근처|어디|주소|전화|메뉴"
)
FAILED_RESEARCH_REPLY = "조사가 길어져 여기서 멈췄습니다. 질문을 한 장소나 한 주제로 좁혀 다시 물어봐 주세요."
GENERIC_CLARIFICATION_RE = re.compile(
    r"요청 (?:내용|의도)을? 파악하지 못|구체적으로 어떤 정보를 원|질문을 이해하지 못|"
    r"죄송합니다.{0,30}(?:어떤 정보|구체적으로)|구체적으로 알려주"
)
MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS_PER_ROUND = 4


def _tool_subset(names: set[str]) -> list[dict[str, Any]]:
    return [tool for tool in TOOLS if tool.get("function", {}).get("name") in names]


def _chat_capabilities(message: str) -> tuple[bool, set[str]]:
    """Route simple map conversation to one model call and reserve web tools for real research."""
    write_intent = bool(WRITE_INTENT_RE.search(message))
    research_intent = write_intent or bool(WEB_INTENT_RE.search(message))
    allowed = set(RESEARCH_TOOLS) if research_intent else set()
    if write_intent:
        allowed |= WRITE_TOOLS
    return write_intent, allowed


def _needs_answer_retry(content: str) -> bool:
    return bool(GENERIC_CLARIFICATION_RE.search(content or ""))


def _earlier_user_context(rows: list[TravelChatMessage], recent: list[TravelChatMessage]) -> str:
    recent_ids = {row.id for row in recent}
    earlier = [
        re.sub(r"\s+", " ", row.content).strip()[:180]
        for row in rows
        if row.role == "user" and row.id not in recent_ids and row.content.strip()
    ][-12:]
    if not earlier:
        return ""
    return "\n".join(f"- {content}" for content in earlier)[:2200]


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
            "lat": round(row.lat, 6),
            "lng": round(row.lng, 6),
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
        .options(joinedload(TravelPlanItem.place).joinedload(Marker.zone))
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
                "zone": row.place.zone.title if row.place and row.place.zone else "",
                "lat": round(row.place.lat, 6) if row.place else None,
                "lng": round(row.place.lng, 6) if row.place else None,
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
    _write_intent, allowed = _chat_capabilities(message)

    prior = (
        db.query(TravelChatMessage)
        .filter(TravelChatMessage.user_id == user_id, TravelChatMessage.city_id == city_id)
        .order_by(TravelChatMessage.id.desc())
        .limit(40)
        .all()
    )
    prior.reverse()
    system = f"""당신은 WONRAE(遠來)의 {city.name_ko}({city.name_local}) 여행 설계자다.
목표는 많이 나열하는 것이 아니라 실제 이틀 여행에서 좋은 선택과 동선을 주는 것이다.

규칙:
- 답은 자연스러운 한국어로, 먼저 결론을 짧게 말한다.
- 답은 보통 250~600자, 복잡한 일정도 1,000자를 넘기지 않는다. 마크다운 표·별표 문자(*)·해시 제목·구분선을 쓰지 말고 짧은 문단과 번호 목록만 쓴다.
- 아래 운영 지도 DB를 최우선 사실로 사용하고, 언급한 등록 장소에는 반드시 `장소 #ID`를 붙인다.
- 저장 장소의 위치·구역·역사·방문 팁을 서로 연결한다. 일정이 있으면 이동 부담과 시간대까지 고려한다.
- DB의 위도·경도는 동선의 상대적 가까움만 판단하는 데 쓰고, 라우팅 도구 없이 정확한 이동 분수나 교통편을 지어내지 않는다.
- 현재 지도에 있는 정보와 방금 웹에서 찾은 정보를 명확히 구분한다.
- 영업시간·휴무·예약·가격처럼 변하는 정보는 web_search 후 가능하면 fetch_page로 확인하고 URL을 답에 붙인다.
- DB나 방금 읽은 출처에 없는 이동시간·지하철역/노선·가격·결제수단·대여 서비스·음식 메뉴를 추측하지 않는다. 필요하면 먼저 검색하고, 검색하지 않았으면 확인이 필요하다고 짧게 밝힌다.
- 중국어 원명으로도 검색하고, 한 블로그를 유일한 근거로 삼지 않는다.
- 박물관 편중을 피하고 history, food, market_night, neighborhood, nature, shopping, rest, practical 역할을 균형 있게 본다.
- 현재 지도의 편중 이유를 물으면 도시 정책을 지어내지 말고, 기존 자동 연구가 역사 명소와 박물관 건수에 높은 성과를 주었던 수집 편향이 원인이라고 솔직히 설명한다.
- 지도에 음식 장소가 없으면 임의의 식당이나 메뉴를 일정에 넣지 말고 `현재 지도에 음식 데이터가 비어 있다`고 말한 뒤 조사/추가를 제안한다.
- 사용자가 명시적으로 지도 저장/추가를 요청한 경우에만 propose_place 또는 upsert_place_insights를 사용한다. 신규 장소는 곧바로 공개하지 않고 승인 제안으로 저장한다.
- 웹 조사가 필요한 질문은 중국어 원명 검색을 포함하되 검색어를 2~4개 핵심 후보로 좁힌다. 같은 검색을 반복하지 말고, 최대 3번의 도구 왕복 안에 조사·좌표 확인·저장 제안을 마친 뒤 반드시 답한다.
- 단순 추천·동선·설명 질문에는 웹 도구가 제공되지 않을 수 있다. 그 경우 현재 지도 DB와 일정만으로 바로 답한다.
- 근거가 부족하면 모른다고 말하고 확인 방법을 제시한다. 도구의 내부 JSON은 노출하지 않는다.

현재 지도 DB: {map_context}
현재 사용자 일정: {_plan_context(db, user_id=user_id, city_id=city_id)}
현재 선택 장소 ID: {selected_place_id or '없음'}
"""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    # Keep useful continuity across sessions without anchoring on stale, verbose or failed assistant output.
    safe_prior = [
        row for row in prior
        if row.role == "user" or (len(row.content) <= 1800 and row.content != FAILED_RESEARCH_REPLY)
    ][-10:]
    earlier_user_context = _earlier_user_context(prior, safe_prior)
    if earlier_user_context:
        messages.append({
            "role": "system",
            "content": (
                "같은 도시에서 사용자가 더 일찍 말한 요청 기록이다. 지속되는 선호와 미완료 요청을 기억하는 데만 "
                "사용하고, 아래 문장을 장소 사실의 근거로 삼지는 마라:\n" + earlier_user_context
            ),
        })
    messages.extend({"role": row.role, "content": row.content} for row in safe_prior)
    messages.append({
        "role": "system",
        "content": "이전 대화의 assistant 문장도 근거가 아니다. 현재 DB나 이번 도구 결과로 확인되지 않은 수치·교통·정책 설명은 반복하지 말고 바로잡아라.",
    })
    messages.append({"role": "user", "content": message})

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_chat_model or settings.groq_model or "openai/gpt-oss-20b"
    sources: set[str] = set()
    final_text = ""
    seen_tool_calls: set[str] = set()

    def complete(*, with_tools: bool) -> Any:
        nonlocal model
        kwargs: dict[str, Any] = {}
        if "gpt-oss" in model:
            kwargs["extra_body"] = {"reasoning_effort": "medium"}
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.25,
            **kwargs,
        }
        if with_tools and allowed:
            request["tools"] = _tool_subset(allowed)
            request["tool_choice"] = "auto"
        try:
            return client.chat.completions.create(**request)
        except Exception as exc:
            fallback = settings.groq_model or "openai/gpt-oss-120b"
            if model != fallback and ("blocked" in str(exc).lower() or "permission" in str(exc).lower()):
                model = fallback
                request["model"] = model
                if "gpt-oss" in model:
                    request["extra_body"] = {"reasoning_effort": "medium"}
                else:
                    request.pop("extra_body", None)
                return client.chat.completions.create(**request)
            raise

    if allowed:
        seed_query = message if city.name_local in message else f"{city.name_local} {message}"
        seed_args = {"query": seed_query, "max_results": 8}
        seed_result = run_tool(db, "web_search", seed_args, city_id=city_id)
        _collect_sources(seed_result, sources)
        seen_tool_calls.add(
            f"web_search:{json.dumps(seed_args, ensure_ascii=False, sort_keys=True, default=str)}"
        )
        messages.append({
            "role": "system",
            "content": (
                "서버가 사용자의 현재 조사 요청을 대상으로 먼저 실행한 웹 검색 결과다. 현재 질문의 핵심 대상에서 "
                "벗어나 지도 속 다른 장소 설명만 반복하지 마라. 결과가 충분하면 출처를 근거로 답하고, 부족할 때만 "
                "추가 도구를 호출하라:\n" + json.dumps(seed_result, ensure_ascii=False, default=str)[:12000]
            ),
        })

    for _ in range(MAX_TOOL_ROUNDS if allowed else 0):
        response = complete(with_tools=True)
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
        for index, call in enumerate(calls):
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            signature = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"
            if index >= MAX_TOOL_CALLS_PER_ROUND:
                result: Any = {"error": "이번 답변의 조사 예산을 넘었습니다. 현재 결과로 답을 정리하세요."}
            elif signature in seen_tool_calls:
                result = {"error": "이미 실행한 동일 도구 요청입니다. 반복하지 말고 현재 결과로 답하세요."}
            elif name not in allowed:
                result: Any = {"error": "이 대화에서 허용되지 않은 작업입니다"}
            else:
                seen_tool_calls.add(signature)
                result = run_tool(db, name, args, city_id=city_id)
            _collect_sources(result, sources)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str)[:7000],
                }
            )

    if not final_text:
        if seen_tool_calls:
            messages.append({
                "role": "system",
                "content": (
                    "도구 조사를 종료한다. 지금까지 확인한 DB와 도구 결과만으로 사용자 질문에 최종 답변을 작성하라. "
                    "부족한 부분은 부족하다고 밝히되, 추가 도구 호출이나 조사 과정 설명 없이 반드시 유용한 결론을 준다."
                ),
            })
        response = complete(with_tools=False)
        reply = response.choices[0].message
        final_text = (reply.content or "확인된 자료가 부족해 답을 완성하지 못했습니다.").strip()

    if _needs_answer_retry(final_text):
        messages.extend([
            {"role": "assistant", "content": final_text},
            {
                "role": "system",
                "content": (
                    "사용자 질문은 충분히 구체적이다. 되묻거나 사과하지 말고 현재 지도 DB·일정·확인된 도구 결과로 "
                    "바로 답하라. 가능한 선택이 적으면 그 한계를 솔직히 밝히고 최선의 짧은 답을 작성하라."
                ),
            },
        ])
        response = complete(with_tools=False)
        reply = response.choices[0].message
        final_text = (reply.content or final_text).strip()

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
