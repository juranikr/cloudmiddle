"""Groq ReAct + tool-calling 에이전트 러너."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.agent.tools import TOOLS, run_tool
from app.config import settings
from app.models import PlaceEvent

SYSTEM = """당신은 중국 지난(济南) 여행 공유 지도의 정리 에이전트입니다.
목표: 미읽음 이력을 바탕으로 지도를 더 완성도 있게 만든다.
우선순위:
1) 같은 장소로 보이는 핀 병합 (가까운 거리 + 이름 유사)
2) 각 장소 agent_context에 한국어로 유용한 요약 컨텍스트 저장
3) 필요하면 web_search로 정보 보완 (불확실하면 단정하지 말 것)
4) 꼭 필요해 보이는 핵심 장소만 소수 추가 (남발 금지)
5) 이미지 순서가 이상하면 reorder_images
작업이 끝나면 처리한 이벤트는 mark_events_read 호출.
좌표는 WGS84. 답변/컨텍스트는 한국어.
"""


def count_unread(db: Session) -> int:
    return db.query(PlaceEvent).filter(PlaceEvent.groq_read_at.is_(None)).count()


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
    if unread_before == 0:
        return {
            "ok": True,
            "steps": 0,
            "message": "미읽음 이력 없음",
            "unread_before": 0,
            "unread_after": 0,
        }

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_model or "llama-3.3-70b-versatile"
    steps_limit = max_steps or settings.agent_max_steps

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": (
                f"미읽음 이벤트 {unread_before}건이 있습니다. "
                "list_unread_events로 확인한 뒤 필요한 tool을 호출해 지도를 정리하세요. "
                "끝나면 mark_events_read로 처리 완료 표시 후 한 줄 요약."
            ),
        },
    ]

    steps = 0
    final_text = ""
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
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
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

    return {
        "ok": True,
        "steps": steps,
        "message": final_text or "에이전트 사이클 완료",
        "unread_before": unread_before,
        "unread_after": count_unread(db),
    }
