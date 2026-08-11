"""Agent long-term lessons / knowledge base."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import AgentKnowledge, AgentKnowledgeArchive


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _derive_structure(content: str) -> tuple[str, list[str], list[str]]:
    lines = [line.strip(" -•\t") for line in (content or "").splitlines() if line.strip()]
    summary = (lines[0] if lines else "")[:800]
    principles = [line[:500] for line in lines if not any(word in line for word in ("다음", "향후", "예정"))][:6]
    next_actions = [line[:500] for line in lines if any(word in line for word in ("다음", "향후", "예정"))][:6]
    return summary, principles, next_actions


def list_knowledge(
    db: Session,
    *,
    limit: int = 40,
    city_id: Optional[int] = None,
) -> list[AgentKnowledge]:
    query = db.query(AgentKnowledge).filter(AgentKnowledge.status == "active")
    if city_id is not None:
        query = query.filter(
            or_(AgentKnowledge.scope == "global", AgentKnowledge.city_id == city_id)
        )
    return (
        query
        .order_by(AgentKnowledge.updated_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )


def _scoped_topic(topic: str, city_id: Optional[int], place_id: Optional[int]) -> str:
    raw = (topic or "general").strip().lower() or "general"
    if place_id is not None:
        return f"place:{place_id}:{raw}"[:120]
    if city_id is not None:
        return f"city:{city_id}:{raw}"[:120]
    return raw[:120]


def get_by_topic(
    db: Session,
    topic: str,
    *,
    city_id: Optional[int] = None,
    place_id: Optional[int] = None,
) -> Optional[AgentKnowledge]:
    t = _scoped_topic(topic, city_id, place_id)
    if not t:
        return None
    return db.query(AgentKnowledge).filter(AgentKnowledge.topic == t).first()


def upsert_knowledge(
    db: Session,
    *,
    topic: str,
    title: str,
    content: str,
    scope: str = "global",
    city_id: Optional[int] = None,
    place_id: Optional[int] = None,
    merge: bool = False,
    category: str = "playbook",
    summary: str = "",
    principles: Optional[list[str]] = None,
    next_actions: Optional[list[str]] = None,
    evidence_count: int = 0,
    quality_score: float = 0.7,
) -> AgentKnowledge:
    raw_topic = (topic or "general").strip().lower()[:100] or "general"
    if raw_topic.startswith("cycle_") or raw_topic.startswith("cycle-"):
        raw_topic = "operations_lessons"
    topic_key = _scoped_topic(raw_topic, city_id, place_id)
    title_s = (title or topic_key)[:200]
    content_s = (content or "").strip()[:12000]
    derived_summary, derived_principles, derived_actions = _derive_structure(content_s)
    summary_s = (summary or derived_summary)[:1000]
    principles_s = [str(item).strip()[:500] for item in (principles or derived_principles) if str(item).strip()][:10]
    next_actions_s = [str(item).strip()[:500] for item in (next_actions or derived_actions) if str(item).strip()][:10]
    scope_key = scope if scope in {"global", "city", "place"} else "global"
    if place_id is not None:
        scope_key = "place"
    elif city_id is not None:
        scope_key = "city"
    row = get_by_topic(db, raw_topic, city_id=city_id, place_id=place_id)
    now = datetime.now(timezone.utc)
    if row is None:
        row = AgentKnowledge(
            topic=topic_key,
            title=title_s,
            content=content_s,
            scope=scope_key,
            city_id=city_id,
            place_id=place_id,
            category=category[:30] or "playbook",
            summary=summary_s,
            principles=json.dumps(principles_s, ensure_ascii=False),
            next_actions=json.dumps(next_actions_s, ensure_ascii=False),
            evidence_count=max(0, evidence_count),
            quality_score=max(0.0, min(float(quality_score), 1.0)),
            status="active",
            version=1,
        )
        db.add(row)
    else:
        # 지식은 로그가 아니다. 매 실행의 최신 합성본으로 교체하고 원시 실행은 AgentRun에 남긴다.
        row.content = content_s or row.content
        if title_s:
            row.title = title_s
        if place_id is not None:
            row.place_id = place_id
        row.scope = scope_key
        row.city_id = city_id
        row.updated_at = now
        row.category = category[:30] or row.category or "playbook"
        row.summary = summary_s or row.summary
        row.principles = json.dumps(principles_s, ensure_ascii=False)
        row.next_actions = json.dumps(next_actions_s, ensure_ascii=False)
        row.evidence_count = max(0, evidence_count)
        row.quality_score = max(0.0, min(float(quality_score), 1.0))
        row.status = "active"
        row.version = int(row.version or 0) + 1
    db.flush()
    return row


def knowledge_brief(db: Session, *, limit: int = 15, city_id: Optional[int] = None) -> list[dict]:
    rows = list_knowledge(db, limit=limit, city_id=city_id)
    return [
        {
            "topic": r.topic,
            "title": r.title,
            "summary": (r.summary or r.content or "")[:800],
            "principles": _json_list(r.principles)[:6],
            "next_actions": _json_list(r.next_actions)[:6],
            "quality_score": r.quality_score,
            "scope": r.scope,
            "city_id": r.city_id,
            "place_id": r.place_id,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


def rebuild_knowledge_base(db: Session) -> dict[str, int]:
    """누적 일지를 보관소로 옮기고, 실행에 바로 쓰는 작은 플레이북으로 재구성한다."""
    rows = db.query(AgentKnowledge).all()
    for row in rows:
        db.add(
            AgentKnowledgeArchive(
                original_id=row.id,
                topic=row.topic,
                title=row.title,
                content=row.content or "",
                scope=row.scope,
                city_id=row.city_id,
                place_id=row.place_id,
                archived_reason="structured_knowledge_rebuild_v1",
            )
        )
        db.delete(row)
    db.flush()

    curated = [
        dict(
            topic="editorial_standard", title="장소 정보 편집 기준", scope="global", city_id=None,
            category="quality", quality_score=1.0,
            summary="장소 본문은 짧은 소개만 유지하고 위치·역사·방문정보·팁은 출처가 있는 구조화 항목으로 분리한다.",
            principles=["description에 실행 로그·이전 제목·조사 과정을 넣지 않는다.", "운영시간·가격은 확인일과 출처를 남긴다.", "좌표는 WGS84와 원본 좌표계·공급자를 함께 기록한다.", "사용자 원문은 보존하고 시스템 이력은 별도 테이블에 기록한다."],
            next_actions=[], content="장소 정보 품질의 공통 기준.", evidence_count=4,
        ),
        dict(
            topic="react_policy", title="성과 기반 ReAct 운영 원칙", scope="global", city_id=None,
            category="workflow", quality_score=1.0,
            summary="스텝 수를 채우지 않고 작업 큐 해소·근거 제안·정보 커버리지·백로그 완료라는 성과가 생기는 동안만 계속한다.",
            principles=["관찰→행동→결과 측정→다음 행동을 반복한다.", "연속 무성과가 누적되면 종료하고 구체적 과제를 백로그에 남긴다.", "같은 검색·URL·변경을 반복하지 않는다.", "고위험 생성·병합은 승인 제안으로 남긴다."],
            next_actions=[], content="에이전트 실행과 종료 판단의 공통 기준.", evidence_count=4,
        ),
        dict(
            topic="chain_and_zone", title="체인·구역 처리 원칙", scope="global", city_id=None,
            category="data_model", quality_score=1.0,
            summary="같은 브랜드의 서로 다른 지점은 병합하지 않고 체인으로 묶으며, 장소는 실제 관광 권역 구역에 배정한다.",
            principles=["체인 병합 금지: 지점명·주소가 다르면 별도 Marker다.", "구역은 행정구역보다 도보 동선과 관광 맥락을 우선한다.", "구역별 정보 밀도와 카테고리 균형을 조사 우선순위에 반영한다."],
            next_actions=[], content="체인과 구역을 활용하는 공통 규칙.", evidence_count=3,
        ),
        dict(
            topic="city_playbook", title="선양 조사 플레이북", scope="city", city_id=2,
            category="city", quality_score=0.95,
            summary="청 초기 수도→근대 군벌과 동북 역사→9·18→공업도시의 시간축을 중제·고궁권, 북릉권, 9·18권, 철서 공업권 동선에 연결한다.",
            principles=["沈阳故宫·张氏帅府·中街는 도보 1일 핵심권으로 묶는다.", "北陵/昭陵·九一八历史博物馆은 북부 역사권으로 묶는다.", "西塔는 음식·야간 체험, 铁西는 공업사·현대문화 축으로 조사한다.", "공식 박물관·정부·교차 출처를 우선한다."],
            next_actions=["이틀 일정에 필요한 핵심 장소를 승인 제안 8건 이상 확보", "핵심 구역 polygon과 장소 배정", "교통·운영시간·예약 팁을 구조화"],
            content="선양의 역사와 현재 동선을 함께 설명하는 도시별 조사 기준.", evidence_count=4,
        ),
        dict(
            topic="city_playbook", title="지난 조사 플레이북", scope="city", city_id=1,
            category="city", quality_score=0.9,
            summary="샘물·옛도심·시장·산지 명소를 구역과 동선으로 연결하고, 오래 누적된 장소 설명을 구조화 정보로 점진 정제한다.",
            principles=["표돌천·오룡담·대명호는 인접하지만 별개 장소다.", "파자육 상호는 지점별로 분리하고 체인으로 관리한다.", "야시장·상업시설은 최신 운영 여부를 재검증한다."],
            next_actions=["기존 장문 설명을 구조화 인사이트로 분리", "미배정 장소를 구역에 연결"],
            content="지난의 중복 오판을 방지하고 기존 데이터를 정제하는 도시별 기준.", evidence_count=3,
        ),
    ]
    for item in curated:
        upsert_knowledge(db, place_id=None, merge=False, **item)
    db.commit()
    return {"archived": len(rows), "active": len(curated)}
