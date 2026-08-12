import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.tools import TOOLS, _filter_search_results, _search_result_quality
from app.config import settings
from app.db import Base
from app.models import City, Marker, MarkerCategory, TravelChatMessage, TravelChatWork
from app.travel_chat import (
    ChatIntent,
    CITY_FOOD_DETAIL_SOURCES,
    RESEARCH_TOOLS,
    WRITE_TOOLS,
    _brand_source_urls,
    _chat_capabilities,
    _classify_snack_fit,
    _food_business_name,
    _food_detail_recovery_query,
    _extract_grounded_candidates,
    _ensure_chat_work,
    _latest_chat_candidate,
    _latest_chat_candidates,
    _inherit_active_intent,
    _missing_brand_targets,
    _needs_answer_retry,
    _pending_work_candidates,
    _research_seed_queries,
    _research_seed_query,
    _resolve_context_message,
    _strip_unsupported_urls,
    _supporting_sources,
    answer_travel_chat,
)


class TravelChatRoutingTests(unittest.TestCase):
    def test_search_filter_keeps_relevant_local_source_and_drops_spam(self) -> None:
        query = "沈阳 中街 按摩店"
        safe = {
            "title": "【绿波廊SPA会馆 (中街店)】电话_地址_东中街洗浴中心",
            "href": "https://m.dianping.com/shop/example",
            "body": "沈阳中街洗浴与按摩服务",
        }
        unsafe = {
            "title": "扫街探店黑料成人视频",
            "href": "https://random.example.cc/archives/108389/",
            "body": "成人内容",
        }
        unrelated = {
            "title": "Amazon Bedrock API 개발자 문서",
            "href": "https://example.com/bedrock",
            "body": "Android API authentication",
        }

        kept, discarded = _filter_search_results(query, [unsafe, unrelated, safe], limit=8)

        self.assertGreater(_search_result_quality(query, safe), 0.7)
        self.assertEqual([item["href"] for item in kept], [safe["href"]])
        self.assertEqual(discarded, 2)

    def test_trusted_host_does_not_bypass_exact_business_relevance(self) -> None:
        query = "高德地图 诚意小厨 皇姑店"
        unrelated_trusted = {
            "title": "上海美食指南",
            "href": "https://ru.trip.com/guide/food/food-in-shanghai.html",
            "body": "",
        }
        relevant_trusted = {
            "title": "诚意小厨 (皇姑店) 电话 地址",
            "href": "https://m.dianping.com/shop/1129127029",
            "body": "诚意小厨皇姑店地址与营业信息",
        }

        kept, discarded = _filter_search_results(
            query, [unrelated_trusted, relevant_trusted], limit=8
        )

        self.assertEqual([item["href"] for item in kept], [relevant_trusted["href"]])
        self.assertEqual(discarded, 1)

    def test_grounded_candidate_is_extracted_from_answer_and_tool_evidence(self) -> None:
        answer = (
            "중제 근처에서는 绿波廊SPA会馆（中街店）을 확인했습니다.\n"
            "주소: 小什字街66号"
        )
        tools = [{
            "name": "web_search",
            "args": {"query": "沈阳 中街 按摩店"},
            "result": {"results": [{
                "title": "【绿波廊SPA会馆（中街店）】电话_地址_价格",
                "href": "https://m.dianping.com/shop/example",
                "body": "地址：小什字街66号",
                "quality": 0.92,
            }]},
        }, {
            "name": "geocode_place",
            "args": {"query": "沈阳 绿波廊SPA会馆 中街店 小什字街66号"},
            "result": {"results": [{
                "display_name": "绿波廊SPA会馆",
                "lat": 41.801,
                "lng": 123.46,
                "storage_allowed": True,
                "confidence": 0.8,
            }]},
        }]

        candidates = _extract_grounded_candidates(answer, tools, message="중제 마사지샵을 찾아줘")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "绿波廊SPA会馆（中街店）")
        self.assertEqual(candidates[0]["address"], "小什字街66号")
        self.assertEqual(candidates[0]["status"], "located")
        self.assertEqual(candidates[0]["lat"], 41.801)
        self.assertEqual(
            _supporting_sources(answer, tools),
            ["https://m.dianping.com/shop/example"],
        )

    def test_short_followup_binds_latest_structured_candidate(self) -> None:
        candidate = {
            "key": "abc",
            "title": "绿波廊SPA会馆（中街店）",
            "address": "小什字街66号",
            "status": "location_needed",
            "source_urls": ["https://m.dianping.com/shop/example"],
        }
        rows = [
            SimpleNamespace(role="user", content="중제쪽 마사지샵도 찾아줘", candidates="[]"),
            SimpleNamespace(
                role="assistant",
                content="자연어 답변은 신뢰하지 않음",
                candidates=json.dumps([candidate], ensure_ascii=False),
            ),
        ]

        work = SimpleNamespace(
            id=1,
            action="research",
            scope="single",
            subject="spa",
            goal="중제 마사지 후보 조사",
            requested_count=1,
            state=json.dumps({"phase": "research", "candidates": [candidate]}, ensure_ascii=False),
        )
        resolved = _resolve_context_message(
            "등록해줘",
            rows,
            intent=ChatIntent(action="write", continuation=True, wants_write=True),
            work=work,
        )

        self.assertEqual(_latest_chat_candidate(rows)["key"], "abc")
        self.assertIn("绿波廊SPA会馆", resolved)
        self.assertIn("小什字街66号", resolved)

    def test_legacy_continuation_skips_empty_failure_and_matches_subject(self) -> None:
        food = {
            "key": "food-1",
            "title": "Food candidate",
            "category": "restaurant",
            "status": "located",
        }
        spa = {
            "key": "spa-1",
            "title": "Spa candidate",
            "category": "other",
            "status": "located",
        }
        rows = [
            SimpleNamespace(role="assistant", candidates=json.dumps([spa])),
            SimpleNamespace(role="assistant", candidates=json.dumps([food])),
            SimpleNamespace(role="assistant", candidates="[]"),
        ]

        recovered = _latest_chat_candidates(rows, subject="food")

        self.assertEqual([item["key"] for item in recovered], ["food-1"])

    def test_shenyang_food_bootstrap_uses_auditable_detail_pages(self) -> None:
        urls = CITY_FOOD_DETAIL_SOURCES["shenyang"]

        self.assertGreaterEqual(len(urls), 2)
        self.assertTrue(all("ctrip.com" in url or "qunar.com" in url for url in urls))

    def test_simple_map_question_skips_web_tools(self) -> None:
        write_intent, tools = _chat_capabilities(ChatIntent(action="answer"))

        self.assertFalse(write_intent)
        self.assertEqual(tools, set())

        write_intent, tools = _chat_capabilities(ChatIntent(action="answer"))
        self.assertFalse(write_intent)
        self.assertEqual(tools, set())

    def test_fresh_place_lookup_enables_research_only(self) -> None:
        write_intent, tools = _chat_capabilities(ChatIntent(action="research", wants_research=True))

        self.assertFalse(write_intent)
        self.assertEqual(tools, RESEARCH_TOOLS)

        write_intent, tools = _chat_capabilities(ChatIntent(action="continue", wants_research=True))
        self.assertFalse(write_intent)
        self.assertEqual(tools, RESEARCH_TOOLS)

    def test_explicit_registration_enables_research_and_write(self) -> None:
        write_intent, tools = _chat_capabilities(ChatIntent(
            action="research_and_write", wants_research=True, wants_write=True
        ))

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

    def test_massage_seed_uses_short_safe_chinese_query(self) -> None:
        city = City(name_local="沈阳")

        queries = _research_seed_queries(city, "중제쪽 마사지샵도 찾아줘")

        self.assertEqual(queries, ["沈阳 中街 正规 洗浴 按摩 推荐"])
        self.assertNotIn("사용자 요청", queries[0])

    def test_short_write_followup_inherits_previous_targets(self) -> None:
        work = SimpleNamespace(
            id=1,
            action="research_and_write",
            scope="all",
            subject="drink",
            goal="모어요거트와 헤이티를 찾아 지도에 추가",
            requested_count=2,
            state=json.dumps({"wants_research": True, "wants_write": True, "candidates": []}),
        )
        intent = _inherit_active_intent(
            ChatIntent(action="continue", continuation=True, wants_write=True), work
        )
        resolved = _resolve_context_message("추가해줘", [], intent=intent, work=work)
        write_intent, tools = _chat_capabilities(intent)

        self.assertIn("모어요거트와 헤이티", resolved)
        self.assertTrue(write_intent)
        self.assertEqual(tools, RESEARCH_TOOLS | WRITE_TOOLS)

    def test_remaining_scope_uses_unfinished_work_candidates(self) -> None:
        work = SimpleNamespace(state=json.dumps({
            "candidates": [
                {"key": "a", "title": "A", "status": "proposed"},
                {"key": "b", "title": "B", "status": "located"},
                {"key": "c", "title": "C", "status": "location_needed"},
            ]
        }))

        remaining = _pending_work_candidates(work)

        self.assertEqual([item["key"] for item in remaining], ["b", "c"])

    def test_search_more_followup_skips_meta_question_and_inherits_food_request(self) -> None:
        rows = [
            SimpleNamespace(
                role="user",
                content="선양에서 먹어야 되는 음식 종류를 찾고 그 식당을 지도에 등록해줬으면 좋겠어",
            ),
            SimpleNamespace(role="assistant", content="저장 기준을 충족하지 못했습니다."),
            SimpleNamespace(role="user", content="충족하지 못한 기준이 무엇인데?"),
            SimpleNamespace(role="assistant", content="주소와 좌표가 필요합니다."),
        ]

        work = SimpleNamespace(
            id=1,
            action="research_and_write",
            scope="all",
            subject="food",
            goal="선양에서 먹어야 되는 음식 종류를 찾고 그 식당을 지도에 등록",
            requested_count=4,
            state=json.dumps({"wants_research": True, "wants_write": True, "candidates": []}),
        )
        intent = _inherit_active_intent(ChatIntent(
            action="continue", continuation=True, wants_research=True, wants_write=True
        ), work)
        resolved = _resolve_context_message(
            "검색 충분히 해서 지도에 추가해줘.", rows, intent=intent, work=work
        )
        city = City(name_local="沈阳")

        self.assertIn("먹어야 되는 음식 종류", resolved)
        self.assertNotIn("충족하지 못한 기준", resolved)
        self.assertEqual(
            _research_seed_queries(city, resolved, subject="food"),
            ["沈阳 必吃 特色美食 传统小吃", "沈阳 老字号 特色餐厅 推荐"],
        )

    def test_food_coordinate_recovery_searches_by_business_not_bad_address(self) -> None:
        city = City(name_ko="선양", name_local="沈阳")

        query = _food_detail_recovery_query(city, "马家烧麦 沈阳市沈河区中街路195号")

        self.assertEqual(query, "沈阳 马家烧麦 去哪儿攻略")

    def test_food_business_name_ignores_an_address_only_geocode(self) -> None:
        city = City(name_ko="선양", name_local="沈阳")

        self.assertEqual(_food_business_name(city, "沈阳市沈河区正阳街88号"), "")
        self.assertEqual(
            _food_business_name(city, "李连贵熏肉大饼 沈阳 地址"),
            "李连贵熏肉大饼",
        )

    def test_brand_seed_queries_use_correct_chinese_names(self) -> None:
        city = City(name_local="沈阳")
        queries = _research_seed_queries(city, "모어요거트와 헤이티를 등록해줘")

        self.assertIn("沈阳 茉酸奶 大悦城旗舰店 地址", queries)
        self.assertIn("沈阳 喜茶 大悦城店 中街益田假日世界店 地址", queries)
        self.assertTrue(all("海蒂" not in query for query in queries))

    def test_food_registration_wish_is_write_intent_with_short_chinese_seeds(self) -> None:
        city = City(name_local="沈阳")
        message = "선양에서 먹어야 되는 음식 종류를 찾고 식당을 지도에 등록해줬으면 좋겠어"

        write_intent, tools = _chat_capabilities(ChatIntent(
            action="research_and_write", subject="food", wants_research=True, wants_write=True
        ))
        queries = _research_seed_queries(city, message, subject="food")

        self.assertTrue(write_intent)
        self.assertEqual(tools, RESEARCH_TOOLS | WRITE_TOOLS)
        self.assertEqual(queries, ["沈阳 必吃 特色美食 传统小吃", "沈阳 老字号 特色餐厅 推荐"])
        self.assertTrue(all("등록해줬으면" not in query for query in queries))

    def test_snack_request_has_distinct_semantic_queries_and_exclusions(self) -> None:
        city = City(name_local="沈阳")
        intent = ChatIntent(
            action="research_and_write",
            subject="food_snack",
            goal="find portable snacks",
            wants_research=True,
            wants_write=True,
            starts_new_work=True,
            constraints=["takeaway", "walk_and_eat"],
            exclusions=["full_meal", "sit_down_restaurant"],
        ).normalized()

        queries = _research_seed_queries(
            city,
            "완전한 식사 말고 간식을 찾아서 추가해줘",
            subject=intent.subject,
        )

        self.assertEqual(intent.subject, "food_snack")
        self.assertIn("full_meal", intent.exclusions)
        self.assertTrue(any("外带" in query for query in queries))
        self.assertTrue(any("糕点" in query for query in queries))
        self.assertTrue(all("特色餐厅" not in query for query in queries))

    def test_answer_validation_requires_each_brand_and_removes_fake_urls(self) -> None:
        self.assertEqual(
            _missing_brand_targets("모어요거트와 헤이티", "喜茶 지점만 확인했습니다."),
            ["모어요거트"],
        )

    def test_brand_sources_exclude_unrelated_search_results(self) -> None:
        items = [
            {"title": "喜茶(沈阳大悦城店)", "body": "沈阳门店", "href": "https://example.com/heytea"},
            {"title": "沈阳酒店", "body": "住宿", "href": "https://example.com/hotel"},
        ]

        self.assertEqual(_brand_source_urls("헤이티", items), ["https://example.com/heytea"])
        self.assertNotIn(
            "https://fake.example/123",
            _strip_unsupported_urls(
                "가짜 링크 https://fake.example/123",
                {"https://real.example/place"},
            ),
        )

    def test_optional_proposal_fields_accept_model_nulls(self) -> None:
        proposal = next(tool for tool in TOOLS if tool["function"]["name"] == "propose_place")
        properties = proposal["function"]["parameters"]["properties"]
        required = proposal["function"]["parameters"]["required"]

        for field in ("zone_id", "branch_name", "coordinate_external_id", "coordinate_source_url"):
            self.assertIn("null", properties[field]["type"])
        self.assertNotIn("confidence", required)
        self.assertNotIn("evidence", required)
        self.assertIn("consumption_mode", properties)
        self.assertNotIn("confidence", properties["insights"]["items"]["required"])

    def test_semantic_snack_guard_rejects_meal_and_allows_treat(self) -> None:
        responses = iter([
            {"allowed": False, "consumption_mode": "full_meal", "reason": "초밥은 보통 한 끼 식사입니다.", "confidence": 0.96},
            {"allowed": True, "consumption_mode": "packaged", "reason": "사탕은 소량 포장 간식입니다.", "confidence": 0.99},
        ])

        class Completions:
            def create(self, **_kwargs):
                content = json.dumps(next(responses), ensure_ascii=False)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

        sushi = _classify_snack_fit(client, model="test-model", candidate={"title": "外带寿司"})
        candy = _classify_snack_fit(client, model="test-model", candidate={"title": "不老林糖果"})

        self.assertFalse(sushi["allowed"])
        self.assertEqual(sushi["consumption_mode"], "full_meal")
        self.assertTrue(candy["allowed"])
        self.assertEqual(candy["consumption_mode"], "packaged")


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
                        arguments=json.dumps(
                            {
                                "title": "喜茶沈阳大悦城店 (헤이티 선양 다웨청점)",
                                "lat": 41.8,
                                "lng": 123.45,
                                "source_urls": ["https://example.com/branch"],
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ),
                SimpleNamespace(
                    id="write-2",
                    function=SimpleNamespace(
                        name="propose_place",
                        arguments=json.dumps(
                            {
                                "title": "茉酸奶沈阳大悦城旗舰店 (모어요거트 선양 다웨청 플래그십점)",
                                "lat": 41.8,
                                "lng": 123.45,
                                "source_urls": ["https://example.com/branch"],
                            },
                            ensure_ascii=False,
                        ),
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


class _SnackResearchThenWriteCompletions:
    """Reproduces Groq asking for more research after a coordinate appears."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    @staticmethod
    def proposal_call(call_id: str, title: str, url: str, lat: float, lng: float):
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name="propose_place",
                arguments=json.dumps({
                    "title": title,
                    "category": "restaurant",
                    "travel_role": "food",
                    "lat": lat,
                    "lng": lng,
                    "evidence": "간식 판매점 상세 페이지에서 상호, 지점과 위치를 확인했습니다.",
                    "source_urls": [url],
                    "insights": [
                        {"kind": "location", "title": "위치", "content": "상세 페이지 위치", "source_url": url},
                        {"kind": "tip", "title": "간식", "content": "포장 가능한 소량 간식", "source_url": url},
                    ],
                    "confidence": 0.9,
                }, ensure_ascii=False),
            ),
        )

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            call = SimpleNamespace(
                id="research-more",
                function=SimpleNamespace(
                    name="web_search",
                    arguments=json.dumps({"query": "沈阳 中街 冰点 外带 地址", "max_results": 5}, ensure_ascii=False),
                ),
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))])
        calls = [
            self.proposal_call("snack-1", "中街冰点 (중제 빙과점)", "https://example.com/ice", 41.801, 123.451),
            self.proposal_call("snack-2", "老字号糕点铺 (노포 제과점)", "https://example.com/pastry", 41.802, 123.452),
        ]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=calls))])


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

    def test_unrelated_answer_does_not_mutate_open_work(self) -> None:
        work = TravelChatWork(
            user_id=1,
            city_id=1,
            action="research_and_write",
            scope="remaining",
            subject="food",
            goal="Add the remaining restaurants",
            state=json.dumps({
                "phase": "write",
                "next_action": "write",
                "candidates": [{"key": "food-1", "title": "Food candidate"}],
            }),
        )
        self.db.add(work)
        self.db.commit()
        original_state = work.state

        selected = _ensure_chat_work(
            self.db,
            user_id=1,
            city_id=1,
            intent=ChatIntent(action="answer", subject="itinerary"),
            active_work=work,
        )

        self.assertIsNone(selected)
        self.db.refresh(work)
        self.assertEqual(work.state, original_state)

    def test_tool_budget_always_ends_with_no_tool_synthesis(self) -> None:
        completions = _FakeCompletions(tool_rounds=3)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat._classify_chat_intent", return_value=ChatIntent(
                action="research", subject="drink", goal="호텔 근처 차 지점 조사",
                wants_research=True, starts_new_work=True,
            )),
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
        self.assertIn(
            "web_search",
            {item["function"]["name"] for item in completions.requests[0]["tools"]},
        )
        self.assertEqual(tool.call_args_list[0].args[1], "web_search")
        self.assertIn("沈阳", tool.call_args_list[0].args[2]["query"])
        self.assertNotIn("tools", completions.requests[-1])

    def test_snack_write_allows_more_research_instead_of_forced_tool_400(self) -> None:
        completions = _SnackResearchThenWriteCompletions()
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        places = {
            "ice": ("中街冰点", 41.801, 123.451),
            "pastry": ("老字号糕点铺", 41.802, 123.452),
            "candy": ("不老林糖果", 41.803, 123.453),
        }
        proposal_number = 40

        def fake_tool(_db, name, args, *, city_id):
            nonlocal proposal_number
            self.assertEqual(city_id, 1)
            if name == "web_search":
                query = str(args.get("query") or "")
                key = "pastry" if "糕点" in query else "candy" if "甜品" in query else "ice"
                title, _lat, _lng = places[key]
                return {"results": [{
                    "href": f"https://example.com/{key}",
                    "title": title,
                    "body": f"沈阳 {title} 外带 간식 판매점",
                }]}
            if name == "fetch_page":
                key = str(args["url"]).rsplit("/", 1)[-1]
                title, lat, lng = places[key]
                return {
                    "url": args["url"],
                    "title": title,
                    "text": f"{title} 포장 간식 판매점 상세 정보",
                    "coordinate_candidates": [{
                        "display_name": title,
                        "lat": lat,
                        "lng": lng,
                        "confidence": 0.9,
                        "storage_allowed": True,
                    }],
                }
            if name == "propose_place":
                proposal_number += 1
                return {"ok": True, "proposal_created": True, "proposal_id": proposal_number}
            return {"results": []}

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat._classify_chat_intent", return_value=ChatIntent(
                action="research_and_write", scope="all", subject="food_snack",
                goal="식사가 아닌 간식 판매점을 찾아 지도에 추가", wants_research=True,
                wants_write=True, starts_new_work=True, requested_count=2,
                constraints=["takeaway", "walk_and_eat"], exclusions=["full_meal"],
            )),
            patch("app.travel_chat._classify_snack_fit", return_value={
                "ok": True, "allowed": True, "consumption_mode": "snack",
                "reason": "소량 포장 간식", "confidence": 0.99,
            }),
            patch("app.travel_chat.run_tool", side_effect=fake_tool) as tool,
        ):
            result = answer_travel_chat(
                self.db,
                user_id=1,
                city_id=1,
                message="완전 식사 말고 간식도 찾아서 추가해줘.",
            )

        self.assertEqual(completions.requests[0]["tool_choice"], "required")
        self.assertIn(
            "web_search",
            {item["function"]["name"] for item in completions.requests[0]["tools"]},
        )
        self.assertTrue(any(call.args[1] == "web_search" for call in tool.call_args_list))
        self.assertEqual(sum(call.args[1] == "propose_place" for call in tool.call_args_list), 2)
        work = self.db.query(TravelChatWork).one()
        state = json.loads(work.state)
        self.assertEqual(len(state["candidates"]), 2)
        self.assertEqual({item["status"] for item in state["candidates"]}, {"proposed"})
        self.assertEqual(len(state["completed_keys"]), 2)
        self.assertEqual(work.status, "completed")
        self.assertIn("승인 대기 제안", result["row"].content)

    def test_continuation_counts_a_prior_success_and_only_writes_the_remainder(self) -> None:
        prior = {
            "key": "prior-snack",
            "title": "Prior Snack Stall",
            "address": "Zhongjie 1",
            "category": "restaurant",
            "status": "proposed",
            "proposal_id": 40,
            "source_urls": ["https://example.com/prior"],
            "lat": 41.8,
            "lng": 123.45,
        }
        self.db.add(TravelChatWork(
            user_id=1,
            city_id=1,
            action="research_and_write",
            scope="all",
            subject="food_snack",
            goal="add_snack_places",
            requested_count=2,
            state=json.dumps({
                "phase": "write",
                "next_action": "write",
                "candidates": [prior],
                "completed_keys": ["prior-snack"],
                "constraints": ["snack_portion"],
                "exclusions": ["full_meal"],
            }),
        ))
        self.db.commit()

        class OneWriteCompletions:
            def __init__(self) -> None:
                self.requests: list[dict] = []

            def create(inner_self, **kwargs):
                inner_self.requests.append(kwargs)
                call = _SnackResearchThenWriteCompletions.proposal_call(
                    "new-snack",
                    "New Snack Stall",
                    "https://example.com/new",
                    41.801,
                    123.451,
                )
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="", tool_calls=[call]),
                )])

        completions = OneWriteCompletions()
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client
        proposal_calls = 0

        def fake_tool(_db, name, args, *, city_id):
            nonlocal proposal_calls
            self.assertEqual(city_id, 1)
            if name == "web_search":
                return {"results": [{
                    "href": "https://example.com/new",
                    "title": "New Snack Stall",
                    "body": "Shenyang portable snack stall",
                }]}
            if name == "fetch_page":
                return {
                    "url": "https://example.com/new",
                    "title": "New Snack Stall",
                    "text": "New Snack Stall sells portable snacks.",
                    "coordinate_candidates": [{
                        "display_name": "New Snack Stall",
                        "lat": 41.801,
                        "lng": 123.451,
                        "confidence": 0.9,
                        "storage_allowed": True,
                    }],
                }
            if name == "propose_place":
                proposal_calls += 1
                return {"ok": True, "proposal_created": True, "proposal_id": 41}
            return {"results": []}

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat._classify_chat_intent", return_value=ChatIntent(
                action="continue", scope="all", subject="food_snack",
                goal="add_snack_places", wants_research=True, wants_write=True,
                continuation=True, requested_count=2,
                constraints=["snack_portion"], exclusions=["full_meal"],
            )),
            patch("app.travel_chat._classify_snack_fit", return_value={
                "ok": True, "allowed": True, "consumption_mode": "snack",
                "reason": "portable snack", "confidence": 0.99,
            }),
            patch("app.travel_chat.run_tool", side_effect=fake_tool),
        ):
            answer_travel_chat(self.db, user_id=1, city_id=1, message="continue")

        work = self.db.query(TravelChatWork).one()
        state = json.loads(work.state)
        self.assertEqual(proposal_calls, 1)
        self.assertEqual(work.status, "completed")
        self.assertEqual(len(state["candidates"]), 2)
        self.assertEqual(len(state["completed_keys"]), 2)

    def test_simple_question_uses_one_tool_free_completion(self) -> None:
        completions = _FakeCompletions(tool_rounds=0)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat._classify_chat_intent", return_value=ChatIntent(
                action="answer", subject="itinerary", goal="동선 단축",
            )),
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
                return {
                    "results": [{
                        "href": "https://example.com/branch",
                        "title": _args["query"],
                        "body": _args["query"],
                    }]
                }
            if name == "fetch_page":
                return {"url": "https://example.com/branch", "title": "branch", "text": "verified"}
            if name == "geocode_place":
                if "茉酸奶" in _args["query"] or "喜茶" in _args["query"]:
                    return {"results": []}
                return {"results": [{
                    "display_name": "沈阳大悦城",
                    "lat": 41.8,
                    "lng": 123.45,
                    "source": "osm",
                    "storage_allowed": True,
                }]}
            if name == "propose_place":
                return {"ok": True, "proposal_created": True, "proposal_id": 31}
            return {"error": "unexpected_tool"}

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat._classify_chat_intent", return_value=ChatIntent(
                action="write", scope="all", subject="drink",
                goal="모어요거트와 헤이티를 지도에 추가", wants_research=True,
                wants_write=True, continuation=True, requested_count=2,
            )),
            patch("app.travel_chat.run_tool", side_effect=fake_tool) as tool,
        ):
            result = answer_travel_chat(self.db, user_id=1, city_id=1, message="추가해줘")

        self.assertIn("실제 저장", result["row"].content)
        self.assertTrue(any(call.args[1] == "propose_place" for call in tool.call_args_list))
        self.assertTrue(any(
            call.args[1] == "geocode_place" and "大悦城C区" in call.args[2]["query"]
            for call in tool.call_args_list
        ))
        self.assertEqual(completions.requests[0]["tools"][0]["function"]["name"], "propose_place")
        self.assertEqual(
            completions.requests[0]["tool_choice"],
            {"type": "function", "function": {"name": "propose_place"}},
        )
        # Write results are rendered deterministically from the execution ledger;
        # no final tool-disabled model call can swallow a ready proposal anymore.
        self.assertEqual(len(completions.requests), 1)

    def test_grounded_candidate_survives_failed_followup_without_target_drift(self) -> None:
        candidate = {
            "key": "candidate-1",
            "title": "绿波廊SPA会馆（中街店）",
            "address": "小什字街66号",
            "category": "other",
            "status": "location_needed",
            "source_urls": ["https://m.dianping.com/shop/example"],
            "lat": None,
            "lng": None,
            "confidence": 0.9,
        }
        self.db.add_all([
            TravelChatMessage(
                user_id=1,
                city_id=1,
                role="user",
                content="중제쪽 마사지샵도 찾아줘",
                sources="[]",
                place_ids="[]",
            ),
            TravelChatMessage(
                user_id=1,
                city_id=1,
                role="assistant",
                content="도구 근거가 있는 후보입니다.",
                sources=json.dumps(candidate["source_urls"]),
                place_ids="[]",
                candidates=json.dumps([candidate], ensure_ascii=False),
            ),
        ])
        self.db.commit()
        completions = _FakeCompletions(tool_rounds=0)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat._classify_chat_intent", return_value=ChatIntent(
                action="write", scope="single", subject="spa", goal="마사지 후보 등록",
                wants_research=True, wants_write=True, continuation=True, requested_count=1,
            )),
            patch("app.travel_chat.run_tool", return_value={"results": []}) as tool,
        ):
            result = answer_travel_chat(self.db, user_id=1, city_id=1, message="등록해줘")

        search_calls = [call for call in tool.call_args_list if call.args[1] == "web_search"]
        self.assertEqual(
            search_calls[0].args[2]["query"],
            "沈阳 绿波廊SPA会馆（中街店） 小什字街66号 地址",
        )
        saved_candidates = json.loads(result["row"].candidates)
        self.assertEqual(saved_candidates[0]["title"], candidate["title"])
        self.assertEqual(saved_candidates[0]["status"], "location_needed")
        self.assertIn("다른 업소로 바꾸지 않았습니다", result["row"].content)
        trace = json.loads(result["row"].tool_trace)
        self.assertTrue(any(item["tool"] == "web_search" for item in trace))

    def test_existing_brand_places_skip_duplicate_research_and_proposals(self) -> None:
        self.db.add_all([
            Marker(
                city_id=1,
                category=MarkerCategory.drink,
                title="喜茶沈阳大悦城店 (헤이티 선양 다웨청점)",
                lat=41.8007,
                lng=123.4637,
            ),
            Marker(
                city_id=1,
                category=MarkerCategory.drink,
                title="茉酸奶沈阳大悦城旗舰店 (모어요거트 선양 다웨청 플래그십점)",
                lat=41.8007,
                lng=123.4637,
            ),
        ])
        self.db.commit()
        completions = _FakeCompletions(tool_rounds=0)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat._classify_chat_intent", return_value=ChatIntent(
                action="research_and_write", scope="all", subject="drink",
                goal="헤이티와 모어요거트를 지도에 추가", wants_research=True,
                wants_write=True, starts_new_work=True, requested_count=2,
            )),
            patch("app.travel_chat.run_tool") as tool,
        ):
            result = answer_travel_chat(
                self.db,
                user_id=1,
                city_id=1,
                message="헤이티와 모어요거트를 지도에 추가해줘",
            )

        tool.assert_not_called()
        self.assertIn("이미 지도에 등록", result["row"].content)
        self.assertEqual(len(result["place_ids"]), 2)

    def test_final_model_tool_error_never_becomes_http_500(self) -> None:
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FailingCompletions()))
        fake_groq = types.ModuleType("groq")
        fake_groq.Groq = lambda **_kwargs: fake_client

        with (
            patch.dict(sys.modules, {"groq": fake_groq}),
            patch.object(settings, "groq_api_key", "test-key"),
            patch("app.travel_chat._classify_chat_intent", return_value=ChatIntent(
                action="answer", subject="general", goal="현재 지도 설명",
            )),
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
