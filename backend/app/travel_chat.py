import hashlib
import json
import logging
import re
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.agent.tools import TOOLS, run_tool
from app.config import settings
from app.geocode import parse_viewbox
from app.models import (
    City,
    Marker,
    MarkerShape,
    TravelChatMessage,
    TravelChatWork,
    TravelPlan,
    TravelPlanItem,
)
from app.personalization import build_user_travel_profile, profile_prompt_context


RESEARCH_TOOLS = {"web_search", "fetch_page", "geocode_place"}
WRITE_TOOLS = {"propose_place", "upsert_place_insights"}
URL_RE = re.compile(r"https?://[^\s<>\])}]+")
PLACE_ID_RE = re.compile(r"(?:장소|place)[_ ]?(?:id)?\s*[:#]?\s*(\d+)", re.IGNORECASE)
BRAND_SEARCH_ALIASES = {
    "헤이티": "喜茶 HEYTEA",
    "희차": "喜茶 HEYTEA",
    "모어요거트": "茉酸奶 More Yogurt",
}
BRAND_STORE_SEARCHES = {
    "모어요거트": "茉酸奶 大悦城旗舰店",
    "헤이티": "喜茶 大悦城店 中街益田假日世界店",
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
MAX_RESEARCH_TOOL_ROUNDS = 3
MAX_WRITE_TOOL_ROUNDS = 8
MAX_TOOL_CALLS_PER_ROUND = 4
# City-local bootstrap evidence is deliberately small and auditable. It provides
# a stable floor when general search engines omit Chinese map/detail pages; the
# model still uses live search for broader discovery and current facts.
CITY_FOOD_DETAIL_SOURCES: dict[str, tuple[str, ...]] = {
    "shenyang": (
        "https://touch.travel.qunar.com/dist/poi/3332184",
        "https://gs.ctrip.com/html5/you/foods/fooddetail/155/5382272.html",
        "https://touch.travel.qunar.com/poi/3330756",
    ),
}
CITY_SNACK_DETAIL_SOURCES: dict[str, tuple[str, ...]] = {
    "shenyang": (
        "https://gs.ctrip.com/html5/you/foods/fooddetail/155/15729804.html",
        "https://gs.ctrip.com/html5/you/foods/fooddetail/155/22551502.html",
        "https://gs.ctrip.com/html5/you/foods/fooddetail/155/5383835.html",
    ),
}
logger = logging.getLogger(__name__)


@dataclass
class ChatIntent:
    """Semantic request contract returned by the model, never by phrase regexes."""

    action: str = "answer"
    scope: str = "unspecified"
    subject: str = ""
    goal: str = ""
    wants_research: bool = False
    wants_write: bool = False
    continuation: bool = False
    starts_new_work: bool = False
    requested_count: int | None = None
    target_keys: list[str] | None = None
    constraints: list[str] | None = None
    exclusions: list[str] | None = None
    confidence: float = 0.0

    def normalized(self) -> "ChatIntent":
        valid_actions = {
            "answer", "research", "write", "research_and_write", "continue",
            "correct", "explain_failure",
        }
        valid_scopes = {"single", "selected", "remaining", "all", "unspecified"}
        self.action = self.action if self.action in valid_actions else "answer"
        self.scope = self.scope if self.scope in valid_scopes else "unspecified"
        self.subject = self.subject.strip()[:120]
        self.goal = self.goal.strip()[:500]
        self.target_keys = [str(item)[:80] for item in (self.target_keys or []) if str(item).strip()][:30]
        self.constraints = [
            str(item).strip()[:120] for item in (self.constraints or []) if str(item).strip()
        ][:20]
        self.exclusions = [
            str(item).strip()[:120] for item in (self.exclusions or []) if str(item).strip()
        ][:20]
        if self.subject == "food_snack":
            # These are execution semantics of the classified concept, not
            # phrase matching against the user's wording. They keep a sparse
            # model response from silently drifting back to full meals.
            self.constraints = list(dict.fromkeys([
                *self.constraints, "snack_portion", "takeaway_or_walk_and_eat",
            ]))
            self.exclusions = list(dict.fromkeys([
                *self.exclusions, "full_meal", "sit_down_restaurant",
            ]))
        if self.requested_count is not None:
            self.requested_count = max(1, min(int(self.requested_count), 20))
        self.confidence = max(0.0, min(float(self.confidence or 0), 1.0))
        return self


def _tool_subset(names: set[str]) -> list[dict[str, Any]]:
    return [tool for tool in TOOLS if tool.get("function", {}).get("name") in names]


def _chat_capabilities(intent: ChatIntent) -> tuple[bool, set[str]]:
    """Grant tools from the structured semantic decision, not matched phrases."""
    allowed = set(RESEARCH_TOOLS) if intent.wants_research or intent.wants_write else set()
    if intent.wants_write:
        allowed |= WRITE_TOOLS
    return intent.wants_write, allowed


def _needs_answer_retry(content: str) -> bool:
    normalized = (content or "").strip()
    return normalized in FAILED_RESEARCH_REPLIES or bool(GENERIC_CLARIFICATION_RE.search(normalized))


def _recent_user_requests(rows: list[TravelChatMessage], *, limit: int = 8) -> list[str]:
    return [
        re.sub(r"\s+", " ", row.content).strip()[:300]
        for row in rows
        if row.role == "user" and row.content.strip()
    ][-limit:]


def _work_state(work: TravelChatWork | None) -> dict[str, Any]:
    if work is None:
        return {}
    try:
        state = json.loads(work.state or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _active_chat_work(db: Session, *, user_id: int, city_id: int) -> TravelChatWork | None:
    return (
        db.query(TravelChatWork)
        .filter(
            TravelChatWork.user_id == user_id,
            TravelChatWork.city_id == city_id,
            TravelChatWork.status == "active",
        )
        .order_by(TravelChatWork.id.desc())
        .first()
    )


def _work_summary(work: TravelChatWork | None) -> dict[str, Any] | None:
    if work is None:
        return None
    state = _work_state(work)
    candidates = [item for item in state.get("candidates", []) if isinstance(item, dict)]
    return {
        "work_id": work.id,
        "action": work.action,
        "scope": work.scope,
        "subject": work.subject,
        "goal": work.goal,
        "requested_count": work.requested_count,
        "phase": state.get("phase", "understand"),
        "next_action": state.get("next_action", ""),
        "candidate_count": len(candidates),
        "candidates": [
            {
                "key": item.get("key"),
                "title": item.get("title"),
                "status": item.get("status"),
                "proposal_id": item.get("proposal_id"),
                "address": item.get("address", ""),
                "category": item.get("category", "other"),
                "source_urls": list(item.get("source_urls") or [])[:3],
                "lat": item.get("lat"),
                "lng": item.get("lng"),
            }
            for item in candidates[:20]
        ],
        "completed_keys": list(state.get("completed_keys") or [])[:30],
        "constraints": list(state.get("constraints") or [])[:20],
        "exclusions": list(state.get("exclusions") or [])[:20],
        "failed": list(state.get("failed") or [])[-10:],
    }


def _parse_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _classify_chat_intent(
    client: Any,
    *,
    model: str,
    message: str,
    recent_requests: list[str],
    active_work: TravelChatWork | None,
) -> ChatIntent:
    """Understand arbitrary Korean/Chinese phrasing through a typed model decision."""
    payload = {
        "current_message": message,
        "recent_user_requests": recent_requests[-8:],
        "active_work": _work_summary(active_work),
    }
    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "너는 여행 지도 대화의 의도 라우터다. 단어나 정규식처럼 표면 표현에 의존하지 말고 "
                    "문장의 의미와 active_work를 함께 이해하라. 반드시 JSON 객체 하나만 반환한다. "
                    "action은 answer|research|write|research_and_write|continue|correct|explain_failure, "
                    "scope는 single|selected|remaining|all|unspecified 중 하나다. "
                    "subject는 food|food_snack|drink|spa|lodging|tourism|transport|shopping|itinerary|place_detail|general "
                    "중 가장 가까운 값으로 정규화한다. "
                    "한 끼 식사가 아닌 간식·디저트·빵·길거리 먹거리·포장 먹거리를 찾는 요청은 "
                    "food가 아니라 food_snack이다. 같은 음식 범주여도 식사에서 간식처럼 대상 조건이 "
                    "바뀌면 새 작업(starts_new_work=true, continuation=false)으로 본다. "
                    "wants_write는 사용자가 지도/DB/일정에 반영·추가·수정하기를 실제로 원할 때만 true다. "
                    "다만 active_work가 조사 후 저장까지 포함하고 사용자가 계속·승인·나머지 처리를 뜻하면 "
                    "그 저장 의도를 이어받는다. starts_new_work는 새 주제일 때만 true다. "
                    "requested_count는 사용자가 수량을 말했거나 '여러 개/다양하게'의 합리적 실행 목표가 "
                    "필요할 때 2~6으로 정하고, 전부/나머지는 null과 scope로 표현한다. "
                    "target_keys에는 active_work의 특정 후보를 가리킬 때만 key를 넣는다. constraints에는 "
                    "takeaway·walk_and_eat·dessert처럼 반드시 지킬 의미 조건을, exclusions에는 full_meal·"
                    "sit_down_restaurant처럼 제외할 대상을 짧은 영문 개념으로 넣는다. "
                    "반환 필드: action,scope,subject,goal,wants_research,wants_write,continuation,"
                    "starts_new_work,requested_count,target_keys,constraints,exclusions,confidence."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    if "gpt-oss" in model:
        request["extra_body"] = {"reasoning_effort": "low"}
    try:
        try:
            response = client.chat.completions.create(**request)
        except Exception:
            # Some compatible providers/models do not implement JSON mode even
            # though they reliably follow a JSON-only instruction.
            request.pop("response_format", None)
            response = client.chat.completions.create(**request)
        data = _parse_json_object(response.choices[0].message.content or "")
        if not data:
            raise ValueError("empty structured intent")
        intent = ChatIntent(
            action=str(data.get("action") or "answer"),
            scope=str(data.get("scope") or "unspecified"),
            subject=str(data.get("subject") or ""),
            goal=str(data.get("goal") or message),
            wants_research=bool(data.get("wants_research", False)),
            wants_write=bool(data.get("wants_write", False)),
            continuation=bool(data.get("continuation", False)),
            starts_new_work=bool(data.get("starts_new_work", False)),
            requested_count=data.get("requested_count"),
            target_keys=data.get("target_keys") if isinstance(data.get("target_keys"), list) else [],
            constraints=data.get("constraints") if isinstance(data.get("constraints"), list) else [],
            exclusions=data.get("exclusions") if isinstance(data.get("exclusions"), list) else [],
            confidence=float(data.get("confidence") or 0),
        ).normalized()
        if active_work is None and intent.starts_new_work and intent.scope == "remaining":
            intent.scope = "all"
        return intent
    except Exception as exc:  # noqa: BLE001
        # Fail closed for mutations but keep ordinary conversation available.
        logger.warning("travel_chat intent classification failed error=%s", str(exc)[:300])
        return ChatIntent(action="answer", goal=message, confidence=0.0)


def _classify_snack_fit(
    client: Any,
    *,
    model: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Semantically enforce the snack boundary before a place mutation."""
    payload = {
        "title": str(candidate.get("title") or ""),
        "description": str(candidate.get("description") or ""),
        "evidence": str(candidate.get("evidence") or ""),
        "insights": candidate.get("insights") if isinstance(candidate.get("insights"), list) else [],
        "claimed_consumption_mode": str(candidate.get("consumption_mode") or ""),
    }
    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "사용자는 한 끼 식사가 아닌 여행 중 간식을 사 먹을 장소만 원한다. 후보의 의미를 판단해 "
                    "JSON 하나로 답하라. 작은 길거리 먹거리, 디저트, 빵·과자·사탕, 아이스크림, 음료, "
                    "소량 포장 간식은 허용한다. 테이크아웃이 가능하더라도 초밥·면·밥·정식·고기요리처럼 "
                    "보통 한 끼로 먹는 주식/식당은 거부한다. 불명확하면 거부한다. 반환 필드: "
                    "allowed(boolean), consumption_mode(snack|dessert|drink|packaged|full_meal|unknown), "
                    "reason(짧은 한국어), confidence(0~1)."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    if "gpt-oss" in model:
        request["extra_body"] = {"reasoning_effort": "low"}
    try:
        try:
            response = client.chat.completions.create(**request)
        except Exception:
            request.pop("response_format", None)
            response = client.chat.completions.create(**request)
        data = _parse_json_object(response.choices[0].message.content or "")
        mode = str(data.get("consumption_mode") or "unknown")
        allowed_modes = {"snack", "dessert", "drink", "packaged"}
        allowed = bool(data.get("allowed")) and mode in allowed_modes
        return {
            "ok": True,
            "allowed": allowed,
            "consumption_mode": mode if mode in allowed_modes | {"full_meal", "unknown"} else "unknown",
            "reason": str(data.get("reason") or "간식 범위 판별 근거 없음")[:300],
            "confidence": max(0.0, min(float(data.get("confidence") or 0), 1.0)),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("travel_chat snack fit classification failed error=%s", str(exc)[:300])
        return {
            "ok": False,
            "allowed": False,
            "consumption_mode": "unknown",
            "reason": "간식 범위를 검증하지 못해 저장을 보류했습니다.",
            "confidence": 0.0,
        }


def _inherit_active_intent(intent: ChatIntent, work: TravelChatWork | None) -> ChatIntent:
    if work is None or not intent.continuation:
        return intent
    state = _work_state(work)
    intent.subject = intent.subject or work.subject
    intent.goal = intent.goal or work.goal
    intent.wants_research = intent.wants_research or bool(state.get("wants_research"))
    intent.wants_write = intent.wants_write or bool(state.get("wants_write"))
    intent.constraints = list(dict.fromkeys([
        *(intent.constraints or []), *list(state.get("constraints") or []),
    ]))
    intent.exclusions = list(dict.fromkeys([
        *(intent.exclusions or []), *list(state.get("exclusions") or []),
    ]))
    if intent.action in {"answer", "continue"}:
        if intent.wants_research and intent.wants_write:
            intent.action = "research_and_write"
        elif intent.wants_write:
            intent.action = "write"
        elif intent.wants_research:
            intent.action = "research"
    if intent.scope == "unspecified":
        intent.scope = work.scope
    if intent.requested_count is None:
        intent.requested_count = work.requested_count
    return intent.normalized()


def _ensure_chat_work(
    db: Session,
    *,
    user_id: int,
    city_id: int,
    intent: ChatIntent,
    active_work: TravelChatWork | None,
) -> TravelChatWork | None:
    needs_work = intent.wants_research or intent.wants_write or intent.continuation
    if not needs_work:
        # An unrelated informational turn may happen while durable work is
        # still open. Keep that ledger untouched so the user can resume it
        # later instead of letting this answer overwrite its phase/candidates.
        return None
    if active_work is not None and intent.starts_new_work and not intent.continuation:
        active_work.status = "superseded"
        active_work = None
    if active_work is None:
        active_work = TravelChatWork(
            user_id=user_id,
            city_id=city_id,
            status="active",
            action=intent.action,
            scope=intent.scope,
            subject=intent.subject,
            goal=intent.goal,
            requested_count=intent.requested_count,
            state=json.dumps({
                "version": 1,
                "phase": "understand",
                "next_action": "research" if intent.wants_research else "write",
                "wants_research": intent.wants_research,
                "wants_write": intent.wants_write,
                "candidates": [],
                "completed_keys": [],
                "constraints": intent.constraints or [],
                "exclusions": intent.exclusions or [],
                "failed": [],
            }, ensure_ascii=False),
        )
        db.add(active_work)
        db.flush()
    else:
        state = _work_state(active_work)
        state["wants_research"] = bool(state.get("wants_research")) or intent.wants_research
        state["wants_write"] = bool(state.get("wants_write")) or intent.wants_write
        state["constraints"] = list(dict.fromkeys([
            *list(state.get("constraints") or []), *(intent.constraints or []),
        ]))[:20]
        state["exclusions"] = list(dict.fromkeys([
            *list(state.get("exclusions") or []), *(intent.exclusions or []),
        ]))[:20]
        state["next_action"] = "write" if intent.wants_write else state.get("next_action", "research")
        active_work.action = intent.action if intent.action != "continue" else active_work.action
        active_work.scope = intent.scope if intent.scope != "unspecified" else active_work.scope
        active_work.subject = intent.subject or active_work.subject
        active_work.goal = intent.goal or active_work.goal
        active_work.requested_count = intent.requested_count or active_work.requested_count
        active_work.state = json.dumps(state, ensure_ascii=False, default=str)
    db.commit()
    db.refresh(active_work)
    return active_work


def _resolve_context_message(
    message: str,
    rows: list[TravelChatMessage],
    *,
    intent: ChatIntent,
    work: TravelChatWork | None,
) -> str:
    """Resolve semantic continuations against durable structured work."""
    if not intent.continuation:
        return message
    summary = _work_summary(work)
    if summary:
        return (
            "이어갈 활성 작업: "
            + json.dumps(summary, ensure_ascii=False, default=str)
            + f"\n현재 후속 지시: {message}"
        )
    previous = _recent_user_requests(rows, limit=8)
    return (
        "활성 작업 원장은 없지만 다음 사용자 요청 흐름을 이어가야 한다: "
        + json.dumps(previous, ensure_ascii=False)
        + f"\n현재 후속 지시: {message}"
    )


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _latest_chat_candidate(rows: list[TravelChatMessage]) -> dict[str, Any] | None:
    """Return the newest structured candidate, never an assistant prose guess."""
    for row in reversed(rows):
        if getattr(row, "role", "") != "assistant":
            continue
        candidates = _json_list(getattr(row, "candidates", "[]"))
        for candidate in candidates:
            if not isinstance(candidate, dict) or not str(candidate.get("title") or "").strip():
                continue
            if str(candidate.get("status") or "") in {"mapped", "dismissed"}:
                continue
            return dict(candidate)
        # Only the latest assistant turn may bind a short command.  Looking past
        # an intervening answer can silently register an older, unrelated place.
        return None
    return None


def _latest_chat_candidates(
    rows: list[TravelChatMessage],
    *,
    subject: str = "",
) -> list[dict[str, Any]]:
    """Recover pre-ledger candidates for a semantic continuation.

    This is only a compatibility bridge for conversations created before
    ``TravelChatWork`` existed. Empty failure/clarification answers may sit
    between the user's continuation and the last useful candidate list, so we
    inspect a small recent window and use the classified subject to avoid
    reviving an unrelated older task.
    """
    subject_categories = {
        "food": {"restaurant"},
        "food_snack": {"restaurant", "drink", "shopping", "other"},
        "drink": {"restaurant", "shopping", "other"},
        "spa": {"other"},
        "lodging": {"hotel"},
        "tourism": {"attraction"},
        "transport": {"transport"},
        "shopping": {"shopping"},
    }
    expected = subject_categories.get(str(subject or "").strip().lower())
    for row in reversed(rows[-16:]):
        if getattr(row, "role", "") != "assistant":
            continue
        candidates = [
            dict(item) for item in _json_list(getattr(row, "candidates", "[]"))
            if isinstance(item, dict) and str(item.get("title") or "").strip()
            and str(item.get("status") or "") not in {"mapped", "dismissed", "approved"}
        ]
        if expected:
            candidates = [
                item for item in candidates
                if str(item.get("category") or "").strip().lower() in expected
            ]
        if candidates:
            return candidates
    return []


def _work_candidates(work: TravelChatWork | None) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _work_state(work).get("candidates", [])
        if isinstance(item, dict) and str(item.get("title") or "").strip()
    ]


def _pending_work_candidates(
    work: TravelChatWork | None,
    *,
    target_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    selected = set(target_keys or [])
    terminal = {"proposed", "mapped", "dismissed", "approved", "duplicate"}
    return [
        item for item in _work_candidates(work)
        if str(item.get("status") or "") not in terminal
        and (not selected or str(item.get("key") or "") in selected)
    ]


def _merge_work_candidates(
    work: TravelChatWork | None,
    candidates: list[dict[str, Any]],
    *,
    proposal_ids: list[int] | None = None,
    proposal_titles: list[str] | None = None,
    phase: str = "research",
    next_action: str = "research",
    failures: list[dict[str, Any]] | None = None,
) -> None:
    if work is None:
        return
    state = _work_state(work)
    existing = {
        str(item.get("key") or _compact_candidate_text(str(item.get("title") or ""))): dict(item)
        for item in state.get("candidates", [])
        if isinstance(item, dict)
    }
    for raw in candidates:
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            continue
        item = dict(raw)
        key = str(item.get("key") or _compact_candidate_text(str(item.get("title") or "")))
        item["key"] = key
        previous = existing.get(key, {})
        merged = {**previous, **{field: value for field, value in item.items() if value not in (None, "", [])}}
        merged["source_urls"] = list(dict.fromkeys([
            *(previous.get("source_urls") or []),
            *(item.get("source_urls") or []),
        ]))[:8]
        existing[key] = merged
    completed = set(str(item) for item in state.get("completed_keys", []))
    titles = [str(item) for item in (proposal_titles or [])]
    proposal_values = list(dict.fromkeys(int(item) for item in (proposal_ids or [])))
    for key, item in existing.items():
        title_key = _compact_candidate_text(str(item.get("title") or ""))
        if any(
            title_key in _compact_candidate_text(title) or _compact_candidate_text(title) in title_key
            for title in titles
        ):
            item["status"] = "proposed"
            if proposal_values:
                item["proposal_id"] = proposal_values[min(len(completed), len(proposal_values) - 1)]
            completed.add(key)
    state["candidates"] = list(existing.values())[:30]
    state["completed_keys"] = sorted(completed)
    state["phase"] = phase
    state["next_action"] = next_action
    if failures:
        state["failed"] = [*list(state.get("failed") or []), *failures][-30:]
    work.state = json.dumps(state, ensure_ascii=False, default=str)


def _candidate_seed_query(city: City, candidate: dict[str, Any]) -> str:
    parts = [city.name_local, str(candidate.get("title") or "").strip()]
    address = str(candidate.get("address") or "").strip()
    if address:
        parts.append(address)
    parts.append("地址")
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()[:300]


def _compact_candidate_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff\uac00-\ud7a3]+", "", (value or "").casefold())


def _entity_text_matches(left: str, right: str) -> bool:
    """Require a meaningful shared entity/address fragment, not merely the city name."""
    a = _compact_candidate_text(left)
    b = _compact_candidate_text(right)
    for generic in ("辽宁省", "沈阳市", "沈阳", "中国", "地址", "추천", "장소"):
        token = _compact_candidate_text(generic)
        a = a.replace(token, "")
        b = b.replace(token, "")
    if not a or not b:
        return False
    if min(len(a), len(b)) >= 3 and (a in b or b in a):
        return True
    max_size = min(len(a), len(b), 20)
    for size in range(max_size, 2, -1):
        if any(a[start:start + size] in b for start in range(len(a) - size + 1)):
            return True
    return False


def _coordinate_record_matches_proposal(
    proposal: dict[str, Any],
    coordinate: dict[str, Any],
) -> bool:
    """Match the POI identity, never merely a shared mall/street address."""
    proposed_entity = " ".join(filter(None, [
        str(proposal.get("title") or ""),
        str(proposal.get("branch_name") or ""),
    ]))
    coordinate_entity = str(coordinate.get("display_name") or "")
    return _entity_text_matches(proposed_entity, coordinate_entity)


def _candidate_title_from_page(value: str) -> str:
    title = re.sub(r"\s+", " ", value or "").strip()
    bracket = re.match(r"^[【\[]([^】\]]{3,120})[】\]]", title)
    if bracket:
        title = bracket.group(1).strip()
    else:
        title = re.split(r"\s*(?:[-—_|｜]|_电话|电话_地址|地址_价格)\s*", title, maxsplit=1)[0].strip()
    title = re.sub(r"^(?:沈阳)?(?:SPA)?", "", title, flags=re.IGNORECASE).strip()
    return title[:160]


def _candidate_category(message: str, *, subject: str = "") -> str:
    if subject in {"food", "food_snack"}:
        return "restaurant"
    if subject == "drink":
        return "drink"
    if subject == "lodging":
        return "lodging"
    if subject == "transport":
        return "transport"
    folded = message.casefold()
    if any(term in folded for term in ("마사지", "按摩", "spa", "스파", "足疗", "洗浴")):
        return "other"
    if any(term in folded for term in ("음료", "카페", "奶茶", "咖啡", "요거트", "喜茶")):
        return "drink"
    if any(term in folded for term in ("식당", "맛집", "음식", "餐厅", "饭店", "美食")):
        return "restaurant"
    if any(term in folded for term in ("호텔", "숙소", "酒店", "宾馆")):
        return "lodging"
    if any(term in folded for term in ("역", "지하철", "공항", "站", "机场")):
        return "transport"
    return "tourist"


def _extract_address(*values: str) -> str:
    for value in values:
        match = re.search(r"(?:주소|地址)\s*[:：]\s*([^\n。；;]{4,140})", value or "")
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" ,，")[:180]
    return ""


def _source_host_quality(url: str) -> float:
    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    if any(host == item or host.endswith(f".{item}") for item in (
        "dianping.com", "meituan.com", "ctrip.com", "qunar.com", "trip.com",
        "360.cn",
        "shenyang.gov.cn", "ln.gov.cn", "bendibao.com", "maigoo.com",
    )):
        return 0.9
    return 0.55


def _candidate_matches_answer(title: str, answer: str) -> bool:
    title_key = _compact_candidate_text(title)
    answer_key = _compact_candidate_text(answer)
    if len(title_key) < 4 or not answer_key:
        return False
    if title_key in answer_key:
        return True
    # Search titles often add a city or category prefix.  Require a long shared
    # entity fragment so generic words such as "中街店" cannot create a candidate.
    for size in range(min(len(title_key), 18), 5, -1):
        if any(title_key[start:start + size] in answer_key for start in range(len(title_key) - size + 1)):
            return True
    return False


def _extract_grounded_candidates(
    answer: str,
    tool_results: list[dict[str, Any]],
    *,
    message: str,
    locked_candidate: dict[str, Any] | None = None,
    subject: str = "",
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    def add_candidate(title: str, url: str, text: str = "", quality: float = 0.0) -> None:
        clean_title = _candidate_title_from_page(title)
        if not clean_title or not _candidate_matches_answer(clean_title, answer):
            return
        key_text = _compact_candidate_text(clean_title)
        if len(key_text) < 4:
            return
        # Collapse SEO/title variants of the same business by a stable shared core.
        existing_key = next(
            (
                key for key in grouped
                if key in key_text or key_text in key or (
                    len(key) >= 6 and len(key_text) >= 6 and key[:6] == key_text[:6]
                )
            ),
            key_text,
        )
        row = grouped.setdefault(existing_key, {
            "title": clean_title,
            "address": "",
            "category": _candidate_category(message, subject=subject),
            "source_urls": [],
            "lat": None,
            "lng": None,
            "confidence": 0.0,
        })
        if len(clean_title) < len(str(row["title"])):
            row["title"] = clean_title
        if url.startswith(("http://", "https://")) and url not in row["source_urls"]:
            row["source_urls"].append(url)
        row["address"] = row["address"] or _extract_address(answer, text)
        row["confidence"] = max(float(row["confidence"]), quality or _source_host_quality(url))

    for item in tool_results:
        name = str(item.get("name") or "")
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        if name == "web_search":
            for hit in result.get("results") or []:
                if not isinstance(hit, dict):
                    continue
                add_candidate(
                    str(hit.get("title") or ""),
                    str(hit.get("href") or ""),
                    str(hit.get("body") or ""),
                    float(hit.get("quality") or 0),
                )
        elif name == "fetch_page":
            add_candidate(
                str(result.get("title") or ""),
                str(result.get("url") or item.get("args", {}).get("url") or ""),
                str(result.get("text") or ""),
                0.85,
            )

    candidates = list(grouped.values())
    if locked_candidate:
        locked_key = _compact_candidate_text(str(locked_candidate.get("title") or ""))
        matching = next(
            (row for row in candidates if locked_key in _compact_candidate_text(str(row["title"]))
             or _compact_candidate_text(str(row["title"])) in locked_key),
            None,
        )
        if matching is None:
            matching = dict(locked_candidate)
            matching["source_urls"] = list(matching.get("source_urls") or [])
            candidates.insert(0, matching)
        else:
            matching["title"] = str(locked_candidate.get("title") or matching["title"])
            matching["address"] = matching.get("address") or str(locked_candidate.get("address") or "")
            matching["source_urls"] = list(dict.fromkeys([
                *(locked_candidate.get("source_urls") or []),
                *(matching.get("source_urls") or []),
            ]))[:8]
        candidates = [matching]

    for candidate in candidates[:12]:
        title = str(candidate.get("title") or "")
        address = str(candidate.get("address") or "")
        title_key = _compact_candidate_text(title)
        for item in tool_results:
            if item.get("name") not in {"geocode_place", "fetch_page"}:
                continue
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            query = str((item.get("args") or {}).get("query") or (item.get("args") or {}).get("url") or "")
            if title_key and title_key not in _compact_candidate_text(query) and (
                not address or _compact_candidate_text(address) not in _compact_candidate_text(query)
            ):
                continue
            points = result.get("results") or result.get("coordinate_candidates") or []
            point = next(
                (hit for hit in points if isinstance(hit, dict) and hit.get("storage_allowed") is not False),
                None,
            )
            if point:
                try:
                    candidate["lat"] = float(point["lat"])
                    candidate["lng"] = float(point["lng"])
                    candidate["confidence"] = max(
                        float(candidate.get("confidence") or 0),
                        float(point.get("confidence") or 0.75),
                    )
                except (KeyError, TypeError, ValueError):
                    pass
                break
        candidate["source_urls"] = list(dict.fromkeys(candidate.get("source_urls") or []))[:8]
        candidate["key"] = hashlib.sha256(
            f"{title_key}|{_compact_candidate_text(address)}".encode("utf-8")
        ).hexdigest()[:16]
        candidate["status"] = "located" if candidate.get("lat") is not None else "location_needed"
        candidate["confidence"] = round(float(candidate.get("confidence") or 0.55), 3)
    candidates.sort(key=lambda row: (-float(row.get("confidence") or 0), str(row.get("title") or "")))
    return candidates[:12]


def _supporting_sources(answer: str, tool_results: list[dict[str, Any]]) -> list[str]:
    ranked: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    order = 0
    for item in tool_results:
        name = str(item.get("name") or "")
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        rows = result.get("results") or [] if name == "web_search" else [result]
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("href") or row.get("url") or (item.get("args") or {}).get("url") or "")
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            title = str(row.get("title") or "")
            if not _candidate_matches_answer(_candidate_title_from_page(title), answer):
                continue
            seen.add(url)
            quality = float(row.get("quality") or (0.9 if name == "fetch_page" else _source_host_quality(url)))
            ranked.append((quality, order, url))
            order += 1
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [url for _quality, _order, url in ranked[:8]]


def _compact_tool_trace(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for sequence, item in enumerate(tool_results, 1):
        result = item.get("result")
        result_dict = result if isinstance(result, dict) else {}
        urls: list[str] = []
        _collect_sources(result_dict, set_urls := set())
        urls.extend(sorted(set_urls)[:5])
        trace_item: dict[str, Any] = {
            "sequence": sequence,
            "tool": str(item.get("name") or "")[:80],
            "args": item.get("args") if isinstance(item.get("args"), dict) else {},
            "ok": bool(result_dict.get("ok")) or (
                not result_dict.get("error") and bool(result_dict.get("results") or result_dict.get("text"))
            ),
            "error": str(result_dict.get("error") or result_dict.get("detail") or "")[:300],
            "result_count": len(result_dict.get("results") or []),
            "urls": urls,
        }
        if item.get("name") == "semantic_intent":
            trace_item["decision"] = result_dict.get("intent")
            trace_item["work_id"] = result_dict.get("work_id")
        elif item.get("name") == "geocode_place":
            trace_item["matches"] = [
                {
                    "display_name": str(hit.get("display_name") or "")[:200],
                    "lat": hit.get("lat"),
                    "lng": hit.get("lng"),
                    "source": hit.get("source"),
                    "confidence": hit.get("confidence"),
                    "storage_allowed": hit.get("storage_allowed"),
                    "matched_query": hit.get("matched_query"),
                }
                for hit in (result_dict.get("results") or [])[:5]
                if isinstance(hit, dict)
            ]
        trace.append(trace_item)
    return trace[-40:]


def _food_business_name(city: City, query: str) -> str:
    """Extract the restaurant token from a restaurant/address search."""
    cleaned = re.sub(r"\s+", " ", query).strip()
    business = ""
    city_tokens = [f"{city.name_local}市", city.name_local, city.name_ko]
    for token in city_tokens:
        position = cleaned.find(token)
        if position > 0:
            business = cleaned[:position].strip()
            break
    if not business:
        business = cleaned
        for token in city_tokens:
            business = business.replace(token, " ")
        business = re.sub(r"\s+", " ", business).strip()
    business = re.split(r"\s+(?:地址|位于|주소|辽宁省|[\u4e00-\u9fff]{1,6}[区县])", business, maxsplit=1)[0]
    business = (business.split() or [""])[0].strip(" ,，:：-·")[:80]
    if (
        not business
        or re.search(r"\d|[区县路街号]$", business)
        or business in {"必吃", "美食", "特色美食", "老字号", "餐厅", "饭店", "小吃"}
    ):
        return ""
    return business


def _food_detail_recovery_query(city: City, query: str) -> str:
    """Reduce an address-heavy geocode query back to its business name."""
    business = _food_business_name(city, query)
    return f"{city.name_local} {business} 去哪儿攻略" if business else ""


def _brand_targets(message: str) -> list[str]:
    targets: list[str] = []
    folded = message.casefold()
    for name, aliases in BRAND_ANSWER_ALIASES.items():
        if any(alias.casefold() in folded for alias in aliases):
            targets.append(name)
    return targets


def _brand_source_urls(target: str, search_items: list[dict[str, Any]]) -> list[str]:
    """Keep URLs whose result text actually names the requested brand."""
    aliases = [
        re.sub(r"[^0-9a-z\u3400-\u9fff\uac00-\ud7a3]+", "", alias.casefold())
        for alias in BRAND_ANSWER_ALIASES[target]
    ]
    urls: list[str] = []
    for item in search_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("href") or "")
        if not url.startswith(("http://", "https://")):
            continue
        haystack = " ".join(
            str(item.get(field) or "") for field in ("title", "body", "href")
        ).casefold()
        normalized = re.sub(r"[^0-9a-z\u3400-\u9fff\uac00-\ud7a3]+", "", haystack)
        if any(alias and alias in normalized for alias in aliases):
            urls.append(url)
    return list(dict.fromkeys(urls))


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


def _research_seed_queries(city: City, message: str, *, subject: str = "") -> list[str]:
    """Prefer short Chinese brand queries; long Korean action sentences produce noisy results."""
    queries: list[str] = []
    folded = message.casefold()
    if any(alias.casefold() in folded for alias in BRAND_ANSWER_ALIASES["모어요거트"]):
        queries.append(f"{city.name_local} {BRAND_STORE_SEARCHES['모어요거트']} 地址")
    if any(alias.casefold() in folded for alias in BRAND_ANSWER_ALIASES["헤이티"]):
        queries.append(f"{city.name_local} {BRAND_STORE_SEARCHES['헤이티']} 地址")
    if not queries and any(term in folded for term in ("마사지", "스파", "按摩", "spa", "足疗", "洗浴")):
        area = " 中街" if any(term in folded for term in ("중제", "中街")) else ""
        queries.append(f"{city.name_local}{area} 正规 洗浴 按摩 推荐")
    if not queries and subject == "food_snack":
        queries.extend([
            f"site:you.ctrip.com/food/shenyang155 {city.name_local} 中街 小吃 外带",
            f"site:touch.travel.qunar.com/poi {city.name_local} 小吃 甜品 糕点",
            f"{city.name_local} 老字号 糕点 点心 门店 地址",
        ])
    if not queries and subject == "food":
        queries.extend([
            f"{city.name_local} 必吃 特色美食 传统小吃",
            f"{city.name_local} 老字号 特色餐厅 推荐",
        ])
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
        .join(TravelPlan, TravelPlan.id == TravelPlanItem.plan_id)
        .options(
            joinedload(TravelPlanItem.place).joinedload(Marker.zone),
            joinedload(TravelPlanItem.plan_day),
            joinedload(TravelPlanItem.creator),
        )
        .filter(
            TravelPlanItem.city_id == city_id,
            TravelPlan.visibility == "city_shared",
            TravelPlan.status != "archived",
        )
        .order_by(TravelPlanItem.plan_day_id, TravelPlanItem.start_time, TravelPlanItem.sort_order, TravelPlanItem.id)
        .all()
    )
    return json.dumps(
        [
            {
                "date": row.plan_day.calendar_date.isoformat() if row.plan_day else None,
                "start_time": row.start_time.isoformat(timespec="minutes") if row.start_time else None,
                "end_time": row.end_time.isoformat(timespec="minutes") if row.end_time else None,
                "legacy_day": row.day if row.plan_day is None else None,
                "legacy_slot": row.slot if row.plan_day is None else None,
                "place_id": row.place_id,
                "title": row.place.title if row.place else "",
                "added_by": row.creator.display_name if row.creator else "",
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

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_chat_model or settings.groq_model or "openai/gpt-oss-20b"
    active_work = _active_chat_work(db, user_id=user_id, city_id=city_id)
    intent = _classify_chat_intent(
        client,
        model=model,
        message=message,
        recent_requests=_recent_user_requests(prior, limit=10),
        active_work=active_work,
    )
    intent = _inherit_active_intent(intent, active_work)
    active_work = _ensure_chat_work(
        db,
        user_id=user_id,
        city_id=city_id,
        intent=intent,
        active_work=active_work,
    )
    if active_work is not None and intent.continuation and not _work_candidates(active_work):
        _merge_work_candidates(
            active_work,
            _latest_chat_candidates(prior, subject=intent.subject),
            phase="research",
            next_action="write" if intent.wants_write else "research",
        )
        db.commit()
        db.refresh(active_work)
    resolved_message = _resolve_context_message(
        message,
        prior,
        intent=intent,
        work=active_work,
    )
    write_intent, allowed = _chat_capabilities(intent)
    pending_work = (
        _pending_work_candidates(active_work, target_keys=intent.target_keys)
        if intent.continuation or intent.wants_write
        else []
    )
    locked_candidate = pending_work[0] if len(pending_work) == 1 else None
    food_discovery = intent.subject in {"food", "food_snack"}
    snack_discovery = intent.subject == "food_snack"
    food_place_label = "간식 판매점" if snack_discovery else "식당"
    brand_targets = _brand_targets(resolved_message)
    travel_profile = build_user_travel_profile(db, user_id=user_id, city_id=city_id)
    existing_target_places: dict[str, Marker] = {}
    for target in brand_targets:
        for marker in markers:
            searchable = f"{marker.title} {marker.description or ''}".casefold()
            if any(alias.casefold() in searchable for alias in BRAND_ANSWER_ALIASES[target]):
                existing_target_places[target] = marker
                break
    if write_intent and brand_targets and set(brand_targets).issubset(existing_target_places):
        allowed = set()
    system = f"""당신은 WONRAE(遠來)의 {city.name_ko}({city.name_local}) 여행 설계자다.
목표는 많이 나열하는 것이 아니라 실제 이틀 여행에서 좋은 선택과 동선을 주는 것이다.

규칙:
- 답은 자연스러운 한국어로, 먼저 결론을 짧게 말한다.
- 답은 보통 250~600자, 복잡한 일정도 1,000자를 넘기지 않는다. 마크다운 표·별표 문자(*)·해시 제목·구분선을 쓰지 말고 짧은 문단과 번호 목록만 쓴다.
- 아래 운영 지도 DB를 최우선 사실로 사용하고, 언급한 등록 장소에는 반드시 `장소 #ID`를 붙인다.
- 저장 장소의 위치·구역·역사·방문 팁을 서로 연결한다. 일정이 있으면 이동 부담과 시간대까지 고려한다.
- 사용자 여행 행동 프로필은 반복 대화·즐겨찾기·직접 추가·일정·이의제기에서 계산된 개인화 힌트다. 확정된 취향처럼 단정하지 말고 추천 이유를 설명한다.
- 반복 요청한 브랜드가 있으면 같은 브랜드의 다른 유용한 지점과 비슷한 현지 음료 브랜드를, 숙소 거점이 있으면 반경과 구역을 고려한 음식·관광 후보를 우선한다.
- 이의제기는 싫어함이 아니라 기존 판단에 대한 교정 조건이다. corrections_not_dislikes를 다음 답변과 행동에서 존중한다.
- DB의 위도·경도는 동선의 상대적 가까움만 판단하는 데 쓰고, 라우팅 도구 없이 정확한 이동 분수나 교통편을 지어내지 않는다.
- 현재 지도에 있는 정보와 방금 웹에서 찾은 정보를 명확히 구분한다.
- 영업시간·휴무·예약·가격처럼 변하는 정보는 web_search 후 가능하면 fetch_page로 확인하고 URL을 답에 붙인다.
- DB나 방금 읽은 출처에 없는 이동시간·지하철역/노선·가격·결제수단·대여 서비스·음식 메뉴를 추측하지 않는다. 필요하면 먼저 검색하고, 검색하지 않았으면 확인이 필요하다고 짧게 밝힌다.
- 중국어 원명으로도 검색하고, 한 블로그를 유일한 근거로 삼지 않는다.
- 박물관 편중을 피하고 history, food, market_night, neighborhood, nature, shopping, rest, practical 역할을 균형 있게 본다.
- 현재 지도의 편중 이유를 물으면 도시 정책을 지어내지 말고, 기존 자동 연구가 역사 명소와 박물관 건수에 높은 성과를 주었던 수집 편향이 원인이라고 솔직히 설명한다.
- 지도에 음식 장소가 없으면 임의의 식당이나 메뉴를 일정에 넣지 말고 `현재 지도에 음식 데이터가 비어 있다`고 말한 뒤 조사/추가를 제안한다.
- 사용자가 명시적으로 지도 저장/추가를 요청한 경우에만 propose_place 또는 upsert_place_insights를 사용한다. 신규 장소는 곧바로 공개하지 않고 승인 제안으로 저장한다.
- 웹 조사가 필요한 질문은 중국어 원명 검색을 포함하되 검색어를 2~4개 핵심 후보로 좁힌다. 같은 검색을 반복하지 말고, 일반 조사는 3번·저장 요청은 최대 8번의 도구 왕복 안에 조사·좌표 확인·저장 제안을 마친 뒤 반드시 답한다.
- 단순 추천·동선·설명 질문에는 웹 도구가 제공되지 않을 수 있다. 그 경우 현재 지도 DB와 일정만으로 바로 답한다.
- 근거가 부족하면 모른다고 말하고 확인 방법을 제시한다. 도구의 내부 JSON은 노출하지 않는다.

현재 지도 DB: {map_context}
현재 도시 공용 일정표: {_plan_context(db, user_id=user_id, city_id=city_id)}
현재 사용자 여행 행동 프로필: {profile_prompt_context(travel_profile)}
현재 선택 장소 ID: {selected_place_id or '없음'}
현재 요청의 구조화된 의미: {json.dumps(asdict(intent), ensure_ascii=False, default=str)}
현재 이어가는 작업 원장: {json.dumps(_work_summary(active_work) if intent.continuation else None, ensure_ascii=False, default=str)}
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
    if intent.continuation:
        messages.append({
            "role": "system",
            "content": (
                "현재 말은 짧은 후속 명령이다. 아래처럼 직전의 구체적 사용자 요청과 결합해 해석했다. "
                "현재 지시를 새 질문처럼 버리지 말고 실제 행동까지 이어가라:\n" + resolved_message
            ),
        })
    if pending_work:
        messages.append({
            "role": "system",
            "content": (
                "이번 후속 명령이 이어갈 미완료 후보 목록이다. 다른 업소나 비슷한 상호로 대체하지 마라. "
                "scope가 remaining/all이면 한 곳 처리 후 끝내지 말고 이 목록의 검증 가능한 후보를 모두 처리하라. "
                "이름·주소·출처와 key를 유지하고, 저장 요청이면 각 후보를 propose_place로 처리하라:\n"
                + json.dumps(pending_work, ensure_ascii=False, default=str)
            ),
        })
    messages.append({"role": "user", "content": message})
    sources: set[str] = set()
    final_text = ""
    seen_tool_calls: set[str] = set()
    tool_results: list[dict[str, Any]] = [{
        "name": "semantic_intent",
        "args": {"message": message},
        "result": {
            "ok": True,
            "intent": asdict(intent),
            "work_id": active_work.id if active_work else None,
        },
    }]
    proposal_ids: list[int] = []
    proposal_titles: list[str] = []
    successful_write_candidates: list[dict[str, Any]] = []
    existing_write_place_ids: list[int] = []
    completed_work_candidates = [
        item for item in _work_candidates(active_work)
        if str(item.get("status") or "") in {"proposed", "mapped", "approved", "duplicate"}
    ]
    write_attempted = False
    write_succeeded = bool(existing_target_places or completed_work_candidates)
    successful_write_count = len(existing_target_places) + len(completed_work_candidates)
    successful_candidate_keys: set[str] = {
        str(item.get("key") or "") for item in completed_work_candidates if item.get("key")
    }
    successful_write_targets: set[str] = set(existing_target_places)
    actionable_targets: set[str] = set()
    verified_coordinates: list[tuple[float, float]] = []
    verified_coordinate_records: list[dict[str, Any]] = []
    target_business_sources: dict[str, list[str]] = {}
    fetched_food_urls: set[str] = set()
    fetched_source_urls: set[str] = set()
    food_detail_queries: set[str] = set()
    food_business_names: list[str] = []
    food_geo_candidates: list[dict[str, Any]] = []
    city_bounds = parse_viewbox(city.search_viewbox or "")
    for item in pending_work:
        try:
            lat, lng = float(item["lat"]), float(item["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        verified_coordinates.append((lat, lng))
        verified_coordinate_records.append({
            "lat": lat,
            "lng": lng,
            "query": " ".join(filter(None, [
                str(item.get("title") or ""),
                str(item.get("address") or ""),
            ])),
            "display_name": str(item.get("title") or ""),
            "confidence": float(item.get("confidence") or 0.5),
        })

    def add_food_geo_candidate(query: str, results: list[dict[str, Any]]) -> None:
        if not results:
            return
        label = str(results[0].get("display_name") or query)
        identity = re.sub(r"[^0-9a-z\u3400-\u9fff\uac00-\ud7a3]+", "", label.casefold())
        identity = identity.replace(city.name_local.casefold(), "").replace(city.name_ko.casefold(), "")
        identity = re.sub(r"^(?:20\d{2})+", "", identity)
        # Detail-page titles contain long SEO suffixes. The first four Han/Hangul
        # characters are stable enough to collapse the same branch from OSM,
        # Ctrip and Qunar without merging unrelated nearby restaurants.
        identity_prefix = identity[:4]
        for marker in markers:
            marker_identity = re.sub(
                r"[^0-9a-z\u3400-\u9fff\uac00-\ud7a3]+",
                "",
                str(marker.title or "").casefold(),
            )
            marker_identity = marker_identity.replace(city.name_local.casefold(), "").replace(
                city.name_ko.casefold(), ""
            )
            if identity_prefix and marker_identity[:4] == identity_prefix:
                return
        if identity_prefix and any(
            str(item.get("identity") or "")[:4] == identity_prefix
            for item in food_geo_candidates
        ):
            return
        food_geo_candidates.append({
            "query": query[:300],
            "identity": identity,
            "results": results[:3],
        })

    def register_page_coordinates(fetch_result: Any, *, query: str) -> None:
        coordinate_rows = (
            fetch_result.get("coordinate_candidates") or []
            if isinstance(fetch_result, dict)
            else []
        )
        grounded: list[dict[str, Any]] = []
        for coordinate in coordinate_rows:
            if not isinstance(coordinate, dict) or coordinate.get("storage_allowed") is False:
                continue
            try:
                point = (float(coordinate["lat"]), float(coordinate["lng"]))
            except (KeyError, TypeError, ValueError):
                continue
            if city_bounds is not None and not city_bounds.contains(point[0], point[1]):
                continue
            verified_coordinates.append(point)
            verified_coordinate_records.append({
                "lat": point[0],
                "lng": point[1],
                "query": query,
                "display_name": str(coordinate.get("display_name") or fetch_result.get("title") or query),
                "confidence": float(coordinate.get("confidence") or 0.5),
            })
            grounded.append({
                "display_name": str(fetch_result.get("title") or query)[:300],
                **coordinate,
            })
        add_food_geo_candidate(query, grounded)

    def fetch_food_evidence(search_result: Any, *, limit: int = 2) -> list[dict[str, Any]]:
        """Read promising search hits immediately so research cannot stall at snippets."""
        if not food_discovery or not isinstance(search_result, dict):
            return []
        fetched: list[dict[str, Any]] = []
        search_items = [item for item in (search_result.get("results") or []) if isinstance(item, dict)]

        def evidence_priority(item: dict[str, Any]) -> tuple[int, int]:
            url = str(item.get("href") or "").casefold()
            title = str(item.get("title") or "")
            score = 0
            if "ctrip.com/food" in url or "ctrip.com/html5/you/foods/fooddetail" in url:
                score += 12
            if "qunar.com" in url and "/poi/" in url:
                score += 12
            if any(host in url for host in ("bendibao.com", "maigoo.com", "gov.cn")):
                score += 4
            if any(term in title for term in ("地址", "攻略", "餐厅", "饭店", "老字号")):
                score += 2
            return (-score, search_items.index(item))

        for item in sorted(search_items, key=evidence_priority):
            if len(fetched) >= limit or len(fetched_food_urls) >= 16:
                break
            url = str(item.get("href") or "")
            if not url.startswith(("http://", "https://")) or url in fetched_food_urls:
                continue
            fetched_food_urls.add(url)
            fetch_args = {"url": url}
            fetch_result = run_tool(db, "fetch_page", fetch_args, city_id=city_id)
            if isinstance(fetch_result, dict) and (
                fetch_result.get("text") or fetch_result.get("coordinate_candidates")
            ):
                fetched_source_urls.add(url)
            seen_tool_calls.add(
                f"fetch_page:{json.dumps(fetch_args, ensure_ascii=False, sort_keys=True, default=str)}"
            )
            _collect_tool_sources("fetch_page", fetch_result, sources)
            tool_results.append({"name": "fetch_page", "args": fetch_args, "result": fetch_result})
            fetched.append({
                "search_title": str(item.get("title") or "")[:300],
                "url": url,
                "page": fetch_result,
            })
            register_page_coordinates(
                fetch_result,
                query=str(item.get("title") or "")[:300],
            )
        return fetched

    food_bootstrap_pages: list[dict[str, Any]] = []
    if food_discovery and write_intent:
        bootstrap_sources = (
            CITY_SNACK_DETAIL_SOURCES if snack_discovery else CITY_FOOD_DETAIL_SOURCES
        )
        for url in bootstrap_sources.get(city.slug, ()):
            if len(food_geo_candidates) >= 2:
                break
            if url in fetched_food_urls:
                continue
            fetched_food_urls.add(url)
            fetch_args = {"url": url}
            fetch_result = run_tool(db, "fetch_page", fetch_args, city_id=city_id)
            if isinstance(fetch_result, dict) and (
                fetch_result.get("text") or fetch_result.get("coordinate_candidates")
            ):
                fetched_source_urls.add(url)
            seen_tool_calls.add(
                f"fetch_page:{json.dumps(fetch_args, ensure_ascii=False, sort_keys=True, default=str)}"
            )
            _collect_tool_sources("fetch_page", fetch_result, sources)
            tool_results.append({"name": "fetch_page", "args": fetch_args, "result": fetch_result})
            register_page_coordinates(fetch_result, query=url)
            food_bootstrap_pages.append({"url": url, "page": fetch_result})

    def writes_complete() -> bool:
        if not write_intent:
            return True
        if brand_targets:
            return set(brand_targets).issubset(successful_write_targets)
        if pending_work:
            return {
                str(item.get("key") or "") for item in pending_work if item.get("key")
            }.issubset(successful_candidate_keys)
        target_count = intent.requested_count or (2 if food_discovery else 1)
        return successful_write_count >= target_count

    def complete(
        *,
        tool_names: set[str] | None = None,
        tool_choice: Any = "auto",
    ) -> Any:
        nonlocal model
        kwargs: dict[str, Any] = {}
        if "gpt-oss" in model:
            kwargs["extra_body"] = {"reasoning_effort": "medium"}
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.0 if tool_choice != "auto" else 0.2,
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

    def complete_text(fallback: str) -> str:
        """A model trying to call tools during final synthesis must never become an HTTP 500."""
        try:
            response = complete()
            reply = response.choices[0].message
            return (reply.content or fallback).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "travel_chat final synthesis failed user=%s city=%s error=%s",
                user_id,
                city_id,
                str(exc)[:400],
            )
            return fallback

    seed_results: list[dict[str, Any]] = []
    if allowed:
        if pending_work:
            seed_queries = [_candidate_seed_query(city, item) for item in pending_work[:6]]
        elif food_discovery:
            seed_queries = _research_seed_queries(city, resolved_message, subject=intent.subject)
        else:
            seed_queries = _research_seed_queries(city, resolved_message, subject=intent.subject)
        for seed_query in seed_queries:
            seed_args = {"query": seed_query, "max_results": 8}
            seed_result = run_tool(db, "web_search", seed_args, city_id=city_id)
            _collect_tool_sources("web_search", seed_result, sources)
            seen_tool_calls.add(
                f"web_search:{json.dumps(seed_args, ensure_ascii=False, sort_keys=True, default=str)}"
            )
            seed_results.append({
                "query": seed_query,
                "result": seed_result,
                "fetched_pages": fetch_food_evidence(seed_result, limit=1),
            })
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
        if food_discovery:
            messages.append({
                "role": "system",
                "content": (
                    f"이번 요청은 {'한 끼 식사가 아닌 간식 발견' if snack_discovery else '음식 발견'} 작업이다. "
                    + (
                        "일반 식당과 완전한 식사 메뉴는 제외한다. 포장하거나 걸으며 조금씩 먹기 좋은 현지 小吃·"
                        "糕点·甜品·烘焙·零食의 구체적인 판매점/지점을 찾는다. 카테고리는 실제 업종에 따라 "
                        "restaurant·drink·shopping 중 고르고 travel_role=food로 저장한다. "
                        if snack_discovery else
                        "1) 선양을 대표하는 음식 유형을 먼저 2~4개로 좁히고, 2) 각 유형을 실제로 파는 "
                        "구체적인 식당/지점을 찾는다. "
                    )
                    + "페이지 근거와 지오코딩 좌표를 검증한 뒤, 저장 요청이면 서로 다른 "
                    f"{food_place_label}을 최소 2건 propose_place로 승인 대기에 저장하라. "
                    "검색 결과 제목만으로 주소나 지점을 만들어내지 말고, 검증이 안 된 후보는 저장하지 마라. "
                    "다음은 이 도시의 로컬 지식 소스에서 방금 다시 읽어 좌표까지 검증한 상세 페이지다. "
                    "현재 지도 DB에 없는 후보만 우선 저장하고 동적 검색으로 보완하라:\n"
                    + json.dumps(food_bootstrap_pages, ensure_ascii=False, default=str)[:14000]
                ),
            })
            if snack_discovery:
                messages.append({
                    "role": "system",
                    "content": (
                        "좌표 복구 절차: 정확한 중국어 상호와 지점을 고른 뒤 geocode_place가 비어 있으면 "
                        "그 상호 + 주소 단서 + 360地图로 web_search하라. 검색 결과의 "
                        "m.map.360.cn/m/search/detail 상세 페이지를 fetch_page로 읽으면 "
                        "coordinate_candidates에 GCJ-02에서 WGS84로 변환된 검증 좌표가 들어온다. "
                        "좌표는 그 값을 그대로 사용하고 임의 추정하지 마라. 메뉴·간식 성격은 Ctrip/Qunar 같은 "
                        "별도 상세 출처로 교차 확인하고, 360 지도는 지점명·주소·좌표 근거로 사용하라."
                    ),
                })
        if write_intent and brand_targets:
            enrichment: list[dict[str, Any]] = []
            chinese_names = {"모어요거트": "茉酸奶", "헤이티": "喜茶 HEYTEA"}
            geo_queries = {
                "모어요거트": [
                    f"{city.name_local}大悦城 茉酸奶旗舰店",
                    f"{city.name_local}大悦城C区",
                    f"{city.name_local}大悦城",
                ],
                "헤이티": [
                    f"{city.name_local}大悦城 喜茶",
                    f"{city.name_local}大悦城C区",
                    f"{city.name_local}大悦城",
                ],
            }
            for target in brand_targets:
                related_seed = next(
                    (
                        item
                        for item in seed_results
                        if chinese_names[target].split()[0] in item["query"]
                    ),
                    None,
                )
                search_items = (
                    (related_seed.get("result") or {}).get("results") or []
                    if related_seed
                    else []
                )
                target_business_sources[target] = _brand_source_urls(target, search_items)
                fetched: list[dict[str, Any]] = []
                for item in search_items[:2]:
                    url = str(item.get("href") or "") if isinstance(item, dict) else ""
                    if not url.startswith(("http://", "https://")):
                        continue
                    fetch_result = run_tool(db, "fetch_page", {"url": url}, city_id=city_id)
                    if isinstance(fetch_result, dict) and (
                        fetch_result.get("text") or fetch_result.get("coordinate_candidates")
                    ):
                        fetched_source_urls.add(url)
                    _collect_tool_sources("fetch_page", fetch_result, sources)
                    tool_results.append({"name": "fetch_page", "args": {"url": url}, "result": fetch_result})
                    fetched.append(fetch_result)
                    if isinstance(fetch_result, dict) and fetch_result.get("text"):
                        break
                geo_result: dict[str, Any] = {"results": []}
                geo_hits: list[dict[str, Any]] = []
                geo_attempts: list[dict[str, Any]] = []
                for geo_query in geo_queries[target]:
                    geo_args = {"query": geo_query, "limit": 5}
                    candidate_result = run_tool(db, "geocode_place", geo_args, city_id=city_id)
                    if not isinstance(candidate_result, dict):
                        candidate_result = {"results": [], "error": "invalid_geocode_result"}
                    _collect_tool_sources("geocode_place", candidate_result, sources)
                    tool_results.append({
                        "name": "geocode_place",
                        "args": geo_args,
                        "result": candidate_result,
                    })
                    geo_attempts.append({"query": geo_query, "result": candidate_result})
                    candidate_hits = candidate_result.get("results") or []
                    if candidate_hits:
                        geo_result = candidate_result
                        geo_hits = candidate_hits
                        break
                for hit in geo_hits:
                    if isinstance(hit, dict) and hit.get("storage_allowed") is not False:
                        try:
                            point = (float(hit["lat"]), float(hit["lng"]))
                        except (KeyError, TypeError, ValueError):
                            continue
                        verified_coordinates.append(point)
                        verified_coordinate_records.append({
                            "lat": point[0],
                            "lng": point[1],
                            "query": geo_query,
                            "display_name": str(hit.get("display_name") or ""),
                            "confidence": float(hit.get("confidence") or 0.5),
                        })
                if target_business_sources[target] and geo_hits:
                    actionable_targets.add(target)
                enrichment.append({
                    "target": target,
                    "search_results": search_items[:5],
                    "fetched_pages": fetched,
                    "geocode": geo_result,
                    "geocode_attempts": geo_attempts,
                    "actionable": target in actionable_targets,
                })
            messages.append({
                "role": "system",
                "content": (
                    "서버가 저장 요청 대상을 각각 페이지 열람·다중 지오코딩까지 실행한 결과다. actionable=true인 대상만 "
                    "실제 지점명·근거 URL·좌표 후보가 준비된 것이다. false인 대상은 추측해 저장하지 마라. "
                    "Each proposal source_urls must include at least one business-specific URL from that target's "
                    "search_results. OpenStreetMap alone proves coordinates, not that the business exists:\n"
                    + json.dumps(enrichment, ensure_ascii=False, default=str)[:24000]
                ),
            })

    force_required = False
    tool_round_limit = (
        14 if food_discovery and write_intent and not brand_targets
        else MAX_WRITE_TOOL_ROUNDS if write_intent
        else MAX_RESEARCH_TOOL_ROUNDS
    )
    for _round in range(tool_round_limit if allowed else 0):
        structured_brand_write = write_intent and bool(brand_targets)
        if structured_brand_write:
            remaining_targets = [
                target
                for target in brand_targets
                if target in actionable_targets and target not in successful_write_targets
            ]
            if not remaining_targets:
                break
            round_tools = {"propose_place"}
            round_choice = {
                "type": "function",
                "function": {"name": "propose_place"},
            }
            messages.append({
                "role": "system",
                "content": (
                    "검색과 좌표 확인은 서버가 끝냈다. 이제 다른 도구를 요구하지 말고 propose_place만 호출해 "
                    f"다음 대상을 실제 승인 대기에 저장하라: {remaining_targets}. actionable=false 대상은 저장하지 마라. "
                    "도구가 오류를 반환하면 내용을 고쳐 다음 호출에서 재시도하라."
                ),
            })
        elif pending_work and write_intent:
            remaining = [
                item for item in pending_work
                if str(item.get("key") or "") not in successful_candidate_keys
            ]
            ready = [
                item for item in remaining
                if item.get("lat") is not None and item.get("lng") is not None
                and item.get("source_urls")
            ]
            if ready:
                round_tools = set(allowed)
                round_choice = {"type": "function", "function": {"name": "propose_place"}}
                messages.append({
                    "role": "system",
                    "content": (
                        "활성 작업의 아래 후보는 출처와 좌표가 준비됐다. 검색을 반복하지 말고 이번 호출에서 "
                        "propose_place를 사용해 승인 대기에 저장하라. 한 응답에서 최대 4개 도구를 병렬 호출할 수 "
                        "있으므로 remaining/all 요청은 하나씩 끊지 말고 처리한다. 후보 key와 대상을 바꾸지 마라:\n"
                        + json.dumps(ready[:4], ensure_ascii=False, default=str)
                    ),
                })
            else:
                round_tools = set(allowed)
                round_choice = "required" if force_required else "auto"
                messages.append({
                    "role": "system",
                    "content": (
                        "활성 작업의 미완료 후보를 이어서 처리하라. 각 후보의 기존 출처를 열고 정확한 주소와 "
                        "좌표를 확인한 뒤 propose_place까지 진행한다. 새로운 주제로 바꾸거나 이미 완료한 후보를 "
                        "다시 조사하지 마라. 미완료 후보:\n"
                        + json.dumps(remaining[:8], ensure_ascii=False, default=str)
                    ),
                })
        elif (
            food_discovery
            and write_intent
            and len(food_geo_candidates) > len(set(proposal_ids))
        ):
            # Keep every permitted schema available because the model can
            # legitimately decide that a coordinate-bearing page still lacks a
            # concrete branch identity. Requiring *a* tool preserves forward
            # progress without the provider-level 400 caused by forcing
            # propose_place while the model requests another search/fetch.
            round_tools = set(allowed)
            round_choice = "required"
            pending_geo = food_geo_candidates[len(set(proposal_ids)):]
            messages.append({
                "role": "system",
                "content": (
                    f"검증 가능한 {food_place_label} 좌표 후보가 준비됐다. 구체적인 상호·지점과 근거가 충분하면 "
                    "propose_place로 관리자 승인 대기에 실제 저장하라. 아직 페이지가 음식 종류만 설명하거나 "
                    "지점 정체성이 불명확하면 필요한 검색·페이지 열람을 한 번 더 하고, 다음 단계에서 저장하라. "
                    "title은 '중국어 원명 (한국어 음역·지점명)' 형식으로 쓰고 中街는 중제라고 "
                    "표기한다. 总店은 본점, 熏肉大饼은 훈제고기 전병처럼 자연스럽게 번역하고 기계식 한자음을 "
                    "쓰지 않는다. 역사 연도와 수식어는 상세 페이지 본문에 명시된 경우에만 출처를 밝혀 쓴다. "
                    + (
                        "완전한 식사 식당은 제안하지 말고 실제 업종에 따라 category=restaurant|drink|shopping, "
                        if snack_discovery else "category=restaurant, "
                    )
                    + "travel_role=food로 쓰고, "
                    + (
                        "consumption_mode는 snack|dessert|drink|packaged 중 실제 값으로 명시하고, "
                        if snack_discovery else ""
                    )
                    + "자동으로 읽은 페이지의 실제 URL을 source_urls와 최소 2개 insights에 넣어라. 아직 저장하지 않은 후보만 "
                    f"처리한다. 좌표 후보: {json.dumps(pending_geo[-2:], ensure_ascii=False, default=str)[:9000]}"
                ),
            })
        else:
            # Initial server-side search is only a seed. Multi-hop requests such as
            # food type -> restaurant -> exact branch need targeted searches the
            # model discovers later; hiding web_search caused Groq 400s.
            round_tools = set(allowed)
            round_choice = "required" if force_required else "auto"
            if food_discovery and write_intent and _round >= 2:
                messages.append({
                    "role": "system",
                    "content": (
                        f"넓은 {'간식 종류' if snack_discovery else '음식 종류'} 검색만 반복하지 마라. 지금까지 읽은 "
                        f"페이지에서 구체적인 {food_place_label} 상호를 하나 골라 "
                        "정확한 지점명·주소를 검색/fetch_page로 확인하고 geocode_place를 실행하라. 이미 한 후보를 "
                        f"저장했다면 서로 다른 두 번째 {food_place_label}을 같은 순서로 처리하라. web_search 인자는 query와 "
                        "max_results만 사용한다."
                    ),
                })
        try:
            response = complete(tool_names=round_tools, tool_choice=round_choice)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "travel_chat tool planning failed user=%s city=%s round=%s error=%s",
                user_id,
                city_id,
                _round + 1,
                str(exc)[:400],
            )
            tool_results.append({
                "name": "tool_planning",
                "args": {"round": _round + 1},
                "result": {"error": str(exc)[:300]},
            })
            force_required = True
            continue
        reply = response.choices[0].message
        calls = reply.tool_calls or []
        if not calls:
            if structured_brand_write and not writes_complete():
                messages.append({
                    "role": "system",
                    "content": "아직 요청 대상 전체의 저장 성공이 확인되지 않았다. 답변하지 말고 현재 단계의 도구를 실행하라.",
                })
                continue
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
            if name == "web_search" and food_discovery:
                business_name = _food_business_name(city, str(args.get("query") or ""))
                if business_name and business_name not in food_business_names:
                    food_business_names.append(business_name)
            signature = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"
            if index >= MAX_TOOL_CALLS_PER_ROUND:
                result: Any = {"error": "이번 답변의 조사 예산을 넘었습니다. 현재 결과로 답을 정리하세요."}
            elif signature in seen_tool_calls:
                result = {"error": "이미 실행한 동일 도구 요청입니다. 반복하지 말고 현재 결과로 답하세요."}
            elif name not in allowed:
                result = {"error": "이 대화에서 허용되지 않은 작업입니다"}
            else:
                unsupported_evidence: list[str] = []
                unread_fact_sources: list[str] = []
                missing_business_evidence = False
                coordinate_mismatch = False
                coordinate_target_unmatched = False
                snack_scope_invalid = False
                snack_scope_check: dict[str, Any] | None = None
                if name == "propose_place":
                    if snack_discovery:
                        snack_scope_check = _classify_snack_fit(
                            client,
                            model=model,
                            candidate=args,
                        )
                        tool_results.append({
                            "name": "snack_scope_check",
                            "args": {"title": str(args.get("title") or "")[:200]},
                            "result": snack_scope_check,
                        })
                        snack_scope_invalid = not bool(snack_scope_check.get("allowed"))
                        args["consumption_mode"] = snack_scope_check.get("consumption_mode", "unknown")
                    if pending_work:
                        proposed_key = _compact_candidate_text(str(args.get("title") or ""))
                        matching_work_target = next((
                            item for item in pending_work
                            if proposed_key and _compact_candidate_text(str(item.get("title") or ""))
                            and (
                                proposed_key in _compact_candidate_text(str(item.get("title") or ""))
                                or _compact_candidate_text(str(item.get("title") or "")) in proposed_key
                            )
                        ), None)
                        if matching_work_target is None:
                            result = {
                                "error": "candidate_target_changed",
                                "detail": (
                                    "후속 명령은 작업 원장에 보존된 후보에 고정되어 있습니다. 다른 업소로 "
                                    "바꾸지 말고 미완료 후보만 제안하세요."
                                ),
                            }
                            tool_results.append({"name": name, "args": args, "result": result})
                            messages.append({
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": json.dumps(result, ensure_ascii=False),
                            })
                            continue
                    serialized_args = json.dumps(args, ensure_ascii=False, default=str).casefold()
                    proposed_targets = [
                        target
                        for target in brand_targets
                        if any(
                            alias.casefold() in serialized_args
                            for alias in BRAND_ANSWER_ALIASES[target]
                        )
                    ]
                    selected_business_urls = list(dict.fromkeys(
                        url
                        for target in proposed_targets
                        for url in target_business_sources.get(target, [])
                    ))[:3]
                    args["source_urls"] = list(dict.fromkeys([
                        str(url)
                        for url in (args.get("source_urls") or [])
                        if str(url) in sources
                    ] + selected_business_urls))
                    supplied_urls = [str(url) for url in (args.get("source_urls") or [])]
                    supplied_urls.extend(
                        str(item.get("source_url") or "")
                        for item in (args.get("insights") or [])
                        if isinstance(item, dict)
                    )
                    coordinate_url = str(args.get("coordinate_source_url") or "")
                    if coordinate_url:
                        supplied_urls.append(coordinate_url)
                    unsupported_evidence = [
                        url for url in supplied_urls if url and url not in sources
                    ]
                    unread_fact_sources = [
                        str(item.get("source_url") or "")
                        for item in (args.get("insights") or [])
                        if isinstance(item, dict)
                        and str(item.get("source_url") or "")
                        and str(item.get("source_url") or "") not in fetched_source_urls
                    ]
                    missing_business_evidence = bool(proposed_targets) and not any(
                        url in target_business_sources.get(target, [])
                        for target in proposed_targets
                        for url in supplied_urls
                    )
                    relevant_coordinates = [
                        item for item in verified_coordinate_records
                        if _coordinate_record_matches_proposal(args, item)
                    ]
                    if not relevant_coordinates:
                        coordinate_target_unmatched = True
                    else:
                        try:
                            proposed_lat = float(args["lat"])
                            proposed_lng = float(args["lng"])
                            coordinate_mismatch = min(
                                ((proposed_lat - float(item["lat"])) * 111_000) ** 2
                                + ((proposed_lng - float(item["lng"])) * 85_000) ** 2
                                for item in relevant_coordinates
                            ) ** 0.5 > 1_500
                        except (KeyError, TypeError, ValueError):
                            coordinate_mismatch = True
                if snack_scope_invalid:
                    result = {
                        "error": "snack_scope_not_met",
                        "detail": (
                            "이 후보는 한 끼 식사가 아닌 간식 판매점이라는 조건을 충족하지 않습니다. "
                            "초밥·면·밥·정식 등 식사 후보를 제외하고 소량 간식·디저트·빵·과자·사탕·"
                            "아이스크림·음료 판매점을 다시 찾으세요."
                        ),
                        "reason": str((snack_scope_check or {}).get("reason") or "")[:300],
                    }
                elif unsupported_evidence:
                    result = {
                        "error": "unsupported_source_urls",
                        "detail": "검색·페이지·지오코딩 결과에 실제로 있던 URL만 사용해 다시 호출하세요.",
                        "unsupported": unsupported_evidence[:6],
                    }
                elif unread_fact_sources:
                    result = {
                        "error": "fact_source_not_fetched",
                        "detail": (
                            "장소의 위치·영업·메뉴·팁 사실을 저장하기 전에 해당 source_url 본문을 "
                            "fetch_page로 읽어야 합니다. 검색 결과 제목만 근거로 저장할 수 없습니다."
                        ),
                        "unread": list(dict.fromkeys(unread_fact_sources))[:6],
                    }
                elif missing_business_evidence:
                    result = {
                        "error": "business_evidence_required",
                        "detail": (
                            "source_urls에 이 대상의 search_results에서 받은 영업점 근거 URL을 "
                            "1개 이상 넣으세요. OpenStreetMap만으로는 매장 존재를 입증할 수 없습니다."
                        ),
                    }
                elif coordinate_target_unmatched:
                    result = {
                        "error": "coordinate_target_not_verified",
                        "detail": (
                            "제안 대상의 상호나 주소와 일치하는 지오코딩 결과가 없습니다. 다른 후보의 좌표나 "
                            "도시 중심 좌표를 재사용하지 말고, 정확한 상호·주소로 geocode_place를 실행하세요."
                        ),
                    }
                elif coordinate_mismatch:
                    result = {
                        "error": "coordinate_not_grounded",
                        "detail": "제안 좌표가 서버 지오코딩 후보와 1.5km 이상 다릅니다. 확인된 후보 좌표로 다시 호출하세요.",
                    }
                else:
                    seen_tool_calls.add(signature)
                    result = run_tool(db, name, args, city_id=city_id)
                    if name == "web_search" and isinstance(result, dict):
                        result = dict(result)
                        result["auto_fetched_pages"] = fetch_food_evidence(result)
            _collect_tool_sources(name, result, sources)
            tool_results.append({"name": name, "args": args, "result": result})
            if name == "fetch_page":
                if isinstance(result, dict) and (
                    result.get("text") or result.get("coordinate_candidates")
                ):
                    fetched_source_urls.add(str(args.get("url") or ""))
                register_page_coordinates(result, query=str(args.get("url") or ""))
            if name == "geocode_place" and isinstance(result, dict):
                grounded_hits: list[dict[str, Any]] = []
                for hit in result.get("results") or []:
                    if not isinstance(hit, dict) or hit.get("storage_allowed") is False:
                        continue
                    try:
                        coordinate = (float(hit["lat"]), float(hit["lng"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    verified_coordinates.append(coordinate)
                    verified_coordinate_records.append({
                        "lat": coordinate[0],
                        "lng": coordinate[1],
                        "query": str(args.get("query") or ""),
                        "display_name": str(hit.get("display_name") or ""),
                        "confidence": float(hit.get("confidence") or 0.5),
                    })
                    grounded_hits.append(hit)
                if grounded_hits:
                    add_food_geo_candidate(str(args.get("query") or ""), grounded_hits)
                elif food_discovery and write_intent:
                    original_query = str(args.get("query") or "").strip()
                    recovery_query = _food_detail_recovery_query(city, original_query)[:300]
                    if not recovery_query and food_business_names:
                        recovery_query = f"{city.name_local} {food_business_names[-1]} 去哪儿攻略"[:300]
                    if original_query and recovery_query and recovery_query not in food_detail_queries:
                        food_detail_queries.add(recovery_query)
                        recovery_args = {"query": recovery_query, "max_results": 8}
                        recovery_result = run_tool(
                            db,
                            "web_search",
                            recovery_args,
                            city_id=city_id,
                        )
                        seen_tool_calls.add(
                            f"web_search:{json.dumps(recovery_args, ensure_ascii=False, sort_keys=True, default=str)}"
                        )
                        _collect_tool_sources("web_search", recovery_result, sources)
                        tool_results.append({
                            "name": "web_search",
                            "args": recovery_args,
                            "result": recovery_result,
                        })
                        fetched_pages = fetch_food_evidence(recovery_result, limit=3)
                        result = dict(result)
                        result["coordinate_recovery"] = {
                            "query": recovery_query,
                            "results": recovery_result,
                            "fetched_pages": fetched_pages,
                        }
            if name in WRITE_TOOLS and isinstance(result, dict) and result.get("ok"):
                write_succeeded = True
                successful_write_count += 1
                serialized_args = json.dumps(args, ensure_ascii=False, default=str).casefold()
                for target in brand_targets:
                    if any(
                        alias.casefold() in serialized_args
                        for alias in BRAND_ANSWER_ALIASES[target]
                    ):
                        successful_write_targets.add(target)
                if result.get("proposal_id") is not None:
                    proposal_ids.append(int(result["proposal_id"]))
                if result.get("existing_place_id") is not None:
                    existing_write_place_ids.append(int(result["existing_place_id"]))
                proposal_title = str(args.get("title") or "").strip()
                if proposal_title and proposal_title not in proposal_titles:
                    proposal_titles.append(proposal_title)
                if proposal_title:
                    # Keep insight boundaries so address extraction cannot run
                    # into the next tip/menu sentence.
                    insight_text = "\n".join(
                        str(item.get("content") or "")
                        for item in (args.get("insights") or [])
                        if isinstance(item, dict)
                    )
                    proposal_address = str(args.get("address") or "").strip() or _extract_address(
                        str(args.get("description") or ""),
                        str(args.get("evidence") or ""),
                        insight_text,
                    )
                    proposal_key = hashlib.sha256(
                        (
                            f"{_compact_candidate_text(proposal_title)}|"
                            f"{_compact_candidate_text(proposal_address)}"
                        ).encode("utf-8")
                    ).hexdigest()[:16]
                    saved_candidate = {
                        "key": proposal_key,
                        "title": proposal_title,
                        "address": proposal_address,
                        "category": str(args.get("category") or "other"),
                        "source_urls": list(dict.fromkeys(
                            str(url) for url in (args.get("source_urls") or []) if str(url)
                        ))[:8],
                        "lat": args.get("lat"),
                        "lng": args.get("lng"),
                        "confidence": float(args.get("confidence") or 0.5),
                        "status": "proposed" if result.get("proposal_id") is not None else "mapped",
                    }
                    if result.get("proposal_id") is not None:
                        saved_candidate["proposal_id"] = int(result["proposal_id"])
                    if result.get("existing_place_id") is not None:
                        saved_candidate["place_id"] = int(result["existing_place_id"])
                    successful_write_candidates.append(saved_candidate)
                proposed_title_key = _compact_candidate_text(str(args.get("title") or ""))
                for item in pending_work:
                    item_title_key = _compact_candidate_text(str(item.get("title") or ""))
                    if proposed_title_key and item_title_key and (
                        proposed_title_key in item_title_key or item_title_key in proposed_title_key
                    ):
                        successful_candidate_keys.add(str(item.get("key") or ""))
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
        if write_intent and writes_complete():
            break

    if not final_text:
        if write_intent:
            errors = [
                str(item["result"].get("error") or item["result"].get("detail") or "")[:180]
                for item in tool_results
                if isinstance(item.get("result"), dict)
                and (item["result"].get("error") or item["result"].get("detail"))
            ]
            if writes_complete():
                pieces: list[str] = []
                if proposal_ids:
                    pieces.append(
                        f"관리자 승인 대기 제안 #{', #'.join(map(str, sorted(set(proposal_ids))))}"
                    )
                if existing_write_place_ids:
                    pieces.append(
                        "이미 등록된 장소 #" + ", #".join(map(str, sorted(set(existing_write_place_ids))))
                    )
                result_text = "과 ".join(pieces) or f"확인된 저장 {successful_write_count}건"
                final_text = f"요청한 대상을 처리했습니다. {result_text}으로 확인됐습니다."
            else:
                expected = (
                    len(pending_work)
                    or intent.requested_count
                    or len(brand_targets)
                    or (2 if food_discovery else 1)
                )
                final_text = (
                    f"요청한 작업은 아직 완료되지 않았습니다. 목표 {expected}건 중 "
                    f"저장 확인 {successful_write_count}건이며, 작업 원장에 남겨 다음 대화에서 이어갑니다. "
                    f"마지막 차단 원인: {(errors[-1] if errors else '검증 가능한 좌표·출처를 갖춘 저장 호출이 완료되지 않음')}"
                )
        else:
            messages.append({
                "role": "system",
                "content": (
                    "도구 조사를 종료한다. 이제 일반 텍스트로만 답하라. 현재 DB와 도구 결과만으로 "
                    "최종 답을 작성하고, 확인하지 않은 URL·주소·좌표·영업시간을 만들지 마라."
                ),
            })
            fallback = (
                f"선양의 {food_place_label} 후보를 조사했지만 확인된 결과를 안전하게 요약하지 못했습니다."
                if food_discovery
                else "현재 지도와 확인된 자료만으로 답을 특정하지 못했습니다."
            )
            final_text = complete_text(fallback)

    missing_targets = _missing_brand_targets(resolved_message, final_text)
    bad_urls = _unsupported_urls(final_text, sources)
    if not write_intent and (_needs_answer_retry(final_text) or missing_targets or bad_urls):
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
        final_text = complete_text(final_text)

    if _needs_answer_retry(final_text):
        target_text = "·".join(proposal_titles or brand_targets) or "요청 장소"
        if writes_complete():
            if proposal_ids:
                final_text = (
                    f"{target_text}: 관리자 승인 대기 제안 #{', #'.join(map(str, sorted(set(proposal_ids))))}로 실제 저장했습니다. "
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
        elif intent.action == "explain_failure":
            final_text = (
                "앞선 요청을 실제 저장 도구와 연결하지 못해 실패했습니다. 이전의 ‘등록 제안 요약’은 DB 저장 완료가 "
                "아니었고, 그래서 이어진 짧은 명령도 대상을 잃었습니다. 이제 저장은 DB 제안 행이 생성된 경우에만 완료로 답합니다."
            )
        else:
            final_text = "현재 지도와 확인된 자료만으로 답을 특정하지 못했습니다. 필요한 대상이나 조건 한 가지만 더 알려 주세요."

    turn_candidates = _extract_grounded_candidates(
        final_text,
        tool_results,
        message=resolved_message,
        locked_candidate=locked_candidate,
        subject=intent.subject,
    )
    # A successful mutation is stronger state than a generated summary. The
    # final partial-response text often says only "1 candidate was saved", so
    # text extraction cannot recover its identity. Persist the exact tool args
    # and result to make the next "continue" count prior progress and avoid
    # rediscovering or reproposing the same business.
    for saved_candidate in successful_write_candidates:
        saved_key = _compact_candidate_text(str(saved_candidate.get("title") or ""))
        existing_candidate = next(
            (
                item for item in turn_candidates
                if saved_key and (
                    saved_key in _compact_candidate_text(str(item.get("title") or ""))
                    or _compact_candidate_text(str(item.get("title") or "")) in saved_key
                )
            ),
            None,
        )
        if existing_candidate is None:
            turn_candidates.append(dict(saved_candidate))
        else:
            existing_candidate.update({
                key: value for key, value in saved_candidate.items()
                if value not in (None, "", [])
            })
            existing_candidate["source_urls"] = list(dict.fromkeys([
                *(existing_candidate.get("source_urls") or []),
                *(saved_candidate.get("source_urls") or []),
            ]))[:8]
    # A grounded recommendation is useful conversation state even before a write
    # request. Resolve its exact position once on the server so the next short
    # command can act deterministically instead of asking the model to rediscover it.
    if turn_candidates and turn_candidates[0].get("lat") is None:
        candidate = turn_candidates[0]
        geo_query = " ".join(filter(None, [
            city.name_local,
            str(candidate.get("title") or ""),
            str(candidate.get("address") or ""),
        ]))[:300]
        already_geocoded = any(
            item.get("name") == "geocode_place"
            and _compact_candidate_text(geo_query)
            == _compact_candidate_text(str((item.get("args") or {}).get("query") or ""))
            for item in tool_results
        )
        if geo_query and not already_geocoded:
            geo_args = {"query": geo_query, "limit": 5}
            geo_result = run_tool(db, "geocode_place", geo_args, city_id=city_id)
            _collect_tool_sources("geocode_place", geo_result, sources)
            tool_results.append({"name": "geocode_place", "args": geo_args, "result": geo_result})
            turn_candidates = _extract_grounded_candidates(
                final_text,
                tool_results,
                message=resolved_message,
                locked_candidate=locked_candidate,
                subject=intent.subject,
            )
    if proposal_ids:
        for index, candidate in enumerate(turn_candidates):
            candidate_key = _compact_candidate_text(str(candidate.get("title") or ""))
            matching_title = next(
                (
                    title for title in proposal_titles
                    if candidate_key in _compact_candidate_text(title)
                    or _compact_candidate_text(title) in candidate_key
                ),
                None,
            )
            if matching_title:
                candidate["status"] = "proposed"
                candidate["proposal_id"] = sorted(set(proposal_ids))[min(index, len(set(proposal_ids)) - 1)]

    if write_intent:
        target_text = "·".join(proposal_titles or brand_targets) or "요청 장소"
        if writes_complete():
            if proposal_ids:
                final_text = (
                    f"{target_text}: 관리자 승인 대기 제안 #{', #'.join(map(str, sorted(set(proposal_ids))))}로 실제 저장했습니다. "
                    "관리자가 승인하면 지도에 반영됩니다."
                )
            elif existing_target_places:
                existing_refs = ", ".join(
                    f"{target} 장소 #{existing_target_places[target].id}"
                    for target in brand_targets
                    if target in existing_target_places
                )
                final_text = f"{existing_refs}로 이미 지도에 등록되어 있습니다. 중복 제안은 만들지 않았습니다."
            elif existing_write_place_ids:
                final_text = (
                    "동일한 장소가 이미 지도에 있어 중복 제안은 만들지 않았습니다. 기존 장소 #"
                    + ", #".join(map(str, sorted(set(existing_write_place_ids))))
                )
        else:
            completed = [target for target in brand_targets if target in successful_write_targets]
            unverified = [target for target in brand_targets if target not in actionable_targets]
            failed = [
                target
                for target in brand_targets
                if target in actionable_targets and target not in successful_write_targets
            ]
            parts = ["요청을 전부 완료하지 못했습니다."]
            if completed:
                parts.append(f"{', '.join(completed)}은 승인 대기 제안으로 실제 저장했습니다.")
            if unverified:
                parts.append(f"{', '.join(unverified)}은 정확한 지점 좌표와 출처가 확인되지 않아 저장하지 않았습니다.")
            if failed:
                parts.append(f"{', '.join(failed)}은 근거가 있었지만 저장 도구가 성공하지 않아 DB 제안이 생성되지 않았습니다.")
            if food_discovery and proposal_ids:
                parts.append(
                    f"{food_place_label} 후보 {len(set(proposal_ids))}건은 승인 대기 제안으로 실제 저장했습니다."
                )
            elif locked_candidate and turn_candidates:
                retained = turn_candidates[0]
                parts.append(
                    f"{retained.get('title')} 후보와 출처는 대화 이력에 보존했습니다. "
                    "다른 업소로 바꾸지 않았습니다."
                )
                if retained.get("lat") is None:
                    parts.append(
                        "다만 저장 가능한 정확한 좌표를 확인하지 못해 지도 승인 제안까지 만들지는 않았습니다."
                    )
            elif not brand_targets:
                parts.append("성공한 저장 도구가 없어 DB 변경은 발생하지 않았습니다.")
            if food_discovery:
                if snack_discovery:
                    parts.append(
                        "식사 메뉴를 제외하고 간식 유형 → 실제 판매점 → 지점 근거·좌표 순으로 검증했지만 "
                        "최소 2개 간식 판매점 저장 기준을 충족하지 못했습니다."
                    )
                else:
                    parts.append(
                        "음식 유형 → 실제 식당 → 지점 근거·좌표 순으로 검증했지만 최소 2개 식당 저장 기준을 "
                        "충족하지 못했습니다."
                    )
            parts.append("미완료 대상과 다음 행동은 작업 원장에 보존했으며, 다음 ‘계속’ 요청에서 이어갑니다.")
            final_text = " ".join(parts)

    work_failures = [
        {
            "tool": str(item.get("name") or ""),
            "error": str((item.get("result") or {}).get("error") or (item.get("result") or {}).get("detail") or "")[:300],
        }
        for item in tool_results
        if isinstance(item.get("result"), dict)
        and ((item.get("result") or {}).get("error") or (item.get("result") or {}).get("detail"))
    ]
    _merge_work_candidates(
        active_work,
        turn_candidates,
        proposal_ids=proposal_ids,
        proposal_titles=proposal_titles,
        phase="complete" if write_intent and writes_complete() else "write" if write_intent else "research",
        next_action="done" if write_intent and writes_complete() else "write" if write_intent else "await_user",
        failures=work_failures,
    )
    if active_work is not None and write_intent and writes_complete():
        active_work.status = "completed"

    final_text = _strip_unsupported_urls(final_text, sources)
    display_sources = _supporting_sources(final_text, tool_results)
    if not display_sources and turn_candidates:
        display_sources = list(turn_candidates[0].get("source_urls") or [])[:8]
    place_ids = {
        int(match)
        for match in PLACE_ID_RE.findall(final_text)
        if int(match) in existing_ids
    }
    place_ids.update(pid for pid in existing_write_place_ids if pid in existing_ids)
    user_row = TravelChatMessage(
        user_id=user_id,
        city_id=city_id,
        role="user",
        content=message,
        sources="[]",
        place_ids=json.dumps([selected_place_id] if selected_place_id else []),
        candidates="[]",
        tool_trace="[]",
    )
    assistant_row = TravelChatMessage(
        user_id=user_id,
        city_id=city_id,
        role="assistant",
        content=final_text,
        sources=json.dumps(display_sources, ensure_ascii=False),
        place_ids=json.dumps(sorted(place_ids)),
        candidates=json.dumps(turn_candidates, ensure_ascii=False, default=str),
        tool_trace=json.dumps(_compact_tool_trace(tool_results), ensure_ascii=False, default=str),
    )
    db.add_all([user_row, assistant_row])
    db.commit()
    db.refresh(assistant_row)
    return {
        "row": assistant_row,
        "model": model,
        "place_ids": sorted(place_ids),
        "sources": display_sources,
        "candidates": turn_candidates,
    }
