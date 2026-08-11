"""Scheduled ECS entry point: ``python -m app.agent``.

When Step Functions supplies a task token, this process reports a classified
result back to the workflow. A Groq edge/network denial is deliberately
reported as ``RetryableNetworkBlock`` so the workflow can start a fresh
Fargate task with a fresh ENI and public IP.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

from app.agent.runner import run_agent
from app.db import Base, SessionLocal, engine
from app.models import City


RETRYABLE_NETWORK_MARKERS = (
    "access denied. please check your network settings",
    "error code: 403",
    "status code: 403",
    "status_code=403",
)


def is_retryable_network_block(result: dict[str, Any]) -> bool:
    """Return true only for the provider/network 403 seen from Groq."""

    if result.get("status") != "failed":
        return False
    message = str(result.get("message") or "").lower()
    # The exact Groq/Cloudflare message is sufficient by itself. Generic 403s
    # are retried only when the error also identifies a network/access denial.
    exact_message = RETRYABLE_NETWORK_MARKERS[0] in message
    generic_network_403 = any(marker in message for marker in RETRYABLE_NETWORK_MARKERS[1:]) and (
        "network" in message or "access denied" in message or "cloudflare" in message
    )
    return exact_message or generic_network_403


def _compact_result(results: list[dict[str, Any]]) -> str:
    """Keep callback payloads well below the Step Functions output limit."""

    payload = {
        "cities": [
            {
                "city_id": result.get("city_id"),
                "run_id": result.get("run_id"),
                "ok": result.get("ok"),
                "status": result.get("status"),
                "steps": result.get("steps"),
                "score": result.get("score"),
                "message": str(result.get("message") or "")[:4000],
            }
            for result in results
        ]
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def report_step_function_result(task_token: str, results: list[dict[str, Any]]) -> str:
    """Complete the callback task and return the reported outcome label."""

    client = boto3.client("stepfunctions", region_name=os.getenv("AWS_REGION"))
    failed = [result for result in results if result.get("status") == "failed"]
    retryable = [result for result in failed if is_retryable_network_block(result)]
    payload = _compact_result(results)

    if retryable:
        client.send_task_failure(
            taskToken=task_token,
            error="RetryableNetworkBlock",
            cause=payload[:32000],
        )
        return "retryable_network_block"
    if failed or not results:
        client.send_task_failure(
            taskToken=task_token,
            error="AgentRunFailed",
            cause=payload[:32000],
        )
        return "failed"

    client.send_task_success(taskToken=task_token, output=payload)
    return "success"


def _selected_cities(db: Any) -> list[City]:
    query = db.query(City).filter(City.status == "active")
    city_id = os.getenv("AGENT_CITY_ID")
    if city_id:
        try:
            query = query.filter(City.id == int(city_id))
        except ValueError:
            return []
    return query.order_by(City.sort_order, City.id).all()


def run_scheduled_agent() -> list[dict[str, Any]]:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        return [run_agent(db, city_id=city.id) for city in _selected_cities(db)]
    finally:
        db.close()


def main() -> None:
    results = run_scheduled_agent()
    print(_compact_result(results), flush=True)

    task_token = os.getenv("SFN_TASK_TOKEN")
    if task_token:
        outcome = report_step_function_result(task_token, results)
        print(json.dumps({"step_functions_outcome": outcome}), flush=True)
        return

    # Manual ECS/local invocations must no longer look successful when the
    # agent failed. Exit 75 distinguishes a retryable network/provider error.
    if any(is_retryable_network_block(result) for result in results):
        raise SystemExit(75)
    if not results or any(result.get("status") == "failed" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
