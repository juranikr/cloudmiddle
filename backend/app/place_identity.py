"""Pure place-identity helpers for branch-aware candidate reconciliation.

The research agents see the same business through SEO article titles, map
providers, and travel-detail pages.  Identity must therefore be stricter than a
title prefix while still tolerating harmless presentation differences.  This
module deliberately has no database or network dependency so chat, batch, and
import paths can share exactly the same decision rules.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


_TEXT_RE = re.compile(r"[^0-9a-z\u3400-\u9fff\uac00-\ud7a3]+")
_TRANSLATION_GROUP_RE = re.compile(r"[（(]([^）)]*)[）)]")
_BRANCH_HINT_RE = re.compile(
    r"(?:店|分店|旗舰店|总店|館|馆|区|座|楼|机场|车站|站|广场|商场|"
    r"购物中心|大悦城|万象城|天地|世界)$",
    re.IGNORECASE,
)
_ADMIN_PREFIX_RE = re.compile(r"^[\u3400-\u9fff]{2,12}(?:省|市|自治区|区|县)")
_ROAD_NUMBER_RE = re.compile(
    r"([0-9a-z\u3400-\u9fff]{1,16}(?:大街|大道|路|街|巷|道))\s*"
    r"(\d+[0-9a-z-]*)\s*[号號]",
    re.IGNORECASE,
)
_ENGLISH_ROAD_NUMBER_RE = re.compile(
    r"(?:no\.?\s*(\d+[0-9a-z-]*)\s*[, ]*)?"
    r"([0-9a-z ]{2,30}(?:road|street|avenue|lane))"
    r"(?:\s+no\.?\s*(\d+[0-9a-z-]*))?",
    re.IGNORECASE,
)
_BRANCH_SECTION_RE = re.compile(r"([0-9a-z]+)(?:馆|館|区|座|楼)$", re.IGNORECASE)
_MARKETING_PREFIX_RE = re.compile(r"^(?:必吃|推荐|探店|打卡|攻略|人气)[:：\s]*")


@dataclass(frozen=True)
class PlaceIdentityInput:
    """Minimal provider-neutral facts needed to reason about a place."""

    city: str
    title: str
    chain_name: str = ""
    branch_name: str = ""
    address: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None


@dataclass(frozen=True)
class PlaceIdentity:
    """Normalized identity components; ``canonical_key`` is deterministic."""

    city_key: str
    entity_key: str
    branch_key: str
    address_key: str
    canonical_key: str


@dataclass(frozen=True)
class IdentityDecision:
    """Inspectable reconciliation outcome for audit logs and tests."""

    same: bool
    reason: str
    confidence: float
    distance_m: Optional[float] = None


def normalize_place_text(value: str) -> str:
    """Normalize punctuation and width without transliterating local names."""

    folded = unicodedata.normalize("NFKC", value or "").casefold()
    return _TEXT_RE.sub("", folded)


def _strip_admin_prefixes(value: str) -> str:
    result = unicodedata.normalize("NFKC", value or "").strip()
    while True:
        updated = _ADMIN_PREFIX_RE.sub("", result, count=1).strip()
        if updated == result:
            return result
        result = updated


def _road_number_key(address: str) -> str:
    compact = _strip_admin_prefixes(address)
    match = _ROAD_NUMBER_RE.search(compact)
    if match:
        return normalize_place_text("".join(match.groups()))
    english = _ENGLISH_ROAD_NUMBER_RE.search(compact)
    if english:
        before, road, after = english.groups()
        number = before or after or ""
        if number:
            return normalize_place_text(f"{road}{number}")
    return ""


def _clean_title(value: str) -> str:
    title = unicodedata.normalize("NFKC", value or "").strip()
    bracket = re.match(r"^[【\[]([^】\]]{2,160})[】\]]", title)
    if bracket:
        title = bracket.group(1).strip()
    if "！" in title or "!" in title:
        title = re.split(r"[！!]", title)[-1].strip()
    title = _MARKETING_PREFIX_RE.sub("", title)
    title = re.split(
        r"\s*(?:[,，｜|]|_电话|电话_地址|地址_价格|[-—_]\s*(?:电话|地址|攻略))\s*",
        title,
        maxsplit=1,
    )[0].strip()
    # Korean/English display translations are not part of the local identity.
    title = _TRANSLATION_GROUP_RE.sub(
        lambda match: match.group(0)
        if _BRANCH_HINT_RE.search(match.group(1).strip())
        else "",
        title,
    ).strip()
    return title


def _inferred_branch(title: str) -> str:
    for match in _TRANSLATION_GROUP_RE.finditer(unicodedata.normalize("NFKC", title or "")):
        value = match.group(1).strip()
        if _BRANCH_HINT_RE.search(value):
            return value
    return ""


def _entity_key(value: PlaceIdentityInput) -> str:
    explicit = normalize_place_text(value.chain_name)
    if explicit:
        return explicit
    cleaned = _clean_title(value.title)
    # A parenthetical branch belongs to branch identity, not to the brand name.
    cleaned = _TRANSLATION_GROUP_RE.sub("", cleaned)
    return normalize_place_text(cleaned)


def _branch_key(value: PlaceIdentityInput, city_key: str) -> str:
    branch = value.branch_name.strip() or _inferred_branch(value.title)
    normalized = normalize_place_text(branch)
    if city_key and normalized.startswith(city_key) and len(normalized) > len(city_key) + 1:
        normalized = normalized[len(city_key):]
    normalized = re.sub(r"(?:旗舰店|分店|門店|门店|店)$", "", normalized)
    return normalized


def _coordinate_bucket(value: PlaceIdentityInput) -> str:
    try:
        lat, lng = float(value.lat), float(value.lng)
    except (TypeError, ValueError):
        return ""
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return ""
    # Roughly eleven metres at this precision. It is only a last-resort identity
    # component; fuzzy reconciliation below handles neighbouring bucket edges.
    return f"{lat:.4f},{lng:.4f}"


def canonical_place_identity(value: PlaceIdentityInput) -> PlaceIdentity:
    """Build a stable identity, preferring branch/address over coordinate bins."""

    city_key = normalize_place_text(value.city)
    entity_key = _entity_key(value)
    branch_key = _branch_key(value, city_key)
    address_key = _road_number_key(value.address)
    discriminator = branch_key or address_key or _coordinate_bucket(value)
    raw = "|".join((city_key, entity_key, discriminator))
    canonical_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return PlaceIdentity(
        city_key=city_key,
        entity_key=entity_key,
        branch_key=branch_key,
        address_key=address_key,
        canonical_key=canonical_key,
    )


def _distance_m(left: PlaceIdentityInput, right: PlaceIdentityInput) -> Optional[float]:
    try:
        lat1, lng1 = float(left.lat), float(left.lng)
        lat2, lng2 = float(right.lat), float(right.lng)
    except (TypeError, ValueError):
        return None
    if not all((-90 <= lat <= 90 and -180 <= lng <= 180) for lat, lng in ((lat1, lng1), (lat2, lng2))):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6_371_000 * 2 * math.asin(math.sqrt(a))


def _entities_compatible(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return min(len(left), len(right)) >= 4 and (left in right or right in left)


def _branches_compatible(left: str, right: str) -> Optional[bool]:
    """Return None when branch evidence is absent, else agreement/conflict."""

    if not left or not right:
        return None
    if left == right or (min(len(left), len(right)) >= 3 and (left in right or right in left)):
        return True
    left_section = _BRANCH_SECTION_RE.search(left)
    right_section = _BRANCH_SECTION_RE.search(right)
    if left_section and right_section and left_section.group(1) != right_section.group(1):
        return False
    return False


def same_place_candidate(
    left: PlaceIdentityInput,
    right: PlaceIdentityInput,
) -> IdentityDecision:
    """Decide whether two observations describe one physical branch.

    Explicitly conflicting branch or street-number evidence always wins over
    geographic proximity. This is what prevents two branches in one mall area
    from disappearing into a title-prefix cluster.
    """

    a = canonical_place_identity(left)
    b = canonical_place_identity(right)
    distance = _distance_m(left, right)

    if not a.city_key or a.city_key != b.city_key:
        return IdentityDecision(False, "city_mismatch", 0.99, distance)
    if not _entities_compatible(a.entity_key, b.entity_key):
        return IdentityDecision(False, "business_mismatch", 0.98, distance)

    branch_match = _branches_compatible(a.branch_key, b.branch_key)
    if branch_match is False:
        return IdentityDecision(False, "branch_mismatch", 0.99, distance)
    if a.address_key and b.address_key and a.address_key != b.address_key:
        return IdentityDecision(False, "address_mismatch", 0.97, distance)
    if distance is not None and distance > 800:
        return IdentityDecision(False, "coordinate_conflict", 0.96, distance)

    if branch_match is True:
        confidence = 0.98 if a.address_key and a.address_key == b.address_key else 0.93
        return IdentityDecision(True, "same_branch", confidence, distance)
    if a.address_key and a.address_key == b.address_key:
        return IdentityDecision(True, "same_business_address", 0.96, distance)
    if distance is not None and distance <= 80:
        return IdentityDecision(True, "same_business_nearby", 0.86, distance)
    if a.canonical_key == b.canonical_key and bool(a.entity_key):
        return IdentityDecision(True, "same_canonical_identity", 0.84, distance)
    return IdentityDecision(False, "insufficient_branch_evidence", 0.7, distance)


__all__ = [
    "IdentityDecision",
    "PlaceIdentity",
    "PlaceIdentityInput",
    "canonical_place_identity",
    "normalize_place_text",
    "same_place_candidate",
]
