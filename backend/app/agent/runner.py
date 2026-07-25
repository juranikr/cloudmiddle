"""Groq ReAct + tool-calling 에이전트 러너."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.agent.tools import TOOLS, run_tool
from app.config import settings
from app.messages import list_open_appeals
from app.models import PlaceEvent

SYSTEM = """당신은 중국 지난(济南) 여행 공유 지도의 정리 에이전트입니다.
목표: 미읽음 이력·이의신청·롤백을 바탕으로 지도를 더 완성도 있게 만든다.

원칙:
- 설명·제목 등 사용자 기록은 최대한 보존. 마음대로 덮어쓰지 말고 append_note·local_name으로 보완.
- 안내 문장·agent_context·이의 답변은 한국어. 다만 장소 명칭·주소·공식명은 중국어 등 현지 표기를 살리고 제목에 병기.
- 좌표는 WGS84.
- list_recent_rollbacks로 관리자 롤백을 확인한다. 롤백된 조치와 같은 방향(같은 병합/같은 추천 추가/같은 필드 덮어쓰기)을 반복하지 말고 다른 접근을 취한다.

우선순위:
1) list_recent_rollbacks → 교훈 파악
2) list_open_appeals로 이의신청 검토, 필요 시 보완 후 resolve_appeal
3) 같은 장소로 보이는 핀 병합 (가까운 거리 + 이름 유사). 병합 시 양쪽 설명/별칭 보존
4) agent_context에 한국어 유용 요약 보완(기존 내용 위에 덧붙이기)
5) 필요하면 web_search로 정보 보완 (불확실하면 단정 금지)
6) 꼭 필요해 보이는 핵심 장소만 소수 추가 (남발 금지)
7) 이미지 순서가 이상하면 reorder_images
끝나면 mark_events_read / mark_appeals_read 호출 (롤백 이벤트 ID도 mark_events_read에 포함).
"""


def count_unread(db: Session) -> int:
    events = db.query(PlaceEvent).filter(PlaceEvent.groq_read_at.is_(None)).count()
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
    if unread_before == 0:
        return {
            "ok": True,
            "steps": 0,
            "message": "미읽음 이력·이의 없음",
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
                f"미읽음 작업 {unread_before}건이 있습니다. "
                "먼저 list_recent_rollbacks로 관리자 롤백 교훈을 확인하세요. "
                "이어서 list_open_appeals, list_unread_events로 정리하세요. "
                "롤백된 방향은 반복하지 말 것. 기존 문구는 보존·보완 위주. "
                "끝나면 mark_events_read·mark_appeals_read 후 한 줄 요약."
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
