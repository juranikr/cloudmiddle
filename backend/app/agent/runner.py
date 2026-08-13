"""Groq ReAct + tool-calling 에이전트 러너."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.agent.tools import TOOLS, is_useful_fetched_page, run_tool
from app.agent.model_recovery import classify_failure, make_recovery_plan
from app.config import settings
from app.agent.memory import (
    checkpoint_after_tool,
    active_work_item_for_mission,
    ensure_mission_for_task,
    evaluate_knowledge_uses,
    finish_model_recovery_attempt,
    finalize_mission,
    learn_from_recent_runs,
    mission_context,
    record_knowledge_uses,
    record_model_recovery_attempt,
    reconcile_work_items,
    retrieve_contextual_knowledge,
    rotate_blocked_work_item,
)
from app.models import (
    AgentKnowledge,
    AgentMission,
    AgentProposal,
    AgentRun,
    AgentRunStep,
    AgentSearchLog,
    AgentTask,
    AgentWorkItem,
    City,
    Marker,
    MarkerShape,
    PlaceAppeal,
    PlaceAppealStatus,
    PlaceEvent,
    PlaceImage,
    PlaceInsight,
)
from app.personalization import city_personalization_brief
from app.knowledge import normalize_knowledge_metadata
from app.place_identity import PlaceIdentityInput, same_place_candidate

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


def _marker_quality_gaps(marker: Marker) -> list[str]:
    """Return measurable missing pieces for a real place, never for a zone polygon."""
    if marker.shape != MarkerShape.point or marker.merged_into_id is not None:
        return []
    gaps: list[str] = []
    if not marker.images:
        gaps.append("image")
    if marker.zone_id is None:
        gaps.append("zone")
    if marker.last_verified_at is None:
        gaps.append("verification")
    if len((marker.description or "").strip()) < 60:
        gaps.append("description")
    if len(marker.insights or []) < 2:
        gaps.append("insights")
    return gaps


def _performance_snapshot(db: Session, city_id: int) -> dict[str, int]:
    points = (
        db.query(Marker)
        .options(joinedload(Marker.images), joinedload(Marker.insights))
        .filter(
            Marker.city_id == city_id,
            Marker.shape == MarkerShape.point,
            Marker.merged_into_id.is_(None),
        )
        .all()
    )
    quality = {marker.id: _marker_quality_gaps(marker) for marker in points}
    snapshot = {
        "unread": count_unread(db, city_id),
        "active_places": len(points),
        "imageless_places": sum("image" in gaps for gaps in quality.values()),
        "unzoned_places": sum("zone" in gaps for gaps in quality.values()),
        "unverified_places": sum("verification" in gaps for gaps in quality.values()),
        "thin_info_places": sum(
            bool({"description", "insights"} & set(gaps)) for gaps in quality.values()
        ),
        "suggested_drafts": sum(
            marker.is_agent_suggested and bool(quality[marker.id]) for marker in points
        ),
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
        "zoned_places": sum(marker.zone_id is not None for marker in points),
        "chained_places": sum(marker.chain_id is not None for marker in points),
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
        # Reward filling coverage holes, not stacking more photos on a place
        # that already had one. Negative deltas mean a measured deficit shrank.
        + max(0, -delta.get("imageless_places", 0)) * 8
        + max(0, -delta.get("suggested_drafts", 0)) * 6
        + max(0, -delta.get("thin_info_places", 0)) * 4
        + max(0, -delta.get("unverified_places", 0)) * 3
        + max(0, -delta.get("unzoned_places", 0)) * 3
        + max(0, delta.get("images", 0)) * 0.5
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
    # A gap must describe a traveler-facing outcome, not whether the model called
    # a particular orchestration tool.  Persisting tool checklists as high-priority
    # work made later cycles select vague tasks instead of exact place-quality work.
    active = snapshot.get("active_places", 0)
    if snapshot.get("imageless_places", 0):
        gaps.append(f"사진 없는 실제 장소 {snapshot['imageless_places']}/{active}")
    if snapshot.get("suggested_drafts", 0):
        gaps.append(f"에이전트 초안 품질 미달 {snapshot['suggested_drafts']}곳")
    if snapshot.get("thin_info_places", 0):
        gaps.append(f"구조화 정보 부족 {snapshot['thin_info_places']}곳")
    if snapshot.get("unzoned_places", 0):
        gaps.append(f"구역 미배정 실제 장소 {snapshot['unzoned_places']}곳")
    if snapshot.get("unverified_places", 0):
        gaps.append(f"운영·존재 미검증 {snapshot['unverified_places']}곳")
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

# A data-integrity mission may inspect operational records and persist only its
# own audit result.  Keep this separate from ordinary verification: tools such
# as ``verify_place`` update ``last_verified_at`` even for an uncertain result,
# which would hide the very inconsistency the audit is meant to surface.
DATA_INTEGRITY_TOOLS = frozenset({
    "get_place",
    "list_places",
    "list_agent_tasks",
    "web_search",
    "fetch_page",
    "geocode_place",
    "upsert_agent_task",
})
DATA_INTEGRITY_LIST_TASKS_LIMIT = 2
DATA_INTEGRITY_TASK_RESULT_STATUSES = frozenset({"completed", "blocked"})

MODEL_OUTPUT_FAILURE_MARKERS = {
    "output_parse_failed": ("output_parse_failed", "parsing failed"),
    "tool_schema_failed": ("tool_use_failed", "failed to parse tool call", "tool call validation failed"),
}

RECOVERY_TOOLS_BY_TASK = {
    "quality_images": {"get_place", "search_place_images", "attach_image_from_url", "upsert_agent_task"},
    "quality_verification": {"get_place", "web_search", "fetch_page", "verify_place", "upsert_agent_task"},
    "quality_zones": {"get_place", "list_zones", "assign_place_zone", "upsert_agent_task"},
    "quality_information": {
        "get_place", "web_search", "fetch_page", "update_place_fields",
        "upsert_place_insights", "upsert_agent_task",
    },
    "quality_drafts": {
        "get_place", "web_search", "fetch_page", "update_place_fields",
        "upsert_place_insights", "verify_place", "list_zones", "assign_place_zone",
        "search_place_images", "attach_image_from_url", "upsert_agent_task",
    },
    "data_integrity": DATA_INTEGRITY_TOOLS,
}


def _model_output_failure_kind(detail: str) -> str:
    kind = classify_failure(detail)
    return kind if kind in MODEL_OUTPUT_FAILURE_MARKERS else ""


def _filtered_tools(tool_names: list[str] | set[str] | None) -> list[dict[str, Any]]:
    selected = set(tool_names or [])
    if not selected:
        return TOOLS
    filtered = [tool for tool in TOOLS if tool["function"]["name"] in selected]
    return filtered or TOOLS


def _model_recovery_plan(
    *,
    failure_kind: str,
    attempt: int,
    model: str,
    mission: AgentMission | None,
    work_item: AgentWorkItem | None,
    prompt_chars: int,
) -> dict[str, Any]:
    """Escalate from a focused retry to a compact minimal-tool retry."""

    task_kind = mission.kind if mission is not None else ""
    recovery_history: list[dict[str, Any]] = []
    if mission is not None:
        try:
            mission_strategy = json.loads(getattr(mission, "strategy", "") or "{}")
            recovery_history = [
                dict(item) for item in mission_strategy.get("recovery_history", [])
                if isinstance(item, dict) and item.get("failure_kind") == failure_kind
            ] if isinstance(mission_strategy, dict) else []
        except (TypeError, json.JSONDecodeError):
            recovery_history = []
    failed_modes = {
        str(item.get("strategy", {}).get("mode") or item.get("mode") or "")
        for item in recovery_history
        if item.get("outcome") == "failed"
    }
    recovered_modes = [
        str(item.get("strategy", {}).get("mode") or item.get("mode") or "")
        for item in recovery_history
        if item.get("outcome") == "recovered"
    ]
    tool_names = set(RECOVERY_TOOLS_BY_TASK.get(task_kind, set()))
    next_action: dict[str, Any] = {}
    if work_item is not None:
        try:
            parsed = json.loads(work_item.next_action or "{}")
            next_action = parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            next_action = {}
    next_tool = str(next_action.get("tool") or "")
    if next_tool and any(tool["function"]["name"] == next_tool for tool in TOOLS):
        tool_names.add(next_tool)
    if not tool_names:
        tool_names.update({"get_place", "list_agent_tasks", "upsert_agent_task"})

    mode_order = ["focused_retry", "compact_retry", "minimal_retry"]
    mode_index = min(max(attempt - 1, 0), len(mode_order) - 1)
    if recovered_modes and recovered_modes[-1] in mode_order:
        mode_index = max(mode_index, mode_order.index(recovered_modes[-1]))
    while mode_index < len(mode_order) - 1 and mode_order[mode_index] in failed_modes:
        mode_index += 1
    mode = mode_order[mode_index]

    if mode == "focused_retry":
        reasoning_effort = "medium"
        force_compaction = False
        recent_round_limit = 4
        max_chars = 60_000
    elif mode == "compact_retry":
        reasoning_effort = "low"
        force_compaction = True
        recent_round_limit = 3
        max_chars = 42_000
    else:
        reasoning_effort = "low"
        force_compaction = True
        recent_round_limit = 2
        max_chars = 28_000
        minimal = {next_tool, "get_place", "upsert_agent_task"}
        if task_kind == "quality_images":
            minimal.update({"search_place_images", "attach_image_from_url"})
        tool_names.intersection_update(name for name in minimal if name)

    return {
        "failure_kind": failure_kind,
        "attempt": attempt,
        "mode": mode,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "force_compaction": force_compaction,
        "recent_round_limit": recent_round_limit,
        "max_chars": max_chars,
        "tool_names": sorted(tool_names),
        "prompt_chars_before": prompt_chars,
        "adapted_from_history": bool(recovery_history),
        "historically_failed_modes": sorted(mode for mode in failed_modes if mode),
        "historically_recovered_mode": recovered_modes[-1] if recovered_modes else "",
    }


def _should_rotate_exhausted_image_target(
    *,
    mission: AgentMission | None,
    work_item: AgentWorkItem | None,
    target_mismatch: dict[str, Any] | None,
    image_searches_by_place: dict[int, int],
) -> bool:
    return bool(
        target_mismatch
        and mission is not None
        and mission.kind == "quality_images"
        and work_item is not None
        and work_item.place_id is not None
        and image_searches_by_place.get(int(work_item.place_id), 0) >= 3
    )
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

# Tools whose ``place_id`` must follow the durable work-item cursor.  Read-only
# list tools are intentionally excluded because they can provide mission-wide
# context, but an explicit action on another place is model drift and must not
# silently steal or contaminate the current target.
TARGET_SCOPED_PLACE_TOOLS = {
    "get_place",
    "find_nearby_candidates",
    "update_place_context",
    "update_place_fields",
    "upsert_place_insights",
    "reorder_images",
    "upsert_knowledge",
    "search_place_images",
    "attach_image_from_url",
    "verify_place",
    "assign_place_zone",
    "assign_place_chain",
}


def _tool_signature(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"


def _normalize_research_query(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _active_target_mismatch(
    name: str,
    args: dict[str, Any],
    work_item: AgentWorkItem | None,
    *,
    mission_kind: str = "",
) -> dict[str, Any] | None:
    """Reject a target-scoped tool call that drifts from the durable cursor."""

    if mission_kind == "data_integrity" and name == "get_place":
        # Integrity audits must compare the active record with duplicates,
        # anchors, and conflicting branches. ``get_place`` is city-scoped and
        # read-only, so cross-target reads are safe; every mutation remains
        # unavailable through DATA_INTEGRITY_TOOLS and the runtime guard.
        return None
    if (
        work_item is None
        or work_item.place_id is None
        or name not in TARGET_SCOPED_PLACE_TOOLS
        or args.get("place_id") is None
    ):
        return None
    try:
        requested_place_id = int(args["place_id"])
    except (TypeError, ValueError):
        return None
    active_place_id = int(work_item.place_id)
    if requested_place_id == active_place_id:
        return None
    return {
        "error": "active_work_item_mismatch",
        "detail": (
            f"현재 연속 작업 대상은 장소 #{active_place_id} ({work_item.title})입니다. "
            f"장소 #{requested_place_id} 호출은 실행하지 않았습니다. 현재 대상의 완료 조건을 "
            "충족하거나 차단 근거를 남긴 뒤 오케스트레이터가 다음 대상으로 전환하게 하세요."
        ),
        "active_place_id": active_place_id,
        "requested_place_id": requested_place_id,
    }


def _active_agent_task_mismatch(
    name: str,
    args: dict[str, Any],
    mission: AgentMission | None,
) -> dict[str, Any] | None:
    """Keep a data-integrity audit from creating or editing another task."""

    if mission is None or mission.kind != "data_integrity" or name != "upsert_agent_task":
        return None
    active_task_id = mission.task_id
    raw_task_id = args.get("task_id")
    requested_task_id = (
        raw_task_id
        if isinstance(raw_task_id, int) and not isinstance(raw_task_id, bool)
        else None
    )
    if active_task_id is not None and requested_task_id == active_task_id:
        return None
    return {
        "error": "active_agent_task_mismatch",
        "detail": (
            "data_integrity 과제는 현재 활성 과제의 결과만 기록할 수 있습니다. "
            f"task_id={active_task_id}를 명시해 다시 호출하세요. task_id 누락, 새 과제 생성, "
            "다른 과제 수정은 실행하지 않습니다."
        ),
        "active_task_id": active_task_id,
        "requested_task_id": requested_task_id,
    }


def _project_data_integrity_task_result_args(
    name: str,
    args: dict[str, Any],
    mission: AgentMission | None,
    task: AgentTask | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Project an integrity-task write onto its narrow server-owned shape."""

    if mission is None or mission.kind != "data_integrity" or name != "upsert_agent_task":
        return args, None
    if (
        task is None
        or task.id != mission.task_id
        or task.city_id != mission.city_id
        or task.kind != "data_integrity"
    ):
        return {}, {
            "error": "active_agent_task_not_writable",
            "detail": (
                "서버가 활성 data_integrity 과제 원본을 확인하지 못해 결과를 기록하지 않았습니다. "
                "새 과제를 만들거나 다른 종류의 과제로 대체하지 않습니다."
            ),
            "active_task_id": mission.task_id,
        }
    status = str(args.get("status") or "").strip().lower()
    if status not in DATA_INTEGRITY_TASK_RESULT_STATUSES:
        return {}, {
            "error": "invalid_data_integrity_task_status",
            "detail": (
                "data_integrity 과제 결과는 completed 또는 blocked로만 종료할 수 있습니다. "
                "기존 과제 정의는 서버가 보존하며 pending 전환이나 임의 상태 변경은 실행하지 않습니다."
            ),
            "allowed_statuses": sorted(DATA_INTEGRITY_TASK_RESULT_STATUSES),
            "requested_status": status or None,
        }
    # Deliberately omit every model-supplied definition field. run_tool resolves
    # the existing task by the already-validated ID and therefore preserves
    # kind/title/detail/success_metric/priority; only the terminal verdict is
    # writable by the model.
    return {
        "task_id": mission.task_id,
        "status": status,
        "result": str(args.get("result") or "")[:8000],
    }, None


