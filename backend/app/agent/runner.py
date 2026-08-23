"""Groq ReAct + tool-calling 에이전트 러너."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.agent.tools import TOOLS, _containing_zone, is_useful_fetched_page, run_tool
from app.agent.model_recovery import classify_failure, make_recovery_plan
from app.config import settings
from app.agent.memory import (
    CORRECTIVE_POLICY_GUARD_ERRORS,
    QUALITY_GAPS_BY_TASK_KIND,
    checkpoint_after_tool,
    active_work_item_for_mission,
    ensure_mission_for_task,
    evaluate_knowledge_uses,
    filter_actionable_quality_gaps,
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
    AgentCheckpoint,
    AgentEvidence,
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


def _data_integrity_system(
    city: City,
    mission: AgentMission,
    work_item: AgentWorkItem,
    *,
    corrective_result_only: bool = False,
) -> str:
    """A narrow prompt with no queue or general research obligations."""

    phase_contract = (
        "The server has already validated a successful get_place checkpoint for "
        "this exact active place and exposes it through server-owned evidence_refs. "
        "Do not call get_place or any research tool again. In this terminal phase, "
        "call only the advertised structured upsert_agent_task exactly once with "
        "exactly these six fields: task_id, status, verdict, reason, marker_changes, "
        "evidence_refs. The original task's requested output fields are superseded; "
        "put every relevant audit fact in reason and send no additional fields."
        if corrective_result_only
        else (
            "First read the active place with get_place. That exact target observation "
            "is mandatory evidence. Then use only the advertised read tools as needed."
        )
    )
    return (
        "You are a read-only operational data-integrity auditor. "
        f"Scope: city_id={city.id}, mission_id={mission.id}, task_id={mission.task_id}, "
        f"active place_id={work_item.place_id} ({work_item.title}).\n"
        "This specialized scope replaces every generic queue, personalization, "
        "quality-backlog, and broad city-research instruction. Do not process or "
        "mention unread events/appeals and do not switch places.\n"
        f"{phase_contract} "
        "list_places, list_agent_tasks, mutation tools, and foreign place IDs are out "
        "of scope. Finish only the current task through the advertised structured "
        "upsert_agent_task schema; never invent or copy a legacy result field."
    )


def _research_themes(city: City) -> str:
    if city.slug == "shenyang":
        return (
            "서탑·중가·노북시장·채탑야시장 같은 먹거리/야간, 오래 걷기 좋은 동네, 공원과 강변, "
            "현지 쇼핑·카페·휴식, 교통/예약 실용정보를 우선한다. 고궁·박물관은 이미 충분하면 추가하지 않는다"
        )
    return f"{city.name_local} 대표 명소·현지 음식·역사·교통·야간 동선"


def _tool_contract_line(tool_names: set[str] | frozenset[str]) -> str:
    """Render the provider-visible tool contract from the actual whitelist."""

    return ", ".join(sorted(tool_names))


def _candidate_discovery_system(
    city: City,
    mission: AgentMission,
    work_item: AgentWorkItem,
) -> str:
    """Narrow system contract for a proposal-only place discovery slice."""

    available = _tool_contract_line(CANDIDATE_DISCOVERY_TOOLS)
    return (
        "당신은 여행 지도 신규 장소 후보 발굴 에이전트입니다. "
        f"범위는 city_id={city.id}, {city.name_ko}({city.name_local}), "
        f"mission_id={mission.id}, task_id={mission.task_id}, work_item_id={work_item.id}입니다.\n"
        "이번 실행은 사용자 작업 큐나 기존 장소 품질 보강이 아니라, 현재 지도에 없는 여행 가치가 높은 "
        "장소를 근거 기반 승인 제안으로 만드는 독립된 시간 조각입니다. 다른 도시를 다루지 마세요.\n"
        f"사용 가능한 도구는 정확히 다음뿐입니다: {available}. "
        "이 목록 밖 기능을 요구하거나 호출하지 말고, 현재 요청에 실제로 광고된 스키마를 따르세요.\n"
        "먼저 내부 목록과 조사 이력으로 중복 및 부족한 여행 역할을 확인한 뒤, 새 검색 결과의 본문을 읽고 "
        "정확한 상호·지점·주소가 같은 후보만 좌표 검증하세요. 좌표와 장소 정체성이 같은 실행에서 검증된 "
        "후보만 propose_place로 관리자 승인 대기에 남길 수 있습니다. 즉시 장소를 생성하는 기능은 없으며 "
        "추측 좌표, 다른 지점, 검색 요약만으로는 제안하지 마세요.\n"
        "propose_place가 제안을 실제 생성하면 서버가 과제와 미션을 자동 완료합니다. completed를 직접 "
        "선언하지 마세요. 서로 다른 출처 축을 실제 조사했지만 안전한 제안이 불가능할 때만 현재 task_id를 "
        "upsert_agent_task(status=blocked)로 종료하세요. 조사 도구 실행 없이 차단할 수 없으며 후보 자체를 "
        "과제로 대신 적는 것은 성과가 아닙니다."
    )


def _scoped_quality_system(
    city: City,
    mission: AgentMission,
    work_item: AgentWorkItem,
    *,
    tool_names: set[str] | frozenset[str],
) -> str:
    """Give quality missions only obligations their advertised tools can meet."""

    available = _tool_contract_line(tool_names)
    place_scope = (
        f"활성 place_id={work_item.place_id} ({work_item.title})만 처리하세요. "
        if work_item.place_id is not None
        else f"활성 작업 {work_item.target_key} ({work_item.title})만 처리하세요. "
    )
    return (
        "당신은 여행 지도에서 하나의 지속 품질 미션을 이어서 처리하는 에이전트입니다. "
        f"범위는 city_id={city.id}, {city.name_ko}({city.name_local}), "
        f"mission_id={mission.id}, task_id={mission.task_id}, work_item_id={work_item.id}입니다.\n"
        f"{place_scope}사용자 작업 큐, 신규 장소 발굴, 다른 품질 미션으로 전환하지 마세요.\n"
        f"사용 가능한 도구는 정확히 다음뿐입니다: {available}. "
        "이 목록 밖 기능을 요구하거나 호출하지 말고, 현재 요청에 실제로 광고된 스키마를 따르세요.\n"
        "저장된 체크포인트와 기존 근거를 먼저 재사용하고, 같은 검색이나 실패한 출처를 반복하지 마세요. "
        "현재 성공조건을 만족시키는 가장 작은 안전한 행동을 수행한 뒤 상태를 다시 확인하세요. 완료할 수 "
        "없으면 확인한 차단 원인과 다음 재시도 조건을 현재 task_id에 기록하세요."
    )


def _scoped_mission_user_message(
    city: City,
    mission: AgentMission,
    work_item: AgentWorkItem,
    *,
    continuity_hint: str,
    personalization_hint: str = "",
) -> str:
    """Build a user message whose named tools match the scoped system contract."""

    if mission.kind == CANDIDATE_DISCOVERY_KIND:
        return (
            f"현재 실행 도시는 {city.name_ko}({city.name_local}), city_id={city.id}입니다.\n"
            f"신규 장소 발굴 목표: {mission.objective}\n"
            f"성공조건: {mission.success_metric}\n"
            f"이전 체크포인트: {continuity_hint}\n"
            "list_places와 list_research_history, list_agent_tasks로 기존 장소·과거 검색·현재 과제를 먼저 "
            "확인하세요. 부족한 여행 역할 하나를 고르고 web_search의 새 결과를 fetch_page로 검증한 뒤, "
            "기존 장소와 중복되지 않는 정확한 지점만 geocode_place로 좌표를 확인해 propose_place로 승인 "
            "제안을 남기세요. 제안 성공은 서버가 자동 완료합니다. 실패한 경우에만 현재 실행의 조사 근거가 "
            "생긴 뒤 현재 task_id를 upsert_agent_task(status=blocked)로 마무리하세요.\n"
            "사용자 행동 기반 참고 신호(확정 취향으로 과장하지 말 것):\n"
            f"{personalization_hint or '[]'}"
        )
    return (
        f"현재 실행 도시는 {city.name_ko}({city.name_local}), city_id={city.id}입니다.\n"
        f"현재 품질 미션: {mission.objective}\n"
        f"성공조건: {mission.success_metric}\n"
        f"활성 대상: {work_item.target_key}, place_id={work_item.place_id}, title={work_item.title}.\n"
        f"이전 체크포인트: {continuity_hint}\n"
        "활성 대상 하나의 현재 결손만 처리하고 실제 상태로 성공조건을 확인하세요. 완료 또는 차단 결과는 "
        "현재 task_id에만 기록하고, 다른 장소나 미션으로 전환하지 마세요."
    )


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
    "web_search",
    "fetch_page",
    "geocode_place",
    "upsert_agent_task",
})
DATA_INTEGRITY_TASK_RESULT_STATUSES = frozenset({"completed", "blocked"})

CANDIDATE_DISCOVERY_KIND = "candidate_discovery"
# Two quality/resume runs for every guaranteed discovery run. Discovery may run
# more often when there is no executable non-discovery work, but a large quality
# backlog can never push it out of the schedule indefinitely.
CANDIDATE_DISCOVERY_INTERVAL = 3
CANDIDATE_DISCOVERY_TOOLS = frozenset({
    "list_knowledge",
    "list_agent_tasks",
    "list_zones",
    "list_places",
    "list_research_history",
    "get_place",
    "web_search",
    "fetch_page",
    "geocode_place",
    "propose_place",
    "upsert_agent_task",
})

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
    CANDIDATE_DISCOVERY_KIND: CANDIDATE_DISCOVERY_TOOLS,
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


def _candidate_discovery_tools(
    *,
    task_id: int,
    tool_names: list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    """Advertise the narrow task terminal shape the runtime actually accepts."""

    tools = copy.deepcopy(_filtered_tools(tool_names or CANDIDATE_DISCOVERY_TOOLS))
    for tool in tools:
        if tool.get("function", {}).get("name") != "upsert_agent_task":
            continue
        tool["function"]["description"] = (
            "Block the current candidate-discovery slice only after this run has a "
            "successful web_search plus either a useful fetch_page or an explicitly "
            "storable non-Brave geocode_place result. Successful propose_place calls "
            "are completed automatically by the server."
        )
        tool["function"]["parameters"] = {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "enum": [task_id]},
                "status": {"type": "string", "enum": ["blocked"]},
                "result": {
                    "type": "string",
                    "description": (
                        "Explain the searched source axis, its independent verification, "
                        "and the retry condition. "
                        "The server stores only its own audited terminal summary."
                    ),
                },
            },
            "required": ["task_id", "status", "result"],
            "additionalProperties": False,
        }
        break
    return tools


def _candidate_discovery_research_refs(db: Session, *, run_id: int) -> list[str]:
    """Return a complete current-run evidence pair for a blocked slice.

    A search result alone is only a lead, while a fetch/geocode result without a
    search does not prove that the discovery lane explored a new source axis.
    Keep the terminal guard server-owned: one successful web search *and* one
    useful independently retainable verification must both exist in this run.
    """

    rows = (
        db.query(AgentRunStep)
        .filter(
            AgentRunStep.run_id == run_id,
            AgentRunStep.tool.in_(("web_search", "fetch_page", "geocode_place")),
        )
        .order_by(AgentRunStep.sequence.asc())
        .all()
    )
    search_refs: list[str] = []
    verification_refs: list[str] = []
    for row in rows:
        try:
            detail = json.loads(row.detail or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(detail, dict):
            continue
        result = detail.get("result")
        progress = detail.get("progress")
        progress = progress if isinstance(progress, dict) else {}
        if not isinstance(result, dict) or result.get("error"):
            continue
        ref = f"run:{run_id}:step:{row.sequence}"
        if row.tool == "web_search":
            # A provider returning a well-formed result set (including an empty
            # set) is a completed search observation. Guard/policy failures are
            # represented by ``error`` above and never qualify.
            if progress.get("discovery_search_ok") is True or isinstance(result.get("results"), list):
                search_refs.append(ref)
            continue
        if row.tool == "fetch_page":
            # Long pages are compacted in AgentRunStep. In that representation
            # ``outcome=ok`` is the server-owned proof that _new_evidence_keys
            # accepted a useful, not-previously-visited page before compaction.
            compacted_useful_page = bool(
                detail.get("truncated") is True
                and row.outcome == "ok"
                and (result.get("url") or result.get("title"))
            )
            if (
                progress.get("discovery_verification_ok") is True
                or is_useful_fetched_page(result)
                or compacted_useful_page
            ):
                verification_refs.append(ref)
            continue
        if row.tool == "geocode_place":
            if progress.get("discovery_verification_ok") is True:
                verification_refs.append(ref)
                continue
            candidates = result.get("results")
            if not isinstance(candidates, list):
                continue
            independently_storable = any(
                isinstance(candidate, dict)
                and candidate.get("storage_allowed") is True
                and not str(candidate.get("source") or "").casefold().startswith("brave")
                for candidate in candidates
            )
            if independently_storable:
                verification_refs.append(ref)
    if not search_refs or not verification_refs:
        return []
    return [*search_refs[:10], *verification_refs[:10]]


def _data_integrity_evidence_refs(
    db: Session,
    mission: AgentMission | None,
    work_item: AgentWorkItem | None,
) -> dict[str, str]:
    """Return bounded server-owned evidence refs and their strength."""

    if mission is None or work_item is None or mission.kind != "data_integrity":
        return {}
    refs: dict[str, str] = {}
    checkpoints = (
        db.query(AgentCheckpoint, AgentRunStep)
        .join(
            AgentRunStep,
            (AgentRunStep.run_id == AgentCheckpoint.run_id)
            & (AgentRunStep.sequence == AgentCheckpoint.sequence),
        )
        .filter(
            AgentCheckpoint.mission_id == mission.id,
            AgentCheckpoint.work_item_id == work_item.id,
            AgentCheckpoint.outcome.in_(("ok", "changed", "no_new_evidence")),
            AgentRunStep.outcome.in_(("ok", "changed", "no_new_evidence")),
            AgentRunStep.tool == "get_place",
        )
        .order_by(AgentCheckpoint.id.desc())
        .limit(20)
        .all()
    )
    for checkpoint, step in checkpoints:
        try:
            result = json.loads(step.detail or "{}").get("result")
        except (TypeError, json.JSONDecodeError):
            result = None
        try:
            detail = json.loads(step.detail or "{}")
        except (TypeError, json.JSONDecodeError):
            detail = {}
        step_args = detail.get("args") if isinstance(detail, dict) else None
        if (
            not refs
            and
            isinstance(result, dict)
            and isinstance(step_args, dict)
            and work_item.place_id is not None
            and result.get("id") == work_item.place_id
            and step_args.get("place_id") == work_item.place_id
        ):
            refs[f"checkpoint:{checkpoint.id}"] = "target_observed"
    evidence = (
        db.query(AgentEvidence)
        .filter(
            AgentEvidence.mission_id == mission.id,
            AgentEvidence.work_item_id == work_item.id,
            AgentEvidence.source_status.in_(("validated", "discovered", "blocked")),
        )
        .order_by(AgentEvidence.id.desc())
        .limit(20)
        .all()
    )
    evidence_refs: dict[str, str] = {}
    for row in evidence:
        evidence_text = " ".join(
            str(value or "")
            for value in (row.title, row.claim, row.excerpt)
        )[:3000]
        evidence_refs[f"evidence:{row.id}"] = (
            f"{row.source_status}|{evidence_text}"
        )
    # The exact target observation is the mandatory grounding anchor and must
    # never be crowded out by source rows. Remaining slots prefer fresh source
    # evidence after that single-purpose checkpoint set.
    return dict(list({**refs, **evidence_refs}.items())[:20])


def _has_data_integrity_target_read(refs: dict[str, str]) -> bool:
    return any(strength == "target_observed" for strength in refs.values())


def _data_integrity_corrective_tools(
    *,
    task_id: int,
    evidence_refs: list[str],
    evidence_details: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return the narrow structured schema for closing the active audit."""

    source = next(
        tool for tool in TOOLS
        if tool["function"]["name"] == "upsert_agent_task"
    )
    tool = copy.deepcopy(source)
    tool["function"]["description"] = (
        "Complete this data_integrity task by citing server-owned evidence. "
        "Marker data is not changed."
    )
    tool["function"]["parameters"] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "enum": [task_id]},
            "status": {"type": "string", "enum": ["completed"]},
            "verdict": {
                "type": "string",
                # Until the server records an explicit identity-match or
                # identity-conflict evidence kind, a cited source can support
                # an audit note but cannot safely prove either conclusion.
                "enum": ["unresolved"],
            },
            "reason": {
                "type": "string",
                "description": (
                    "Explain the verdict and repeat exact entity, branch, or "
                    "address terms from the cited evidence."
                ),
            },
            "marker_changes": {"type": "integer", "enum": [0]},
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string", "enum": evidence_refs},
                "minItems": 1,
                "description": (
                    "Cite one or more server-owned refs. Available evidence: "
                    + "; ".join(
                        f"{ref}={str((evidence_details or {}).get(ref) or '')[:180]}"
                        for ref in evidence_refs
                    )[:3500]
                ),
            },
        },
        "required": [
            "task_id", "status", "verdict", "reason",
            "marker_changes", "evidence_refs",
        ],
        "additionalProperties": False,
    }
    return [tool]


