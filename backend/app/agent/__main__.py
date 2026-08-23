"""Scheduled ECS entry point: ``python -m app.agent``.

When Step Functions supplies a task token, this process reports a classified
result back to the workflow. A Groq edge/network denial is deliberately
reported as ``RetryableNetworkBlock`` so the workflow can start a fresh
Fargate task with a fresh ENI and public IP.
"""

from __future__ import annotations

import json
import os
import traceback
from typing import Any

import boto3

from app.agent.runner import run_agent
from app.db import Base, SessionLocal, engine
from app.migrate import ensure_schema
from app.models import City


RETRYABLE_NETWORK_MARKERS = (
    "access denied. please check your network settings",
    "error code: 403",
    "status code: 403",
    "status_code=403",
)

RETRYABLE_MODEL_OUTPUT_MARKERS = (
    "output_parse_failed",
    "tool_use_failed",
    "failed to parse tool call",
    "tool call validation failed",
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


def is_retryable_model_output_failure(result: dict[str, Any]) -> bool:
    """Recognize provider-side structured/tool output failures after local retries."""

    if result.get("status") != "failed":
        return False
    message = str(result.get("message") or "").casefold()
    return any(marker in message for marker in RETRYABLE_MODEL_OUTPUT_MARKERS)


def _compact_result(results: list[dict[str, Any]]) -> str:
    """Keep callback payloads well below the Step Functions output limit."""

    payload = {
        "cities": [
            {
                "city_id": result.get("city_id"),
                "run_id": result.get("run_id"),
                "ok": result.get("ok"),
                "status": result.get("status"),
                "outcome": result.get("outcome"),
                "steps": result.get("steps"),
                "score": result.get("score"),
                "message": str(result.get("message") or "")[:4000],
                "unread_before": result.get("unread_before", 0),
                "unread_after": result.get("unread_after", 0),
                "performance": (
                    result.get("performance")
                    if isinstance(result.get("performance"), dict)
                    else {}
                ),
                "remaining_gaps": [
                    str(gap)[:300]
                    for gap in (
                        result.get("remaining_gaps")
                        if isinstance(result.get("remaining_gaps"), list)
                        else []
                    )[:20]
                ],
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
    retryable_model_output = [
        result for result in failed if is_retryable_model_output_failure(result)
    ]
    payload = _compact_result(results)

    if retryable:
        client.send_task_failure(
            taskToken=task_token,
            error="RetryableNetworkBlock",
            cause=payload[:32000],
        )
        return "retryable_network_block"
    if retryable_model_output:
        client.send_task_failure(
            taskToken=task_token,
            error="RetryableModelOutput",
            cause=payload[:32000],
        )
        return "retryable_model_output"
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
    ensure_schema()
    db = SessionLocal()
    try:
        return [run_agent(db, city_id=city.id) for city in _selected_cities(db)]
    finally:
        db.close()


def main() -> None:
    try:
        results = run_scheduled_agent()
    except Exception as exc:
        failure = {
            "city_id": int(os.getenv("AGENT_CITY_ID") or 0) or None,
            "ok": False,
            "status": "failed",
            "steps": 0,
            "score": 0.0,
            "message": f"scheduled agent crashed: {type(exc).__name__}: {exc}"[:4000],
        }
        results = [failure]
        traceback.print_exc()
        task_token = os.getenv("SFN_TASK_TOKEN")
        if task_token:
            outcome = report_step_function_result(task_token, results)
            print(json.dumps({"step_functions_outcome": outcome}), flush=True)
            return
        raise
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
