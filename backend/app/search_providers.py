"""Optional structured search providers with inspectable failure outcomes."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


BRAVE_PLACE_URL = "https://api.search.brave.com/res/v1/local/place_search"


_COUNTRY_PROFILES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    # alpha-3, HTTP language preference, Wikidata languages, official host suffixes
    "CN": ("CHN", ("zh-CN", "zh", "ko", "en"), ("zh", "en", "ko"), (".gov.cn",)),
    "KR": ("KOR", ("ko", "en"), ("ko", "en"), (".go.kr",)),
    "JP": ("JPN", ("ja", "en", "ko"), ("ja", "en", "ko"), (".go.jp",)),
    "US": ("USA", ("en",), ("en",), (".gov",)),
    "CA": ("CAN", ("en", "fr"), ("en", "fr"), (".gc.ca",)),
    "GB": ("GBR", ("en",), ("en",), (".gov.uk",)),
    "FR": ("FRA", ("fr", "en"), ("fr", "en"), (".gouv.fr",)),
    "DE": ("DEU", ("de", "en"), ("de", "en"), (".bund.de",)),
    "IT": ("ITA", ("it", "en"), ("it", "en"), (".gov.it",)),
    "ES": ("ESP", ("es", "en"), ("es", "en"), (".gob.es",)),
    "AU": ("AUS", ("en",), ("en",), (".gov.au",)),
    "NZ": ("NZL", ("en",), ("en",), (".govt.nz",)),
    "SG": ("SGP", ("en", "zh"), ("en", "zh"), (".gov.sg",)),
    "TH": ("THA", ("th", "en"), ("th", "en"), (".go.th",)),
    "VN": ("VNM", ("vi", "en"), ("vi", "en"), (".gov.vn",)),
}

_CITY_PROFILES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "jinan": (
        ("济南", "济南市", "지난", "jinan"),
        ("jinan.gov.cn", "jn.gov.cn", "shandong.gov.cn", "sd.gov.cn"),
    ),
    "shenyang": (
        ("沈阳", "沈阳市", "선양", "심양", "shenyang"),
        ("shenyang.gov.cn", "ln.gov.cn"),
    ),
}

_CITY_RELEVANCE_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "jinan": (
        ("宽厚里", "관후리", "kuanhouli"),
        ("芙蓉街", "푸룽제", "furong street", "furongjie"),
        ("泉城路", "취안청루", "quancheng road", "quanchenglu"),
    ),
    "shenyang": (("中街", "중제", "zhongjie"),),
}


def _normalized_country_code(value: str) -> str:
    code = str(value or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else "CN"


def _dedupe_strings(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return tuple(out)


@dataclass(frozen=True)
class SearchProviderProfile:
    """Provider parameters and relevance hints derived from a destination city.

    The profile contains no provider credentials and is safe to construct in API,
    chat, and batch paths. Unknown countries remain usable: Brave/Nominatim receive
    their ISO alpha-2 code, while ArcGIS simply omits an unsupported alpha-3 filter.
    """

    country_code: str
    arcgis_country_code: str
    language_tags: tuple[str, ...]
    wikidata_languages: tuple[str, ...]
    city_aliases: tuple[str, ...]
    official_domains: tuple[str, ...]
    official_host_suffixes: tuple[str, ...]
    local_relevance_groups: tuple[tuple[str, ...], ...]


def build_search_provider_profile(
    *,
    country_code: str = "CN",
    city_slug: str = "",
    city_name: str = "",
    city_name_ko: str = "",
) -> SearchProviderProfile:
    """Build one normalized provider profile without hard-coding a single city path."""

    alpha2 = _normalized_country_code(country_code)
    alpha3, language_tags, wikidata_languages, official_suffixes = _COUNTRY_PROFILES.get(
        alpha2,
        ("", ("en",), ("en",), ()),
    )
    city_key = str(city_slug or "").strip().casefold()
    known_aliases, official_domains = _CITY_PROFILES.get(city_key, ((), ()))
    aliases = _dedupe_strings([
        city_name,
        city_name[:-1] if city_name.endswith("市") else "",
        city_name_ko,
        city_slug,
        *known_aliases,
    ])
    return SearchProviderProfile(
        country_code=alpha2,
        arcgis_country_code=alpha3,
        language_tags=language_tags,
        wikidata_languages=wikidata_languages,
        city_aliases=aliases,
        official_domains=official_domains,
        official_host_suffixes=official_suffixes,
        local_relevance_groups=_CITY_RELEVANCE_GROUPS.get(city_key, ()),
    )


def _failure_kind(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in {401, 403}:
        return "authorization_or_plan"
    if status >= 500:
        return "provider_unavailable"
    return "http_error"


def search_brave_places(
    *,
    api_key: str,
    query: str,
    latitude: float,
    longitude: float,
    count: int = 10,
    radius_m: float = 20_000,
    city_bounds: tuple[float, float, float, float] | None = None,
    country_code: str = "CN",
    storage_allowed: bool = False,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Search Brave Place and normalize POIs without persisting ephemeral IDs.

    ``city_bounds`` is ``(south, west, north, east)``. Brave radius is a bias,
    so every result is hard-filtered locally when bounds are available.
    """

    if not api_key.strip():
        return {"status": "skipped_no_key", "results": [], "provider": "brave_place"}
    params = urllib.parse.urlencode({
        "q": query,
        "latitude": float(latitude),
        "longitude": float(longitude),
        "radius": max(0.0, float(radius_m)),
        "count": max(1, min(int(count), 100)),
        "country": _normalized_country_code(country_code),
        "units": "metric",
        "safesearch": "strict",
        "spellcheck": "false",
    })
    request = urllib.request.Request(
        f"{BRAVE_PLACE_URL}?{params}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key.strip(),
            "User-Agent": "WONRAE-TravelMap/1.0",
        },
    )
    payload: dict[str, Any] | None = None
    retries = 0
    for attempt in range(2):
        try:
            with opener(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                retry_after = str(exc.headers.get("Retry-After") or "1")
                try:
                    delay = max(0.0, min(float(retry_after), 2.0))
                except ValueError:
                    delay = 1.0
                retries += 1
                sleeper(delay)
                continue
            return {
                "status": "error",
                "error": _failure_kind(exc.code),
                "http_status": exc.code,
                "retries": retries,
                "results": [],
                "provider": "brave_place",
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return {
                "status": "error",
                "error": "timeout" if "timed out" in str(exc).casefold() else "network_or_parse",
                "detail": str(exc)[:180],
                "retries": retries,
                "results": [],
                "provider": "brave_place",
            }
    if not isinstance(payload, dict):
        return {"status": "error", "error": "empty_response", "results": [], "provider": "brave_place"}

    results: list[dict[str, Any]] = []
    outside_city = 0
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        coordinates = raw.get("coordinates") or []
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            lat, lng = float(coordinates[0]), float(coordinates[1])
        except (TypeError, ValueError):
            continue
        if city_bounds is not None:
            south, west, north, east = city_bounds
            if not (south <= lat <= north and west <= lng <= east):
                outside_city += 1
                continue
        postal = raw.get("postal_address") if isinstance(raw.get("postal_address"), dict) else {}
        provider_url = str(raw.get("provider_url") or raw.get("url") or "")
        results.append({
            "display_name": str(raw.get("title") or "")[:300],
            "address": str(postal.get("displayAddress") or postal.get("streetAddress") or "")[:300],
            "lat": lat,
            "lng": lng,
            "source": "brave_place",
            "source_url": provider_url[:1000],
            # Brave IDs expire in roughly eight hours. Expose them only as a
            # transient trace field; never as coordinate_external_id.
            "transient_id": str(raw.get("id") or "")[:300],
            "external_id": "",
            "categories": [str(item)[:100] for item in (raw.get("categories") or [])[:10]],
            "confidence": 0.78,
            "storage_allowed": bool(storage_allowed),
            "requires_cross_verification": not bool(storage_allowed),
        })
    return {
        "status": "ok",
        "provider": "brave_place",
        "results": results,
        "raw_count": len(payload.get("results") or []),
        "outside_city_count": outside_city,
        "retries": retries,
        "storage_allowed": bool(storage_allowed),
    }


__all__ = [
    "BRAVE_PLACE_URL",
    "SearchProviderProfile",
    "build_search_provider_profile",
    "search_brave_places",
]
