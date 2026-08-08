"""Fail-closed Benjamini-Hochberg families for shared factor evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FDRFamily:
    """Pre-registered family whose identity, membership, and alpha are sealed together."""

    family_id: str
    member_ids: tuple[str, ...]
    alpha: float

    def __post_init__(self) -> None:
        family_id = str(self.family_id or "").strip()
        members = tuple(str(member or "").strip() for member in self.member_ids)
        if not family_id:
            raise ValueError("family_id must not be blank")
        if not members or any(not member for member in members):
            raise ValueError("member_ids must contain non-blank members")
        if len(set(members)) != len(members):
            raise ValueError("member_ids must be unique")
        alpha = float(self.alpha)
        if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
            raise ValueError("alpha must be finite and strictly between 0 and 1")
        object.__setattr__(self, "family_id", family_id)
        object.__setattr__(self, "member_ids", members)
        object.__setattr__(self, "alpha", alpha)

    @property
    def registration_sha256(self) -> str:
        canonical = json.dumps(
            {
                "alpha": format(self.alpha, ".17g"),
                "family_id": self.family_id,
                "member_ids": sorted(self.member_ids),
                "schema_version": "factor_validation_fdr_family_v1",
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def membership_sha256(self) -> str:
        """Backward-compatible name for the full registration seal."""

        return self.registration_sha256


@dataclass(frozen=True)
class FDRDecision:
    family_id: str
    family_registration_sha256: str
    alpha: float
    member_id: str
    p_value: float | None
    q_value: float
    accepted: bool
    testable: bool

    @property
    def family_membership_sha256(self) -> str:
        """Backward-compatible name for the full registration seal."""

        return self.family_registration_sha256


def _validated_p_value(value: float | None, *, member_id: str) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"p-value for {member_id!r} must be finite and in [0, 1]")
    return parsed


def apply_benjamini_hochberg(
    family: FDRFamily,
    p_values: Mapping[str, float | None],
) -> tuple[FDRDecision, ...]:
    """Apply BH to an exact pre-registered family and return adjusted q-values.

    Untestable members remain in the multiplicity denominator with an effective p-value of 1.0.
    Missing or extra member keys are rejected so a rerun cannot silently shrink or grow the family.
    """

    expected = set(family.member_ids)
    supplied = set(p_values)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        raise ValueError(f"FDR family membership mismatch: missing={missing}; extra={extra}")

    validated = {
        member_id: _validated_p_value(p_values[member_id], member_id=member_id)
        for member_id in family.member_ids
    }
    effective = {member_id: 1.0 if value is None else value for member_id, value in validated.items()}
    ordered = sorted(effective.items(), key=lambda item: (item[1], item[0]))
    family_size = len(ordered)
    adjusted: dict[str, float] = {}
    running_minimum = 1.0
    for rank_zero_based in range(family_size - 1, -1, -1):
        member_id, p_value = ordered[rank_zero_based]
        rank = rank_zero_based + 1
        running_minimum = min(running_minimum, p_value * family_size / rank)
        adjusted[member_id] = min(1.0, running_minimum)

    digest = family.registration_sha256
    return tuple(
        FDRDecision(
            family_id=family.family_id,
            family_registration_sha256=digest,
            alpha=family.alpha,
            member_id=member_id,
            p_value=validated[member_id],
            q_value=adjusted[member_id],
            accepted=validated[member_id] is not None and adjusted[member_id] <= family.alpha,
            testable=validated[member_id] is not None,
        )
        for member_id in family.member_ids
    )
