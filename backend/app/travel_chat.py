import json
import logging
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
    r"지도에\s*(?:넣|찍)(?:어|어줘|어주세요)?|"
    r"(?:추가|저장|등록|보강|갱신)(?:해(?:줘|주세요)?|하(?:자|고|라|여줘)?|시켜줘)?(?![가-힣])"
)
WEB_INTENT_RE = re.compile(
    r"찾아|검색|확인|최신|오늘|지금|영업|운영|휴무|예약|가격|요금|"
    r"지점|체인|가까운|근처|어디|주소|전화|메뉴"
)
BRAND_SEARCH_ALIASES = {
    "헤이티": "喜茶 HEYTEA",
    "희차": "喜茶 HEYTEA",
    "모어요거트": "茉酸奶 More Yogurt",
}
BRAND_ANSWER_ALIASES = {
    "헤이티": ("헤이티", "희차", "喜茶", "heytea"),
    "모어요거트": ("모어요거트", "茉酸奶", "more yogurt"),
}
FAILED_RESEARCH_REPLIES = {
    "조사가 길어져 여기서 멈췄습니다. 질문을 한 장소나 한 주제로 좁혀 다시 물어봐 주세요.",
    "확인된 자료가 부족해 답을 완성하지 못했습니다.",
    "답변을 만들지 못했습니다.",
}
GENERIC_CLARIFICATION_RE = re.compile(
    r"요청 (?:내용|의도)을? 파악하지 못|구체적으로 어떤 정보를 원|질문을 이해하지 못|"
    r"죄송합니다.{0,30}(?:어떤 정보|구체적으로)|구체적으로 알려주|"
    r"어떤 정보를 원하시는지 알려|구체적인 질문을 파악하기 어렵|"
    r"확인된 자료가 부족해 답을 완성하지 못"
)
SHORT_FOLLOWUP_RE = re.compile(
    r"^(?:(?:그거|그것|둘\s*다|전부|모두|찾은\s*것|위\s*장소)(?:를|도|부터)?\s*)?"
    r"(?:지도에\s*)?(?:추가|등록|저장|넣)(?:해|하|해줘|해주세요|시켜줘)?[.!?\s]*$"
)
FAILURE_QUESTION_RE = re.compile(r"왜.{0,12}(?:실패|못|안\s*(?:돼|되)|자료.{0,5}부족)|자꾸.{0,10}(?:실패|못|안\s*(?:돼|되))")
MAX_RESEARCH_TOOL_ROUNDS = 3
MAX_WRITE_TOOL_ROUNDS = 5
MAX_TOOL_CALLS_PER_ROUND = 4
logger = logging.getLogger(__name__)


def _tool_subset(names: set[str]) -> list[dict[str, Any]]:
    return [tool for tool in TOOLS if tool.get("function", {}).get("name") in names]


def _chat_capabilities(message: str, *, context_message: str = "") -> tuple[bool, set[str]]:
    """Route simple map conversation to one model call and reserve web tools for real research."""
    write_intent = bool(WRITE_INTENT_RE.search(message))
    combined = f"{context_message}\n{message}" if context_message else message
    research_intent = write_intent or bool(WEB_INTENT_RE.search(combined))
    allowed = set(RESEARCH_TOOLS) if research_intent else set()
    if write_intent:
        allowed |= WRITE_TOOLS
    return write_intent, allowed


def _needs_answer_retry(content: str) -> bool:
    normalized = (content or "").strip()
    return normalized in FAILED_RESEARCH_REPLIES or bool(GENERIC_CLARIFICATION_RE.search(normalized))


def _recent_user_requests(rows: list[TravelChatMessage], *, limit: int = 8) -> list[str]:
    return [
        re.sub(r"\s+", " ", row.content).strip()[:300]
        for row in rows
        if row.role == "user" and row.content.strip()
    ][-limit:]


def _resolve_context_message(message: str, rows: list[TravelChatMessage]) -> str:
    """Attach the previous concrete user request to short commands such as `추가해줘`."""
    if not SHORT_FOLLOWUP_RE.search(message.strip()):
        return message
    previous = _recent_user_requests(rows, limit=8)
    for content in reversed(previous):
        if not SHORT_FOLLOWUP_RE.search(content) and not FAILURE_QUESTION_RE.search(content):
            return f"이전 사용자 요청: {content}\n현재 후속 지시: {message}"
    return message


def _brand_targets(message: str) -> list[str]:
    targets: list[str] = []
    folded = message.casefold()
    for name, aliases in BRAND_ANSWER_ALIASES.items():
        if any(alias.casefold() in folded for alias in aliases):
            targets.append(name)
    return targets


