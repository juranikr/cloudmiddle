"""Groq tool-calling용 도구 정의 + 실행."""

from __future__ import annotations

import copy
import json
import hashlib
import logging
import math
import re
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app import storage
from app.config import settings
from app.coordinate_attestation import issue_coordinate_attestation, trusted_coordinate_evidence
from app.db_lock import transaction_lock

from app.events import (
    diff_marker_fields,
    ensure_contributor,
    log_place_event,
    mark_events_read,
    marker_field_snapshot,
    summary_for_changes,
)
from app.geocode import parse_viewbox, search_address
from app.gcj02 import gcj02_to_wgs84
from app.knowledge import list_knowledge, upsert_knowledge
from app.place_identity import PlaceIdentityInput, same_place_candidate
from app.place_integrity import assess_new_place
from app.search_providers import (
    SearchProviderProfile,
    build_search_provider_profile,
    search_brave_places,
)
from app.messages import (
    list_open_appeals,
    mark_appeals_read,
    notify_all_users,
    notify_place_contributors,
)
from app.models import (
    AgentProposal,
    AgentSearchLog,
    AgentSearchResult,
    AgentWebVisit,
    AgentTask,
    City,
    Marker,
    MarkerCategory,
    MarkerShape,
    PlaceAppeal,
    PlaceAppealStatus,
    PlaceEvent,
    PlaceEventAction,
    PlaceImage,
    PlaceInsight,
    PlaceChain,
    UserMessageKind,
)
from app.rollback import _rollback_merge, marker_snapshot

logger = logging.getLogger(__name__)

_TRUSTED_SEARCH_HOSTS = (
    "dianping.com",
    "meituan.com",
    "ctrip.com",
    "qunar.com",
    "trip.com",
    "baidu.com",
    "amap.com",
    "360.cn",
    "openstreetmap.org",
    "wikidata.org",
    "wikipedia.org",
    "shenyang.gov.cn",
    "ln.gov.cn",
    "bendibao.com",
    "maigoo.com",
)
_BLOCKED_SEARCH_HOST_PARTS = (
    "tanhuasq.com",
    "shangspa.com",
    "zjbinshuiyuan.cn",
    "quanshanyayuan.cn",
    "sxyijia.cn",
    "nihaoad.com",
    "62652.cn",
    "tongchenganmo.cn",
    "moyedaojia.com",
    "shufuanmo.com",
    "xjspa.com",
    "024leisure.com",
    "024xyw.com",
    "qltyw.com",
    "ypljj.com",
    "fenglougg.com",
    "paperword.cn",
    "jiupinfang2.cn",
    "huangloublog.com",
)
_UNSAFE_SEARCH_TEXT_RE = re.compile(
    r"探花|黑料|成人视频|成人短剧|福利(?:视频|外流)|约炮|换妻|巨乳|少妇技师|"
    r"丝足|上门按摩|上门推拿|同城按摩|私人会所|养生网|凤楼|休闲验证|"
    r"求操|骚|情色|裸聊",
    re.IGNORECASE,
)
_IRRELEVANT_TECH_TEXT_RE = re.compile(
    r"API|Android|SCIM|Okta|Snowflake|Bedrock|WordPress|密码重置|"
    r"프로그래밍|개발자|기술 블로그|인증 라이브러리|비밀번호 재설정",
    re.IGNORECASE,
)
_QUERY_ACTION_WORDS = {
    "이전", "사용자", "요청", "현재", "후속", "지시", "찾아줘", "검색", "등록해줘",
    "추가해줘", "지도에", "주소", "推荐", "攻略", "地址", "찾아", "등록", "추가",
}

_PAGE_CHALLENGE_MARKERS = (
    "验证中心",
    "安全验证",
    "身份核实",
    "请登录",
    "扫码登录",
    "app扫码",
    "扫描二维码登录",
    "账号登录/注册",
    "二维码已失效",
    "登录失败",
    "登录后查看",
    "captcha",
    "access denied",
    "odm products",
    "mini projector",
    "android tv box",
)

_SHENYANG_DISTRICTS = {
    "shenhe": ("沈河", "shenhe"),
    "huanggu": ("皇姑", "huanggu"),
    "heping": ("和平", "heping"),
    "dadong": ("大东", "dadong"),
    "tiexi": ("铁西", "tiexi"),
    "hunnan": ("浑南", "hunnan"),
    "shenbei": ("沈北", "shenbei"),
    "yuhong": ("于洪", "yuhong"),
    "sujiatun": ("苏家屯", "sujiatun"),
    "liaozhong": ("辽中", "liaozhong"),
    "xinmin": ("新民", "xinmin"),
    "kangping": ("康平", "kangping"),
    "faku": ("法库", "faku"),
}


def _search_host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def _blocked_search_url(url: str) -> bool:
    host = _search_host(url)
    if not host or any(part in host for part in _BLOCKED_SEARCH_HOST_PARTS):
        return True
    return bool(
        re.search(r"/archives?/\d+/?$", url, re.IGNORECASE)
        and (host.endswith(".cc") or host.endswith("cloudfront.net"))
    )


def is_useful_fetched_page(result: dict[str, Any]) -> bool:
    """A login/challenge shell is not evidence even if the HTTP request succeeded."""
    title = str(result.get("title") or "").strip()
    body = str(result.get("text") or "").strip()
    if len(body) < 120:
        return False
    sample = f"{title}\n{body[:1200]}".casefold()
    return not any(marker in sample for marker in _PAGE_CHALLENGE_MARKERS)


def _normalize_evidence_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    path = urllib.parse.unquote(parsed.path).rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{path}{query}"


def _note_urls(note: str) -> set[str]:
    return {
        normalized
        for raw in re.findall(r"https?://[^\s)）\]}>]+", note)
        if (normalized := _normalize_evidence_url(raw.rstrip(".,;")))
    }


def _district_tokens(value: str) -> set[str]:
    folded = str(value or "").casefold()
    return {
        district
        for district, aliases in _SHENYANG_DISTRICTS.items()
        if any(alias.casefold() in folded for alias in aliases)
    }


def _insight_fact_tokens(value: str) -> set[str]:
    """Stable factual identifiers used to collapse title-only duplicate insights."""
    folded = str(value or "").casefold()
    return {
        re.sub(r"\s+", "", token)
        for token in re.findall(r"(?:\+?\d[\d\s-]{6,}\d)", folded)
    }


def _search_relevance_groups(
    query: str,
    profile: Optional[SearchProviderProfile] = None,
) -> list[tuple[str, ...]]:
    folded = query.casefold()
    groups: list[tuple[str, ...]] = []
    if profile and any(alias.casefold() in folded for alias in profile.city_aliases):
        groups.append(profile.city_aliases)
    if profile:
        groups.extend(
            group
            for group in profile.local_relevance_groups
            if any(term.casefold() in folded for term in group)
        )
    if any(term in folded for term in ("마사지", "按摩", "spa", "스파", "足疗", "洗浴")):
        groups.append(("마사지", "按摩", "spa", "스파", "足疗", "洗浴", "推拿"))
    if any(term in folded for term in ("식당", "맛집", "음식", "餐厅", "美食", "小吃", "饭店", "快餐")):
        groups.append(("식당", "맛집", "음식", "餐厅", "美食", "小吃", "饭店", "快餐", "restaurant"))
    if any(term in folded for term in ("호텔", "숙소", "酒店", "宾馆", "住宿")):
        groups.append(("호텔", "숙소", "酒店", "宾馆", "住宿", "hotel"))
    if any(term in folded for term in ("카페", "음료", "차", "咖啡", "奶茶", "饮品")):
        groups.append(("카페", "음료", "咖啡", "奶茶", "饮品", "tea", "cafe"))
    if any(term in folded for term in ("관광", "명소", "景点", "景区", "博物馆", "museum", "attraction")):
        groups.append(("관광", "명소", "景点", "景区", "博物馆", "museum", "attraction"))
    return groups


def _search_result_quality(
    query: str,
    item: dict[str, Any],
    profile: Optional[SearchProviderProfile] = None,
) -> float:
    """Reject unsafe/off-topic search hits before they reach the model or history."""
    url = str(item.get("href") or "")
    host = _search_host(url)
    if not host or not url.startswith(("http://", "https://")):
        return 0.0
    if _blocked_search_url(url):
        return 0.0
    text = " ".join(str(item.get(field) or "") for field in ("title", "body", "href"))
    if _UNSAFE_SEARCH_TEXT_RE.search(text):
        return 0.0
    if _IRRELEVANT_TECH_TEXT_RE.search(text) and not _IRRELEVANT_TECH_TEXT_RE.search(query):
        return 0.0
    # Disposable spam mirrors in this corpus overwhelmingly use /archives/<id>
    # on .cc or CloudFront hosts and have no stable publisher identity.
    trusted_domains = (
        (*_TRUSTED_SEARCH_HOSTS, *profile.official_domains)
        if profile
        else _TRUSTED_SEARCH_HOSTS
    )
    trusted = any(host == domain or host.endswith(f".{domain}") for domain in trusted_domains)
    if profile and not trusted:
        trusted = any(
            host == suffix.lstrip(".") or host.endswith(suffix)
            for suffix in profile.official_host_suffixes
        )
    folded_text = text.casefold()
    relevance_groups = _search_relevance_groups(query, profile)
    matched_groups = sum(any(term.casefold() in folded_text for term in group) for group in relevance_groups)

    # Python's Unicode word class keeps Japanese, Thai, Arabic, Cyrillic, etc.;
    # an explicit Chinese/Korean/Latin allow-list made other destinations look
    # unrelated even when a business name matched exactly.
    raw_tokens = re.findall(r"[^\W_]{2,}", query, flags=re.UNICODE)
    tokens = [token for token in raw_tokens if token.casefold() not in _QUERY_ACTION_WORDS]
    token_matches = sum(1 for token in tokens[:12] if token.casefold() in folded_text)
    relevance_terms = {
        term.casefold()
        for group in relevance_groups
        for term in group
    }
    entity_tokens: list[str] = []
    for token in tokens:
        entity_token = token.casefold()
        for term in sorted(relevance_terms, key=len, reverse=True):
            entity_token = entity_token.replace(term, "")
        if len(entity_token) >= 2:
            entity_tokens.append(entity_token)
    entity_matches = sum(1 for token in entity_tokens[:12] if token.casefold() in folded_text)
    exact_entity_match = any(
        len(_compact_subject(token)) >= 3 and _compact_subject(token) in _compact_subject(text)
        for token in entity_tokens[:12]
    )

    score = 0.18 + (0.38 if trusted else 0.0)
    score += min(0.28, matched_groups * 0.14)
    score += min(0.2, token_matches * 0.05)
    if exact_entity_match:
        score += 0.34
    if relevance_groups and matched_groups == 0 and entity_matches == 0:
        return 0.0
    if len(relevance_groups) >= 2 and matched_groups < 2 and entity_matches == 0:
        return 0.0
    if relevance_groups and entity_tokens and entity_matches == 0 and not exact_entity_match:
        return 0.0
    # A reputable host is not enough when an exact business query has no
    # semantic group (city/food/hotel/etc.).  For example, Trip.com's unrelated
    # Shanghai guide once survived a search for a named Shenyang restaurant
    # solely because trip.com is trusted.  Exact queries must still share at
    # least one meaningful token with the result.
    if not relevance_groups and token_matches == 0:
        return 0.0
    return round(min(score, 1.0), 3)


