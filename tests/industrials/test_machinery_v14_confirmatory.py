from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from industrials.core.cross_family_validation import (
    compare_replication_contracts,
    sha256_file,
)
from industrials.machinery.confirmatory_v14 import (
    COMPONENT_FIELDS,
    DEFAULT_PROTOCOL_PATH,
    FORBIDDEN_SIGNAL_FIELD_TOKENS,
    SIGNAL_FIELDS,
    assess_defense_compatibility,
    capture_forward_signals,
    confirmatory_paths,
    load_protocol_definition,
)


def _write_csv(
    path: Path,
    fields: list[str] | tuple[str, ...],
    rows: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _install_freeze(output_root: Path) -> None:
    path = confirmatory_paths(output_root).freeze_manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "protocol_definition_sha256": sha256_file(
                    DEFAULT_PROTOCOL_PATH
                ),
            }
        ),
        encoding="utf-8",
    )


def _rank_rows(asof: str, count: int = 35) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index in range(count):
        row = {
            "asof_date": asof,
            "ticker": f"T{index:02d}",
            "company_name": f"Company {index}",
            "calibration_cohort": "heavy_machinery",
            "development_stage": "operating",
            "membership_start_date": "2019-01-02",
            "membership_end_date": "",
            "rank_ready_flag": "1",
            "model_status": "complete",
        }
        row.update(
            {
                field: str(40.0 + index + component_index)
                for component_index, field in enumerate(COMPONENT_FIELDS)
            }
        )
        rows.append(row)
    return rows


def test_v14_protocol_is_one_fixed_disclosed_specification() -> None:
    protocol = load_protocol_definition()
    assert protocol["candidate_id"] == "equal_components"
    assert protocol["optimizer_enabled"] is False
    assert protocol["specification_count"] == 1
    assert "not_independent_confirmation" in protocol["candidate_origin"]
    assert sum(protocol["weights"].values()) == pytest.approx(1.0)


def test_cross_family_contract_does_not_promote_on_semantic_mapping() -> None:
    result = compare_replication_contracts(
        target_components=("quality_score", "capex_cycle_score"),
        source_components=("quality_score",),
        semantic_mapping={
            "quality_score": "quality_score",
            "capex_cycle_score": None,
        },
        target_horizons=(21, 63),
        source_horizons=(63,),
        target_return_basis="next_session_open_execution_excess",
        source_return_basis="adjusted_close_to_adjusted_close",
        target_cost_bps=20.0,
        source_cost_bps=None,
        target_benchmark="XLI",
        source_benchmark="XAR",
    )
    assert result["direct_replication_ready"] is False
    assert result["machinery_acceptance_eligible"] is False
    assert "capex_cycle_score" in result["unmapped_target_components"]


def test_forward_capture_writes_signals_without_outcomes(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "v14"
    _install_freeze(output_root)
    rank_table = tmp_path / "machinery_final_rank_table.csv"
    rows = _rank_rows("2026-07-30")
    fields = tuple(rows[0])
    _write_csv(rank_table, fields, rows)

    result = capture_forward_signals(
        asof="2026-07-30",
        rank_table=rank_table,
        output_root=output_root,
    )

    assert result["acceptance"] == "PASS"
    assert result["row_count"] == 35
    assert result["selected_count"] == 10
    assert not [
        field
        for field in SIGNAL_FIELDS
        if any(
            token in field.lower() for token in FORBIDDEN_SIGNAL_FIELD_TOKENS
        )
    ]


def test_forward_capture_refuses_pre_freeze_signal_date(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "v14"
    _install_freeze(output_root)
    rank_table = tmp_path / "machinery_final_rank_table.csv"
    rows = _rank_rows("2026-07-29")
    _write_csv(rank_table, tuple(rows[0]), rows)

    with pytest.raises(ValueError, match="precedes frozen start"):
        capture_forward_signals(
            asof="2026-07-29",
            rank_table=rank_table,
            output_root=output_root,
        )


def test_forward_capture_is_idempotent_for_identical_source(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "v14"
    _install_freeze(output_root)
    rank_table = tmp_path / "machinery_final_rank_table.csv"
    rows = _rank_rows("2026-07-30")
    _write_csv(rank_table, tuple(rows[0]), rows)

    first = capture_forward_signals(
        asof="2026-07-30",
        rank_table=rank_table,
        output_root=output_root,
    )
    second = capture_forward_signals(
        asof="2026-07-30",
        rank_table=rank_table,
        output_root=output_root,
    )

    assert first == second
    assert second["acceptance"] == "PASS"


def test_defense_compatibility_is_read_only_and_non_promotional(
    tmp_path: Path,
) -> None:
    panel = tmp_path / "defense" / "defense_oos_calibration_panel.csv"
    fields = (
        "ticker",
        "forward_days",
        "price_basis",
        "benchmark_ticker",
        "valuation_score",
        "quality_score",
        "risk_control_score",
        "positioning_score",
        "market_behavior_score",
        "growth_score",
        "sector_cycle_score",
        "defense_budget_backlog_score",
    )
    _write_csv(
        panel,
        fields,
        [
            {
                "ticker": "DEF",
                "forward_days": "63",
                "price_basis": "adj_close",
                "benchmark_ticker": "XAR",
                **{field: "50" for field in fields if field.endswith("_score")},
            }
        ],
    )
    manifest = panel.parent / "defense_oos_calibration_panel_manifest.json"
    manifest.write_text(
        json.dumps({"snapshot_count": 100}),
        encoding="utf-8",
    )
    before = (sha256_file(panel), sha256_file(manifest))

    result = assess_defense_compatibility(
        defense_panel=panel,
        output_root=tmp_path / "v14",
    )

    after = (sha256_file(panel), sha256_file(manifest))
    assert before == after
    assert result["acceptance"] == "PASS"
    assert result["defense_artifacts_modified"] is False
    assert result["direct_replication_ready"] is False
    assert result["machinery_acceptance_eligible"] is False
