"""따종/고덕 공유 텍스트·단축 URL → 제목/주소/좌표 추출 (등록 초안)."""

from __future__ import annotations

import re
import ssl
import time
import urllib.error
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
PRICE_RE = re.compile(r"¥\s*([0-9.]+)\s*/\s*(?:人|사람)")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
FOOD_HINT_RE = re.compile(
    r"(음식|餐饮|美食|菜|肉|火锅|烧烤|咖啡|奶茶|甜点|小吃|饭店|餐厅|食堂)",
    re.I,
)
RATING_ONLY_RE = re.compile(r"^(?:评分\s*)?\d(?:\.\d+)?\s*分$", re.I)
SHARE_BOILERPLATE_RE = re.compile(r"^(?:分享|推荐|打开|复制|查看).*(?:高德|地图|链接|App)", re.I)
ADDRESS_HINT_RE = re.compile(
    r"(?:\d+\s*号|交叉口|路口|(?:省|市|区|县).*(?:路|街|巷)|(?:Street|Road|Avenue)\b|\bNo\.?\s*\d+)",
    re.I,
)


@dataclass
class ShareImportResult:
    source: str  # amap | dianping | unknown
    title: str
    description: str
    address: str
    source_url: str
    lat: Optional[float]
    lng: Optional[float]
    category_hint: str
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


def import_share_text(
    text: str,
    preferred_source: str = "",
    *,
    city_name: str,
    city_context: str,
    viewbox: str = "",
) -> ShareImportResult:
    raw = text.strip()
    if not raw:
        raise ValueError("붙여넣을 내용이 없습니다")

    amap_url = _first_match(AMAP_URL_RE, raw)
    dp_url = _first_match(DP_URL_RE, raw)
    pref = preferred_source.strip().lower()

    if pref == "dianping" or (not pref and (dp_url or "【" in raw) and not amap_url):
        return _import_dianping(
            raw, dp_url, city_name=city_name, city_context=city_context, viewbox=viewbox
        )

    if pref == "amap" or amap_url or "surl.amap.com" in raw.lower() or "amap.com" in raw.lower():
        url = amap_url or ""
        if not url:
            raise ValueError("고덕 공유 링크(surl.amap.com)가 필요합니다")
        return _import_amap(
            url, raw, city_name=city_name, city_context=city_context, viewbox=viewbox
        )

    if dp_url or "【" in raw:
        return _import_dianping(
            raw, dp_url, city_name=city_name, city_context=city_context, viewbox=viewbox
        )

    raise ValueError("따종·고덕 공유 텍스트/링크를 인식하지 못했습니다")


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(0).rstrip(".,);]") if m else ""


def _has_cjk(s: str) -> bool:
    return bool(CJK_RE.search(s or ""))


def _prefer_name(*candidates: str) -> str:
    def score(value: str) -> tuple[int, int]:
        cleaned = value.strip()
        if not cleaned or RATING_ONLY_RE.fullmatch(cleaned) or SHARE_BOILERPLATE_RE.search(cleaned):
            return (-1000, 0)
        points = 30 if _has_cjk(cleaned) else 0
        if any(
            keyword in cleaned
            for keyword in (
                "酒店", "宾馆", "博物馆", "故宫", "公园", "市场", "早市", "餐厅", "饭店", "咖啡", "广场", "府",
            )
        ):
            points += 20
        if ADDRESS_HINT_RE.search(cleaned):
            points -= 25
        return (points, min(len(cleaned), 80))

    usable = [candidate.strip() for candidate in candidates if candidate and candidate.strip()]
    if not usable:
        return ""
    best = max(enumerate(usable), key=lambda item: (*score(item[1]), -item[0]))[1]
    return "" if score(best)[0] < 0 else best


