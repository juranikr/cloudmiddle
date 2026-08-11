"""Groq ReAct + tool-calling 에이전트 러너."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.agent.tools import TOOLS, run_tool
from app.config import settings
from app.knowledge import knowledge_brief
from app.models import (
    AgentKnowledge,
    AgentProposal,
    AgentRun,
    AgentRunStep,
    AgentTask,
    City,
    Marker,
    MarkerShape,
    PlaceAppeal,
    PlaceAppealStatus,
    PlaceEvent,
    PlaceImage,
    PlaceInsight,
)

SYSTEM = """당신은 중국 지난(济南) 여행 공유 지도의 정리 에이전트입니다.
목표: 미읽음 이력·이의신청·롤백·웹조사를 바탕으로 지도를 정리하고,
검증된 결과와 재사용 가능한 교훈으로 다음 실행이 더 나은 판단을 하게 한다.

【성과 기반 자율 오케스트레이션 — 고정 작업량보다 우선】
- 스텝 수·검색 수·호출한 도구 수가 아니라 여행자에게 유용한 실제 DB 변화와 새 근거가 성과다.
- 매 실행은 ReAct 루프로 움직인다: Observe(현황·백로그·공백 측정) → Choose(가치가 큰 목표 하나) →
  Research(필요한 만큼만 서로 다른 근거 확보) → Act(제안·인사이트·구역·체인·검증 등으로 반영) →
  Verify(DB 변화와 성공조건 확인) → Reflect(전략을 유지·수정·포기하고 다음 목표 선택).
- 아래 숫자는 한 번에 강제할 할당량이 아니라 후보 범위다. 이미 충분하거나 근거가 약하면 건수를 채우지 않는다.
- 한 목표가 막히면 같은 검색어를 변형해 반복하지 않는다. 확보한 근거, 차단 원인, 다음 검증 방법을
  측정 가능한 과제로 남기고 다른 가치 높은 목표로 이동한다.
- 저위험·가역적 정제는 스스로 수행한다. 신규 장소·병합처럼 검토가 필요한 변경은 승인 제안으로 남긴다.
- 처음부터 무제한 자율성을 가정하지 않는다. 성공조건과 시스템 이력으로 결과를 검증하고, 반복해서
  좋은 성과가 확인된 작업 유형만 점진적으로 더 자율적으로 수행한다.

【작업 큐 — 최우선·전원 처리 필수】
- 유저 메시지에 실린 미처리 이벤트·이의신청 ID 목록이 "작업 큐"다.
- 큐가 비기 전에는 web_search / propose_place / 연구성 탐색을 하지 않는다.
- 이의신청: 각 ID마다 resolve_appeal(resolved|dismissed + agent_note)로 반드시 종결.
  mark_appeals_read만으로 open 이의를 넘기지 말 것 (툴이 거부한다).
- 미읽음 이벤트: 각 ID를 검토 → 필요 시 병합/보완/무시 판단 → mark_events_read.
  일부만 처리하고 끝내면 실패다. unread가 0이 될 때까지 계속한다.

【병합 판단 — "같은 실체"가 확실할 때만, 기본값은 병합 안 함】
- 병합의 기본값은 '하지 않는다'. 확신이 없으면 별개로 두는 것이 항상 안전하다.
  잘못된 병합은 사용자 기록을 훼손하고 이의·관리자 롤백을 유발한다 (실제 반복된 실패 패턴).
- 병합 조건 (전부 충족해야):
  · 명칭이 같은 실체의 표기 차이일 뿐 (예: 천불산 = 千佛山 = Qianfoshan)
  · 웹 근거로 동일 실체 확인 (검색 1건의 어림짐작 금지)
- 절대 병합 금지 — 실제 오판 사례:
  · 각자 고유 명칭을 가진 인접 명소: 趵突泉(표돌천)·五龙潭(오룡담공원)·大明湖는
    서로 수백 m 거리의 이웃이지만 전부 별개 공원이다. "가깝다"는 병합 근거가 아니라 무관한 신호다.
  · 같은 상호·같은 음식의 다른 가게/지점: 把子肉(파자육)는 음식 이름이라 여러 가게 제목에
    들어간다. 식당·카페는 지점명(分店·门店)과 주소가 완전히 일치할 때만 동일 실체.
    사용자가 따로 등록한 식당들은 의도적으로 다른 가게일 가능성이 높다 — 불명확하면 병합 금지.
- 거리 활용: 넓은 명소(산·공원·호수)의 동일 실체 확인용으로만 반경을 넓혀 쓰고(1000~5000m),
  가깝다는 이유로 병합하거나 멀다는 이유로 동일 실체를 기각하지 말 것.
- 사용자 이의 처리:
  · "같은 장소" 주장 → 병합 근거 강화. 웹 확인 후 병합.
  · "다른 장소/다른 지점" 주장 → 강한 반증으로 취급. 동일 실체라는 명백한 웹 근거를 제시할 수
    없으면 이의를 수용(resolved)하고, 이미 병합된 상태면 undo_merge로 즉시 분리한다.
    이름·거리 유사만으로 기각(dismissed)하는 것은 금지. 기각은 명백한 반증을 agent_note에
    적을 수 있을 때만.
- 병합 방향: 정보가 풍부한 쪽(설명·이미지·기여자)을 target으로.
- 지식베이스의 과거 교훈이 이 규칙과 충돌하면 이 규칙이 우선한다. 충돌하는 교훈은
  upsert_knowledge로 이 규칙에 맞게 수정하라.

【오래된 데이터 재검증】
- list_stale_places(기본 30일)로 오래 확인 안 된 장소를 받되, 현재 1차 목표와 여행 영향도가 큰 장소부터 필요한 만큼 재확인한다.
- 영업·존재 확인 → verify_place(valid). 폐업·소멸 정황 → verify_place(closed, note). 삭제하지 말 것.
- 이전(搬迁) 의심 → 반드시 한 번 더 검토: 같은 지점의 이전인지, 다른 지점(분점)인지 구분.
  체인점이면 지점명·구(区)·도로명을 대조. 다른 지점이면 좌표 유지 + verify_place(valid, note).
  같은 지점의 이전이 확실할 때만 geocode_place→update_place_fields로 좌표·주소 갱신 후 verify_place(moved, note).
