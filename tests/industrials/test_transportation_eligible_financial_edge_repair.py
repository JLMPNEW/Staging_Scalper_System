from __future__ import annotations

import json
from pathlib import Path

import pytest

from industrials.transportation.eligible_financial_edge_repair import (
    apply_metric_repairs,
    artifact_sha256,
    extract_chrw_interest_ttm_usd,
    extract_expd_current_debt_usd,
)


def _filing(tmp_path: Path, name: str, body: str) -> tuple[Path, str]:
    path = tmp_path / name
    path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
    return path, artifact_sha256(path)


def test_source_backed_edge_operands_parse_and_roll_forward(tmp_path: Path) -> None:
    annual, annual_hash = _filing(
        tmp_path,
        "chrw-annual.htm",
        "primarily consisted of $63.1 million of interest expense",
    )
    quarter, quarter_hash = _filing(
        tmp_path,
        "chrw-quarter.htm",
        (
            "consisting primarily of $14.0 million of interest expense, "
            "which decreased $2.8 million versus last year"
        ),
    )
    result = extract_chrw_interest_ttm_usd(
        annual_path=annual,
        annual_sha256=annual_hash,
        quarter_path=quarter,
        quarter_sha256=quarter_hash,
    )
    assert result["prior_quarter_interest_expense_usd"] == 16_800_000.0
    assert result["interest_expense_ttm_usd"] == pytest.approx(60_300_000.0)

    expd, expd_hash = _filing(
        tmp_path,
        "expd-quarter.htm",
        "At March 31, 2026, borrowings under these credit lines were $33 million",
    )
    assert extract_expd_current_debt_usd(
        quarter_path=expd,
        quarter_sha256=expd_hash,
    ) == 33_000_000.0


def test_pinned_filing_hash_and_existing_observations_fail_closed(tmp_path: Path) -> None:
    filing, filing_hash = _filing(
        tmp_path,
        "expd-quarter.htm",
        "At March 31, 2026, borrowings under these credit lines were $33 million",
    )
    with pytest.raises(ValueError, match="immutable filing hash mismatch"):
        extract_expd_current_debt_usd(
            quarter_path=filing,
            quarter_sha256="0" * 64,
        )

    rows = [
        {
            "ticker": "EXPD",
            "metric_values_json": json.dumps({"net_debt_to_ebitda": 1.25}),
            "metric_status_json": json.dumps({"net_debt_to_ebitda": "REPORTED"}),
        },
        {
            "ticker": "WERN",
            "metric_values_json": "{}",
            "metric_status_json": json.dumps({"net_debt_to_ebitda": "NOT_DISCLOSED"}),
        },
    ]
    repaired = apply_metric_repairs(
        rows,
        {
            ("EXPD", "net_debt_to_ebitda"): -1.1,
            ("WERN", "net_debt_to_ebitda"): 2.6,
        },
    )
    assert json.loads(repaired[0]["metric_values_json"])["net_debt_to_ebitda"] == 1.25
    assert json.loads(repaired[1]["metric_values_json"])["net_debt_to_ebitda"] == 2.6
    assert json.loads(repaired[1]["metric_status_json"])["net_debt_to_ebitda"] == "DERIVED"