def _project_structured_integrity_result(
    args: dict[str, Any],
    *,
    allowed_refs: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Validate cited server evidence and build the stored result string."""

    verdict = str(args.get("verdict") or "").strip().lower()
    reason = " ".join(str(args.get("reason") or "").split())
    raw_refs = args.get("evidence_refs")
    refs = (
        list(dict.fromkeys(str(ref) for ref in raw_refs))
        if isinstance(raw_refs, list)
        else []
    )
    strengths = [allowed_refs.get(ref) for ref in refs]
    valid = bool(
        verdict == "unresolved"
        and len(reason) >= 10
        and args.get("marker_changes") == 0
        and refs
        and all(strength is not None for strength in strengths)
        and any(strength == "target_observed" for strength in strengths)
    )
    if not valid:
        return {}, {
            "error": "invalid_data_integrity_task_result",
            "error_class": "policy_guard",
            "guard_disposition": "retry",
            "detail": (
                "Use verdict=unresolved and only server-advertised evidence_refs. "
                "At least one cited ref must be the active target get_place observation. "
                "Confirmed/conflict remains unavailable until the server owns "
                "an explicit identity verdict evidence kind."
            ),
            "allowed_evidence_refs": sorted(allowed_refs),
        }
    return {
        "task_id": args.get("task_id"),
        "status": "completed",
        "result": (
            f"verdict={verdict}; marker_changes=0; "
            f"evidence_refs={json.dumps(refs, ensure_ascii=False)}; reason={reason}"
        )[:8000],
    }, None


def _resume_data_integrity_corrective_result(
    mission: AgentMission | None,
    work_item: AgentWorkItem | None,
) -> bool:
    """Restore only a server-checkpointed integrity terminal-decision phase."""

    if (
        mission is None
        or work_item is None
        or mission.kind != "data_integrity"
        or mission.task_id is None
        or work_item.status != "active"
        or work_item.stage != "decide"
    ):
        return False
    try:
        next_action = json.loads(work_item.next_action or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(next_action, dict) or next_action.get("tool") != "upsert_agent_task":
        return False
    if next_action.get("phase") == "data_integrity_terminal_verdict_v1":
        task_id = next_action.get("task_id")
        return bool(
            isinstance(task_id, int)
            and not isinstance(task_id, bool)
            and task_id == mission.task_id
            and next_action.get("guard_disposition") == "decide"
        )
    # Read compatibility for production cursors written before the structured
    # phase marker. New writes below never publish a model-copyable result.
    args = next_action.get("args")
    if not isinstance(args, dict):
        return False
    task_id = args.get("task_id")
    if isinstance(task_id, bool) or not isinstance(task_id, int):
        return False
    result = str(args.get("result") or "").casefold().replace(" ", "")
    return bool(
        task_id == mission.task_id
        and str(args.get("status") or "").strip().lower() == "completed"
        and "policy_guard=" in result
        and "guard_disposition=decide" in result
        and "marker_changes=0" in result
    )


def _persist_data_integrity_corrective_cursor(
    mission: AgentMission | None,
    work_item: AgentWorkItem | None,
    *,
    guard_error: str,
) -> None:
    """Keep the server-owned terminal phase durable across retry boundaries."""

    if (
        mission is None
        or work_item is None
        or mission.kind != "data_integrity"
        or mission.task_id is None
    ):
        return
    action = {
        "phase": "data_integrity_terminal_verdict_v1",
        "tool": "upsert_agent_task",
        "task_id": mission.task_id,
        "status": "completed",
        "guard_error": guard_error,
        "guard_disposition": "decide",
        "required_fields": [
            "task_id", "status", "verdict", "reason",
            "marker_changes", "evidence_refs",
        ],
        "purpose": (
            "Use the currently advertised tool schema to record the terminal "
            "verdict; do not copy fields from this checkpoint."
        ),
    }
    work_item.stage = "decide"
    work_item.status = "active"
    work_item.next_action = json.dumps(action, ensure_ascii=False)
    try:
        progress = json.loads(mission.progress or "{}")
    except (TypeError, json.JSONDecodeError):
        progress = {}
    if not isinstance(progress, dict):
        progress = {}
    progress["active_work_item_id"] = work_item.id
    progress["next_action"] = action
    mission.progress = json.dumps(progress, ensure_ascii=False)


def _complete_data_integrity_mission(
    db: Session,
    *,
    mission: AgentMission | None,
    task: AgentTask | None,
    run_id: int | None = None,
) -> bool:
    """Idempotently align a terminal integrity task and its durable cursor.

    This helper intentionally does not commit. During a normal result write the
    task mutation, checkpoint, and cursor transition share the runner's commit;
    at startup it also repairs legacy split-commit state before any model call.
    """

    if (
        mission is None
        or task is None
        or mission.kind != "data_integrity"
        or task.kind != "data_integrity"
        or task.status != "completed"
        or mission.task_id != task.id
    ):
        return False
    now = datetime.now(timezone.utc)
    mission.status = "completed"
    mission.completed_at = mission.completed_at or now
    if run_id is not None:
        mission.last_run_id = run_id
    try:
        progress = json.loads(mission.progress or "{}")
    except (TypeError, json.JSONDecodeError):
        progress = {}
    if not isinstance(progress, dict):
        progress = {}
    progress.update({
        "active_work_item_id": None,
        "next_action": {},
        "terminal_task_id": task.id,
        "terminal_status": "completed",
    })
    mission.progress = json.dumps(progress, ensure_ascii=False)
    for item in db.query(AgentWorkItem).filter(
        AgentWorkItem.mission_id == mission.id,
        AgentWorkItem.status.in_(("ready", "active", "blocked")),
    ).all():
        item.status = "done"
        item.stage = "complete"
        item.next_action = "{}"
        item.completed_at = item.completed_at or now
    return True


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
        # Scoped mission prompts are generated from their complete advertised
        # contract. Keeping that same bounded contract is safer than silently
        # removing a required next-stage tool during provider parse recovery.
        if task_kind not in RECOVERY_TOOLS_BY_TASK or task_kind == "data_integrity":
            minimal = {next_tool, "get_place", "upsert_agent_task"}
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


def _auditable_run_step_refs(
    db: Session,
    *,
    run_id: int,
    place_id: int,
    tools: set[str],
    successful_only: bool = False,
) -> list[str]:
    """Return stable refs for exact-target tool observations in this run."""

    refs: list[str] = []
    rows = (
        db.query(AgentRunStep)
        .filter(
            AgentRunStep.run_id == run_id,
            AgentRunStep.tool.in_(tools),
        )
        .order_by(AgentRunStep.sequence.asc())
        .all()
    )
    for row in rows:
        try:
            detail = json.loads(row.detail or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        args = detail.get("args") if isinstance(detail, dict) else None
        result = detail.get("result") if isinstance(detail, dict) else None
        try:
            exact_target = isinstance(args, dict) and int(args.get("place_id")) == place_id
        except (TypeError, ValueError):
            exact_target = False
        if not exact_target:
            continue
        if successful_only and isinstance(result, dict) and result.get("error"):
            continue
        refs.append(f"run:{run_id}:step:{row.sequence}")
    return refs


def _image_search_audit(
    db: Session,
    *,
    run_id: int,
    place_id: int,
) -> dict[str, list[str]]:
    """Classify exact-target image searches without trusting model narration."""

    audit: dict[str, list[str]] = {
        "all": [],
        "clean_empty": [],
        "provider_failure": [],
        "with_candidates": [],
    }
    rows = (
        db.query(AgentRunStep)
        .filter(
            AgentRunStep.run_id == run_id,
            AgentRunStep.tool == "search_place_images",
        )
        .order_by(AgentRunStep.sequence.asc())
        .all()
    )
    for row in rows:
        try:
            detail = json.loads(row.detail or "{}")
            args = detail.get("args") if isinstance(detail, dict) else None
            result = detail.get("result") if isinstance(detail, dict) else None
            exact = isinstance(args, dict) and int(args.get("place_id")) == place_id
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not exact:
            continue
        # The runtime budget permits only three executed exact-target searches.
        # Ignore a later policy-guard step from legacy/replayed runs so it cannot
        # turn three clean observations into an apparent provider failure.
        if len(audit["all"]) >= 3:
            continue
        ref = f"run:{run_id}:step:{row.sequence}"
        audit["all"].append(ref)
        result_rows = result.get("results") if isinstance(result, dict) else None
        if isinstance(result_rows, list) and result_rows:
            audit["with_candidates"].append(ref)
        elif (
            isinstance(result, dict)
            and isinstance(result_rows, list)
            and not result_rows
            and not result.get("error")
            and not (result.get("warnings") or [])
        ):
            audit["clean_empty"].append(ref)
        else:
            # Warning-only partial outages and malformed payloads are not clean
            # evidence of source exhaustion.
            audit["provider_failure"].append(ref)
    return audit


def _zone_catalog_geometry_valid(rows: Any) -> bool:
    """Confirm that every advertised zone has a usable polygon geometry.

    An empty catalogue is a valid observed state. A non-empty catalogue with a
    truncated or malformed polygon is not evidence that a place lies outside
    every zone, so it must never create a durable waiver.
    """

    if not isinstance(rows, list):
        return False
    seen_ids: set[int] = set()
    for row in rows:
        raw_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0 or raw_id in seen_ids:
            return False
        seen_ids.add(raw_id)
        polygon = row.get("polygon") if isinstance(row, dict) else None
        if not isinstance(polygon, list) or len(polygon) < 3:
            return False
        vertices: list[tuple[float, float]] = []
        try:
            for point in polygon:
                if not isinstance(point, dict):
                    return False
                lat = float(point["lat"])
                lng = float(point["lng"])
                if not math.isfinite(lat) or not math.isfinite(lng):
                    return False
                vertices.append((lat, lng))
        except (KeyError, TypeError, ValueError):
            return False
        # Three repeated/collinear vertices do not form a usable polygon.
        twice_area = sum(
            x1 * y2 - x2 * y1
            for (y1, x1), (y2, x2) in zip(vertices, vertices[1:] + vertices[:1])
        )
        if abs(twice_area) <= 1e-12:
            return False
    return True


def _zone_catalog_audit(
    db: Session,
    *,
    city_id: int,
    lat: float,
    lng: float,
    observed_rows: Any | None = None,
) -> dict[str, Any]:
    """Audit the full DB catalogue before closing one zone gap."""

    zones = (
        db.query(Marker)
        .filter(
            Marker.city_id == city_id,
            Marker.shape == MarkerShape.polygon,
            Marker.merged_into_id.is_(None),
        )
        .order_by(Marker.id.asc())
        .all()
    )
    server_rows: list[dict[str, Any]] = []
    for zone in zones:
        try:
            polygon = json.loads(zone.polygon or "[]")
        except (TypeError, json.JSONDecodeError):
            polygon = None
        server_rows.append({"id": zone.id, "polygon": polygon})
    if not _zone_catalog_geometry_valid(server_rows):
        return {"valid": False, "reason": "server_zone_geometry_invalid"}
    if observed_rows is not None:
        if not _zone_catalog_geometry_valid(observed_rows):
            return {"valid": False, "reason": "observed_zone_catalog_invalid"}
        observed_ids = {int(row["id"]) for row in observed_rows}
        server_ids = {int(row["id"]) for row in server_rows}
        if observed_ids != server_ids:
            return {"valid": False, "reason": "observed_zone_catalog_incomplete"}
    containing = _containing_zone(db, city_id=city_id, lat=lat, lng=lng)
    return {
        "valid": True,
        "contains": containing is not None,
        "containing_zone_id": containing.id if containing is not None else None,
    }
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

    if (
        mission is None
        or mission.kind not in {"data_integrity", CANDIDATE_DISCOVERY_KIND}
        or name != "upsert_agent_task"
    ):
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
        "error_class": "policy_guard",
        "guard_disposition": "retry",
        "detail": (
            "전용 미션은 현재 활성 과제의 결과만 기록할 수 있습니다. "
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
            "error_class": "policy_guard",
            "guard_disposition": "retry",
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


def _project_candidate_discovery_task_result_args(
    name: str,
    args: dict[str, Any],
    mission: AgentMission | None,
    task: AgentTask | None,
    *,
    run_id: int,
    transient_provider_seen: bool,
    research_evidence_refs: list[str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Limit discovery bookkeeping to its existing server-owned task."""

    if mission is None or mission.kind != CANDIDATE_DISCOVERY_KIND or name != "upsert_agent_task":
        return args, None
    if (
        task is None
        or task.id != mission.task_id
        or task.city_id != mission.city_id
        or task.kind != CANDIDATE_DISCOVERY_KIND
    ):
        return {}, {
            "error": "active_agent_task_not_writable",
            "detail": "서버가 현재 신규 장소 발굴 과제를 확인하지 못해 결과를 기록하지 않았습니다.",
            "active_task_id": mission.task_id,
        }
    status = str(args.get("status") or "").strip().lower()
    if status == "completed":
        return {}, {
            "error": "candidate_discovery_completion_server_controlled",
            "error_class": "policy_guard",
            "guard_disposition": "retry",
            "detail": (
                "신규 장소 발굴 완료는 propose_place가 승인 제안을 실제 생성한 뒤 서버가 자동 기록합니다. "
                "모델의 완료 선언만으로 과제를 종료하지 않습니다."
            ),
        }
    if status != "blocked":
        return {}, {
            "error": "invalid_candidate_discovery_task_status",
            "error_class": "policy_guard",
            "guard_disposition": "retry",
            "detail": "발굴 과제는 감사 가능한 조사 후 blocked로만 직접 종료할 수 있습니다.",
        }
    if not research_evidence_refs:
        return {}, {
            "error": "candidate_discovery_research_evidence_required",
            "error_class": "policy_guard",
            "guard_disposition": "retry",
            "detail": (
                "현재 실행에서 성공한 web_search와 독립 검증 근거의 조합을 확인하지 못했습니다. "
                "유효한 fetch_page 본문 또는 storage_allowed=true인 비-Brave geocode_place 결과까지 "
                "확보한 뒤 blocked를 다시 기록하세요."
            ),
        }
    retention_note = (
        " Brave Place transient 원문·파생 자유서술은 보존하지 않았습니다."
        if transient_provider_seen
        else ""
    )
    result_text = (
        f"실행 #{run_id}: 현재 실행의 조사 근거 {', '.join(research_evidence_refs)}를 확인하고 "
        f"신규 장소 발굴 조각을 blocked로 종료했습니다.{retention_note}"
    )[:8000]
    return {
        "task_id": task.id,
        "status": "blocked",
        "result": result_text,
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
    # A duplicate proposal returns the existing proposal_id for traceability,
    # but proposal_created=False is an explicit no-op and must win over that ID.
    if name == "propose_place" and result.get("proposal_created") is False:
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
            if (
                not isinstance(item, dict)
                or item.get("storage_allowed") is not True
                or item.get("source") == "brave_place"
            ):
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
            if (
                isinstance(item, dict)
                and item.get("storage_allowed") is True
                and item.get("source") != "brave_place"
            ):
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


def _compact_evidence_text(value: Any) -> str:
    return re.sub(
        r"[^0-9a-z\u3400-\u9fff\uac00-\ud7a3]+",
        "",
        str(value or "").casefold(),
    )


def _page_supports_coordinate_evidence(
    page: dict[str, Any],
    coordinate_evidence: dict[str, Any],
) -> bool:
    """Bind a fetched public page to the independently geocoded POI."""

    try:
        evidence_lat = float(coordinate_evidence["lat"])
        evidence_lng = float(coordinate_evidence["lng"])
    except (KeyError, TypeError, ValueError):
        return False
    for row in page.get("coordinate_candidates") or []:
        if (
            not isinstance(row, dict)
            or row.get("storage_allowed") is not True
            or str(row.get("source") or "").casefold().startswith("brave")
        ):
            continue
        try:
            if (
                abs(float(row["lat"]) - evidence_lat) <= 0.002
                and abs(float(row["lng"]) - evidence_lng) <= 0.002
            ):
                return True
        except (KeyError, TypeError, ValueError):
            continue

    identity = str(
        coordinate_evidence.get("title")
        or coordinate_evidence.get("display_name")
        or ""
    ).split(",", 1)[0]
    identity_key = _compact_evidence_text(identity)
    page_key = _compact_evidence_text(
        f"{page.get('title') or ''} {str(page.get('text') or '')[:2500]}"
    )
    if len(identity_key) >= 3 and identity_key in page_key:
        return True
    address_key = _compact_evidence_text(coordinate_evidence.get("address"))
    return len(address_key) >= 5 and address_key in page_key


def _canonical_transient_proposal_args(
    coordinate_evidence: dict[str, Any],
    verified_pages: list[dict[str, Any]],
    transient_taint: "_TransientProviderTaint",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Build a proposal using only independently retainable observations.

    Once a no-retention candidate was visible to the model, free-form model
    fields cannot be proven independent merely because they are paraphrased.
    The server therefore rebuilds the entire durable proposal from a storable
    non-Brave coordinate row and a separately fetched, matching public page.
    """

    coordinate_source = str(coordinate_evidence.get("source") or "").strip()
    if (
        coordinate_evidence.get("storage_allowed") is not True
        or not coordinate_source
        or coordinate_source.casefold().startswith("brave")
    ):
        return None, {
            "error": "independent_coordinate_evidence_required",
            "detail": "저장 가능한 비-Brave 좌표 근거가 없어 제안을 기록하지 않았습니다.",
        }
    display_name = str(
        coordinate_evidence.get("title")
        or coordinate_evidence.get("display_name")
        or ""
    ).strip()
    local_name, separator, display_address = display_name.partition(",")
    local_name = local_name.strip()
    address = str(coordinate_evidence.get("address") or "").strip()
    if not address and separator:
        address = display_address.strip()
    if not local_name or not address:
        return None, {
            "error": "independent_identity_address_required",
            "detail": "독립 좌표 근거에 정확한 장소명과 주소가 모두 없어 제안을 기록하지 않았습니다.",
        }

    matched_pages = [
        page
        for page in verified_pages
        if (
            isinstance(page, dict)
            and is_useful_fetched_page(page)
            and str(page.get("url") or "").startswith(("http://", "https://"))
            and not transient_taint.was_observed_transient(page.get("url"))
            and _page_supports_coordinate_evidence(page, coordinate_evidence)
        )
    ]
    if not matched_pages:
        return None, {
            "error": "independent_page_evidence_required",
            "detail": "Brave 후보와 별개로 발견해 본문을 확인한 동일 장소 출처가 없어 제안을 기록하지 않았습니다.",
        }

    source_urls = list(dict.fromkeys(
        str(page.get("url") or "").strip() for page in matched_pages
    ))[:4]
    primary_page = matched_pages[0]
    evidence_text = " ".join((
        display_name,
        str(coordinate_evidence.get("type") or ""),
        str(primary_page.get("title") or ""),
        str(primary_page.get("text") or "")[:1200],
    )).casefold()
    category = "other"
    category_label = "여행 장소"
    travel_role = "general"
    category_rules = (
        (r"酒店|宾馆|旅馆|hotel|lodging", "lodging", "숙소", "rest"),
        (r"餐厅|饭店|餐馆|restaurant|food", "restaurant", "음식점", "local_food"),
        (r"咖啡|茶饮|奶茶|饮品|甜品|cafe|coffee|tea", "drink", "음료점", "snack"),
        (r"地铁|车站|机场|transport|station", "transport", "교통 장소", "practical"),
        (r"商场|市场|购物|mall|shopping", "shopping", "쇼핑 장소", "shopping"),
        (r"博物馆|故宫|公园|景区|museum|park|attraction", "tourist", "관광지", "culture"),
        (r"便利店|药店|超市|convenience|pharmacy", "convenience", "편의 장소", "practical"),
    )
    for pattern, candidate_category, label, role in category_rules:
        if re.search(pattern, evidence_text, re.IGNORECASE):
            category, category_label, travel_role = candidate_category, label, role
            break
    title = local_name[:150]
    if not re.search(r"[\uac00-\ud7a3]", title):
        title = f"{title} ({category_label})"[:200]
    source_url = source_urls[0]
    source_title = str(primary_page.get("title") or local_name).strip()[:300]
    description = f"독립 출처로 확인한 {category_label}입니다. 주소: {address}"[:2000]
    return {
        "title": title,
        "description": description,
        "address": address[:300],
        "category": category,
        "travel_role": travel_role,
        "lat": float(coordinate_evidence["lat"]),
        "lng": float(coordinate_evidence["lng"]),
        "context": "저장 가능한 독립 지오코딩과 공개 본문으로 재검증한 관리자 승인 대기 제안입니다.",
        "coordinate_source": coordinate_source[:50],
        "coordinate_external_id": str(coordinate_evidence.get("external_id") or "")[:200],
        "coordinate_query": display_name[:300],
        "coordinate_source_url": str(coordinate_evidence.get("source_url") or "")[:1000],
        "coordinate_confidence": max(
            0.0, min(float(coordinate_evidence.get("confidence") or 0.7), 1.0)
        ),
        "zone_id": None,
        "chain_name_local": "",
        "chain_name_ko": "",
        "branch_name": str(coordinate_evidence.get("branch_name") or "")[:120],
        "evidence": (
            "저장 가능한 비-Brave 좌표 근거로 장소명·주소·위치를 확인하고, "
            "별도 공개 페이지의 본문으로 동일 장소임을 교차 검증했습니다."
        ),
        "source_urls": source_urls,
        "confidence": max(
            0.0, min(float(coordinate_evidence.get("confidence") or 0.7), 0.95)
        ),
        "insights": [
            {
                "kind": "location",
                "title": "위치 확인",
                "content": f"독립 좌표 근거에서 주소를 {address}로 확인했습니다.",
                "source_url": source_url,
                "source_title": source_title,
                "confidence": max(
                    0.0, min(float(coordinate_evidence.get("confidence") or 0.7), 0.95)
                ),
            },
            {
                "kind": "visit",
                "title": "방문 전 확인",
                "content": "방문 전 운영시간과 입장·예약 조건을 연결된 출처에서 다시 확인하세요.",
                "source_url": source_url,
                "source_title": source_title,
                "confidence": 0.7,
            },
        ],
        "_validated_source_urls": source_urls,
    }, None


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


def _persistable_material_changes(
    changes: list[dict[str, Any]],
    transient_taint: "_TransientProviderTaint",
) -> list[dict[str, Any]]:
    if not transient_taint.candidate_count:
        return transient_taint.redact(changes)
    allowed = {"sequence", "tool", "place_id", "proposal_id", "task_id", "changed", "status"}
    return [
        {key: value for key, value in item.items() if key in allowed}
        for item in changes
    ]


_TRANSIENT_REDACTION = "[BRAVE_TRANSIENT_REDACTED]"
_TRANSIENT_QUERY_REDACTION = "[BRAVE_TRANSIENT_QUERY_DISCARDED]"


class _TransientProviderTaint:
    """Keep no-storage provider values inside one live model loop only.

    Brave Place can be useful as a lead even when the plan grants no retention
    rights.  Values are therefore tainted when exposed to the model and may be
    promoted only when a storage-capable geocoder or a fetched public page
    independently confirms the same value.  The registry itself is deliberately
    run-local and is never serialized.
    """

    _CANDIDATE_STRING_FIELDS = {
        "display_name", "address", "source_url", "transient_id", "external_id",
    }
    _COORDINATE_FIELDS = {"lat", "lng", "latitude", "longitude"}

    def __init__(self) -> None:
        self._strings: set[str] = set()
        self._numbers: set[float] = set()
        self._promoted_strings: set[str] = set()
        self._promoted_numbers: set[float] = set()
        self.candidate_count = 0

    @staticmethod
    def _normalized(value: Any) -> str:
        return " ".join(str(value or "").casefold().split())

    def observe_brave_candidates(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        for candidate in result.get("place_candidates") or []:
            if not isinstance(candidate, dict) or candidate.get("storage_allowed") is True:
                continue
            self.candidate_count += 1
            for field in self._CANDIDATE_STRING_FIELDS:
                value = str(candidate.get(field) or "").strip()
                if len(self._normalized(value)) >= 2:
                    self._strings.add(value)
            for value in candidate.get("categories") or []:
                text = str(value or "").strip()
                if len(self._normalized(text)) >= 3:
                    self._strings.add(text)
            for field in ("lat", "lng"):
                try:
                    self._numbers.add(float(candidate[field]))
                except (KeyError, TypeError, ValueError):
                    continue

    @staticmethod
    def _storage_capable_rows(name: str, result: Any) -> list[dict[str, Any]]:
        if not isinstance(result, dict) or result.get("error"):
            return []
        if name == "geocode_place":
            return [
                row for row in (result.get("results") or [])
                if (
                    isinstance(row, dict)
                    and row.get("storage_allowed") is True
                    and row.get("source") != "brave_place"
                )
            ]
        if name == "fetch_page" and is_useful_fetched_page(result):
            rows: list[dict[str, Any]] = [{
                "url": result.get("url"),
                "title": result.get("title"),
                "text": result.get("text"),
            }]
            rows.extend(
                row for row in (result.get("coordinate_candidates") or [])
                if (
                    isinstance(row, dict)
                    and row.get("storage_allowed") is True
                    and row.get("source") != "brave_place"
                )
            )
            return rows
        return []

    @staticmethod
    def _flatten_strings(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [
                item
                for child in value.values()
                for item in _TransientProviderTaint._flatten_strings(child)
            ]
        if isinstance(value, (list, tuple)):
            return [
                item
                for child in value
                for item in _TransientProviderTaint._flatten_strings(child)
            ]
        return [str(value)] if isinstance(value, str) and value else []

    @staticmethod
    def _coordinate_values(value: Any) -> list[float]:
        output: list[float] = []
        if isinstance(value, dict):
            for key, child in value.items():
                if key in _TransientProviderTaint._COORDINATE_FIELDS:
                    try:
                        output.append(float(child))
                    except (TypeError, ValueError):
                        pass
                else:
                    output.extend(_TransientProviderTaint._coordinate_values(child))
        elif isinstance(value, (list, tuple)):
            for child in value:
                output.extend(_TransientProviderTaint._coordinate_values(child))
        return output

    def promote_independent_evidence(self, name: str, result: Any) -> None:
        rows = self._storage_capable_rows(name, result)
        if not rows:
            return
        canonical_text = self._normalized(" ".join(self._flatten_strings(rows)))
        for value in self._strings:
            normalized = self._normalized(value)
            if normalized and normalized in canonical_text:
                self._promoted_strings.add(value)
        canonical_numbers = self._coordinate_values(rows)
        for value in self._numbers:
            if any(abs(value - observed) <= 0.00001 for observed in canonical_numbers):
                self._promoted_numbers.add(value)

    def _unverified_strings(self) -> list[str]:
        return sorted(
            self._strings - self._promoted_strings,
            key=lambda value: len(value),
            reverse=True,
        )

    def has_unverified(self) -> bool:
        return bool(
            self._strings - self._promoted_strings
            or self._numbers - self._promoted_numbers
        )

    def was_observed_transient(self, value: Any) -> bool:
        """True for exact provider values even after independent promotion."""

        normalized = self._normalized(value)
        return bool(normalized) and any(
            normalized == self._normalized(candidate) for candidate in self._strings
        )

    def independently_validated_call(self, name: str, result: Any) -> bool:
        return bool(self._storage_capable_rows(name, result))

    def redact_text(self, value: Any) -> str:
        text = str(value or "")
        for candidate in self._unverified_strings():
            text = re.sub(re.escape(candidate), _TRANSIENT_REDACTION, text, flags=re.IGNORECASE)
        return text

    def contains_unverified(self, value: Any, *, key: str = "") -> bool:
        if isinstance(value, dict):
            return any(
                self.contains_unverified(child, key=str(child_key))
                for child_key, child in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(self.contains_unverified(child, key=key) for child in value)
        if isinstance(value, str):
            lowered = value.casefold()
            return any(candidate.casefold() in lowered for candidate in self._unverified_strings())
        if key in self._COORDINATE_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            return any(
                abs(numeric - candidate) <= 0.00001
                for candidate in self._numbers - self._promoted_numbers
            )
        return False

    def redact(self, value: Any, *, key: str = "") -> Any:
        if isinstance(value, dict):
            return {
                child_key: self.redact(child, key=str(child_key))
                for child_key, child in value.items()
            }
        if isinstance(value, list):
            return [self.redact(child, key=key) for child in value]
        if isinstance(value, tuple):
            return tuple(self.redact(child, key=key) for child in value)
        if isinstance(value, str):
            return self.redact_text(value)
        if key in self._COORDINATE_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if any(
                abs(numeric - candidate) <= 0.00001
                for candidate in self._numbers - self._promoted_numbers
            ):
                return _TRANSIENT_REDACTION
        return value

    def storage_safe_query(self, query: Any) -> str:
        raw = str(query or "").strip()
        # Once a transient lead has entered the live context, any later model
        # query may be a paraphrase of it. Persist only an opaque duplicate key
        # until an independent provider result validates that call.
        if not self.has_unverified() and not self.contains_unverified(raw):
            return raw
        # A digest is still a durable derivative of provider data. Use one
        # constant marker; exact duplicate detection remains run-local on the
        # raw model arguments and never needs a persisted fingerprint.
        return _TRANSIENT_QUERY_REDACTION

    def safe_free_text(self, value: Any, *, purpose: str) -> str:
        """Discard model prose after a transient lead entered its context.

        Exact string scrubbing is useful defense in depth, but cannot prove a
        paraphrase is independent. Server-authored operational text is the only
        safe durable representation once Brave no-storage content was exposed.
        """

        if self.candidate_count:
            return f"[{purpose}: Brave transient provider prose discarded]"
        return self.redact_text(value)


def _persistable_tool_result(
    name: str,
    result: Any,
    *,
    transient_taint: _TransientProviderTaint | None = None,
    transient_input: bool = False,
) -> Any:
    """Strip provider payloads whose license permits transient use only.

    The model may inspect Brave place candidates in the live tool round, but a
    standard Search plan does not grant retention rights. Keep independently
    obtained ordinary web results, but do not retain candidate content, counts,
    provider errors, or identifiers derived from the transient response.
    """

    safe = result
    if name == "web_search" and isinstance(result, dict):
        candidates = result.get("place_candidates")
        brave_attempt = any(
            isinstance(item, dict) and item.get("provider") == "brave_place"
            for item in (result.get("provider_attempts") or [])
        )
        if isinstance(candidates, list) or brave_attempt:
            safe = dict(result)
            safe.pop("place_candidates", None)
            safe.pop("place_candidate_summary", None)
            safe["provider_attempts"] = [
                {"provider": "brave_place", "status": "transient_discarded"}
                if isinstance(item, dict) and item.get("provider") == "brave_place"
                else item
                for item in (result.get("provider_attempts") or [])
            ]
        if transient_input:
            safe = dict(safe)
            safe.pop("backend_errors", None)
            if safe.get("error") and not safe.get("results"):
                safe = {
                    "error": "independent_search_failed",
                    "results": [],
                    "provider_attempts": safe.get("provider_attempts", []),
                }
    if transient_input and name == "fetch_page" and transient_taint is not None:
        # A successful fetch can be used in the live run to corroborate a lead,
        # but its model-selected URL and page body are not written to run steps
        # after no-retention provider context. The proposal projector below may
        # retain only a separately discovered matching page.
        safe = (
            {
                "validation_status": "independent_page_validated",
                "text": "Server verified an independently retainable public page.",
            }
            if transient_taint.independently_validated_call(name, result)
            else {"error": "independent_verification_failed"}
        )
    elif (
        transient_input
        and name == "geocode_place"
        and transient_taint is not None
        and not transient_taint.independently_validated_call(name, result)
    ):
        safe = {"error": "independent_verification_failed"}
    return transient_taint.redact(safe) if transient_taint is not None else safe


def _persistable_tool_args(
    name: str,
    args: dict[str, Any],
    result: Any,
    *,
    transient_taint: _TransientProviderTaint,
    transient_input: bool,
    storage_safe_query: str = "",
) -> dict[str, Any]:
    """Project model arguments onto a retention-safe durable shape."""

    if name == "web_search":
        return {
            "query": storage_safe_query or _TRANSIENT_QUERY_REDACTION,
            "max_results": args.get("max_results"),
        }
    if transient_input and name == "geocode_place":
        return {"query": _TRANSIENT_QUERY_REDACTION}
    if transient_input and name == "fetch_page":
        return {"url": _TRANSIENT_QUERY_REDACTION}
    if transient_input and name not in {"propose_place", "upsert_agent_task"}:
        return {"transient_input": "discarded"}
    return transient_taint.redact(args)


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
                "ok", "error", "detail", "status", "id", "proposal_id", "place_id", "task_id",
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
    focus_hint: str = "",
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
    continuation = (
        f"Keep this immutable scope: {focus_hint}. Use only advertised tools; "
        "do not inspect queue items or another place."
        if focus_hint
        else (
            "최근 관찰만 사용해 현재 1차 목표를 이어가세요. 필요한 현재 상태는 list_agent_tasks와 "
            "list_places/get_place로 다시 확인하되, 이미 끝낸 장소를 반복 조사하지 마세요."
        )
    )
    compact_note = {
        "role": "user",
        "content": (
            "【이전 ReAct 문맥 자동 압축】 오래된 원문·도구 응답은 AgentRunStep에 보존되어 있습니다. "
            f"현재 성과 점수 {current_score}, 실제 변경 {len(material_changes)}건. "
            f"도구 누계: {json.dumps(tool_counts, ensure_ascii=False)}. "
            + continuation
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
QUALITY_SOURCE_REVISIONS = {
    # Legal image sources currently available to persisted quality work.
    # Brave place/photo payloads are discovery-only and intentionally absent.
    "image": "wikimedia:v1|openverse:v1|manual-upload:v1",
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
    raw_gaps_by_id = {marker.id: _marker_quality_gaps(marker) for marker in points}
    gaps_by_id = filter_actionable_quality_gaps(
        db,
        markers=points,
        gaps_by_id=raw_gaps_by_id,
        source_revisions=QUALITY_SOURCE_REVISIONS,
    )

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
                relevant_gaps = QUALITY_GAPS_BY_TASK_KIND.get(kind, frozenset())
                physical_count = sum(
                    1
                    for marker in points
                    if relevant_gaps & set(raw_gaps_by_id.get(marker.id, []))
                )
                prefix = f"실행 #{run_id}: " if run_id else ""
                row.result = (
                    f"{prefix}운영 DB에는 물리적 결손 {physical_count}건이 남아 있으나 "
                    "근거가 있는 terminal/cooldown disposition으로 현재 실행 대상에서 제외했습니다. "
                    "장소·구역·공급자 조건이 바뀌면 자동 재개됩니다."
                    if physical_count
                    else f"{prefix}운영 DB 재측정 결과 해당 품질 결손이 없습니다."
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


def _recent_autonomous_mission_kinds(
    db: Session,
    *,
    city_id: int,
    limit: int,
) -> list[str]:
    """Return recent scheduled lanes for one city, including legacy idle runs."""

    rows = (
        db.query(AgentRun)
        .filter(
            AgentRun.city_id == city_id,
            AgentRun.mode.in_(("research", "idle")),
        )
        .order_by(AgentRun.id.desc())
        .limit(max(1, limit))
        .all()
    )
    kinds: list[str] = []
    for row in rows:
        mission = db.get(AgentMission, row.mission_id) if row.mission_id else None
        kinds.append(mission.kind if mission is not None else "idle")
    return kinds


def _candidate_discovery_due(db: Session, *, city_id: int) -> bool:
    """Guarantee one discovery slice in every configured autonomous window."""

    recent = _recent_autonomous_mission_kinds(
        db,
        city_id=city_id,
        limit=CANDIDATE_DISCOVERY_INTERVAL - 1,
    )
    return CANDIDATE_DISCOVERY_KIND not in recent


def _ensure_candidate_discovery_task(db: Session, *, city: City) -> AgentTask:
    """Create or resume the single bounded discovery cycle for a city."""

    task = (
        db.query(AgentTask)
        .filter(
            AgentTask.city_id == city.id,
            AgentTask.kind == CANDIDATE_DISCOVERY_KIND,
            AgentTask.status.in_(("pending", "blocked")),
        )
        .order_by(AgentTask.id.desc())
        .first()
    )
    if task is None:
        task = AgentTask(
            city_id=city.id,
            kind=CANDIDATE_DISCOVERY_KIND,
            title=f"자동 신규 장소 발굴: {city.name_ko}",
            detail=(
                f"{city.name_ko}({city.name_local})의 현재 지도와 조사 이력을 읽고, "
                f"{_research_themes(city)} 중 부족한 여행 역할 하나를 선택하세요. "
                "서로 다른 출처의 본문으로 정확한 상호·지점·주소를 확인하고 기존 장소와 중복되지 않는 "
                "후보만 좌표 검증 후 관리자 승인 제안으로 남기세요. 안전한 후보가 없으면 조사한 출처 축과 "
                "탈락 사유, 다음에 달라져야 할 재시도 조건을 기록하세요."
            ),
            success_metric=(
                "중복·본문·좌표가 검증된 신규 장소 승인 제안 1건 이상 또는 서로 다른 출처 축을 "
                "소진했다는 감사 가능한 차단 결과"
            ),
            priority=88,
            status="pending",
        )
        db.add(task)
        db.commit()
        db.refresh(task)
    elif task.status == "blocked":
        # Preserve the prior mission/work-item/checkpoint chain. A blocked
        # discovery slice becomes executable only when the weighted scheduler
        # reserves the next discovery turn and calls this helper again.
        task.status = "pending"
        task.completed_at = None
        db.commit()
        db.refresh(task)
    return task


def _fair_non_discovery_task(db: Session, *, city_id: int) -> AgentTask | None:
    """Choose durable work by safety, fewest slices, then priority.

    A recently paused mission keeps its cooldown. Once that expires it competes
    on attempts, so an always-active high-priority quality dimension cannot
    permanently prevent an older paused quality mission from resuming.
    """

    cooldown_cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    candidates = (
        db.query(AgentTask)
        .filter(
            AgentTask.city_id == city_id,
            AgentTask.status == "pending",
            AgentTask.kind != CANDIDATE_DISCOVERY_KIND,
        )
        .all()
    )
    eligible: list[tuple[AgentTask, AgentMission | None]] = []
    for task in candidates:
        mission = (
            db.query(AgentMission)
            .filter(
                AgentMission.city_id == city_id,
                AgentMission.task_id == task.id,
                AgentMission.status.in_(("active", "paused")),
            )
            .order_by(AgentMission.id.desc())
            .first()
        )
        if mission is not None and mission.status == "paused":
            # Let the database compare timestamps: SQLite test rows are naive
            # while PostgreSQL production rows are timezone-aware.
            cooling = db.query(AgentMission.id).filter(
                AgentMission.id == mission.id,
                AgentMission.updated_at > cooldown_cutoff,
            ).first()
            if cooling is not None:
                continue
        eligible.append((task, mission))
    if not eligible:
        return None

    def fair_key(row: tuple[AgentTask, AgentMission | None]) -> tuple[Any, ...]:
        task, mission = row
        return (
            0 if task.kind == "data_integrity" else 1,
            int(task.attempts or 0),
            int(mission.last_run_id or 0) if mission is not None else 0,
            -int(task.priority or 0),
            task.id,
        )

    return min(eligible, key=fair_key)[0]


def _select_autonomous_task(
    db: Session,
    *,
    city: City,
) -> tuple[AgentTask, str, bool]:
    """Select a safety-first lane while reserving periodic discovery capacity."""

    non_discovery = _fair_non_discovery_task(db, city_id=city.id)
    # Operational integrity is explicit safety work and remains ahead of the
    # weighted research lanes. User events are handled before this function.
    if non_discovery is not None and non_discovery.kind == "data_integrity":
        return non_discovery, "data_integrity", False

    recent_lanes = _recent_autonomous_mission_kinds(db, city_id=city.id, limit=1)
    if non_discovery is not None and not recent_lanes:
        first_mission = (
            db.query(AgentMission)
            .filter(
                AgentMission.city_id == city.id,
                AgentMission.task_id == non_discovery.id,
                AgentMission.status.in_(("active", "paused")),
            )
            .order_by(AgentMission.id.desc())
            .first()
        )
        if first_mission is None or first_mission.last_run_id is None:
            # Give pre-existing durable work one initial checkpointed slice on
            # upgrade. Its research run immediately makes discovery due next,
            # preserving continuity without allowing backlog starvation.
            return non_discovery, "quality_or_backlog", False

    discovery_due = _candidate_discovery_due(db, city_id=city.id)
    if discovery_due:
        return (
            _ensure_candidate_discovery_task(db, city=city),
            CANDIDATE_DISCOVERY_KIND,
            True,
        )
    if non_discovery is not None:
        return non_discovery, "quality_or_backlog", False
    # Avoid a no-op idle run when every quality target is cooling down. This is
    # an opportunistic discovery slice; the periodic counter still lets a cooled
    # quality mission win on the next non-due run once it becomes executable.
    return (
        _ensure_candidate_discovery_task(db, city=city),
        CANDIDATE_DISCOVERY_KIND,
        False,
    )


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
    queue_mode = unread_before > 0
    autonomous_mode = bool(allow_research and not queue_mode)
    if not queue_mode and not allow_research:
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
    queue = _work_queue(db, city_id=city_id) if queue_mode else {
        "events": [], "appeals": [], "event_ids": [], "appeal_ids": [], "total": 0,
    }
    personalization_hint = city_personalization_brief(db, city_id=city_id)[:7000]
    quality_task_ids_before = (
        _sync_quality_tasks(db, city_id=city_id) if autonomous_mode else []
    )
    primary_task = None
    active_mission = None
    active_work_item = None
    selected_lane = "queue" if queue_mode else "disabled"
    discovery_reserved = False
    if autonomous_mode:
        # Repair any legacy task-first integrity completion before lane
        # selection. Terminal audit cursors must never steal a later research
        # slice merely because their old mission row still says active.
        terminal_integrity_missions = (
            db.query(AgentMission)
            .join(AgentWorkItem, AgentWorkItem.mission_id == AgentMission.id)
            .filter(
                AgentMission.city_id == city_id,
                AgentMission.kind == "data_integrity",
                AgentMission.status == "active",
                AgentWorkItem.status.in_(("active", "ready")),
            )
            .order_by(AgentMission.id.desc())
            .all()
        )
        repaired_integrity = False
        for resumable_mission in terminal_integrity_missions:
            integrity_task = db.get(AgentTask, resumable_mission.task_id)
            if _complete_data_integrity_mission(
                db,
                mission=resumable_mission,
                task=integrity_task,
            ):
                repaired_integrity = True
        if repaired_integrity:
            db.commit()
        if repaired_integrity and _fair_non_discovery_task(db, city_id=city_id) is None:
            performance = _performance_snapshot(db, city_id)
            repair_run = AgentRun(
                city_id=city_id,
                mode="idle",
                status="completed",
                objective="Reconcile an immutable legacy integrity terminal cursor.",
                score=0,
                metrics=json.dumps({
                    "before": performance,
                    "after": performance,
                    "delta": {},
                    "tool_counts": {},
                    "material_changes": [],
                    "material_change_count": 0,
                    "lane": "integrity_repair",
                    "idle_reason": "legacy_integrity_terminal_reconciled",
                    "quality_task_ids_before": quality_task_ids_before,
                }, ensure_ascii=False),
                summary="완료된 무결성 과제의 지속 미션 상태를 모델 호출 없이 원자적으로 복구했습니다.",
                finished_at=datetime.now(timezone.utc),
            )
            db.add(repair_run)
            db.commit()
            db.refresh(repair_run)
            return {
                "ok": True,
                "status": "completed",
                "steps": 0,
                "message": repair_run.summary,
                "unread_before": 0,
                "unread_after": 0,
                "tool_counts": {},
                "score": 0,
                "performance": {},
                "remaining_gaps": _research_gaps({}, {}, performance),
                "run_id": repair_run.id,
                "city_id": city_id,
            }
        primary_task, selected_lane, discovery_reserved = _select_autonomous_task(
            db,
            city=city,
        )
        primary_task.attempts += 1
        active_mission, active_work_item = ensure_mission_for_task(db, primary_task)
        db.commit()
    integrity_mode = bool(
        autonomous_mode
        and active_mission is not None
        and active_mission.kind == "data_integrity"
        and active_work_item is not None
    )
    discovery_mode = bool(
        autonomous_mode
        and active_mission is not None
        and active_mission.kind == CANDIDATE_DISCOVERY_KIND
        and active_work_item is not None
    )
    scoped_quality_mode = bool(
        autonomous_mode
        and active_mission is not None
        and active_mission.kind in QUALITY_TASK_KINDS
        and active_work_item is not None
    )
    integrity_evidence_refs: dict[str, str] = {}
    integrity_corrective_resume = False
    continuity_payload = (
        mission_context(active_mission, active_work_item)
        if active_mission is not None and active_work_item is not None
        else {}
    )
    if integrity_mode:
        # Legacy production cursors contained a free-form ``result`` example,
        # which the provider copied into the new structured tool and rejected.
        # Preserve the durable state in DB but expose only server-owned phase
        # metadata; the advertised provider schema is the sole argument truth.
        integrity_evidence_refs = _data_integrity_evidence_refs(
            db,
            active_mission,
            active_work_item,
        )
        integrity_corrective_resume = _resume_data_integrity_corrective_result(
            active_mission, active_work_item
        ) and _has_data_integrity_target_read(
            integrity_evidence_refs
        )
        safe_next_action = {
            "phase": "data_integrity_terminal_verdict_v1",
            "tool": "upsert_agent_task",
            "task_id": active_mission.task_id,
            "guard_disposition": "decide",
        } if integrity_corrective_resume else {}
        if integrity_corrective_resume:
            # Mission objective/success_metric and legacy recovery history may
            # enumerate obsolete free-form result keys. In terminal correction,
            # expose only server-owned phase/identity metadata; the advertised
            # six-field provider schema is the sole output contract.
            continuity_payload = {
                "mission_id": active_mission.id,
                "work_item_id": active_work_item.id,
                "target": {
                    "type": active_work_item.target_type,
                    "key": active_work_item.target_key,
                    "place_id": active_work_item.place_id,
                    "title": active_work_item.title,
                },
                "stage": "decide",
                "status": active_work_item.status,
                "progress": {
                    "active_work_item_id": active_work_item.id,
                    "next_action": safe_next_action,
                },
                "next_action": safe_next_action,
            }
        else:
            continuity_payload["next_action"] = safe_next_action
            progress = continuity_payload.get("progress")
            if isinstance(progress, dict) and "next_action" in progress:
                progress = dict(progress)
                progress["next_action"] = safe_next_action
                continuity_payload["progress"] = progress
    continuity_hint = json.dumps(
        continuity_payload, ensure_ascii=False
    )[:7000] if continuity_payload else "{}"
    primary_task_hint = (
        f"백로그 #{primary_task.id} '{primary_task.title}'. 상세: {primary_task.detail or '없음'}. "
        f"이전 실행 인계: {(primary_task.result or '없음')[:3000]}. "
        f"성공조건: {primary_task.success_metric or '근거가 있는 실제 DB 변화로 완료 여부 입증'}."
        if primary_task is not None
        else "지정된 백로그가 없습니다. 측정된 여행 역할 공백 중 가치가 가장 큰 하나를 먼저 선택하세요."
    )
    if integrity_mode and primary_task is not None:
        # Pending legacy task.result text may contain an obsolete free-form tool
        # example. The exact task definition is useful; model-copyable handoff
        # payloads are not. Durable observations are supplied through owned refs.
        primary_task_hint = (
            f"백로그 #{primary_task.id} '{primary_task.title}'. "
            "서버 소유 evidence_refs를 인용해 현재 광고된 스키마로 "
            "현재 과제의 근거 기반 결과만 기록하세요."
            if integrity_corrective_resume
            else (
                f"백로그 #{primary_task.id} '{primary_task.title}'. "
                f"상세: {primary_task.detail or '없음'}. "
                f"성공조건: {primary_task.success_metric or '활성 대상 관찰을 인용한 감사 결과 기록'}."
            )
        )

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_model or "openai/gpt-oss-120b"
    # 종료는 아래 성과 게이트/정체 판단으로 결정한다. 이 값은 비정상 무한루프만 막는 안전 상한이다.
    steps_limit = max_steps or (
        max(40, settings.agent_max_steps)
        if autonomous_mode
        else max(100, 64 + unread_before * 4)
    )
    performance_before = _performance_snapshot(db, city_id)
    agent_run = AgentRun(
        city_id=city_id,
        mission_id=active_mission.id if active_mission is not None else None,
        work_item_id=active_work_item.id if active_work_item is not None else None,
        mode="research" if autonomous_mode else "queue",
        status="running",
        objective=(
            f"{primary_task_hint}만 감사하고 종료"
            if integrity_mode
            else f"{primary_task_hint} 신규 장소 승인 후보 발굴 조각 수행"
            if discovery_mode
            else f"{primary_task_hint} 현재 품질 미션의 활성 대상만 처리"
            if scoped_quality_mode
            else f"{primary_task_hint} 완료 후 다음 성과 공백 진행"
            if autonomous_mode
            else "사용자 작업 큐 전원 처리"
        ),
        metrics=json.dumps({
            "before": performance_before,
            "lane": selected_lane,
            "discovery_reserved": discovery_reserved,
        }, ensure_ascii=False),
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

    if autonomous_mode:
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
            "5) 큐가 비면 같은 실행에서 연구·품질 백로그로 전환하지 말고 즉시 한 줄 요약 후 종료\n"
            "일부만 처리하고 끝내면 실패이며, 큐 밖 작업을 시작해도 실패다."
        )

    # Research used to be a long fixed checklist.  Keep queue handling explicit,
    # but let research adapt its plan to measured gaps and verify each outcome.
    if autonomous_mode:
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

    if integrity_mode:
        integrity_phase_instruction = (
            "서버가 이 활성 장소의 성공한 get_place 체크포인트를 이미 검증했고, 현재 요청의 "
            "evidence_refs로 제공합니다. get_place나 조사 도구를 다시 호출하지 마세요. 이번 종결 "
            "단계에서는 광고된 structured upsert_agent_task만 정확히 한 번 호출하세요. 정확히 "
            "task_id, status, verdict, reason, marker_changes, evidence_refs 여섯 필드만 보내고, "
            "원래 과제에 적힌 출력 필드 명세는 폐기되었습니다. 관련 사실은 모두 reason에 넣으며 "
            "result를 포함한 추가 필드는 보내지 마세요."
            if integrity_corrective_resume
            else (
                "이 장소만 get_place로 먼저 읽으세요. 이 성공 관찰 없이는 결과를 종료할 수 없습니다. "
                "그 뒤 필요할 때만 web_search, fetch_page, geocode_place로 동일 장소의 정체성·지점·주소·"
                "좌표 근거를 확인하세요. list_places와 list_agent_tasks는 이 감사에서 사용할 수 없습니다. "
                "다른 place_id, 사용자 큐, 다른 품질 과제는 조회하거나 처리하지 마세요. 마지막에는 현재 요청에 "
                "광고된 structured upsert_agent_task 스키마만 사용해 현재 task_id를 종료하세요. result 필드는 "
                "보내지 마세요."
            )
        )
        user_msg = (
            f"현재 실행 도시는 {city.name_ko}({city.name_local}), city_id={city.id}입니다.\n"
            f"현재 data_integrity 과제: {primary_task_hint}\n"
            f"서버 체크포인트: {continuity_hint}\n"
            f"활성 대상: place_id={active_work_item.place_id}, title={active_work_item.title}.\n"
            f"{integrity_phase_instruction}"
        )
    elif (discovery_mode or scoped_quality_mode) and active_mission is not None and active_work_item is not None:
        user_msg = _scoped_mission_user_message(
            city,
            active_mission,
            active_work_item,
            continuity_hint=continuity_hint,
            personalization_hint=personalization_hint if discovery_mode else "",
        )
    elif autonomous_mode:
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
    if queue_mode:
        runtime_policy = (
            "\n\n【현재 운영 안전 모드 — 위의 연구 할당보다 우선】\n"
            "- 사용자 작업 큐만 처리한다. 자율 웹 조사, 신규 장소 발굴, 사진 보강, 작업량 채우기를 하지 않는다.\n"
            "- 자동 장소 생성과 자동 병합은 비활성화되어 있다. 해당 조치가 필요하면 "
            "propose_place/merge_places를 호출해 근거·출처·신뢰도가 있는 관리자 승인 제안으로 남긴다.\n"
            "- 큐가 비면 즉시 종료한다. 스텝을 채우는 것은 목표가 아니다.\n"
        )
    system_content = (
        _data_integrity_system(
            city,
            active_mission,
            active_work_item,
            corrective_result_only=integrity_corrective_resume,
        )
        if integrity_mode
        else _candidate_discovery_system(city, active_mission, active_work_item)
        if discovery_mode and active_mission is not None and active_work_item is not None
        else _scoped_quality_system(
            city,
            active_mission,
            active_work_item,
            tool_names=RECOVERY_TOOLS_BY_TASK[active_mission.kind],
        )
        if scoped_quality_mode and active_mission is not None and active_work_item is not None
        else _system_for_city(city) + runtime_policy
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_msg},
    ]

    transient_taint = _TransientProviderTaint()
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
    verified_page_records: list[dict[str, Any]] = []
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
    corrective_result_only = integrity_corrective_resume
    corrective_evidence_refs: dict[str, str] = (
        dict(integrity_evidence_refs) if corrective_result_only else {}
    )
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
    scoped_lane_terminal = False
    integrity_focus_hint = (
        f"mission_id={active_mission.id}, task_id={active_mission.task_id}, "
        f"place_id={active_work_item.place_id}, title={active_work_item.title}"
        if integrity_mode
        else (
            f"mission_id={active_mission.id}, task_id={active_mission.task_id}, "
            f"target={active_work_item.target_key}, title={active_work_item.title}; "
            "continue only this scoped mission with the currently advertised tools"
            if (discovery_mode or scoped_quality_mode)
            and active_mission is not None
            and active_work_item is not None
            else ""
        )
    )
    try:
        for _ in range(steps_limit):
            steps += 1
            messages, compacted = _compact_react_messages(
                messages,
                tool_counts=tool_counts,
                material_changes=material_changes,
                current_score=current_score,
                focus_hint=integrity_focus_hint,
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
                if corrective_result_only:
                    # This overrides both the normal mission tools and any
                    # adaptive model-recovery plan installed after a provider
                    # error during correction.
                    mission_tool_names = ["upsert_agent_task"]
                    corrective_evidence_refs = _data_integrity_evidence_refs(
                        db,
                        active_mission,
                        active_work_item,
                    )
                elif active_mission is not None and active_mission.kind == "data_integrity":
                    # This is a hard safety boundary, not merely a model hint.
                    # Clamp even an adaptive-recovery tool list so an old or
                    # malformed strategy can never re-introduce write tools.
                    requested = set(mission_tool_names or DATA_INTEGRITY_TOOLS)
                    mission_tool_names = sorted(
                        (requested & DATA_INTEGRITY_TOOLS) or DATA_INTEGRITY_TOOLS
                    )
                if (
                    not mission_tool_names
                    and autonomous_mode
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
                    tools=(
                        _data_integrity_corrective_tools(
                            task_id=int(active_mission.task_id),
                            evidence_refs=sorted(corrective_evidence_refs),
                            evidence_details=corrective_evidence_refs,
                        )
                        if corrective_result_only
                        else _candidate_discovery_tools(
                            task_id=int(active_mission.task_id),
                            tool_names=mission_tool_names,
                        )
                        if discovery_mode
                        and active_mission is not None
                        and active_mission.task_id is not None
                        else _filtered_tools(mission_tool_names)
                    ),
                    tool_choice="required" if corrective_result_only else "auto",
                    temperature=0.2,
                    **extra,
                )
            except Exception as exc:
                # 모델이 스키마에 안 맞는 인자(null 등)를 생성한 경우: 사이클을 죽이지 않고 교정 재시도
                detail = str(exc)
                persistable_error_detail = transient_taint.safe_free_text(
                    detail,
                    purpose="model-output error",
                )
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
                    if corrective_result_only:
                        # The recovery planner is intentionally reusable across
                        # mission stages and may suggest get_place. A terminal
                        # integrity cursor is stricter: the exact-target read is
                        # already server-owned evidence and every retry must keep
                        # the same upsert-only provider/runtime boundary.
                        strategy = {
                            **strategy,
                            "tool_names": ["upsert_agent_task"],
                            "corrective_result_only": True,
                        }
                    messages, recovery_compacted = _compact_react_messages(
                        messages,
                        tool_counts=tool_counts,
                        material_changes=material_changes,
                        current_score=current_score,
                        max_chars=int(strategy["max_chars"]),
                        force=bool(strategy["force_compaction"]),
                        recent_round_limit=int(strategy["recent_round_limit"]),
                        focus_hint=integrity_focus_hint,
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
                            "error": persistable_error_detail[:1200],
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
                        error=persistable_error_detail,
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
                            f"Current target: {integrity_focus_hint or (active_work_item.target_key if active_work_item else 'none')}."
                            + (
                                " The exact active target was already observed in a server-owned "
                                "checkpoint. Do not call get_place or any research tool; call the "
                                "advertised structured upsert_agent_task exactly once with only "
                                "task_id, status, verdict, reason, marker_changes, and evidence_refs. "
                                "The original task's output-field list is superseded; put all relevant "
                                "facts in reason and send no additional fields."
                                if corrective_result_only
                                else " Do not inspect or process unread queue items."
                                if integrity_mode
                                else ""
                            )
                        ),
                    })
                    continue
                if failure_kind:
                    terminal_model_failure_kind = failure_kind
                if not failure_kind and "tool_use_failed" in detail and schema_retries < 3:
                    schema_retries += 1
                    schema_retry_instruction = (
                        "활성 대상은 서버 체크포인트에서 이미 관찰되었습니다. get_place를 "
                        "호출하지 말고 현재 광고된 structured upsert_agent_task에 정확히 task_id, "
                        "status, verdict, reason, marker_changes, evidence_refs 여섯 필드만 보내세요. "
                        "원래 과제의 출력 필드는 폐기되었으며 모든 관련 사실은 reason에 넣으세요. "
                        if corrective_result_only
                        else (
                            "값이 없는 선택 필드는 null을 넣지 말고 아예 생략한 뒤 "
                            "같은 툴을 다시 호출하세요. "
                        )
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "직전 툴 호출이 스키마 검증에 실패했습니다. "
                                + schema_retry_instruction
                                + f"오류: {persistable_error_detail[:600]}"
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
                if corrective_result_only and steps < steps_limit:
                    messages.append({
                        "role": "user",
                        "content": (
                            "정책 가드 교정 단계는 서술로 종료할 수 없습니다. 현재 미션의 task_id로 "
                            "현재 요청에 광고된 upsert_agent_task 스키마를 그대로 사용하세요. "
                            "필수 필드는 task_id, status=completed, verdict=unresolved, reason, "
                            "marker_changes=0, evidence_refs이며 result 필드는 보내지 마세요."
                        ),
                    })
                    continue
                remaining = count_unread(db, city_id)
                if queue_mode and remaining > 0 and work_nudges < 4 and steps < steps_limit:
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
                if autonomous_mode:
                    _sync_quality_tasks(db, city_id=city_id, run_id=agent_run.id)
                    if primary_task is not None:
                        db.refresh(primary_task)
                    if (
                        (discovery_mode or scoped_quality_mode)
                        and primary_task is not None
                        and primary_task.status in {"completed", "blocked"}
                    ):
                        finalize_mission(
                            db,
                            mission=active_mission,
                            task=primary_task,
                            run_id=agent_run.id,
                        )
                        final_text = msg.content or (
                            "현재 전용 미션의 성공조건 또는 감사 가능한 차단조건을 충족해 종료했습니다."
                        )
                        break
                current_snapshot = _performance_snapshot(db, city_id)
                current_delta = _performance_delta(performance_before, current_snapshot)
                gaps = _research_gaps(current_delta, successful_tool_counts, current_snapshot) if autonomous_mode else []
                current_score = _performance_score(current_delta, successful_tool_counts)
                if gaps and progress_nudges < 8 and no_progress_actions < 18 and steps < steps_limit:
                    progress_nudges += 1
                    scoped_progress = (
                        "현재 미션과 활성 대상에서 벗어나지 말고, 현재 요청에 광고된 도구만 사용해 "
                        "성공조건에 가장 가까운 행동을 수행하세요. 기존 체크포인트와 새 근거를 재사용하고, "
                        "완료가 불가능하면 현재 task_id에 차단 원인과 달라져야 할 재시도 조건을 기록하세요."
                        if discovery_mode or scoped_quality_mode
                        else (
                            "같은 검색을 반복하지 말고 list_agent_tasks를 다시 읽어 운영 DB가 생성한 "
                            "정확한 장소 ID 단위 품질 백로그를 이어서 처리하세요. 신규 장소 후보는 "
                            "geocode_place 직후 propose_place로 승인 대기에 저장하세요. 그 후보를 "
                            "upsert_agent_task에 승인 제안으로 적는 것은 금지됩니다. 실패한 후속 조사만 "
                            "지식 본문이 아니라 upsert_agent_task에 구체적으로 남기세요."
                        )
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"성과 게이트가 아직 충족되지 않았습니다(현재 점수 {current_score}). "
                                f"남은 결과: {', '.join(gaps)}. 스텝 수가 아니라 실제 결과를 만들고 다시 측정하세요. "
                                + scoped_progress
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
            for tool_call_index, tc in enumerate(tool_calls):
                if queue_mode and count_unread(db, city_id) == 0:
                    # A queue run has no authority to become an autonomous run.
                    # This also covers another worker draining the queue between
                    # the model response and local tool execution.
                    mission_halted = True
                    final_text = "사용자 작업 큐가 이미 비어 연구 전환 없이 종료했습니다."
                    break
                name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                argument_error = ""
                try:
                    args = json.loads(raw_args)
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must decode to an object")
                except (json.JSONDecodeError, ValueError) as exc:
                    argument_error = f"malformed_tool_arguments: {exc}"
                    if transient_taint.candidate_count:
                        args = {"_malformed_arguments": "discarded_after_transient_provider_context"}
                    else:
                        raw_digest = hashlib.sha256(raw_args.encode("utf-8", errors="replace")).hexdigest()
                        args = {
                            "_malformed_arguments_sha256": raw_digest,
                            "_malformed_arguments_preview": raw_args[:300],
                        }
                used_tools.add(name)
                tool_counts[name] = tool_counts.get(name, 0) + 1
                transient_input = transient_taint.candidate_count > 0
                signature = _tool_signature(name, args)
                repeated = name in EXPENSIVE_RESEARCH_TOOLS and signature in seen_expensive_calls
                storage_safe_search_query = (
                    transient_taint.storage_safe_query(args.get("query"))
                    if name == "web_search"
                    else ""
                )
                normalized_query = (
                    _normalize_research_query(args.get("query"))
                    if name == "web_search"
                    else ""
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
                transient_mutation_violation = bool(
                    not argument_error
                    and name in MUTATION_TOOLS
                    and transient_input
                    # Discovery proposals are rebuilt from server-owned
                    # independent evidence at execution time below. No model
                    # field survives that projection, including paraphrases
                    # that an exact taint matcher could never detect.
                    and name not in {"propose_place", "upsert_agent_task"}
                )
                agent_task_mismatch = (
                    None
                    if argument_error
                    else _active_agent_task_mismatch(name, args, active_mission)
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
                    if not argument_error
                    else (args, None)
                )
                if (
                    not argument_error
                    and active_mission is not None
                    and active_mission.kind == CANDIDATE_DISCOVERY_KIND
                    and name == "upsert_agent_task"
                ):
                    projected_task_args, task_projection_error = (
                        _project_candidate_discovery_task_result_args(
                            name,
                            args,
                            active_mission,
                            primary_task,
                            run_id=agent_run.id,
                            transient_provider_seen=bool(transient_taint.candidate_count),
                            research_evidence_refs=_candidate_discovery_research_refs(
                                db,
                                run_id=agent_run.id,
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
                integrity_scope_violation = bool(
                    not argument_error
                    and active_mission is not None
                    and active_mission.kind == "data_integrity"
                    and name not in DATA_INTEGRITY_TOOLS
                )
                corrective_scope_violation = bool(
                    not argument_error
                    and corrective_result_only
                    and name != "upsert_agent_task"
                )
                corrective_status_violation = bool(
                    not argument_error
                    and corrective_result_only
                    and name == "upsert_agent_task"
                    and str(args.get("status") or "").strip().lower() != "completed"
                )
                integrity_task_terminal_request = bool(
                    active_mission is not None
                    and active_mission.kind == "data_integrity"
                    and name == "upsert_agent_task"
                    and str(args.get("status") or "").strip().lower()
                    in DATA_INTEGRITY_TASK_RESULT_STATUSES
                )
                structured_task_args, corrective_result_error = (
                    _project_structured_integrity_result(
                        args,
                        allowed_refs=corrective_evidence_refs,
                    )
                    if corrective_result_only
                    and name == "upsert_agent_task"
                    and not argument_error
                    and not corrective_status_violation
                    else ({}, None)
                )
                if structured_task_args:
                    projected_task_args = structured_task_args
                normal_terminal_write = bool(
                    integrity_task_terminal_request and not corrective_result_only
                )
                malformed_attempt = 0
                effective_tool_args = args
                if transient_mutation_violation:
                    result = {
                        "error": "transient_provider_data_not_verified",
                        "error_class": "policy_guard",
                        "guard_disposition": "retry",
                        "detail": (
                            "Brave Place의 no-storage 후보 원문은 운영 DB에 기록할 수 없습니다. "
                            "geocode_place의 저장 가능한 좌표 또는 fetch_page의 유효한 공개 본문으로 "
                            "같은 사실을 독립 확인한 뒤 canonical 값만 다시 사용하세요."
                        ),
                    }
                elif corrective_scope_violation:
                    result = {
                        "error": "tool_not_allowed_for_data_integrity",
                        "error_class": "policy_guard",
                        "guard_disposition": "retry",
                        "detail": (
                            "정책 가드 교정 단계에서는 현재 감사 과제의 결과를 기록하는 "
                            "upsert_agent_task만 실행할 수 있습니다. 다른 호출은 실행하지 않았습니다."
                        ),
                        "allowed_tools": ["upsert_agent_task"],
                    }
                elif corrective_status_violation:
                    result = {
                        "error": "invalid_data_integrity_task_status",
                        "error_class": "policy_guard",
                        "guard_disposition": "retry",
                        "detail": (
                            "정책 가드 교정 단계는 현재 감사 과제를 verdict=unresolved로 completed "
                            "종료해야 합니다. pending 또는 blocked 상태는 즉시 재개를 유발할 수 있어 "
                            "실행하지 않았습니다."
                        ),
                        "allowed_statuses": ["completed"],
                        "requested_status": args.get("status"),
                    }
                elif integrity_scope_violation:
                    # Providers normally call only advertised tools, but never
                    # rely on that for a read-only operational-data boundary.
                    result = {
                        "error": "tool_not_allowed_for_data_integrity",
                        "error_class": "policy_guard",
                        "guard_disposition": "retry",
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
                elif normal_terminal_write:
                    allowed_refs = _data_integrity_evidence_refs(
                        db,
                        active_mission,
                        active_work_item,
                    )
                    result = {
                        "error": "structured_integrity_verdict_required",
                        "error_class": "policy_guard",
                        "guard_disposition": (
                            "decide"
                            if _has_data_integrity_target_read(allowed_refs)
                            else "retry"
                        ),
                        "detail": (
                            "A data_integrity task can only close through the "
                            "server-owned structured verdict schema. Cite an "
                            "advertised evidence ref on the next call."
                        ),
                        "allowed_evidence_refs": sorted(allowed_refs),
                    }
                elif corrective_result_error is not None:
                    result = corrective_result_error
                elif repeated_data_integrity_place_read:
                    result = {
                        "error": "duplicate_data_integrity_place_read",
                        "error_class": "policy_guard",
                        "guard_disposition": "decide",
                        "detail": (
                            f"장소 #{data_integrity_get_place_id}의 get_place 결과는 이번 감사에서 이미 "
                            "성공적으로 읽었습니다. 같은 조회를 반복하지 말고 기존 관찰을 사용해 "
                            f"현재 과제 task_id={active_mission.task_id if active_mission else None}의 "
                            "result를 upsert_agent_task로 기록하세요."
                        ),
                        "place_id": data_integrity_get_place_id,
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
                        "signature": args.get(
                            "_malformed_arguments_sha256",
                            "discarded_after_transient_provider_context",
                        ),
                    }
                elif target_mismatch is not None:
                    result = target_mismatch
                elif decision_required:
                    result = {
                        "error": "material_decision_required",
                        "error_class": "policy_guard",
                        "guard_disposition": "decide",
                        "detail": (
                            "실제 DB 변화 없이 조사 도구를 8회 사용했습니다. 추가 조회·검색을 중단하고 "
                            "이미 확보한 근거만 사용해 현재 요청에 광고된 저장 도구 중 성공조건에 맞는 "
                            "하나를 안전하게 실행하세요. 근거가 부족하면 현재 과제의 차단 원인과 다음 검증 "
                            "방법을 task 결과에 남기세요. 추측 저장과 다른 대상으로의 전환은 금지합니다."
                        ),
                    }
                elif recent_search_repeat:
                    repeated_calls += 1
                    repeated = True
                    result = {
                        "error": "recent_duplicate_search",
                        "error_class": "policy_guard",
                        "guard_disposition": "retry",
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
                        "error_class": "policy_guard",
                        "guard_disposition": "retry",
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
                        and active_mission.kind in {"data_integrity", CANDIDATE_DISCOVERY_KIND}
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
                    effective_tool_args = tool_args
                    if name == "propose_place":
                        if transient_input:
                            # Until the server projection below succeeds, no
                            # model-authored proposal field is durable.
                            effective_tool_args = {"transient_input": "discarded"}
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
                            projection_error = None
                            if transient_input and discovery_mode:
                                canonical_args, projection_error = _canonical_transient_proposal_args(
                                    coordinate_evidence,
                                    verified_page_records,
                                    transient_taint,
                                )
                                if canonical_args is not None:
                                    tool_args = canonical_args
                            if projection_error is not None:
                                result = projection_error
                            else:
                                tool_args = {**tool_args, "_coordinate_evidence": coordinate_evidence}
                                effective_tool_args = tool_args
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
                            server_defer_commit=bool(
                                active_mission is not None
                                and active_mission.kind in {"data_integrity", CANDIDATE_DISCOVERY_KIND}
                                and name == "upsert_agent_task"
                            ),
                            server_allow_brave_places=(
                                discovery_mode and name == "web_search"
                            ),
                            server_storage_query=(
                                storage_safe_search_query if name == "web_search" else None
                            ),
                            server_record_web_visit=not (
                                transient_input and name == "fetch_page"
                            ),
                        )
                    if name == "web_search":
                        transient_taint.observe_brave_candidates(result)
                    transient_taint.promote_independent_evidence(name, result)
                    if (
                        name == "fetch_page"
                        and isinstance(result, dict)
                        and not result.get("error")
                        and is_useful_fetched_page(result)
                        and result.get("url")
                    ):
                        validated_source_urls.add(str(result["url"]))
                        verified_page_records.append({
                            "url": str(result.get("url") or "")[:1000],
                            "title": str(result.get("title") or "")[:300],
                            "text": str(result.get("text") or "")[:7000],
                            "coordinate_candidates": [
                                dict(row)
                                for row in (result.get("coordinate_candidates") or [])
                                if (
                                    isinstance(row, dict)
                                    and row.get("storage_allowed") is True
                                    and not str(row.get("source") or "").casefold().startswith("brave")
                                )
                            ],
                        })
                    if name in {"geocode_place", "fetch_page"} and isinstance(result, dict):
                        coordinate_rows = (
                            result.get("results") or []
                            if name == "geocode_place"
                            else result.get("coordinate_candidates") or []
                        )
                        for raw_coordinate in coordinate_rows:
                            if (
                                not isinstance(raw_coordinate, dict)
                                or raw_coordinate.get("storage_allowed") is not True
                                or raw_coordinate.get("source") == "brave_place"
                            ):
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
                                    or (args.get("query") if not transient_input else "")
                                    or ""
                                ),
                                "source_url": str(
                                    raw_coordinate.get("source_url")
                                    or result.get("url")
                                    or ""
                                ),
                            }
                            if transient_taint.was_observed_transient(record["source_url"]):
                                record["source_url"] = ""
                            verified_coordinate_records.append(record)
                    if normalized_query:
                        recent_search_queries.add(normalized_query)
                if (
                    isinstance(result, dict)
                    and active_mission is not None
                    and active_mission.kind == "data_integrity"
                    and result.get("error_class") == "policy_guard"
                    and result.get("guard_disposition") == "decide"
                    and not _has_data_integrity_target_read(
                        _data_integrity_evidence_refs(
                            db,
                            active_mission,
                            active_work_item,
                        )
                    )
                ):
                    # A loop/budget guard without a single durable observation
                    # is not enough to close an audit. Correct course and keep
                    # researching until a terminal verdict can cite facts.
                    result = {
                        **result,
                        "guard_disposition": "retry",
                        "detail": (
                            f"{result.get('detail') or ''} No durable observation "
                            "exists yet; gather one allowed observation before "
                            "recording a terminal verdict."
                        ).strip(),
                    }
                error = isinstance(result, dict) and bool(result.get("error"))
                policy_guard_disposition = (
                    str(result.get("guard_disposition") or "")
                    if isinstance(result, dict)
                    else ""
                )
                integrity_policy_guard = bool(
                    error
                    and active_mission is not None
                    and active_mission.kind == "data_integrity"
                    and str(result.get("error") or "")
                    in CORRECTIVE_POLICY_GUARD_ERRORS
                )
                terminal_corrective_guard = bool(
                    error
                    and active_mission is not None
                    and active_mission.kind == "data_integrity"
                    and (
                        corrective_result_only
                        or (
                            integrity_policy_guard
                            and policy_guard_disposition == "decide"
                        )
                    )
                )
                data_integrity_task_completed = bool(
                    active_mission is not None
                    and active_mission.kind == "data_integrity"
                    and name == "upsert_agent_task"
                    and not error
                    and isinstance(result, dict)
                    and result.get("status") == "completed"
                )
                if (
                    not error
                    and active_mission is not None
                    and active_mission.kind == "data_integrity"
                ):
                    if name == "get_place" and data_integrity_get_place_id is not None:
                        data_integrity_place_reads.add(data_integrity_get_place_id)
                if not error:
                    successful_tool_counts[name] = successful_tool_counts.get(name, 0) + 1
                material_change = _is_material_change(name, result)
                # A follow-up fetch after transient provider exposure can help
                # the live decision, but its model-selected URL/body must not
                # flow into durable handoff state. Candidate-discovery terminal
                # logic uses a separate server-owned verification flag.
                new_evidence = (
                    set()
                    if transient_input and name == "fetch_page"
                    else _new_evidence_keys(name, result, evidence_keys)
                )
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
                persistable_args = _persistable_tool_args(
                    name,
                    effective_tool_args,
                    result,
                    transient_taint=transient_taint,
                    transient_input=transient_input,
                    storage_safe_query=storage_safe_search_query,
                )
                persistable_result = _persistable_tool_result(
                    name,
                    result,
                    transient_taint=transient_taint,
                    transient_input=transient_input,
                )
                discovery_search_ok = bool(
                    discovery_mode
                    and name == "web_search"
                    and isinstance(result, dict)
                    and not result.get("error")
                    and isinstance(result.get("results"), list)
                )
                discovery_verification_ok = bool(
                    discovery_mode
                    and isinstance(result, dict)
                    and not result.get("error")
                    and (
                        (name == "fetch_page" and is_useful_fetched_page(result))
                        or (
                            name == "geocode_place"
                            and any(
                                isinstance(candidate, dict)
                                and candidate.get("storage_allowed") is True
                                and not str(candidate.get("source") or "").casefold().startswith("brave")
                                for candidate in (result.get("results") or [])
                            )
                        )
                    )
                )
                db.add(
                    AgentRunStep(
                        run_id=agent_run.id,
                        sequence=action_sequence,
                        phase="observe" if name.startswith("list_") or name in EXPENSIVE_RESEARCH_TOOLS else "act",
                        tool=name,
                        outcome=outcome,
                        score_delta=score_delta,
                        detail=_step_detail_json(
                            persistable_args,
                            persistable_result,
                            {
                                "material_change": material_change,
                                "new_evidence": len(new_evidence),
                                "score": current_score,
                                "no_material_actions": no_material_actions,
                                "discovery_search_ok": discovery_search_ok,
                                "discovery_verification_ok": discovery_verification_ok,
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
                    args=persistable_args,
                    result=persistable_result,
                    outcome=outcome,
                    new_evidence_count=len(new_evidence),
                    material_change=material_change,
                )
                discovery_task_terminal = False
                if (
                    discovery_mode
                    and primary_task is not None
                    and active_mission is not None
                ):
                    if (
                        name == "propose_place"
                        and not error
                        and isinstance(result, dict)
                        and result.get("proposal_created") is True
                    ):
                        primary_task.status = "completed"
                        primary_task.completed_at = datetime.now(timezone.utc)
                        primary_task.result = (
                            f"실행 #{agent_run.id}: 검증된 신규 장소 승인 제안 "
                            f"#{result.get('proposal_id')} 생성"
                        )[:8000]
                    if primary_task.status in {"completed", "blocked"}:
                        finalize_mission(
                            db,
                            mission=active_mission,
                            task=primary_task,
                            run_id=agent_run.id,
                            commit=False,
                        )
                        discovery_task_terminal = True
                        scoped_lane_terminal = True
                        active_work_item = None
                if terminal_corrective_guard:
                    # The next round has one safe purpose: record an honest
                    # terminal verdict on this mission's own task. Re-exposing
                    # research tools here lets the model repeat the same guard
                    # until the generic three-failure rotation fires.
                    local_recovery_tool_names = {"upsert_agent_task"}
                    corrective_result_only = True
                    # Preserve the actual failed action/checkpoint above, then
                    # re-pin only the server-owned correction cursor. This is
                    # required when the run ends before another model round.
                    _persist_data_integrity_corrective_cursor(
                        active_mission,
                        active_work_item,
                        guard_error=str(result.get("error") or "policy_guard"),
                    )
                if active_work_item is not None:
                    agent_run.work_item_id = active_work_item.id
                canonical_active = active_work_item_for_mission(db, active_mission)
                if canonical_active is not None:
                    active_work_item = canonical_active
                    agent_run.work_item_id = canonical_active.id
                zone_assignment_outside_all = bool(
                    active_mission is not None
                    and active_mission.kind == "quality_zones"
                    and active_work_item is not None
                    and active_work_item.place_id is not None
                    and name == "assign_place_zone"
                    and isinstance(result, dict)
                    and result.get("error") == "place_outside_zone_polygon"
                    and result.get("suggested_zone_id") is None
                )
                zone_audit_requested = bool(
                    active_mission is not None
                    and active_mission.kind == "quality_zones"
                    and active_work_item is not None
                    and active_work_item.place_id is not None
                    and (name == "list_zones" or zone_assignment_outside_all)
                )
                zone_audit: dict[str, Any] | None = None
                if zone_audit_requested:
                    target_place = db.get(Marker, int(active_work_item.place_id))
                    if target_place is None:
                        zone_audit = {"valid": False, "reason": "zone_target_missing"}
                    else:
                        zone_audit = _zone_catalog_audit(
                            db,
                            city_id=city_id,
                            lat=float(target_place.lat),
                            lng=float(target_place.lng),
                            observed_rows=result if name == "list_zones" else None,
                        )
                zone_catalog_disposition = (
                    "blocked"
                    if zone_audit is not None and not zone_audit.get("valid")
                    else "waived"
                    if zone_audit is not None
                    and zone_audit.get("valid")
                    and not zone_audit.get("contains")
                    else None
                )
                zone_catalog_exhausted = zone_catalog_disposition is not None
                active_image_place_id = (
                    int(active_work_item.place_id)
                    if active_mission is not None
                    and active_mission.kind == "quality_images"
                    and active_work_item is not None
                    and active_work_item.place_id is not None
                    else None
                )
                image_attempt_count = (
                    image_searches_by_place.get(active_image_place_id, 0)
                    if active_image_place_id is not None
                    else 0
                )
                image_audit = (
                    _image_search_audit(
                        db,
                        run_id=agent_run.id,
                        place_id=active_image_place_id,
                    )
                    if active_image_place_id is not None and image_attempt_count >= 3
                    else {"all": [], "clean_empty": [], "provider_failure": [], "with_candidates": []}
                )
                image_terminal_disposition = None
                if len(image_audit["all"]) >= 3 and not image_audit["with_candidates"]:
                    if len(image_audit["clean_empty"]) == 3:
                        image_terminal_disposition = "source_exhausted"
                    elif image_audit["provider_failure"]:
                        # One warning/error means the three-attempt sample did
                        # not cleanly exhaust the source pool. Cool down instead
                        # of making a durable source-exhausted claim.
                        image_terminal_disposition = "blocked"
                image_budget_terminal = bool(
                    active_mission is not None
                    and active_mission.kind == "quality_images"
                    and active_work_item is not None
                    and active_work_item.place_id is not None
                    and image_attempt_count >= 3
                    and image_terminal_disposition is not None
                    and db.query(PlaceImage.id).filter(
                        PlaceImage.place_id == int(active_work_item.place_id)
                    ).first() is None
                )
                if material_change and not data_integrity_task_completed:
                    if scoped_quality_mode:
                        scoped_lane_terminal = True
                    active_work_item = reconcile_work_items(db, mission=active_mission)
                    if active_work_item is not None:
                        agent_run.work_item_id = active_work_item.id
                        continuity = mission_context(active_mission, active_work_item) if active_mission else continuity
                elif zone_catalog_exhausted and not data_integrity_task_completed:
                    scoped_lane_terminal = True
                    previous_target = active_work_item.target_key
                    zone_refs = [f"run:{agent_run.id}:step:{action_sequence}"]
                    zone_reason = (
                        "서버가 현재 장소 좌표를 도시의 최신 구역 폴리곤 전체와 대조했으며, 모든 구역 "
                        "geometry가 유효하지만 포함하는 구역이 없어 이번 구역 결손을 면제합니다."
                        if zone_catalog_disposition == "waived"
                        else (
                            "도시 구역 카탈로그에 누락·손상·퇴화 geometry가 있어 전체 불포함을 증명할 수 "
                            "없습니다. 24시간 냉각 후 카탈로그를 다시 감사합니다."
                        )
                    )
                    active_work_item = rotate_blocked_work_item(
                        db,
                        mission=active_mission,
                        current=active_work_item,
                        run_id=agent_run.id,
                        reason=zone_reason,
                        quality_disposition=zone_catalog_disposition,
                        quality_gap_kinds=["zone"],
                        quality_evidence_refs=zone_refs,
                    )
                    if active_work_item is not None:
                        agent_run.work_item_id = active_work_item.id
                        continuity["rotation"] = {
                            "from": previous_target,
                            "to": active_work_item.target_key,
                            "reason": (
                                "current zone catalogue contains no matching polygon"
                                if zone_catalog_disposition == "waived"
                                else "zone catalogue geometry invalid"
                            ),
                        }
                elif image_budget_terminal and not data_integrity_task_completed:
                    scoped_lane_terminal = True
                    previous_target = active_work_item.target_key
                    image_refs = image_audit["all"]
                    # Source exhaustion is a server-observed fact only when
                    # three exact searches cleanly returned no candidates. A
                    # provider outage is temporary and receives a cooldown.
                    terminal_disposition = image_terminal_disposition
                    terminal_reason = (
                        "정확한 장소 사진 검색 3회가 모두 정상 완료됐지만 자유 라이선스 후보가 0건이라 "
                        "현재 이미지 출처 집합을 소진한 것으로 기록합니다."
                        if terminal_disposition == "source_exhausted"
                        else (
                            "세 번의 이미지 검색 표본 중 하나 이상에서 공급자 오류·경고가 발생해 "
                            "24시간 냉각 후 재시도하도록 차단합니다."
                        )
                        if terminal_disposition == "blocked"
                        else "이미지 결과를 서버가 분류하지 못해 차단했습니다."
                    )
                    active_work_item = rotate_blocked_work_item(
                        db,
                        mission=active_mission,
                        current=active_work_item,
                        run_id=agent_run.id,
                        reason=terminal_reason,
                        quality_disposition=terminal_disposition,
                        quality_gap_kinds=["image"] if terminal_disposition else None,
                        quality_evidence_refs=image_refs,
                        quality_source_revision=QUALITY_SOURCE_REVISIONS["image"],
                    )
                    if active_work_item is not None:
                        agent_run.work_item_id = active_work_item.id
                        research_actions_since_material = 0
                        no_progress_actions = 0
                        continuity["rotation"] = {
                            "from": previous_target,
                            "to": active_work_item.target_key,
                            "reason": "exact-subject image search budget exhausted",
                        }
                elif (
                    not data_integrity_task_completed
                    and
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
                if data_integrity_task_completed:
                    # run_tool deferred its commit for this path. The task,
                    # checkpoint, and every mission cursor become terminal in
                    # this single commit or roll back together on any error.
                    if not _complete_data_integrity_mission(
                        db,
                        mission=active_mission,
                        task=primary_task,
                        run_id=agent_run.id,
                    ):
                        raise RuntimeError(
                            "data_integrity_terminal_state_alignment_failed"
                        )
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
                queue_drained = bool(queue_mode and count_unread(db, city_id) == 0)
                if queue_drained:
                    # Queue and autonomous work are different runs. Once the
                    # last user item is closed, never execute research calls
                    # that the model emitted later in the same/parallel round.
                    for skipped in tool_calls[tool_call_index + 1:]:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": skipped.id,
                            "content": json.dumps({
                                "error": "skipped_after_queue_drained",
                                "detail": (
                                    "사용자 작업 큐가 비어 같은 실행의 자율 연구 호출을 "
                                    "실행하지 않았습니다. 다음 실행이 별도로 계획합니다."
                                ),
                            }, ensure_ascii=False),
                        })
                    mission_halted = True
                    final_text = "사용자 작업 큐를 모두 처리하고 연구 전환 없이 종료했습니다."
                    break
                if (
                    integrity_policy_guard
                    or terminal_corrective_guard
                    or data_integrity_task_completed
                ):
                    # The assistant may emit parallel calls even though the
                    # first guard changes the allowed action set. Acknowledge
                    # the remaining call IDs without executing them so the next
                    # provider request has a valid tool-message sequence.
                    for skipped in tool_calls[tool_call_index + 1:]:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": skipped.id,
                            "content": json.dumps({
                                "error": "skipped_after_integrity_guard",
                                "detail": (
                                    "앞선 정책 가드 이후 허용 도구가 upsert_agent_task 하나로 "
                                    "축소되어 병렬 후속 호출을 실행하지 않았습니다."
                                ),
                            }, ensure_ascii=False),
                        })
                    if data_integrity_task_completed:
                        corrective_result_only = False
                        mission_halted = True
                        final_text = (
                            "현재 무결성 과제에 근거 기반 최종 판정을 기록하고 종료했습니다."
                        )
                    break
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
                scoped_material_instruction = (
                    "현재 미션의 광고된 도구 중 성공조건을 만족시키는 저장 행동을 선택하세요. 근거가 "
                    "부족하면 다른 대상으로 옮기지 말고 현재 task_id에 차단 원인과 재시도 조건을 기록하세요."
                    if discovery_mode or scoped_quality_mode
                    else (
                        "지금까지 확보한 근거로 제안·인사이트·구역·체인·검증 중 하나를 완료하세요. "
                        "근거가 부족하면 동일 검색을 변형해 반복하지 말고, 차단 원인과 다음 검증 방법을 "
                        "측정 가능한 upsert_agent_task로 남긴 뒤 다른 목표로 이동하세요."
                    )
                )
                messages.append({
                    "role": "user",
                    "content": (
                        f"최근 {no_material_actions}개 행동에서 실제 DB 변화가 없습니다. 조사 자체는 성과가 아닙니다. "
                        + scoped_material_instruction
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
        detail = transient_taint.safe_free_text(str(exc), purpose="run failure detail")
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
                "Detail omitted by the provider-retention boundary."
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
                        "lane": selected_lane,
                        "discovery_reserved": discovery_reserved,
                        "tool_counts": tool_counts,
                        "successful_tool_counts": successful_tool_counts,
                        "material_changes": _persistable_material_changes(
                            material_changes,
                            transient_taint,
                        ),
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
        if autonomous_mode
        else []
    )
    performance_after = _performance_snapshot(db, city_id)
    performance_delta = _performance_delta(performance_before, performance_after)
    current_score = _performance_score(performance_delta, successful_tool_counts)
    gaps = _research_gaps(performance_delta, successful_tool_counts, performance_after) if autonomous_mode else []
    gap_task_ids = _ensure_gap_tasks(db, city_id=city_id, run_id=agent_run.id, gaps=gaps) if gaps else []
    # New user input arriving during an autonomous run belongs to the next
    # queue-only run; it must not retroactively turn a focused mission partial.
    outcome_unread = unread_after if queue_mode else 0
    ok = outcome_unread == 0
    run_status = _run_outcome_status(
        unread_after=outcome_unread,
        gaps=gaps,
        material_change_count=len(material_changes),
    )
    if (discovery_mode or scoped_quality_mode) and scoped_lane_terminal and outcome_unread == 0:
        # City-wide gaps remain useful metrics, but cannot make an exact scoped
        # target look partial after it reached a server-audited terminal state.
        run_status = "completed"
    final_text = transient_taint.safe_free_text(final_text, purpose="run summary")
    summary = final_text or "에이전트 사이클 완료"
    if tool_counts:
        stats = ", ".join(f"{t}×{c}" for t, c in sorted(tool_counts.items(), key=lambda x: -x[1]))
        summary = f"{summary}\n[작업 통계] {stats}"
    if queue_mode and unread_after > 0:
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
    if autonomous_mode:
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
                "lane": selected_lane,
                "discovery_reserved": discovery_reserved,
                "tool_counts": tool_counts,
                "successful_tool_counts": successful_tool_counts,
                "material_changes": _persistable_material_changes(
                    material_changes,
                    transient_taint,
                ),
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
