"""Pure integrity checks for proposed places and China-local POI evidence.

The agent, chat, and approval paths can all call this module without a database
session.  It deliberately distinguishes missing evidence from contradictory
evidence: missing fields generally become warnings, while explicit conflicts
block the proposal or keep it in a verification quarantine.
"""

from __future__ import annotations

import math
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional


_ADMIN_TRANSLATION = str.maketrans(
    {
        "號": "号",
        "區": "区",
        "縣": "县",
        "樓": "楼",
        "館": "馆",
        "門": "门",
        "瀋": "沈",
        "陽": "阳",
        "東": "东",
        "鐵": "铁",
        "總": "总",
        "廣": "广",
        "場": "场",
    }
)
_CITY_NOISE = (
    "中华人民共和国",
    "中国",
    "辽宁省",
    "山东省",
    "沈阳市",
    "沈阳",
    "济南市",
    "济南",
    "shenyang",
    "jinan",
)
_SEO_PREFIX_RE = re.compile(r"^(?:必吃|推荐|探店|打卡|攻略)[:：\s]*", re.IGNORECASE)
_DISTRICT_RE = re.compile(r"([^省市区县]{1,8}(?:区|县))")
_ROAD_RE = re.compile(
    r"([0-9A-Za-z\u3400-\u9fff]{1,18}?(?:大道|大街|公路|胡同|路|街|巷))"
)
_HOUSE_RE = re.compile(r"(\d+(?:[-－—之]\d+)?号)")
_ADDRESS_LABEL_RE = re.compile(
    r"(?:주소|地址)\s*[:：]\s*([^\n。；;]{4,220})",
    re.IGNORECASE,
)
_LANDMARK_SUFFIXES = (
    "购物中心",
    "大悦城",
    "广场",
    "商场",
    "大厦",
    "中心",
    "公园",
    "博物馆",
    "机场",
    "车站",
)
_BRANCH_SUFFIX_RE = re.compile(r"(?:旗舰店|总店|分店|门店|店|점)$", re.IGNORECASE)


@dataclass(frozen=True)
class IntegrityReport:
    """A stable result that is easy to persist in an approval proposal."""

    ok: bool
    error: str = ""
    warnings: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChinaAddressTokens:
    normalized: str
    districts: frozenset[str] = frozenset()
    roads: frozenset[str] = frozenset()
    house_numbers: frozenset[str] = frozenset()
    landmarks: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PlaceIdentity:
    brand: str
    branches: frozenset[str] = frozenset()


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_ADMIN_TRANSLATION)
    return re.sub(r"[^0-9a-z\u3400-\u9fff\uac00-\ud7a3]+", "", text.casefold())


def _without_city_noise(value: str) -> str:
    compact = _compact(value)
    for token in _CITY_NOISE:
        compact = compact.replace(_compact(token), "")
    return compact


def _script_family(value: str) -> str:
    has_han = bool(re.search(r"[\u3400-\u9fff]", value))
    has_hangul = bool(re.search(r"[\uac00-\ud7a3]", value))
    has_latin = bool(re.search(r"[a-z]", value, re.IGNORECASE))
    if sum((has_han, has_hangul, has_latin)) != 1:
        return "mixed"
    if has_han:
        return "han"
    if has_hangul:
        return "hangul"
    return "latin"


def _canonical_road(value: str) -> str:
    compact = _compact(value)
    return re.sub(r"^(?:辽宁省|山东省|沈阳市|济南市|沈阳|济南)", "", compact)


def _canonical_landmark(value: str) -> str:
    compact = _without_city_noise(value)
    return re.sub(r"(?:a|b|c|d)?(?:馆|座)?(?:\d+楼|\d+层)?$", "", compact)