def _follow_redirects(url: str, max_hops: int = 8, total_budget_s: float = 12.0) -> str:
    """리다이렉트 추적. 서버 환경에서 amap이 응답을 지연시킬 수 있어 총 시간 예산을 강제한다
    (게이트웨이 30초 타임아웃 전에 빨리 실패 → 텍스트 폴백 경로로 전환)."""
    ctx = ssl.create_default_context()
    cur = url
    deadline = time.monotonic() + total_budget_s
    for _ in range(max_hops):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("공유 링크 연결이 시간 초과됐습니다")
        req = urllib.request.Request(
            cur,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=min(6.0, remaining)) as resp:
                return resp.geturl()
        except urllib.error.HTTPError as exc:
            loc = exc.headers.get("Location")
            if not loc:
                return cur
            cur = urljoin(cur, loc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("공유 링크에 연결하지 못했습니다") from exc
    return cur


def _parse_share_body_lines(text: str, url: str) -> tuple[str, str, str, str]:
    """본문에서 (title, address, price_label, cuisine_or_meta) 추출."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = ""
    address = ""
    price_label = ""
    meta = ""

    for ln in lines:
        cleaned = ln
        if url:
            cleaned = cleaned.replace(url, "").strip()
        cleaned = AMAP_URL_RE.sub("", cleaned).strip()
        cleaned = DP_URL_RE.sub("", cleaned).strip()
        if not cleaned:
            continue

        price_m = PRICE_RE.search(cleaned)
        if price_m or cleaned.startswith("¥"):
            price_label = cleaned
            # ¥22/사람·중국 음식
            if "·" in cleaned:
                meta = cleaned.split("·", 1)[1].strip()
            elif "・" in cleaned:
                meta = cleaned.split("・", 1)[1].strip()
            continue

        if "★" in cleaned:
            continue

        if RATING_ONLY_RE.fullmatch(cleaned) or SHARE_BOILERPLATE_RE.search(cleaned):
            continue

        if ADDRESS_HINT_RE.search(cleaned):
            if not address:
                address = cleaned
            continue

        if not title and not cleaned.startswith("http"):
            title = cleaned

    return title, address, price_label, meta


def _category_from_text(*parts: str) -> str:
    blob = " ".join(p for p in parts if p)
    if any(k in blob for k in ("酒店", "宾馆", "民宿", "旅馆")):
        return "lodging"
    if FOOD_HINT_RE.search(blob) or "鲁菜" in blob or "人" in blob or "사람" in blob:
        return "restaurant"
    if any(k in blob for k in ("咖啡", "奶茶", "酒吧", "酒馆")):
        return "drink"
    return "other"


def _import_amap(
    url: str,
    original: str,
    *,
    city_name: str,
    city_context: str,
    viewbox: str,
) -> ShareImportResult:
    try:
        final = _follow_redirects(url)
        parsed = _parse_amap_final(final)
    except RuntimeError:
        # 서버에서 amap 접속이 막히거나 지연되는 경우 — 본문 텍스트로 초안 폴백
        return _import_amap_text_fallback(
            url,
            original,
            city_name=city_name,
            city_context=city_context,
            viewbox=viewbox,
        )
    if not parsed:
        raise RuntimeError("고덕 링크에서 위치 정보를 읽지 못했습니다. 링크가 만료됐을 수 있습니다.")

    lat_gcj, lng_gcj, url_title, url_address = parsed
    lat, lng = gcj02_to_wgs84(lat_gcj, lng_gcj)

    text_title, text_address, price_label, meta = _parse_share_body_lines(original, url)
    title = _prefer_name(text_title, url_title) or "고덕 장소"
    address = text_address or url_address

    desc_lines: list[str] = []
    if price_label:
        desc_lines.append(price_label)
    elif meta:
        desc_lines.append(meta)
    if address:
        desc_lines.append(address)
    desc_lines.append(url)

    category = _category_from_text(title, address, price_label, meta, original)

    return ShareImportResult(
        source="amap",
        title=title[:200],
        description="\n".join(desc_lines)[:2000],
        address=address,
        source_url=url,
        lat=lat,
        lng=lng,
        category_hint=category,
        needs_map_pick=False,
        note="고덕 공유에서 명칭·주소·좌표 초안을 만들었습니다. 유형만 확인하고 저장하세요.",
    )


def _import_amap_text_fallback(
    url: str,
    original: str,
    *,
    city_name: str,
    city_context: str,
    viewbox: str,
) -> ShareImportResult:
    """링크 추적 실패 시: 공유 본문의 제목·주소로 초안 구성, 가능하면 지오코딩."""
    title, address, price_label, meta = _parse_share_body_lines(original, url)
    if not title:
        raise RuntimeError(
            "고덕 링크에 연결하지 못했고 본문에서 이름도 찾지 못했습니다. "
            "공유 텍스트 전체(이름·주소 포함)를 붙여넣어 주세요."
        )

    lat: Optional[float] = None
    lng: Optional[float] = None
    needs_pick = True
    note = (
        "고덕 링크 연결이 안 돼 본문 텍스트로 초안을 만들었습니다. "
        "지도에서 위치를 탭해 핀을 놓고 저장하세요."
    )
    for q in filter(None, [f"{title} {city_name}", address and f"{address} {city_name}", address]):
        try:
            hits = search_address(
                q,
                limit=3,
                viewbox=viewbox,
                city_name=city_name,
                city_context=city_context,
            )
        except RuntimeError:
            hits = []
        if hits:
            lat = hits[0]["lat"]
            lng = hits[0]["lng"]
            needs_pick = False
            note = (
                "고덕 링크 연결이 안 돼 본문 텍스트와 지오코딩으로 초안을 만들었습니다. "
                "핀 위치가 맞는지 확인한 뒤 저장하세요."
            )
            break

    desc_lines: list[str] = []
    if price_label:
        desc_lines.append(price_label)
    elif meta:
        desc_lines.append(meta)
    if address:
        desc_lines.append(address)
    desc_lines.append(url)

    return ShareImportResult(
        source="amap",
        title=title[:200],
        description="\n".join(desc_lines)[:2000],
        address=address,
        source_url=url,
        lat=lat,
        lng=lng,
        category_hint=_category_from_text(title, address, price_label, meta, original),
        needs_map_pick=needs_pick,
        note=note,
    )


def _parse_amap_final(final_url: str) -> Optional[tuple[float, float, str, str]]:
    qs = parse_qs(urlparse(final_url).query)

    android = unquote(qs.get("android", [""])[0])
    if android:
        aqs = parse_qs(urlparse("x://" + android.replace("androidamap?", "?", 1)).query)
        p = aqs.get("p", [""])[0]
        if not p and "p=" in android:
            p = unquote(android.split("p=", 1)[1].split("&", 1)[0])
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


def _import_dianping(
    text: str,
    url: str,
    *,
    city_name: str,
    city_context: str,
    viewbox: str,
) -> ShareImportResult:
    source_url = url or _first_match(DP_URL_RE, text)
    title_m = TITLE_BRACKET_RE.search(text)
    title = (title_m.group(1).strip() if title_m else "").strip()
    text_title, text_address, price_label, meta = _parse_share_body_lines(text, source_url)
    if not title:
        title = text_title or "따종 장소"

    rating_m = RATING_RE.search(text)
    price_m = PRICE_RE.search(text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    address = text_address
    area_cuisine = meta
    for ln in lines:
        if ln.startswith("【") or "★" in ln or ln.startswith("¥") or ln.startswith("http"):
            continue
        cleaned = DP_URL_RE.sub("", ln).strip()
        if not cleaned:
            continue
        if not area_cuisine and ("路" in cleaned or "街" in cleaned) and len(cleaned) < 40 and "交叉" not in cleaned:
            # 解放东路 鲁菜 — 짧은 상권/요리 줄
            if " " in cleaned or "鲁" in cleaned or "菜" in cleaned:
                area_cuisine = cleaned
                continue
        if re.search(r"(交叉口|东北角|西南角|号)", cleaned) or len(cleaned) >= 10:
            if not address:
                address = cleaned

    for ln in lines:
        if "http" in ln and any(k in ln for k in ("路", "街", "号", "交叉")):
            cleaned = DP_URL_RE.sub("", ln).strip()
            if cleaned:
                address = cleaned

    meta_bits: list[str] = []
    if rating_m:
        meta_bits.append(f"평점 {rating_m.group(1)}")
    if price_m:
        meta_bits.append(f"¥{price_m.group(1)}/인")
    elif price_label:
        meta_bits.append(price_label)
    if area_cuisine:
        meta_bits.append(area_cuisine)

    desc_lines: list[str] = []
    if meta_bits:
        desc_lines.append(" · ".join(meta_bits))
    if address:
        desc_lines.append(address)
    if source_url:
        desc_lines.append(source_url)

    lat: Optional[float] = None
    lng: Optional[float] = None
    needs_pick = True
    note = "따종 공유에서 이름·주소를 채웠습니다. 지도에서 위치만 탭한 뒤 유형을 확인하고 저장하세요."

    geo_queries = []
    if address:
        geo_queries.extend([f"{address} {city_name}", address])
    geo_queries.append(f"{title} {city_name}")
    for q in geo_queries:
        try:
            hits = search_address(
                q,
                limit=3,
                viewbox=viewbox,
                city_name=city_name,
                city_context=city_context,
            )
        except RuntimeError:
            hits = []
        if hits:
            lat = hits[0]["lat"]
            lng = hits[0]["lng"]
            needs_pick = False
            note = "따종 초안을 만들었습니다. 핀 위치가 맞는지 확인한 뒤 유형만 고르고 저장하세요."
            break

    category = _category_from_text(title, address, area_cuisine, " ".join(meta_bits), text)
    if category == "other":
        category = "restaurant"

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
