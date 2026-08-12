"""도시 범위 안에서 여러 위치 공급자를 검색하고 하나의 후보 목록으로 합친다.

공급자별 역할은 의도적으로 다르다.
- local: 이미 운영 DB에 저장된 장소를 먼저 보여 중복 등록을 막는다.
- ArcGIS: 중국 POI 탐색 품질을 보강한다. API 키가 없으면 표시만 허용한다.
- Nominatim: 저장 가능한 OSM 좌표와 주소를 제공한다.
- Wikidata: 저장 가능한 좌표와 지식 그래프 식별자를 제공한다.

ArcGIS 문서상 ``forStorage=true``와 유효한 토큰 없이 반환된 좌표는 영구 저장할
수 없다. 따라서 익명 ArcGIS 단독 후보는 ``storage_allowed=False``로 반환한다.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
ARCGIS_URL = (
    "https://geocode-api.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
    "findAddressCandidates"
)
ARCGIS_ANONYMOUS_URL = (
    "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
    "findAddressCandidates"
)
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
USER_AGENT = "CloudmiddleTravelMap/0.4 (+https://d232kzujcg4ufp.cloudfront.net)"

_CACHE_TTL_SECONDS = 60 * 60 * 12
_CACHE_MAX_ITEMS = 512
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_nominatim_lock = threading.Lock()
_nominatim_last_request = 0.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Bounds:
    west: float
    south: float
    east: float
    north: float

    def contains(self, lat: float, lng: float, *, padding: float = 0.0) -> bool:
        return (
            self.south - padding <= lat <= self.north + padding
            and self.west - padding <= lng <= self.east + padding
        )

    @property
    def center(self) -> tuple[float, float]:
        return ((self.south + self.north) / 2, (self.west + self.east) / 2)

    @property
    def arcgis_extent(self) -> str:
        return f"{self.west},{self.south},{self.east},{self.north}"


def parse_viewbox(viewbox: str) -> Optional[Bounds]:
    """Nominatim 순서(west,north,east,south)를 공통 Bounds로 바꾼다."""
    if not viewbox:
        return None
    try:
        west, north, east, south = (float(value.strip()) for value in viewbox.split(","))
    except (TypeError, ValueError):
        return None
    if west >= east or south >= north:
        return None
    return Bounds(west=west, south=south, east=east, north=north)


def _get_json(
    base_url: str,
    params: dict[str, Any],
    *,
    timeout: float = 10,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cached(key: str, loader: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
    value = loader()
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ITEMS:
            oldest = min(_cache, key=lambda item: _cache[item][0])
            _cache.pop(oldest, None)
        _cache[key] = (now, value)
    return value


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff\uac00-\ud7a3]+", "", value)


def _meaningful_entity_match(query: str, display_name: str, city_name: str = "") -> bool:
    """Reject city-centre/provider fallbacks that do not name the requested entity or address."""
    generic = {
        _normalize(item) for item in (
            city_name, f"{city_name}市", "沈阳", "沈阳市", "济南", "济南市",
            "辽宁省", "山东省", "中国", "地址", "位置", "地图",
            "추천", "장소", "검색", "어디",
        ) if item
    }

    def fragments(value: str) -> list[str]:
        raw = re.split(r"[\s,，、;；:：!！?？()（）\[\]【】·|/\\]+", value or "")
        out: list[str] = []
        for part in raw:
            normalized = _normalize(part)
            for token in generic:
                normalized = normalized.replace(token, "")
            if len(normalized) >= 2:
                out.append(normalized)
        return out

    query_parts = fragments(query)
    display_parts = fragments(display_name)
    if not query_parts or not display_parts:
        return False
    query_text = "".join(query_parts)
    display_text = "".join(display_parts)
    if min(len(query_text), len(display_text)) >= 4 and (
        query_text in display_text or display_text in query_text
    ):
        return True
    for left in query_parts:
        for right in display_parts:
            if min(len(left), len(right)) >= 3 and (left in right or right in left):
                return True
            max_size = min(len(left), len(right), 16)
            for size in range(max_size, 3, -1):
                if any(left[start:start + size] in right for start in range(len(left) - size + 1)):
                    return True
    return False


def _haversine_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    lat1, lng1 = math.radians(float(a["lat"])), math.radians(float(a["lng"]))
    lat2, lng2 = math.radians(float(b["lat"])), math.radians(float(b["lng"]))
    d_lat, d_lng = lat2 - lat1, lng2 - lng1
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lng / 2) ** 2
    return 6_371_000 * 2 * math.asin(math.sqrt(h))


def _confidence_label(score: float, source_count: int, existing_marker_id: Optional[int]) -> str:
    if existing_marker_id:
        return "내 지도에 등록됨"
    if source_count >= 2 and score >= 0.82:
        return "교차 확인"
    if score >= 0.9:
        return "높은 일치"
    if score >= 0.76:
        return "유력"
    return "확인 필요"


def _local_hits(query: str, candidates: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    needle = _normalize(query)
    if not needle:
        return []
    results: list[dict[str, Any]] = []
    for item in candidates:
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        title_norm = _normalize(title)
        description_norm = _normalize(description)
        if needle == title_norm:
            confidence = 1.0
        elif needle in title_norm or title_norm in needle:
            confidence = 0.97
        elif needle in description_norm:
            confidence = 0.88
        else:
            continue
        detail = next((line.strip() for line in description.splitlines() if line.strip()), "")
        results.append(
            {
                "query": query,
                "display_name": f"{title}, {detail}" if detail else title,
                "lat": float(item["lat"]),
                "lng": float(item["lng"]),
                "type": str(item.get("type") or "saved_place"),
                "source": "local",
                "sources": ["local"],
                "confidence": confidence,
                "storage_allowed": True,
                "existing_marker_id": int(item["id"]),
                "external_id": "",
                "source_url": "",
            }
        )
    results.sort(key=lambda hit: hit["confidence"], reverse=True)
    return results[:limit]


def _search_nominatim(
    query: str,
    *,
    limit: int,
    bounds: Optional[Bounds],
    city_name: str,
) -> list[dict[str, Any]]:
    search_query = f"{query}, {city_name}" if city_name and city_name not in query else query
    cache_key = f"nominatim:{search_query}:{bounds}:{limit}"

    def load() -> list[dict[str, Any]]:
        global _nominatim_last_request
        # 공개 Nominatim 정책의 앱 전체 1 req/s 상한을 단일 프로세스에서 지킨다.
        with _nominatim_lock:
            wait = 1.05 - (time.monotonic() - _nominatim_last_request)
            if wait > 0:
                time.sleep(wait)
            values: dict[str, Any] = {
                "q": search_query,
                "format": "jsonv2",
                "addressdetails": 1,
                "namedetails": 1,
                "limit": max(1, min(limit, 10)),
                "bounded": 1 if bounds else 0,
                "accept-language": "ko,zh-CN,en",
                "countrycodes": "cn",
            }
            if bounds:
                values["viewbox"] = f"{bounds.west},{bounds.north},{bounds.east},{bounds.south}"
            try:
                data = _get_json(NOMINATIM_URL, values, timeout=12)
            finally:
                _nominatim_last_request = time.monotonic()

        results: list[dict[str, Any]] = []
        query_norm = _normalize(query)
        for item in data if isinstance(data, list) else []:
            try:
                lat, lng = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if bounds and not bounds.contains(lat, lng):
                continue
            display_name = str(item.get("display_name") or query)
            if not _meaningful_entity_match(query, display_name, city_name):
                continue
            importance = max(0.0, min(float(item.get("importance") or 0.35), 1.0))
            exact_bonus = 0.07 if query_norm and query_norm in _normalize(display_name) else 0.0
            results.append(
                {
                    "query": query,
                    "display_name": display_name,
                    "lat": lat,
                    "lng": lng,
                    "type": str(item.get("addresstype") or item.get("type") or item.get("class") or ""),
                    "source": "nominatim",
                    "sources": ["nominatim"],
                    "confidence": min(0.9, 0.62 + importance * 0.22 + exact_bonus),
                    "storage_allowed": True,
                    "existing_marker_id": None,
                    "external_id": str(item.get("osm_id") or ""),
                    "source_url": "https://www.openstreetmap.org/" if item.get("osm_id") else "",
                }
            )
        return results

    return _cached(cache_key, load)


def _arcgis_type_weight(match_type: str) -> float:
    return {
        "POI": 1.0,
        "PointAddress": 1.0,
        "StreetAddress": 0.96,
        "StreetName": 0.88,
        "Postal": 0.72,
        "Locality": 0.64,
    }.get(match_type, 0.78)


def _place_query_core(query: str) -> str:
    normalized = _normalize(query)
    for suffix in (
        "历史博物馆",
        "故宫博物院",
        "博物馆",
        "纪念馆",
        "美食街",
        "步行街",
        "风景区",
        "公园",
        "广场",
    ):
        suffix_norm = _normalize(suffix)
        if normalized.endswith(suffix_norm) and len(normalized) - len(suffix_norm) >= 2:
            return normalized[: -len(suffix_norm)]
    return normalized


def _search_arcgis(
    query: str,
    *,
    limit: int,
    bounds: Optional[Bounds],
    city_name: str,
    city_context: str,
    api_key: str,
) -> list[dict[str, Any]]:
    can_store = bool(api_key)
    search_parts = [query]
    context = city_context or " ".join(part for part in (city_name, "中国") if part)
    if context:
        search_parts.append(context)
    search_query = " ".join(search_parts)
    cache_key = f"arcgis:{search_query}:{bounds}:{limit}:{can_store}"

    def load() -> list[dict[str, Any]]:
        values: dict[str, Any] = {
            "SingleLine": search_query,
            "f": "json",
            "outFields": "Match_addr,Addr_type,PlaceName,MatchID",
            "maxLocations": max(1, min(limit, 10)),
            "sourceCountry": "CHN",
            "forStorage": "true" if can_store else "false",
        }
        if bounds:
            values["searchExtent"] = bounds.arcgis_extent
        if api_key:
            values["token"] = api_key
        data = _get_json(ARCGIS_URL if api_key else ARCGIS_ANONYMOUS_URL, values, timeout=10)
        results: list[dict[str, Any]] = []
        for item in data.get("candidates", []) if isinstance(data, dict) else []:
            try:
                lat = float(item["location"]["y"])
                lng = float(item["location"]["x"])
                raw_score = float(item.get("score") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if bounds and not bounds.contains(lat, lng):
                continue
            attrs = item.get("attributes") or {}
            match_type = str(attrs.get("Addr_type") or "")
            display_name = str(item.get("address") or attrs.get("Match_addr") or query)
            query_norm = _normalize(query)
            display_norm = _normalize(display_name)
            query_core = _place_query_core(query)
            # ArcGIS가 POI를 못 찾았을 때 도시 자체를 높은 점수 후보로 돌려주는 오탐을 제거한다.
            meaningfully_matched = query_norm in display_norm or (
                len(query_core) >= 2 and query_core in display_norm
            )
            if not meaningfully_matched:
                continue
            if match_type == "Locality" and query_core == _normalize(city_name):
                continue
            confidence = max(0.0, min(raw_score / 100 * _arcgis_type_weight(match_type), 1.0))
            results.append(
                {
                    "query": query,
                    "display_name": display_name,
                    "lat": lat,
                    "lng": lng,
                    "type": match_type,
                    "source": "arcgis",
                    "sources": ["arcgis"],
                    "confidence": confidence,
                    "storage_allowed": can_store,
                    "existing_marker_id": None,
                    "external_id": str(attrs.get("MatchID") or ""),
                    "source_url": "",
                }
            )
        return results

    return _cached(cache_key, load)


def _query_languages(query: str) -> list[str]:
    if re.search(r"[\uac00-\ud7a3]", query):
        return ["ko", "zh", "en"]
    if re.search(r"[\u3400-\u9fff]", query):
        return ["zh", "ko", "en"]
    return ["en", "zh", "ko"]


def _entity_label(entity: dict[str, Any], languages: list[str], fallback: str) -> str:
    labels = entity.get("labels") or {}
    for language in [*languages, "zh-hans", "zh-cn"]:
        if language in labels and labels[language].get("value"):
            return str(labels[language]["value"])
    return fallback


def _entity_description(entity: dict[str, Any], languages: list[str]) -> str:
    descriptions = entity.get("descriptions") or {}
    for language in [*languages, "zh-hans", "zh-cn"]:
        if language in descriptions and descriptions[language].get("value"):
            return str(descriptions[language]["value"])
    return ""


def _entity_coordinate(entity: dict[str, Any]) -> Optional[tuple[float, float]]:
    claims = entity.get("claims") or {}
    for statement in claims.get("P625") or []:
        try:
            value = statement["mainsnak"]["datavalue"]["value"]
            return float(value["latitude"]), float(value["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _search_wikidata(
    query: str,
    *,
    limit: int,
    bounds: Optional[Bounds],
) -> list[dict[str, Any]]:
    languages = _query_languages(query)
    cache_key = f"wikidata:{query}:{bounds}:{limit}"

    def load() -> list[dict[str, Any]]:
        ranked_ids: list[str] = []
        matched_names: dict[str, str] = {}
        for language in languages:
            data = _get_json(
                WIKIDATA_API_URL,
                {
                    "action": "wbsearchentities",
                    "search": query,
                    "language": language,
                    "uselang": language,
                    "type": "item",
                    "limit": min(max(limit, 3), 8),
                    "format": "json",
                },
                timeout=10,
            )
            for row in data.get("search", []) if isinstance(data, dict) else []:
                entity_id = str(row.get("id") or "")
                if entity_id and entity_id not in ranked_ids:
                    ranked_ids.append(entity_id)
                    matched_names[entity_id] = str(row.get("label") or row.get("match", {}).get("text") or query)
            if len(ranked_ids) >= max(limit * 2, 8):
                break
        if not ranked_ids:
            return []

        data = _get_json(
            WIKIDATA_API_URL,
            {
                "action": "wbgetentities",
                "ids": "|".join(ranked_ids[:20]),
                "props": "claims|labels|descriptions|aliases",
                "languages": "|".join(dict.fromkeys([*languages, "zh-hans", "zh-cn"])),
                "format": "json",
            },
            timeout=12,
        )
        entities = data.get("entities") or {}
        query_norm = _normalize(query)
        results: list[dict[str, Any]] = []
        for rank, entity_id in enumerate(ranked_ids):
            entity = entities.get(entity_id) or {}
            coordinate = _entity_coordinate(entity)
            if not coordinate:
                continue
            lat, lng = coordinate
            if bounds and not bounds.contains(lat, lng):
                continue
            label = _entity_label(entity, languages, matched_names.get(entity_id, query))
            description = _entity_description(entity, languages)
            if not _meaningful_entity_match(query, f"{label} {description}"):
                continue
            exact_bonus = 0.08 if _normalize(label) == query_norm else 0.0
            rank_score = max(0.0, 0.86 - rank * 0.025 + exact_bonus)
            results.append(
                {
                    "query": query,
                    "display_name": f"{label} · {description}" if description else label,
                    "lat": lat,
                    "lng": lng,
                    "type": "knowledge_place",
                    "source": "wikidata",
                    "sources": ["wikidata"],
                    "confidence": min(rank_score, 0.95),
                    "storage_allowed": True,
                    "existing_marker_id": None,
                    "external_id": entity_id,
                    "source_url": f"https://www.wikidata.org/wiki/{entity_id}",
                }
            )
            if len(results) >= limit:
                break
        return results

    return _cached(cache_key, load)


def _preferred_hit(group: list[dict[str, Any]]) -> dict[str, Any]:
    # 익명 ArcGIS 좌표·주소는 저장 가능한 후보가 함께 있을 때도 대표값으로 쓰지 않는다.
    priority = {"local": 0, "nominatim": 1, "wikidata": 2, "arcgis": 3}
    storable = [hit for hit in group if hit.get("storage_allowed")]
    pool = storable or group
    return min(pool, key=lambda hit: (priority.get(str(hit.get("source")), 9), -float(hit["confidence"])))


def _merge_hits(hits: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for hit in sorted(hits, key=lambda row: float(row.get("confidence") or 0), reverse=True):
        match: Optional[list[dict[str, Any]]] = None
        hit_name = _normalize(str(hit.get("display_name") or "").split(",")[0].split(" · ")[0])
        for group in groups:
            representative = group[0]
            distance = _haversine_m(hit, representative)
            representative_name = _normalize(
                str(representative.get("display_name") or "").split(",")[0].split(" · ")[0]
            )
            if distance <= 140 or (hit_name and hit_name == representative_name and distance <= 800):
                match = group
                break
        if match is None:
            groups.append([hit])
        else:
            match.append(hit)

    merged: list[dict[str, Any]] = []
    for group in groups:
        preferred = dict(_preferred_hit(group))
        sources = sorted(
            {str(hit.get("source") or "") for hit in group if hit.get("source")},
            key=lambda source: {"local": 0, "arcgis": 1, "nominatim": 2, "wikidata": 3}.get(source, 9),
        )
        existing_id = next((hit.get("existing_marker_id") for hit in group if hit.get("existing_marker_id")), None)
        wikidata_id = next(
            (hit.get("external_id") for hit in group if hit.get("source") == "wikidata" and hit.get("external_id")),
            None,
        )
        source_url = next((hit.get("source_url") for hit in group if hit.get("source_url")), "")
        score = min(1.0, max(float(hit.get("confidence") or 0) for hit in group) + 0.04 * (len(sources) - 1))
        preferred.update(
            {
                "source": sources[0] if len(sources) == 1 else "combined",
                "sources": sources,
                "confidence": round(score, 3),
                "confidence_label": _confidence_label(score, len(sources), existing_id),
                "storage_allowed": any(bool(hit.get("storage_allowed")) for hit in group),
                "existing_marker_id": int(existing_id) if existing_id else None,
                "external_id": str(wikidata_id or preferred.get("external_id") or ""),
                "source_url": str(source_url or ""),
            }
        )
        merged.append(preferred)

    merged.sort(
        key=lambda hit: (
            0 if hit.get("existing_marker_id") else 1,
            0 if hit.get("storage_allowed") else 1,
            -float(hit.get("confidence") or 0),
        )
    )
    return merged[: max(1, min(limit, 20))]


def search_address(
    query: str,
    limit: int = 8,
    *,
    viewbox: str = "",
    city_name: str = "",
    city_context: str = "",
    local_candidates: Optional[Iterable[dict[str, Any]]] = None,
    include_display_only: bool = False,
    arcgis_api_key: Optional[str] = None,
) -> list[dict[str, Any]]:
    """여러 공급자 후보를 도시 범위로 제한하고 중복 제거해 반환한다.

    ``include_display_only``는 익명 ArcGIS 후보처럼 DB에 바로 저장할 수 없는 위치를
    UI에 참고용으로 노출할 때만 사용한다. 에이전트와 공유 가져오기는 기본값(False)을
    유지해 저장 가능한 공개 데이터 후보만 받는다.
    """
    q = query.strip()
    if not q:
        return []
    limit = max(1, min(limit, 20))
    bounds = parse_viewbox(viewbox)
    api_key = (arcgis_api_key if arcgis_api_key is not None else os.environ.get("ARCGIS_API_KEY", "")).strip()

    all_hits = _local_hits(q, local_candidates or [], limit)
    providers: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
        (
            "nominatim",
            lambda: _search_nominatim(q, limit=limit, bounds=bounds, city_name=city_name),
        ),
        (
            "wikidata",
            lambda: _search_wikidata(q, limit=limit, bounds=bounds),
        ),
    ]
    if api_key or include_display_only:
        providers.append(
            (
                "arcgis",
                lambda: _search_arcgis(
                    q,
                    limit=limit,
                    bounds=bounds,
                    city_name=city_name,
                    city_context=city_context,
                    api_key=api_key,
                ),
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(providers)) as executor:
        futures = {executor.submit(loader): name for name, loader in providers}
        for future in concurrent.futures.as_completed(futures):
            provider = futures[future]
            try:
                all_hits.extend(future.result())
            except (TimeoutError, urllib.error.HTTPError, urllib.error.URLError, ValueError, KeyError) as exc:
                logger.warning("Geocode provider %s failed: %s", provider, exc)
            except Exception as exc:  # 공급자 하나의 장애가 전체 검색을 깨지 않게 한다.
                logger.warning("Unexpected geocode provider %s failure: %s", provider, exc)

    # Chinese POI searches often arrive as "city + brand + branch + street number".
    # Open data providers may index only the short storefront name, so retry a small,
    # deterministic set of progressively simpler variants before giving up.
    if not any(hit.get("storage_allowed") for hit in all_hits):
        without_city = q.replace(city_name, " ").strip() if city_name else q
        first_chunk = re.split(r"\s+", without_city, maxsplit=1)[0].strip()
        stripped_suffix = re.sub(r"(?:旗舰)?(?:分)?(?:店|馆|門店|门店)$", "", first_chunk).strip()
        variants: list[str] = []
        for variant in (without_city, first_chunk, stripped_suffix):
            if len(_normalize(variant)) >= 2 and variant != q and variant not in variants:
                variants.append(variant)
        for variant in variants[:3]:
            fallback_hits: list[dict[str, Any]] = []
            fallback_providers: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
                ("nominatim", lambda variant=variant: _search_nominatim(
                    variant, limit=limit, bounds=bounds, city_name=city_name
                )),
                ("wikidata", lambda variant=variant: _search_wikidata(
                    variant, limit=limit, bounds=bounds
                )),
            ]
            if api_key or include_display_only:
                fallback_providers.append(("arcgis", lambda variant=variant: _search_arcgis(
                    variant,
                    limit=limit,
                    bounds=bounds,
                    city_name=city_name,
                    city_context=city_context,
                    api_key=api_key,
                )))
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(fallback_providers)) as executor:
                futures = {executor.submit(loader): name for name, loader in fallback_providers}
                for future in concurrent.futures.as_completed(futures):
                    provider = futures[future]
                    try:
                        fallback_hits.extend(future.result())
                    except Exception as exc:
                        logger.warning("Fallback geocode provider %s failed: %s", provider, exc)
            for hit in fallback_hits:
                hit["matched_query"] = variant
                hit["query"] = q
            all_hits.extend(fallback_hits)
            if any(hit.get("storage_allowed") for hit in fallback_hits):
                break

    if not include_display_only:
        all_hits = [hit for hit in all_hits if hit.get("storage_allowed")]
    return _merge_hits(all_hits, limit)
