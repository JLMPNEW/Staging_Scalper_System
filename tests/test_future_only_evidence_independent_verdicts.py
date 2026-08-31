from future_only_evidence.independent_verdicts import add_independent_sleeve_actions


def test_failed_sleeve_does_not_block_passing_sleeve_submission() -> None:
    result = add_independent_sleeve_actions(
        {
            "sleeve_verdicts": [
                {"sleeve_id": "surface", "pass": True},
                {"sleeve_id": "tankers", "pass": False},
            ],
            "payload_sha256": "x",
        }
    )
    assert result["passing_sleeves"] == ["surface"]
    assert result["blocked_sleeves"] == ["tankers"]
    assert result["any_sleeve_pass"] is True
    assert result["sector_wide_all_sleeves_pass"] is False
    assert result["action"] == "submit_passing_sleeves_for_independent_review"
    assert result["production_activation_authorized"] is False
