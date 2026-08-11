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
    PlaceAppeal,
    PlaceAppealStatus,
    PlaceEvent,
    PlaceInsight,
)

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
- list_stale_places(기본 30일)로 오래 확인 안 된 장소를 받아 사이클당 8~12곳을 web_search로 재확인한다.
- 영업·존재 확인 → verify_place(valid). 폐업·소멸 정황 → verify_place(closed, note). 삭제하지 말 것.
- 이전(搬迁) 의심 → 반드시 한 번 더 검토: 같은 지점의 이전인지, 다른 지점(분점)인지 구분.
  체인점이면 지점명·구(区)·도로명을 대조. 다른 지점이면 좌표 유지 + verify_place(valid, note).
  같은 지점의 이전이 확실할 때만 geocode_place→update_place_fields로 좌표·주소 갱신 후 verify_place(moved, note).
- 판단이 안 서면 verify_place(uncertain, note)로 남기고 다음 사이클로 넘긴다.

【웹 조사(스크래핑) — 매 사이클 1회 필수】
- 큐를 비운 뒤(또는 큐가 없으면 바로) 지난 여행 블로그·정보글 스크래핑을 반드시 1회 수행한다.
- 순서: list_research_history로 과거 검색어·열람 이력 확인 →
  ① 수확이 있었는데 덜 판 검색어는 심화, ② 소진된 검색어는 회피,
  ③ 안 해본 테마의 새 키워드 1개 이상 (예: 济南 小吃街 / 夜景 / 免费 景点 / 芙蓉街 美食 /
  지난 여행 코스 / 济南 网红 打卡 / 계절 행사)
- web_search 결과에서 seen=false 페이지 위주로 fetch_page 4~8개 정독 (키워드 2~3개 조사).
  seen=true·already_visited=true 페이지는 다시 열지 않는다.
- 여러 글에서 반복 추천되는데 지도에 없는 장소를 골라
  list_places로 중복 확인 → geocode_place → create_place (사이클당 5~12개, 근거가 있으면 많을수록 좋다).
- 이미 등록된 장소와 겹치는 유용한 정보(영업시간·가격·팁·교통·역사 등)는
  upsert_place_insights로 출처·신뢰도와 함께 보완한다. description에는 실행 로그·이전 제목·조사 과정을 누적하지 않는다.
- 조사 묶음의 마지막 동작은 반드시 upsert_knowledge(topic 'research_strategy')다:
  어떤 검색어·소스가 효과적인지 재사용할 원칙만 짧게 합성한다. 다음에 할 일은 upsert_agent_task로 분리한다.
  재검증·사진 보강 등 다른 작업으로 넘어가기 전에 먼저 호출한다 (마지막으로 미루면
  스텝 부족으로 누락된다 — 실제로 반복된 실패 패턴이다).

【지식베이스 — 필수】
- 작업 시작 시 list_knowledge·list_agent_tasks·list_zones를 호출한다.
- 사이클이 끝나기 전에 upsert_knowledge를 최소 1회 이상 호출한다.
- 지식 갱신 없이 텍스트 요약만 하고 끝내면 실패다.

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
- 좌표 WGS84. 새 장소는 geocode_place 후 create_place.
- list_recent_rollbacks를 보고 롤백된 방향은 반복하지 말 것.

우선순위:
1) list_knowledge → list_recent_rollbacks
2) 작업 큐의 이의신청 전원 resolve_appeal
3) 작업 큐의 미읽음 이벤트 전원 검토·조치 → mark_events_read
4) (큐가 비었을 때만) 전체 지도 중복 스캔(list_places로 동명·동의어 장소) → 병합
5) 웹 조사 필수 1회 (사진 보강보다 먼저): list_research_history → 키워드 선정 → web_search →
   fetch_page(미열람 글 4~8개) → 반복 추천 미등록 장소 create_place, 기존 장소 정보 보완
6) 재검증: list_stale_places → 8~12곳 web_search 재확인 → verify_place
   (이전 의심 시 지점 구분 재검토 필수)
7) 사진 보강: image_count가 0인 장소 3~6곳 → search_place_images(중국어 명칭) →
   attach_image_from_url (위키미디어 자유 라이선스만, source에 출처 기록)