def _missing_brand_targets(message: str, content: str) -> list[str]:
    folded = (content or "").casefold()
    return [
        target
        for target in _brand_targets(message)
        if not any(alias.casefold() in folded for alias in BRAND_ANSWER_ALIASES[target])
    ]


def _research_seed_query(city: City, message: str) -> str:
    query = message
    for name, alias in BRAND_SEARCH_ALIASES.items():
        if name in query and alias not in query:
            query = query.replace(name, f"{name} {alias}")
    return query if city.name_local in query else f"{city.name_local} {query}"


def _research_seed_queries(city: City, message: str) -> list[str]:
    """Prefer short Chinese brand queries; long Korean action sentences produce noisy results."""
    queries: list[str] = []
    folded = message.casefold()
    if any(alias.casefold() in folded for alias in BRAND_ANSWER_ALIASES["모어요거트"]):
        queries.append(f"{city.name_local} 茉酸奶 门店 地址")
    if any(alias.casefold() in folded for alias in BRAND_ANSWER_ALIASES["헤이티"]):
        queries.append(f"{city.name_local} 喜茶 HEYTEA 门店 地址")
    general = _research_seed_query(city, message)
    if not queries:
        queries.append(general)
    return queries[:3]


def _unsupported_urls(content: str, sources: set[str]) -> list[str]:
    return [url for url in URL_RE.findall(content or "") if url not in sources]


