"""Pure recovery policy shared by interactive and autonomous agents.

This module deliberately has no database, provider SDK, or application-model
dependencies.  Callers own persistence and execution; the helpers here only
classify a failure and choose a smaller, auditable retry surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal


FailureKind = Literal[
    "output_parse_failed",
    "tool_schema_failed",
    "malformed_tool_arguments",
    "context_limit",
    "timeout",
    "rate_limit",
    "network_block",
    "authorization_failed",
    "provider_unavailable",
    "unknown",
]
RecoveryMode = Literal["focused_retry", "compact_retry", "minimal_retry"]


MODEL_OUTPUT_FAILURES = frozenset({
    "output_parse_failed",
    "tool_schema_failed",
    "malformed_tool_arguments",
})

RESEARCH_TOOLS = frozenset({"web_search", "fetch_page", "geocode_place"})
WRITE_TOOLS = frozenset({"propose_place", "upsert_place_insights"})

_FAILURE_MARKERS: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    # Provider codes are more precise than the explanatory text that follows.
    ("output_parse_failed", ("output_parse_failed", "parsing failed")),
    (
        "tool_schema_failed",
        (
            "tool_schema_failed",
            "tool_use_failed",
            "failed to parse tool call",
            "tool call validation failed",
            "tool validation failed",
            "does not match tool schema",
        ),
    ),
    (
        "malformed_tool_arguments",
        (
            "malformed_tool_arguments",
            "malformed tool arguments",
            "invalid json arguments",
            "invalid tool arguments json",
            "unterminated string starting at",
            "expecting property name enclosed in double quotes",
        ),
    ),
    (
        "context_limit",
        (
            "context_length_exceeded",
            "maximum context length",
            "context window exceeded",
            "too many tokens",
            "prompt is too long",
        ),
    ),
    (
        "rate_limit",
        (
            "rate_limit_exceeded",
            "rate limit exceeded",
            "too many requests",
            "429 too many requests",
            "429 rate limit",
            "http 429",
            "error code: 429",
            "status code 429",
        ),
    ),
    (
        "timeout",
        (
            "timeout",
            "timed out",
            "readtimeout",
            "connecttimeout",
            "deadline exceeded",
            "request took too long",
        ),
    ),
    (
        "authorization_failed",
        (
            "authentication_error",
            "invalid api key",
            "unauthorized",
            "error code: 401",
            "status code 401",
        ),
    ),
    (
        "network_block",
        (
            "network_block",
            "network access denied",
            "access denied",
            "temporarily blocked",
            "public ip",
            "403 forbidden",
            "403 blocked",
            "error code: 403",
            "status code 403",
        ),
    ),
    (
        "provider_unavailable",
        (
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "provider unavailable",
            "overloaded",
            "error code: 502",
            "error code: 503",
            "status code 502",
            "status code 503",
        ),
    ),
)

_PHASE_TOOL_ORDER: dict[str, tuple[str, ...]] = {
    "research": ("web_search", "fetch_page", "geocode_place"),
    "evidence": ("fetch_page", "web_search"),
    "locate": ("geocode_place", "fetch_page", "web_search"),
    "write": ("propose_place", "upsert_place_insights"),
    "enrich": ("upsert_place_insights", "fetch_page", "web_search"),
}

# These are tool-result errors, not provider failures.  They express an unmet
# precondition, so the next retry should expose only tools that can satisfy it.
_ERROR_TOOL_ROUTES: dict[str, tuple[str, ...]] = {
    "fact_source_not_fetched": ("fetch_page",),
    "unsupported_source_urls": ("web_search", "fetch_page"),
    "business_evidence_required": ("web_search", "fetch_page"),
    "evidence_required": ("web_search", "fetch_page"),
    "proposal_source_not_validated": ("fetch_page", "web_search"),
    "description_source_not_validated": ("fetch_page", "web_search"),
    "description_source_place_mismatch": ("web_search", "fetch_page"),
    "insights_required": ("fetch_page", "web_search"),
    "coordinate_target_not_verified": ("geocode_place", "fetch_page", "web_search"),
    "coordinate_not_grounded": ("geocode_place", "fetch_page", "web_search"),
    "place_integrity_failed": ("geocode_place", "fetch_page", "web_search"),
    "share_coordinate_not_resolved": ("geocode_place", "web_search"),
    "candidate_target_changed": ("propose_place",),
    "snack_scope_not_met": ("web_search", "fetch_page", "geocode_place"),
}

_MODES: tuple[RecoveryMode, ...] = (
    "focused_retry",
    "compact_retry",
    "minimal_retry",
)


@dataclass(frozen=True)
class RecoveryPlan:
    """A provider-agnostic retry decision; callers persist its outcome."""

    failure_kind: FailureKind
    attempt: int
    mode: RecoveryMode
    reasoning_effort: str
    force_compaction: bool
    recent_round_limit: int
    max_context_chars: int
    allowed_tools: frozenset[str]


def _failure_text(value: Any) -> str:
    """Flatten common exception/API payload shapes without provider imports."""

    if value is None:
        return ""
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}".casefold()
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("code", "type", "error", "message", "detail", "failed_generation", "status"):
            if key in value:
                parts.append(_failure_text(value[key]))
        return " ".join(part for part in parts if part).casefold()
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(_failure_text(item) for item in value).casefold()
    return str(value).casefold()


def classify_failure(error: Any) -> FailureKind:
    """Return a stable failure kind for an exception, string, or API payload."""

    text = " ".join(_failure_text(error).split())
    for kind, markers in _FAILURE_MARKERS:
        if any(marker in text for marker in markers):
            return kind
    return "unknown"


def recovery_mode(attempt: int) -> RecoveryMode:
    """Escalate attempts 1, 2, and 3+ from focused to minimal recovery."""

    if attempt < 1:
        raise ValueError("recovery attempt must be at least 1")
    return _MODES[min(attempt - 1, len(_MODES) - 1)]


def _intersect_available(
    ordered_tools: Iterable[str],
    available_tools: Iterable[str] | None,
) -> tuple[str, ...]:
    ordered = tuple(dict.fromkeys(str(tool) for tool in ordered_tools if str(tool)))
    if available_tools is None:
        return ordered
    available = {str(tool) for tool in available_tools if str(tool)}
    return tuple(tool for tool in ordered if tool in available)


def allowed_tools_after_failure(
    failure: str,
    *,
    attempt: int = 1,
    phase: str = "research",
    available_tools: Iterable[str] | None = None,
    last_tool: str = "",
    next_tool: str = "",
) -> frozenset[str]:
    """Choose the smallest useful tool surface after a failed action.

    ``failure`` may be a classified provider failure or an application tool
    error such as ``fact_source_not_fetched``.  ``available_tools`` is a hard
    capability boundary: this helper never grants a tool the caller withheld.
    """

    mode = recovery_mode(attempt)
    failure_key = str(failure or "unknown").strip().casefold()
    phase_order = _PHASE_TOOL_ORDER.get(phase, _PHASE_TOOL_ORDER["research"])

    routed = _ERROR_TOOL_ROUTES.get(failure_key)
    if routed is not None:
        selected = _intersect_available(routed, available_tools)
    elif failure_key in MODEL_OUTPUT_FAILURES:
        if mode == "focused_retry" and (next_tool or last_tool):
            selected = _intersect_available((next_tool or last_tool,), available_tools)
        else:
            selected = _intersect_available(phase_order, available_tools)
    else:
        # Timeouts, throttling, provider outages and unknown failures do not
        # imply that a different application tool is semantically correct.
        if available_tools is None:
            selected = phase_order
        else:
            available = {str(tool) for tool in available_tools if str(tool)}
            stable_order = (
                *phase_order,
                *sorted(tool for tool in available if tool not in phase_order),
            )
            selected = _intersect_available(stable_order, available)

    if failure_key in {"duplicate_tool_call", "recent_duplicate_search"} and last_tool:
        selected = tuple(tool for tool in selected if tool != last_tool)

    if mode == "minimal_retry" and selected:
        preferred = next_tool if next_tool in selected else selected[0]
        selected = (preferred,)

    return frozenset(selected)


def make_recovery_plan(
    error: Any,
    *,
    attempt: int,
    phase: str = "research",
    available_tools: Iterable[str] | None = None,
    last_tool: str = "",
    next_tool: str = "",
) -> RecoveryPlan:
    """Compose failure classification, retry mode, and tool restriction."""

    kind = classify_failure(error)
    mode = recovery_mode(attempt)
    if mode == "focused_retry":
        reasoning_effort, force_compaction, recent_round_limit, max_chars = (
            "medium", False, 4, 60_000,
        )
    elif mode == "compact_retry":
        reasoning_effort, force_compaction, recent_round_limit, max_chars = (
            "low", True, 3, 42_000,
        )
    else:
        reasoning_effort, force_compaction, recent_round_limit, max_chars = (
            "low", True, 2, 28_000,
        )
    return RecoveryPlan(
        failure_kind=kind,
        attempt=attempt,
        mode=mode,
        reasoning_effort=reasoning_effort,
        force_compaction=force_compaction,
        recent_round_limit=recent_round_limit,
        max_context_chars=max_chars,
        allowed_tools=allowed_tools_after_failure(
            kind,
            attempt=attempt,
            phase=phase,
            available_tools=available_tools,
            last_tool=last_tool,
            next_tool=next_tool,
        ),
    )
