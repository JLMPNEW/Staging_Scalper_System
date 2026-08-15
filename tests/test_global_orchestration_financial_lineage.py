from __future__ import annotations

import csv
import runpy
from pathlib import Path

from orchestration_contracts.financial_lineage import LINEAGE_FIELDS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, *, candidate: str, gate: str, status: str) -> None:
    fields = ["ticker", "portfolio_candidate_gate", *LINEAGE_FIELDS]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "CAE",
                "portfolio_candidate_gate": candidate,
                "financial_lineage_checked_asof_date": "2026-08-13",
                "financial_lineage_status": status,
                "financial_lineage_gate": gate,
                "financial_lineage_classification": (
                    "INCORPORATED" if gate == "1" else "CANONICALIZATION_GAP"
                ),
                "latest_material_financial_filing_date": "2026-08-12",
                "latest_material_financial_form": "6-K",
                "latest_material_financial_accession": "cover",
                "latest_material_financial_report_date": "2026-06-30",
                "incorporated_financial_filing_date": "2026-08-12",
                "incorporated_financial_accession": "data",
                "incorporated_financial_report_date": "2026-06-30",
                "incorporated_financial_core_metric_count": "3",
                "financial_lineage_reason": "test",
            }
        )


def test_global_lineage_verifier_requires_complete_reconciliation(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(PROJECT_ROOT / "orchestration" / "run_all.py"))
    verify = namespace["_financial_lineage_errors"]
    path = tmp_path / "rank.csv"

    _write(path, candidate="1", gate="0", status="REVIEW_REQUIRED")
    assert any("candidate_has_unresolved" in error for error in verify(path, "2026-08-13"))

    _write(path, candidate="0", gate="0", status="REVIEW_REQUIRED")
    assert any(
        "material_financial_filing_unresolved" in error for error in verify(path, "2026-08-13")
    )

    _write(path, candidate="1", gate="1", status="INCORPORATED")
    assert verify(path, "2026-08-13") == []


def test_global_lineage_sidecar_must_match_published_rank_contract(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(PROJECT_ROOT / "orchestration" / "run_all.py"))
    verify = namespace["_financial_lineage_sidecar_errors"]
    rank_path = tmp_path / "rank.csv"
    lineage_path = tmp_path / "lineage.csv"

    _write(rank_path, candidate="1", gate="1", status="INCORPORATED")
    _write(lineage_path, candidate="0", gate="1", status="INCORPORATED")

    errors = verify(rank_path, lineage_path, "2026-08-13")

    assert any("portfolio_candidate_gate" in error for error in errors)
