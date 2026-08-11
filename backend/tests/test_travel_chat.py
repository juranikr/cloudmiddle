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
from app.models import City
from app.travel_chat import (
    RESEARCH_TOOLS,
    WRITE_TOOLS,
    _chat_capabilities,
    _needs_answer_retry,
    _research_seed_query,
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

    def test_explicit_registration_enables_research_and_write(self) -> None:
        write_intent, tools = _chat_capabilities("가까운 지점을 찾아서 지도에 등록해줘")

        self.assertTrue(write_intent)
        self.assertEqual(tools, RESEARCH_TOOLS | WRITE_TOOLS)

    def test_generic_clarification_is_retried(self) -> None:
        self.assertTrue(_needs_answer_retry("죄송합니다, 요청 내용을 파악하지 못했습니다."))
        self.assertTrue(_needs_answer_retry("죄송합니다. 어떤 정보를 원하시는지 구체적으로 알려주세요."))
        self.assertFalse(_needs_answer_retry("현재 지도에는 음식 장소가 비어 있습니다."))

    def test_china_chain_names_expand_in_seed_search(self) -> None:
        city = City(name_local="沈阳")
        query = _research_seed_query(city, "호텔 근처 헤이티와 모어요거트 지점을 찾아줘")

        self.assertIn("沈阳", query)
        self.assertIn("喜茶 HEYTEA", query)
        self.assertIn("茉酸奶 More Yogurt", query)


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


if __name__ == "__main__":
    unittest.main()
