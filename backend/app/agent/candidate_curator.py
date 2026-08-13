"""Turn server-grounded POI observations into reviewable proposal payloads.

The model is only allowed to translate and summarize evidence. Coordinates,
URLs, provider IDs, and branch observations are copied from server records so a
malformed or imaginative model response cannot move a place on the map.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from app.coordinate_attestation import trusted_coordinate_evidence
from app.place_identity import PlaceIdentityInput, same_place_candidate
from app.place_integrity import compare_place_identity


_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_ADDRESS_RE = re.compile(r"(?:주소|地址)\s*[:：]\s*([^\n。；;]{4,180})", re.IGNORECASE)


CURATED_CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "accepted": {"type": "boolean"},
        "reason": {"type": "string"},
        "local_name": {"type": "string"},
        "korean_name": {"type": "string"},
        "branch_name": {"type": "string"},
        "category": {
            "type": "string",
            "enum": [
                "tourist", "lodging", "restaurant", "transport", "shopping",
                "drink", "convenience", "other",
            ],
        },
        "travel_role": {
            "type": "string",
            "enum": [
                "history", "food", "market_night", "neighborhood", "nature",
                "shopping", "rest", "practical", "general",
            ],
        },
        "description": {"type": "string"},
        "evidence": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "insights": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["location", "history", "visit", "tip"],
                    },
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "year_label": {"type": "string"},
                    "source_index": {"type": "integer", "minimum": 0},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "kind", "title", "content", "year_label", "source_index", "confidence",
                ],
            },
        },
    },
    "required": [
        "accepted", "reason", "local_name", "korean_name", "branch_name",
        "category", "travel_role", "description", "evidence", "confidence", "insights",
    ],
}


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _page_address(result: Mapping[str, Any], coordinate: Mapping[str, Any]) -> str:
    direct = str(coordinate.get("address") or "").strip()
    if direct:
        return direct[:240]
    for value in (result.get("text"), result.get("title"), coordinate.get("display_name")):
        match = _ADDRESS_RE.search(str(value or ""))
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" ,，")[:240]
    return ""


def grounded_candidate_packets(
    tool_results: Iterable[Mapping[str, Any]],
    *,
    city_name: str,
    locked_candidates: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Extract coordinate-bearing observations without consulting assistant prose."""

    packets: list[dict[str, Any]] = []
    for item in tool_results:
        if str(item.get("name") or "") != "fetch_page":
            continue
        result = item.get("result")
        if not isinstance(result, Mapping) or result.get("error"):
            continue
        page_url = str(result.get("url") or (item.get("args") or {}).get("url") or "").strip()
        if not page_url.startswith(("http://", "https://")):
            continue
        page_title = str(result.get("title") or "").strip()
        excerpt = re.sub(r"\s+", " ", str(result.get("text") or "")).strip()[:5000]
        for coordinate in result.get("coordinate_candidates") or []:
            if not isinstance(coordinate, Mapping) or coordinate.get("storage_allowed") is False:
                continue
            lat, lng = _float(coordinate.get("lat")), _float(coordinate.get("lng"))
            if lat is None or lng is None:
                continue
            display_name = str(coordinate.get("display_name") or page_title).strip()
            if not display_name:
                continue
            coordinate_url = str(coordinate.get("source_url") or page_url).strip()
            # Facts and insights cite the page whose body was actually read.
            # A coordinate companion remains provenance, not an unfetched fact source.
            source_urls = [page_url]
            evidence = {
                "title": page_title or display_name,
                "display_name": display_name,
                "branch_name": str(coordinate.get("branch_name") or ""),
                "address": _page_address(result, coordinate),
                "lat": lat,
                "lng": lng,
                "source": str(coordinate.get("source") or "agent_research"),
                "source_url": coordinate_url,
                "external_id": str(coordinate.get("external_id") or ""),
                "confidence": float(coordinate.get("confidence") or 0.5),
                "storage_allowed": True,
            }
            packet = {
                "candidate_key": f"{evidence['source']}:{evidence['external_id'] or page_url}:{lat:.6f}:{lng:.6f}",
                "title": display_name,
                "address": evidence["address"],
                "lat": lat,
                "lng": lng,
                "source_urls": source_urls,
                "source_titles": [page_title or display_name],
                "source_excerpt": excerpt,
                "coordinate_evidence": evidence,
            }
            incoming = PlaceIdentityInput(
                city=city_name,
                title=display_name,
                branch_name=evidence["branch_name"],
                address=evidence["address"],
                lat=lat,
                lng=lng,
            )
            duplicate = False
            for current in packets:
                decision = same_place_candidate(
                    incoming,
                    PlaceIdentityInput(
                        city=city_name,
                        title=str(current.get("title") or ""),
                        branch_name=str(current.get("coordinate_evidence", {}).get("branch_name") or ""),
                        address=str(current.get("address") or ""),
                        lat=current.get("lat"),
                        lng=current.get("lng"),
                    ),
                )
                if decision.same:
                    current["source_urls"] = list(dict.fromkeys([
                        *current.get("source_urls", []), *source_urls,
                    ]))[:6]
                    if len(excerpt) > len(str(current.get("source_excerpt") or "")):
                        current["source_excerpt"] = excerpt
                    duplicate = True
                    break
            if not duplicate:
                packets.append(packet)

    # Only a server-attested resolved share/durable observation can bypass a
    # fresh fetch. Legacy assistant candidates may guide research, but their
    # carried coordinates are never promoted to evidence.
    for raw in locked_candidates:
        evidence = trusted_coordinate_evidence(raw)
        if evidence is None or not raw.get("source_urls"):
            continue
        lat, lng = _float(evidence.get("lat")), _float(evidence.get("lng"))
        if lat is None or lng is None:
            continue
        title = str(evidence.get("display_name") or evidence.get("title") or "").strip()
        if not title:
            continue
        candidate = PlaceIdentityInput(
            city=city_name,
            title=title,
            branch_name=str(evidence.get("branch_name") or ""),
            address=str(evidence.get("address") or ""),
            lat=lat,
            lng=lng,
        )
        if any(same_place_candidate(
            candidate,
            PlaceIdentityInput(
                city=city_name,
                title=str(item.get("title") or ""),
                address=str(item.get("address") or ""),
                lat=item.get("lat"),
                lng=item.get("lng"),
            ),
        ).same for item in packets):
            continue
        urls = [str(url) for url in raw.get("source_urls") or [] if str(url).startswith(("http://", "https://"))]
        source = str(evidence.get("source") or "agent_research")
        source_url = str(evidence.get("source_url") or (urls[0] if urls else ""))
        packets.append({
            "candidate_key": str(raw.get("key") or f"locked:{title}:{lat:.6f}:{lng:.6f}"),
            "title": title,
            "address": str(evidence.get("address") or ""),
            "lat": lat,
            "lng": lng,
            "source_urls": urls[:6],
            "source_titles": [title],
            "source_excerpt": str(raw.get("evidence") or raw.get("description") or "")[:5000],
            "coordinate_evidence": {
                "title": str(evidence.get("title") or title),
                "display_name": str(evidence.get("display_name") or title),
                "branch_name": str(evidence.get("branch_name") or ""),
                "address": str(evidence.get("address") or ""),
                "lat": lat,
                "lng": lng,
                "source": source,
                "source_url": source_url,
                "external_id": str(evidence.get("external_id") or ""),
                "confidence": float(evidence.get("confidence") or 0.7),
                "storage_allowed": True,
            },
        })
    return packets[:20]


