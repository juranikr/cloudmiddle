"""Opt-in live Codex/MCP smoke using an isolated SQLite database, never production data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-id", help="Read OPENAI_API_KEY from this Secrets Manager secret")
    parser.add_argument("--batch", action="store_true", help="Also run a small autonomous local-data batch")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if args.secret_id:
        import boto3
        secret = json.loads(boto3.client("secretsmanager", region_name="ap-northeast-2")
                            .get_secret_value(SecretId=args.secret_id)["SecretString"])
        os.environ["CODEX_API_KEY"] = secret["OPENAI_API_KEY"]
    if not (os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        parser.error("Set CODEX_API_KEY/OPENAI_API_KEY or pass --secret-id")
    with tempfile.TemporaryDirectory(prefix="wonrae-integration-", ignore_cleanup_errors=True) as directory:
        os.environ.update({"DATABASE_URL": "sqlite:///" + (Path(directory) / "check.db").as_posix(),
                           "APP_DB_MODE": "local", "AGENT_BACKEND": "codex",
                           "CODEX_TIMEOUT_SECONDS": "300", "CODEX_BATCH_TIMEOUT_SECONDS": "300"})
        from app.db import Base, SessionLocal, engine
        from app.models import AgentRun, City, Marker, TravelChatMessage, TravelChatWork, User
        from app.travel_chat import answer_travel_chat
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            db.add(User(id=1, email="integration@example.invalid", display_name="검증 사용자", password_hash="unused"))
            db.add(City(id=1, slug="shenyang-check", name_ko="선양", name_local="沈阳",
                        center_lat=41.8, center_lng=123.45, status="active"))
            db.flush()
            db.add(Marker(id=1, city_id=1, user_id=1, title="검증용 선양 숙소", description="테스트 데이터",
                          lat=41.8, lng=123.45, category="lodging", shape="point"))
            db.commit()
            first = answer_travel_chat(db, user_id=1, city_id=1,
                message="get_place 도구로 장소 #1을 확인하고 그 장소 이름과 도시를 답해줘. 웹 검색이나 저장은 하지 마.")
            assert "검증용" in first["row"].content, first["row"].content
            work = db.query(TravelChatWork).first()
            ledger = json.loads(work.state)["codex"]
            assert ledger.get("status") == "completed", ledger.get("last_error")
            assert ledger.get("tool_counts", {}).get("get_place", 0) >= 1, "MCP tool was not used"
            second = answer_travel_chat(db, user_id=1, city_id=1,
                message="방금 확인한 장소의 이름과 번호만 다시 알려줘. 새 장소를 만들지는 마.")
            assert "검증용" in second["row"].content, second["row"].content
            assert db.query(TravelChatMessage).count() == 4
            print(json.dumps({"chat": "passed", "model": first["model"],
                              "mcp_tool_counts": ledger.get("tool_counts"),
                              "followup": second["row"].content}, ensure_ascii=False))
            if args.batch:
                from app.agent.runner import run_agent
                result = run_agent(db, city_id=1, max_steps=6, autonomous_research=True)
                assert result.get("run_id") and db.get(AgentRun, result["run_id"]), result
                print(json.dumps({"batch": result}, ensure_ascii=False, default=str))
        engine.dispose()


if __name__ == "__main__":
    main()
