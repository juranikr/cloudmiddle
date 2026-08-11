"""Agent long-term lessons / knowledge base."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import AgentKnowledge


def list_knowledge(
    db: Session,
    *,
    limit: int = 40,
    city_id: Optional[int] = None,
) -> list[AgentKnowledge]:
    query = db.query(AgentKnowledge)
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
) -> AgentKnowledge:
    raw_topic = (topic or "general").strip().lower()[:100] or "general"
    if raw_topic.startswith("cycle_") or raw_topic.startswith("cycle-"):
        raw_topic = "operations_lessons"
    topic_key = _scoped_topic(raw_topic, city_id, place_id)
    title_s = (title or topic_key)[:200]
    content_s = (content or "").strip()[:12000]
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
        )
        db.add(row)
    else:
        if merge and row.content and content_s and content_s not in row.content:
            # Keep prior lessons and append new synthesis (agent should pass merged text; this is a safety net).
            combined = (row.content.rstrip() + "\n\n---\n\n" + content_s).strip()
            row.content = combined[:12000]
        else:
            row.content = content_s or row.content
        if title_s:
            row.title = title_s
        if place_id is not None:
            row.place_id = place_id
        row.scope = scope_key
        row.city_id = city_id
        row.updated_at = now
    db.flush()
    return row


def knowledge_brief(db: Session, *, limit: int = 15, city_id: Optional[int] = None) -> list[dict]:
    rows = list_knowledge(db, limit=limit, city_id=city_id)
    return [
        {
            "topic": r.topic,
            "title": r.title,
            "content": (r.content or "")[:1200],
            "scope": r.scope,
            "city_id": r.city_id,
            "place_id": r.place_id,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
