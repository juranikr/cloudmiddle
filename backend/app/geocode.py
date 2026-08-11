import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "CloudmiddleTravelMap/0.2 (local travel notes; contact: local-dev)"


def search_address(
    query: str,
    limit: int = 6,
    *,
    viewbox: str = "",
    city_name: str = "",
) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []

    search_query = f"{q}, {city_name}" if city_name and city_name not in q else q
    values: dict[str, Any] = {
            "q": search_query,
            "format": "json",
            "addressdetails": 1,
            "limit": max(1, min(limit, 10)),
            "bounded": 0,
            "accept-language": "ko,zh-CN,en",
    }
    if viewbox:
        values["viewbox"] = viewbox
    params = urllib.parse.urlencode(values)
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"지오코딩 서버 오류 ({exc.code})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("지오코딩 서버에 연결하지 못했습니다") from exc

    results: list[dict[str, Any]] = []
    for item in data:
        try:
            results.append(
                {
                    "display_name": item.get("display_name") or q,
                    "lat": float(item["lat"]),
                    "lng": float(item["lon"]),
                    "type": item.get("type") or item.get("class") or "",
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return results
