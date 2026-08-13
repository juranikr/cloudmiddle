"""Versioned server attestations for coordinates carried across chat turns.

A candidate saved in chat history is not automatically trusted just because it
contains latitude and longitude. Only the server can issue this HMAC record,
and every later consumer verifies both the signature and the exact coordinates
before treating the candidate as coordinate evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

from app.config import settings


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonical_evidence(evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    lat, lng = _float(evidence.get("lat")), _float(evidence.get("lng"))
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return {
        "title": str(evidence.get("title") or evidence.get("display_name") or "")[:300],
        "display_name": str(evidence.get("display_name") or evidence.get("title") or "")[:300],
        "branch_name": str(evidence.get("branch_name") or "")[:160],
        "address": str(evidence.get("address") or "")[:500],
        "lat": round(lat, 7),
        "lng": round(lng, 7),
        "source": str(evidence.get("source") or "agent_research")[:80],
        "source_url": str(evidence.get("source_url") or "")[:1000],
        "external_id": str(evidence.get("external_id") or "")[:300],
        "confidence": round(max(0.0, min(_float(evidence.get("confidence")) or 0.5, 1.0)), 4),
        "storage_allowed": evidence.get("storage_allowed") is not False,
    }


def _digest(evidence: Mapping[str, Any], *, secret: str) -> str:
    canonical = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def issue_coordinate_attestation(
    candidate: Mapping[str, Any],
    evidence: Mapping[str, Any] | None = None,
    *,
    secret: str | None = None,
) -> dict[str, Any]:
    """Return a copy with a signed, canonical coordinate observation."""

    output = dict(candidate)
    source = evidence or {
        "title": candidate.get("title"),
        "display_name": candidate.get("title"),
        "branch_name": candidate.get("branch_name"),
        "address": candidate.get("address"),
        "lat": candidate.get("lat"),
        "lng": candidate.get("lng"),
        "source": candidate.get("coordinate_source"),
        "source_url": candidate.get("coordinate_source_url"),
        "external_id": candidate.get("coordinate_external_id"),
        "confidence": candidate.get("coordinate_confidence") or candidate.get("confidence"),
        "storage_allowed": True,
    }
    canonical = _canonical_evidence(source)
    if canonical is None or canonical["storage_allowed"] is False:
        output.pop("coordinate_attestation", None)
        return output
    signing_secret = secret if secret is not None else settings.jwt_secret
    output["coordinate_attestation"] = {
        "version": 1,
        "evidence": canonical,
        "digest": _digest(canonical, secret=signing_secret),
    }
    return output


def trusted_coordinate_evidence(
    candidate: Mapping[str, Any],
    *,
    secret: str | None = None,
) -> dict[str, Any] | None:
    """Verify a carried candidate and return its immutable coordinate evidence."""

    attestation = candidate.get("coordinate_attestation")
    if not isinstance(attestation, Mapping) or attestation.get("version") != 1:
        return None
    raw_evidence = attestation.get("evidence")
    if not isinstance(raw_evidence, Mapping):
        return None
    canonical = _canonical_evidence(raw_evidence)
    if canonical is None or canonical["storage_allowed"] is False:
        return None
    signing_secret = secret if secret is not None else settings.jwt_secret
    if not hmac.compare_digest(
        str(attestation.get("digest") or ""),
        _digest(canonical, secret=signing_secret),
    ):
        return None
    candidate_lat, candidate_lng = _float(candidate.get("lat")), _float(candidate.get("lng"))
    if candidate_lat is None or candidate_lng is None:
        return None
    if abs(candidate_lat - canonical["lat"]) > 0.0000002 or abs(candidate_lng - canonical["lng"]) > 0.0000002:
        return None
    return canonical


def strip_untrusted_coordinate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Keep discovery metadata but remove any unsigned persisted location."""

    output = dict(candidate)
    if trusted_coordinate_evidence(output) is not None:
        return output
    for field in (
        "lat", "lng", "coordinate_source", "coordinate_source_url",
        "coordinate_external_id", "coordinate_query", "coordinate_confidence",
        "coordinate_attestation",
    ):
        output.pop(field, None)
    if str(output.get("status") or "") == "located":
        output["status"] = "location_needed"
    return output


__all__ = [
    "issue_coordinate_attestation",
    "strip_untrusted_coordinate",
    "trusted_coordinate_evidence",
]