def _strip_unsupported_urls(content: str, sources: set[str]) -> str:
    cleaned = content
    for url in _unsupported_urls(cleaned, sources):
        cleaned = cleaned.replace(url, "[검증되지 않은 링크 제거]")
    return cleaned


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
        .limit(120)
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
            "description": (row.description or "")[:160],
            "insights": [
                {"kind": item.kind, "title": item.title, "content": (item.content or "")[:140]}
                for item in (row.insights or [])[:2]
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


def _collect_tool_sources(name: str, value: Any, out: set[str]) -> None:
    """Keep the evidence list small and tied to results the agent actually received."""
    if name == "web_search" and isinstance(value, dict):
        for item in (value.get("results") or [])[:5]:
            if isinstance(item, dict) and str(item.get("href") or "").startswith(("http://", "https://")):
                out.add(str(item["href"]))
        return
    _collect_sources(value, out)


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

    prior = (
        db.query(TravelChatMessage)
        .filter(TravelChatMessage.user_id == user_id, TravelChatMessage.city_id == city_id)
        .order_by(TravelChatMessage.id.desc())
        .limit(40)
        .all()
    )
    prior.reverse()
    resolved_message = _resolve_context_message(message, prior)
    write_intent, allowed = _chat_capabilities(message, context_message=resolved_message)
    brand_targets = _brand_targets(resolved_message)
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
- 웹 조사가 필요한 질문은 중국어 원명 검색을 포함하되 검색어를 2~4개 핵심 후보로 좁힌다. 같은 검색을 반복하지 말고, 일반 조사는 3번·저장 요청은 최대 5번의 도구 왕복 안에 조사·좌표 확인·저장 제안을 마친 뒤 반드시 답한다.
- 단순 추천·동선·설명 질문에는 웹 도구가 제공되지 않을 수 있다. 그 경우 현재 지도 DB와 일정만으로 바로 답한다.
- 근거가 부족하면 모른다고 말하고 확인 방법을 제시한다. 도구의 내부 JSON은 노출하지 않는다.

현재 지도 DB: {map_context}
현재 사용자 일정: {_plan_context(db, user_id=user_id, city_id=city_id)}
현재 선택 장소 ID: {selected_place_id or '없음'}
"""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    # Prior assistant text may itself be hallucinated. Preserve user intent, never assistant claims.
    recent_requests = _recent_user_requests(prior, limit=10)
    if recent_requests:
        messages.append({
            "role": "system",
            "content": (
                "같은 도시에서 사용자가 앞서 말한 요청 기록이다. 지속되는 선호·대상·미완료 요청을 이어받는 데만 "
                "사용하고, 장소 사실의 근거로 삼지는 마라. assistant의 이전 답변은 의도적으로 제외했다:\n"
                + "\n".join(f"- {item}" for item in recent_requests)
            ),
        })
    messages.append({
        "role": "system",
        "content": "이전 대화의 assistant 문장도 근거가 아니다. 현재 DB나 이번 도구 결과로 확인되지 않은 수치·교통·정책 설명은 반복하지 말고 바로잡아라.",
    })
    if resolved_message != message:
        messages.append({
            "role": "system",
            "content": (
                "현재 말은 짧은 후속 명령이다. 아래처럼 직전의 구체적 사용자 요청과 결합해 해석했다. "
                "현재 지시를 새 질문처럼 버리지 말고 실제 행동까지 이어가라:\n" + resolved_message
            ),
        })
    messages.append({"role": "user", "content": message})

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_chat_model or settings.groq_model or "openai/gpt-oss-20b"
    sources: set[str] = set()
    final_text = ""
    seen_tool_calls: set[str] = set()
    tool_results: list[dict[str, Any]] = []
    proposal_ids: list[int] = []
    write_attempted = False
    write_succeeded = False
    successful_write_targets: set[str] = set()

    def writes_complete() -> bool:
        return write_succeeded and (
            not brand_targets or set(brand_targets).issubset(successful_write_targets)
        )

    def complete(
        *,
        tool_names: set[str] | None = None,
        tool_choice: str = "auto",
    ) -> Any:
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
        if tool_names:
            request["tools"] = _tool_subset(tool_names)
            request["tool_choice"] = tool_choice
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
        seed_results: list[dict[str, Any]] = []
        for seed_query in _research_seed_queries(city, resolved_message):
            seed_args = {"query": seed_query, "max_results": 8}
            seed_result = run_tool(db, "web_search", seed_args, city_id=city_id)
            _collect_tool_sources("web_search", seed_result, sources)
            seen_tool_calls.add(
                f"web_search:{json.dumps(seed_args, ensure_ascii=False, sort_keys=True, default=str)}"
            )
            seed_results.append({"query": seed_query, "result": seed_result})
            tool_results.append({"name": "web_search", "args": seed_args, "result": seed_result})
        messages.append({
            "role": "system",
            "content": (
                "서버가 현재 요청의 핵심 대상을 짧은 중국어 검색어로 먼저 조사한 결과다. `喜茶`는 HEYTEA이며 "
                "`海蒂`(사람 이름·호텔·다른 찻집)와 혼동하면 안 된다. 지도 속 다른 장소 설명으로 질문을 회피하지 마라. "
                "저장 요청이면 검색 → 페이지/좌표 확인 → propose_place 실행까지 끝내라:\n"
                + json.dumps(seed_results, ensure_ascii=False, default=str)[:18000]
            ),
        })

    force_required = False
    tool_round_limit = MAX_WRITE_TOOL_ROUNDS if write_intent else MAX_RESEARCH_TOOL_ROUNDS
    for _round in range(tool_round_limit if allowed else 0):
        response = complete(
            tool_names=allowed,
            tool_choice="required" if force_required else "auto",
        )
        reply = response.choices[0].message
        calls = reply.tool_calls or []
        if not calls:
            if write_intent and not writes_complete() and not force_required:
                messages.extend([
                    {"role": "assistant", "content": reply.content or ""},
                    {
                        "role": "system",
                        "content": (
                            "사용자가 저장을 명시했지만 아직 저장 도구 성공이 0건이다. 말로 `제안하겠다`고 답하는 것은 실패다. "
                            "지금 반드시 도구를 호출하라. 근거·좌표가 부족하면 fetch_page/geocode_place로 채우고, "
                            "준비된 대상은 propose_place로 관리자 승인 대기에 실제 저장하라."
                        ),
                    },
                ])
                force_required = True
                continue
            final_text = (reply.content or "답변을 만들지 못했습니다.").strip()
            break
        force_required = False
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
            if name in WRITE_TOOLS:
                write_attempted = True
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
                result = {"error": "이 대화에서 허용되지 않은 작업입니다"}
            else:
                seen_tool_calls.add(signature)
                result = run_tool(db, name, args, city_id=city_id)
            _collect_tool_sources(name, result, sources)
            tool_results.append({"name": name, "args": args, "result": result})
            if name in WRITE_TOOLS and isinstance(result, dict) and result.get("ok"):
                write_succeeded = True
                serialized_args = json.dumps(args, ensure_ascii=False, default=str).casefold()
                for target in brand_targets:
                    if any(
                        alias.casefold() in serialized_args
                        for alias in BRAND_ANSWER_ALIASES[target]
                    ):
                        successful_write_targets.add(target)
                if result.get("proposal_id") is not None:
                    proposal_ids.append(int(result["proposal_id"]))
            logger.info(
                "travel_chat tool user=%s city=%s round=%s tool=%s ok=%s error=%s",
                user_id,
                city_id,
                _round + 1,
                name,
                bool(isinstance(result, dict) and result.get("ok")),
                str(result.get("error") or "")[:120] if isinstance(result, dict) else "",
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str)[:7000],
                }
            )
        if writes_complete():
            break

    if not final_text:
        action_state = ""
        if write_intent and writes_complete():
            action_state = (
                f" 실제 저장 상태: 관리자 승인 대기 제안 {sorted(set(proposal_ids)) or '생성 완료'}가 DB에 생성되었다. "
                "반드시 완료 사실과 승인 대기임을 구분해 말하라."
            )
        elif write_intent:
            errors = [
                str(item["result"].get("error") or item["result"].get("detail") or "")[:180]
                for item in tool_results
                if isinstance(item.get("result"), dict)
                and (item["result"].get("error") or item["result"].get("detail"))
            ]
            action_state = (
                " 실제 저장 상태: 성공한 저장 도구가 0건이다. 저장했다고 말하지 마라. "
                f"확인된 실패 이유: {errors[-4:] or ['모델이 저장 도구를 끝까지 실행하지 않음']}"
            )
        messages.append({
            "role": "system",
            "content": (
                "도구 조사를 종료한다. 현재 DB와 도구 결과만으로 최종 답을 작성하라. 검색 결과에 없던 URL·주소·좌표·"
                "영업시간을 만들지 말고, 핵심 조사 대상을 각각 언급한다. 내부 과정 대신 사용자에게 필요한 결론을 준다."
                + action_state
            ),
        })
        response = complete()
        reply = response.choices[0].message
        final_text = (reply.content or "확인된 자료가 부족해 답을 완성하지 못했습니다.").strip()

    missing_targets = _missing_brand_targets(resolved_message, final_text)
    bad_urls = _unsupported_urls(final_text, sources)
    if _needs_answer_retry(final_text) or missing_targets or bad_urls:
        messages.extend([
            {"role": "assistant", "content": final_text},
            {
                "role": "system",
                "content": (
                    "방금 답은 검증에 실패했다. 되묻거나 포괄적인 자료 부족 문장으로 끝내지 마라. "
                    f"반드시 언급할 대상: {missing_targets or brand_targets or ['현재 사용자 질문']}. "
                    f"답에 사용할 수 있는 URL은 다음 목록뿐이다: {sorted(sources)[:12]}. "
                f"요청 대상 전체의 저장 완료 여부는 {writes_complete()}이고 제안 ID는 {sorted(set(proposal_ids))}다. "
                    "확인하지 못한 사실은 그 항목만 구체적으로 밝히고, 실제로 한 행동과 못 한 행동을 분리해 다시 써라."
                ),
            },
        ])
        response = complete()
        reply = response.choices[0].message
        final_text = (reply.content or final_text).strip()

    if _needs_answer_retry(final_text):
        target_text = "·".join(brand_targets) or "요청한 장소"
        if writes_complete():
            if proposal_ids:
                final_text = (
                    f"{target_text}을 관리자 승인 대기 제안 #{', #'.join(map(str, sorted(set(proposal_ids))))}로 실제 저장했습니다. "
                    "관리자가 승인하면 지도에 반영됩니다."
                )
            else:
                final_text = f"{target_text}의 확인된 정보를 기존 지도 장소에 실제 반영했습니다."
        elif write_intent:
            attempted = "저장 도구 실행은 시도했지만" if write_attempted else "저장 도구가 실행되지 않아"
            completed = "·".join(sorted(successful_write_targets))
            remaining = "·".join(target for target in brand_targets if target not in successful_write_targets)
            progress = (
                f" {completed}은 저장됐지만 {remaining}은 저장되지 않았습니다."
                if completed and remaining
                else ""
            )
            final_text = (
                f"요청을 전부 완료하지 못했습니다.{progress} {attempted} "
                "정확한 지점 좌표와 출처를 갖춘 제안을 만들지 못했습니다. 말로만 제안한 상태를 완료로 처리하지 않았습니다."
            )
        elif FAILURE_QUESTION_RE.search(message):
            final_text = (
                "앞선 요청을 실제 저장 도구와 연결하지 못해 실패했습니다. 이전의 ‘등록 제안 요약’은 DB 저장 완료가 "
                "아니었고, 그래서 이어진 짧은 명령도 대상을 잃었습니다. 이제 저장은 DB 제안 행이 생성된 경우에만 완료로 답합니다."
            )
        else:
            final_text = "현재 지도와 확인된 자료만으로 답을 특정하지 못했습니다. 필요한 대상이나 조건 한 가지만 더 알려 주세요."

    final_text = _strip_unsupported_urls(final_text, sources)
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