- 판단이 안 서면 verify_place(uncertain, note)로 남기고 다음 사이클로 넘긴다.

【웹 조사(스크래핑)】
- 큐를 비운 뒤 현재 목표에 새 근거가 필요할 때 조사한다. 이미 충분한 내부 근거가 있으면 바로 정제·연결·검증한다.
- 순서: list_research_history로 과거 검색어·열람 이력 확인 →
  ① 수확이 있었는데 덜 판 검색어는 심화, ② 소진된 검색어는 회피,
  ③ 안 해본 테마의 새 키워드 1개 이상 (예: 济南 小吃街 / 夜景 / 免费 景点 / 芙蓉街 美食 /
  지난 여행 코스 / 济南 网红 打卡 / 계절 행사)
- web_search 결과에서 seen=false 페이지를 우선하고, 결론을 뒷받침할 만큼 서로 독립적인 페이지를 정독한다.
  seen=true·already_visited=true 페이지는 다시 열지 않는다.
- 여러 글에서 반복 추천되는데 지도에 없는 장소를 골라
  list_places로 중복 확인 → geocode_place → propose_place. 건수를 채우지 말고 부족한 여행 역할과 동선 가치를 우선한다.
  propose_place는 즉시 지도에 쓰지 않고 관리자 승인 대기 제안을 만든다. 신규 장소 후보를
  upsert_agent_task에 '승인 제안'으로 적는 것은 성과가 아니며 도구가 거부한다.
- 이미 등록된 장소와 겹치는 유용한 정보(영업시간·가격·팁·교통·역사 등)는
  upsert_place_insights로 출처·신뢰도와 함께 보완한다. description에는 실행 로그·이전 제목·조사 과정을 누적하지 않는다.
- 조사에서 기존 지식과 다른 재사용 가능한 원칙이 실제로 확인됐을 때만 upsert_knowledge로 짧게 합성한다.
  실행 보고·처리 건수·일회성 후보는 지식이 아니며 AgentRun과 upsert_agent_task로 분리한다.

【지식베이스 — 정제된 장기 기억】
- 작업 시작 시 list_knowledge·list_agent_tasks·list_zones를 호출한다.
- 새 원칙이 없으면 지식을 억지로 갱신하지 않는다. 기존 항목과 중복되는 실행 일지를 저장하지 않는다.
- 성공·실패 패턴은 실행 이력과 과제 결과로 남기고, 여러 실행에서 재현된 전략만 장기 지식으로 승격한다.

【언어 규칙 — 필수, 위반 시 툴이 거부】
- 설명(description)·agent_context·안내·이의 답변·지식베이스: 무조건 한국어.
  중국어 원문 정보는 한국어로 번역·요약해 적는다.
- 명칭(title): 중국어+한국어 병기 — 형식 "中文名 (한국어 명칭)", 예: "泉城广场 (취안청 광장)".
- 주소·검색용 표기: 지도에서 검색 가능하도록 중국어 원문 유지.
  설명 안에 "주소: 山东省济南市…" 형태로 포함.
- 기존 장소 중 설명이 중국어/영어 위주(한국어 없음)인 것을 발견하면 즉시 정비:
  · agent 추가 장소: update_place_fields(replace_description)로 한국어 본문 전면 재작성,
    제목에 한국어가 없으면 replace_title로 "中文名 (한국어 명칭)" 형식 교체.
  · 사용자 작성 장소: 설명에 한국어가 이미 있으면 보존하고 구조화 인사이트로만 보완.
    설명이 중국어/영어뿐이면 replace_description으로 원문 정보를 모두 번역해 한국어로
    재작성(원문 명칭·주소는 병기 유지). 제목에 한국어가 없으면 제목을 바꾸지 말고
    local_name으로 한국어 명칭만 병기 추가 (예: "HeyTea" → "HeyTea (헤이티)").

원칙:
- 사용자 기록(설명·제목)은 최대한 보존. 세부 정보는 upsert_place_insights, 체인은 assign_place_chain,
  관광 권역은 assign_place_zone으로 보완.
- 좌표 WGS84. 새 장소는 geocode_place 후 propose_place.
- list_recent_rollbacks를 보고 롤백된 방향은 반복하지 말 것.