def curate_grounded_candidate(
    client: Any,
    *,
    model: str,
    city_name: str,
    user_goal: str,
    subject: str,
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Use strict structured output for language work, never for location facts."""

    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "너는 여행 장소 후보의 한국어 편집자다. 서버가 고정한 한 장소의 원문만 읽고 JSON으로 "
                    "번역·요약한다. 좌표, URL, 주소, 지점은 새로 만들거나 다른 곳으로 바꾸지 않는다. "
                    "구체 상호가 아니거나 사용자 목표에 맞지 않거나 본문 근거가 너무 적으면 accepted=false다. "
                    "accepted=true이면 local_name은 근거에 실제 등장한 현지 상호, korean_name은 자연스러운 한국어 "
                    "음역·지점명, description/evidence/insight content는 한국어로 쓴다. 영업시간·가격·연도는 "
                    "원문에 명시된 경우에만 쓴다. insight는 최소 2개이며 source_index는 제공된 sources의 인덱스다."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "city": city_name,
                    "goal": user_goal,
                    "subject": subject,
                    "candidate": {
                        "title": packet.get("title"),
                        "address": packet.get("address"),
                        "source_titles": packet.get("source_titles"),
                        "source_excerpt": packet.get("source_excerpt"),
                        "sources": packet.get("source_urls"),
                    },
                }, ensure_ascii=False, default=str),
            },
        ],
        "temperature": 0.0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "grounded_place_curator",
                "strict": True,
                "schema": CURATED_CANDIDATE_SCHEMA,
            },
        },
    }
    if "gpt-oss" in model:
        request["extra_body"] = {"reasoning_effort": "medium"}
    response = client.chat.completions.create(**request)
    data = json.loads(response.choices[0].message.content or "{}")
    if not isinstance(data, dict) or not data.get("accepted"):
        return {"ok": False, "error": "candidate_rejected", "detail": str(data.get("reason") or "")[:300]}

    identity = compare_place_identity(
        str(data.get("local_name") or ""),
        str(packet.get("title") or ""),
        proposed_branch=str(data.get("branch_name") or ""),
        evidence_branch=str(packet.get("coordinate_evidence", {}).get("branch_name") or ""),
    )
    if not identity.ok:
        return {"ok": False, "error": identity.error, "detail": identity.details}
    if not _HANGUL_RE.search(str(data.get("korean_name") or "")):
        return {"ok": False, "error": "korean_name_required"}
    if not _HANGUL_RE.search(str(data.get("description") or "")):
        return {"ok": False, "error": "korean_description_required"}

    urls = [str(url) for url in packet.get("source_urls") or [] if str(url).startswith(("http://", "https://"))]
    insights: list[dict[str, Any]] = []
    for item in data.get("insights") or []:
        if not isinstance(item, Mapping):
            continue
        index = int(item.get("source_index") or 0)
        if not 0 <= index < len(urls):
            continue
        content = str(item.get("content") or "").strip()
        if not content or not _HANGUL_RE.search(content):
            continue
        insights.append({
            "kind": str(item.get("kind") or "tip"),
            "title": str(item.get("title") or "정보")[:200],
            "content": content[:4000],
            "year_label": str(item.get("year_label") or "")[:50],
            "source_url": urls[index],
            "source_title": str((packet.get("source_titles") or [packet.get("title")])[0] or "")[:300],
            "confidence": max(0.0, min(float(item.get("confidence") or 0.5), 1.0)),
        })
    if len(insights) < 2:
        return {"ok": False, "error": "insights_required"}

    local_name = str(data.get("local_name") or "").strip()[:120]
    korean_name = str(data.get("korean_name") or "").strip()[:120]
    description = str(data.get("description") or "").strip()[:1600]
    address = str(packet.get("address") or "").strip()
    if address and address not in description:
        description = f"{description}\n주소: {address}"[:2000]
    coordinate = dict(packet.get("coordinate_evidence") or {})
    coordinate_confidence = max(0.0, min(float(coordinate.get("confidence") or 0.5), 1.0))
    return {
        "ok": True,
        "args": {
            "title": f"{local_name} ({korean_name})",
            "description": description,
            "address": address,
            "branch_name": str(data.get("branch_name") or "")[:120] or None,
            "category": str(data.get("category") or "other"),
            "travel_role": str(data.get("travel_role") or "general"),
            "lat": float(packet["lat"]),
            "lng": float(packet["lng"]),
            "coordinate_source": str(coordinate.get("source") or "agent_research")[:50],
            "coordinate_external_id": str(coordinate.get("external_id") or "")[:200] or None,
            "coordinate_query": str(coordinate.get("display_name") or packet.get("title") or "")[:300],
            "coordinate_source_url": str(coordinate.get("source_url") or "")[:1000] or None,
            "coordinate_confidence": coordinate_confidence,
            "evidence": str(data.get("evidence") or "").strip()[:4000],
            "source_urls": urls[:8],
            "confidence": min(float(data.get("confidence") or 0.5), coordinate_confidence),
            "insights": insights[:4],
            "_coordinate_evidence": coordinate,
        },
    }


__all__ = [
    "CURATED_CANDIDATE_SCHEMA",
    "curate_grounded_candidate",
    "grounded_candidate_packets",
]