def _halt_stalled_mission(
    db: Session,
    *,
    mission: AgentMission | None,
    work_item: AgentWorkItem | None,
    run_id: int,
    sequence: int,
    reason: str,
) -> AgentWorkItem | None:
    """Checkpoint and rotate a target before ending a no-progress run."""

    if mission is None or work_item is None:
        return work_item
    halt_sequence = sequence + 1
    result = {
        "error": "no_progress_limit_reached",
        "detail": reason,
    }
    try:
        db.add(AgentRunStep(
            run_id=run_id,
            sequence=halt_sequence,
            phase="orchestrate",
            tool="orchestrator_no_progress",
            outcome="blocked",
            score_delta=0,
            detail=_step_detail_json(
                {"work_item_id": work_item.id},
                result,
                {"material_change": False, "new_evidence": 0},
            ),
        ))
        stalled_item, _ = checkpoint_after_tool(
            db,
            mission=mission,
            work_item=work_item,
            run_id=run_id,
            sequence=halt_sequence,
            tool="orchestrator_no_progress",
            args={"work_item_id": work_item.id},
            result=result,
            outcome="blocked",
            new_evidence_count=0,
            material_change=False,
        )
        next_item = rotate_blocked_work_item(
            db,
            mission=mission,
            current=stalled_item,
            run_id=run_id,
            reason=reason,
            activate_next=False,
            commit=False,
        )
        progress = json.loads(mission.progress or "{}")
        if not isinstance(progress, dict):
            progress = {}
        progress.update({
            "active_work_item_id": None,
            "resume_work_item_id": next_item.id if next_item is not None else None,
            "last_checkpoint_sequence": halt_sequence,
            "last_outcome": "blocked",
            "pause_reason": reason[:1000],
            "retry_condition": "새 근거 또는 12시간 냉각 후 다음 대상으로 재개",
        })
        mission.status = "paused"
        mission.progress = json.dumps(progress, ensure_ascii=False)
        mission.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        raise
    # The completed run remains attributed to the target it actually handled;
    # the ready sibling is only a future resume cursor.
    return stalled_item


def _mission_has_no_executable_target(
    mission: AgentMission | None,
    work_item: AgentWorkItem | None,
) -> bool:
    """True when the orchestrator has deliberately ended this tool round."""

    return bool(
        mission is not None
        and work_item is None
        and mission.status in {"paused", "completed"}
    )


def _is_material_change(name: str, result: Any) -> bool:
    if name not in MUTATION_TOOLS or not isinstance(result, dict) or result.get("error"):
        return False
    # Backlog bookkeeping is an orchestration aid, not a traveler-facing DB
    # improvement. It must not reset the no-material-progress guard or appear in
    # the run's "actual changes" list.
    if name == "upsert_agent_task":
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
        if not result.get("already_visited") and is_useful_fetched_page(result):
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
            if not isinstance(item, dict):
                continue
            image_url = item.get("image_url") or item.get("url")
            if image_url:
                candidates.add(f"image:{image_url}")
    return candidates - seen