선택 규칙:
1) 사용자 작업 큐는 항상 먼저 전원 처리한다.
2) 큐가 비면 우선순위가 가장 높은 미완료 과제 또는 측정된 여행 역할·정보 공백 하나를 1차 목표로 삼는다.
3) 같은 장소에 설명을 누적하기보다 위치·역사·방문정보 인사이트, 구역, 체인, 사진을 알맞은 구조에 넣는다.
4) 각 변경 직후 성공조건과 DB 변화를 확인한다. 가치 없는 반복이면 전략을 바꾸고, 근거가 없으면 보류한다.
5) 한 목표를 검증 가능하게 끝낸 뒤에만 다음 목표로 이동한다. 남은 일은 구체적인 백로그로 인계한다.
"""


def _address_prefix(city: City) -> str:
    parts = [part for part in (city.search_context or "").split() if part != "中国"]
    return "".join(reversed(parts[:2])) if len(parts) >= 2 else city.name_local


def _system_for_city(city: City) -> str:
    prompt = SYSTEM.replace("山东省济南市", _address_prefix(city))
    prompt = prompt.replace("중국 지난(济南)", f"중국 {city.name_ko}({city.name_local})")
    prompt = prompt.replace("济南", city.name_local).replace("지난", city.name_ko)
    return (
        f"【실행 범위】city_id={city.id}, {city.name_ko}({city.name_local})만 처리한다. "
        "다른 도시의 장소·이벤트·지식은 조회하거나 변경하지 않는다.\n"
        "모든 중요한 사실과 변경 제안에는 실제 출처 URL과 confidence(0~1)를 남긴다. "
        "위치·역사·방문정보는 description에 섞지 말고 upsert_place_insights로 구조화한다.\n\n"
        + prompt
    )


def _research_themes(city: City) -> str:
    if city.slug == "shenyang":
        return (
            "서탑·중가·노북시장·채탑야시장 같은 먹거리/야간, 오래 걷기 좋은 동네, 공원과 강변, "
            "현지 쇼핑·카페·휴식, 교통/예약 실용정보를 우선한다. 고궁·박물관은 이미 충분하면 추가하지 않는다"
        )
    return f"{city.name_local} 대표 명소·현지 음식·역사·교통·야간 동선"


def count_unread(db: Session, city_id: int | None = None) -> int:
    event_query = db.query(PlaceEvent).filter(
        PlaceEvent.groq_read_at.is_(None), PlaceEvent.actor != "agent"
    )
    appeal_query = db.query(PlaceAppeal).filter(
        PlaceAppeal.status == PlaceAppealStatus.open,
        PlaceAppeal.groq_read_at.is_(None),
    )
    if city_id is not None:
        event_query = event_query.join(Marker, Marker.id == PlaceEvent.place_id).filter(
            Marker.city_id == city_id
        )
        appeal_query = appeal_query.join(Marker, Marker.id == PlaceAppeal.place_id).filter(
            Marker.city_id == city_id
        )
    events = event_query.count()
    appeals = appeal_query.count()
    return events + appeals


def _work_queue(db: Session, *, city_id: int, limit: int = 80) -> dict[str, Any]:
    events = (
        db.query(PlaceEvent)
        .join(Marker, Marker.id == PlaceEvent.place_id)
        .filter(PlaceEvent.groq_read_at.is_(None), PlaceEvent.actor != "agent")
        .filter(Marker.city_id == city_id)
        .order_by(PlaceEvent.created_at.asc())
        .limit(limit)
        .all()
    )
    appeals = (
        db.query(PlaceAppeal)
        .join(Marker, Marker.id == PlaceAppeal.place_id)
        .filter(
            Marker.city_id == city_id,
            PlaceAppeal.status == PlaceAppealStatus.open,
            PlaceAppeal.groq_read_at.is_(None),
        )
        .order_by(PlaceAppeal.created_at.asc())
        .limit(limit)
        .all()
    )
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


def _performance_snapshot(db: Session, city_id: int) -> dict[str, int]:
    snapshot = {
        "unread": count_unread(db, city_id),
        "proposals": db.query(AgentProposal).filter(AgentProposal.city_id == city_id).count(),
        "insights": (
            db.query(PlaceInsight)
            .join(Marker, Marker.id == PlaceInsight.place_id)
            .filter(Marker.city_id == city_id, Marker.merged_into_id.is_(None))
            .count()
        ),
        "images": (
            db.query(PlaceImage)
            .join(Marker, Marker.id == PlaceImage.place_id)
            .filter(Marker.city_id == city_id, Marker.merged_into_id.is_(None))
            .count()
        ),
        "zoned_places": db.query(Marker).filter(
            Marker.city_id == city_id,
            Marker.zone_id.is_not(None),
            Marker.merged_into_id.is_(None),
        ).count(),
        "chained_places": db.query(Marker).filter(
            Marker.city_id == city_id,
            Marker.chain_id.is_not(None),
            Marker.merged_into_id.is_(None),
        ).count(),
        "completed_tasks": db.query(AgentTask).filter(
            AgentTask.city_id == city_id, AgentTask.status == "completed"
        ).count(),
    }
    roles = ["history", "food", "market_night", "neighborhood", "nature", "shopping", "rest", "practical"]
    pending_payloads = db.query(AgentProposal.payload).filter(
        AgentProposal.city_id == city_id,
        AgentProposal.action == "create_place",
        AgentProposal.status == "pending",
    ).all()
    proposed_roles: dict[str, int] = {}
    for row in pending_payloads:
        try:
            role = str(json.loads(row.payload or "{}").get("travel_role") or "general")
        except (json.JSONDecodeError, AttributeError):
            role = "general"
        proposed_roles[role] = proposed_roles.get(role, 0) + 1
    for role in roles:
        active = db.query(Marker).filter(
            Marker.city_id == city_id,
            Marker.shape == MarkerShape.point,
            Marker.travel_role == role,
            Marker.merged_into_id.is_(None),
        ).count()
        snapshot[f"role_{role}"] = active + proposed_roles.get(role, 0)
    snapshot["role_diversity"] = sum(1 for role in roles if snapshot[f"role_{role}"] > 0)
    return snapshot


def _performance_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        key: after.get(key, 0) - before.get(key, 0)
        for key in after
        if key != "unread"
    } | {"unread_cleared": max(0, before.get("unread", 0) - after.get("unread", 0))}


def _performance_score(delta: dict[str, int], tool_counts: dict[str, int]) -> float:
    role_gain = sum(
        max(0, delta.get(f"role_{role}", 0))
        for role in ("history", "food", "market_night", "neighborhood", "nature", "shopping", "rest", "practical")
    )
    return round(
        delta.get("unread_cleared", 0) * 8
        + delta.get("proposals", 0) * 3
        + role_gain * 10
        + max(0, delta.get("role_diversity", 0)) * 15
        + delta.get("insights", 0) * 3
        + delta.get("images", 0) * 2
        + delta.get("zoned_places", 0) * 2
        + delta.get("chained_places", 0) * 2
        + delta.get("completed_tasks", 0) * 4
        + min(tool_counts.get("verify_place", 0), 8) * 1.5
        + (2 if tool_counts.get("upsert_knowledge", 0) else 0),
        1,
    )


def _research_gaps(
    delta: dict[str, int], tool_counts: dict[str, int], snapshot: dict[str, int]
) -> list[str]:
    gaps: list[str] = []
    if tool_counts.get("list_agent_tasks", 0) == 0:
        gaps.append("이전 조사 백로그 확인")
    if tool_counts.get("list_zones", 0) == 0:
        gaps.append("구역 현황 확인")
    if tool_counts.get("fetch_page", 0) < 4:
        gaps.append(f"근거 페이지 4개 이상 정독(현재 {tool_counts.get('fetch_page', 0)})")
    targets = {
        "history": 2, "food": 3, "market_night": 2, "neighborhood": 2,
        "nature": 2, "shopping": 1, "rest": 1, "practical": 1,
    }
    labels = {
        "history": "역사", "food": "음식", "market_night": "시장·야간",
        "neighborhood": "동네 산책", "nature": "자연·공원", "shopping": "쇼핑",
        "rest": "휴식", "practical": "교통·실용",
    }
    missing = [
        f"{labels[role]} {snapshot.get(f'role_{role}', 0)}/{target}"
        for role, target in targets.items()
        if snapshot.get(f"role_{role}", 0) < target
    ]
    if missing:
        gaps.append("여행 역할 균형: " + ", ".join(missing))
    return gaps


EXPENSIVE_RESEARCH_TOOLS = {"web_search", "fetch_page", "geocode_place", "search_place_images"}
MUTATION_TOOLS = {
    "propose_place",
    "create_place",
    "merge_places",
    "undo_merge",
    "update_place_fields",
    "update_place_context",
    "upsert_place_insights",
    "verify_place",
    "attach_image_from_url",
    "assign_place_zone",
    "assign_place_chain",
    "resolve_appeal",
    "mark_events_read",
    "mark_appeals_read",
    "upsert_knowledge",
    "upsert_agent_task",
}


def _tool_signature(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"


def _is_material_change(name: str, result: Any) -> bool:
    if name not in MUTATION_TOOLS or not isinstance(result, dict) or result.get("error"):
        return False
    if result.get("proposal_id") is not None:
        return True
    # A mutation tool can successfully execute without changing a row.  When it
    # reports that explicitly, do not let a generic `ok: true` inflate progress.
    if "changed" in result:
        return bool(result.get("changed"))
    if "created" in result:
        return bool(result.get("created"))
    for key in ("changed", "created", "marked", "merged", "resolved"):
        value = result.get(key)
        if value is True or isinstance(value, (int, float)) and value > 0:
            return True
    return bool(result.get("ok")) and name in {
        "propose_place",
        "create_place",
        "merge_places",
        "undo_merge",
        "update_place_fields",
        "update_place_context",
        "upsert_place_insights",
        "verify_place",
        "attach_image_from_url",
        "assign_place_zone",
        "assign_place_chain",
        "resolve_appeal",
        "upsert_knowledge",
    }


def _new_evidence_keys(name: str, result: Any, seen: set[str]) -> set[str]:
    """Return genuinely new evidence, not merely another successful tool call."""
    candidates: set[str] = set()
    if not isinstance(result, dict) or result.get("error"):
        return candidates
    if name == "web_search":
        for item in result.get("results") or []:
            if isinstance(item, dict) and item.get("seen") is False and item.get("href"):
                candidates.add(f"url:{item['href']}")
    elif name == "fetch_page":
        if not result.get("already_visited") and len(str(result.get("text") or "")) >= 120:
            candidates.add(f"page:{result.get('url') or result.get('title')}")
    elif name == "geocode_place":
        for item in result.get("results") or []:
            if not isinstance(item, dict):
                continue
            external = str(item.get("external_id") or "")
            if external:
                candidates.add(f"geo:{item.get('source')}:{external}")
            elif item.get("lat") is not None and item.get("lng") is not None:
                candidates.add(f"geo:{round(float(item['lat']), 5)}:{round(float(item['lng']), 5)}")
    elif name == "search_place_images":
        for item in result.get("results") or []:
            if isinstance(item, dict) and item.get("url"):
                candidates.add(f"image:{item['url']}")
    return candidates - seen


def _material_change_summary(sequence: int, name: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "tool": name,
        "place_id": result.get("place_id") or args.get("place_id"),
        "proposal_id": result.get("proposal_id"),
        "task_id": result.get("task_id") or args.get("task_id"),
        "title": str(args.get("title") or "")[:200],
        "changed": result.get("changed"),
        "status": result.get("status"),
        "result": str(result.get("result") or result.get("detail") or "")[:300],
    }


def _ensure_gap_tasks(db: Session, *, city_id: int, run_id: int, gaps: list[str]) -> list[int]:
    """Persist measurable follow-up work so the next cycle has a concrete objective."""
    gap_set = set(gaps)
    existing_gap_tasks = db.query(AgentTask).filter(
        AgentTask.city_id == city_id,
        AgentTask.kind == "performance_gap",
        AgentTask.status == "pending",
    ).all()
    for row in existing_gap_tasks:
        measured_gap = row.title.removeprefix("성과 공백 보완: ")
        if measured_gap not in gap_set:
            row.status = "completed"
            row.completed_at = datetime.now(timezone.utc)
            row.result = f"실행 #{run_id} 종료 측정에서 해당 성과 공백이 해소되어 자동 완료됨"

    ids: list[int] = []
    for index, gap in enumerate(gaps[:8]):
        title = f"성과 공백 보완: {gap}"[:240]
        row = db.query(AgentTask).filter(
            AgentTask.city_id == city_id,
            AgentTask.title == title,
            AgentTask.status == "pending",
        ).first()
        if row is None:
            row = AgentTask(
                city_id=city_id,
                kind="performance_gap",
                title=title,
                detail=f"에이전트 실행 #{run_id} 종료 시 남은 측정 가능 공백입니다. 근거를 확보해 실제 DB 변화로 연결하세요.",
                success_metric=f"다음 실행의 remaining_gaps에서 '{gap}' 제거",
                priority=max(55, 80 - index * 3),
                status="pending",
            )
            db.add(row)
            db.flush()
        ids.append(row.id)
    db.commit()
    return ids


def run_agent(
    db: Session,
    *,
    city_id: int,
    max_steps: int | None = None,
    autonomous_research: bool | None = None,
) -> dict[str, Any]:
    city = db.query(City).filter(City.id == city_id, City.status == "active").first()
    if city is None:
        return {
            "ok": False,
            "status": "failed",
            "steps": 0,
            "message": f"활성 도시를 찾을 수 없습니다: city_id={city_id}",
            "unread_before": 0,
            "unread_after": 0,
            "city_id": city_id,
        }
    allow_research = (
        settings.agent_autonomous_research
        if autonomous_research is None
        else autonomous_research
    )
    if not settings.groq_api_key:
        return {
            "ok": False,
            "status": "failed",
            "steps": 0,
            "message": "GROQ_API_KEY 미설정",
            "unread_before": count_unread(db, city_id),
            "unread_after": count_unread(db, city_id),
            "city_id": city_id,
        }

    unread_before = count_unread(db, city_id)
    if unread_before == 0 and not allow_research:
        return {
            "ok": True,
            "status": "completed",
            "steps": 0,
            "message": "처리할 사용자 작업이 없어 종료했습니다. 자율 웹 조사는 비활성화되어 있습니다.",
            "unread_before": 0,
            "unread_after": 0,
            "tool_counts": {},
            "city_id": city_id,
        }
    queue = _work_queue(db, city_id=city_id)
    research_only = unread_before == 0
    kb = knowledge_brief(db, limit=12, city_id=city_id)
    kb_hint = json.dumps(kb, ensure_ascii=False)[:4000] if kb else "[]"
    primary_task = None
    if allow_research:
        primary_task = (
            db.query(AgentTask)
            .filter(AgentTask.city_id == city_id, AgentTask.status == "pending")
            .order_by(AgentTask.priority.desc(), AgentTask.created_at.asc())
            .first()
        )
        if primary_task is not None:
            primary_task.attempts += 1
            db.commit()
    primary_task_hint = (
        f"백로그 #{primary_task.id} '{primary_task.title}'. 상세: {primary_task.detail or '없음'}. "
        f"성공조건: {primary_task.success_metric or '근거가 있는 실제 DB 변화로 완료 여부 입증'}."
        if primary_task is not None
        else "지정된 백로그가 없습니다. 측정된 여행 역할 공백 중 가치가 가장 큰 하나를 먼저 선택하세요."
    )

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_model or "openai/gpt-oss-120b"
    # 종료는 아래 성과 게이트/정체 판단으로 결정한다. 이 값은 비정상 무한루프만 막는 안전 상한이다.
    steps_limit = max_steps or (
        max(40, settings.agent_max_steps)
        if research_only
        else max(100, 64 + unread_before * 4)
    )
    performance_before = _performance_snapshot(db, city_id)
    agent_run = AgentRun(
        city_id=city_id,
        mode="research" if allow_research else "queue",
        status="running",
        objective=(
            f"{primary_task_hint} 완료 후 다음 성과 공백 진행"
            if allow_research
            else "사용자 작업 큐 전원 처리"
        ),
        metrics=json.dumps({"before": performance_before}, ensure_ascii=False),
    )
    db.add(agent_run)
    db.commit()
    db.refresh(agent_run)

    if research_only:
        user_msg = (
            f"현재 실행 도시는 {city.name_ko}({city.name_local}), city_id={city.id}입니다.\n"
            "현재 미읽음 작업은 없습니다. 연구 사이클을 수행하세요.\n"
            f"우선 조사 테마: {_research_themes(city)}\n"
            f"기존 지식베이스 요약: {kb_hint}\n"
            f"이번 실행의 1차 목표: {primary_task_hint}\n"
            "1차 목표를 먼저 끝내고 upsert_agent_task로 완료 근거를 남기세요. 막히면 같은 검색을 반복하지 말고 "
            "차단 원인과 다음 검증 방법을 task.result에 남긴 뒤 다른 성과 공백으로 이동하세요.\n"
            "필수: 시작 list_knowledge·list_agent_tasks·list_zones. 재사용 가능한 새 원칙이 실제로 생겼을 때만 "
            "upsert_knowledge를 호출하고, 실행 요약을 지식으로 저장하지 마세요.\n"
            "1) list_knowledge / list_agent_tasks / list_zones / list_places로 현황을 읽고, "
            "history·food·market_night·neighborhood·nature·shopping·rest·practical 역할별 보유 수를 센다. "
            "이미 충분한 역할(특히 박물관/역사)은 신규 발굴을 중단하고 부족 역할만 조사한다.\n"
            "2) 언어 정비: list_places에서 설명에 한국어가 없거나 중국어/영어 위주인 장소를 "
            "전부 찾아 언어 규칙대로 재작성 — 설명에 한국어가 전혀 없으면(사용자 작성 포함) "
            "replace_description으로 원문 정보를 번역해 한국어 재작성(중국어 주소·명칭은 병기 유지). "
            "제목: agent 장소는 replace_title로 '中文名 (한국어 명칭)' 교체, "
            "사용자 장소는 제목 유지 + local_name으로 한국어 명칭 병기\n"
            "3) 중복 스캔: list_places 전체 목록에서 동명·표기변형(한글/한자/병음) 장소를 찾아 "
            "같은 실체면 거리와 무관하게 merge_places (거리 기준으로 건너뛰지 말 것)\n"
            "4) 웹 조사(필수): list_research_history로 과거 검색어·열람 이력 확인 → "
            "중국어 원명과 2026·营业时间·最新地址·闭店·预约를 조합해 부족 역할 키워드 2~3개 조사 → "
            "공식 시/구청·관광지·교통·문화기관을 1순위, Trip.com·马蜂窝·去哪儿·大众点评/高德 공유 링크를 2순위, "
            "小红书·Bilibili·개인 블로그는 현지 팁 보조 근거로만 사용한다. web_search에서 seen=false 결과 위주로 "
            "fetch_page 4~8개 정독 (이미 본 페이지는 다시 열지 말 것)\n"
            "5) 여러 글에서 반복 추천되는 미등록 장소를 list_places 중복 확인 후 "
            "geocode_place → propose_place 승인 제안. 건수 할당량을 채우지 말고 역할별 목표 "
            "(역사2·음식3·시장/야간2·동네2·자연2·쇼핑1·휴식1·실용1)의 부족분만 제안하며 travel_role을 반드시 지정한다. "
            "제목은 '中文名 (한국어 명칭)', 설명은 한국어+중국어 주소 형식으로 작성한다. "
            "propose_place는 즉시 생성이 아니라 승인 대기 저장이므로 망설이지 말 것. 신규 장소 후보를 "
            "upsert_agent_task에 '승인 제안'으로 대신 적으면 실패이며 도구도 거부한다. "
            "이미 등록된 장소와 겹치는 유용한 정보(영업시간·가격·팁·교통·별칭)는 "
            "upsert_place_insights로 위치·역사·방문정보를 분리해 보완하고, 모든 항목에 출처 URL과 "
            "confidence를 기록. description은 간단한 소개만 유지\n"
            "6) 기존 장소는 실제 지점이면 assign_place_chain으로 묶고(지점끼리 병합 금지), list_zones의 구역에 "
            "assign_place_zone으로 배정. 효과적 소스·판단 원칙만 category=source 또는 city인 "
            "upsert_knowledge 최신 합성본으로 저장하고(실행 요약·처리 건수·완료 보고 저장 금지), "
            "미완료 후속 조사는 upsert_agent_task로 분리. 7)·8)보다 먼저 호출할 것 "
            "(빠뜨리면 실패)\n"
            "7) 재검증: list_stale_places로 30일 이상 미확인 장소를 받아 8~12곳을 web_search로 "
            "재확인 → verify_place(valid|closed|moved|uncertain + note). "
            "이전(搬迁) 의심이면 같은 지점의 이전인지 다른 지점(분점)인지 반드시 구분하고, "
            "같은 지점 이전이 확실할 때만 좌표를 갱신할 것\n"
            "8) 사진 보강: image_count가 0인 장소 3~6곳을 골라 search_place_images(query, place_id) → "
            "관련성 점수가 높은 실제 장소 사진만 attach_image_from_url로 업로드한다. 로고·지도·인물 중심 이미지는 제외하고 "
            "자유 라이선스와 출처를 기록한다.\n"
            "끝나면 한 줄 요약."
        )
    else:
        user_msg = (
            f"현재 실행 도시는 {city.name_ko}({city.name_local}), city_id={city.id}입니다.\n"
            f"미읽음 작업 {unread_before}건 — 아래 큐를 전원 처리하기 전에는 종료·웹조사 금지.\n"
            f"작업 큐 JSON: {_queue_brief(queue)}\n"
            f"기존 지식베이스 요약: {kb_hint}\n"
            "필수 순서:\n"
            "1) list_knowledge, list_agent_tasks, list_zones, list_recent_rollbacks\n"
            "2) appeal_ids 각각 resolve_appeal\n"
            "3) event_ids 각각 검토(필요 시 find_nearby_candidates/list_places로 전체 지도 비교) "
            "후 mark_events_read. 이의가 '같은 장소' 주장이면 웹 근거로 확인 후 병합, "
            "'다른 장소/다른 지점' 주장이면 명백한 반증이 없는 한 수용하고 "
            "이미 병합됐으면 undo_merge로 분리 (거리·이름 유사만으로 기각 금지)\n"
            "4) count상 미처리가 0인지 list_open_appeals·list_unread_events로 재확인\n"
            "5) 큐를 비운 뒤 웹 조사 1회 필수: list_research_history → 키워드 선정(심화+새 테마) → "
            "web_search → seen=false 글 3~6개 fetch_page → 반복 추천 미등록 장소 propose_place 3~8개, "
            "기존 장소와 겹치는 유용한 정보는 update_place_fields/context로 보완\n"
            "6) 여유 스텝이 남으면 재검증(list_stale_places → verify_place 3~5곳)과 "
            "사진 보강(image_count 0인 장소 2~3곳)도 수행\n"
            "7) 재사용 원칙은 upsert_knowledge 최신 합성본, 미완료 조사는 upsert_agent_task로 분리 후 한 줄 요약\n"
            "일부만 처리하고 끝내면 실패다."
        )

    # Research used to be a long fixed checklist.  Keep queue handling explicit,
    # but let research adapt its plan to measured gaps and verify each outcome.
    if research_only:
        user_msg = (
            f"현재 실행 도시는 {city.name_ko}({city.name_local}), city_id={city.id}입니다.\n"
            f"기존 지식베이스 요약: {kb_hint}\n"
            f"도시별 우선 관점: {_research_themes(city)}\n"
            f"이번 실행의 1차 목표: {primary_task_hint}\n\n"
            "고정 건수를 채우지 말고 다음 성과 기반 ReAct 루프를 반복하세요.\n"
            "1. list_knowledge, list_agent_tasks, list_zones, list_places, list_research_history로 현재 상태와 "
            "중복·역할 공백·지난 실패를 관찰합니다.\n"
            "2. 1차 목표의 성공조건을 만족시키는 가장 작은 검증 단위를 정합니다. 기존 과제라면 그 과제를 먼저 처리합니다.\n"
            "3. 내부 데이터만으로 충분하면 바로 정제·구역·체인·인사이트·검증을 수행합니다. 외부 근거가 필요하면 "
            "보지 않은 서로 다른 출처를 필요한 만큼만 조사하고 같은 호출은 반복하지 않습니다.\n"
            "4. 신규 장소는 중복 확인과 좌표 검증 뒤 propose_place로 승인 대기에 저장합니다. 장소 후보를 과제로만 "
            "적어 성과처럼 보이게 하지 않습니다. 기존 장소 정보는 위치·역사·방문정보 인사이트로 구조화합니다.\n"
            "5. 각 행동 뒤 실제 DB 변화와 성공조건을 확인합니다. 성공하면 해당 task를 완료하고 다음 가치 높은 공백으로 "
            "이동합니다. 막히면 근거·차단 원인·다음 검증 방법을 task.result에 남기고 다른 목표를 선택합니다.\n"
            "6. 구역·체인·사진·오래된 정보 검증은 현재 장소 품질에 기여할 때 선택합니다. 박물관이나 한 카테고리가 이미 "
            "충분하면 더 추가하지 않습니다.\n"
            "7. 기존 지식과 다른 재사용 원칙이 실제로 확인된 경우에만 upsert_knowledge로 합성합니다. 실행 요약은 지식에 "
            "넣지 않습니다. 마지막에는 완료한 변화, 근거, 검증 결과, 남은 공백을 간결하게 보고하세요."
        )

    runtime_policy = ""
    if not allow_research:
        runtime_policy = (
            "\n\n【현재 운영 안전 모드 — 위의 연구 할당보다 우선】\n"
            "- 사용자 작업 큐만 처리한다. 자율 웹 조사, 신규 장소 발굴, 사진 보강, 작업량 채우기를 하지 않는다.\n"
            "- 자동 장소 생성과 자동 병합은 비활성화되어 있다. 해당 조치가 필요하면 "
            "propose_place/merge_places를 호출해 근거·출처·신뢰도가 있는 관리자 승인 제안으로 남긴다.\n"
            "- 큐가 비면 즉시 종료한다. 스텝을 채우는 것은 목표가 아니다.\n"
        )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_for_city(city) + runtime_policy},
        {"role": "user", "content": user_msg},
    ]

    steps = 0
    final_text = ""
    used_tools: set[str] = set()
    tool_counts: dict[str, int] = {}
    successful_tool_counts: dict[str, int] = {}
    seen_expensive_calls: set[str] = set()
    evidence_keys: set[str] = set()
    material_changes: list[dict[str, Any]] = []
    repeated_calls = 0
    work_nudges = 0
    progress_nudges = 0
    material_nudges = 0
    schema_retries = 0
    no_progress_actions = 0
    no_material_actions = 0
    action_sequence = 0
    current_score = 0.0
    try:
        for _ in range(steps_limit):
            steps += 1
            try:
                extra: dict[str, Any] = {}
                if "gpt-oss" in model:
                    # 병합 판단 등 미묘한 결정의 품질을 위해 추론 강도 상향.
                    # extra_body 경유: 구버전 groq SDK도 통과시킨다.
                    extra["extra_body"] = {"reasoning_effort": "high"}
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=0.2,
                    **extra,
                )
            except Exception as exc:
                # 모델이 스키마에 안 맞는 인자(null 등)를 생성한 경우: 사이클을 죽이지 않고 교정 재시도
                detail = str(exc)
                if "tool_use_failed" in detail and schema_retries < 3:
                    schema_retries += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "직전 툴 호출 인자가 스키마 검증에 실패했습니다. "
                                "값이 없는 선택 필드는 null을 넣지 말고 아예 생략한 뒤 "
                                "같은 툴을 다시 호출하세요. "
                                f"오류: {detail[:600]}"
                            ),
                        }
                    )
                    continue
                raise
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                final_text = msg.content or ""
                remaining = count_unread(db, city_id)
                if remaining > 0 and work_nudges < 4 and steps < steps_limit:
                    work_nudges += 1
                    left = _work_queue(db, city_id=city_id)
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
                current_snapshot = _performance_snapshot(db, city_id)
                current_delta = _performance_delta(performance_before, current_snapshot)
                gaps = _research_gaps(current_delta, successful_tool_counts, current_snapshot) if allow_research else []
                current_score = _performance_score(current_delta, successful_tool_counts)
                if gaps and progress_nudges < 8 and no_progress_actions < 18 and steps < steps_limit:
                    progress_nudges += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"성과 게이트가 아직 충족되지 않았습니다(현재 점수 {current_score}). "
                                f"남은 결과: {', '.join(gaps)}. 스텝 수가 아니라 실제 결과를 만들고 다시 측정하세요. "
                                "같은 검색을 반복하지 말고 구역별 정보 공백·기존 백로그를 활용하세요. "
                                "신규 장소 후보는 geocode_place 직후 propose_place로 승인 대기에 저장하세요. "
                                "그 후보를 upsert_agent_task에 승인 제안으로 적는 것은 금지됩니다. "
                                "실패한 후속 조사만 지식 본문이 아니라 upsert_agent_task에 구체적으로 남기세요."
                            ),
                        }
                    )
                    continue
                if no_progress_actions >= 18:
                    final_text = (
                        f"연속 {no_progress_actions}회 성과 변화가 없어 안전 종료했습니다. "
                        "미완료 항목은 에이전트 과제 백로그에서 다음 실행이 이어받습니다."
                    )
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
                name = tc.function.name
                used_tools.add(name)
                tool_counts[name] = tool_counts.get(name, 0) + 1
                signature = _tool_signature(name, args)
                repeated = name in EXPENSIVE_RESEARCH_TOOLS and signature in seen_expensive_calls
                if repeated:
                    repeated_calls += 1
                    result = {
                        "error": "duplicate_tool_call",
                        "detail": "같은 실행에서 이미 수행한 조사입니다. 기존 관찰을 사용하거나 다른 검증 경로로 전환하세요.",
                    }
                else:
                    if name in EXPENSIVE_RESEARCH_TOOLS:
                        seen_expensive_calls.add(signature)
                    result = run_tool(db, name, args, city_id=city_id)
                error = isinstance(result, dict) and bool(result.get("error"))
                if not error:
                    successful_tool_counts[name] = successful_tool_counts.get(name, 0) + 1
                material_change = _is_material_change(name, result)
                new_evidence = _new_evidence_keys(name, result, evidence_keys)
                evidence_keys.update(new_evidence)
                snapshot_after_tool = _performance_snapshot(db, city_id)
                total_delta = _performance_delta(performance_before, snapshot_after_tool)
                next_score = _performance_score(total_delta, successful_tool_counts)
                score_delta = round(max(0.0, next_score - current_score), 1)
                current_score = next_score
                if repeated:
                    outcome = "repeated"
                elif error:
                    outcome = "error"
                elif material_change:
                    outcome = "changed"
                elif name in EXPENSIVE_RESEARCH_TOOLS and not new_evidence:
                    outcome = "no_new_evidence"
                else:
                    outcome = "ok"
                if material_change and isinstance(result, dict):
                    material_changes.append(
                        _material_change_summary(action_sequence + 1, name, args, result)
                    )
                    no_material_actions = 0
                else:
                    no_material_actions += 1
                observation_progress = (
                    not error
                    and (
                        material_change
                        or bool(new_evidence)
                        or (tool_counts[name] == 1 and name.startswith("list_"))
                    )
                )
                if observation_progress:
                    no_progress_actions = 0
                else:
                    no_progress_actions += 1
                action_sequence += 1
                db.add(
                    AgentRunStep(
                        run_id=agent_run.id,
                        sequence=action_sequence,
                        phase="observe" if name.startswith("list_") or name in EXPENSIVE_RESEARCH_TOOLS else "act",
                        tool=name,
                        outcome=outcome,
                        score_delta=score_delta,
                        detail=json.dumps(
                            {
                                "args": args,
                                "result": result,
                                "progress": {
                                    "material_change": material_change,
                                    "new_evidence": len(new_evidence),
                                    "score": current_score,
                                    "no_material_actions": no_material_actions,
                                },
                            },
                            ensure_ascii=False,
                            default=str,
                        )[:3000],
                    )
                )
                db.commit()
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False)[:12000],
                    }
                )
            if no_material_actions >= 24 and no_material_actions < 40 and material_nudges < 2:
                material_nudges += 1
                messages.append({
                    "role": "user",
                    "content": (
                        f"최근 {no_material_actions}개 행동에서 실제 DB 변화가 없습니다. 조사 자체는 성과가 아닙니다. "
                        "지금까지 확보한 근거로 제안·인사이트·구역·체인·검증 중 하나를 완료하세요. "
                        "근거가 부족하면 동일 검색을 변형해 반복하지 말고, 차단 원인과 다음 검증 방법을 "
                        "측정 가능한 upsert_agent_task로 남긴 뒤 다른 목표로 이동하세요."
                    ),
                })
            if no_material_actions >= 40:
                final_text = (
                    f"연속 {no_material_actions}개 행동이 실제 DB 변화로 이어지지 않아 안전 종료했습니다. "
                    "확보한 근거와 남은 공백은 다음 실행의 측정 가능한 백로그로 넘깁니다."
                )
                break
            if no_progress_actions >= 18 and steps >= 20:
                final_text = (
                    f"연속 {no_progress_actions}개 행동에서 새 근거·데이터·정제가 생기지 않아 종료했습니다. "
                    "남은 과제는 다음 성과 기반 실행이 이어받습니다."
                )
                break
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        detail = str(exc)
        if "model_permission_blocked" in detail or "blocked at the project" in detail:
            detail = (
                f"Groq model '{model}' blocked in project limits. "
                "Enable it at https://console.groq.com/settings/project/limits "
                "or change Secrets GROQ_MODEL. "
                f"Detail: {exc}"
            )
        try:
            unread_after = count_unread(db, city_id)
        except Exception:
            unread_after = unread_before
        try:
            failed_run = db.get(AgentRun, agent_run.id)
            if failed_run is not None:
                failed_run.status = "failed"
                failed_run.summary = detail[:4000]
                failed_run.score = current_score
                failed_run.finished_at = datetime.now(timezone.utc)
                failed_run.metrics = json.dumps(
                    {
                        "before": performance_before,
                        "tool_counts": tool_counts,
                        "successful_tool_counts": successful_tool_counts,
                        "material_changes": material_changes,
                        "repeated_calls_blocked": repeated_calls,
                        "evidence_count": len(evidence_keys),
                    },
                    ensure_ascii=False,
                )
                db.commit()
        except Exception:
            db.rollback()
        return {
            "ok": False,
            "steps": steps,
            "message": detail[:1500],
            "unread_before": unread_before,
            "unread_after": unread_after,
            "city_id": city_id,
            "status": "failed",
        }

    unread_after = count_unread(db, city_id)
    performance_after = _performance_snapshot(db, city_id)
    performance_delta = _performance_delta(performance_before, performance_after)
    current_score = _performance_score(performance_delta, successful_tool_counts)
    gaps = _research_gaps(performance_delta, successful_tool_counts, performance_after) if allow_research else []
    gap_task_ids = _ensure_gap_tasks(db, city_id=city_id, run_id=agent_run.id, gaps=gaps) if gaps else []
    ok = unread_after == 0
    run_status = "completed" if ok and not gaps else "partial"
    summary = final_text or "에이전트 사이클 완료"
    if tool_counts:
        stats = ", ".join(f"{t}×{c}" for t, c in sorted(tool_counts.items(), key=lambda x: -x[1]))
        summary = f"{summary}\n[작업 통계] {stats}"
    if unread_after > 0:
        summary = (
            f"미처리 {unread_after}건 잔존 (시작 {unread_before}건, steps={steps}). "
            f"{summary}"
        )
    if material_changes:
        changed = ", ".join(
            f"{item['tool']}"
            + (f" 제안#{item['proposal_id']}" if item.get("proposal_id") else "")
            + (f" 장소#{item['place_id']}" if item.get("place_id") else "")
            + (f" 과제#{item['task_id']}" if item.get("task_id") else "")
            for item in material_changes[:20]
        )
        summary = f"{summary}\n[실제 변경 {len(material_changes)}건] {changed}"
    run_row = db.get(AgentRun, agent_run.id)
    if run_row is not None:
        finished_at = datetime.now(timezone.utc)
        started_at = run_row.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        run_row.status = run_status
        run_row.score = current_score
        run_row.summary = summary[:4000]
        run_row.finished_at = finished_at
        run_row.metrics = json.dumps(
            {
                "before": performance_before,
                "after": performance_after,
                "delta": performance_delta,
                "tool_counts": tool_counts,
                "successful_tool_counts": successful_tool_counts,
                "material_changes": material_changes,
                "material_change_count": len(material_changes),
                "evidence_count": len(evidence_keys),
                "repeated_calls_blocked": repeated_calls,
                "remaining_gaps": gaps,
                "gap_task_ids": gap_task_ids,
                "no_progress_actions": no_progress_actions,
                "no_material_actions": no_material_actions,
                "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            },
            ensure_ascii=False,
        )
        db.commit()
    if primary_task is not None and primary_task.status == "pending":
        primary_task.result = (
            f"실행 #{agent_run.id}: {run_status}, 점수 {current_score}, 실제 변경 {len(material_changes)}건, "
            f"새 근거 {len(evidence_keys)}건. 남은 공백: {', '.join(gaps) or '없음'}"
        )[:8000]
        db.commit()
    if gaps:
        summary = f"{summary}\n[남은 성과] {', '.join(gaps)}"
    return {
        "ok": ok,
        "status": run_status,
        "steps": steps,
        "message": summary[:1500],
        "unread_before": unread_before,
        "unread_after": unread_after,
        "tool_counts": tool_counts,
        "score": current_score,
        "performance": performance_delta,
        "remaining_gaps": gaps,
        "run_id": agent_run.id,
        "city_id": city_id,
    }
