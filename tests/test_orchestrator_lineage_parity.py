from __future__ import annotations

import csv
import runpy
from pathlib import Path

from industrials.core.financial_filing_lineage import (
    validate_financial_lineage_rank_rows,
)
from orchestration_contracts.financial_lineage import (
    LINEAGE_FIELDS,
    POLICY_STRICT_UNIVERSE,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASOF = "2026-08-13"


def _unresolved_row() -> dict[str, str]:
    return {
        "ticker": "TEST",
        "portfolio_candidate_gate": "0",
        "financial_lineage_checked_asof_date": ASOF,
        "financial_lineage_status": "REVIEW_REQUIRED",
        "financial_lineage_gate": "0",
        "financial_lineage_classification": "CANONICALIZATION_GAP",
        "latest_material_financial_filing_date": "2026-08-12",
        "latest_material_financial_form": "10-Q",
        "latest_material_financial_accession": "unresolved",
        "latest_material_financial_report_date": "2026-06-30",
        "incorporated_financial_filing_date": "",
        "incorporated_financial_accession": "",
        "incorporated_financial_report_date": "",
        "incorporated_financial_core_metric_count": "0",
        "financial_lineage_reason": "test",
    }


def test_global_registry_derives_lineage_policy_from_central_registry() -> None:
    namespace = runpy.run_path(str(PROJECT_ROOT / "orchestration" / "run_all.py"))
    registry = namespace["load_registry"](
        PROJECT_ROOT / "orchestration" / "registry.yaml"
    )

    defense = registry.by_name("defense")
    machinery = registry.by_name("machinery")
    transportation = registry.by_name("transportation")
    assert defense.financial_lineage_policy == POLICY_STRICT_UNIVERSE
    assert machinery.financial_lineage_policy == POLICY_STRICT_UNIVERSE
    assert defense.financial_lineage_required is True
    assert machinery.financial_lineage_required is True
    assert transportation.financial_lineage_required is False


def test_local_and_global_strict_validators_return_identical_reasons(
    tmp_path: Path,
) -> None:
    row = _unresolved_row()
    path = tmp_path / "rank.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["ticker", "portfolio_candidate_gate", *LINEAGE_FIELDS],
        )
        writer.writeheader()
        writer.writerow(row)

    local_errors = validate_financial_lineage_rank_rows(
        [row],
        expected_asof=ASOF,
        policy_mode=POLICY_STRICT_UNIVERSE,
    )
    namespace = runpy.run_path(str(PROJECT_ROOT / "orchestration" / "run_all.py"))
    global_errors = namespace["_financial_lineage_errors"](
        path,
        ASOF,
        policy_mode=POLICY_STRICT_UNIVERSE,
    )

    assert local_errors == global_errors
    assert any("material_financial_filing_unresolved" in error for error in local_errors)


def test_local_orchestrators_run_shared_recovery_before_financial_build() -> None:
    defense_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "defense" / "scripts" / "16_run_defense_daily_refresh.py")
    )
    machinery_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "17_run_machinery_refresh_pipeline.py")
    )
    defense_steps = defense_namespace["build_steps"](
        ASOF,
        "2018-01-01",
        positioning_through_publish_only=False,
    )
    machinery_steps = machinery_namespace["build_steps"](
        ASOF,
        force=False,
        include_norgate_backfill=False,
    )
    defense_ids = [step.step_id for step in defense_steps]
    machinery_ids = [step.step_id for step in machinery_steps]

    assert defense_ids.index("07_sync_sec") < defense_ids.index("07c_recover_financial_lineage")
    assert defense_ids.index("07c_recover_financial_lineage") < defense_ids.index("08_build_financial")
    assert machinery_ids.index("07_sync_sec") < machinery_ids.index("07c_recover_financial_lineage")
    assert machinery_ids.index("07c_recover_financial_lineage") < machinery_ids.index("08_build_financial")
    assert (
        PROJECT_ROOT / "industrials" / "scripts" / "07c_recover_industrials_financial_lineage.py"
    ).is_file()