def normalize_china_address(value: str) -> ChinaAddressTokens:
    """Tokenize comparable Chinese address facts without requiring full equality.

    Missing administrative prefixes and floor/unit wording are intentionally
    ignored.  Only explicit tokens are later considered for contradictions.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).translate(_ADMIN_TRANSLATION)
    text = re.sub(r"\s+", "", text)
    districts = frozenset(_compact(item) for item in _DISTRICT_RE.findall(text))

    # Administrative units otherwise become a greedy part of a road token.
    road_text = re.sub(r"[\u3400-\u9fff]{1,10}(?:省|市|区|县)", "|", text)
    roads = frozenset(
        road
        for item in _ROAD_RE.findall(road_text)
        if (road := _canonical_road(item))
    )
    house_numbers = frozenset(
        _compact(item).replace("－", "-").replace("—", "-").replace("之", "-")
        for item in _HOUSE_RE.findall(text)
    )

    landmarks: set[str] = set()
    segments = re.split(r"[|,，。；;、()（）\[\]【】]", road_text)
    for segment in segments:
        for suffix in _LANDMARK_SUFFIXES:
            position = segment.find(suffix)
            if position < 0:
                continue
            start = max(0, position - 12)
            candidate = segment[start : position + len(suffix)]
            candidate = re.sub(r"^.*(?:号|路|街|巷)", "", candidate)
            normalized = _canonical_landmark(candidate)
            if len(normalized) >= 3:
                landmarks.add(normalized)

    return ChinaAddressTokens(
        normalized=_compact(text),
        districts=districts,
        roads=roads,
        house_numbers=house_numbers,
        landmarks=frozenset(landmarks),
    )


def compare_china_addresses(left: str, right: str) -> IntegrityReport:
    """Report only explicit address contradictions, never mere omissions."""

    a = normalize_china_address(left)
    b = normalize_china_address(right)
    errors: list[str] = []
    if a.districts and b.districts and a.districts.isdisjoint(b.districts):
        errors.append("address_district_mismatch")
    if a.roads and b.roads and a.roads.isdisjoint(b.roads):
        errors.append("address_road_mismatch")
    same_location_axis = bool(
        (a.roads and b.roads and not a.roads.isdisjoint(b.roads))
        or (a.landmarks and b.landmarks and not a.landmarks.isdisjoint(b.landmarks))
    )
    if (
        same_location_axis
        and a.house_numbers
        and b.house_numbers
        and a.house_numbers.isdisjoint(b.house_numbers)
    ):
        errors.append("address_house_number_mismatch")
    if a.landmarks and b.landmarks and a.landmarks.isdisjoint(b.landmarks):
        errors.append("address_landmark_mismatch")

    details = {
        "errors": errors,
        "left": {
            "districts": sorted(a.districts),
            "roads": sorted(a.roads),
            "house_numbers": sorted(a.house_numbers),
            "landmarks": sorted(a.landmarks),
        },
        "right": {
            "districts": sorted(b.districts),
            "roads": sorted(b.roads),
            "house_numbers": sorted(b.house_numbers),
            "landmarks": sorted(b.landmarks),
        },
    }
    return IntegrityReport(ok=not errors, error=errors[0] if errors else "", details=details)


def _clean_business_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_ADMIN_TRANSLATION).strip()
    if "！" in text or "!" in text:
        text = re.split(r"[！!]", text)[-1]
    text = _SEO_PREFIX_RE.sub("", text)
    text = re.split(r"[，,｜|_\n]", text, maxsplit=1)[0]
    return text.strip()


def _identity(value: str, branch_name: str = "") -> PlaceIdentity:
    text = _clean_business_title(value)
    parenthetical = re.findall(r"[（(]([^（）()]{1,80})[）)]", text)
    local = re.split(r"[（(]", text, maxsplit=1)[0]
    brand = _without_city_noise(local)

    branches: set[str] = set()
    for raw in [branch_name, *parenthetical]:
        candidate = _without_city_noise(raw)
        if not candidate or not _BRANCH_SUFFIX_RE.search(candidate):
            continue
        candidate = _BRANCH_SUFFIX_RE.sub("", candidate)
        if len(candidate) >= 2:
            branches.add(candidate)
    return PlaceIdentity(brand=brand, branches=frozenset(branches))


def _brand_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    shortest = min(len(left), len(right))
    return shortest >= 3 and (left in right or right in left)


def compare_place_identity(
    proposed_title: str,
    evidence_title: str,
    *,
    proposed_branch: str = "",
    evidence_branch: str = "",
) -> IntegrityReport:
    """Compare a business identity and reject a confirmed different branch."""

    proposed = _identity(proposed_title, proposed_branch)
    evidence = _identity(evidence_title, evidence_branch)
    details: dict[str, Any] = {
        "proposed_brand": proposed.brand,
        "evidence_brand": evidence.brand,
        "proposed_branches": sorted(proposed.branches),
        "evidence_branches": sorted(evidence.branches),
    }
    if not proposed.brand or not evidence.brand:
        return IntegrityReport(
            ok=True,
            warnings=("place_identity_missing",),
            details=details,
        )
    if not _brand_matches(proposed.brand, evidence.brand):
        return IntegrityReport(ok=False, error="place_identity_mismatch", details=details)

    warnings: list[str] = []
    if proposed.branches and evidence.branches:
        branch_match = any(
            left == right or (min(len(left), len(right)) >= 3 and (left in right or right in left))
            for left in proposed.branches
            for right in evidence.branches
        )
        if not branch_match:
            families = {
                (_script_family(left), _script_family(right))
                for left in proposed.branches
                for right in evidence.branches
            }
            comparable = any(left == right and left != "mixed" for left, right in families)
            if comparable:
                return IntegrityReport(ok=False, error="branch_identity_mismatch", details=details)
            warnings.append("branch_identity_unresolved")
    return IntegrityReport(ok=True, warnings=tuple(warnings), details=details)


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _float_coordinate(row: Any, key: str) -> Optional[float]:
    try:
        value = float(_value(row, key))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)


def _viewbox_bounds(value: str) -> Optional[tuple[float, float, float, float]]:
    try:
        numbers = [float(item.strip()) for item in str(value or "").split(",")]
    except ValueError:
        return None
    if len(numbers) != 4 or not all(math.isfinite(item) for item in numbers):
        return None
    first_lng, first_lat, second_lng, second_lat = numbers
    return (
        min(first_lat, second_lat),
        min(first_lng, second_lng),
        max(first_lat, second_lat),
        max(first_lng, second_lng),
    )


def _extract_payload_address(payload: Mapping[str, Any]) -> str:
    direct = str(payload.get("address") or "").strip()
    if direct:
        return direct
    values = [str(payload.get("description") or "")]
    for item in payload.get("insights") or []:
        if isinstance(item, Mapping) and str(item.get("kind") or "").casefold() == "location":
            values.append(str(item.get("content") or ""))
    for value in values:
        match = _ADDRESS_LABEL_RE.search(value)
        if match:
            return match.group(1).strip(" ,，")
    return ""


def _anchor_variants(anchor: Any) -> list[str]:
    values = [str(_value(anchor, "title", "") or "")]
    aliases = _value(anchor, "aliases", ()) or ()
    if isinstance(aliases, str):
        aliases = re.split(r"[,，|]", aliases)
    values.extend(str(item) for item in aliases)
    variants: list[str] = []
    for value in values:
        local = re.split(r"[（(]", value, maxsplit=1)[0]
        compact = _without_city_noise(local)
        if len(compact) >= 3 and compact not in variants:
            variants.append(compact)
    return variants


def assess_new_place(
    payload: Mapping[str, Any],
    *,
    city_viewbox: str = "",
    coordinate_evidence: Optional[Mapping[str, Any]] = None,
    anchors: Iterable[Any] = (),
    evidence_max_distance_m: float = 300.0,
    anchor_max_distance_m: float = 1_500.0,
) -> IntegrityReport:
    """Assess a proposed point without mutating a model or database session."""

    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}

    lat = _float_coordinate(payload, "lat")
    lng = _float_coordinate(payload, "lng")
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        errors.append("invalid_coordinate")
    else:
        details["coordinate"] = {"lat": lat, "lng": lng}

    source = str(payload.get("coordinate_source") or "agent_research").strip().casefold()
    source_url = str(payload.get("coordinate_source_url") or "").strip()
    external_id = str(payload.get("coordinate_external_id") or "").strip()
    if coordinate_evidence:
        source_url = source_url or str(coordinate_evidence.get("source_url") or "").strip()
        external_id = external_id or str(coordinate_evidence.get("external_id") or "").strip()
    if source_url and not _valid_http_url(source_url):
        errors.append("invalid_coordinate_source_url")
    if source in {"", "agent_research", "unknown"} and not source_url and not external_id:
        errors.append("coordinate_evidence_required")
    details["coordinate_provenance"] = {
        "source": source,
        "source_url": source_url,
        "external_id": external_id,
    }

    bounds = _viewbox_bounds(city_viewbox)
    if city_viewbox and bounds is None:
        warnings.append("invalid_city_viewbox")
    elif bounds is not None and lat is not None and lng is not None:
        south, west, north, east = bounds
        details["city_bounds"] = {
            "south": south,
            "west": west,
            "north": north,
            "east": east,
        }
        if not (south <= lat <= north and west <= lng <= east):
            errors.append("coordinate_outside_city")

    proposed_address = _extract_payload_address(payload)
    details["proposed_address"] = proposed_address

    if coordinate_evidence:
        if coordinate_evidence.get("storage_allowed") is not True:
            errors.append("coordinate_storage_not_allowed")
        evidence_lat = _float_coordinate(coordinate_evidence, "lat")
        evidence_lng = _float_coordinate(coordinate_evidence, "lng")
        if (
            lat is not None
            and lng is not None
            and evidence_lat is not None
            and evidence_lng is not None
        ):
            distance = _haversine_m(lat, lng, evidence_lat, evidence_lng)
            details["coordinate_evidence_distance_m"] = round(distance, 1)
            if distance > evidence_max_distance_m:
                errors.append("coordinate_not_grounded")
        else:
            errors.append("coordinate_evidence_missing_point")

        evidence_title = str(
            coordinate_evidence.get("display_name")
            or coordinate_evidence.get("title")
            or ""
        )
        identity = compare_place_identity(
            str(payload.get("title") or ""),
            evidence_title,
            proposed_branch=str(payload.get("branch_name") or ""),
            evidence_branch=str(coordinate_evidence.get("branch_name") or ""),
        )
        details["identity"] = identity.details
        if not identity.ok:
            errors.append(identity.error)
        warnings.extend(identity.warnings)

        evidence_address = str(coordinate_evidence.get("address") or "").strip()
        details["evidence_address"] = evidence_address
        if proposed_address and evidence_address:
            address = compare_china_addresses(proposed_address, evidence_address)
            details["address"] = address.details
            if not address.ok:
                errors.extend(address.details.get("errors") or [address.error])
    elif source_url or external_id:
        warnings.append("coordinate_identity_unchecked")

    if lat is not None and lng is not None:
        claim_text = _without_city_noise(
            " ".join(
                str(value or "")
                for value in (
                    payload.get("title"),
                    proposed_address,
                    payload.get("description"),
                )
            )
        )
        anchor_checks: list[dict[str, Any]] = []
        for anchor in anchors:
            variants = _anchor_variants(anchor)
            matched = next((item for item in variants if item in claim_text), "")
            if not matched:
                continue
            anchor_lat = _float_coordinate(anchor, "lat")
            anchor_lng = _float_coordinate(anchor, "lng")
            if anchor_lat is None or anchor_lng is None:
                warnings.append("invalid_landmark_anchor")
                continue
            distance = _haversine_m(lat, lng, anchor_lat, anchor_lng)
            limit = float(_value(anchor, "max_distance_m", anchor_max_distance_m))
            anchor_checks.append(
                {
                    "id": _value(anchor, "id"),
                    "title": str(_value(anchor, "title", "")),
                    "matched": matched,
                    "distance_m": round(distance, 1),
                    "max_distance_m": limit,
                }
            )
            if distance > limit:
                errors.append("landmark_anchor_mismatch")
        if anchor_checks:
            details["anchor_checks"] = anchor_checks

    # Preserve order for deterministic API output while avoiding duplicate codes.
    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))
    details["errors"] = errors
    return IntegrityReport(
        ok=not errors,
        error=errors[0] if errors else "",
        warnings=tuple(warnings),
        details=details,
    )
