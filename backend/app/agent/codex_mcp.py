"""City-scoped business tools exposed to the official Codex harness over MCP.

The subprocess owns its SQLAlchemy sessions. The database ledger, not a local
Codex transcript, is the durable source of tool outcomes and research evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

from app.agent.tools import TOOLS, is_useful_fetched_page, run_tool
from app.coordinate_attestation import issue_coordinate_attestation, trusted_coordinate_evidence
from app.db import SessionLocal
from app.models import AgentRun, AgentRunStep, TravelChatWork


READ_TOOLS = frozenset({
    "list_places", "get_place", "find_nearby_candidates", "list_zones",
    "list_knowledge", "list_research_history", "list_agent_tasks",
    "list_stale_places", "web_search", "fetch_page", "geocode_place",
})
CHAT_TOOLS = READ_TOOLS | {"propose_place", "checkpoint_travel_work"}
BATCH_TOOLS = CHAT_TOOLS | {
    "list_unread_events", "mark_events_read", "list_open_appeals",
    "list_recent_rollbacks", "upsert_place_insights", "verify_place",
    "search_place_images", "attach_image_from_url", "assign_place_zone",
    "assign_place_chain",
}
_SCHEMAS = {item["function"]["name"]: item["function"] for item in TOOLS}
_SCHEMAS["checkpoint_travel_work"] = {
    "description": "Persist a concise observed-work handoff for the next conversation or batch. Facts must be grounded; never private reasoning.",
    "parameters": {"type": "object", "properties": {
        "state_summary": {"type": "string"}, "next_action": {"type": "string"},
        "hypothesis": {"type": "string"}, "blocked_reason": {"type": "string"},
        "retry_condition": {"type": "string"},
    }, "required": ["state_summary", "next_action"], "additionalProperties": False},
}
_MUTATIONS = BATCH_TOOLS - READ_TOOLS - {
    "list_unread_events", "list_open_appeals", "list_recent_rollbacks", "search_place_images", "checkpoint_travel_work",
}


def json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _clean_result(value: Any) -> Any:
    """Brave POI discovery is disabled; also fail closed on unexpected payloads."""
    if isinstance(value, list):
        return [_clean_result(item) for item in value]
    if isinstance(value, dict):
        if str(value.get("source") or "").casefold().startswith("brave_place"):
            return {"source": "transient_provider_discarded"}
        return {
            key: _clean_result(item)
            for key, item in value.items()
            if key not in {"place_candidates", "place_candidate_summary", "provider_attempts"}
        }
    return value


@dataclass(frozen=True)
class ToolContext:
    city_id: int
    mode: str
    run_id: int
    user_id: int | None = None
    max_tool_calls: int = 100


class TravelToolBridge:
    def __init__(self, context: ToolContext, session_factory: Callable = SessionLocal):
        self.context = context
        self.session_factory = session_factory

    @property
    def allowed(self) -> frozenset[str]:
        return CHAT_TOOLS if self.context.mode == "chat" else BATCH_TOOLS

    def tools(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": _SCHEMAS[name].get("description", ""),
             "inputSchema": _SCHEMAS[name]["parameters"]}
            for name in sorted(self.allowed) if name in _SCHEMAS
        ]

    def _owner(self, db: Any) -> Any:
        if self.context.mode == "chat":
            return db.query(TravelChatWork).filter(
                TravelChatWork.id == self.context.run_id,
                TravelChatWork.city_id == self.context.city_id,
                TravelChatWork.user_id == self.context.user_id,
                TravelChatWork.status == "active",
            ).first()
        return db.query(AgentRun).filter(
            AgentRun.id == self.context.run_id,
            AgentRun.city_id == self.context.city_id,
            AgentRun.status == "running",
        ).first()

    def _load(self, owner: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        container = json_object(owner.state if self.context.mode == "chat" else owner.metrics)
        ledger = container.get("codex")
        return container, ledger if isinstance(ledger, dict) else {}

    def _save(self, db: Any, owner: Any, container: dict, ledger: dict) -> None:
        container["codex"] = ledger
        encoded = json.dumps(container, ensure_ascii=False, default=str)
        if self.context.mode == "chat":
            owner.state = encoded
        else:
            owner.metrics = encoded
        db.commit()

    def _prepare(self, name: str, args: dict, ledger: dict, db: Any) -> tuple[dict, dict | None]:
        # Model-supplied arguments can never manufacture server provenance or
        # change the authenticated city/user or approval mode.
        args = {key: value for key, value in args.items()
                if not key.startswith("_") and key not in {"city_id", "approved", "user_id"}}
        pages = [item for item in ledger.get("pages", []) if isinstance(item, dict)]
        urls = {str(item.get("url") or "") for item in pages}
        if name == "mark_events_read":
            observed = set(ledger.get("observed_event_ids", []))
            if any(item not in observed for item in args.get("event_ids", [])):
                return args, {"error": "event_not_observed", "detail": "Read each event before acknowledging it."}
        if name == "attach_image_from_url":
            if str(args.get("image_url") or "") not in ledger.get("image_urls", []):
                return args, {"error": "image_not_observed", "detail": "Select a returned search_place_images result."}
        if name in {"propose_place", "upsert_place_insights", "verify_place"}:
            args["_validated_source_urls"] = sorted(urls)
            args["_validated_source_documents"] = pages
        if name == "propose_place":
            from app.agent.runner import _page_supports_coordinate_evidence
            from app.travel_chat import _coordinate_record_matches_proposal

            coordinates = [item for item in ledger.get("coordinates", []) if isinstance(item, dict)]
            if self.context.mode == "chat":
                owner = self._owner(db)
                state = json_object(owner.state) if owner is not None else {}
                for candidate in state.get("candidates", []):
                    evidence = trusted_coordinate_evidence(candidate) if isinstance(candidate, dict) else None
                    if evidence is not None:
                        coordinates.append(evidence)
            selected = next((item for item in coordinates
                             if item.get("storage_allowed") is True
                             and _coordinate_record_matches_proposal(args, item)), None)
            if selected is None:
                return args, {"error": "coordinate_target_not_verified",
                              "detail": "Use geocode_place or a fetched exact-POI page; coordinates must match this branch."}
            matched_pages = [page for page in pages if _page_supports_coordinate_evidence(page, selected)]
            if not matched_pages:
                return args, {"error": "business_evidence_required",
                              "detail": "Fetch a page proving this exact business/branch before proposing it."}
            args["lat"], args["lng"] = selected["lat"], selected["lng"]
            args["_coordinate_evidence"] = selected
            args["_validated_source_documents"] = matched_pages
            args["_validated_source_urls"] = [page["url"] for page in matched_pages]
        return args, None

    def execute(self, name: str, arguments: Any) -> Any:
        db = self.session_factory()
        try:
            owner = self._owner(db)
            if owner is None:
                return {"error": "inactive_or_foreign_execution"}
            container, ledger = self._load(owner)
            count = int(ledger.get("tool_calls") or 0)
            if count >= self.context.max_tool_calls:
                ledger["ceiling_reached"] = True
                ledger["stop_reason"] = "safety_limit"
                if name != "checkpoint_travel_work":
                    self._save(db, owner, container, ledger)
                    return {"error": "tool_safety_ceiling",
                            "detail": "Use checkpoint_travel_work once to preserve the exact unfinished work, then summarize."}
                if ledger.get("post_ceiling_checkpoint"):
                    return {"ok": True, "checkpoint": ledger.get("handoff", {}),
                            "detail": "Final handoff already persisted; summarize now."}
                ledger["post_ceiling_checkpoint"] = True
            sequence = count + 1
            ledger["tool_calls"] = sequence
            # Reserve the action before executing a tool that may commit itself.
            ledger["in_flight"] = {"sequence": sequence, "tool": name}
            self._save(db, owner, container, ledger)
            args = arguments if isinstance(arguments, dict) else {}
            safe_args = {key: value for key, value in args.items() if not key.startswith("_")}
            result: Any = None
            if name not in self.allowed:
                result = {"error": "tool_not_allowed"}
            elif not isinstance(arguments, dict):
                result = {"error": "malformed_tool_arguments"}
            else:
                try:
                    effective, result = self._prepare(name, args, ledger, db)
                except (ValueError, TypeError, KeyError):
                    effective, result = {}, {"error": "invalid_tool_arguments"}
                safe_args = {key: value for key, value in effective.items() if not key.startswith("_")}
                if result is None:
                    if name == "propose_place":
                        # Persist the exact intended entity before run_tool can
                        # commit its proposal. Reconciliation can then recover
                        # an action even if the subprocess dies after that commit.
                        evidence = effective.get("_coordinate_evidence") or {}
                        pending = issue_coordinate_attestation({
                            "key": hashlib.sha256(str(effective.get("title") or "").encode("utf-8")).hexdigest()[:20],
                            "title": effective.get("title", ""), "address": effective.get("address", ""),
                            "category": effective.get("category", "other"),
                            "lat": effective.get("lat"), "lng": effective.get("lng"),
                            "source_urls": effective.get("source_urls", []), "status": "pending",
                        }, evidence)
                        ledger["candidates"] = [*ledger.get("candidates", []), pending][-30:]
                        if self.context.mode == "chat":
                            container["candidates"] = [*container.get("candidates", []), pending][-30:]
                        self._save(db, owner, container, ledger)
                    try:
                        if name == "checkpoint_travel_work":
                            result = {"ok": True, "checkpoint": {
                                key: str(effective.get(key) or "")[:3000]
                                for key in ("state_summary", "next_action", "hypothesis", "blocked_reason", "retry_condition")
                            }}
                        else:
                            result = run_tool(db, name, effective, city_id=self.context.city_id,
                                              approved=False, server_allow_brave_places=False)
                    except Exception as exc:
                        db.rollback()
                        result = {"error": "business_tool_failed", "detail": str(exc)[:500]}
            from app.agent.runner import _persistable_tool_result
            result = _clean_result(_persistable_tool_result(name, result))
            # Re-read because the business tool may commit or roll back.
            db.expire_all()
            owner = self._owner(db)
            if owner is None:
                return {"error": "execution_ended_during_tool"}
            container, saved = self._load(owner)
            saved.update(ledger)
            ledger = saved
            trace = {"name": name, "args": safe_args, "result": result}
            ledger["tool_trace"] = [*ledger.get("tool_trace", []), trace][-100:]
            ledger["in_flight"] = None
            ledger["tool_counts"] = dict(ledger.get("tool_counts") or {})
            ledger["tool_counts"][name] = int(ledger["tool_counts"].get(name) or 0) + 1
            error = result.get("error") if isinstance(result, dict) else None
            if error:
                ledger["last_error"] = {"tool": name, "error": error}
            else:
                ledger["successful_tool_counts"] = dict(ledger.get("successful_tool_counts") or {})
                ledger["successful_tool_counts"][name] = int(ledger["successful_tool_counts"].get(name) or 0) + 1
            self._observe(ledger, name, safe_args, result)
            if ledger.get("handoff"):
                container["next_action"] = ledger["handoff"].get("next_action", "")
                container["state_summary"] = ledger["handoff"].get("state_summary", "")
            if self.context.mode == "chat":
                container["tool_trace"] = ledger["tool_trace"]
                candidates = {item.get("key"): item for item in container.get("candidates", [])
                              if isinstance(item, dict) and item.get("key")}
                for item in ledger.get("candidates", []):
                    candidates[item["key"]] = item
                container["candidates"] = list(candidates.values())[-30:]
                container["proposal_ids"] = list(dict.fromkeys([
                    *container.get("proposal_ids", []), *ledger.get("proposal_ids", []),
                ]))
                if name == "propose_place":
                    container["wants_write"] = True
            else:
                from app.agent.runner import _is_material_change, _step_detail_json
                db.add(AgentRunStep(run_id=owner.id, sequence=sequence, tool=name,
                                    phase="act" if name in _MUTATIONS else "observe",
                                    outcome="error" if error else "ok",
                                    detail=_step_detail_json(safe_args, result, {
                                        "material_change": _is_material_change(name, result),
                                    })))
                container["tool_counts"] = ledger["tool_counts"]
                container["material_changes"] = ledger.get("material_changes", [])
                container["material_change_count"] = len(container["material_changes"])
            self._save(db, owner, container, ledger)
            return result
        finally:
            db.close()

    def _observe(self, ledger: dict, name: str, args: dict, result: Any) -> None:
        if name == "list_unread_events" and isinstance(result, list):
            ledger["observed_event_ids"] = list(dict.fromkeys([
                *ledger.get("observed_event_ids", []),
                *[item["id"] for item in result if isinstance(item, dict) and item.get("id") is not None],
            ]))
        if not isinstance(result, dict) or result.get("error"):
            return
        if name == "checkpoint_travel_work":
            ledger["handoff"] = result["checkpoint"]
        if name == "fetch_page" and is_useful_fetched_page(result):
            page = {"url": str(result.get("url") or args.get("url") or ""),
                    "title": str(result.get("title") or "")[:300],
                    "text": str(result.get("text") or "")[:7000],
                    "coordinate_candidates": result.get("coordinate_candidates") or []}
            ledger["pages"] = [*ledger.get("pages", []), page][-30:]
        rows = result.get("results", []) if name == "geocode_place" else result.get("coordinate_candidates", []) if name == "fetch_page" else []
        for raw in rows:
            if not isinstance(raw, dict) or raw.get("storage_allowed") is not True:
                continue
            if str(raw.get("source") or "").casefold().startswith("brave"):
                continue
            record = {**raw, "display_name": str(raw.get("display_name") or raw.get("title") or result.get("title") or args.get("query") or ""),
                      "source_url": str(raw.get("source_url") or result.get("url") or "")}
            try:
                float(record["lat"]), float(record["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            ledger["coordinates"] = [*ledger.get("coordinates", []), record][-50:]
        if name == "search_place_images":
            ledger["image_urls"] = list(dict.fromkeys([
                *ledger.get("image_urls", []),
                *[str(item.get("image_url") or "") for item in result.get("results", []) if isinstance(item, dict)],
            ]))[-100:]
        if name == "propose_place" and result.get("ok"):
            key = hashlib.sha256(str(args.get("title") or "").encode("utf-8")).hexdigest()[:20]
            candidate = {"key": key, "title": args.get("title", ""), "address": args.get("address", ""),
                         "category": args.get("category", "other"), "lat": args.get("lat"), "lng": args.get("lng"),
                         "source_urls": args.get("source_urls", []), "confidence": args.get("confidence", 0.5),
                         "status": "proposed" if result.get("proposal_id") else "duplicate",
                         "proposal_id": result.get("proposal_id"), "place_id": result.get("existing_place_id")}
            evidence = next((item for item in ledger.get("coordinates", [])
                             if item.get("lat") == candidate["lat"] and item.get("lng") == candidate["lng"]), None)
            candidate = issue_coordinate_attestation(candidate, evidence) if evidence else candidate
            ledger["candidates"] = [*ledger.get("candidates", []), candidate][-30:]
            if result.get("proposal_id"):
                ledger["proposal_ids"] = list(dict.fromkeys([*ledger.get("proposal_ids", []), result["proposal_id"]]))
        from app.agent.runner import _is_material_change
        if _is_material_change(name, result):
            ledger["material_changes"] = [*ledger.get("material_changes", []), {
                "tool": name, "place_id": result.get("place_id") or result.get("existing_place_id"),
                "proposal_id": result.get("proposal_id"), "title": args.get("title", ""),
            }]


def serve(bridge: TravelToolBridge) -> None:
    for line in sys.stdin:
        request: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or "id" not in request:
                continue
            method, params = request.get("method"), request.get("params") or {}
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "cloudmiddle-travel", "version": "1.0.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": bridge.tools()}
            elif method == "tools/call":
                value = bridge.execute(str(params.get("name") or ""), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
                          "isError": bool(isinstance(value, dict) and value.get("error"))}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                  "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32603, "message": str(exc)[:500]}}
        print(json.dumps(response, ensure_ascii=False, default=str), flush=True)


def main() -> None:
    context = ToolContext(city_id=int(os.environ["CODEX_TRAVEL_CITY_ID"]),
                          mode=os.environ.get("CODEX_TRAVEL_MODE", "chat"),
                          user_id=int(os.environ["CODEX_TRAVEL_USER_ID"]) if os.environ.get("CODEX_TRAVEL_USER_ID") else None,
                          run_id=int(os.environ["CODEX_TRAVEL_RUN_ID"]),
                          max_tool_calls=int(os.environ.get("CODEX_TRAVEL_MAX_TOOL_CALLS", "100")))
    if context.mode not in {"chat", "batch"} or context.city_id <= 0 or context.run_id <= 0:
        raise ValueError("Invalid travel execution context")
    serve(TravelToolBridge(context))


if __name__ == "__main__":
    main()
