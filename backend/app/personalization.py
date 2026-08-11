"""Travel-behavior based recommendations for one user and city.

The profile is derived from durable user actions instead of being another free-form
knowledge dump.  It deliberately avoids demographic or sensitive inferences.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import (
    Marker,
    MarkerCategory,
    MarkerShape,
    PlaceAppeal,
    PlaceFavorite,
    TravelChatMessage,
    TravelPlanItem,
)


BRANDS: dict[str, tuple[str, ...]] = {
    "헤이티": ("헤이티", "희차", "喜茶", "heytea"),
    "모어요거트": ("모어요거트", "茉酸奶", "more yogurt"),
}
CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "drink": ("음료", "카페", "커피", "차 ", "밀크티", "요거트", "헤이티", "모어요거트", "喜茶", "茉酸奶"),
    "restaurant": ("음식", "식당", "맛집", "먹", "요리", "메뉴", "간식"),
    "lodging": ("호텔", "숙소", "호스텔"),
    "shopping": ("쇼핑", "백화점", "상점", "기념품"),
    "tourist": ("관광", "명소", "박물관", "고궁", "유적", "공원"),
    "transport": ("지하철", "교통", "공항", "역 ", "동선", "접근성"),
}
ROLE_TERMS: dict[str, tuple[str, ...]] = {
    "food": ("음식", "식당", "맛집", "먹", "요리"),
    "market_night": ("시장", "야시장", "밤", "야경"),
    "neighborhood": ("동네", "골목", "거리", "구역"),
    "history": ("역사", "유적", "고궁", "박물관"),
    "nature": ("공원", "강변", "자연", "산책"),
    "shopping": ("쇼핑", "백화점", "기념품"),
    "rest": ("휴식", "카페", "음료", "커피", "차 ", "요거트"),
    "practical": ("교통", "지하철", "접근성", "예약", "결제"),
}


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    folded = f" {text.casefold()} "
    return any(term.casefold() in folded for term in terms)


def _brand_hits(text: str) -> list[str]:
    return [name for name, aliases in BRANDS.items() if _contains(text, aliases)]


def _marker_brand(marker: Marker) -> str:
    text = " ".join(
        part
        for part in (
            marker.title,
            marker.branch_name,
            marker.chain.name_local if marker.chain else "",
            marker.chain.name_ko if marker.chain else "",
            marker.chain.aliases if marker.chain else "",
        )
        if part
    )
    hits = _brand_hits(text)
    return hits[0] if hits else ""


def _distance_km(a: Marker, b: Marker) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def _add_marker_signal(
    marker: Marker,
    weight: float,
    *,
    category_scores: Counter[str],
    role_scores: Counter[str],
    zone_scores: Counter[int],
    chain_scores: Counter[int],
    brand_scores: Counter[str],
) -> None:
    category_scores[marker.category.value] += weight
    role_scores[marker.travel_role or "general"] += weight
    if marker.zone_id:
        zone_scores[marker.zone_id] += weight
    if marker.chain_id:
        chain_scores[marker.chain_id] += weight
    brand = _marker_brand(marker)
    if brand:
        brand_scores[brand] += weight


def build_user_travel_profile(db: Session, *, user_id: int, city_id: int) -> dict[str, Any]:
    markers = (
        db.query(Marker)
        .options(joinedload(Marker.zone), joinedload(Marker.chain))
        .filter(
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
            Marker.shape == MarkerShape.point,
        )
        .all()
    )
    marker_by_id = {row.id: row for row in markers}
    favorite_rows = (
        db.query(PlaceFavorite)
        .options(joinedload(PlaceFavorite.place).joinedload(Marker.zone), joinedload(PlaceFavorite.place).joinedload(Marker.chain))
        .join(Marker, Marker.id == PlaceFavorite.place_id)
        .filter(PlaceFavorite.user_id == user_id, Marker.city_id == city_id, Marker.merged_into_id.is_(None))
        .all()
    )
    created = [row for row in markers if row.user_id == user_id]
    plan_rows = (
        db.query(TravelPlanItem)
        .options(joinedload(TravelPlanItem.place).joinedload(Marker.zone), joinedload(TravelPlanItem.place).joinedload(Marker.chain))
        .filter(TravelPlanItem.user_id == user_id, TravelPlanItem.city_id == city_id)
        .all()
    )
    chat_rows = (
        db.query(TravelChatMessage)
        .filter(
            TravelChatMessage.user_id == user_id,
            TravelChatMessage.city_id == city_id,
            TravelChatMessage.role == "user",
        )
        .order_by(TravelChatMessage.id.desc())
        .limit(120)
        .all()
    )
    appeals = (
        db.query(PlaceAppeal)
        .join(Marker, Marker.id == PlaceAppeal.place_id)
        .filter(PlaceAppeal.user_id == user_id, Marker.city_id == city_id)
        .order_by(PlaceAppeal.id.desc())
        .limit(12)
        .all()
    )

    category_scores: Counter[str] = Counter()
    role_scores: Counter[str] = Counter()
    brand_scores: Counter[str] = Counter()
    zone_scores: Counter[int] = Counter()
    chain_scores: Counter[int] = Counter()
    brand_mentions: Counter[str] = Counter()

    for row in created:
        _add_marker_signal(row, 4.0, category_scores=category_scores, role_scores=role_scores, zone_scores=zone_scores, chain_scores=chain_scores, brand_scores=brand_scores)
    for favorite in favorite_rows:
        _add_marker_signal(favorite.place, 3.0, category_scores=category_scores, role_scores=role_scores, zone_scores=zone_scores, chain_scores=chain_scores, brand_scores=brand_scores)
    for item in plan_rows:
        if item.place:
            _add_marker_signal(item.place, 2.0, category_scores=category_scores, role_scores=role_scores, zone_scores=zone_scores, chain_scores=chain_scores, brand_scores=brand_scores)
    for row in chat_rows:
        text = re.sub(r"\s+", " ", row.content).strip()
        for category, terms in CATEGORY_TERMS.items():
            if _contains(text, terms):
                category_scores[category] += 1.0
        for role, terms in ROLE_TERMS.items():
            if _contains(text, terms):
                role_scores[role] += 1.0
        for brand in _brand_hits(text):
            brand_mentions[brand] += 1
            brand_scores[brand] += 2.0

    known_ids = {row.id for row in created} | {row.place_id for row in favorite_rows}
    anchors_by_id: dict[int, tuple[Marker, set[str]]] = {}
    for marker, source in [
        *((row, "직접 추가") for row in created),
        *((row.place, "즐겨찾기") for row in favorite_rows),
        *((row.place, "일정") for row in plan_rows if row.place),
    ]:
        if marker.category != MarkerCategory.lodging:
            continue
        if marker.id not in anchors_by_id:
            anchors_by_id[marker.id] = (marker, set())
        anchors_by_id[marker.id][1].add(source)
    anchors = [
        {
            "place_id": marker.id,
            "title": marker.title,
            "lat": marker.lat,
            "lng": marker.lng,
            "zone": marker.zone.title if marker.zone else "",
            "sources": sorted(sources),
        }
        for marker, sources in anchors_by_id.values()
    ]

    recommendations: list[dict[str, Any]] = []
    for marker in markers:
        if marker.id in known_ids or marker.category == MarkerCategory.lodging:
            continue
        score = 0.0
        reasons: list[str] = []
        category_score = category_scores[marker.category.value]
        if category_score:
            score += min(4.0, category_score * 0.35)
            reasons.append(f"{marker.category.value} 관심")
        if marker.travel_role and role_scores[marker.travel_role]:
            score += min(2.5, role_scores[marker.travel_role] * 0.25)
        marker_brand = _marker_brand(marker)
        if marker_brand and brand_scores[marker_brand]:
            score += min(6.0, 2.0 + brand_scores[marker_brand] * 0.35)
            reasons.insert(0, f"반복 요청한 {marker_brand}의 다른 지점")
        if marker.chain_id and chain_scores[marker.chain_id]:
            score += min(5.0, 1.5 + chain_scores[marker.chain_id] * 0.4)
            reasons.insert(0, "관심 체인의 다른 지점")
        if marker.zone_id and zone_scores[marker.zone_id]:
            score += min(2.0, zone_scores[marker.zone_id] * 0.2)
            reasons.append(f"관심 구역 {marker.zone.title if marker.zone else ''}".strip())
        nearest: tuple[float, Marker] | None = None
        for anchor, _sources in anchors_by_id.values():
            distance = _distance_km(anchor, marker)
            if nearest is None or distance < nearest[0]:
                nearest = (distance, anchor)
        distance_km: float | None = None
        if nearest and nearest[0] <= 6.0:
            distance_km = round(nearest[0], 1)
            score += max(0.4, 4.0 - nearest[0] * 0.65)
            if marker.category in (MarkerCategory.restaurant, MarkerCategory.drink):
                score += 1.2
            reasons.append(f"거점 {nearest[1].title}에서 약 {distance_km}km")
        if score < 1.0:
            continue
        recommendations.append({
            "place_id": marker.id,
            "title": marker.title,
            "category": marker.category.value,
            "travel_role": marker.travel_role or "general",
            "zone": marker.zone.title if marker.zone else "",
            "score": round(score, 2),
            "reason": " · ".join(dict.fromkeys(reasons)) or "여행 행동과 유사한 장소",
            "distance_km": distance_km,
        })
    recommendations.sort(key=lambda item: (-item["score"], item["title"]))

    signals: list[dict[str, Any]] = []
    for brand, count in brand_mentions.most_common():
        if count >= 2:
            signals.append({
                "key": f"brand:{brand}",
                "label": f"{brand} 요청 {count}회 — 음료·다른 지점 추천에 반영",
                "score": round(brand_scores[brand], 1),
                "evidence_count": count,
            })
    if anchors:
        signals.append({
            "key": "lodging_anchor",
            "label": f"{anchors[0]['title']}을 여행 거점으로 보고 주변 접근성을 반영",
            "score": 5.0,
            "evidence_count": len(anchors),
        })
    if favorite_rows or created or appeals:
        signals.append({
            "key": "activity",
            "label": f"즐겨찾기 {len(favorite_rows)} · 직접 추가 {len(created)} · 이의/교정 {len(appeals)}건 반영",
            "score": float(len(favorite_rows) * 3 + len(created) * 4),
            "evidence_count": len(favorite_rows) + len(created) + len(appeals),
        })
    if not signals and category_scores:
        top_category, top_score = category_scores.most_common(1)[0]
        signals.append({
            "key": f"category:{top_category}",
            "label": f"대화와 일정에서 {top_category} 관심이 보여 추천에 반영",
            "score": round(top_score, 1),
            "evidence_count": len(chat_rows) + len(plan_rows),
        })

    top_categories = dict(category_scores.most_common(5))
    top_roles = dict(role_scores.most_common(5))
    top_brands = dict(brand_scores.most_common(5))
    corrections = [
        {
            "place_id": appeal.place_id,
            "body": appeal.body[:300],
            "status": appeal.status.value,
            "agent_note": (appeal.agent_note or "")[:300],
        }
        for appeal in appeals
    ]
    return {
        "user_id": user_id,
        "city_id": city_id,
        "signals": signals[:6],
        "anchors": anchors[:4],
        "recommendations": recommendations[:8],
        "category_scores": top_categories,
        "role_scores": top_roles,
        "brand_scores": top_brands,
        "favorite_place_ids": sorted(row.place_id for row in favorite_rows),
        "created_place_ids": sorted(row.id for row in created),
        "direct_source_counts": dict(Counter(
            "share_import" if any(source in (row.coordinate_source or "").casefold() for source in ("amap", "gaode", "dianping")) or any(source in (row.coordinate_source_url or "").casefold() for source in ("amap", "gaode", "dianping")) else "manual"
            for row in created
        )),
        "corrections": corrections,
        "evidence": {
            "chat_requests": len(chat_rows),
            "favorites": len(favorite_rows),
            "direct_additions": len(created),
            "appeals": len(appeals),
            "plan_items": len(plan_rows),
        },
    }


def profile_prompt_context(profile: dict[str, Any]) -> str:
    """Compact, factual context suitable for an LLM prompt."""
    return json.dumps(
        {
            "inferred_from_user_actions_only": True,
            "signals": profile.get("signals", []),
            "lodging_anchors": profile.get("anchors", []),
            "category_scores": profile.get("category_scores", {}),
            "role_scores": profile.get("role_scores", {}),
            "brand_scores": profile.get("brand_scores", {}),
            "corrections_not_dislikes": profile.get("corrections", []),
            "existing_map_recommendations": profile.get("recommendations", [])[:6],
        },
        ensure_ascii=False,
    )


def city_personalization_brief(db: Session, *, city_id: int) -> str:
    user_ids: set[int] = {
        row[0] for row in db.query(TravelChatMessage.user_id).filter(TravelChatMessage.city_id == city_id).distinct().all()
    }
    user_ids.update(
        row[0] for row in db.query(Marker.user_id).filter(Marker.city_id == city_id, Marker.user_id.is_not(None)).distinct().all()
    )
    profiles = [build_user_travel_profile(db, user_id=user_id, city_id=city_id) for user_id in sorted(user_ids)]
    return json.dumps(
        [
            {
                "user_id": profile["user_id"],
                "signals": profile["signals"][:4],
                "anchors": profile["anchors"][:3],
                "brands": profile["brand_scores"],
                "categories": profile["category_scores"],
                "recommended_existing_place_ids": [item["place_id"] for item in profile["recommendations"][:5]],
            }
            for profile in profiles
        ],
        ensure_ascii=False,
    )