def _evidence_handoff_items(name: str, result: Any, new_keys: set[str]) -> list[str]:
    """Compact useful evidence so a later run can continue instead of re-searching."""
    if not new_keys or not isinstance(result, dict):
        return []
    items: list[str] = []
    if name == "web_search":
        for item in result.get("results") or []:
            if not isinstance(item, dict) or not item.get("href"):
                continue
            if f"url:{item['href']}" not in new_keys:
                continue
            title = str(item.get("title") or "제목 없음").strip()
            snippet = str(item.get("body") or item.get("snippet") or "").strip()
            items.append(f"검색 | {title[:100]} | {item['href']} | {snippet[:180]}")
    elif name == "fetch_page":
        title = str(result.get("title") or "제목 없음").strip()
        url = str(result.get("url") or "").strip()
        body = " ".join(str(result.get("text") or "").split())
        items.append(f"본문 | {title[:100]} | {url} | {body[:240]}")
    elif name == "geocode_place":
        for item in (result.get("results") or [])[:3]:
            if isinstance(item, dict):
                items.append(
                    "좌표 | "
                    f"{str(item.get('name') or item.get('display_name') or '')[:100]} | "
                    f"{item.get('source')}:{item.get('external_id')} | "
                    f"{item.get('lat')},{item.get('lng')}"
                )
    elif name == "search_place_images":
        for item in (result.get("results") or [])[:3]:
            if isinstance(item, dict):
                items.append(
                    f"이미지 | {str(item.get('title') or '')[:100]} | "
                    f"{item.get('image_url') or item.get('url')}"
                )
    return [item[:700] for item in items if item.strip()]


def _matching_coordinate_evidence(
    args: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    city_name: str,
) -> dict[str, Any] | None:
    """Resolve a proposal to a coordinate record observed by this run."""

    incoming = PlaceIdentityInput(
        city=city_name,
        title=str(args.get("title") or ""),
        chain_name=str(args.get("chain_name_local") or ""),
        branch_name=str(args.get("branch_name") or ""),
        address=str(args.get("address") or args.get("description") or ""),
        lat=args.get("lat"),
        lng=args.get("lng"),
    )
    matches: list[tuple[float, dict[str, Any]]] = []
    for record in records:
        decision = same_place_candidate(
            incoming,
            PlaceIdentityInput(
                city=city_name,
                title=str(record.get("display_name") or record.get("title") or ""),
                branch_name=str(record.get("branch_name") or ""),
                address=str(record.get("address") or ""),
                lat=record.get("lat"),
                lng=record.get("lng"),
            ),
        )
        if decision.same:
            matches.append((decision.distance_m if decision.distance_m is not None else 10**9, record))
    return dict(min(matches, key=lambda item: item[0])[1]) if matches else None


def _run_outcome_status(*, unread_after: int, gaps: list[str], material_change_count: int) -> str:
    """Run completion is distinct from whether the city's long-term backlog is empty."""
    if unread_after > 0:
        return "partial"
    if material_change_count > 0:
        return "completed"
    return "completed" if not gaps else "partial"


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


