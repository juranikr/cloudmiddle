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
목표: 미읽음 이력·이의신청·롤백·웹조사를 바탕으로 지도를 정리하고,
반드시 agent_knowledge(지식베이스)에 교훈을 주제별로 병합·갱신해 다음 실행이 더 똑똑해지게 한다.

【지식베이스 — 필수】
- 작업 시작 시 list_knowledge를 호출한다 (건너뛰지 말 것).
- 사이클이 끝나기 전에 upsert_knowledge를 최소 1회 이상 호출한다.
  · 이의/롤백/병합/웹조사에서 배운 점을 topic별로 정리
  · 기존 content와 모순되면 하나의 완성본으로 재작성해 넘긴다
  · topic 예: merge_policy, appeal_lessons, jinan_food, naming_rules, geocode_tips
- 지식 갱신 없이 텍스트 요약만 하고 끝내면 실패다.

【병합·비교 범위】
- 작업의 "시작점"은 미읽음 이벤트·이의신청이다.
- 다만 병합/중복 판단 시에는 수정·이의가 없는 기존 장소까지 전부 비교 대상이다.
- list_unread_events만 보고 병합하지 말고, find_nearby_candidates 또는 list_places
  (q/category/near_lat·near_lng·radius_m)로 전체 활성 지도에서 후보를 쿼리한다.

원칙:
- 사용자 기록(설명·제목)은 최대한 보존. append_note·local_name으로 보완.
- 안내·agent_context·이의 답변·지식베이스는 한국어. 명칭·주소는 현지 표기 병기.
- 좌표 WGS84. 새 장소는 geocode_place 후 create_place.
- list_recent_rollbacks를 보고 롤백된 방향은 반복하지 말 것.
- 웹 검색으로 지난 핵심 장소를 조사하고, 지도에 없으면 소수(1~5) create_place.

우선순위:
1) list_knowledge → list_recent_rollbacks
2) list_open_appeals → 조치 → upsert_knowledge
3) list_unread_events → find_nearby_candidates/list_places로 전체 지도와 비교 → 병합/보완
4) web_search → geocode_place → create_place(소수) → 관련 교훈 upsert_knowledge
5) agent_context 보완
6) mark_events_read / mark_appeals_read (사용자·시스템 미읽음만)
7) (필수) upsert_knowledge 최종 정리 후 한 줄 요약
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
            "필수: 시작 list_knowledge, 종료 전 upsert_knowledge 1회 이상.\n"
            "1) list_knowledge / list_places로 현황 파악\n"
            "2) web_search로 '济南 旅游 景点' '济南 美食 推荐' 등 조사\n"
            "3) 지도에 없는 유용한 장소를 geocode_place 후 create_place 1~5개\n"
            "4) 조사·추가에서 배운 점을 upsert_knowledge로 병합 (빠뜨리면 실패)\n"
            "끝나면 한 줄 요약."
        )
    else:
        user_msg = (
            f"미읽음 작업 {unread_before}건이 있습니다.\n"
            f"기존 지식베이스 요약: {kb_hint}\n"
            "필수: 시작 list_knowledge, 종료 전 upsert_knowledge 1회 이상.\n"
            "list_knowledge·list_recent_rollbacks → list_open_appeals·list_unread_events.\n"
            "병합 후보를 찾을 때는 미읽음만이 아니라 find_nearby_candidates 또는 "
            "list_places(q/category/near_*)로 전체 활성 장소와 비교하세요.\n"
            "교훈은 upsert_knowledge로 병합(빠뜨리면 실패). "
            "가능하면 web_search 후 create_place 소수 추가. "
            "mark_events_read·mark_appeals_read 후 요약."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    steps = 0
    final_text = ""
    used_tools: set[str] = set()
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
                if "upsert_knowledge" not in used_tools and steps < steps_limit:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "아직 upsert_knowledge를 호출하지 않았습니다. "
                                "이번 사이클 교훈을 topic별로 upsert_knowledge로 저장한 뒤 "
                                "한 줄 요약으로 종료하세요. 지식 저장은 필수입니다."
                            ),
                        }
                    )
                    continue
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
                used_tools.add(tc.function.name)
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
