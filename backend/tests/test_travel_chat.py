import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import City, TravelChatMessage
from app.travel_chat import (
    RESEARCH_TOOLS,
    WRITE_TOOLS,
    _chat_capabilities,
    _missing_brand_targets,
    _needs_answer_retry,
    _research_seed_queries,
    _research_seed_query,
    _resolve_context_message,
    _strip_unsupported_urls,
    answer_travel_chat,
)


class TravelChatRoutingTests(unittest.TestCase):
    def test_simple_map_question_skips_web_tools(self) -> None:
        write_intent, tools = _chat_capabilities("내 일정의 이동 동선을 줄여줘")

        self.assertFalse(write_intent)
        self.assertEqual(tools, set())

        write_intent, tools = _chat_capabilities("박물관 말고 현재 지도 장소로 반나절을 짜줘")
        self.assertFalse(write_intent)
        self.assertEqual(tools, set())

    def test_fresh_place_lookup_enables_research_only(self) -> None:
        write_intent, tools = _chat_capabilities("헤이티 지점이 호텔 근처 어디 있는지 찾아줘")

        self.assertFalse(write_intent)
        self.assertEqual(tools, RESEARCH_TOOLS)

        write_intent, tools = _chat_capabilities("다른 후보도 추가로 찾아줘")
        self.assertFalse(write_intent)
        self.assertEqual(tools, RESEARCH_TOOLS)

    def test_explicit_registration_enables_research_and_write(self) -> None:
        write_intent, tools = _chat_capabilities("가까운 지점을 찾아서 지도에 등록해줘")

        self.assertTrue(write_intent)
        self.assertEqual(tools, RESEARCH_TOOLS | WRITE_TOOLS)

    def test_generic_clarification_is_retried(self) -> None:
        self.assertTrue(_needs_answer_retry("죄송합니다, 요청 내용을 파악하지 못했습니다."))
        self.assertTrue(_needs_answer_retry("죄송합니다. 어떤 정보를 원하시는지 구체적으로 알려주세요."))
        self.assertTrue(_needs_answer_retry("먼저 어떤 정보를 원하시는지 알려 주세요."))
        self.assertTrue(_needs_answer_retry("확인된 자료가 부족해 답을 완성하지 못했습니다."))
        self.assertFalse(_needs_answer_retry("현재 지도에는 음식 장소가 비어 있습니다."))

    def test_china_chain_names_expand_in_seed_search(self) -> None:
        city = City(name_local="沈阳")
        query = _research_seed_query(city, "호텔 근처 헤이티와 모어요거트 지점을 찾아줘")

        self.assertIn("沈阳", query)
        self.assertIn("喜茶 HEYTEA", query)
        self.assertIn("茉酸奶 More Yogurt", query)

    def test_short_write_followup_inherits_previous_targets(self) -> None:
        rows = [
            SimpleNamespace(role="user", content="모어요거트와 헤이티를 찾아줘"),
            SimpleNamespace(role="assistant", content="검증되지 않은 답"),
        ]
        resolved = _resolve_context_message("추가해줘", rows)
        write_intent, tools = _chat_capabilities("추가해줘", context_message=resolved)

        self.assertIn("모어요거트와 헤이티", resolved)
        self.assertTrue(write_intent)
        self.assertEqual(tools, RESEARCH_TOOLS | WRITE_TOOLS)

    def test_brand_seed_queries_use_correct_chinese_names(self) -> None:
        city = City(name_local="沈阳")
        queries = _research_seed_queries(city, "모어요거트와 헤이티를 등록해줘")

        self.assertIn("沈阳 茉酸奶 大悦城旗舰店 地址", queries)
        self.assertIn("沈阳 喜茶 大悦城店 中街益田假日世界店 地址", queries)
        self.assertTrue(all("海蒂" not in query for query in queries))

    def test_answer_validation_requires_each_brand_and_removes_fake_urls(self) -> None:
        self.assertEqual(
            _missing_brand_targets("모어요거트와 헤이티", "喜茶 지점만 확인했습니다."),
            ["모어요거트"],
        )
        self.assertNotIn(
            "https://fake.example/123",
            _strip_unsupported_urls(
                "가짜 링크 https://fake.example/123",
                {"https://real.example/place"},
            ),
        )