def _step_detail_json(
    args: dict[str, Any],
    result: Any,
    progress: dict[str, Any],
    *,
    max_chars: int = 12000,
) -> str:
    """Keep run history useful and valid without turning it into a raw-data dump."""
    payload = {"args": args, "result": result, "progress": progress}
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return encoded

    if isinstance(result, dict):
        compact_result = {
            key: result[key]
            for key in (
                "ok", "error", "detail", "status", "proposal_id", "place_id", "task_id",
                "changed", "created", "marked", "merged", "resolved", "url", "title",
                "already_visited",
            )
            if key in result
        }
        rows = result.get("results")
        if isinstance(rows, list):
            compact_result["result_count"] = len(rows)
            compact_result["results_preview"] = rows[:5]
    elif isinstance(result, list):
        compact_result = {"result_count": len(result), "results_preview": result[:5]}
    else:
        compact_result = {"preview": str(result)[:2000]}

    compact_payload = {
        "args": args,
        "result": compact_result,
        "progress": progress,
        "truncated": True,
    }
    encoded = json.dumps(compact_payload, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return encoded
    # Even unusually large arguments/previews remain valid JSON.
    fallback = json.dumps(
        {
            "args_preview": json.dumps(args, ensure_ascii=False, default=str)[:max(200, max_chars // 5)],
            "result_preview": json.dumps(compact_result, ensure_ascii=False, default=str)[:max(500, max_chars // 2)],
            "progress": progress,
            "truncated": True,
        },
        ensure_ascii=False,
        default=str,
    )
    if len(fallback) <= max_chars:
        return fallback
    return json.dumps({"progress": progress, "truncated": True}, ensure_ascii=False, default=str)


def _compact_react_messages(
    messages: list[dict[str, Any]],
    *,
    tool_counts: dict[str, int],
    material_changes: list[dict[str, Any]],
    current_score: float,
    max_chars: int = 120_000,
    force: bool = False,
    recent_round_limit: int = 6,
) -> tuple[list[dict[str, Any]], bool]:
    """Keep long autonomous runs inside provider context limits.

    Preserve the stable system/objective prompts and the most recent complete
    assistant→tool rounds. Older raw pages are already persisted in run steps;
    feeding all of them back to the model only makes a successful long run fail
    at final synthesis.
    """
    if not force and len(json.dumps(messages, ensure_ascii=False, default=str)) <= max_chars:
        return messages, False
    prefix = messages[:2]
    rounds: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages[2:]:
        if message.get("role") in {"assistant", "user"} and current:
            rounds.append(current)
            current = []
        current.append(message)
    if current:
        rounds.append(current)
    recent_rounds = rounds[-max(1, recent_round_limit):]
    compact_note = {
        "role": "user",
        "content": (
            "【이전 ReAct 문맥 자동 압축】 오래된 원문·도구 응답은 AgentRunStep에 보존되어 있습니다. "
            f"현재 성과 점수 {current_score}, 실제 변경 {len(material_changes)}건. "
            f"도구 누계: {json.dumps(tool_counts, ensure_ascii=False)}. "
            "최근 관찰만 사용해 현재 1차 목표를 이어가세요. 필요한 현재 상태는 list_agent_tasks와 "
            "list_places/get_place로 다시 확인하되, 이미 끝낸 장소를 반복 조사하지 마세요."
        ),
    }
    def assemble() -> list[dict[str, Any]]:
        recent = [item for round_messages in recent_rounds for item in round_messages]
        return [*prefix, compact_note, *recent]

    compacted = assemble()
    # Drop complete rounds so an assistant tool call is never separated from
    # its matching tool results. One final large round is kept intact.
    while len(recent_rounds) > 1 and len(json.dumps(compacted, ensure_ascii=False, default=str)) > max_chars:
        recent_rounds = recent_rounds[1:]
        compacted = assemble()
    return compacted, True


QUALITY_TASK_KINDS = {
    "quality_images",
    "quality_drafts",
    "quality_information",
    "quality_zones",
    "quality_verification",
}


def _quality_target_line(marker: Marker, gaps: list[str]) -> str:
    labels = {
        "image": "사진 없음",
        "zone": "구역 미배정",
        "verification": "미검증",
        "description": "짧은 소개",
        "insights": "구조화 정보 부족",
    }
    return f"- #{marker.id} {marker.title} (현재: {', '.join(labels[item] for item in gaps)})"


def _canonical_quality_task(
    db: Session,
    *,
    city_id: int,
    kind: str,
    now: datetime,
) -> AgentTask | None:
    """Keep one DB-derived quality task and one active mission per dimension.

    Older agents could mark the parent task complete while its durable mission
    still had an active place, then create a duplicate task in the same run.
    Prefer the task already owned by the newest unfinished active mission; only
    fall back to the latest task when there is no durable cursor to preserve.
    """

    rows = (
        db.query(AgentTask)
        .filter(AgentTask.city_id == city_id, AgentTask.kind == kind)
        .order_by(AgentTask.id.desc())
        .all()
    )
    if not rows:
        return None
    row_by_id = {row.id: row for row in rows}
    active_missions = (
        db.query(AgentMission)
        .join(AgentWorkItem, AgentWorkItem.mission_id == AgentMission.id)
        .filter(
            AgentMission.city_id == city_id,
            AgentMission.kind == kind,
            AgentMission.status == "active",
            AgentMission.task_id.in_(row_by_id),
            AgentWorkItem.status.in_(("active", "ready")),
        )
        .order_by(AgentMission.updated_at.desc(), AgentMission.id.desc())
        .all()
    )
    canonical_mission = active_missions[0] if active_missions else None
    canonical = row_by_id.get(canonical_mission.task_id) if canonical_mission else rows[0]
    if canonical is None:
        canonical = rows[0]

    seen_missions: set[int] = set()
    for mission in active_missions:
        if mission.id in seen_missions:
            continue
        seen_missions.add(mission.id)
        if canonical_mission is None or mission.id == canonical_mission.id:
            continue
        try:
            parsed_progress = json.loads(mission.progress or "{}")
            progress = parsed_progress if isinstance(parsed_progress, dict) else {}
        except (TypeError, json.JSONDecodeError):
            progress = {}
        progress.update({
            "pause_reason": "duplicate managed quality mission consolidated",
            "superseded_by_mission_id": canonical_mission.id,
        })
        mission.progress = json.dumps(progress, ensure_ascii=False)
        mission.status = "paused"
        mission.updated_at = now

    for duplicate in rows:
        if duplicate.id == canonical.id or duplicate.status != "pending":
            continue
        duplicate.status = "completed"
        duplicate.completed_at = now
        duplicate.result = (
            f"동일 품질 차원의 지속 과제 #{canonical.id}에 통합됨; "
            "체크포인트와 실행 이력은 기존 미션에 보존됩니다."
        )[:8000]
    return canonical


def _sync_quality_tasks(
    db: Session,
    *,
    city_id: int,
    run_id: int | None = None,
) -> list[int]:
    """Materialize current DB quality holes as small, self-validating work batches.

    The model used to mention follow-up work only in prose. These tasks are
    derived from live rows and reopened when the model claims completion without
    satisfying the metric, so later scheduled runs cannot silently forget them.
    """
    points = (
        db.query(Marker)
        .options(joinedload(Marker.images), joinedload(Marker.insights))
        .filter(
            Marker.city_id == city_id,
            Marker.shape == MarkerShape.point,
            Marker.merged_into_id.is_(None),
        )
        .all()
    )
    gaps_by_id = {marker.id: _marker_quality_gaps(marker) for marker in points}

    def ranked(markers: list[Marker], limit: int) -> list[Marker]:
        return sorted(
            markers,
            key=lambda marker: (
                not marker.is_agent_suggested,
                -len(gaps_by_id[marker.id]),
                marker.id,
            ),
        )[:limit]

    specs: dict[str, dict[str, Any]] = {
        "quality_images": {
            "title": "자동 품질 보강: 사진 없는 실제 장소",
            "priority": 100,
            "targets": ranked([m for m in points if "image" in gaps_by_id[m.id]], 6),
            "instructions": (
                "각 ID마다 search_place_images(query, place_id) 결과 중 제목이 장소와 일치하는 자유 라이선스 "
                "실사진만 attach_image_from_url로 첨부하세요. 단순 인근 사진·로고·지도는 금지합니다. "
                "후보 3개가 모두 실패하면 오류와 시도한 출처를 result에 남기고 다음 ID로 이동하세요."
            ),
            "metric": "아래 모든 대상의 image_count가 1 이상",
        },
        "quality_drafts": {
            "title": "자동 품질 보강: 에이전트 초안 완성",
            "priority": 96,
            "targets": ranked(
                [m for m in points if m.is_agent_suggested and gaps_by_id[m.id]], 4
            ),
            "instructions": (
                "각 초안의 현재 결손만 채우세요. 출처가 있는 위치·역사·방문정보는 upsert_place_insights, "
                "실제 운영 여부는 verify_place, 구역은 assign_place_zone, 짧거나 부정확한 한국어 소개는 "
                "update_place_fields로 정제합니다. 사진 결손도 같은 방식으로 보강하되 근거 없는 내용을 만들지 마세요."
            ),
            "metric": "아래 대상에서 사진·구역·검증·소개·구조화 정보 결손이 모두 제거됨",
        },
        "quality_information": {
            "title": "자동 품질 보강: 장소 정보 구조화",
            "priority": 86,
            "targets": ranked(
                [
                    m
                    for m in points
                    if {"description", "insights"} & set(gaps_by_id[m.id])
                ],
                5,
            ),
            "instructions": (
                "쓰레기통식 설명 누적은 금지합니다. 짧은 소개는 한국어로 정제하고, 위치 의미와 역사 또는 "
                "방문정보를 서로 분리한 인사이트 최소 2개로 저장하며 각각 실제 URL과 confidence를 기록하세요."
            ),
            "metric": "아래 대상의 한국어 소개가 60자 이상이고 구조화 인사이트가 2개 이상",
        },
        "quality_zones": {
            "title": "자동 품질 보강: 구역 미배정 장소",
            "priority": 82,
            "targets": ranked([m for m in points if "zone" in gaps_by_id[m.id]], 8),
            "instructions": (
                "list_zones와 실제 좌표를 확인해 포함되는 기존 구역에 assign_place_zone으로 배정하세요. "
                "기존 어느 구역에도 속하지 않으면 억지로 배정하지 말고 result에 사유를 남기세요."
            ),
            "metric": "아래 대상에 타당한 zone_id가 있거나 구역 밖이라는 검증 결과가 기록됨",
        },
        "quality_verification": {
            "title": "자동 품질 보강: 운영·존재 검증",
            "priority": 78,
            "targets": ranked([m for m in points if "verification" in gaps_by_id[m.id]], 6),
            "instructions": (
                "공식·지도·신뢰 가능한 여행 출처에서 현재 운영/존재 여부를 확인하고 verify_place를 호출하세요. "
                "불확실하면 uncertain으로 정직하게 기록하고 같은 검색을 반복하지 마세요."
            ),
            "metric": "아래 모든 대상의 last_verified_at이 기록됨",
        },
    }

    task_ids: list[int] = []
    now = datetime.now(timezone.utc)
    for kind, spec in specs.items():
        row = _canonical_quality_task(db, city_id=city_id, kind=kind, now=now)
        targets: list[Marker] = spec["targets"]
        if not targets:
            if row is not None and row.status != "completed":
                row.status = "completed"
                row.completed_at = now
                row.result = (
                    f"실행 #{run_id}: 운영 DB 재측정 결과 해당 품질 결손이 없습니다."
                    if run_id
                    else "운영 DB 재측정 결과 해당 품질 결손이 없습니다."
                )
            continue

        detail = spec["instructions"] + "\n대상:\n" + "\n".join(
            _quality_target_line(marker, gaps_by_id[marker.id]) for marker in targets
        )
        if row is None:
            row = AgentTask(city_id=city_id, kind=kind, title=spec["title"])
            db.add(row)
            db.flush()
        target_changed = row.detail != detail
        # Keep the dimension-level attempt count while a task is still open.
        # Otherwise every small target-list change (for example one new photo)
        # resets the image task to priority 100 and starves drafts/info again.
        # A genuinely completed dimension that later gains a new gap starts fresh.
        if target_changed and row.status == "completed":
            row.attempts = 0
        was_false_completion = row.status == "completed" and not target_changed
        if row.status != "pending":
            previous = (row.result or "")[-2500:]
            row.result = (
                f"실행 #{run_id}: 완료 주장 후 운영 DB 재측정에서 결손이 남아 자동 재개했습니다."
                + (f" 이전 결과: {previous}" if previous else "")
            )[:8000]
        row.title = spec["title"]
        row.detail = detail
        row.success_metric = spec["metric"]
        # One unchanged attempted cycle is enough to cool this dimension down.
        # Exact local-business photos are often absent from free-media sources;
        # the next run must progress drafts/info instead of retrying forever.
        cooldown = 36 if row.attempts >= 1 or was_false_completion else 0
        row.priority = max(50, int(spec["priority"]) - cooldown)
        row.status = "pending"
        row.completed_at = None
        task_ids.append(row.id)
    db.commit()
    return task_ids


def _ensure_gap_tasks(db: Session, *, city_id: int, run_id: int, gaps: list[str]) -> list[int]:
    """Persist measurable follow-up work so the next cycle has a concrete objective."""
    # Row-level quality gaps have stable, exact-ID tasks managed above. Persisting
    # their changing counts here would create a new vague task after every image.
    quality_prefixes = (
        "사진 없는 실제 장소",
        "에이전트 초안 품질 미달",
        "구조화 정보 부족",
        "구역 미배정 실제 장소",
        "운영·존재 미검증",
    )
    procedural_gaps = {"이전 조사 백로그 확인", "구역 현황 확인"}
    gaps = [
        gap for gap in gaps
        if not gap.startswith(quality_prefixes) and gap not in procedural_gaps
    ]
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
    personalization_hint = city_personalization_brief(db, city_id=city_id)[:7000]
    quality_task_ids_before = (
        _sync_quality_tasks(db, city_id=city_id) if allow_research else []
    )
    primary_task = None
    active_mission = None
    active_work_item = None
    if allow_research:
        resumable_mission = (
            db.query(AgentMission)
            .join(AgentWorkItem, AgentWorkItem.mission_id == AgentMission.id)
            .filter(
                AgentMission.city_id == city_id,
                AgentMission.status == "active",
                AgentWorkItem.status.in_(("active", "ready")),
            )
            .order_by(AgentMission.updated_at.desc(), AgentMission.id.desc())
            .first()
        )
        if resumable_mission is not None:
            primary_task = db.get(AgentTask, resumable_mission.task_id)
        else:
            candidates = (
                db.query(AgentTask)
                .filter(AgentTask.city_id == city_id, AgentTask.status == "pending")
                .order_by(AgentTask.priority.desc(), AgentTask.created_at.asc())
                .all()
            )
            cooldown_cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
            for candidate in candidates:
                paused = db.query(AgentMission).filter(
                    AgentMission.city_id == city_id,
                    AgentMission.task_id == candidate.id,
                    AgentMission.status == "paused",
                    AgentMission.updated_at > cooldown_cutoff,
                ).first()
                if paused is None:
                    primary_task = candidate
                    break
        if primary_task is not None:
            primary_task.attempts += 1
            active_mission, active_work_item = ensure_mission_for_task(db, primary_task)
            db.commit()
        elif unread_before == 0:
            # Every durable mission is completed or intentionally cooling down.
            # Starting a free-form research loop here loses the explicit cursor
            # and tends to repeat broad searches without a measurable target.
            # Record a normal idle cycle and let the next schedule re-evaluate
            # cooldowns, new user events, and newly synchronized quality gaps.
            performance = _performance_snapshot(db, city_id)
            idle_run = AgentRun(
                city_id=city_id,
                mode="idle",
                status="completed",
                objective="No executable durable target; wait for retry conditions or new input.",
                score=0,
                metrics=json.dumps({
                    "before": performance,
                    "after": performance,
                    "delta": {},
                    "tool_counts": {},
                    "material_changes": [],
                    "material_change_count": 0,
                    "idle_reason": "all_durable_targets_terminal_or_cooling_down",
                    "quality_task_ids_before": quality_task_ids_before,
                }, ensure_ascii=False),
                summary=(
                    "실행 가능한 지속 과제가 없습니다. 완료·차단 상태와 재시도 조건을 유지한 채 "
                    "새 사용자 입력, 새 품질 공백 또는 냉각 시간 경과를 기다립니다."
                ),
                finished_at=datetime.now(timezone.utc),
            )
            db.add(idle_run)
            db.commit()
            db.refresh(idle_run)
            return {
                "ok": True,
                "status": "completed",
                "steps": 0,
                "message": idle_run.summary,
                "unread_before": 0,
                "unread_after": 0,
                "tool_counts": {},
                "score": 0,
                "performance": {},
                "remaining_gaps": _research_gaps({}, {}, performance),
                "run_id": idle_run.id,
                "city_id": city_id,
            }
    continuity_hint = (
        json.dumps(mission_context(active_mission, active_work_item), ensure_ascii=False)[:7000]
        if active_mission is not None and active_work_item is not None
        else "{}"
    )
    primary_task_hint = (
        f"백로그 #{primary_task.id} '{primary_task.title}'. 상세: {primary_task.detail or '없음'}. "
        f"이전 실행 인계: {(primary_task.result or '없음')[:3000]}. "
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
        mission_id=active_mission.id if active_mission is not None else None,
        work_item_id=active_work_item.id if active_work_item is not None else None,
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
    normalize_knowledge_metadata(db, city_id=city_id)
    learn_from_recent_runs(db, city_id=city_id)
    retrieved_knowledge = retrieve_contextual_knowledge(
        db,
        city_id=city_id,
        mission=active_mission,
        work_item=active_work_item,
        query=primary_task_hint,
        limit=10,
    )
    record_knowledge_uses(
        db,
        run=agent_run,
        mission=active_mission,
        work_item=active_work_item,
        retrieved=retrieved_knowledge,
    )
    kb_hint = json.dumps(retrieved_knowledge, ensure_ascii=False)[:7000]

    if research_only:
        user_msg = (
            f"현재 실행 도시는 {city.name_ko}({city.name_local}), city_id={city.id}입니다.\n"
            "현재 미읽음 작업은 없습니다. 연구 사이클을 수행하세요.\n"
            f"우선 조사 테마: {_research_themes(city)}\n"
            f"현재 상황에 맞게 검색된 지식·검증된 교훈: {kb_hint}\n"
            f"이전 실행에서 이어받은 작업 체크포인트: {continuity_hint}\n"
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
            f"현재 상황에 맞게 검색된 지식·검증된 교훈: {kb_hint}\n"
            f"이전 실행에서 이어받은 작업 체크포인트: {continuity_hint}\n"
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
            f"현재 상황에 맞게 검색된 지식·검증된 교훈: {kb_hint}\n"
            f"이전 실행에서 이어받은 작업 체크포인트: {continuity_hint}\n"
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

    user_msg += (
        "\n\n【사용자 행동 기반 개인화 관찰】\n"
        f"{personalization_hint or '[]'}\n"
        "이 데이터는 대화·즐겨찾기·직접 추가·일정·이의제기에서 계산한 여행 행동 신호다. 민감한 속성이나 "
        "확정 취향으로 확대 해석하지 말고, 실제 추천 근거로만 사용한다. 반복된 음료 브랜드는 같은 브랜드의 "
        "다른 지점과 검증된 유사 음료 브랜드 발굴로 연결하고, lodging anchor는 가까운 음식점·접근성 좋은 관광지 "
        "조사의 거점으로 사용한다. 이의제기는 거부 취향이 아니라 교정 조건이다. 이미 추천된 place_id는 중복 신규 "
        "제안하지 말고, 외부 후보는 기존과 동일하게 출처·좌표·중복을 검증한 뒤 propose_place로 남긴다."
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
    recent_search_queries = {
        _normalize_research_query(row[0])
        for row in db.query(AgentSearchLog.query).filter(
            AgentSearchLog.city_id == city_id,
            AgentSearchLog.searched_at >= datetime.now(timezone.utc) - timedelta(days=7),
            AgentSearchLog.results_count > 0,
        ).order_by(AgentSearchLog.searched_at.desc()).limit(500)
        if _normalize_research_query(row[0])
    }
    evidence_keys: set[str] = set()
    validated_source_urls: set[str] = set()
    verified_coordinate_records: list[dict[str, Any]] = []
    material_changes: list[dict[str, Any]] = []
    repeated_calls = 0
    image_searches_by_place: dict[int, int] = {}
    work_nudges = 0
    progress_nudges = 0
    material_nudges = 0
    model_recovery_attempts = 0
    model_recovery_history: list[dict[str, Any]] = []
    pending_model_recovery: dict[str, Any] | None = None
    terminal_model_failure_kind = ""
    local_recovery_tool_names: set[str] | None = None
    malformed_tool_failures: dict[str, int] = {}
    # Compatibility fallback for provider wording not recognized by the
    # adaptive classifier.
    schema_retries = 0
    no_progress_actions = 0
    no_material_actions = 0
    research_actions_since_material = 0
    evidence_handoff: list[str] = []
    action_sequence = 0
    current_score = 0.0
    context_compactions = 0
    data_integrity_place_reads: set[int] = set()
    data_integrity_list_task_calls = 0
    try:
        for _ in range(steps_limit):
            steps += 1
            messages, compacted = _compact_react_messages(
                messages,
                tool_counts=tool_counts,
                material_changes=material_changes,
                current_score=current_score,
            )
            if compacted:
                context_compactions += 1
            try:
                extra: dict[str, Any] = {}
                active_recovery_strategy = (
                    pending_model_recovery.get("strategy") if pending_model_recovery else {}
                )
                mission_tool_names = active_recovery_strategy.get("tool_names")
                if not mission_tool_names and local_recovery_tool_names:
                    mission_tool_names = sorted(local_recovery_tool_names)
                if active_mission is not None and active_mission.kind == "data_integrity":
                    # This is a hard safety boundary, not merely a model hint.
                    # Clamp even an adaptive-recovery tool list so an old or
                    # malformed strategy can never re-introduce write tools.
                    requested = set(mission_tool_names or DATA_INTEGRITY_TOOLS)
                    mission_tool_names = sorted(
                        (requested & DATA_INTEGRITY_TOOLS) or DATA_INTEGRITY_TOOLS
                    )
                if (
                    not mission_tool_names
                    and research_only
                    and active_mission is not None
                    and active_mission.kind in RECOVERY_TOOLS_BY_TASK
                ):
                    # During autonomous quality work, make the current mission's
                    # affordances explicit. User-event runs retain the full tool
                    # set so queue processing can still resolve every event type.
                    mission_tool_names = sorted(RECOVERY_TOOLS_BY_TASK[active_mission.kind])
                if "gpt-oss" in model:
                    # 병합 판단 등 미묘한 결정의 품질을 위해 추론 강도 상향.
                    # extra_body 경유: 구버전 groq SDK도 통과시킨다.
                    extra["extra_body"] = {
                        "reasoning_effort": str(active_recovery_strategy.get("reasoning_effort") or "high")
                    }
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=_filtered_tools(mission_tool_names),
                    tool_choice="auto",
                    temperature=0.2,
                    **extra,
                )
            except Exception as exc:
                # 모델이 스키마에 안 맞는 인자(null 등)를 생성한 경우: 사이클을 죽이지 않고 교정 재시도
                detail = str(exc)
                failure_kind = _model_output_failure_kind(detail)
                if pending_model_recovery is not None:
                    finish_model_recovery_attempt(
                        db,
                        mission=active_mission,
                        work_item=active_work_item,
                        run_id=agent_run.id,
                        evidence_ref=str(pending_model_recovery["evidence_ref"]),
                        failure_kind=str(pending_model_recovery["failure_kind"]),
                        strategy=dict(pending_model_recovery["strategy"]),
                        successful=False,
                    )
                    recovery_step = db.get(AgentRunStep, pending_model_recovery.get("step_id"))
                    if recovery_step is not None:
                        recovery_step.outcome = "failed"
                    model_recovery_history.append({
                        **dict(pending_model_recovery["strategy"]),
                        "outcome": "failed",
                    })
                    pending_model_recovery = None
                    db.commit()
                if failure_kind and model_recovery_attempts < 3:
                    model_recovery_attempts += 1
                    strategy = _model_recovery_plan(
                        failure_kind=failure_kind,
                        attempt=model_recovery_attempts,
                        model=model,
                        mission=active_mission,
                        work_item=active_work_item,
                        prompt_chars=len(json.dumps(messages, ensure_ascii=False, default=str)),
                    )
                    messages, recovery_compacted = _compact_react_messages(
                        messages,
                        tool_counts=tool_counts,
                        material_changes=material_changes,
                        current_score=current_score,
                        max_chars=int(strategy["max_chars"]),
                        force=bool(strategy["force_compaction"]),
                        recent_round_limit=int(strategy["recent_round_limit"]),
                    )
                    if recovery_compacted:
                        context_compactions += 1
                    action_sequence += 1
                    recovery_step = AgentRunStep(
                        run_id=agent_run.id,
                        sequence=action_sequence,
                        phase="recover",
                        tool="model_output",
                        outcome="retry",
                        score_delta=0,
                        detail=json.dumps({
                            "failure_kind": failure_kind,
                            "error": detail[:1200],
                            "strategy": strategy,
                        }, ensure_ascii=False, default=str),
                    )
                    db.add(recovery_step)
                    db.flush()
                    evidence_ref = record_model_recovery_attempt(
                        db,
                        mission=active_mission,
                        work_item=active_work_item,
                        run_id=agent_run.id,
                        sequence=action_sequence,
                        failure_kind=failure_kind,
                        error=detail,
                        attempt=model_recovery_attempts,
                        strategy=strategy,
                    )
                    pending_model_recovery = {
                        "evidence_ref": evidence_ref,
                        "failure_kind": failure_kind,
                        "strategy": strategy,
                        "step_id": recovery_step.id,
                    }
                    db.commit()
                    messages.append({
                        "role": "user",
                        "content": (
                            f"The provider rejected the previous model output as {failure_kind}. "
                            f"Recovery mode is {strategy['mode']}. Keep the durable target unchanged. "
                            "Call exactly one allowed tool with valid JSON arguments; omit optional "
                            "fields instead of emitting null, and do not narrate or switch targets. "
                            f"Allowed tools: {', '.join(strategy['tool_names'])}. "
                            f"Current target: {active_work_item.target_key if active_work_item else 'none'}."
                        ),
                    })
                    continue
                if failure_kind:
                    terminal_model_failure_kind = failure_kind
                if not failure_kind and "tool_use_failed" in detail and schema_retries < 3:
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
            if pending_model_recovery is not None:
                finish_model_recovery_attempt(
                    db,
                    mission=active_mission,
                    work_item=active_work_item,
                    run_id=agent_run.id,
                    evidence_ref=str(pending_model_recovery["evidence_ref"]),
                    failure_kind=str(pending_model_recovery["failure_kind"]),
                    strategy=dict(pending_model_recovery["strategy"]),
                    successful=True,
                )
                recovery_step = db.get(AgentRunStep, pending_model_recovery.get("step_id"))
                if recovery_step is not None:
                    recovery_step.outcome = "recovered"
                model_recovery_history.append({
                    **dict(pending_model_recovery["strategy"]),
                    "outcome": "recovered",
                })
                pending_model_recovery = None
                db.commit()
            msg = resp.choices[0].message
            # A local malformed-argument restriction applies to one provider
            # round. A new failure below can install the next narrower retry.
            local_recovery_tool_names = None
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
                if allow_research:
                    _sync_quality_tasks(db, city_id=city_id, run_id=agent_run.id)
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
                                "같은 검색을 반복하지 말고 list_agent_tasks를 다시 읽어 운영 DB가 생성한 "
                                "정확한 장소 ID 단위 품질 백로그를 이어서 처리하세요. "
                                "신규 장소 후보는 geocode_place 직후 propose_place로 승인 대기에 저장하세요. "
                                "그 후보를 upsert_agent_task에 승인 제안으로 적는 것은 금지됩니다. "
                                "실패한 후속 조사만 지식 본문이 아니라 upsert_agent_task에 구체적으로 남기세요."
                            ),
                        }
                    )
                    continue
                if no_progress_actions >= 18:
                    stall_reason = (
                        f"연속 {no_progress_actions}회 성과 변화가 없고 모델이 "
                        "추가 행동 없이 종료를 요청함"
                    )
                    active_work_item = _halt_stalled_mission(
                        db,
                        mission=active_mission,
                        work_item=active_work_item,
                        run_id=agent_run.id,
                        sequence=action_sequence,
                        reason=stall_reason,
                    )
                    if active_work_item is not None:
                        agent_run.work_item_id = active_work_item.id
                    final_text = (
                        f"연속 {no_progress_actions}회 성과 변화가 없어 안전 종료했습니다. "
                        "현재 대상은 차단 사유와 체크포인트를 남기고 회전했습니다."
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
            continuity_updates: list[dict[str, Any]] = []
            mission_halted = False
            for tc in tool_calls:
                name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                argument_error = ""
                try:
                    args = json.loads(raw_args)
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must decode to an object")
                except (json.JSONDecodeError, ValueError) as exc:
                    raw_digest = hashlib.sha256(raw_args.encode("utf-8", errors="replace")).hexdigest()
                    argument_error = f"malformed_tool_arguments: {exc}"
                    args = {
                        "_malformed_arguments_sha256": raw_digest,
                        "_malformed_arguments_preview": raw_args[:300],
                    }
                used_tools.add(name)
                tool_counts[name] = tool_counts.get(name, 0) + 1
                signature = _tool_signature(name, args)
                repeated = name in EXPENSIVE_RESEARCH_TOOLS and signature in seen_expensive_calls
                normalized_query = (
                    _normalize_research_query(args.get("query")) if name == "web_search" else ""
                )
                recent_search_repeat = bool(
                    normalized_query and normalized_query in recent_search_queries
                )
                image_place_id = None
                if name == "search_place_images" and args.get("place_id") is not None:
                    try:
                        image_place_id = int(args["place_id"])
                    except (TypeError, ValueError):
                        image_place_id = None
                image_budget_exhausted = bool(
                    image_place_id is not None
                    and image_searches_by_place.get(image_place_id, 0) >= 3
                )
                followup_url = str(args.get("url") or "")
                is_new_evidence_followup = bool(
                    name == "fetch_page"
                    and followup_url
                    and f"url:{followup_url}" in evidence_keys
                )
                decision_required = bool(
                    research_actions_since_material >= 8
                    and name not in MUTATION_TOOLS
                    and not is_new_evidence_followup
                )
                target_mismatch = _active_target_mismatch(
                    name,
                    args,
                    active_work_item,
                    mission_kind=active_mission.kind if active_mission is not None else "",
                )
                agent_task_mismatch = _active_agent_task_mismatch(
                    name, args, active_mission
                )
                projected_task_args, task_projection_error = (
                    _project_data_integrity_task_result_args(
                        name,
                        args,
                        active_mission,
                        (
                            db.get(AgentTask, active_mission.task_id)
                            if active_mission is not None
                            and active_mission.kind == "data_integrity"
                            and active_mission.task_id is not None
                            and name == "upsert_agent_task"
                            else None
                        ),
                    )
                )
                data_integrity_get_place_id = None
                if (
                    active_mission is not None
                    and active_mission.kind == "data_integrity"
                    and name == "get_place"
                    and args.get("place_id") is not None
                ):
                    try:
                        data_integrity_get_place_id = int(args["place_id"])
                    except (TypeError, ValueError):
                        data_integrity_get_place_id = None
                repeated_data_integrity_place_read = bool(
                    data_integrity_get_place_id is not None
                    and data_integrity_get_place_id in data_integrity_place_reads
                )
                data_integrity_task_list_exhausted = bool(
                    active_mission is not None
                    and active_mission.kind == "data_integrity"
                    and name == "list_agent_tasks"
                    and data_integrity_list_task_calls >= DATA_INTEGRITY_LIST_TASKS_LIMIT
                )
                integrity_scope_violation = bool(
                    active_mission is not None
                    and active_mission.kind == "data_integrity"
                    and name not in DATA_INTEGRITY_TOOLS
                )
                malformed_attempt = 0
                if integrity_scope_violation:
                    # Providers normally call only advertised tools, but never
                    # rely on that for a read-only operational-data boundary.
                    result = {
                        "error": "tool_not_allowed_for_data_integrity",
                        "detail": (
                            f"data_integrity 과제에서는 읽기 도구와 과제 결과 기록만 허용됩니다. "
                            f"'{name}' 호출은 실행하지 않았습니다."
                        ),
                        "allowed_tools": sorted(DATA_INTEGRITY_TOOLS),
                    }
                elif agent_task_mismatch is not None:
                    result = agent_task_mismatch
                elif task_projection_error is not None:
                    result = task_projection_error
                elif repeated_data_integrity_place_read:
                    result = {
                        "error": "duplicate_data_integrity_place_read",
                        "detail": (
                            f"장소 #{data_integrity_get_place_id}의 get_place 결과는 이번 감사에서 이미 "
                            "성공적으로 읽었습니다. 같은 조회를 반복하지 말고 기존 관찰을 사용해 "
                            f"현재 과제 task_id={active_mission.task_id if active_mission else None}의 "
                            "result를 upsert_agent_task로 기록하세요."
                        ),
                        "place_id": data_integrity_get_place_id,
                    }
                elif data_integrity_task_list_exhausted:
                    result = {
                        "error": "data_integrity_task_list_budget_exhausted",
                        "detail": (
                            f"list_agent_tasks는 data_integrity 감사에서 최대 "
                            f"{DATA_INTEGRITY_LIST_TASKS_LIMIT}회만 허용됩니다. 이미 확인한 활성 task_id를 "
                            "사용해 감사 결과를 기록하세요."
                        ),
                        "limit": DATA_INTEGRITY_LIST_TASKS_LIMIT,
                        "active_task_id": active_mission.task_id if active_mission else None,
                    }
                elif argument_error:
                    malformed_attempt = malformed_tool_failures.get(name, 0) + 1
                    malformed_tool_failures[name] = malformed_attempt
                    plan = make_recovery_plan(
                        argument_error,
                        attempt=malformed_attempt,
                        phase="write" if name in MUTATION_TOOLS else "research",
                        available_tools={name},
                        last_tool=name,
                        next_tool=name,
                    )
                    local_recovery_tool_names = set(plan.allowed_tools)
                    result = {
                        "error": "malformed_tool_arguments",
                        "detail": (
                            "도구 인자가 JSON 객체가 아니어서 도구를 실행하지 않았습니다. "
                            "같은 대상을 유지하고 유효한 JSON으로 한 번만 다시 호출하세요."
                        ),
                        "attempt": malformed_attempt,
                        "recovery_mode": plan.mode,
                        "allowed_tools": sorted(plan.allowed_tools),
                        "signature": args["_malformed_arguments_sha256"],
                    }
                elif target_mismatch is not None:
                    result = target_mismatch
                elif decision_required:
                    result = {
                        "error": "material_decision_required",
                        "detail": (
                            "실제 DB 변화 없이 조사 도구를 8회 사용했습니다. 추가 조회·검색을 중단하고 "
                            "이미 확보한 근거만 사용해 장소 정보·인사이트·검증·사진·구역·체인 중 하나를 "
                            "안전하게 갱신하세요. 근거가 부족하면 해당 장소의 차단 원인과 다음 검증 방법을 "
                            "과제 result에 남기고 다른 정확한 대상의 저장 행동으로 전환하세요. 추측 저장은 금지합니다."
                        ),
                    }
                elif recent_search_repeat:
                    repeated_calls += 1
                    repeated = True
                    result = {
                        "error": "recent_duplicate_search",
                        "detail": (
                            "동일한 검색어가 최근 7일 안에 이미 실행되었습니다. list_research_history와 "
                            "과제의 이전 실행 인계를 사용하세요. 새 검증이 필요하면 검색어만 꾸미지 말고 "
                            "공식 기관·지도·예약 플랫폼처럼 출처 축을 바꾸거나 다른 정확한 대상으로 이동하세요."
                        ),
                    }
                elif image_budget_exhausted:
                    result = {
                        "error": "place_image_search_budget_exhausted",
                        "detail": (
                            f"장소 #{image_place_id}는 이번 실행에서 이미 이미지 검색 3회를 사용했습니다. "
                            "관련 없는 인근 사진을 붙이지 말고 실패 근거를 과제 result에 남긴 뒤 다음 장소나 "
                            "다른 품질 과제로 이동하세요."
                        ),
                    }
                elif repeated:
                    repeated_calls += 1
                    result = {
                        "error": "duplicate_tool_call",
                        "detail": "같은 실행에서 이미 수행한 조사입니다. 기존 관찰을 사용하거나 다른 검증 경로로 전환하세요.",
                    }
                else:
                    if name in EXPENSIVE_RESEARCH_TOOLS:
                        seen_expensive_calls.add(signature)
                    if image_place_id is not None:
                        image_searches_by_place[image_place_id] = (
                            image_searches_by_place.get(image_place_id, 0) + 1
                        )
                    tool_args = (
                        projected_task_args
                        if active_mission is not None
                        and active_mission.kind == "data_integrity"
                        and name == "upsert_agent_task"
                        else args
                    )
                    if name in {
                        "verify_place",
                        "upsert_place_insights",
                        "update_place_fields",
                        "propose_place",
                    }:
                        tool_args = {
                            **args,
                            "_validated_source_urls": sorted(validated_source_urls),
                        }
                    if name == "propose_place":
                        coordinate_evidence = _matching_coordinate_evidence(
                            args,
                            verified_coordinate_records,
                            city_name=city.name_local or city.slug,
                        )
                        if coordinate_evidence is None:
                            result = {
                                "error": "coordinate_target_not_verified",
                                "detail": (
                                    "이번 실행에서 같은 상호·지점으로 확인한 geocode/fetch_page 좌표가 없습니다. "
                                    "모델 좌표를 직접 저장하지 말고 정확한 지점 좌표를 먼저 확인하세요."
                                ),
                            }
                        else:
                            tool_args = {**tool_args, "_coordinate_evidence": coordinate_evidence}
                            result = run_tool(db, name, tool_args, city_id=city_id)
                    else:
                        result = run_tool(
                            db,
                            name,
                            tool_args,
                            city_id=city_id,
                            server_pure_read=bool(
                                active_mission is not None
                                and active_mission.kind == "data_integrity"
                                and name == "list_agent_tasks"
                            ),
                        )
                    if (
                        name == "fetch_page"
                        and isinstance(result, dict)
                        and not result.get("error")
                        and is_useful_fetched_page(result)
                        and result.get("url")
                    ):
                        validated_source_urls.add(str(result["url"]))
                    if name in {"geocode_place", "fetch_page"} and isinstance(result, dict):
                        coordinate_rows = (
                            result.get("results") or []
                            if name == "geocode_place"
                            else result.get("coordinate_candidates") or []
                        )
                        for raw_coordinate in coordinate_rows:
                            if not isinstance(raw_coordinate, dict) or raw_coordinate.get("storage_allowed") is False:
                                continue
                            try:
                                float(raw_coordinate["lat"])
                                float(raw_coordinate["lng"])
                            except (KeyError, TypeError, ValueError):
                                continue
                            record = {
                                **raw_coordinate,
                                "display_name": str(
                                    raw_coordinate.get("display_name")
                                    or result.get("title")
                                    or args.get("query")
                                    or ""
                                ),
                                "source_url": str(
                                    raw_coordinate.get("source_url")
                                    or result.get("url")
                                    or ""
                                ),
                            }
                            verified_coordinate_records.append(record)
                    if normalized_query:
                        recent_search_queries.add(normalized_query)
                error = isinstance(result, dict) and bool(result.get("error"))
                if (
                    not error
                    and active_mission is not None
                    and active_mission.kind == "data_integrity"
                ):
                    if name == "get_place" and data_integrity_get_place_id is not None:
                        data_integrity_place_reads.add(data_integrity_get_place_id)
                    elif name == "list_agent_tasks":
                        data_integrity_list_task_calls += 1
                if not error:
                    successful_tool_counts[name] = successful_tool_counts.get(name, 0) + 1
                material_change = _is_material_change(name, result)
                new_evidence = _new_evidence_keys(name, result, evidence_keys)
                evidence_keys.update(new_evidence)
                for handoff_item in _evidence_handoff_items(name, result, new_evidence):
                    if handoff_item not in evidence_handoff and len(evidence_handoff) < 12:
                        evidence_handoff.append(handoff_item)
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
                    research_actions_since_material = 0
                else:
                    no_material_actions += 1
                    if name in EXPENSIVE_RESEARCH_TOOLS and not is_new_evidence_followup:
                        research_actions_since_material += 1
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
                        detail=_step_detail_json(
                            args,
                            result,
                            {
                                "material_change": material_change,
                                "new_evidence": len(new_evidence),
                                "score": current_score,
                                "no_material_actions": no_material_actions,
                            },
                        ),
                    )
                )
                agent_run.score = current_score
                active_work_item, continuity = checkpoint_after_tool(
                    db,
                    mission=active_mission,
                    work_item=active_work_item,
                    run_id=agent_run.id,
                    sequence=action_sequence,
                    tool=name,
                    args=args,
                    result=result,
                    outcome=outcome,
                    new_evidence_count=len(new_evidence),
                    material_change=material_change,
                )
                if active_work_item is not None:
                    agent_run.work_item_id = active_work_item.id
                canonical_active = active_work_item_for_mission(db, active_mission)
                if canonical_active is not None:
                    active_work_item = canonical_active
                    agent_run.work_item_id = canonical_active.id
                if material_change:
                    active_work_item = reconcile_work_items(db, mission=active_mission)
                    if active_work_item is not None:
                        agent_run.work_item_id = active_work_item.id
                        continuity = mission_context(active_mission, active_work_item) if active_mission else continuity
                elif _should_rotate_exhausted_image_target(
                    mission=active_mission,
                    work_item=active_work_item,
                    target_mismatch=target_mismatch,
                    image_searches_by_place=image_searches_by_place,
                ):
                    previous_target = active_work_item.target_key
                    active_work_item = rotate_blocked_work_item(
                        db,
                        mission=active_mission,
                        current=active_work_item,
                        run_id=agent_run.id,
                        reason=(
                            "Three image searches produced no attachable exact-subject image; "
                            "the model then attempted a different target. Rotate instead of repeating drift."
                        ),
                    )
                    if active_work_item is not None:
                        agent_run.work_item_id = active_work_item.id
                        research_actions_since_material = 0
                        no_progress_actions = 0
                        continuity["rotation"] = {
                            "from": previous_target,
                            "to": active_work_item.target_key,
                            "reason": "image search exhausted and next-target intent observed",
                        }
                elif (
                    active_work_item is not None
                    and len(continuity.get("failed_approaches") or []) >= 3
                ):
                    previous_target = active_work_item.target_key
                    active_work_item = rotate_blocked_work_item(
                        db,
                        mission=active_mission,
                        current=active_work_item,
                        run_id=agent_run.id,
                        reason="서로 다른 조사 경로 3회가 실패하거나 차단됨",
                    )
                    if active_work_item is not None:
                        agent_run.work_item_id = active_work_item.id
                        research_actions_since_material = 0
                        no_progress_actions = 0
                        continuity["rotation"] = {
                            "from": previous_target,
                            "to": active_work_item.target_key,
                            "reason": "실패 경로 3회 누적",
                        }
                db.commit()
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False)[:12000],
                    }
                )
                if continuity:
                    continuity_updates.append(continuity)
                if argument_error and malformed_attempt >= 3:
                    mission_halted = True
                    final_text = (
                        f"{name} 도구 인자 생성이 세 번 연속 깨져 실행하지 않고 안전 종료했습니다. "
                        "실패 서명과 현재 대상은 체크포인트에 남겼으며 다음 실행은 다른 복구 전략으로 이어집니다."
                    )
                    break
                if _mission_has_no_executable_target(active_mission, active_work_item):
                    # One model response may contain parallel calls. Once the
                    # orchestrator pauses/completes the mission, later calls no
                    # longer have a durable target and must not be executed.
                    mission_halted = True
                    break
            if mission_halted:
                if not final_text:
                    final_text = (
                        "현재 미션의 실행 가능한 대상이 모두 완료되었거나 차단되어 종료했습니다. "
                        "저장된 체크포인트와 재시도 조건에서 다음 실행이 이어집니다."
                    )
                break
            if continuity_updates:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "작업 연속성 체크포인트가 저장되었습니다. 다음 호출은 이 상태를 이어가세요: "
                            + json.dumps(continuity_updates[-3:], ensure_ascii=False)[:3500]
                        ),
                    }
                )
            if no_material_actions in {12, 18} and material_nudges < 2:
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
            if no_material_actions >= 24:
                active_work_item = rotate_blocked_work_item(
                    db,
                    mission=active_mission,
                    current=active_work_item,
                    run_id=agent_run.id,
                    reason=f"연속 {no_material_actions}개 행동에서 실제 DB 변화 없음",
                )
                final_text = (
                    f"연속 {no_material_actions}개 행동이 실제 DB 변화로 이어지지 않아 안전 종료했습니다. "
                    "무성과 조사 8회부터 추가 조사를 막고 저장 여부 결정을 요구했으며, 확보한 근거와 "
                    "차단 사유는 다음 실행의 정확한 백로그로 넘깁니다."
                )
                break
            if no_progress_actions >= 18 and steps >= 20:
                stall_reason = (
                    f"연속 {no_progress_actions}개 행동에서 새 근거·데이터·정제가 생기지 않음"
                )
                active_work_item = _halt_stalled_mission(
                    db,
                    mission=active_mission,
                    work_item=active_work_item,
                    run_id=agent_run.id,
                    sequence=action_sequence,
                    reason=stall_reason,
                )
                if active_work_item is not None:
                    agent_run.work_item_id = active_work_item.id
                final_text = (
                    f"연속 {no_progress_actions}개 행동에서 새 근거·데이터·정제가 생기지 않아 종료했습니다. "
                    "현재 대상은 차단 사유와 체크포인트를 남기고 회전했습니다."
                )
                break
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        detail = str(exc)
        recoverable_model_failure = bool(
            terminal_model_failure_kind or _model_output_failure_kind(detail)
        )
        if recoverable_model_failure and model_recovery_attempts:
            detail = (
                f"{detail}\nAdaptive model-output recovery exhausted after "
                f"{model_recovery_attempts} changed-strategy attempts."
            )
        failure_status = (
            "partial" if recoverable_model_failure and bool(material_changes) else "failed"
        )
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
                failed_run.status = failure_status
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
                        "image_searches_by_place": image_searches_by_place,
                        "context_compactions": context_compactions,
                        "evidence_count": len(evidence_keys),
                        "model_recovery_attempts": model_recovery_attempts,
                        "model_recovery_history": model_recovery_history,
                        "terminal_model_failure_kind": (
                            terminal_model_failure_kind or _model_output_failure_kind(detail)
                        ),
                    },
                    ensure_ascii=False,
                )
                db.commit()
        except Exception:
            db.rollback()
        try:
            evaluate_knowledge_uses(
                db,
                run_id=agent_run.id,
                material_change_count=len(material_changes),
            )
        except Exception:
            db.rollback()
        return {
            "ok": False,
            "steps": steps,
            "message": detail[:1500],
            "unread_before": unread_before,
            "unread_after": unread_after,
            "city_id": city_id,
            "status": failure_status,
            "run_id": agent_run.id,
            "score": current_score,
            "model_recovery_attempts": model_recovery_attempts,
        }

    unread_after = count_unread(db, city_id)
    quality_task_ids_after = (
        _sync_quality_tasks(db, city_id=city_id, run_id=agent_run.id)
        if allow_research
        else []
    )
    performance_after = _performance_snapshot(db, city_id)
    performance_delta = _performance_delta(performance_before, performance_after)
    current_score = _performance_score(performance_delta, successful_tool_counts)
    gaps = _research_gaps(performance_delta, successful_tool_counts, performance_after) if allow_research else []
    gap_task_ids = _ensure_gap_tasks(db, city_id=city_id, run_id=agent_run.id, gaps=gaps) if gaps else []
    ok = unread_after == 0
    run_status = _run_outcome_status(
        unread_after=unread_after,
        gaps=gaps,
        material_change_count=len(material_changes),
    )
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
    if allow_research:
        summary = (
            f"{summary}\n[품질 변화] 사진 없음 "
            f"{performance_before.get('imageless_places', 0)}→{performance_after.get('imageless_places', 0)}, "
            f"초안 미완성 {performance_before.get('suggested_drafts', 0)}→"
            f"{performance_after.get('suggested_drafts', 0)}, 정보 부족 "
            f"{performance_before.get('thin_info_places', 0)}→{performance_after.get('thin_info_places', 0)}, "
            f"미검증 {performance_before.get('unverified_places', 0)}→"
            f"{performance_after.get('unverified_places', 0)}"
        )
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
                "evidence_handoff": evidence_handoff,
                "repeated_calls_blocked": repeated_calls,
                "image_searches_by_place": image_searches_by_place,
                "context_compactions": context_compactions,
                "model_recovery_attempts": model_recovery_attempts,
                "model_recovery_history": model_recovery_history,
                "remaining_gaps": gaps,
                "gap_task_ids": gap_task_ids,
                "quality_task_ids_before": quality_task_ids_before,
                "quality_task_ids_after": quality_task_ids_after,
                "primary_task_id": primary_task.id if primary_task is not None else None,
                "mission_id": active_mission.id if active_mission is not None else None,
                "work_item_id": active_work_item.id if active_work_item is not None else None,
                "continuity": (
                    mission_context(active_mission, active_work_item)
                    if active_mission is not None and active_work_item is not None
                    else None
                ),
                "retrieved_knowledge_ids": [
                    item.get("id") for item in retrieved_knowledge.get("knowledge", [])
                ],
                "applied_lesson_ids": [
                    item.get("id") for item in retrieved_knowledge.get("lessons", [])
                ],
                "no_progress_actions": no_progress_actions,
                "no_material_actions": no_material_actions,
                "research_actions_since_material": research_actions_since_material,
                "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            },
            ensure_ascii=False,
        )
        db.commit()
    if primary_task is not None and primary_task.status == "pending":
        primary_result = (
            f"실행 #{agent_run.id}: {run_status}, 점수 {current_score}, 실제 변경 {len(material_changes)}건, "
            f"새 근거 {len(evidence_keys)}건. 남은 공백: {', '.join(gaps) or '없음'}"
        )
        if evidence_handoff:
            primary_result += "\n다음 실행 근거 인계:\n- " + "\n- ".join(evidence_handoff)
        primary_task.result = primary_result[:8000]
        db.commit()
    finalize_mission(
        db,
        mission=active_mission,
        task=primary_task,
        run_id=agent_run.id,
    )
    evaluate_knowledge_uses(
        db,
        run_id=agent_run.id,
        material_change_count=len(material_changes),
    )
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
        "model_recovery_attempts": model_recovery_attempts,
        "run_id": agent_run.id,
        "city_id": city_id,
    }
