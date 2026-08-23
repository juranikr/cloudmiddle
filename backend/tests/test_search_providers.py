import io
import json
import sys
import types
import unittest
import urllib.error
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.tools import run_tool
from app.config import settings
from app.db import Base
from app.models import AgentSearchLog, AgentWebVisit, City
from app.search_providers import search_brave_places


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


class BravePlaceProviderTests(unittest.TestCase):
    def test_missing_key_skips_without_network(self):
        calls = []

        result = search_brave_places(
            api_key="",
            query="沈阳 茶饮",
            latitude=41.8,
            longitude=123.43,
            opener=lambda *_args, **_kwargs: calls.append(True),
        )

        self.assertEqual(result["status"], "skipped_no_key")
        self.assertEqual(result["results"], [])
        self.assertEqual(calls, [])

    def test_normalizes_and_hard_filters_results(self):
        payload = {
            "results": [
                {
                    "id": "temporary-inside",
                    "title": "茶饮店（中街店）",
                    "provider_url": "https://example.test/inside",
                    "coordinates": [41.801, 123.432],
                    "postal_address": {"displayAddress": "沈河区中街路1号"},
                    "categories": ["Tea"],
                },
                {
                    "id": "temporary-outside",
                    "title": "北京门店",
                    "coordinates": [39.9, 116.4],
                },
            ]
        }
        requests = []

        def opener(request, **_kwargs):
            requests.append(request)
            return _Response(payload)

        result = search_brave_places(
            api_key="secret",
            query="沈阳 茶饮",
            latitude=41.8,
            longitude=123.43,
            city_bounds=(41.5, 122.9, 42.1, 124.0),
            opener=opener,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["outside_city_count"], 1)
        self.assertEqual(len(result["results"]), 1)
        candidate = result["results"][0]
        self.assertEqual(candidate["transient_id"], "temporary-inside")
        self.assertEqual(candidate["external_id"], "")
        self.assertFalse(candidate["storage_allowed"])
        self.assertTrue(candidate["requires_cross_verification"])
        self.assertIn("country=CN", requests[0].full_url)
        self.assertNotIn("secret", requests[0].full_url)

    def test_storage_flag_does_not_promote_ephemeral_id(self):
        result = search_brave_places(
            api_key="secret",
            query="沈阳 景点",
            latitude=41.8,
            longitude=123.43,
            storage_allowed=True,
            opener=lambda *_args, **_kwargs: _Response({
                "results": [{
                    "id": "temporary",
                    "title": "景点",
                    "coordinates": [41.8, 123.43],
                }]
            }),
        )

        candidate = result["results"][0]
        self.assertTrue(candidate["storage_allowed"])
        self.assertFalse(candidate["requires_cross_verification"])
        self.assertEqual(candidate["external_id"], "")

    def test_rate_limit_retries_once_with_bounded_delay(self):
        attempts = []
        delays = []

        def opener(request, **_kwargs):
            attempts.append(request)
            if len(attempts) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    {"Retry-After": "99"},
                    io.BytesIO(),
                )
            return _Response({"results": []})

        result = search_brave_places(
            api_key="secret",
            query="沈阳 小吃",
            latitude=41.8,
            longitude=123.43,
            opener=opener,
            sleeper=delays.append,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["retries"], 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(delays, [2.0])


class BravePlaceToolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add(City(
            id=1,
            slug="shenyang",
            name_ko="선양",
            name_local="沈阳",
            center_lat=41.8,
            center_lng=123.43,
            search_viewbox="122.85,42.15,123.85,41.45",
            search_context="沈阳市 辽宁省 中国",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _ddgs_module(results=None, error=None):
        module = types.ModuleType("ddgs")

        class DDGS:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def text(self, *_args, **_kwargs):
                if error:
                    raise RuntimeError(error)
                return list(results or [])

        module.DDGS = DDGS
        return module

    def test_structured_place_survives_text_backend_failure(self):
        brave = {
            "status": "ok",
            "results": [{
                "display_name": "不老林糖果(中街店)",
                "address": "中街路123号",
                "lat": 41.801,
                "lng": 123.452,
                "source": "brave_place",
                "source_url": "https://example.test/place",
                "external_id": "",
                "storage_allowed": False,
                "requires_cross_verification": True,
            }],
        }
        with (
            patch.dict(sys.modules, {"ddgs": self._ddgs_module(error="blocked")}),
            patch.object(settings, "brave_search_api_key", "test-key"),
            patch("app.agent.tools.search_brave_places", return_value=brave) as provider,
        ):
            result = run_tool(
                self.db,
                "web_search",
                {"query": "沈阳 不老林糖果", "max_results": 5},
                city_id=1,
                server_allow_brave_places=True,
            )

        self.assertNotIn("error", result)
        self.assertEqual(len(result["place_candidates"]), 1)
        self.assertEqual(result["results"], [])
        self.assertTrue(any("blocked" in item for item in result["backend_errors"]))
        self.assertEqual(self.db.query(AgentSearchLog).count(), 1)
        self.assertEqual(self.db.query(AgentSearchLog).one().results_count, 0)
        kwargs = provider.call_args.kwargs
        self.assertEqual(kwargs["city_bounds"], (41.45, 122.85, 42.15, 123.85))

    def test_brave_plan_error_does_not_break_text_results(self):
        brave = {
            "status": "error",
            "error": "authorization_or_plan",
            "http_status": 403,
            "results": [],
        }
        text_results = [{
            "title": "沈阳不老林糖果中街店攻略",
            "href": "https://gs.ctrip.com/example",
            "body": "沈阳 中街 糖果 地址",
        }]
        with (
            patch.dict(sys.modules, {"ddgs": self._ddgs_module(results=text_results)}),
            patch.object(settings, "brave_search_api_key", "test-key"),
            patch("app.agent.tools.search_brave_places", return_value=brave),
        ):
            result = run_tool(
                self.db,
                "web_search",
                {"query": "沈阳 不老林糖果", "max_results": 5},
                city_id=1,
                server_allow_brave_places=True,
            )

        self.assertTrue(result["results"])
        self.assertEqual(result["place_candidates"], [])
        self.assertEqual(result["provider_attempts"][0]["http_status"], 403)

    def test_brave_places_are_disabled_without_server_discovery_opt_in(self):
        text_results = [{
            "title": "沈阳旅行",
            "href": "https://example.test/shenyang",
            "body": "沈阳 行程",
        }]
        with (
            patch.dict(sys.modules, {"ddgs": self._ddgs_module(results=text_results)}),
            patch.object(settings, "brave_search_api_key", "test-key"),
            patch("app.agent.tools.search_brave_places") as provider,
        ):
            result = run_tool(
                self.db,
                "web_search",
                {"query": "沈阳旅行", "max_results": 5},
                city_id=1,
            )

        provider.assert_not_called()
        self.assertEqual(result["place_candidates"], [])
        self.assertEqual(result["provider_attempts"], [])

    def test_transient_followup_query_is_not_logged_or_persisted(self):
        secret = "BRAVE_ONLY_FOLLOWUP_SECRET"
        marker = "[BRAVE_TRANSIENT_QUERY_DISCARDED]"
        with (
            patch.dict(sys.modules, {"ddgs": self._ddgs_module(error=f"blocked {secret}")}),
            patch("app.agent.tools.logger.warning") as warning,
        ):
            run_tool(
                self.db,
                "web_search",
                {"query": secret, "max_results": 5},
                city_id=1,
                server_storage_query=marker,
            )

        row = self.db.query(AgentSearchLog).one()
        self.assertEqual(row.query, marker)
        logged = " ".join(str(value) for value in warning.call_args.args)
        self.assertNotIn(secret, logged)
        self.assertIn("transient_search_failed", logged)

    def test_transient_followup_fetch_does_not_record_web_visit(self):
        tainted_url = "https://brave-only.example/temporary-place-id"
        with (
            patch("app.agent.tools._ctrip_food_coordinate_url", return_value=f"{tainted_url}/companion"),
            patch("app.agent.tools._extract_page_text", side_effect=[{
                "title": "독립 공개 페이지",
                "text": "여행 장소의 주소와 방문 정보를 제공하는 충분한 공개 본문입니다. " * 8,
                "coordinate_candidates": [],
            }, RuntimeError(f"blocked while fetching {tainted_url}/companion")]),
            patch("app.agent.tools.logger.info") as info,
        ):
            result = run_tool(
                self.db,
                "fetch_page",
                {"url": tainted_url},
                city_id=1,
                server_record_web_visit=False,
            )

        self.assertNotIn("error", result)
        self.assertEqual(self.db.query(AgentWebVisit).count(), 0)
        logged = " ".join(str(value) for value in info.call_args.args)
        self.assertNotIn(tainted_url, logged)
        self.assertIn("RuntimeError", logged)


if __name__ == "__main__":
    unittest.main()
