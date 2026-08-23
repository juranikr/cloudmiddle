import json
import unittest
from types import SimpleNamespace

from app.agent.candidate_curator import (
    curate_grounded_candidate,
    grounded_candidate_packets,
)
from app.coordinate_attestation import issue_coordinate_attestation


class _StructuredClient:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(self.payload, ensure_ascii=False),
        ))])


def _fetch(title, url, lat, lng, *, address="", branch=""):
    return {
        "name": "fetch_page",
        "args": {"url": url},
        "result": {
            "url": url,
            "title": title,
            "text": f"{title} 地址：{address} 여행자가 포장해 먹을 수 있는 디저트 매장입니다.",
            "coordinate_candidates": [{
                "display_name": title,
                "branch_name": branch,
                "address": address,
                "lat": lat,
                "lng": lng,
                "source": "360map_embedded_poi",
                "source_url": url,
                "external_id": f"poi-{branch}",
                "confidence": 0.88,
                "storage_allowed": True,
            }],
        },
    }


class GroundedCandidatePacketTests(unittest.TestCase):
    def test_coordinate_without_explicit_storage_grant_is_not_promoted(self):
        row = _fetch(
            "임시 후보", "https://example.test/transient", 41.8012, 123.4521,
            address="中街路1号", branch="中街店",
        )
        row["result"]["coordinate_candidates"][0].pop("storage_allowed")

        packets = grounded_candidate_packets([row], city_name="沈阳")

        self.assertEqual(packets, [])

    def test_unsigned_locked_candidate_is_not_promoted_to_coordinate_evidence(self):
        locked = {
            "title": "喜茶(中街店)",
            "branch_name": "中街店",
            "address": "中街路1号",
            "lat": 39.9,
            "lng": 116.4,
            "coordinate_source": "agent_research",
            "source_urls": ["https://example.test/legacy"],
        }

        packets = grounded_candidate_packets([], city_name="沈阳", locked_candidates=[locked])

        self.assertEqual(packets, [])

    def test_signed_locked_candidate_keeps_server_coordinate(self):
        locked = issue_coordinate_attestation({
            "key": "trusted",
            "title": "喜茶(中街店)",
            "branch_name": "中街店",
            "address": "中街路1号",
            "lat": 41.8012,
            "lng": 123.4521,
            "coordinate_source": "amap_share",
            "coordinate_source_url": "https://surl.amap.com/trusted",
            "source_urls": ["https://surl.amap.com/trusted"],
            "confidence": 0.98,
            "storage_allowed": True,
        })

        packets = grounded_candidate_packets([], city_name="沈阳", locked_candidates=[locked])

        self.assertEqual(len(packets), 1)
        self.assertEqual((packets[0]["lat"], packets[0]["lng"]), (41.8012, 123.4521))
        self.assertEqual(packets[0]["coordinate_evidence"]["source"], "amap_share")

    def test_same_chain_different_branches_are_preserved(self):
        rows = [
            _fetch(
                "喜茶(沈阳大悦城店)", "https://m.map.360.cn/a", 41.8007, 123.4637,
                address="小东路6号", branch="沈阳大悦城店",
            ),
            _fetch(
                "喜茶(中街益田店)", "https://m.map.360.cn/b", 41.8014, 123.4520,
                address="中街路268号", branch="中街益田店",
            ),
        ]

        packets = grounded_candidate_packets(rows, city_name="沈阳")

        self.assertEqual(len(packets), 2)
        self.assertNotEqual(packets[0]["candidate_key"], packets[1]["candidate_key"])

    def test_same_branch_provider_variants_are_reconciled(self):
        rows = [
            _fetch(
                "【喜茶(沈阳大悦城店)】电话_地址", "https://m.map.360.cn/a", 41.80070, 123.46370,
                address="小东路6号", branch="沈阳大悦城店",
            ),
            _fetch(
                "喜茶（大悦城店）", "https://touch.travel.qunar.com/poi/2", 41.80073, 123.46372,
                address="辽宁省沈阳市大东区小东路6号", branch="大悦城店",
            ),
        ]

        packets = grounded_candidate_packets(rows, city_name="沈阳")

        self.assertEqual(len(packets), 1)
        self.assertEqual(len(packets[0]["source_urls"]), 2)


class CandidateCurationTests(unittest.TestCase):
    def test_model_can_edit_language_but_cannot_move_coordinate_or_sources(self):
        client = _StructuredClient({
            "accepted": True,
            "reason": "간식 요청과 맞습니다.",
            "local_name": "不老林糖果",
            "korean_name": "부라오린 사탕",
            "branch_name": "中街店",
            "category": "shopping",
            "travel_role": "food",
            "description": "선양의 포장 사탕을 살 수 있는 매장입니다.",
            "evidence": "상세 페이지에서 상호와 주소, 업종을 확인했습니다.",
            "confidence": 0.91,
            "insights": [
                {
                    "kind": "location", "title": "위치", "content": "중제 지점의 주소가 확인됩니다.",
                    "year_label": "", "source_index": 0, "confidence": 0.9,
                },
                {
                    "kind": "tip", "title": "간식", "content": "포장 사탕을 고르기 좋은 매장입니다.",
                    "year_label": "", "source_index": 0, "confidence": 0.8,
                },
            ],
        })
        packet = grounded_candidate_packets([
            _fetch(
                "不老林糖果(中街店)", "https://m.map.360.cn/candy", 41.8012, 123.4521,
                address="中街路123号", branch="中街店",
            )
        ], city_name="沈阳")[0]

        result = curate_grounded_candidate(
            client,
            model="openai/gpt-oss-120b",
            city_name="沈阳",
            user_goal="식사 말고 간식 가게를 추가해줘",
            subject="food_snack",
            packet=packet,
        )

        self.assertTrue(result["ok"], result)
        args = result["args"]
        self.assertEqual((args["lat"], args["lng"]), (41.8012, 123.4521))
        self.assertEqual(args["source_urls"], ["https://m.map.360.cn/candy"])
        self.assertEqual(args["_coordinate_evidence"]["external_id"], "poi-中街店")
        self.assertTrue(client.requests[0]["response_format"]["json_schema"]["strict"])


if __name__ == "__main__":
    unittest.main()