class _FakeCompletions:
    def __init__(self, tool_rounds: int) -> None:
        self.tool_rounds = tool_rounds
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        round_number = len(self.requests)
        if round_number <= self.tool_rounds:
            call = SimpleNamespace(
                id=f"call-{round_number}",
                function=SimpleNamespace(
                    name="web_search",
                    arguments=json.dumps({"query": f"沈阳 茶店 {round_number}"}, ensure_ascii=False),
                ),
            )
            message = SimpleNamespace(content="", tool_calls=[call])
        else:
            message = SimpleNamespace(content="확인한 세 지점을 가까운 순서로 정리했습니다.", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _ForcedWriteCompletions:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        round_number = len(self.requests)
        if round_number == 1:
            calls = [
                SimpleNamespace(
                    id="write-1",
                    function=SimpleNamespace(
                        name="propose_place",
                        arguments=json.dumps({"title": "喜茶中街店 (헤이티 중제점)"}, ensure_ascii=False),
                    ),
                ),
                SimpleNamespace(
                    id="write-2",
                    function=SimpleNamespace(
                        name="propose_place",
                        arguments=json.dumps({"title": "茉酸奶中街店 (모어요거트 중제점)"}, ensure_ascii=False),
                    ),
                ),
            ]
            message = SimpleNamespace(content="", tool_calls=calls)
        else:
            message = SimpleNamespace(
                content="모어요거트와 헤이티를 관리자 승인 대기 제안으로 실제 저장했습니다.",
                tool_calls=[],
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FailingCompletions:
    def create(self, **_kwargs):
        raise RuntimeError("Tool choice is none, but model called a tool")


class TravelChatLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add(City(
            id=1,
            slug="shenyang",
            name_ko="선양",
            name_local="沈阳",
            center_lat=41.80,
            center_lng=123.43,
            search_viewbox="122.85,42.15,123.85,41.45",
            search_context="沈阳市 辽宁省 中国",
        ))
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_tool_budget_always_ends_with_no_tool_synthesis(self) -> None:
        completions = _FakeCompletions(tool_rounds=3)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat.run_tool", return_value={"items": [], "source": "https://example.com"}) as tool,
        ):
            result = answer_travel_chat(
                self.db,
                user_id=1,
                city_id=1,
                message="호텔 근처 차 지점을 찾아줘",
            )

        self.assertEqual(result["row"].content, "확인한 세 지점을 가까운 순서로 정리했습니다.")
        self.assertEqual(len(completions.requests), 4)
        self.assertIn("tools", completions.requests[0])
        self.assertEqual(tool.call_args_list[0].args[1], "web_search")
        self.assertIn("沈阳", tool.call_args_list[0].args[2]["query"])
        self.assertNotIn("tools", completions.requests[-1])

    def test_simple_question_uses_one_tool_free_completion(self) -> None:
        completions = _FakeCompletions(tool_rounds=0)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
        ):
            answer_travel_chat(self.db, user_id=1, city_id=1, message="내 일정 동선을 줄여줘")

        self.assertEqual(len(completions.requests), 1)
        self.assertNotIn("tools", completions.requests[0])

    def test_short_followup_forces_real_write_before_claiming_success(self) -> None:
        self.db.add_all([
            TravelChatMessage(
                user_id=1,
                city_id=1,
                role="user",
                content="찾은 모어요거트와 헤이티 지도에 등록해줘",
                sources="[]",
                place_ids="[]",
            ),
            TravelChatMessage(
                user_id=1,
                city_id=1,
                role="assistant",
                content="등록 제안 요약만 작성했습니다.",
                sources="[]",
                place_ids="[]",
            ),
        ])
        self.db.commit()
        completions = _ForcedWriteCompletions()
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        def fake_tool(_db, name, _args, *, city_id):
            self.assertEqual(city_id, 1)
            if name == "web_search":
                return {"results": [{"href": "https://example.com/branch", "title": "branch"}]}
            if name == "fetch_page":
                return {"url": "https://example.com/branch", "title": "branch", "text": "verified"}
            if name == "geocode_place":
                return {"results": [{"lat": 41.8, "lng": 123.45, "source": "osm"}]}
            if name == "propose_place":
                return {"ok": True, "proposal_created": True, "proposal_id": 31}
            return {"error": "unexpected_tool"}

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat.run_tool", side_effect=fake_tool) as tool,
        ):
            result = answer_travel_chat(self.db, user_id=1, city_id=1, message="추가해줘")

        self.assertIn("실제 저장", result["row"].content)
        self.assertTrue(any(call.args[1] == "propose_place" for call in tool.call_args_list))
        self.assertEqual(completions.requests[0]["tools"][0]["function"]["name"], "propose_place")
        self.assertEqual(
            completions.requests[0]["tool_choice"],
            {"type": "function", "function": {"name": "propose_place"}},
        )
        self.assertNotIn("tools", completions.requests[-1])

    def test_final_model_tool_error_never_becomes_http_500(self) -> None:
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FailingCompletions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
        ):
            result = answer_travel_chat(
                self.db,
                user_id=1,
                city_id=1,
                message="현재 지도 장소를 간단히 설명해줘",
            )

        self.assertIn("현재 지도", result["row"].content)


if __name__ == "__main__":
    unittest.main()
