import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# 지난시 대략 범위 (Nominatim viewbox: left, top, right, bottom)
JINAN_VIEWBOX = "116.70,36.95,117.55,36.35"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "JinanTravelMap/0.1 (local travel notes; contact: local-dev)"


def search_address(query: str, limit: int = 6) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []

    params = urllib.parse.urlencode(
        {
            "q": q,
            "format": "json",
            "addressdetails": 1,
            "limit": max(1, min(limit, 10)),
            "viewbox": JINAN_VIEWBOX,
            "bounded": 0,  # 지난 우선, 결과 없으면 밖도 허용
            "accept-language": "ko,zh-CN,en",
        }
    )
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
