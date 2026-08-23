from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.agent.tools import (
    _filter_search_results,
    _source_mentions_place,
)
from app.geocode import (
    _normalize,
    _search_arcgis,
    _search_nominatim,
)
from app.models import Marker, MarkerCategory
from app.search_providers import (
    build_search_provider_profile,
    search_brave_places,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


class DestinationProviderProfileTests(unittest.TestCase):
    def test_brave_place_uses_destination_country_instead_of_fixed_china(self) -> None:
        requests = []

        result = search_brave_places(
            api_key="secret",
            query="東京 喫茶店",
            latitude=35.68,
            longitude=139.76,
            country_code="JP",
            opener=lambda request, **_kwargs: (
                requests.append(request) or _Response({"results": []})
            ),
        )

        self.assertEqual(result["status"], "ok")
        self.assertIn("country=JP", requests[0].full_url)
        self.assertNotIn("country=CN", requests[0].full_url)

    def test_nominatim_and_arcgis_receive_profile_country_and_languages(self) -> None:
        profile = build_search_provider_profile(
            country_code="JP",
            city_slug="tokyo",
            city_name="東京",
            city_name_ko="도쿄",
        )
        requests: dict[str, dict] = {}

        def fake_get_json(url, params, **_kwargs):
            requests[url] = dict(params)
            return [] if "nominatim" in url else {"candidates": []}

        with (
            patch("app.geocode._get_json", side_effect=fake_get_json),
            patch("app.geocode.time.sleep"),
        ):
            _search_nominatim(
                "すし大本店 profile-country-test",
                limit=3,
                bounds=None,
                city_name="東京",
                profile=profile,
            )
            _search_arcgis(
                "すし大本店 profile-country-test",
                limit=3,
                bounds=None,
                city_name="東京",
                city_context="東京都 日本",
                api_key="arcgis-token",
                profile=profile,
            )

        nominatim = next(value for key, value in requests.items() if "nominatim" in key)
        arcgis = next(value for key, value in requests.items() if "arcgis" in key)
        self.assertEqual(nominatim["countrycodes"], "jp")
        self.assertTrue(nominatim["accept-language"].startswith("ja"))
        self.assertEqual(arcgis["sourceCountry"], "JPN")
        self.assertNotIn("中国", arcgis["SingleLine"])

    def test_non_china_scripts_survive_normalization_and_relevance(self) -> None:
        profile = build_search_provider_profile(
            country_code="JP",
            city_slug="tokyo",
            city_name="東京",
            city_name_ko="도쿄",
        )
        item = {
            "title": "すし大本店 営業時間とアクセス",
            "href": "https://local.example.jp/sushi-dai",
            "body": "築地にある店舗です",
        }

        kept, discarded = _filter_search_results(
            "東京 すし大本店",
            [item],
            limit=5,
            profile=profile,
        )

        self.assertEqual(_normalize("カフェ・ド・フロール"), "カフェドフロール")
        self.assertEqual([row["href"] for row in kept], [item["href"]])
        self.assertEqual(discarded, 0)

    def test_jinan_official_and_exact_local_business_survive_without_city_in_title(self) -> None:
        profile = build_search_provider_profile(
            country_code="CN",
            city_slug="jinan",
            city_name="济南",
            city_name_ko="지난",
        )
        official = {
            "title": "超意兴快餐门店信息",
            "href": "https://www.jinan.gov.cn/art/food/shops.html",
            "body": "经十路门店地址与营业信息",
        }
        exact_local = {
            "title": "超意兴快餐（泉城路店）地址",
            "href": "https://local.example.cn/place/chaoyixing",
            "body": "超意兴泉城路门店",
        }
        unrelated = {
            "title": "上海餐饮旅游指南",
            "href": "https://www.jinan.gov.cn/art/unrelated.html",
            "body": "上海餐厅推荐",
        }

        kept, discarded = _filter_search_results(
            "济南 超意兴快餐 地址",
            [unrelated, official, exact_local],
            limit=5,
            profile=profile,
        )

        self.assertEqual(
            {row["href"] for row in kept},
            {official["href"], exact_local["href"]},
        )
        self.assertEqual(discarded, 1)


class BranchSubjectRegressionTests(unittest.TestCase):
    def test_manxin_exact_name_and_address_can_replace_missing_branch_token(self) -> None:
        marker = Marker(
            city_id=2,
            category=MarkerCategory.lodging,
            title="瀋陽中街故宮漫心酒店 (선양 중제고궁 만신호텔)",
            description="주소: 沈阳市沈河区北中街路118号",
            coordinate_query="沈阳 中街故宫漫心酒店 北中街路118号",
            branch_name="中街故宫店",
            lat=41.80,
            lng=123.45,
        )

        self.assertTrue(_source_mentions_place(
            marker,
            "携程：沈阳中街故宫漫心酒店，地址：沈阳市沈河区北中街路118号",
            "https://hotels.ctrip.com/hotels/110.html",
        ))
        self.assertFalse(_source_mentions_place(
            marker,
            "携程：沈阳太原街漫心酒店，地址：沈阳市和平区中华路88号",
            "https://hotels.ctrip.com/hotels/999.html",
        ))


if __name__ == "__main__":
    unittest.main()
