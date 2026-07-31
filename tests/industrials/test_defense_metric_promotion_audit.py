from __future__ import annotations

import csv
import json
from pathlib import Path

from industrials.defense.metric_contract import STRUCTURALLY_DISABLED_PILLARS
from industrials.defense.metric_promotion_audit import (
    EXPECTED_COLUMNS,
    candidate_calibration_identification_issues,
    classify_column,
    production_constant_pillars,
)


def test_every_known_metric_column_has_a_disposition() -> None:
    unclassified = [
        f"{table}.{column}"
        for table, columns in EXPECTED_COLUMNS.items()
        for column in columns
        if classify_column(table, column).disposition == "unclassified"
    ]

    assert unclassified == []


def test_specialized_and_unassigned_candidates_are_explicit() -> None:
    specialized = classify_column(
        "feature_financial_statement",
        "reported_backlog_yoy_growth",
    )
    unassigned = classify_column(
        "feature_financial_statement",
        "roic",
    )

    assert specialized.disposition == "specialized_candidate_input"
    assert specialized.consumer == "defense_budget_backlog"
    assert specialized.promotion_candidate is True
    assert unassigned.disposition == "shadow_candidate_input"
    assert unassigned.promotion_candidate is True


def test_unknown_schema_column_fails_closed() -> None:
    disposition = classify_column(
        "feature_financial_statement",
        "future_unreviewed_metric",
    )

    assert disposition.disposition == "unclassified"


def test_constant_nonzero_production_pillar_is_detected() -> None:
    rows = [
        {
            "valuation_score": "40",
            "sector_cycle_score": "50",
        },
        {
            "valuation_score": "60",
            "sector_cycle_score": "50",
        },
    ]
    manifest = {
        "promotion_payload": {
            "weights": {
                "valuation_score": 0.5,
                "sector_cycle_score": 0.5,
            }
        }
    }

    findings = production_constant_pillars(rows, manifest)

    assert findings == [("sector_cycle_score", 0.5, 1)]
    assert STRUCTURALLY_DISABLED_PILLARS == {"sector_cycle_score"}


def test_inventory_contract_can_be_written_as_csv(tmp_path: Path) -> None:
    path = tmp_path / "contract.csv"
    rows = [
        {
            "table": table,
            "column": column,
            "disposition": classify_column(table, column).disposition,
        }
        for table, columns in EXPECTED_COLUMNS.items()
        for column in columns
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["table", "column", "disposition"],
        )
        writer.writeheader()
        writer.writerows(rows)

    assert len(list(csv.DictReader(path.open(encoding="utf-8")))) == sum(
        len(columns) for columns in EXPECTED_COLUMNS.values()
    )


def test_candidate_calibration_requires_zero_weight_for_constant_pillars(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "summary.csv"
    manifest_path = tmp_path / "manifest.json"
    fields = [
        "valuation_score",
        "quality_score",
        "risk_control_score",
        "positioning_score",
        "market_behavior_score",
        "growth_score",
        "sector_cycle_score",
        "defense_budget_backlog_score",
    ]
    weights = {field: 0.0 for field in fields}
    weights["quality_score"] = 0.6
    weights["defense_budget_backlog_score"] = 0.4
    with summary_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["best_weights_json"])
        writer.writeheader()
        writer.writerow({"best_weights_json": json.dumps(weights)})
    manifest_path.write_text(
        json.dumps(
            {
                "inactive_pillars": [
                    "sector_cycle_score",
                    "valuation_score",
                ]
            }
        ),
        encoding="utf-8",
    )
    comparison = {
        "inputs": {
            "calibration_summary": {"candidate": str(summary_path)},
            "calibration_manifest": {"candidate": str(manifest_path)},
        }
    }
    panel_rows = [
        {
            "panel_row_eligible_flag": "1",
            "split_name": split_name,
            **{
                field: (
                    "50"
                    if field in {"valuation_score", "sector_cycle_score"}
                    else str(index + offset)
                )
                for offset, field in enumerate(fields)
            },
        }
        for index, split_name in enumerate(["train", "validation"], start=1)
    ]

    issues, evidence = candidate_calibration_identification_issues(
        panel_rows,
        comparison,
    )

    assert issues == []
    assert evidence["constant_pillars"] == [
        "valuation_score",
        "sector_cycle_score",
    ]

    weights["valuation_score"] = 0.1
    weights["quality_score"] = 0.5
    with summary_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["best_weights_json"])
        writer.writeheader()
        writer.writerow({"best_weights_json": json.dumps(weights)})

    issues, _ = candidate_calibration_identification_issues(
        panel_rows,
        comparison,
    )

    assert any("valuation_score has nonzero weight" in issue for issue in issues)
