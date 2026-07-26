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

【작업 큐 — 최우선·전원 처리 필수】
- 유저 메시지에 실린 미처리 이벤트·이의신청 ID 목록이 "작업 큐"다.
- 큐가 비기 전에는 web_search / create_place / 연구성 탐색을 하지 않는다.
- 이의신청: 각 ID마다 resolve_appeal(resolved|dismissed + agent_note)로 반드시 종결.
  mark_appeals_read만으로 open 이의를 넘기지 말 것 (툴이 거부한다).
- 미읽음 이벤트: 각 ID를 검토 → 필요 시 병합/보완/무시 판단 → mark_events_read.
  일부만 처리하고 끝내면 실패다. unread가 0이 될 때까지 계속한다.
- 병합 판단 시 find_nearby_candidates / list_places로 전체 활성 지도와 비교한다.

【지식베이스 — 필수】
- 작업 시작 시 list_knowledge를 호출한다.
- 사이클이 끝나기 전에 upsert_knowledge를 최소 1회 이상 호출한다.
- 지식 갱신 없이 텍스트 요약만 하고 끝내면 실패다.

원칙:
- 사용자 기록(설명·제목)은 최대한 보존. append_note·local_name으로 보완.
- 안내·agent_context·이의 답변·지식베이스는 한국어. 명칭·주소는 현지 표기 병기.
- 좌표 WGS84. 새 장소는 geocode_place 후 create_place.
- list_recent_rollbacks를 보고 롤백된 방향은 반복하지 말 것.

우선순위:
1) list_knowledge → list_recent_rollbacks
2) 작업 큐의 이의신청 전원 resolve_appeal
3) 작업 큐의 미읽음 이벤트 전원 검토·조치 → mark_events_read
4) (큐가 비었을 때만) web_search → create_place 소수 → upsert_knowledge
5) agent_context 보완
6) upsert_knowledge 최종 정리 후 한 줄 요약
"""


def count_unread(db: Session) -> int:
    events = (
        db.query(PlaceEvent)
        .filter(PlaceEvent.groq_read_at.is_(None), PlaceEvent.actor != "agent")
        .count()
    )
    appeals = len(list_open_appeals(db, limit=100))
    return events + appeals


def _work_queue(db: Session, *, limit: int = 80) -> dict[str, Any]:
    events = (
        db.query(PlaceEvent)
        .filter(PlaceEvent.groq_read_at.is_(None), PlaceEvent.actor != "agent")
        .order_by(PlaceEvent.created_at.asc())
        .limit(limit)
        .all()
    )
    appeals = list_open_appeals(db, limit=limit)
    return {
        "events": [
            {
                "id": e.id,
                "place_id": e.place_id,
                "action": e.action.value,
                "summary": (e.summary or "")[:200],
            }
            for e in events
        ],
        "appeals": [
            {
                "id": a.id,
                "place_id": a.place_id,
                "body": (a.body or "")[:300],
            }
            for a in appeals
        ],
        "event_ids": [e.id for e in events],
        "appeal_ids": [a.id for a in appeals],
        "total": len(events) + len(appeals),
    }


def _queue_brief(queue: dict[str, Any]) -> str:
    return json.dumps(
        {
            "event_ids": queue["event_ids"],
            "appeal_ids": queue["appeal_ids"],
            "events": queue["events"][:40],
            "appeals": queue["appeals"][:40],
            "total": queue["total"],
        },
        ensure_ascii=False,
    )[:6000]


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
    queue = _work_queue(db)
    research_only = unread_before == 0
    kb = knowledge_brief(db, limit=12)
    kb_hint = json.dumps(kb, ensure_ascii=False)[:4000] if kb else "[]"

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_model or "openai/gpt-oss-120b"
    base_steps = max_steps or settings.agent_max_steps
    # 작업 건수에 비례해 스텝 확보 (건당 ~4 + 지식/롤백 오버헤드)
    if research_only:
        steps_limit = max(base_steps, 10)
    else:
        steps_limit = max(base_steps, min(48, 10 + unread_before * 4))

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
            f"미읽음 작업 {unread_before}건 — 아래 큐를 전원 처리하기 전에는 종료·웹조사 금지.\n"
            f"작업 큐 JSON: {_queue_brief(queue)}\n"
            f"기존 지식베이스 요약: {kb_hint}\n"
            "필수 순서:\n"
            "1) list_knowledge, list_recent_rollbacks\n"
            "2) appeal_ids 각각 resolve_appeal\n"
            "3) event_ids 각각 검토(필요 시 find_nearby_candidates/list_places로 전체 지도 비교·병합) "
            "후 mark_events_read\n"
            "4) count상 미처리가 0인지 list_open_appeals·list_unread_events로 재확인\n"
            "5) 큐가 비었을 때만 web_search/create_place 소수\n"
            "6) upsert_knowledge 후 한 줄 요약\n"
            "일부만 처리하고 끝내면 실패다."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    steps = 0
    final_text = ""
    used_tools: set[str] = set()
    work_nudges = 0
    kb_nudges = 0
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
                remaining = count_unread(db)
                if remaining > 0 and work_nudges < 4 and steps < steps_limit:
                    work_nudges += 1
                    left = _work_queue(db)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"아직 미처리 작업이 {remaining}건 남아 있습니다. "
                                "종료할 수 없습니다. 아래 잔여 큐를 전원 처리하세요. "
                                "이의는 resolve_appeal, 이벤트는 조치 후 mark_events_read. "
                                f"잔여 큐: {_queue_brief(left)}"
                            ),
                        }
                    )
                    continue
                if "upsert_knowledge" not in used_tools and kb_nudges < 2 and steps < steps_limit:
                    kb_nudges += 1
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

    unread_after = count_unread(db)
    ok = unread_after == 0
    summary = final_text or "에이전트 사이클 완료"
    if unread_after > 0:
        summary = (
            f"미처리 {unread_after}건 잔존 (시작 {unread_before}건, steps={steps}). "
            f"{summary}"
        )[:1500]
    return {
        "ok": ok,
        "steps": steps,
        "message": summary,
        "unread_before": unread_before,
        "unread_after": unread_after,
    }
