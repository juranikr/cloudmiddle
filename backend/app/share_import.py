"""따종/고덕 공유 텍스트·단축 URL → 제목/주소/좌표 추출."""

from __future__ import annotations

import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from app.gcj02 import gcj02_to_wgs84
from app.geocode import search_address

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

AMAP_URL_RE = re.compile(
    r"https?://(?:surl\.amap\.com|www\.amap\.com|m\.amap\.com|ditu\.amap\.com)/\S+",
    re.I,
)
DP_URL_RE = re.compile(
    r"https?://(?:dpurl\.cn|www\.dianping\.com|m\.dianping\.com)/\S+",
    re.I,
)
TITLE_BRACKET_RE = re.compile(r"【([^】]+)】")
RATING_RE = re.compile(r"★+[☆★]*\s*([0-9.]+)")
PRICE_RE = re.compile(r"¥\s*([0-9.]+)\s*/\s*人")


@dataclass
class ShareImportResult:
    source: str  # amap | dianping | unknown
    title: str
    description: str
    address: str
    source_url: str
    lat: Optional[float]
    lng: Optional[float]
    category_hint: str  # restaurant | other | ...
    needs_map_pick: bool
    note: str


def looks_like_share_text(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if AMAP_URL_RE.search(t) or DP_URL_RE.search(t):
        return True
    if "【" in t and "】" in t and ("¥" in t or "★" in t or "dianping" in t.lower()):
        return True
    return False


def import_share_text(text: str) -> ShareImportResult:
    raw = text.strip()
    if not raw:
        raise ValueError("붙여넣을 내용이 없습니다")

    amap_url = _first_match(AMAP_URL_RE, raw)
    dp_url = _first_match(DP_URL_RE, raw)

    if amap_url or "surl.amap.com" in raw.lower() or "amap.com" in raw.lower():
        url = amap_url or raw.split()[0]
        return _import_amap(url, raw)

    if dp_url or "【" in raw:
        return _import_dianping(raw, dp_url)

    raise ValueError("따종·고덕 공유 텍스트/링크를 인식하지 못했습니다")


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(0).rstrip(".,);]") if m else ""


def _follow_redirects(url: str, max_hops: int = 8) -> str:
    ctx = ssl.create_default_context()
    cur = url
    for _ in range(max_hops):
        req = urllib.request.Request(
            cur,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                return resp.geturl()
        except urllib.error.HTTPError as exc:
            loc = exc.headers.get("Location")
            if not loc:
                return cur
            cur = urljoin(cur, loc)
        except urllib.error.URLError as exc:
            raise RuntimeError("공유 링크에 연결하지 못했습니다") from exc
    return cur


def _import_amap(url: str, original: str) -> ShareImportResult:
    final = _follow_redirects(url)
    parsed = _parse_amap_final(final)
    if not parsed:
        raise RuntimeError("고덕 링크에서 위치 정보를 읽지 못했습니다. 링크가 만료됐을 수 있습니다.")

    lat_gcj, lng_gcj, title, address = parsed
    lat, lng = gcj02_to_wgs84(lat_gcj, lng_gcj)
    title = title or "고덕 장소"
    desc_parts = []
    if address:
        desc_parts.append(address)
    desc_parts.append(url)
    if original.strip() != url.strip():
        # 원문 추가 정보는 생략 (URL만으로 충분한 경우)
        pass

    return ShareImportResult(
        source="amap",
        title=title[:200],
        description="\n".join(desc_parts)[:2000],
        address=address,
        source_url=url,
        lat=lat,
        lng=lng,
        category_hint="other",
        needs_map_pick=False,
        note="고덕 공유 링크에서 좌표·명칭을 가져왔습니다 (GCJ-02→WGS84 변환).",
    )


def _parse_amap_final(final_url: str) -> Optional[tuple[float, float, str, str]]:
    qs = parse_qs(urlparse(final_url).query)

    # android=androidamap?action=shorturl&p=POIID,lat,lng,name,address
    android = unquote(qs.get("android", [""])[0])
    if android:
        aqs = parse_qs(urlparse("x://" + android.replace("androidamap?", "?", 1)).query)
        p = aqs.get("p", [""])[0]
        if not p and "p=" in android:
            p = android.split("p=", 1)[1].split("&", 1)[0]
            p = unquote(p)
        hit = _parse_amap_p(p)
        if hit:
            return hit

    mo = unquote(qs.get("mo", [""])[0])
    if mo:
        mqs = parse_qs(urlparse(mo).query)
        hit = _parse_amap_p(mqs.get("p", [""])[0])
        if hit:
            return hit

    ios = unquote(qs.get("ios", [""])[0])
    if ios:
        iqs = parse_qs(urlparse("x://?" + ios.split("?", 1)[-1]).query)
        q = unquote(iqs.get("q", [""])[0])
        title = unquote(iqs.get("title", [""])[0]).replace("+", " ")
        parts = q.split(",")
        if len(parts) >= 2:
            try:
                lat = float(parts[0])
                lng = float(parts[1])
                address = unquote(parts[3]).replace("+", " ") if len(parts) >= 4 else ""
                name = title or (unquote(parts[2]).replace("+", " ") if len(parts) >= 3 else "")
                return lat, lng, name, address
            except ValueError:
                pass

    # URL 전체에서 lat,lng 패턴
    m = re.search(r"(3[0-9]\.\d+)[,/%2C]+(11[0-9]\.\d+)", final_url)
    if m:
        try:
            return float(m.group(1)), float(m.group(2)), "", ""
        except ValueError:
            return None
    return None


def _parse_amap_p(p: str) -> Optional[tuple[float, float, str, str]]:
    if not p:
        return None
    parts = p.split(",")
    if len(parts) < 3:
        return None
    try:
        # POIID, lat, lng, name, address...
        lat = float(parts[1])
        lng = float(parts[2])
    except ValueError:
        try:
            lat = float(parts[0])
            lng = float(parts[1])
            name = parts[2] if len(parts) > 2 else ""
            address = parts[3] if len(parts) > 3 else ""
            return lat, lng, name.strip(), address.strip()
        except ValueError:
            return None
    name = parts[3].strip() if len(parts) > 3 else ""
    address = parts[4].strip() if len(parts) > 4 else ""
    return lat, lng, name, address


def _import_dianping(text: str, url: str) -> ShareImportResult:
    source_url = url or _first_match(DP_URL_RE, text)
    title_m = TITLE_BRACKET_RE.search(text)
    title = (title_m.group(1).strip() if title_m else "").strip()
    if not title:
        title = "따종 장소"

    rating_m = RATING_RE.search(text)
    price_m = PRICE_RE.search(text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    address = ""
    area_cuisine = ""
    for ln in lines:
        if ln.startswith("【") or ln.startswith("http") or "★" in ln or ln.startswith("¥"):
            continue
        if re.search(r"https?://", ln):
            # 같은 줄 끝 URL 제거
            cleaned = DP_URL_RE.sub("", ln).strip()
            if cleaned and not address:
                address = cleaned
            continue
        # "解放东路 鲁菜" 형태
        if not area_cuisine and ("路" in ln or "街" in ln) and len(ln) < 40:
            area_cuisine = ln
            continue
        if not address:
            address = ln

    # 같은 줄에 주소+URL
    for ln in lines:
        if "http" in ln and ("路" in ln or "街" in ln or "号" in ln):
            cleaned = DP_URL_RE.sub("", ln).strip()
            if cleaned:
                address = cleaned

    meta_bits: list[str] = []
    if rating_m:
        meta_bits.append(f"평점 {rating_m.group(1)}")
    if price_m:
        meta_bits.append(f"¥{price_m.group(1)}/인")
    if area_cuisine:
        meta_bits.append(area_cuisine)

    desc_lines = []
    if meta_bits:
        desc_lines.append(" · ".join(meta_bits))
    if address:
        desc_lines.append(address)
    if source_url:
        desc_lines.append(source_url)

    lat: Optional[float] = None
    lng: Optional[float] = None
    note = "따종 공유 문구에서 이름·주소를 채웠습니다."
    needs_pick = True

    # Nominatim으로 주소/이름 추정 (중국 POI는 실패하는 경우 많음)
    geo_queries = []
    if address:
        geo_queries.append(f"{address} 济南")
        geo_queries.append(address)
    geo_queries.append(f"{title} 济南")
    for q in geo_queries:
        try:
            hits = search_address(q, limit=3)
        except RuntimeError:
            hits = []
        if hits:
            lat = hits[0]["lat"]
            lng = hits[0]["lng"]
            needs_pick = False
            note = "따종 문구를 파싱했고, 주소 검색으로 위치를 추정했습니다. 핀이 어긋나면 지도를 옮겨 주세요."
            break

    if needs_pick:
        note = "따종 문구에서 이름·링크는 채웠습니다. 좌표는 지도를 탭해 위치를 지정해 주세요."

    category = "restaurant"
    if area_cuisine and any(k in area_cuisine for k in ("酒店", "宾馆", "民宿")):
        category = "lodging"

    return ShareImportResult(
        source="dianping",
        title=title[:200],
        description="\n".join(desc_lines)[:2000],
        address=address,
        source_url=source_url,
        lat=lat,
        lng=lng,
        category_hint=category,
        needs_map_pick=needs_pick,
        note=note,
    )
