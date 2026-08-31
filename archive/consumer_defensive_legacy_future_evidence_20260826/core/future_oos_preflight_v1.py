"""Operational preflight enrichment for Consumer future-only evidence."""

from __future__ import annotations

from typing import Any

from future_only_evidence.protocol import canonical_sha256

from .future_oos_protocol_v1 import build_preflight


def build_operational_preflight(**kwargs: Any) -> dict[str, Any]:
    payload = build_preflight(**kwargs)
    payload.pop("payload_sha256", None)
    payload.update(
        valid_future_capture_count=0,
        qualified_outcome_count={21: 0, 63: 0, 126: 0},
        remaining_outcome_count={21: 12, 63: 6, 126: 4},
        earliest_session_math={
            "basis": "true_nonoverlap_session_lower_bound_after_first_captured_entry",
            "21": "12 * 21 = 252 market sessions",
            "63": "6 * 63 = 378 market sessions",
            "126": "4 * 126 = 504 market sessions",
            "calendar_date_guarantee": False,
        },
    )
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


__all__ = ["build_operational_preflight"]
