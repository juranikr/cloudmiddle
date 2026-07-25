"""Groq ReAct + tool-calling 에이전트 러너."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.agent.tools import TOOLS, run_tool
from app.config import settings
from app.knowledge import knowledge_brief
from app.messages import list_open_appeals
from app.models import PlaceEvent

SYSTEM = """당신은 중국 지난(济南) 여행 공유 지도의 정리 에이전트입니다.
목표: 미읽음 이력·이의신청·롤백·웹조사를 바탕으로 지도를 더 유용하게 만들고,
교훈을 agent_knowledge에 주제별로 병합·갱신해 사용할수록 똑똑해진다.

원칙:
- 설명·제목 등 사용자 기록은 최대한 보존. append_note·local_name으로 보완.
- 안내·agent_context·이의 답변·지식베이스는 한국어. 장소 명칭·주소는 현지 표기 병기.
- 좌표는 WGS84. 새 장소는 반드시 geocode_place 또는 신뢰할 좌표로 등록.
- list_recent_rollbacks·list_knowledge를 먼저 보고 같은 실수를 반복하지 말 것.
- 이의/롤백/새 데이터에서 얻은 교훈은 upsert_knowledge로 기존 주제와 유기적으로 병합.
- 웹 검색(web_search)으로 지난 핵심 명소·맛집·교통을 조사하고, 지도에 없는 유용한 장소는
  매 사이클 1~5개 create_place로 추천 추가(중복·남발 금지, 기존 list_places와 대조).

우선순위:
1) list_knowledge, list_recent_rollbacks
2) list_open_appeals → 조치 후 교훈 upsert_knowledge
3) list_unread_events → 병합/보완
4) web_search로 지난 여행 정보 조사 → geocode_place → create_place(소수)
5) agent_context·지식 주제 갱신
6) mark_events_read / mark_appeals_read (사용자/시스템 미읽음만)
"""


def count_unread(db: Session) -> int:
    events = (
        db.query(PlaceEvent)
        .filter(PlaceEvent.groq_read_at.is_(None), PlaceEvent.actor != "agent")
        .count()
    )
    appeals = len(list_open_appeals(db, limit=100))
    return events + appeals


def run_agent(db: Session, *, max_steps: int | None = None) -> dict[str, Any]:
    if not settings.groq_api_key:
        return {
            "ok": False,
            "steps": 0,
            "message": "GROQ_API_KEY 미설정",
            "unread_before": count_unread(db),
            "unread_after": count_unread(db),
        }

    unread_before = count_unread(db)
    research_only = unread_before == 0
    kb = knowledge_brief(db, limit=12)
    kb_hint = json.dumps(kb, ensure_ascii=False)[:4000] if kb else "[]"

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_model or "openai/gpt-oss-120b"
    steps_limit = max_steps or settings.agent_max_steps
    if research_only:
        steps_limit = max(steps_limit, 10)

    if research_only:
        user_msg = (
            "현재 미읽음 작업은 없습니다. 연구 사이클을 수행하세요.\n"
            f"기존 지식베이스 요약: {kb_hint}\n"
            "1) list_knowledge / list_places로 현황 파악\n"
            "2) web_search로 '济南 旅游 景点' '济南 美食 推荐' 등 조사\n"
            "3) 지도에 없는 유용한 장소를 geocode_place 후 create_place 1~5개\n"
            "4) 새 교훈은 upsert_knowledge로 병합\n"
            "끝나면 한 줄 요약."
        )
    else:
        user_msg = (
            f"미읽음 작업 {unread_before}건이 있습니다.\n"
            f"기존 지식베이스 요약: {kb_hint}\n"
            "list_knowledge·list_recent_rollbacks를 먼저 보고, "
            "list_open_appeals·list_unread_events를 처리하세요. "
            "교훈은 upsert_knowledge로 병합. "
            "가능하면 web_search 후 부족한 장소를 create_place로 소수 추가. "
            "끝나면 mark_events_read·mark_appeals_read 후 요약."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    steps = 0
    final_text = ""
    try:
        for _ in range(steps_limit):
            steps += 1
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
            )
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                final_text = msg.content or ""
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                raw_args = tc.function.arguments or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
                result = run_tool(db, tc.function.name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False)[:12000],
                    }
                )
    except Exception as exc:
        detail = str(exc)
        if "model_permission_blocked" in detail or "blocked at the project" in detail:
            detail = (
                f"Groq model '{model}' blocked in project limits. "
                "Enable it at https://console.groq.com/settings/project/limits "
                "or change Secrets GROQ_MODEL. "
                f"Detail: {exc}"
            )
        return {
            "ok": False,
            "steps": steps,
            "message": detail[:1500],
            "unread_before": unread_before,
            "unread_after": count_unread(db),
        }

    return {
        "ok": True,
        "steps": steps,
        "message": final_text or "에이전트 사이클 완료",
        "unread_before": unread_before,
        "unread_after": count_unread(db),
    }