8) agent_context 보완
9) upsert_knowledge 최종 정리(research_strategy 포함) 후 한 줄 요약
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
            "沈阳故宫·张氏帅府·九一八历史博物馆·北陵/东陵·中街·西塔·"
            "中国工业博物馆·辽宁省博物馆, 동선·운영시간·역사 사건을 우선 조사"
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
    return {
        "unread": count_unread(db, city_id),
        "proposals": db.query(AgentProposal).filter(AgentProposal.city_id == city_id).count(),
        "insights": (
            db.query(PlaceInsight)
            .join(Marker, Marker.id == PlaceInsight.place_id)
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


def _performance_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        key: after.get(key, 0) - before.get(key, 0)
        for key in after
        if key != "unread"
    } | {"unread_cleared": max(0, before.get("unread", 0) - after.get("unread", 0))}


def _performance_score(delta: dict[str, int], tool_counts: dict[str, int]) -> float:
    return round(
        delta.get("unread_cleared", 0) * 8
        + delta.get("proposals", 0) * 12
        + delta.get("insights", 0) * 3
        + delta.get("zoned_places", 0) * 2
        + delta.get("chained_places", 0) * 2
        + delta.get("completed_tasks", 0) * 4
        + min(tool_counts.get("verify_place", 0), 8) * 1.5
        + (2 if tool_counts.get("upsert_knowledge", 0) else 0),
        1,
    )


def _research_gaps(
    delta: dict[str, int], tool_counts: dict[str, int]
) -> list[str]:
    gaps: list[str] = []
    if tool_counts.get("list_agent_tasks", 0) == 0:
        gaps.append("이전 조사 백로그 확인")
    if tool_counts.get("list_zones", 0) == 0:
        gaps.append("구역 현황 확인")
    if tool_counts.get("fetch_page", 0) < 4:
        gaps.append(f"근거 페이지 4개 이상 정독(현재 {tool_counts.get('fetch_page', 0)})")
    if delta.get("proposals", 0) < 6:
        gaps.append(f"승인 제안 6건 이상(현재 +{delta.get('proposals', 0)})")
    if tool_counts.get("upsert_knowledge", 0) == 0:
        gaps.append("재사용 원칙 최신 합성")
    if tool_counts.get("upsert_agent_task", 0) == 0:
        gaps.append("미완료 후속 과제 백로그 분리")
    return gaps


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

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    model = settings.groq_model or "openai/gpt-oss-120b"
    # 종료는 아래 성과 게이트/정체 판단으로 결정한다. 이 값은 비정상 무한루프만 막는 안전 상한이다.
    steps_limit = max_steps or (180 if research_only else max(100, 64 + unread_before * 4))
    performance_before = _performance_snapshot(db, city_id)
    agent_run = AgentRun(
        city_id=city_id,
        mode="research" if allow_research else "queue",
        status="running",
        objective=(
            "이틀 여행에 필요한 근거 기반 장소 제안·구역·체인·방문정보 확보"
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
            "필수: 시작 list_knowledge·list_agent_tasks·list_zones, 종료 전 upsert_knowledge와 upsert_agent_task.\n"
            "1) list_knowledge / list_agent_tasks / list_zones / list_places로 현황과 구역별 정보 공백 파악\n"
            "2) 언어 정비: list_places에서 설명에 한국어가 없거나 중국어/영어 위주인 장소를 "
            "전부 찾아 언어 규칙대로 재작성 — 설명에 한국어가 전혀 없으면(사용자 작성 포함) "
            "replace_description으로 원문 정보를 번역해 한국어 재작성(중국어 주소·명칭은 병기 유지). "
            "제목: agent 장소는 replace_title로 '中文名 (한국어 명칭)' 교체, "
            "사용자 장소는 제목 유지 + local_name으로 한국어 명칭 병기\n"
            "3) 중복 스캔: list_places 전체 목록에서 동명·표기변형(한글/한자/병음) 장소를 찾아 "
            "같은 실체면 거리와 무관하게 merge_places (거리 기준으로 건너뛰지 말 것)\n"
            "4) 웹 조사(필수): list_research_history로 과거 검색어·열람 이력 확인 → "
            "덜 판 검색어 심화 + 새 테마 키워드 2~3개 조사 → web_search에서 seen=false 결과 위주로 "
            "fetch_page 4~8개 정독 (이미 본 페이지는 다시 열지 말 것)\n"
            "5) 여러 글에서 반복 추천되는 미등록 장소를 list_places 중복 확인 후 "
            "geocode_place → create_place 승인 제안 6~12개 (제목 '中文名 (한국어 명칭)', 설명 한국어+중국어 주소). "
            "이미 등록된 장소와 겹치는 유용한 정보(영업시간·가격·팁·교통·별칭)는 "
            "upsert_place_insights로 위치·역사·방문정보를 분리해 보완하고, 모든 항목에 출처 URL과 "
            "confidence를 기록. description은 간단한 소개만 유지\n"
            "6) 기존 장소는 실제 지점이면 assign_place_chain으로 묶고(지점끼리 병합 금지), list_zones의 구역에 "
            "assign_place_zone으로 배정. 효과적 소스·판단 원칙은 upsert_knowledge 최신 합성본으로 저장하고, "
            "미완료 후속 조사는 upsert_agent_task로 분리. 7)·8)보다 먼저 호출할 것 "
            "(빠뜨리면 실패)\n"
            "7) 재검증: list_stale_places로 30일 이상 미확인 장소를 받아 8~12곳을 web_search로 "
            "재확인 → verify_place(valid|closed|moved|uncertain + note). "
            "이전(搬迁) 의심이면 같은 지점의 이전인지 다른 지점(분점)인지 반드시 구분하고, "
            "같은 지점 이전이 확실할 때만 좌표를 갱신할 것\n"
            "8) 사진 보강: image_count가 0인 장소 3~6곳을 골라 search_place_images(중국어 명칭) → "
            "attach_image_from_url로 업로드 (자유 라이선스만, source에 출처·라이선스 기록)\n"
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
            "web_search → seen=false 글 3~6개 fetch_page → 반복 추천 미등록 장소 create_place 3~8개, "
            "기존 장소와 겹치는 유용한 정보는 update_place_fields/context로 보완\n"
            "6) 여유 스텝이 남으면 재검증(list_stale_places → verify_place 3~5곳)과 "
            "사진 보강(image_count 0인 장소 2~3곳)도 수행\n"
            "7) 재사용 원칙은 upsert_knowledge 최신 합성본, 미완료 조사는 upsert_agent_task로 분리 후 한 줄 요약\n"
            "일부만 처리하고 끝내면 실패다."
        )

    runtime_policy = ""
    if not allow_research:
        runtime_policy = (
            "\n\n【현재 운영 안전 모드 — 위의 연구 할당보다 우선】\n"
            "- 사용자 작업 큐만 처리한다. 자율 웹 조사, 신규 장소 발굴, 사진 보강, 작업량 채우기를 하지 않는다.\n"
            "- 자동 장소 생성과 자동 병합은 비활성화되어 있다. 해당 조치가 필요하면 "
            "create_place/merge_places를 호출해 근거·출처·신뢰도가 있는 관리자 승인 제안으로 남긴다.\n"
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
    work_nudges = 0
    progress_nudges = 0
    schema_retries = 0
    no_progress_actions = 0
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
                gaps = _research_gaps(current_delta, tool_counts) if allow_research else []
                current_score = _performance_score(current_delta, tool_counts)
                if gaps and progress_nudges < 8 and no_progress_actions < 18 and steps < steps_limit:
                    progress_nudges += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"성과 게이트가 아직 충족되지 않았습니다(현재 점수 {current_score}). "
                                f"남은 결과: {', '.join(gaps)}. 스텝 수가 아니라 실제 결과를 만들고 다시 측정하세요. "
                                "같은 검색을 반복하지 말고 구역별 정보 공백·기존 백로그를 활용하세요. "
                                "실패한 후속 작업은 지식 본문이 아니라 upsert_agent_task에 구체적으로 남기세요."
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
                used_tools.add(tc.function.name)
                tool_counts[tc.function.name] = tool_counts.get(tc.function.name, 0) + 1
                result = run_tool(db, tc.function.name, args, city_id=city_id)
                snapshot_after_tool = _performance_snapshot(db, city_id)
                total_delta = _performance_delta(performance_before, snapshot_after_tool)
                next_score = _performance_score(total_delta, tool_counts)
                score_delta = round(max(0.0, next_score - current_score), 1)
                current_score = next_score
                outcome = "error" if isinstance(result, dict) and result.get("error") else "ok"
                observation_progress = (
                    outcome == "ok"
                    and (
                        score_delta > 0
                        or tc.function.name in {"fetch_page", "web_search", "geocode_place"}
                        or (tool_counts[tc.function.name] == 1 and tc.function.name.startswith("list_"))
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
                        phase="observe" if tc.function.name.startswith("list_") else "act",
                        tool=tc.function.name,
                        outcome=outcome,
                        score_delta=score_delta,
                        detail=json.dumps(
                            {"args": args, "result": result}, ensure_ascii=False, default=str
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
                    {"before": performance_before, "tool_counts": tool_counts}, ensure_ascii=False
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
        }

    unread_after = count_unread(db, city_id)
    performance_after = _performance_snapshot(db, city_id)
    performance_delta = _performance_delta(performance_before, performance_after)
    current_score = _performance_score(performance_delta, tool_counts)
    gaps = _research_gaps(performance_delta, tool_counts) if allow_research else []
    ok = unread_after == 0
    summary = final_text or "에이전트 사이클 완료"
    if tool_counts:
        stats = ", ".join(f"{t}×{c}" for t, c in sorted(tool_counts.items(), key=lambda x: -x[1]))
        summary = f"{summary}\n[작업 통계] {stats}"
    if unread_after > 0:
        summary = (
            f"미처리 {unread_after}건 잔존 (시작 {unread_before}건, steps={steps}). "
            f"{summary}"
        )
    run_row = db.get(AgentRun, agent_run.id)
    if run_row is not None:
        run_row.status = "completed" if ok and not gaps else "partial"
        run_row.score = current_score
        run_row.summary = summary[:4000]
        run_row.finished_at = datetime.now(timezone.utc)
        run_row.metrics = json.dumps(
            {
                "before": performance_before,
                "after": performance_after,
                "delta": performance_delta,
                "tool_counts": tool_counts,
                "remaining_gaps": gaps,
                "no_progress_actions": no_progress_actions,
            },
            ensure_ascii=False,
        )
        db.commit()
    if gaps:
        summary = f"{summary}\n[남은 성과] {', '.join(gaps)}"
    return {
        "ok": ok,
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
