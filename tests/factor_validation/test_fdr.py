from __future__ import annotations

import pytest

from factor_validation import FDRFamily, apply_benjamini_hochberg


def test_fdr_family_is_hash_sealed_and_order_independent() -> None:
    first = FDRFamily("core_factors_21d", ("quality", "value", "momentum"), 0.10)
    second = FDRFamily("core_factors_21d", ("momentum", "quality", "value"), 0.10)
    different_alpha = FDRFamily("core_factors_21d", ("quality", "value", "momentum"), 0.05)
    different_id = FDRFamily("other_family", ("quality", "value", "momentum"), 0.10)

    assert first.registration_sha256 == second.registration_sha256
    assert first.registration_sha256 != different_alpha.registration_sha256
    assert first.registration_sha256 != different_id.registration_sha256
    assert len(first.registration_sha256) == 64


def test_bh_returns_q_values_and_keeps_untestable_members_in_family() -> None:
    family = FDRFamily("family", ("strong", "weak", "untestable", "noise"), 0.05)
    decisions = apply_benjamini_hochberg(
        family,
        {"strong": 0.001, "weak": 0.03, "untestable": None, "noise": 0.8},
    )
    by_member = {decision.member_id: decision for decision in decisions}

    assert by_member["strong"].accepted is True
    assert by_member["strong"].alpha == pytest.approx(0.05)
    assert by_member["strong"].q_value == pytest.approx(0.004)
    assert by_member["weak"].accepted is False
    assert by_member["untestable"].testable is False
    assert by_member["untestable"].q_value == pytest.approx(1.0)
    assert all(decision.family_membership_sha256 == family.membership_sha256 for decision in decisions)


def test_bh_matches_standard_five_hypothesis_worked_example() -> None:
    family = FDRFamily("worked_example", ("p1", "p2", "p3", "p4", "p5"), 0.05)
    decisions = apply_benjamini_hochberg(
        family,
        {"p1": 0.01, "p2": 0.04, "p3": 0.03, "p4": 0.002, "p5": 0.05},
    )
    q_values = {decision.member_id: decision.q_value for decision in decisions}

    assert q_values == pytest.approx(
        {"p1": 0.025, "p2": 0.05, "p3": 0.05, "p4": 0.01, "p5": 0.05}
    )
    assert all(decision.accepted for decision in decisions)


def test_bh_fails_closed_on_family_drift_or_invalid_p_values() -> None:
    family = FDRFamily("family", ("a", "b"), 0.10)
    with pytest.raises(ValueError, match="membership mismatch"):
        apply_benjamini_hochberg(family, {"a": 0.01})
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        apply_benjamini_hochberg(family, {"a": -0.01, "b": 0.5})


def test_registration_digest_changes_with_membership() -> None:
    base = FDRFamily("family", ("a", "b"), 0.10)
    assert base.registration_sha256 != FDRFamily("family", ("a", "c"), 0.10).registration_sha256
    assert base.registration_sha256 != FDRFamily("family", ("a", "b", "c"), 0.10).registration_sha256
    assert base.registration_sha256 == FDRFamily("family", ("b", "a"), 0.10).registration_sha256