def _filter_search_results(
    query: str,
    results: list[dict[str, Any]],
    *,
    limit: int,
    profile: Optional[SearchProviderProfile] = None,
) -> tuple[list[dict[str, Any]], int]:
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for index, item in enumerate(results):
        quality = _search_result_quality(query, item, profile)
        if quality < 0.55:
            continue
        enriched = dict(item)
        enriched["quality"] = quality
        ranked.append((quality, index, enriched))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    kept = [item for _quality, _index, item in ranked[:limit]]
    return kept, max(0, len(results) - len(kept))

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_unread_events",
            "description": (
                "아직 처리하지 않은 장소 이력(생성·수정·병합·이의 기록 등)을 오래된 순으로 가져온다. "
                "작업 큐의 핵심. 반환된 id를 빠짐없이 검토·조치한 뒤 mark_events_read 할 것."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 30},
                    "query": {"type": "string", "description": "현재 작업·대상·실패 상황을 설명하는 검색 문장"},
                    "place_id": {"type": ["integer", "null"], "description": "특정 장소 지식이 필요할 때"},
                    "categories": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_events_read",
            "description": (
                "검토·조치를 끝낸 이벤트 ID만 읽음 처리한다. "
                "아직 안 본 ID를 한꺼번에 표시하지 말 것. 잔여 미읽음이 0이 될 때까지 반복."
            ),
            "parameters": {
                "type": "object",
                "properties": {"event_ids": {"type": "array", "items": {"type": "integer"}}},
                "required": ["event_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_places",
            "description": (
                "활성 장소(병합되지 않은 것)를 조건으로 조회한다. "
                "병합 후보를 찾을 때 미읽음/이의 대상뿐 아니라 수정되지 않은 기존 장소까지 "
                "이 툴로 전체 지도에서 쿼리한다. "
                "q·category·near_lat/near_lng/radius_m를 조합해 필요한 범위만 가져올 것. "
                "동명 중복 탐색은 q(이름 부분일치)로, 한글/한자 표기가 다르면 "
                "전체 목록을 가져와 직접 비교한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": "제목·설명·agent_context 부분 일치 검색(한국어/중국어/영문)",
                    },
                    "category": {
                        "type": "string",
                        "description": "tourist|lodging|restaurant|transport|shopping|drink|convenience|other",
                    },
                    "near_lat": {"type": "number", "description": "이 좌표 근처만 (WGS84)"},
                    "near_lng": {"type": "number", "description": "이 좌표 근처만 (WGS84)"},
                    "radius_m": {
                        "type": "number",
                        "default": 150,
                        "description": "near_lat/lng와 함께 쓸 반경(미터). 기본 150",
                    },
                    "exclude_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "결과에서 제외할 place_id (자기 자신 등)",
                    },
                    "limit": {"type": "integer", "default": 80},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_place",
            "description": "장소 상세 + 이미지 + 최근 이벤트.",
            "parameters": {
                "type": "object",
                "properties": {"place_id": {"type": "integer"}},
                "required": ["place_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_nearby_candidates",
            "description": (
                "기준 place_id 주변의 활성 장소를 거리순으로 반환한다. "
                "수정/이의가 없는 기존 핀도 모두 포함된다. "
                "주의: 가깝다는 것은 병합 근거가 아니다 — 인접한 별개 명소(趵突泉/五龙潭 등)와 "
                "다른 가게·지점이 흔하다. 산·공원 등 넓은 명소의 동일 실체 확인에만 "
                "radius_m 1000~5000을 활용하고, 병합은 명칭·웹 근거로 같은 실체일 때만."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer", "description": "미읽음/이의로 주목 중인 기준 장소"},
                    "radius_m": {
                        "type": "number",
                        "default": 120,
                        "description": "미터. 넓은 명소는 1000~5000 권장. 고정 기준 아님",
                    },
                },
                "required": ["place_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_places",
            "description": (
                "source_place_id를 target_place_id로 병합한다. 설명/기여자/이미지를 합친다. "
                "'같은 실체'가 확실할 때만 사용 — 확신이 없으면 병합하지 않는 것이 기본값. "
                "금지: 각자 고유 명칭의 인접 명소(예: 趵突泉과 五龙潭은 이웃한 별개 공원), "
                "같은 상호·같은 음식의 다른 가게/지점(예: 把子肉 파자육집들). "
                "식당은 지점명·주소가 완전히 일치할 때만 동일 실체다. "
                "사용자가 '다른 장소'라고 이의한 조합은 명백한 반증 없이 병합 금지. "
                "정보가 풍부한 쪽을 target으로. 잘못 병합했으면 undo_merge로 되돌릴 것."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_place_id": {"type": "integer"},
                    "source_place_id": {"type": "integer"},
                    "reason": {"type": "string", "description": "동일 실체 근거 (웹 출처 포함)"},
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "동일 실체를 확인한 실제 URL",
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["target_place_id", "source_place_id", "reason", "source_urls", "confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "undo_merge",
            "description": (
                "잘못된 병합을 되돌려 source 장소를 별개 핀으로 복구한다 "
                "(제목·설명·이미지 원복). 사용자 이의가 '다른 장소/다른 지점'이라고 주장하고 "
                "동일 실체라는 명백한 반증을 제시할 수 없으면 즉시 호출할 것. "
                "reason에 분리 근거를 한국어로 기록."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_place_id": {
                        "type": "integer",
                        "description": "병합으로 사라진(merged) 쪽 장소 ID",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["source_place_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_place_context",
            "description": "장소의 현재 내부 판단 요약(agent_context)을 교체한다. 실행 로그를 누적하지 말고 최신 결론만 1,500자 이내로 유지한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "context": {"type": "string"},
                },
                "required": ["place_id", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_place_fields",
            "description": (
                "장소의 짧은 소개와 명칭을 정제한다. 운영 로그·이전 제목·조사 과정은 description에 넣지 않는다. "
                "먼저 list_places/get_place에서 읽은 현재 제목을 expected_title에 그대로 넣어 대상 ID를 재확인한다. "
                "언어 규칙: 설명 본문은 무조건 한국어(중국어 정보는 번역), "
                "명칭은 중국어+한국어 병기('中文名 (한국어 명칭)'), 주소는 중국어 원문. "
                "방문 팁·운영시간·역사·위치는 append_note가 아니라 upsert_place_insights로 출처와 함께 저장한다. "
                "설명이 중국어/영어 위주로 잘못 작성된 장소(주로 agent 추가분)는 "
                "replace_description으로 한국어 본문으로 전면 재작성 "
                "(주소는 '주소: [중국어 원문]' 형태로 유지)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "expected_title": {
                        "type": "string",
                        "description": "방금 읽은 대상 장소의 현재 제목. ID-장소 불일치 방지용",
                    },
                    "append_note": {"type": "string", "description": "사용 중단. 구조화 정보는 upsert_place_insights 사용"},
                    "local_name": {"type": "string", "description": "현지(중국어 등) 공식 명칭·주소 병기"},
                    "replace_title": {
                        "type": "string",
                        "description": "제목 교체. 반드시 '中文名 (한국어 명칭)' 병기 형식",
                    },
                    "replace_description": {
                        "type": "string",
                        "description": (
                            "설명 전면 재작성(한국어 본문 필수). agent 추가 장소이거나 "
                            "기존 설명에 한국어가 전혀 없을 때만 허용. 기존 유용 정보는 번역해 포함"
                        ),
                    },
                    "category": {"type": "string"},
                    "travel_role": {
                        "type": "string",
                        "enum": ["history", "food", "market_night", "neighborhood", "nature", "shopping", "rest", "practical", "general"],
                        "description": "이틀 여행에서 이 장소가 채우는 역할. 박물관은 history, 식당은 food처럼 실제 목적 기준",
                    },
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "replace_description의 대상 장소를 확인한, 이번 실행에서 실제로 읽은 URL",
                    },
                },
                "required": ["place_id", "expected_title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_place",
            "description": (
                "웹 조사·지오코딩 후 현재 실행 도시의 지도에 없는 유용한 장소를 추천 추가한다. "
                "매 사이클 소수를 적극 등록. "
                "언어 규칙(위반 시 거부됨): title은 '中文名 (한국어 명칭)' 형식으로 "
                "중국어+한국어 병기 (예: '泉城广场 (취안청 광장)'). "
                "description 본문은 무조건 한국어로 작성하고, 지도 검색용 주소는 "
                "'주소: [중국어 원문]' 형태로 포함할 것."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "'中文名 (한국어 명칭)' 병기 필수"},
                    "description": {"type": ["string", "null"], "description": "한국어 본문 + 중국어 주소 포함"},
                    "category": {"type": ["string", "null"]},
                    "lat": {"type": "number"},
                    "lng": {"type": "number"},
                    "context": {"type": ["string", "null"]},
                    "coordinate_source": {"type": ["string", "null"]},
                    "coordinate_external_id": {"type": ["string", "null"]},
                    "coordinate_query": {"type": ["string", "null"]},
                    "coordinate_source_url": {"type": ["string", "null"]},
                    "coordinate_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    "zone_id": {"type": ["integer", "null"], "description": "list_zones에서 확인한 소속 관광 구역"},
                    "chain_name_local": {"type": ["string", "null"], "description": "체인점이면 브랜드 현지명"},
                    "chain_name_ko": {"type": ["string", "null"]},
                    "branch_name": {"type": ["string", "null"], "description": "체인점의 실제 지점명"},
                    "travel_role": {
                        "type": ["string", "null"],
                        "enum": ["history", "food", "market_night", "neighborhood", "nature", "shopping", "rest", "practical", "general"],
                        "description": "여행 경험에서 맡는 역할. history만 반복하지 말고 현재 도시의 부족 역할을 우선",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "왜 이 장소를 추가해야 하는지와 교차 확인한 근거를 한국어로 요약",
                    },
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "판단에 사용한 실제 출처 URL. 최소 1개",
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "consumption_mode": {
                        "type": ["string", "null"],
                        "enum": ["snack", "dessert", "drink", "packaged", "full_meal", "unknown", None],
                        "description": "먹는 방식. 간식 요청에서는 full_meal을 제안하지 않는다.",
                    },
                    "insights": {
                        "type": "array",
                        "minItems": 2,
                        "description": "최소 위치 맥락 1개와 역사/방문정보 1개를 구조화",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["location", "history", "visit", "tip"]},
                                "title": {"type": "string"},
                                "content": {"type": "string"},
                                "year_label": {"type": ["string", "null"]},
                                "source_url": {"type": "string"},
                                "source_title": {"type": ["string", "null"]},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["kind", "title", "content", "source_url"],
                        },
                    },
                },
                "required": ["title", "lat", "lng", "source_urls", "insights"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_place_insights",
            "description": (
                "장소의 현재 위치 의미·역사 사건·방문 정보·현지 팁을 출처와 신뢰도와 함께 구조화한다. "
                "본문 description에 뒤섞지 말고 이 툴로 저장한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "insights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["location", "history", "visit", "tip"]},
                                "title": {"type": "string"},
                                "content": {"type": "string"},
                                "year_label": {"type": "string"},
                                "source_url": {"type": "string"},
                                "source_title": {"type": "string"},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["kind", "title", "content", "source_url", "confidence"],
                        },
                    },
                },
                "required": ["place_id", "insights"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reorder_images",
            "description": "장소 이미지 표시 순서와 그룹 키를 조정한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "ordered_ids": {"type": "array", "items": {"type": "integer"}},
                    "group_keys": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "image_id(string) -> group_key",
                    },
                },
                "required": ["place_id", "ordered_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_open_appeals",
            "description": (
                "미종결(open) 이의신청 목록. 각 id마다 resolve_appeal로 반드시 종결해야 한다. "
                "읽음 표시만으로 넘기면 안 된다."
            ),
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_appeal",
            "description": (
                "이의신청을 검토한 뒤 반영(resolved)/기각(dismissed)하고 신청자에게 결과 메시지를 보낸다. "
                "open 이의를 끝내는 유일한 정상 경로. "
                "사용자가 '다른 장소/다른 지점'이라고 주장하면 기본은 수용(resolved) — "
                "잘못된 병합은 undo_merge로 분리한 뒤 호출한다. "
                "기각(dismissed)은 동일 실체라는 명백한 웹 근거를 agent_note에 "
                "제시할 수 있을 때만 허용된다. 거리·이름 유사만으로 기각 금지."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appeal_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["resolved", "dismissed"]},
                    "agent_note": {"type": "string", "description": "한국어로 조치 설명"},
                },
                "required": ["appeal_id", "status", "agent_note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_appeals_read",
            "description": (
                "이미 resolve_appeal로 종결된 이의만 읽음 처리할 때 사용. "
                "status=open 이의 ID는 거부된다 — 반드시 resolve_appeal을 먼저 호출."
            ),
            "parameters": {
                "type": "object",
                "properties": {"appeal_ids": {"type": "array", "items": {"type": "integer"}}},
                "required": ["appeal_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_rollbacks",
            "description": (
                "관리자가 에이전트 조치를 롤백한 미읽음 이력. "
                "같은 실수를 반복하지 않도록 반드시 확인한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "list_knowledge",
            "description": "【필수·시작】에이전트 장기 지식/교훈. 다른 툴보다 먼저 호출한다.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_knowledge",
            "description": (
                "【필수】재사용할 원칙만 주제별 최신 합성본으로 저장한다. 실행 일지·검색 목록·다음에 할 일은 "
                "content에 누적하지 말고 AgentRun과 upsert_agent_task로 분리한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "영문/숫자 slug, 예: appeal_lessons, jinan_food"},
                    "title": {"type": "string"},
                    "content": {"type": "string", "description": "한국어로 정리된 짧은 최신 합성본"},
                    "category": {"type": "string", "description": "quality|workflow|city|source|data_model"},
                    "summary": {"type": "string", "description": "핵심 결론 1~3문장"},
                    "principles": {"type": "array", "items": {"type": "string"}, "description": "다음 실행이 재사용할 원칙"},
                    "next_actions": {"type": "array", "items": {"type": "string"}, "description": "표시용 요약. 실제 과제는 upsert_agent_task에도 저장"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "검색에 쓸 대상·상황·출처 키워드"},
                    "applicability": {"type": "object", "description": "적용 조건: task_kinds, stages, domains, trigger 등"},
                    "source_refs": {"type": "array", "items": {"type": "string"}, "description": "근거 URL 또는 run:/evidence: 참조"},
                    "evidence_count": {"type": "integer"},
                    "quality_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "scope": {
                        "type": "string",
                        "enum": ["global", "city", "place"],
                        "description": "공통 지식은 global, 지역 지식은 city, 장소 지식은 place",
                    },
                    "city_id": {
                        "type": ["integer", "null"],
                        "description": "도시 지식일 때 도시 ID",
                    },
                    "place_id": {
                        "type": ["integer", "null"],
                        "description": "특정 장소와 연관된 지식일 때만. 없으면 생략하거나 null",
                    },
                    "merge": {"type": "boolean", "default": True},
                },
                "required": ["topic", "title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "geocode_place",
            "description": "지정 도시 안에서 DB·OSM·Wikidata 다중 주소/장소 검색. create_place 전에 사용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "다중 웹 검색(Yahoo·Yandex, 설정 시 Brave 구조화 장소 검색). "
                "place_candidates는 주소·좌표가 있는 발견 후보이며 storage_allowed=false이면 "
                "이름·주소를 다른 공개 페이지와 geocode_place로 교차 검증한 뒤에만 저장한다. "
                "각 웹 결과에 seen(이미 열람한 페이지 여부)이 붙고, "
                "검색어·시각은 자동으로 이력에 기록된다. "
                "past_searches로 같은 검색어의 과거 조사 횟수를 알려주니, "
                "이미 여러 번 조사한 검색어보다 새 키워드를 우선할 것. "
                "seen=false 결과를 골라 fetch_page로 본문을 읽는다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "웹 페이지 본문을 읽는다(블로그·여행기·정보글 스크래핑). "
                "열람한 URL은 자동 기록되어 다음부터 web_search 결과에 seen=true로 표시된다. "
                "already_visited=true로 돌아오면 과거에 이미 읽은 페이지이므로 "
                "다른 새 페이지를 고를 것. 자주 언급되는 미등록 장소를 찾으면 "
                "list_places로 중복 확인 → geocode_place → create_place. "
                "이미 등록된 장소에 대한 유용한 정보(영업시간·가격·팁·교통·별칭)가 나오면 "
                "upsert_place_insights로 출처·신뢰도와 함께 보완할 것."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_research_history",
            "description": (
                "과거 웹 조사 이력을 반환한다: 검색어별 조사 횟수·최근 시각·새 콘텐츠 수확량, "
                "최근 열람 페이지 목록. 웹 조사를 시작하기 전에 반드시 호출해서 "
                "① 수확이 있었는데 덜 판 검색어는 심화하고 ② 이미 소진된 검색어는 피하고 "
                "③ 안 해본 테마(먹거리·야경·시장·무료 명소·계절 행사 등)의 새 키워드를 고른다."
            ),
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_place_images",
            "description": (
                "위키미디어 커먼즈에서 자유 라이선스 장소 사진을 검색한다. "
                "image_count가 0인 장소의 사진 보강용. "
                "중국어 명칭(예: 千佛山, 大明湖)으로 검색하면 결과가 좋다. "
                "결과의 image_url을 attach_image_from_url에 넘겨 업로드한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "장소 명칭 (중국어 권장)"},
                    "place_id": {"type": "integer", "description": "등록 장소 ID. 명칭 변형과 좌표 주변 사진을 함께 검색"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attach_image_from_url",
            "description": (
                "이미지 URL을 다운로드해 해당 장소의 사진으로 업로드한다(S3). "
                "search_place_images 결과의 image_url을 수정·축약하지 말고 그대로 사용할 것. "
                "source에는 결과의 page_url과 라이선스를 정확히 기록한다. 서버가 Wikimedia page_url에서 "
                "실제 다운로드 URL을 다시 해석한다. 장소당 1~2장이면 충분."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "image_url": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": "출처·라이선스 메모 (예: Wikimedia Commons, CC BY-SA 4.0, page_url)",
                    },
                },
                "required": ["place_id", "image_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stale_places",
            "description": (
                "추가/수정/재검증된 지 오래된(기본 30일 이상) 활성 장소를 오래된 순으로 반환한다. "
                "이 장소들은 폐업·이전 가능성이 있으므로 web_search로 유효성을 재확인하고 "
                "결과를 verify_place로 기록할 것. 사이클당 3~5곳이면 충분하다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30, "description": "이 일수 이상 미확인 장소만"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_place",
            "description": (
                "장소 재검증 결과를 기록한다. valid/closed/moved는 last_verified_at을 갱신하고, "
                "uncertain은 확인 실패 상태로 남겨 다음 조사에서 다시 선택되게 한다. "
                "status: valid(영업·존재 확인) | closed(폐업·소멸 추정) | moved(같은 지점의 이전 확인) "
                "| uncertain(판단 불가). "
                "주의 — moved로 판정하기 전에 반드시 한 번 더 검토: 웹에서 찾은 다른 주소가 "
                "'같은 지점의 이전(搬迁)'인지 '다른 지점(분점)'인지 구분할 것. "
                "체인점이면 지점명·구(区)·도로명을 대조하고, 다른 지점이면 moved가 아니라 "
                "valid + note로 기록하고 좌표를 옮기지 말 것. "
                "같은 지점의 이전이 확실할 때만 geocode_place→update_place_fields로 좌표를 "
                "갱신한 뒤 moved로 기록한다. closed는 삭제하지 말고 기록만 남긴다. "
                "valid/closed/moved는 이번 실행에서 fetch_page로 유효한 본문을 읽은 URL을 note에 반드시 "
                "포함해야 하며, 지점의 구(区)가 다르면 거부된다. 판단 불가면 uncertain을 사용한다. "
                "closed/moved/uncertain의 note는 agent_context에 자동 병합된다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["valid", "closed", "moved", "uncertain"],
                    },
                    "note": {
                        "type": "string",
                        "description": "판단 근거·출처. closed/moved/uncertain은 필수 권장",
                    },
                },
                "required": ["place_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_zones",
            "description": "현재 도시의 구역과 소속 장소 수를 조회한다. 조사를 구역별로 분산하고 동선을 구성할 때 먼저 사용한다.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_place_zone",
            "description": "장소를 같은 도시의 polygon 구역에 배정하거나 zone_id를 생략해 해제한다. 실제 동네·관광 권역 근거가 있을 때 사용한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "zone_id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["place_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_place_chain",
            "description": "체인 본체를 찾거나 만들고 실제 지점을 연결한다. 같은 브랜드의 다른 지점을 병합하지 말고 이 도구로 묶는다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "chain_name_local": {"type": "string"},
                    "chain_name_ko": {"type": "string"},
                    "branch_name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["place_id", "chain_name_local", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_agent_tasks",
            "description": "이전 실행이 남긴 미완료 조사·정제 과제를 우선순위순으로 조회한다.",
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 12}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_agent_task",
            "description": "다음 실행이 이어받을 구체적 과제를 만들거나 기존 과제를 완료한다. 예고 문장을 지식베이스에 넣지 말고 백로그로 분리한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    # Groq commonly emits explicit null for optional fields.
                    # The handler already treats null as "create a new task";
                    # keep the tool schema aligned so validation does not abort
                    # an otherwise resumable batch before the call reaches us.
                    "task_id": {"type": ["integer", "null"]},
                    "kind": {"type": "string"},
                    "title": {"type": "string"},
                    "detail": {"type": "string", "description": "다음 실행이 바로 행동할 수 있는 대상·근거·차단 원인"},
                    "success_metric": {"type": "string", "description": "완료 여부를 DB 수치나 검증 결과로 판정할 수 있는 기준"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 100, "description": "100이 가장 높은 우선순위, 1이 가장 낮음"},
                    "status": {"type": "string", "enum": ["pending", "completed", "blocked"]},
                    "result": {"type": "string"},
                },
                "required": ["title", "status"],
            },
        },
    },
]

# The model repeatedly interpreted ``create_place`` as an unsafe direct write even
# though the handler creates an admin proposal when auto-create is disabled.  Keep
# the internal/approval action name for compatibility, but expose an unambiguous
# research-facing alias so a successful research cycle produces reviewable data.
_create_place_tool = next(
    tool for tool in TOOLS if tool.get("function", {}).get("name") == "create_place"
)
_propose_place_tool = copy.deepcopy(_create_place_tool)
_propose_place_tool["function"]["name"] = "propose_place"
_propose_place_tool["function"]["description"] = (
    "조사한 신규 장소를 관리자 승인 대기 제안으로 저장한다. 지도에 즉시 생성하지 않는다. "
    "장소 후보를 upsert_agent_task에 적는 것은 성과가 아니며, 근거·좌표·구조화 정보가 "
    "준비되면 반드시 이 도구를 호출한다. "
    + _propose_place_tool["function"]["description"]
)
TOOLS.append(_propose_place_tool)


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _zone_contains(zone: Marker, *, lat: float, lng: float) -> bool:
    try:
        points = json.loads(zone.polygon or "[]")
        vertices = [(float(item["lat"]), float(item["lng"])) for item in points]
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False
    if len(vertices) < 3:
        return False
    inside = False
    j = len(vertices) - 1
    for i, (yi, xi) in enumerate(vertices):
        yj, xj = vertices[j]
        crosses = (yi > lat) != (yj > lat)
        if crosses and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _containing_zone(db: Session, *, city_id: int, lat: float, lng: float) -> Optional[Marker]:
    """Return the smallest active polygon zone containing a WGS84 point."""
    matches: list[tuple[float, Marker]] = []
    zones = db.query(Marker).filter(
        Marker.city_id == city_id,
        Marker.shape == MarkerShape.polygon,
        Marker.merged_into_id.is_(None),
    ).all()
    for zone in zones:
        if _zone_contains(zone, lat=lat, lng=lng):
            points = json.loads(zone.polygon or "[]")
            vertices = [(float(item["lat"]), float(item["lng"])) for item in points]
            lats = [item[0] for item in vertices]
            lngs = [item[1] for item in vertices]
            matches.append(((max(lats) - min(lats)) * (max(lngs) - min(lngs)), zone))
    return min(matches, key=lambda item: item[0])[1] if matches else None


def _place_brief(m: Marker) -> dict[str, Any]:
    quality_gaps: list[str] = []
    if m.shape == MarkerShape.point and m.merged_into_id is None:
        if not m.images:
            quality_gaps.append("image")
        if m.zone_id is None:
            quality_gaps.append("zone")
        if m.last_verified_at is None:
            quality_gaps.append("verification")
        if len((m.description or "").strip()) < 60:
            quality_gaps.append("description")
        if len(m.insights or []) < 2:
            quality_gaps.append("insights")
    return {
        "id": m.id,
        "city_id": m.city_id,
        "title": m.title,
        "category": m.category.value if m.category else None,
        "travel_role": m.travel_role or "general",
        "shape": m.shape.value if m.shape else None,
        "lat": m.lat,
        "lng": m.lng,
        "description": (m.description or "")[:400],
        "agent_context": (m.agent_context or "")[:800],
        "merged_into_id": m.merged_into_id,
        "is_agent_suggested": m.is_agent_suggested,
        "image_count": len(m.images or []),
        "insight_count": len(m.insights or []),
        "last_verified_at": m.last_verified_at.isoformat() if m.last_verified_at else None,
        "quality_gaps": quality_gaps,
        "coordinate_source": m.coordinate_source or "manual",
        "coordinate_confidence": m.coordinate_confidence,
        "coordinate_crs": m.coordinate_crs or "WGS84",
        "zone_id": m.zone_id,
        "zone_title": m.zone.title if m.zone else "",
        "chain_id": m.chain_id,
        "chain_name": m.chain.name_local if m.chain else "",
        "branch_name": m.branch_name or "",
    }


def _candidate_title_key(value: str) -> str:
    """Stable local-name key used only for pending-proposal/task reconciliation."""
    local_name = value.split("(", 1)[0].strip()
    # Search/article titles often wrap the real business in marketing copy such
    # as "必吃！鸣记脆皮烤鱼，香辣酸甜一网打尽".  Identity must be the shop,
    # otherwise the same candidate can pass through chat-card and proposal paths
    # as two different places.
    if "！" in local_name or "!" in local_name:
        local_name = re.split(r"[！!]", local_name)[-1]
    local_name = re.split(r"[，,｜|]", local_name, maxsplit=1)[0]
    local_name = re.sub(r"^(?:必吃|推荐|探店|打卡)[:：\s]*", "", local_name).strip()
    return re.sub(r"[^\w]+", "", local_name, flags=re.UNICODE).casefold()


def _matching_existing_place(
    db: Session,
    *,
    city_id: int,
    title: str,
    lat: float,
    lng: float,
    chain_name: str = "",
    branch_name: str = "",
    address: str = "",
    max_distance_m: float = 800.0,
) -> Optional[Marker]:
    city = db.get(City, city_id)
    if city is None or len(_candidate_title_key(title)) < 4:
        return None
    incoming = PlaceIdentityInput(
        city=city.name_local or city.slug,
        title=title,
        chain_name=chain_name,
        branch_name=branch_name,
        address=address,
        lat=lat,
        lng=lng,
    )
    rows = db.query(Marker).filter(
        Marker.city_id == city_id,
        Marker.shape == MarkerShape.point,
        Marker.merged_into_id.is_(None),
    ).all()
    for row in rows:
        row_chain = row.chain.name_local if row.chain is not None else ""
        decision = same_place_candidate(
            incoming,
            PlaceIdentityInput(
                city=city.name_local or city.slug,
                title=row.title,
                chain_name=row_chain,
                branch_name=row.branch_name or "",
                address=row.description or "",
                lat=row.lat,
                lng=row.lng,
            ),
        )
        if decision.same and (decision.distance_m is None or decision.distance_m <= max_distance_m):
            return row
    return None


def _complete_matching_proposal_tasks(
    db: Session, *, city_id: int, proposal_title: str, proposal_id: int
) -> int:
    key = _candidate_title_key(proposal_title)
    if not key:
        return 0
    matched = 0
    rows = db.query(AgentTask).filter(
        AgentTask.city_id == city_id,
        AgentTask.status == "pending",
    ).all()
    for row in rows:
        if key not in _candidate_title_key(row.title):
            continue
        row.status = "completed"
        row.result = f"관리자 승인 대기 제안 #{proposal_id}로 전환"
        row.completed_at = datetime.now(timezone.utc)
        matched += 1
    return matched


def reconcile_proposal_tasks(db: Session, *, city_id: int) -> int:
    """Self-heal legacy candidate tasks once an equivalent proposal exists."""
    matched = 0
    normalized = 0
    legacy_tasks = db.query(AgentTask).filter(
        AgentTask.city_id == city_id,
        AgentTask.status == "pending",
    ).all()
    for task in legacy_tasks:
        if task.title.startswith("승인 제안:"):
            task.title = task.title.replace("승인 제안:", "후보 검증:", 1)
            task.kind = "candidate_research"
            normalized += 1
    proposals = db.query(AgentProposal).filter(
        AgentProposal.city_id == city_id,
        AgentProposal.action == "create_place",
        AgentProposal.status.in_(["pending", "approved"]),
    ).all()
    for proposal in proposals:
        matched += _complete_matching_proposal_tasks(
            db,
            city_id=city_id,
            proposal_title=proposal.title,
            proposal_id=proposal.id,
        )
    if matched or normalized:
        db.commit()
    return matched


def _pending_proposal(
    db: Session,
    *,
    city_id: int,
    action: str,
    title: str,
    payload: dict[str, Any],
    evidence: str,
    source_urls: list[str],
    confidence: float,
    place_id: Optional[int] = None,
) -> dict[str, Any]:
    # Different payload wording can still describe the same branch, so lock the
    # whole city/action identity check rather than only the final payload hash.
    transaction_lock(db, f"agent-proposal:{city_id}:{action}")
    try:
        existing_place = _matching_existing_place(
            db,
            city_id=city_id,
            title=title,
            lat=float(payload["lat"]),
            lng=float(payload["lng"]),
            chain_name=str(payload.get("chain_name_local") or ""),
            branch_name=str(payload.get("branch_name") or ""),
            address=str(payload.get("address") or payload.get("description") or ""),
        )
    except (KeyError, TypeError, ValueError):
        existing_place = None
    if existing_place is not None:
        return {
            "ok": True,
            "proposal_created": False,
            "proposal_id": None,
            "duplicate": True,
            "existing_place_id": existing_place.id,
        }
    canonical = json.dumps(
        {"city_id": city_id, "action": action, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    proposal_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    existing = (
        db.query(AgentProposal)
        .filter(
            AgentProposal.proposal_key == proposal_key,
            AgentProposal.status == "pending",
        )
        .order_by(AgentProposal.id.desc())
        .first()
    )
    if existing is None:
        same_action = db.query(AgentProposal).filter(
            AgentProposal.city_id == city_id,
            AgentProposal.action == action,
            AgentProposal.status == "pending",
        ).all()
        city = db.get(City, city_id)
        incoming = PlaceIdentityInput(
            city=(city.name_local or city.slug) if city else str(city_id),
            title=title,
            chain_name=str(payload.get("chain_name_local") or ""),
            branch_name=str(payload.get("branch_name") or ""),
            address=str(payload.get("address") or payload.get("description") or ""),
            lat=payload.get("lat"),
            lng=payload.get("lng"),
        )
        for candidate in same_action:
            try:
                candidate_payload = json.loads(candidate.payload or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            decision = same_place_candidate(
                incoming,
                PlaceIdentityInput(
                    city=(city.name_local or city.slug) if city else str(city_id),
                    title=candidate.title,
                    chain_name=str(candidate_payload.get("chain_name_local") or ""),
                    branch_name=str(candidate_payload.get("branch_name") or ""),
                    address=str(candidate_payload.get("address") or candidate_payload.get("description") or ""),
                    lat=candidate_payload.get("lat"),
                    lng=candidate_payload.get("lng"),
                ),
            )
            same_large_feature = False
            if (
                str(payload.get("category") or "") in {"tourist", "transport"}
                and _candidate_title_key(candidate.title) == _candidate_title_key(title)
            ):
                try:
                    same_large_feature = _haversine_m(
                        float(payload["lat"]),
                        float(payload["lng"]),
                        float(candidate_payload["lat"]),
                        float(candidate_payload["lng"]),
                    ) <= 1_500
                except (KeyError, TypeError, ValueError):
                    same_large_feature = False
            if decision.same or same_large_feature:
                existing = candidate
                break
    if existing:
        completed_tasks = _complete_matching_proposal_tasks(
            db, city_id=city_id, proposal_title=existing.title, proposal_id=existing.id
        )
        if completed_tasks:
            db.commit()
        return {
            "ok": True,
            "proposal_created": False,
            "proposal_id": existing.id,
            "duplicate": True,
            "completed_tasks": completed_tasks,
        }
    row = AgentProposal(
        city_id=city_id,
        place_id=place_id,
        action=action,
        title=title[:200],
        payload=json.dumps(payload, ensure_ascii=False),
        evidence=evidence.strip()[:8000],
        source_urls=json.dumps([u[:1000] for u in source_urls if u][:12], ensure_ascii=False),
        confidence=max(0.0, min(float(confidence), 1.0)),
        proposal_key=proposal_key,
        status="pending",
    )
    db.add(row)
    db.flush()
    completed_tasks = _complete_matching_proposal_tasks(
        db, city_id=city_id, proposal_title=row.title, proposal_id=row.id
    )
    db.commit()
    return {
        "ok": True,
        "proposal_created": True,
        "proposal_id": row.id,
        "completed_tasks": completed_tasks,
    }


def _has_hangul(s: str) -> bool:
    return any("\uac00" <= ch <= "\ud7a3" for ch in s)


def _compact_subject(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").translate(str.maketrans({
        "瀋": "沈",
        "陽": "阳",
        "宮": "宫",
        "號": "号",
        "門": "门",
        "館": "馆",
        "來": "来",
        "國": "国",
        "廣": "广",
        "區": "区",
    })).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _title_subjects(title: str) -> list[str]:
    """Stable local/Korean name forms used to reject cross-place description overwrites."""
    parts = re.split(r"[()（）]", title or "")
    candidates = [_compact_subject(part) for part in parts]
    return [candidate for candidate in candidates if len(candidate) >= 4]


def _address_subjects(*values: Any) -> set[str]:
    """Extract stable street-number anchors without treating general prose as an address."""

    out: set[str] = set()
    for raw in values:
        value = unicodedata.normalize("NFKC", urllib.parse.unquote(str(raw or "")))
        for match in re.findall(
            r"[^\s,，;；]{2,45}(?:路|街|巷|道)\s*\d{1,6}(?:号|號)",
            value,
            flags=re.IGNORECASE,
        ):
            # Province/city/district prefixes differ by publisher. The actual
            # street-number suffix remains stable enough for branch identity.
            suffix = re.split(r"[省市区區县縣]", match)[-1]
            compact = _compact_subject(suffix)
            if len(compact) >= 6:
                out.add(compact)
        for match in re.findall(
            r"[0-9A-Za-z .'-]{2,50}(?:Road|Street|Avenue|Lane)\s*(?:No\.?\s*)?\d{1,6}",
            value,
            flags=re.IGNORECASE,
        ):
            compact = _compact_subject(match)
            if len(compact) >= 8:
                out.add(compact)
    return out


def _source_mentions_place(marker: Marker, *values: Any) -> bool:
    """Require exact-place evidence for branch facts and attached images."""

    aliases = _title_subjects(marker.title)
    branch = _compact_subject(marker.branch_name or "")
    haystack = _compact_subject(
        " ".join(urllib.parse.unquote(str(value or "")) for value in values)
    )
    if not haystack:
        return False
    if len(branch) < 4:
        return any(alias in haystack for alias in aliases)
    if branch in haystack:
        return True

    # Some booking pages use the exact official hotel name and full street
    # address but omit our internal branch token (e.g. 中街故宫店). Accept that
    # stronger identity pair while still rejecting brand-only/other-branch pages.
    if not any(alias in haystack for alias in aliases):
        return False
    expected_addresses = _address_subjects(marker.coordinate_query, marker.description)
    source_addresses = _address_subjects(*values)
    return any(
        min(len(expected), len(source)) >= 6
        and (expected in source or source in expected)
        for expected in expected_addresses
        for source in source_addresses
    )


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


class _TextExtractor(HTMLParser):
    """script/style 제외 본문 텍스트 + 제목 추출."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript", "svg", "iframe"):
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg", "iframe") and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)


def _decode_html(raw: bytes, content_type: str) -> str:
    charset = ""
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip().strip('"')
    for enc in filter(None, [charset, "utf-8", "gb18030"]):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def _extract_embedded_coordinates(html_text: str, url: str) -> list[dict[str, Any]]:
    """Extract a primary POI coordinate from supported detail-page metadata."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.casefold()
    path = parsed.path.casefold()
    source = ""
    lat_gcj: float
    lng_gcj: float
    metadata: dict[str, Any] = {}
    if "ctrip.com" in host and re.search(r"/(?:food|foods|fooddetail)/", path):
        # Do not turn a stale search-engine hit into a map proposal.
        if "营业提示：暂停营业" in html_text:
            return []
        match = re.search(
            r'"GDCoord"\s*:\s*\{\s*"Lat"\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*"Lng"\s*:\s*(-?\d+(?:\.\d+)?)',
            html_text,
            re.IGNORECASE,
        )
        if not match:
            return []
        lat_gcj, lng_gcj = float(match.group(1)), float(match.group(2))
        source = "ctrip_embedded_gdcoord"
    elif host.endswith("qunar.com") and re.fullmatch(r"/(?:dist/)?poi/\d+", path.rstrip("/")):
        lat_match = re.search(r"\bPOI_LAT\s*=\s*(-?\d+(?:\.\d+)?)", html_text, re.IGNORECASE)
        lng_match = re.search(r"\bPOI_LNG\s*=\s*(-?\d+(?:\.\d+)?)", html_text, re.IGNORECASE)
        if not lat_match or not lng_match:
            return []
        lat_gcj, lng_gcj = float(lat_match.group(1)), float(lng_match.group(1))
        source = "qunar_embedded_poi"
    elif host == "m.map.360.cn" and path.startswith("/m/search/detail"):
        # 360's mobile POI page exposes the selected business as JSON.  It is a
        # useful account-free fallback for China branch addresses and GCJ-02
        # coordinates when OSM/Nominatim do not know a local business.
        marker = re.search(r"window\.__STATE__\s*=\s*", html_text)
        if not marker:
            return []
        try:
            state, _ = json.JSONDecoder().raw_decode(html_text[marker.end():])
            poi = state["searchDetailByPguid"]["data"]["poi"]
            lat_gcj, lng_gcj = float(poi["y"]), float(poi["x"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(poi, dict):
            return []
        source = "360map_embedded_poi"
        metadata = {
            "display_name": ", ".join(filter(None, [
                str(poi.get("name") or "").strip(),
                str(poi.get("address") or "").strip(),
            ]))[:500],
            "address": str(poi.get("address") or "").strip()[:300],
            "external_id": str(poi.get("primaryid") or poi.get("pguid") or "")[:100],
            "tags": str(poi.get("tags") or "")[:500],
        }
    else:
        return []
    if not (-90 <= lat_gcj <= 90 and -180 <= lng_gcj <= 180):
        return []
    lat, lng = gcj02_to_wgs84(lat_gcj, lng_gcj)
    return [{
        "lat": round(lat, 7),
        "lng": round(lng, 7),
        "source": source,
        "source_url": url,
        "source_crs": "GCJ-02",
        "storage_crs": "WGS84",
        "confidence": 0.86,
        "storage_allowed": True,
        **metadata,
    }]


def _ctrip_food_coordinate_url(url: str) -> str:
    """Return Ctrip's coordinate-bearing mobile companion for a POI page."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if host not in {"you.ctrip.com", "www.ctrip.com"}:
        return ""
    match = re.fullmatch(
        r"/food/(?:[a-z-]*?(\d+)|(\d+))/(\d+)(?:-dianping\d+)?\.html",
        parsed.path,
        re.IGNORECASE,
    )
    if not match:
        return ""
    city_code = match.group(1) or match.group(2)
    poi_id = match.group(3)
    return f"https://gs.ctrip.com/html5/you/foods/fooddetail/{city_code}/{poi_id}.html"


def _extract_page_text(url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    safe_url = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.encode("idna").decode("ascii"),
            # Some Chinese mobile map detail routes use ``pid=<id>`` as a path
            # segment.  Encoding that equals sign changes the resource into an
            # empty shell, so preserve it while still escaping unsafe bytes.
            urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/%:@="),
            urllib.parse.quote(urllib.parse.unquote(parsed.query), safe="=&%:+,;@/?"),
            "",
        )
    )
    req = urllib.request.Request(safe_url, headers={"User-Agent": _IMAGE_UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return {"error": f"not_html:{ctype.split(';')[0]}"}
        raw = resp.read(2 * 1024 * 1024)
    html_text = _decode_html(raw, ctype)
    parser = _TextExtractor()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001 — 깨진 HTML은 파싱된 데까지만 사용
        pass
    body = "\n".join(parser.parts)
    return {
        "title": parser.title.strip()[:300],
        "text": body[:7000],
        "coordinate_candidates": _extract_embedded_coordinates(html_text, url),
    }


def _record_visit(db: Session, url: str, title: str = "", city_id: Optional[int] = None) -> bool:
    """방문 기록 upsert. 반환값: 이번이 첫 방문인지."""
    row = db.query(AgentWebVisit).filter(AgentWebVisit.url == url).first()
    if row:
        row.visit_count += 1
        if title and not row.title:
            row.title = title[:300]
        if city_id is not None:
            row.city_id = city_id
        db.commit()
        return False
    db.add(AgentWebVisit(url=url[:1000], title=title[:300], city_id=city_id))
    db.commit()
    return True


_WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
_IMAGE_UA = "CloudmiddleTravelMap/0.2 (shared travel map; image enrichment)"
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_IMAGE_MAX_BYTES = 5 * 1024 * 1024


def _commons_image_from_source(source: str) -> Optional[dict[str, str]]:
    """Resolve a Commons file page to an exact API-provided download URL.

    Models sometimes shortened a Wikimedia thumbnail URL, removing its required
    size suffix and causing HTTP 400/404. The source page is stable, so resolve
    it server-side instead of trusting a copied CDN URL.
    """
    match = re.search(r"https://commons\.wikimedia\.org/[^\s]+", source or "")
    if not match:
        return None
    page_url = match.group(0).rstrip(".,;)]}")
    parsed = urllib.parse.urlsplit(page_url)
    params: dict[str, Any] = {
        "action": "query",
        "format": "json",
        "prop": "imageinfo",
        "iiprop": "url|mime",
        "iiurlwidth": 1280,
    }
    if parsed.path.startswith("/wiki/File:"):
        params["titles"] = urllib.parse.unquote(parsed.path.removeprefix("/wiki/"))
    else:
        page_ids = urllib.parse.parse_qs(parsed.query).get("curid") or []
        if not page_ids:
            return None
        params["pageids"] = page_ids[0]
    req = urllib.request.Request(
        f"{_WIKIMEDIA_API}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": _IMAGE_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=18) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for page in (data.get("query", {}).get("pages") or {}).values():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            image_url = str(info.get("thumburl") or info.get("url") or "")
            if image_url.startswith("https://"):
                return {"image_url": image_url, "page_url": page_url}
    except Exception:  # noqa: BLE001 - caller still has the supplied URL fallback
        return None
    return None


def _wikimedia_image_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": max(1, min(limit, 10)),
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": 1280,
        }
    )
    req = urllib.request.Request(
        f"{_WIKIMEDIA_API}?{params}", headers={"User-Agent": _IMAGE_UA}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out: list[dict[str, Any]] = []
    for page in (data.get("query", {}).get("pages") or {}).values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        ii = infos[0]
        thumb = ii.get("thumburl") or ii.get("url") or ""
        if not str(thumb).lower().rsplit("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        meta = ii.get("extmetadata") or {}
        license_name = ((meta.get("LicenseShortName") or {}).get("value") or "")[:100]
        out.append(
            {
                "title": page.get("title", ""),
                "image_url": thumb,
                "width": ii.get("thumbwidth") or ii.get("width"),
                "height": ii.get("thumbheight") or ii.get("height"),
                "license": license_name,
                "page_url": ii.get("descriptionurl") or "",
                "provider": "Wikimedia Commons",
            }
        )
    return out


def _wikimedia_geosearch(lat: float, lng: float, limit: int = 8) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "generator": "geosearch",
            "ggsprimary": "all",
            "ggsnamespace": 6,
            "ggsradius": 10000,
            "ggscoord": f"{lat}|{lng}",
            "ggslimit": max(1, min(limit, 20)),
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": 1280,
        }
    )
    req = urllib.request.Request(f"{_WIKIMEDIA_API}?{params}", headers={"User-Agent": _IMAGE_UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out: list[dict[str, Any]] = []
    for page in (data.get("query", {}).get("pages") or {}).values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        ii = infos[0]
        thumb = ii.get("thumburl") or ii.get("url") or ""
        if not str(thumb).lower().rsplit("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        meta = ii.get("extmetadata") or {}
        out.append(
            {
                "title": page.get("title", ""),
                "image_url": thumb,
                "width": ii.get("thumbwidth") or ii.get("width"),
                "height": ii.get("thumbheight") or ii.get("height"),
                "license": ((meta.get("LicenseShortName") or {}).get("value") or "")[:100],
                "page_url": ii.get("descriptionurl") or "",
                "provider": "Wikimedia Commons nearby",
            }
        )
    return out


def _openverse_image_search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"q": query, "page_size": max(1, min(limit, 20))})
    req = urllib.request.Request(
        f"https://api.openverse.org/v1/images/?{params}",
        headers={"User-Agent": _IMAGE_UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=18) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    allowed = {"cc0", "pdm", "by", "by-sa"}
    out: list[dict[str, Any]] = []
    for item in data.get("results") or []:
        license_code = str(item.get("license") or "").lower()
        if license_code not in allowed:
            continue
        image_url = item.get("thumbnail") or item.get("url") or ""
        if not str(image_url).lower().startswith("https://"):
            continue
        out.append(
            {
                "title": item.get("title") or "",
                "image_url": image_url,
                "width": item.get("width"),
                "height": item.get("height"),
                "license": f"{license_code.upper()} {item.get('license_version') or ''}".strip(),
                "page_url": item.get("foreign_landing_url") or item.get("detail_url") or "",
                "provider": f"Openverse · {item.get('source') or item.get('provider') or 'open media'}",
            }
        )
    return out


def _image_relevance(item: dict[str, Any], query: str) -> float:
    haystack = f"{item.get('title', '')} {item.get('page_url', '')}".casefold()
    tokens = [token.casefold() for token in re.findall(r"[\w\u3400-\u9fff]{2,}", query)]
    score = 24.0 if str(item.get("provider") or "").startswith("Wikimedia") else 18.0
    score += sum(16.0 for token in set(tokens) if token in haystack)
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    if width >= 1200:
        score += 12
    elif width >= 800:
        score += 7
    elif width and width < 500:
        score -= 20
    if width and height:
        ratio = width / max(height, 1)
        if 0.75 <= ratio <= 2.0:
            score += 8
        elif ratio < 0.45 or ratio > 3.0:
            score -= 16
    if re.search(r"logo|icon|map|地图|portrait|person|旗|徽|seal|diagram", haystack):
        score -= 36
    return round(score, 1)


def run_tool(
    db: Session,
    name: str,
    args: dict[str, Any],
    *,
    city_id: int,
    approved: bool = False,
    server_pure_read: bool = False,
    server_defer_commit: bool = False,
    server_allow_brave_places: bool = False,
    server_storage_query: Optional[str] = None,
    server_record_web_visit: bool = True,
) -> Any:
    if name == "list_unread_events":
        limit = int(args.get("limit") or 30)
        rows = (
            db.query(PlaceEvent)
            .join(Marker, Marker.id == PlaceEvent.place_id)
            .filter(PlaceEvent.groq_read_at.is_(None), PlaceEvent.actor != "agent")
            .filter(Marker.city_id == city_id)
            .order_by(PlaceEvent.created_at.asc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        return [
            {
                "id": e.id,
                "place_id": e.place_id,
                "user_id": e.user_id,
                "actor": e.actor,
                "action": e.action.value,
                "summary": e.summary,
                "payload": json.loads(e.payload or "{}"),
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]

    if name == "mark_events_read":
        ids = [int(x) for x in (args.get("event_ids") or [])]
        allowed = [
            row[0]
            for row in db.query(PlaceEvent.id)
            .join(Marker, Marker.id == PlaceEvent.place_id)
            .filter(PlaceEvent.id.in_(ids), Marker.city_id == city_id)
            .all()
        ]
        n = mark_events_read(db, allowed)
        db.commit()
        return {"marked": n}

    if name == "list_places":
        limit = int(args.get("limit") or 80)
        q = str(args.get("q") or "").strip()
        cat_raw = str(args.get("category") or "").strip()
        exclude = args.get("exclude_ids") or []
        try:
            exclude_ids = {int(x) for x in exclude}
        except (TypeError, ValueError):
            exclude_ids = set()
        near_lat = args.get("near_lat")
        near_lng = args.get("near_lng")
        radius = float(args.get("radius_m") or 150)

        query = db.query(Marker).filter(
            Marker.city_id == city_id, Marker.merged_into_id.is_(None)
        )
        if cat_raw:
            try:
                query = query.filter(Marker.category == MarkerCategory(cat_raw))
            except ValueError:
                pass
        if q:
            like = f"%{q}%"
            query = query.filter(
                (Marker.title.ilike(like))
                | (Marker.description.ilike(like))
                | (Marker.agent_context.ilike(like))
            )
        if exclude_ids:
            query = query.filter(~Marker.id.in_(exclude_ids))

        # Geo filter needs all candidates then haversine (dataset is small)
        if near_lat is not None and near_lng is not None:
            nlat, nlng = float(near_lat), float(near_lng)
            rows = query.all()
            scored = []
            for m in rows:
                dist = _haversine_m(nlat, nlng, m.lat, m.lng)
                if dist <= radius:
                    scored.append((dist, m))
            scored.sort(key=lambda x: x[0])
            return [
                {**_place_brief(m), "distance_m": round(dist, 1)}
                for dist, m in scored[: max(1, min(limit, 200))]
            ]

        rows = (
            query.order_by(Marker.updated_at.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return [_place_brief(m) for m in rows]

    if name == "get_place":
        pid = int(args["place_id"])
        m = (
            db.query(Marker)
            .options(joinedload(Marker.images), joinedload(Marker.contributors))
            .filter(Marker.id == pid, Marker.city_id == city_id)
            .first()
        )
        if not m:
            return {"error": "not_found"}
        events = (
            db.query(PlaceEvent)
            .filter(PlaceEvent.place_id == pid)
            .order_by(PlaceEvent.created_at.desc())
            .limit(20)
            .all()
        )
        brief = _place_brief(m)
        brief["images"] = [
            {"id": i.id, "sort_order": i.sort_order, "group_key": i.group_key, "s3_key": i.s3_key}
            for i in sorted(m.images, key=lambda x: x.sort_order)
        ]
        brief["contributor_user_ids"] = [c.user_id for c in m.contributors]
        brief["recent_events"] = [
            {"id": e.id, "action": e.action.value, "summary": e.summary, "groq_read": e.groq_read_at is not None}
            for e in events
        ]
        return brief

    if name == "find_nearby_candidates":
        pid = int(args["place_id"])
        radius = float(args.get("radius_m") or 120)
        m = db.query(Marker).filter(
            Marker.id == pid,
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
        ).first()
        if not m:
            return {"error": "not_found"}
        others = db.query(Marker).filter(
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
            Marker.id != pid,
        ).all()
        hits = []
        for o in others:
            dist = _haversine_m(m.lat, m.lng, o.lat, o.lng)
            if dist <= radius:
                hits.append({**_place_brief(o), "distance_m": round(dist, 1)})
        hits.sort(key=lambda x: x["distance_m"])
        return hits

    if name == "merge_places":
        target_id = int(args["target_place_id"])
        source_id = int(args["source_place_id"])
        reason = str(args.get("reason") or "same place")
        if target_id == source_id:
            return {"error": "same_id"}
        target = db.query(Marker).filter(
            Marker.id == target_id,
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
        ).first()
        source = db.query(Marker).filter(
            Marker.id == source_id,
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
        ).first()
        if not target or not source:
            return {"error": "not_found"}
        if target.city_id != source.city_id:
            return {"error": "cross_city_merge_forbidden"}
        if not settings.agent_allow_auto_merge and not approved:
            payload = {
                "target_place_id": target_id,
                "source_place_id": source_id,
                "reason": reason,
            }
            return _pending_proposal(
                db,
                city_id=city_id,
                action="merge_places",
                title=f"{source.title} → {target.title} 병합",
                payload=payload,
                evidence=reason,
                source_urls=[str(u) for u in (args.get("source_urls") or [])],
                confidence=float(args.get("confidence") or 0.5),
                place_id=target_id,
            )
        source_title = source.title
        before = {
            "target": marker_snapshot(target),
            "source": marker_snapshot(source),
            "source_images": {
                str(img.id): {"sort_order": img.sort_order, "group_key": img.group_key}
                for img in source.images
            },
        }
        moved_image_ids = [img.id for img in list(source.images)]
        # 기존 기록 보존: 설명·제목 정보를 덧붙임
        chunks = [target.description or ""]
        if source.description and source.description not in (target.description or ""):
            chunks.append(f"[병합 보존:{source.title}] {source.description}")
        elif source.title and source.title not in (target.title or ""):
            chunks.append(f"[병합 보존 별칭] {source.title}")
        target.description = "\n\n".join(c for c in chunks if c).strip()[:2000]
        if source.title and source.title not in target.title:
            combined = f"{target.title} / {source.title}"
            target.title = combined[:200]
        if source.agent_context:
            target.agent_context = ((target.agent_context or "") + "\n" + source.agent_context).strip()[:8000]
        contributor_ids: set[int] = {c.user_id for c in list(source.contributors)}
        if source.user_id:
            contributor_ids.add(source.user_id)
        for uid in sorted(contributor_ids):
            ensure_contributor(db, target.id, uid)
        for img in list(source.images):
            img.place_id = target.id
            img.sort_order = 1000 + img.sort_order
        source.merged_into_id = target.id
        ev = log_place_event(
            db,
            place_id=target.id,
            user=None,
            action=PlaceEventAction.merge,
            summary=f"병합: #{source.id} → #{target.id} ({reason})",
            payload={
                "source_id": source_id,
                "target_id": target_id,
                "reason": reason,
                "before": before,
                "moved_image_ids": moved_image_ids,
            },
            actor="agent",
        )
        db.flush()
        notify_place_contributors(
            db,
            place_ids=[target_id, source_id],
            kind=UserMessageKind.agent_merge,
            title=f"장소가 병합되었습니다: {target.title}",
            body=(
                f"에이전트가 「{source_title}」(#{source_id})를 "
                f"「{target.title}」(#{target_id})로 합쳤습니다.\n"
                f"사유: {reason}\n\n"
                "잘못되었다고 생각되면 메시지에서 이의신청을 남겨 주세요. "
                "다음 새벽 정리 주기에 다시 검토합니다."
            ),
            place_id=target_id,
            related_event_id=ev.id,
        )
        db.commit()
        return {"ok": True, "target_id": target_id, "source_id": source_id}

    if name == "undo_merge":
        source_id = int(args["source_place_id"])
        reason = str(args.get("reason") or "사용자 이의 수용")[:500]
        source = db.query(Marker).filter(Marker.id == source_id, Marker.city_id == city_id).first()
        if not source:
            return {"error": "not_found"}
        if source.merged_into_id is None:
            return {"error": "not_merged", "detail": "이 장소는 병합된 상태가 아닙니다."}
        merge_ev = None
        merge_data: dict[str, Any] = {}
        rows = (
            db.query(PlaceEvent)
            .filter(PlaceEvent.action == PlaceEventAction.merge)
            .order_by(PlaceEvent.created_at.desc())
            .all()
        )
        for ev in rows:
            try:
                data = json.loads(ev.payload or "{}")
            except json.JSONDecodeError:
                continue
            if int(data.get("source_id") or 0) == source_id and not data.get("rolled_back"):
                merge_ev, merge_data = ev, data
                break
        if merge_ev is not None:
            target_id, detail = _rollback_merge(db, merge_data)
            merge_data["rolled_back"] = True
            merge_data["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
            merge_data["rolled_back_by"] = "agent"
            merge_ev.payload = json.dumps(merge_data, ensure_ascii=False)
        else:
            # 병합 이벤트를 못 찾으면 최소한 분리만 수행
            target_id = source.merged_into_id
            source.merged_into_id = None
            detail = f"병합 해제(스냅샷 없음) #{source_id}"
        log_place_event(
            db,
            place_id=source_id,
            user=None,
            action=PlaceEventAction.rollback,
            summary=f"병합 취소: #{source_id} 분리 ({reason})"[:500],
            payload={
                "source_id": source_id,
                "target_id": target_id,
                "reason": reason,
                "detail": detail,
                "undone_event_id": merge_ev.id if merge_ev else None,
            },
            actor="agent",
        )
        db.flush()
        notify_place_contributors(
            db,
            place_ids=[source_id] + ([target_id] if target_id else []),
            kind=UserMessageKind.agent_merge,
            title=f"병합이 취소되었습니다: {source.title}",
            body=(
                f"에이전트가 「{source.title}」(#{source_id})를 다시 별개 장소로 분리했습니다.\n"
                f"사유: {reason}"
            ),
            place_id=source_id,
        )
        db.commit()
        return {"ok": True, "source_id": source_id, "target_id": target_id, "detail": detail}

    if name == "update_place_context":
        pid = int(args["place_id"])
        ctx = str(args.get("context") or "").strip()[:3000]
        m = db.query(Marker).filter(
            Marker.id == pid,
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
        ).first()
        if not m:
            return {"error": "not_found"}
        before = marker_field_snapshot(m)
        # 실행 과정은 AgentRunStep에 남기고, 장소에는 최신 판단만 유지한다.
        m.agent_context = ctx or m.agent_context
        after = marker_field_snapshot(m)
        changes = diff_marker_fields(before, after, keys=["agent_context"])
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.context_update,
            summary=summary_for_changes("에이전트 컨텍스트 보완", changes),
            payload={
                "chars": len(m.agent_context or ""),
                "before": before,
                "after": after,
                "changes": changes,
                "fields": [c["field"] for c in changes],
            },
            actor="agent",
        )
        db.commit()
        return {"ok": True}

    if name == "update_place_fields":
        pid = int(args["place_id"])
        m = db.query(Marker).filter(
            Marker.id == pid,
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
        ).first()
        if not m:
            return {"error": "not_found"}
        if str(args.get("append_note") or "").strip():
            return {
                "error": "structured_insight_required",
                "detail": "설명 누적은 중단되었습니다. 위치·역사·방문정보·팁을 출처 URL과 함께 upsert_place_insights로 저장하세요.",
            }
        expected_title = str(args.get("expected_title") or "").strip()
        if not expected_title or _compact_subject(expected_title) != _compact_subject(m.title):
            return {
                "error": "target_confirmation_required",
                "detail": (
                    f"대상 ID의 현재 제목은 '{m.title}'입니다. list_places/get_place로 대상을 다시 읽고 "
                    "expected_title을 정확히 넣어 재시도하세요."
                ),
            }
        before = marker_field_snapshot(m)
        original_description = m.description or ""
        changed: dict[str, Any] = {}
        local_name = str(args.get("local_name") or "").strip()
        prospective_title = m.title
        if local_name and local_name not in m.title:
            prospective_title = f"{m.title} ({local_name})"[:200]
        replace_title = str(args.get("replace_title") or "").strip()
        if replace_title and replace_title != m.title:
            if not _has_hangul(replace_title):
                return {
                    "error": "korean_required",
                    "detail": "제목은 '中文名 (한국어 명칭)' 형식으로 한국어를 병기해 다시 호출하세요.",
                }
            existing_korean = "".join(re.findall(r"[가-힣]+", m.title or ""))
            incoming_korean = "".join(re.findall(r"[가-힣]+", replace_title))
            if len(existing_korean) >= 4 and existing_korean not in incoming_korean:
                return {
                    "error": "existing_korean_name_must_be_preserved",
                    "detail": (
                        f"기존 한국어 장소명 '{m.title}'을 replace_title에서 그대로 보존해야 합니다. "
                        "중국어 원문명만 추가하려면 기존 한국어 표기를 변경하지 마세요."
                    ),
                }
            prospective_title = replace_title[:200]
        replace_description = str(args.get("replace_description") or "").strip()
        if replace_description and replace_description != m.description:
            if not _has_hangul(replace_description):
                return {
                    "error": "korean_required",
                    "detail": "replace_description 본문은 한국어여야 합니다 (주소만 중국어 유지).",
                }
            if not m.is_agent_suggested and _has_hangul(original_description):
                return {
                    "error": "user_content_protected",
                    "detail": "사용자가 작성한 한국어 설명은 전면 교체할 수 없습니다. 세부 사실은 upsert_place_insights로 보완하세요.",
                }
            subject_title = prospective_title
            aliases = _title_subjects(subject_title)
            compact_description = _compact_subject(replace_description)
            if aliases and not any(alias in compact_description for alias in aliases):
                return {
                    "error": "description_subject_mismatch",
                    "detail": (
                        f"새 설명에 대상 장소명 '{subject_title}'이 확인되지 않습니다. 다른 장소 조사 결과를 "
                        "잘못 덮어쓰지 않도록 대상 이름을 본문에 명시하고 다시 확인하세요."
                    ),
                }
            requested_sources = {
                _normalize_evidence_url(str(url))
                for url in (args.get("source_urls") or [])
                if _normalize_evidence_url(str(url))
            }
            validated_sources = {
                _normalize_evidence_url(str(url))
                for url in (args.get("_validated_source_urls") or [])
                if _normalize_evidence_url(str(url))
            }
            if not requested_sources or not requested_sources.issubset(validated_sources):
                return {
                    "error": "description_source_not_validated",
                    "detail": (
                        "설명을 교체하려면 이번 실행에서 fetch_page로 읽은 대상 장소 출처를 "
                        "source_urls에 넣어야 합니다."
                    ),
                }
            visits = db.query(AgentWebVisit).filter(
                AgentWebVisit.city_id == city_id,
            ).all()
            matching_visits = [
                visit for visit in visits
                if _normalize_evidence_url(visit.url) in requested_sources
                and _source_mentions_place(m, visit.title, visit.url)
            ]
            if not matching_visits:
                return {
                    "error": "description_source_place_mismatch",
                    "detail": (
                        "읽은 출처의 제목·URL에서 현재 장소명 또는 지점명을 확인할 수 없습니다. "
                        "다른 지점의 설명을 덮어쓰지 마세요."
                    ),
                }
            cleaned = "\n".join(
                line for line in replace_description.splitlines()
                if not line.strip().startswith("[이전 제목 보존]")
            ).strip()
            if len(cleaned) > 1200:
                return {
                    "error": "description_too_long",
                    "detail": "description은 1,200자 이내의 소개만 유지하고 세부 사실은 upsert_place_insights로 분리하세요.",
                }
            m.description = cleaned
            changed["replace_description"] = True
        if prospective_title != m.title:
            m.title = prospective_title
            if replace_title:
                changed["replace_title"] = m.title
            else:
                changed["local_name"] = local_name
        if args.get("category"):
            try:
                new_category = MarkerCategory(str(args["category"]))
                if new_category != m.category:
                    m.category = new_category
                    changed["category"] = m.category.value
            except ValueError:
                pass
        role = str(args.get("travel_role") or "").strip()
        valid_roles = {"history", "food", "market_night", "neighborhood", "nature", "shopping", "rest", "practical", "general"}
        if role in valid_roles and role != (m.travel_role or "general"):
            m.travel_role = role
            changed["travel_role"] = role
        if not changed:
            return {"ok": True, "changed": {}}
        after = marker_field_snapshot(m)
        changes = diff_marker_fields(before, after)
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.update,
            summary=summary_for_changes("에이전트 정보 보완", changes),
            payload={
                **changed,
                "before": before,
                "after": after,
                "changes": changes,
                "fields": [c["field"] for c in changes],
            },
            actor="agent",
        )
        db.commit()
        return {"ok": True, "changed": changed}

    if name in {"create_place", "propose_place"}:
        if name == "create_place" and approved:
            # Serialize duplicate-check + insert for administrator approvals and
            # any approved replay of the same city's place identity.
            transaction_lock(db, f"approved-place-create:{city_id}")
        title = str(args.get("title") or "추천 장소")[:200]
        desc = str(args.get("description") or "")[:2000]
        if not _has_hangul(title):
            return {
                "error": "korean_required",
                "detail": (
                    "title은 '中文名 (한국어 명칭)' 형식으로 중국어+한국어를 병기해야 합니다. "
                    "예: '泉城广场 (취안청 광장)'. 수정해 다시 호출하세요."
                ),
            }
        if desc and not _has_hangul(desc):
            return {
                "error": "korean_required",
                "detail": (
                    "description 본문은 무조건 한국어로 작성해야 합니다. "
                    "중국어 정보는 번역하고, 주소만 '주소: [중국어 원문]' 형태로 유지하세요."
                ),
            }
        cat_raw = str(args.get("category") or "other")
        try:
            cat = MarkerCategory(cat_raw)
        except ValueError:
            cat = MarkerCategory.other
        insights_payload: list[dict[str, Any]] = []
        for raw in (args.get("insights") or [])[:20]:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").strip().lower()
            item_title = str(raw.get("title") or "").strip()[:200]
            content = str(raw.get("content") or "").strip()[:4000]
            source_url = str(raw.get("source_url") or "").strip()[:1000]
            if (
                kind not in {"location", "history", "visit", "tip"}
                or not item_title
                or not content
                or not source_url
                or not _has_hangul(content)
            ):
                continue
            insights_payload.append(
                {
                    "kind": kind,
                    "title": item_title,
                    "content": content,
                    "year_label": str(raw.get("year_label") or "").strip()[:50],
                    "source_url": source_url,
                    "source_title": str(raw.get("source_title") or "").strip()[:300],
                    "confidence": max(0.0, min(float(raw.get("confidence") or 0.5), 1.0)),
                }
            )
        coordinate_evidence = (
            dict(args.get("_coordinate_evidence") or {})
            if isinstance(args.get("_coordinate_evidence"), dict)
            else None
        )
        stored_attestation = args.get("_integrity_attestation")
        if coordinate_evidence is None and approved and isinstance(stored_attestation, dict):
            try:
                attestation_version = int(stored_attestation.get("version") or 0)
            except (TypeError, ValueError):
                attestation_version = 999
            signed_candidate = {
                **args,
                "coordinate_attestation": stored_attestation.get("coordinate_attestation"),
            }
            trusted_evidence = trusted_coordinate_evidence(signed_candidate)
            if attestation_version >= 2 and trusted_evidence is None:
                return {
                    "error": "proposal_integrity_attestation_invalid",
                    "detail": "제안 이후 좌표 근거가 변경되었거나 서명을 확인할 수 없어 승인할 수 없습니다.",
                }
            if trusted_evidence is not None:
                # Approval never trusts the old pass/fail flag. It re-runs
                # current rules against this signed server observation.
                coordinate_evidence = trusted_evidence
        coordinate_lat = args.get("lat")
        coordinate_lng = args.get("lng")
        if coordinate_evidence is not None:
            # The model may select an observation, but it cannot rewrite the
            # observation's location or provenance.
            coordinate_lat = coordinate_evidence.get("lat", coordinate_lat)
            coordinate_lng = coordinate_evidence.get("lng", coordinate_lng)
        payload = {
            "title": title,
            "description": desc,
            "address": str(args.get("address") or "")[:300],
            "category": cat.value,
            "travel_role": str(args.get("travel_role") or "general")[:30],
            "lat": float(coordinate_lat),
            "lng": float(coordinate_lng),
            "context": str(args.get("context") or "")[:8000],
            "coordinate_source": str(
                (coordinate_evidence or {}).get("source")
                or args.get("coordinate_source")
                or "agent_research"
            )[:50],
            "coordinate_external_id": str(
                (coordinate_evidence or {}).get("external_id")
                or args.get("coordinate_external_id")
                or ""
            )[:200],
            "coordinate_query": str(
                (coordinate_evidence or {}).get("display_name")
                or (coordinate_evidence or {}).get("title")
                or args.get("coordinate_query")
                or title
            )[:300],
            "coordinate_source_url": str(
                (coordinate_evidence or {}).get("source_url")
                or args.get("coordinate_source_url")
                or ""
            )[:1000],
            "coordinate_confidence": max(
                0.0,
                min(
                    float(
                        (coordinate_evidence or {}).get("confidence")
                        or args.get("coordinate_confidence")
                        or args.get("confidence")
                        or 0.5
                    ),
                    1.0,
                ),
            ),
            "zone_id": int(args["zone_id"]) if args.get("zone_id") is not None else None,
            "chain_name_local": str(args.get("chain_name_local") or "").strip()[:160],
            "chain_name_ko": str(args.get("chain_name_ko") or "").strip()[:160],
            "branch_name": str(args.get("branch_name") or "").strip()[:120],
            "insights": insights_payload,
        }
        if name == "propose_place":
            existing_place = _matching_existing_place(
                db,
                city_id=city_id,
                title=title,
                lat=payload["lat"],
                lng=payload["lng"],
                chain_name=payload["chain_name_local"],
                branch_name=payload["branch_name"],
                address=payload["address"] or payload["description"],
            )
            if existing_place is not None:
                return {
                    "ok": True,
                    "proposal_created": False,
                    "proposal_id": None,
                    "duplicate": True,
                    "existing_place_id": existing_place.id,
                }
        strict_integrity = (
            name == "propose_place"
            or (name == "create_place" and approved)
            or bool(args.get("_integrity_attestation"))
        )
        if strict_integrity:
            city = db.get(City, city_id)
            anchors = db.query(Marker).filter(
                Marker.city_id == city_id,
                Marker.shape == MarkerShape.point,
                Marker.category.in_([
                    MarkerCategory.tourist,
                    MarkerCategory.shopping,
                    MarkerCategory.transport,
                ]),
                Marker.merged_into_id.is_(None),
            ).all()
            integrity = assess_new_place(
                payload,
                city_viewbox=city.search_viewbox if city is not None else "",
                coordinate_evidence=coordinate_evidence,
                anchors=anchors,
            )
            if not integrity.ok:
                return {
                    "error": "place_integrity_failed",
                    "detail": integrity.error,
                    "integrity_errors": integrity.details.get("errors", []),
                    "integrity": integrity.details,
                }
            signed_coordinate = (
                issue_coordinate_attestation(payload, coordinate_evidence).get("coordinate_attestation")
                if coordinate_evidence is not None
                else None
            )
            payload["_integrity_attestation"] = {
                "version": 2,
                "warnings": list(integrity.warnings),
                "details": integrity.details,
                "coordinate_evidence": {
                    field: coordinate_evidence.get(field)
                    for field in (
                        "title", "display_name", "branch_name", "address",
                        "lat", "lng", "source", "source_url", "external_id",
                        "confidence", "storage_allowed",
                    )
                    if coordinate_evidence is not None and field in coordinate_evidence
                } if coordinate_evidence is not None else None,
                "coordinate_attestation": signed_coordinate,
            }
        if not settings.agent_allow_auto_create and not approved:
            source_urls = [str(u) for u in (args.get("source_urls") or []) if str(u).strip()]
            evidence = str(args.get("evidence") or "").strip()
            if not evidence or not source_urls:
                return {
                    "error": "evidence_required",
                    "detail": "장소 제안에는 evidence와 실제 source_urls가 필요합니다.",
                }
            if len(insights_payload) < 2:
                return {
                    "error": "insights_required",
                    "detail": "신규 장소 제안에는 출처가 있는 구조화 정보가 2건 이상 필요합니다.",
                }
            validated_urls = {
                _normalize_evidence_url(str(url))
                for url in (args.get("_validated_source_urls") or [])
                if _normalize_evidence_url(str(url))
            }
            if "_validated_source_urls" in args:
                requested_urls = {
                    _normalize_evidence_url(url)
                    for url in source_urls
                    if _normalize_evidence_url(url)
                }
                insight_urls = {
                    _normalize_evidence_url(str(item.get("source_url") or ""))
                    for item in insights_payload
                    if _normalize_evidence_url(str(item.get("source_url") or ""))
                }
                if (requested_urls | insight_urls) - validated_urls:
                    return {
                        "error": "proposal_source_not_validated",
                        "detail": "장소 제안의 사실 출처는 이번 실행에서 유효한 본문을 읽은 URL이어야 합니다.",
                    }
            return _pending_proposal(
                db,
                city_id=city_id,
                action="create_place",
                title=title,
                payload=payload,
                evidence=evidence,
                source_urls=source_urls,
                confidence=float(args.get("confidence") or 0.5),
            )
        existing_place = _matching_existing_place(
            db,
            city_id=city_id,
            title=title,
            lat=payload["lat"],
            lng=payload["lng"],
            chain_name=payload["chain_name_local"],
            branch_name=payload["branch_name"],
            address=payload["address"] or payload["description"],
        )
        if existing_place is not None:
            return {
                "ok": True,
                "place_id": existing_place.id,
                "duplicate": True,
                "created": False,
            }
        m = Marker(
            user_id=None,
            city_id=city_id,
            category=cat,
            shape=MarkerShape.point,
            title=title,
            description=desc,
            lat=payload["lat"],
            lng=payload["lng"],
            agent_context=payload["context"],
            coordinate_source=payload["coordinate_source"],
            coordinate_external_id=payload["coordinate_external_id"],
            coordinate_query=payload["coordinate_query"],
            coordinate_source_url=payload["coordinate_source_url"],
            coordinate_confidence=payload["coordinate_confidence"],
            coordinate_crs="WGS84",
            coordinate_verified_at=datetime.now(timezone.utc),
            is_agent_suggested=True,
            travel_role=payload["travel_role"],
        )
        if payload["zone_id"] is not None:
            zone = db.query(Marker).filter(
                Marker.id == payload["zone_id"],
                Marker.city_id == city_id,
                Marker.shape == MarkerShape.polygon,
                Marker.merged_into_id.is_(None),
            ).first()
            if zone is not None:
                m.zone_id = zone.id
        else:
            inferred_zone = _containing_zone(
                db,
                city_id=city_id,
                lat=payload["lat"],
                lng=payload["lng"],
            )
            if inferred_zone is not None:
                m.zone_id = inferred_zone.id
        if payload["chain_name_local"]:
            chain = db.query(PlaceChain).filter(
                PlaceChain.name_local.ilike(payload["chain_name_local"])
            ).first()
            if chain is None:
                chain = PlaceChain(
                    name_local=payload["chain_name_local"],
                    name_ko=payload["chain_name_ko"],
                    category=cat.value,
                    aliases="[]",
                    description="에이전트 제안에서 생성",
                )
                db.add(chain)
                db.flush()
            m.chain_id = chain.id
            m.branch_name = payload["branch_name"]
        db.add(m)
        db.flush()
        for index, item in enumerate(insights_payload):
            db.add(
                PlaceInsight(
                    place_id=m.id,
                    kind=item["kind"],
                    title=item["title"],
                    content=item["content"],
                    year_label=item["year_label"],
                    source_url=item["source_url"],
                    source_title=item["source_title"],
                    confidence=item["confidence"],
                    created_by="agent",
                    sort_order=index,
                    verified_at=datetime.now(timezone.utc),
                )
            )
        ev = log_place_event(
            db,
            place_id=m.id,
            user=None,
            action=PlaceEventAction.agent_create,
            summary=f"에이전트 장소 추가: {title}",
            payload={"lat": m.lat, "lng": m.lng, "place_id": m.id, "before": {}},
            actor="agent",
        )
        db.flush()
        notify_all_users(
            db,
            kind=UserMessageKind.agent_create,
            title=f"새 추천 장소: {title}",
            body=(
                f"에이전트가 장소를 추가했습니다.\n"
                f"이름: {title}\n좌표: {m.lat:.5f}, {m.lng:.5f}\n\n"
                "필요 없거나 잘못되었으면 해당 장소 상세 또는 이 메시지에서 이의신청을 남겨 주세요. "
                "다음 새벽 정리 주기에 다시 검토합니다."
            ),
            place_id=m.id,
            related_event_id=ev.id,
        )
        db.commit()
        return {"ok": True, "place_id": m.id}

    if name == "upsert_place_insights":
        pid = int(args["place_id"])
        m = db.query(Marker).filter(
            Marker.id == pid,
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
        ).first()
        if not m:
            return {"error": "not_found"}
        raw_items = args.get("insights") or []
        if not isinstance(raw_items, list):
            return {"error": "bad_insights"}
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("content") or "")
            has_source_currency = bool(re.search(r"(?:위안|元|CNY|RMB|¥)", content, re.IGNORECASE))
            has_converted_currency = bool(re.search(r"(?:\d[\d,]*(?:\.\d+)?\s*원|KRW|₩)", content, re.IGNORECASE))
            if has_source_currency and has_converted_currency:
                return {
                    "error": "derived_currency_conversion_forbidden",
                    "detail": (
                        "장소 지식에는 출처가 표시한 원 통화 금액만 저장하세요. 환율에 따라 바뀌는 "
                        "원화 환산값은 고정 사실처럼 저장할 수 없습니다."
                    ),
                }
        marker_context = " ".join(
            str(value or "")
            for value in (m.title, m.description, m.branch_name, m.coordinate_query)
        )
        marker_districts = _district_tokens(marker_context)
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            source_districts = _district_tokens(
                " ".join(
                    str(value or "")
                    for value in (raw.get("source_title"), raw.get("content"))
                )
            )
            if marker_districts and source_districts and marker_districts.isdisjoint(source_districts):
                return {
                    "error": "insight_branch_mismatch",
                    "detail": (
                        f"장소의 구역 단서 {sorted(marker_districts)}와 인사이트 출처·내용의 "
                        f"구역 단서 {sorted(source_districts)}가 다릅니다. 같은 체인의 다른 지점 "
                        "정보를 현재 장소에 저장할 수 없습니다."
                    ),
                }
        if m.branch_name:
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                kind = str(raw.get("kind") or "").strip().lower()
                if kind not in {"location", "visit", "tip"}:
                    continue
                if not _source_mentions_place(
                    m,
                    raw.get("source_title"),
                    raw.get("source_url"),
                ):
                    return {
                        "error": "insight_branch_source_mismatch",
                        "detail": (
                            f"{m.title}의 지점별 위치·방문 정보에는 지점명 '{m.branch_name}'이 "
                            "확인되는 출처가 필요합니다. 브랜드 일반 페이지의 내용을 특정 지점의 "
                            "주소·영업시간·방문 팁으로 저장할 수 없습니다."
                        ),
                    }
        validated_urls = {
            normalized
            for raw in (args.get("_validated_source_urls") or [])
            if (normalized := _normalize_evidence_url(str(raw)))
        }
        requested_urls = {
            normalized
            for raw in raw_items
            if isinstance(raw, dict)
            if (normalized := _normalize_evidence_url(str(raw.get("source_url") or "")))
        }
        if requested_urls - validated_urls:
            return {
                "error": "insight_source_not_validated",
                "detail": (
                    "구조화 정보의 모든 source_url은 이번 실행에서 fetch_page로 유효한 본문을 읽어야 합니다. "
                    "검색 결과 요약·로그인 화면·다른 지점 페이지를 그대로 저장하지 마세요."
                ),
            }
        allowed_kinds = {"location", "history", "visit", "tip"}
        changed = 0
        now = datetime.now(timezone.utc)
        for index, raw in enumerate(raw_items[:30]):
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").strip().lower()
            title_s = str(raw.get("title") or "").strip()[:200]
            content_s = str(raw.get("content") or "").strip()[:4000]
            source_url = str(raw.get("source_url") or "").strip()[:1000]
            if kind not in allowed_kinds or not title_s or not content_s or not source_url:
                continue
            if not _has_hangul(content_s):
                continue
            confidence = max(0.0, min(float(raw.get("confidence") or 0.5), 1.0))
            row = db.query(PlaceInsight).filter(
                PlaceInsight.place_id == pid,
                PlaceInsight.kind == kind,
                PlaceInsight.title == title_s,
            ).first()
            if row is None:
                incoming_facts = _insight_fact_tokens(content_s)
                same_source_rows = db.query(PlaceInsight).filter(
                    PlaceInsight.place_id == pid,
                    PlaceInsight.kind == kind,
                    PlaceInsight.source_url == source_url,
                ).all()
                row = next(
                    (
                        existing
                        for existing in same_source_rows
                        if incoming_facts
                        and incoming_facts == _insight_fact_tokens(existing.content)
                    ),
                    None,
                )
            if row is None:
                row = PlaceInsight(place_id=pid, kind=kind, title=title_s)
                db.add(row)
            row.content = content_s
            row.year_label = str(raw.get("year_label") or "").strip()[:50]
            row.source_url = source_url
            row.source_title = str(raw.get("source_title") or "").strip()[:300]
            row.confidence = confidence
            row.created_by = "agent"
            row.sort_order = index
            row.verified_at = now
            changed += 1
        if changed:
            log_place_event(
                db,
                place_id=pid,
                user=None,
                action=PlaceEventAction.context_update,
                summary=f"구조화 정보 {changed}건 보완: {m.title}",
                payload={
                    "insight_count": changed,
                    "fields": ["insights"],
                    "changes": [{"field": "insights", "before": None, "after": changed}],
                },
                actor="agent",
            )
            db.commit()
        return {"ok": True, "changed": changed}

    if name == "list_open_appeals":
        limit = int(args.get("limit") or 30)
        rows = (
            db.query(PlaceAppeal)
            .join(Marker, Marker.id == PlaceAppeal.place_id)
            .filter(
                Marker.city_id == city_id,
                PlaceAppeal.status == PlaceAppealStatus.open,
                PlaceAppeal.groq_read_at.is_(None),
            )
            .order_by(PlaceAppeal.created_at.asc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        return [
            {
                "id": a.id,
                "place_id": a.place_id,
                "user_id": a.user_id,
                "body": a.body,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ]

    if name == "resolve_appeal":
        aid = int(args["appeal_id"])
        status_raw = str(args.get("status") or "resolved")
        note = str(args.get("agent_note") or "").strip()[:2000]
        try:
            status = PlaceAppealStatus(status_raw)
        except ValueError:
            return {"error": "bad_status"}
        if status not in (PlaceAppealStatus.resolved, PlaceAppealStatus.dismissed):
            return {"error": "bad_status"}
        appeal = (
            db.query(PlaceAppeal)
            .join(Marker, Marker.id == PlaceAppeal.place_id)
            .filter(PlaceAppeal.id == aid, Marker.city_id == city_id)
            .first()
        )
        if not appeal:
            return {"error": "not_found"}
        appeal.status = status
        appeal.agent_note = note
        appeal.resolved_at = datetime.now(timezone.utc)
        if appeal.groq_read_at is None:
            appeal.groq_read_at = appeal.resolved_at
        label = "반영" if status == PlaceAppealStatus.resolved else "기각"
        from app.messages import contributor_user_ids, notify_users

        recipients = contributor_user_ids(db, [appeal.place_id])
        recipients.add(appeal.user_id)
        notify_users(
            db,
            user_ids=recipients,
            kind=UserMessageKind.appeal_result,
            title=f"이의신청 {label} (장소 #{appeal.place_id})",
            body=f"이의 내용: {appeal.body[:500]}\n\n에이전트 조치: {note}",
            place_id=appeal.place_id,
        )
        db.commit()
        return {"ok": True, "status": status.value}

    if name == "mark_appeals_read":
        ids = [int(x) for x in (args.get("appeal_ids") or [])]
        if not ids:
            return {"marked": 0}
        open_ids = [
            a.id
            for a in db.query(PlaceAppeal)
            .join(Marker, Marker.id == PlaceAppeal.place_id)
            .filter(
                PlaceAppeal.id.in_(ids),
                PlaceAppeal.status == PlaceAppealStatus.open,
                Marker.city_id == city_id,
            )
            .all()
        ]
        if open_ids:
            return {
                "error": "open_appeals_must_resolve",
                "open_ids": open_ids,
                "hint": "resolve_appeal(resolved|dismissed)로 종결한 뒤 읽음 처리하세요.",
            }
        allowed = [
            row[0]
            for row in db.query(PlaceAppeal.id)
            .join(Marker, Marker.id == PlaceAppeal.place_id)
            .filter(PlaceAppeal.id.in_(ids), Marker.city_id == city_id)
            .all()
        ]
        n = mark_appeals_read(db, allowed)
        db.commit()
        return {"marked": n}

    if name == "reorder_images":
        pid = int(args["place_id"])
        if db.query(Marker.id).filter(Marker.id == pid, Marker.city_id == city_id).first() is None:
            return {"error": "not_found"}
        ordered = [int(x) for x in (args.get("ordered_ids") or [])]
        groups = args.get("group_keys") or {}
        images = db.query(PlaceImage).filter(PlaceImage.place_id == pid).all()
        before_orders = {
            str(i.id): {"sort_order": i.sort_order, "group_key": i.group_key} for i in images
        }
        by_id = {i.id: i for i in images}
        for idx, iid in enumerate(ordered):
            if iid in by_id:
                by_id[iid].sort_order = idx
                gk = groups.get(str(iid)) or groups.get(iid)
                if gk is not None:
                    by_id[iid].group_key = str(gk)[:100]
        before_ids = [i.id for i in sorted(images, key=lambda x: x.sort_order)]
        changes = [{"field": "image_ids", "before": before_ids, "after": ordered}]
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.image_reorder,
            summary="에이전트 이미지 순서 조정",
            payload={
                "ordered_ids": ordered,
                "before": {"image_orders": before_orders},
                "after": {"image_ids": ordered},
                "changes": changes,
                "fields": ["image_ids"],
            },
            actor="agent",
        )
        db.commit()
        return {"ok": True}

    if name == "list_recent_rollbacks":
        limit = int(args.get("limit") or 20)
        rows = (
            db.query(PlaceEvent)
            .join(Marker, Marker.id == PlaceEvent.place_id)
            .filter(
                PlaceEvent.action == PlaceEventAction.rollback,
                PlaceEvent.groq_read_at.is_(None),
                Marker.city_id == city_id,
            )
            .order_by(PlaceEvent.created_at.desc())
            .limit(max(1, min(limit, 50)))
            .all()
        )
        return [
            {
                "id": e.id,
                "place_id": e.place_id,
                "summary": e.summary,
                "payload": json.loads(e.payload or "{}"),
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]


    if name == "list_zones":
        zones = (
            db.query(Marker)
            .filter(
                Marker.city_id == city_id,
                Marker.shape == MarkerShape.polygon,
                Marker.merged_into_id.is_(None),
            )
            .order_by(Marker.title.asc())
            .all()
        )
        counts = dict(
            db.query(Marker.zone_id, func.count(Marker.id))
            .filter(Marker.city_id == city_id, Marker.zone_id.is_not(None), Marker.merged_into_id.is_(None))
            .group_by(Marker.zone_id)
            .all()
        )
        output: list[dict[str, Any]] = []
        for zone in zones:
            polygon_status = "valid"
            polygon_error = None
            try:
                polygon = json.loads(zone.polygon or "[]")
            except (TypeError, json.JSONDecodeError):
                # Preserve the zone ID in the observed catalogue so the runner
                # can record a blocked/cooldown disposition instead of crashing
                # or falsely treating a broken polygon as outside every zone.
                polygon = None
                polygon_status = "invalid"
                polygon_error = "malformed_polygon_json"
            else:
                try:
                    vertices = [
                        (float(point["lat"]), float(point["lng"]))
                        for point in polygon
                        if isinstance(point, dict)
                    ] if isinstance(polygon, list) else []
                    twice_area = sum(
                        x1 * y2 - x2 * y1
                        for (y1, x1), (y2, x2) in zip(
                            vertices,
                            vertices[1:] + vertices[:1],
                        )
                    )
                    geometry_valid = (
                        isinstance(polygon, list)
                        and len(vertices) == len(polygon)
                        and len(vertices) >= 3
                        and all(
                            math.isfinite(lat) and math.isfinite(lng)
                            for lat, lng in vertices
                        )
                        and abs(twice_area) > 1e-12
                    )
                except (KeyError, TypeError, ValueError):
                    geometry_valid = False
                if not geometry_valid:
                    polygon_status = "invalid"
                    polygon_error = "invalid_polygon_geometry"
            output.append({
                "id": zone.id,
                "title": zone.title,
                "category": zone.category.value if zone.category else "other",
                "member_count": int(counts.get(zone.id, 0)),
                "polygon": polygon,
                "polygon_status": polygon_status,
                "polygon_error": polygon_error,
            })
        return output

    if name == "assign_place_zone":
        place_id = int(args["place_id"])
        zone_id = int(args["zone_id"]) if args.get("zone_id") is not None else None
        place = db.query(Marker).filter(
            Marker.id == place_id,
            Marker.city_id == city_id,
            Marker.shape == MarkerShape.point,
            Marker.merged_into_id.is_(None),
        ).first()
        if place is None:
            return {"error": "place_not_found_or_not_point"}
        if zone_id is not None:
            zone = db.query(Marker).filter(
                Marker.id == zone_id,
                Marker.city_id == city_id,
                Marker.shape == MarkerShape.polygon,
                Marker.merged_into_id.is_(None),
            ).first()
            if zone is None:
                return {"error": "zone_not_found"}
            if not _zone_contains(zone, lat=place.lat, lng=place.lng):
                inferred = _containing_zone(
                    db, city_id=city_id, lat=place.lat, lng=place.lng
                )
                return {
                    "error": "place_outside_zone_polygon",
                    "requested_zone_id": zone_id,
                    "requested_zone_title": zone.title,
                    "suggested_zone_id": inferred.id if inferred else None,
                    "suggested_zone_title": inferred.title if inferred else "",
                    "detail": (
                        "장소 좌표가 요청한 구역 폴리곤 밖에 있어 잘못된 ID 배정을 차단했습니다. "
                        "list_zones의 ID와 제목을 다시 대조하세요."
                    ),
                }
        if place.zone_id == zone_id:
            return {"ok": True, "changed": False, "zone_id": zone_id}
        before = marker_field_snapshot(place)
        place.zone_id = zone_id
        after = marker_field_snapshot(place)
        changes = diff_marker_fields(before, after, keys=["zone_id"])
        log_place_event(
            db,
            place_id=place.id,
            user=None,
            action=PlaceEventAction.context_update,
            summary=summary_for_changes("에이전트 구역 배정", changes),
            payload={"reason": str(args.get("reason") or "")[:1000], "before": before, "after": after, "changes": changes},
            actor="agent",
        )
        db.commit()
        return {"ok": True, "changed": True, "zone_id": zone_id}

    if name == "assign_place_chain":
        place_id = int(args["place_id"])
        place = db.query(Marker).filter(
            Marker.id == place_id,
            Marker.city_id == city_id,
            Marker.shape == MarkerShape.point,
            Marker.merged_into_id.is_(None),
        ).first()
        if place is None:
            return {"error": "place_not_found_or_not_point"}
        chain_local = str(args.get("chain_name_local") or "").strip()[:160]
        if not chain_local:
            return {"error": "chain_name_required"}
        chain = db.query(PlaceChain).filter(PlaceChain.name_local.ilike(chain_local)).first()
        if chain is None:
            chain = PlaceChain(
                name_local=chain_local,
                name_ko=str(args.get("chain_name_ko") or "").strip()[:160],
                category=place.category.value if place.category else "other",
                aliases=json.dumps([str(item)[:160] for item in (args.get("aliases") or [])], ensure_ascii=False),
                description=str(args.get("reason") or "").strip()[:2000],
            )
            db.add(chain)
            db.flush()
        before = marker_field_snapshot(place)
        place.chain_id = chain.id
        place.branch_name = str(args.get("branch_name") or "").strip()[:120]
        after = marker_field_snapshot(place)
        changes = diff_marker_fields(before, after, keys=["chain_id", "branch_name"])
        if changes:
            log_place_event(
                db,
                place_id=place.id,
                user=None,
                action=PlaceEventAction.context_update,
                summary=summary_for_changes("에이전트 체인 연결", changes),
                payload={"reason": str(args.get("reason") or "")[:1000], "before": before, "after": after, "changes": changes},
                actor="agent",
            )
        db.commit()
        return {"ok": True, "changed": bool(changes), "chain_id": chain.id}

    if name == "list_agent_tasks":
        # ``server_pure_read`` is trusted call provenance, deliberately outside
        # model-controlled ``args`` and the published tool schema. Integrity
        # audits use it to inspect the backlog without the legacy reconciliation
        # write/commit side effect. Ordinary callers retain self-healing.
        if not server_pure_read:
            reconcile_proposal_tasks(db, city_id=city_id)
        limit = max(1, min(int(args.get("limit") or 12), 50))
        query = (
            db.query(AgentTask)
            .filter(AgentTask.city_id == city_id, AgentTask.status == "pending")
            .order_by(AgentTask.priority.desc(), AgentTask.created_at.asc())
            .limit(limit)
        )
        if server_pure_read:
            # A SELECT normally autoflushes unrelated pending ORM changes. That
            # would violate the integrity audit's read-only boundary even with
            # reconciliation disabled, especially if a later checkpoint commits
            # the surrounding session. Keep this query free of implicit writes.
            with db.no_autoflush:
                rows = query.all()
        else:
            rows = query.all()
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "title": row.title,
                "detail": row.detail,
                "success_metric": row.success_metric,
                "priority": row.priority,
                "attempts": row.attempts,
            }
            for row in rows
        ]

    if name == "upsert_agent_task":
        status_value = str(args.get("status") or "pending")
        task_id = int(args["task_id"]) if args.get("task_id") is not None else None
        row = db.query(AgentTask).filter(AgentTask.id == task_id, AgentTask.city_id == city_id).first() if task_id else None
        if row is not None and row.kind == "data_integrity":
            # A scheduled worker, a manual run, and a legacy endpoint can reach
            # the same durable audit from different processes.  Serialize the
            # terminal decision in PostgreSQL, then discard any identity-map
            # snapshot loaded before the lock so a completed verdict can never
            # be overwritten by a stale pending row.
            transaction_lock(db, f"data-integrity-task:{city_id}:{task_id}")
            db.expire(row)
            row = (
                db.query(AgentTask)
                .filter(AgentTask.id == task_id, AgentTask.city_id == city_id)
                .with_for_update()
                .first()
            )
        created = False
        if (
            row is not None
            and row.kind == "data_integrity"
            and row.status == "completed"
        ):
            # Terminal audit results are immutable. This also provides an
            # idempotent recovery boundary for legacy runs that committed the
            # task before finalizing their mission cursor.
            return {
                "ok": True,
                "task_id": row.id,
                "status": "completed",
                "changed": False,
                "created": False,
                "already_completed": True,
                "immutable": True,
                "result": row.result,
                "attempts": row.attempts,
            }
        if row is None:
            title = str(args.get("title") or "").strip()[:240]
            task_text = " ".join(
                [
                    title,
                    str(args.get("kind") or ""),
                    str(args.get("detail") or ""),
                    str(args.get("success_metric") or ""),
                ]
            ).lower()
            requested_kind = str(args.get("kind") or "").strip()
            managed_kinds = {
                "quality_images",
                "quality_drafts",
                "quality_information",
                "quality_zones",
                "quality_verification",
            }
            managed_quality_kind = requested_kind if requested_kind in managed_kinds else None
            quality_keywords = {
                "quality_images": ("이미지", "사진", "image"),
                "quality_drafts": ("초안", "draft"),
                "quality_information": ("인사이트", "정보 구조", "description"),
                "quality_zones": ("구역", "zone"),
                "quality_verification": ("운영 검증", "존재 검증", "verify"),
            }
            for quality_kind, keywords in quality_keywords.items():
                if any(keyword in task_text for keyword in keywords):
                    managed_quality_kind = quality_kind
                    break
            if managed_quality_kind:
                managed = db.query(AgentTask).filter(
                    AgentTask.city_id == city_id,
                    AgentTask.kind == managed_quality_kind,
                ).order_by(AgentTask.id.desc()).first()
                if managed is not None:
                    blocker = str(args.get("result") or args.get("detail") or "").strip()
                    if blocker:
                        changed = managed.result != blocker[:8000]
                        managed.result = blocker[:8000]
                        if server_defer_commit:
                            db.flush()
                        else:
                            db.commit()
                        return {
                            "ok": True,
                            "task_id": managed.id,
                            "status": managed.status,
                            "changed": changed,
                            "created": False,
                            "status_controlled_by_orchestrator": True,
                            "requested_status_ignored": str(args.get("status") or "pending") != managed.status,
                            "detail": (
                                "차단 근거를 기존 품질 과제에 기록했습니다. 실제 결손 재측정 전에는 "
                                "상위 과제를 완료하지 않으며, 오케스트레이터가 다음 장소로 이동합니다."
                            ),
                        }
                    return {
                        "error": "quality_gap_already_tracked",
                        "task_id": managed.id,
                        "detail": (
                            f"같은 품질 결손은 자동 과제 #{managed.id}에서 이미 추적 중입니다. "
                            "새 과제를 만들어 성과로 세지 말고, 차단 내용을 기존 과제 result에 기록한 뒤 "
                            "다른 대상 또는 다른 품질 과제로 이동하세요."
                        ),
                    }
            if any(
                marker in task_text
                for marker in (
                    "승인 제안",
                    "신규 장소 추가",
                    "장소 등록 제안",
                    "place proposal",
                    "new place proposal",
                )
            ):
                return {
                    "error": "proposal_masquerading_as_task",
                    "detail": (
                        "신규 장소 후보는 백로그 성과가 아닙니다. 근거와 좌표를 보강한 뒤 "
                        "propose_place로 관리자 승인 대기 제안을 저장하세요."
                    ),
                }
            row = db.query(AgentTask).filter(
                AgentTask.city_id == city_id,
                AgentTask.title == title,
                AgentTask.status == "pending",
            ).first()
            if row is None:
                row = AgentTask(city_id=city_id, title=title)
                db.add(row)
                created = True
        # SQLAlchemy column defaults are applied when a new row is flushed.  A
        # just-created backlog item therefore has ``kind is None`` here even
        # though the database column defaults to ``research``.  Scheduled runs
        # used to crash before persisting their checkpoint on this exact path.
        managed_existing = str(row.kind or "").startswith("quality_")
        before = (
            row.kind,
            row.detail,
            row.success_metric,
            row.priority,
            row.status,
            row.result,
        )
        if managed_existing:
            # Exact quality tasks are derived from the DB.  The model may record a
            # blocker/result, but must not rename or reclassify them and cause the
            # synchronizer to create a duplicate replacement task.
            blocker = str(args.get("result") or args.get("detail") or "").strip()
            if blocker:
                row.result = blocker[:8000]
        else:
            row.kind = str(args.get("kind") or row.kind or "research")[:30]
            row.detail = str(args.get("detail") or row.detail or "")[:8000]
            row.success_metric = str(args.get("success_metric") or row.success_metric or "")[:2000]
            row.priority = max(1, min(int(args.get("priority") or row.priority or 50), 100))
        requested_status = status_value if status_value in {"pending", "completed", "blocked"} else "pending"
        # DB-derived quality work is completed only after the synchronizer
        # re-measures the actual gap. A model-level completion claim must not
        # sever an active mission from its durable cursor.
        if not managed_existing:
            row.status = requested_status
        if not managed_existing:
            row.result = str(args.get("result") or row.result or "")[:8000]
        if row.status == "completed":
            row.completed_at = datetime.now(timezone.utc)
        after = (
            row.kind,
            row.detail,
            row.success_metric,
            row.priority,
            row.status,
            row.result,
        )
        if server_defer_commit:
            db.flush()
        else:
            db.commit()
        return {
            "ok": True,
            "task_id": row.id,
            "status": row.status,
            "created": created,
            "changed": created or before != after,
            "attempts": row.attempts,
            "status_controlled_by_orchestrator": managed_existing,
            "requested_status_ignored": managed_existing and requested_status != row.status,
        }

    if name == "list_knowledge":
        limit = int(args.get("limit") or 30)
        query = str(args.get("query") or "").strip()
        place_id = int(args["place_id"]) if args.get("place_id") is not None else None
        categories = {str(item).strip() for item in (args.get("categories") or []) if str(item).strip()}
        if query or place_id is not None or categories:
            from app.agent.memory import retrieve_contextual_knowledge

            if place_id is not None and db.query(Marker.id).filter(
                Marker.id == place_id, Marker.city_id == city_id
            ).first() is None:
                return {"error": "cross_city_place_forbidden"}
            retrieved = retrieve_contextual_knowledge(
                db,
                city_id=city_id,
                query=" ".join([query, " ".join(categories)]),
                limit=limit,
            )
            if place_id is not None:
                rows = list_knowledge(db, limit=100, city_id=city_id)
                seen = {item["id"] for item in retrieved["knowledge"]}
                for row in rows:
                    if row.place_id != place_id or row.id in seen:
                        continue
                    retrieved["knowledge"].insert(0, {
                        "id": row.id, "topic": row.topic, "title": row.title,
                        "category": row.category, "scope": row.scope,
                        "summary": (row.summary or row.content or "")[:900],
                        "principles": json.loads(row.principles or "[]"),
                        "next_actions": json.loads(row.next_actions or "[]"),
                        "score": 99.0, "reason": "exact place scope",
                    })
            return retrieved
        rows = list_knowledge(db, limit=limit, city_id=city_id)
        return [
            {
                "id": r.id,
                "topic": r.topic,
                "title": r.title,
                "content": r.content,
                "summary": r.summary,
                "principles": json.loads(r.principles or "[]"),
                "next_actions": json.loads(r.next_actions or "[]"),
                "keywords": json.loads(r.keywords or "[]"),
                "applicability": json.loads(r.applicability or "{}"),
                "source_refs": json.loads(r.source_refs or "[]"),
                "quality_score": r.quality_score,
                "scope": r.scope,
                "city_id": r.city_id,
                "place_id": r.place_id,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    if name == "upsert_knowledge":
        scope = str(args.get("scope") or "city")
        category = str(args.get("category") or "playbook").strip().lower()
        topic = str(args.get("topic") or "general").strip()
        title = str(args.get("title") or "교훈").strip()
        content = str(args.get("content") or "").strip()
        journal_text = f"{topic} {title} {content}".lower()
        if any(
            marker in journal_text
            for marker in ("사이클 요약", "승인 제안 요약", "작업 완료", "cycle summary", "run summary")
        ):
            return {
                "error": "run_history_forbidden_in_knowledge",
                "detail": (
                    "실행 요약·처리 건수·완료 보고는 AgentRunStep에 이미 기록됩니다. "
                    "지식에는 재사용 가능한 출처 전략·도시 맥락·편집 원칙만 저장하고, "
                    "후속 일은 upsert_agent_task로 분리하세요."
                ),
            }
        if category not in {"quality", "workflow", "city", "source", "data_model", "playbook"}:
            return {
                "error": "invalid_knowledge_category",
                "detail": "category는 quality|workflow|city|source|data_model|playbook 중 하나여야 합니다.",
            }
        if category == "workflow" and (
            int(args.get("evidence_count") or 0) < 2
            or not (args.get("principles") or [])
            or not isinstance(args.get("applicability"), dict)
        ):
            return {
                "error": "workflow_knowledge_requires_evidence",
                "detail": "workflow 지식은 사례 2건 이상, 재사용 원칙, 적용 조건을 함께 제공해야 저장할 수 있습니다.",
            }
        requested_place_id = int(args["place_id"]) if args.get("place_id") is not None else None
        if requested_place_id is not None and db.query(Marker.id).filter(
            Marker.id == requested_place_id, Marker.city_id == city_id
        ).first() is None:
            return {"error": "cross_city_place_forbidden"}
        knowledge_city_id = None if scope == "global" else city_id
        row = upsert_knowledge(
            db,
            topic=topic,
            title=title,
            content=content,
            scope=scope,
            city_id=knowledge_city_id,
            place_id=requested_place_id,
            merge=bool(args.get("merge", False)),
            category=category,
            summary=str(args.get("summary") or ""),
            principles=[str(item) for item in (args.get("principles") or [])],
            next_actions=[str(item) for item in (args.get("next_actions") or [])],
            keywords=[str(item) for item in (args.get("keywords") or [])],
            applicability=args.get("applicability") if isinstance(args.get("applicability"), dict) else {},
            source_refs=[str(item) for item in (args.get("source_refs") or [])],
            evidence_count=int(args.get("evidence_count") or 0),
            quality_score=float(args.get("quality_score") or 0.7),
        )
        db.commit()
        return {"ok": True, "topic": row.topic, "id": row.id, "chars": len(row.content or "")}

    if name == "geocode_place":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"results": []}
        city = db.query(City).filter(City.id == city_id, City.status == "active").first()
        if city is None:
            return {"results": [], "error": f"unknown city_id: {city_id}"}
        local_candidates = [
            {
                "id": marker.id,
                "title": marker.title,
                "description": marker.description or "",
                "lat": marker.lat,
                "lng": marker.lng,
                "type": marker.category.value,
            }
            for marker in db.query(Marker).filter(
                Marker.city_id == city_id,
                Marker.shape == MarkerShape.point,
                Marker.merged_into_id.is_(None),
            ).all()
        ]
        try:
            hits = search_address(
                query,
                limit=int(args.get("limit") or 5),
                viewbox=city.search_viewbox,
                city_name=city.name_local,
                city_context=city.search_context,
                city_slug=city.slug,
                city_name_ko=city.name_ko,
                country_code=city.country_code,
                local_candidates=local_candidates,
                arcgis_api_key=settings.arcgis_api_key,
            )
        except Exception as exc:
            return {"results": [], "error": str(exc)}
        return {"results": hits}

    if name == "web_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"results": []}
        # This value is trusted runner provenance and never comes from the model
        # tool schema. A no-storage Brave lead may be used as a live query while
        # only a constant, non-derived marker is retained in search history.
        stored_query = (
            query
            if server_storage_query is None
            else str(server_storage_query or "[transient-query]").strip()[:300]
        )
        max_results = max(1, min(int(args.get("max_results") or 8), 15))
        city = db.get(City, city_id)
        profile = (
            build_search_provider_profile(
                country_code=city.country_code,
                city_slug=city.slug,
                city_name=city.name_local,
                city_name_ko=city.name_ko,
            )
            if city is not None
            else None
        )
        place_candidates: list[dict[str, Any]] = []
        provider_attempts: list[dict[str, Any]] = []
        if (
            server_allow_brave_places
            and settings.brave_place_enabled
            and settings.brave_search_api_key
            and city is not None
        ):
            bounds = parse_viewbox(city.search_viewbox)
            try:
                brave_result = search_brave_places(
                    api_key=settings.brave_search_api_key,
                    query=query,
                    latitude=city.center_lat,
                    longitude=city.center_lng,
                    count=max_results,
                    city_bounds=(bounds.south, bounds.west, bounds.north, bounds.east) if bounds else None,
                    country_code=profile.country_code if profile else city.country_code,
                    storage_allowed=settings.brave_search_storage_rights,
                )
            except Exception as exc:  # noqa: BLE001 - optional provider must not break text search
                brave_result = {
                    "status": "error",
                    "error": "provider_exception",
                    "detail": str(exc)[:180],
                    "results": [],
                }
            place_candidates = list(brave_result.get("results") or [])
            provider_attempts.append({
                "provider": "brave_place",
                "status": brave_result.get("status"),
                "error": brave_result.get("error"),
                "http_status": brave_result.get("http_status"),
                "result_count": len(place_candidates),
                "outside_city_count": int(brave_result.get("outside_city_count") or 0),
                "retries": int(brave_result.get("retries") or 0),
            })
        elif server_allow_brave_places and settings.brave_place_enabled:
            provider_attempts.append({
                "provider": "brave_place",
                "status": "skipped_no_key",
                "result_count": 0,
            })
        results: list[dict[str, Any]] = []
        search_backends: list[str] = []
        backend_errors: list[str] = []
        try:
            try:
                from ddgs import DDGS
            except ImportError:  # 구버전 환경 호환
                from duckduckgo_search import DDGS

            # The library's automatic meta-backend can wait on many engines and
            # discard otherwise good results when the last engine times out. Merge
            # two bounded engines instead: Yahoo is fast for Chinese text and
            # Yandex often exposes Chinese POI detail pages Yahoo omits.
            backend_batches: list[list[dict[str, Any]]] = []
            with DDGS() as ddgs:
                for backend in ("yahoo", "yandex"):
                    try:
                        backend_results = list(ddgs.text(
                            query,
                            max_results=min(max_results * 2, 30),
                            backend=backend,
                        ))
                    except Exception as backend_exc:  # noqa: BLE001
                        backend_errors.append(f"{backend}: {backend_exc}")
                        continue
                    if backend_results:
                        search_backends.append(backend)
                        backend_batches.append(backend_results)
            seen_result_urls: set[str] = set()
            raw_limit = min(max_results * 3, 45)
            for index in range(max((len(batch) for batch in backend_batches), default=0)):
                for batch in backend_batches:
                    if index >= len(batch):
                        continue
                    item = batch[index]
                    item_url = str(item.get("href") or "")
                    if item_url in seen_result_urls:
                        continue
                    seen_result_urls.add(item_url)
                    results.append(item)
                    if len(results) >= raw_limit:
                        break
                if len(results) >= raw_limit:
                    break
            if not results and not place_candidates:
                raise RuntimeError("; ".join(backend_errors) or "no search results")
        except Exception as exc:  # noqa: BLE001
            if place_candidates:
                backend_errors.append(f"text_search: {exc}")
                results = []
                search_backends = []
            else:
                db.add(
                    AgentSearchLog(
                        query=stored_query,
                        city_id=city_id,
                        results_count=0,
                        new_count=0,
                    )
                )
                db.commit()
                logger.warning(
                    "web_search failed city=%s query=%r error=%s",
                    city_id,
                    stored_query[:120],
                    (
                        "transient_search_failed"
                        if stored_query != query
                        else str(exc)[:240]
                    ),
                )
                return {
                    "error": str(exc),
                    "results": [],
                    "place_candidates": [],
                    "provider_attempts": provider_attempts,
                }

        raw_result_count = len(results)
        results, discarded_count = _filter_search_results(
            query,
            results,
            limit=max_results,
            profile=profile,
        )
        hrefs = [r.get("href") or "" for r in results]
        known_rows = (
            db.query(AgentSearchResult).filter(AgentSearchResult.url.in_(hrefs)).all()
            if hrefs else []
        )
        known_by_url = {row.url: row for row in known_rows}
        seen_urls = {url for url, row in known_by_url.items() if row.city_id == city_id}
        out = [
            {
                "title": r.get("title"),
                "href": r.get("href"),
                "body": (r.get("body") or "")[:300],
                "quality": float(r.get("quality") or 0),
                "seen": (r.get("href") or "") in seen_urls,
            }
            for r in results
        ]
        past = (
            db.query(AgentSearchLog)
            .filter(AgentSearchLog.query == stored_query, AgentSearchLog.city_id == city_id)
            .order_by(AgentSearchLog.searched_at.desc())
            .all()
        )
        for item in results:
            url = item.get("href") or ""
            if not url:
                continue
            existing = known_by_url.get(url)
            if existing:
                existing.seen_count += 1
                existing.city_id = city_id
                existing.last_seen_at = datetime.now(timezone.utc)
                if item.get("title") and not existing.title:
                    existing.title = str(item["title"])[:300]
            else:
                db.add(
                    AgentSearchResult(
                        url=url[:1000],
                        title=str(item.get("title") or "")[:300],
                        city_id=city_id,
                    )
                )
        db.add(
            AgentSearchLog(
                query=stored_query,
                city_id=city_id,
                # Brave's no-storage response is excluded even from aggregate
                # counters; only independently returned text-search rows count.
                results_count=len(out),
                new_count=sum(1 for r in out if not r["seen"]),
            )
        )
        db.commit()
        return {
            "results": out,
            "place_candidates": place_candidates,
            "provider_attempts": provider_attempts,
            "backend": "+".join(search_backends),
            "backend_errors": backend_errors,
            "raw_results_count": raw_result_count,
            "discarded_count": discarded_count,
            "past_searches": {
                "times": len(past),
                "last_at": past[0].searched_at.isoformat() if past else None,
            },
        }

    if name == "fetch_page":
        url = str(args.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return {"error": "bad_url"}
        if _blocked_search_url(url):
            return {"error": "blocked_source", "detail": "스팸·성인·저품질 출처는 열람하지 않습니다."}
        # This switch is server-owned and deliberately absent from the tool
        # schema. A URL chosen after a no-retention provider result may itself
        # be provider-derived, so the discovery runner can fetch it for live
        # cross-verification without writing the URL to AgentWebVisit.
        prior = (
            db.query(AgentWebVisit).filter(
                AgentWebVisit.url == url,
                AgentWebVisit.city_id == city_id,
            ).first()
            if server_record_web_visit
            else None
        )
        already_visited = prior is not None
        try:
            page = _extract_page_text(url)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"fetch_failed: {exc}"[:300]}
        if "error" in page:
            return page
        coordinate_companion_url = ""
        if not page.get("coordinate_candidates"):
            coordinate_companion_url = _ctrip_food_coordinate_url(url)
            if coordinate_companion_url:
                try:
                    companion_page = _extract_page_text(coordinate_companion_url)
                except Exception as exc:  # noqa: BLE001
                    if server_record_web_visit:
                        logger.info(
                            "Ctrip coordinate companion failed url=%s error=%s",
                            coordinate_companion_url,
                            str(exc)[:180],
                        )
                    else:
                        logger.info(
                            "Transient follow-up coordinate companion failed class=%s",
                            type(exc).__name__,
                        )
                else:
                    companion_title = str(companion_page.get("title") or "").strip()
                    companion_coordinates = []
                    for raw_coordinate in companion_page.get("coordinate_candidates") or []:
                        if not isinstance(raw_coordinate, dict):
                            continue
                        coordinate = dict(raw_coordinate)
                        coordinate["display_name"] = str(
                            coordinate.get("display_name") or companion_title
                        )[:500]
                        companion_coordinates.append(coordinate)
                    if companion_coordinates:
                        page["coordinate_candidates"] = companion_coordinates
                        page["coordinate_companion_url"] = coordinate_companion_url
                        if not page.get("title") and companion_title:
                            page["title"] = companion_title
        page_text = f"{page.get('title') or ''} {page.get('text') or ''}"
        if _UNSAFE_SEARCH_TEXT_RE.search(page_text):
            return {"error": "unsafe_source_content", "detail": "여행 정보에 부적합한 출처를 제외했습니다."}
        candidate_page = {
            "url": url,
            "title": page.get("title") or "",
            "text": page.get("text") or "",
        }
        if not page.get("coordinate_candidates") and not is_useful_fetched_page(candidate_page):
            return {
                "error": "page_not_useful_evidence",
                "detail": "로그인·인증 화면이거나 여행 정보 본문이 없어 검증 근거로 사용할 수 없습니다.",
            }
        if server_record_web_visit:
            _record_visit(db, url, page.get("title") or "", city_id=city_id)
        return {
            "url": url,
            "title": page.get("title") or "",
            "text": page.get("text") or "",
            "coordinate_candidates": page.get("coordinate_candidates") or [],
            "coordinate_companion_url": page.get("coordinate_companion_url") or "",
            "already_visited": already_visited,
            "last_visited_at": (
                prior.last_visited_at.isoformat()
                if prior and prior.last_visited_at
                else None
            ),
        }

    if name == "list_research_history":
        limit = max(1, min(int(args.get("limit") or 20), 60))
        # 검색어별 집계: 횟수·최근 시각·최근 새 콘텐츠 수확
        logs = (
            db.query(AgentSearchLog)
            .filter(AgentSearchLog.city_id == city_id)
            .order_by(AgentSearchLog.searched_at.desc())
            .limit(300)
            .all()
        )
        by_query: dict[str, dict[str, Any]] = {}
        for log in logs:
            entry = by_query.setdefault(
                log.query,
                {
                    "query": log.query,
                    "times": 0,
                    "last_at": log.searched_at.isoformat() if log.searched_at else None,
                    "last_new_count": log.new_count,
                    "last_results_count": log.results_count,
                },
            )
            entry["times"] += 1
        visits = (
            db.query(AgentWebVisit)
            .filter(AgentWebVisit.city_id == city_id)
            .order_by(AgentWebVisit.last_visited_at.desc())
            .limit(limit)
            .all()
        )
        return {
            "searches": list(by_query.values())[:limit],
            "recent_visits": [
                {
                    "url": v.url,
                    "title": v.title,
                    "visit_count": v.visit_count,
                    "last_visited_at": (
                        v.last_visited_at.isoformat() if v.last_visited_at else None
                    ),
                }
                for v in visits
            ],
            "total_visited_pages": db.query(AgentWebVisit).filter(
                AgentWebVisit.city_id == city_id
            ).count(),
        }

    if name == "search_place_images":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"results": []}
        limit = max(1, min(int(args.get("limit") or 8), 20))
        try:
            marker = None
            if args.get("place_id") is not None:
                marker = db.query(Marker).filter(
                    Marker.id == int(args["place_id"]),
                    Marker.city_id == city_id,
                    Marker.merged_into_id.is_(None),
                ).first()
            queries = [query]
            if marker is not None:
                local = marker.title.split("(", 1)[0].strip()
                if local and local.casefold() != query.casefold():
                    queries.append(local)
                city = db.get(City, city_id)
                if city:
                    queries.append(f"{city.slug} {local or query}")
                    if city.name_local not in query:
                        queries.append(f"{city.name_local} {local or query}")
            candidates: list[dict[str, Any]] = []
            errors: list[str] = []
            for candidate_query in queries[:3]:
                try:
                    candidates.extend(_wikimedia_image_search(candidate_query, limit=max(5, limit)))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"commons:{exc}"[:180])
                try:
                    candidates.extend(_openverse_image_search(candidate_query, limit=max(5, limit)))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"openverse:{exc}"[:180])
            if marker is not None:
                try:
                    nearby = _wikimedia_geosearch(marker.lat, marker.lng, limit=max(8, limit))
                    for item in nearby:
                        item["nearby"] = True
                    candidates.extend(nearby)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"geosearch:{exc}"[:180])
            unique: dict[str, dict[str, Any]] = {}
            for item in candidates:
                url = str(item.get("image_url") or "")
                if not url or url in unique:
                    continue
                item["score"] = _image_relevance(item, query) + (5 if item.get("nearby") else 0)
                unique[url] = item
            ranked = sorted(unique.values(), key=lambda item: item.get("score", 0), reverse=True)
            return {"results": ranked[:limit], "pool_size": len(ranked), "queries": queries[:3], "warnings": errors[:4]}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:300], "results": []}

    if name == "attach_image_from_url":
        pid = int(args["place_id"])
        requested_url = str(args.get("image_url") or "").strip()
        source = str(args.get("source") or "")[:500]
        if not requested_url.lower().startswith("https://"):
            return {"error": "bad_url"}
        m = (
            db.query(Marker)
            .options(joinedload(Marker.images))
            .filter(
                Marker.id == pid,
                Marker.city_id == city_id,
                Marker.merged_into_id.is_(None),
            )
            .first()
        )
        if not m:
            return {"error": "not_found"}
        if not _source_mentions_place(m, source, requested_url):
            return {
                "error": "image_source_subject_mismatch",
                "detail": (
                    f"이미지 출처 제목/URL에서 대상 장소 '{m.title}'을 확인할 수 없습니다. "
                    "단순 인근 사진은 첨부하지 말고 장소명이 직접 일치하는 사진만 사용하세요."
                ),
            }
        if not storage.s3_enabled():
            return {"error": "s3_disabled"}
        if len(m.images) >= 8:
            return {"error": "too_many_images"}
        commons = _commons_image_from_source(source)
        url = commons["image_url"] if commons else requested_url
        duplicate_key = commons["page_url"] if commons else requested_url
        duplicate = db.query(PlaceEvent.id).filter(
            PlaceEvent.place_id == pid,
            PlaceEvent.action == PlaceEventAction.image_add,
            PlaceEvent.payload.contains(duplicate_key),
        ).first()
        if duplicate:
            return {"error": "duplicate_image_source"}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _IMAGE_UA})
            with urllib.request.urlopen(req, timeout=25) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in _IMAGE_TYPES:
                    return {"error": f"unsupported_type:{ctype}"}
                data = resp.read(_IMAGE_MAX_BYTES + 1)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"download_failed: {exc}"[:300]}
        if len(data) > _IMAGE_MAX_BYTES:
            return {"error": "too_large"}
        if len(data) < 1024:
            return {"error": "too_small"}
        key = storage.build_object_key(pid, "web-image", ctype)
        try:
            storage.put_object_bytes(key, data, ctype)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"upload_failed: {exc}"[:300]}
        max_order = max([i.sort_order for i in m.images], default=-1)
        img = PlaceImage(
            place_id=pid,
            s3_key=key,
            content_type=ctype,
            sort_order=max_order + 1,
            uploaded_by_user_id=None,
        )
        db.add(img)
        db.flush()
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.image_add,
            summary=f"웹 이미지 추가: {m.title}",
            payload={
                "image_id": img.id,
                "s3_key": key,
                "source_url": url[:500],
                "requested_url": requested_url[:500],
                "source_page_url": commons["page_url"][:500] if commons else "",
                "source": source,
                "changes": [{"field": "image_id", "before": None, "after": img.id}],
                "fields": ["image_id"],
            },
            actor="agent",
        )
        db.commit()
        return {
            "ok": True,
            "image_id": img.id,
            "url": storage.public_url(key),
            "source_resolved": bool(commons),
        }

    if name == "list_stale_places":
        days = max(7, int(args.get("days") or 30))
        limit = max(1, min(int(args.get("limit") or 10), 50))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        rows = db.query(Marker).filter(
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
        ).all()
        stale: list[dict[str, Any]] = []
        for m in rows:
            candidates = [t for t in (m.updated_at, m.last_verified_at, m.created_at) if t]
            if not candidates:
                continue
            last_checked = max(
                t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in candidates
            )
            if last_checked >= cutoff:
                continue
            stale.append(
                {
                    **_place_brief(m),
                    "last_checked_at": last_checked.isoformat(),
                    "days_since_check": (now - last_checked).days,
                }
            )
        stale.sort(key=lambda x: x["days_since_check"], reverse=True)
        return stale[:limit]

    if name == "verify_place":
        pid = int(args["place_id"])
        status = str(args.get("status") or "").strip()
        note = str(args.get("note") or "").strip()[:1000]
        if status not in ("valid", "closed", "moved", "uncertain"):
            return {"error": "bad_status"}
        m = db.query(Marker).filter(
            Marker.id == pid,
            Marker.city_id == city_id,
            Marker.merged_into_id.is_(None),
        ).first()
        if not m:
            return {"error": "not_found"}
        if status != "uncertain":
            note_urls = _note_urls(note)
            validated_urls = {
                normalized
                for raw in (args.get("_validated_source_urls") or [])
                if (normalized := _normalize_evidence_url(str(raw)))
            }
            if not note_urls:
                return {
                    "error": "verification_source_required",
                    "detail": "valid/closed/moved 판정에는 실제로 읽은 근거 URL을 note에 포함해야 합니다.",
                }
            if not note_urls.intersection(validated_urls):
                return {
                    "error": "verification_source_not_validated",
                    "detail": (
                        "note의 URL은 유효한 본문을 읽은 근거 목록에 없습니다. 검색 결과 제목이나 로그인·인증 "
                        "화면만으로 판정하지 말고 fetch_page로 본문을 확인하세요."
                    ),
                }
            marker_context = " ".join(
                str(value or "")
                for value in (
                    m.title,
                    m.description,
                    m.branch_name,
                    m.coordinate_query,
                )
            )
            marker_districts = _district_tokens(marker_context)
            source_districts = _district_tokens(note)
            if marker_districts and source_districts and marker_districts.isdisjoint(source_districts):
                return {
                    "error": "verification_branch_mismatch",
                    "detail": (
                        f"장소의 구역 단서 {sorted(marker_districts)}와 근거의 구역 단서 "
                        f"{sorted(source_districts)}가 다릅니다. 다른 지점 근거를 현재 장소에 적용하지 마세요."
                    ),
                }
        before = marker_field_snapshot(m)
        now = datetime.now(timezone.utc)
        if status != "uncertain":
            m.last_verified_at = now
        tag = {
            "valid": "정보 유효",
            "closed": "폐업 추정",
            "moved": "이전 확인",
            "uncertain": "확인 필요",
        }[status]
        if status != "valid" and note:
            line = f"[재검증 {now.strftime('%Y-%m-%d')} · {tag}] {note}"
            if line not in (m.agent_context or ""):
                m.agent_context = ((m.agent_context or "").rstrip() + "\n\n" + line).strip()[
                    :8000
                ]
        after = marker_field_snapshot(m)
        changes = diff_marker_fields(before, after, keys=["agent_context"])
        log_place_event(
            db,
            place_id=pid,
            user=None,
            action=PlaceEventAction.context_update,
            summary=f"재검증({tag}): {m.title}",
            payload={
                "verify_status": status,
                "note": note,
                "before": before,
                "after": after,
                "changes": changes,
                "fields": [c["field"] for c in changes],
            },
            actor="agent",
        )
        db.commit()
        return {
            "ok": True,
            "status": status,
            "last_verified_at": (
                m.last_verified_at.isoformat() if m.last_verified_at else None
            ),
            "requires_retry": status == "uncertain",
        }

    return {"error": f"unknown_tool:{name}"}
