from __future__ import annotations

import csv
import json
import runpy
from dataclasses import replace
from pathlib import Path

from orchestration_contracts.financial_lineage import LINEAGE_FIELDS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write(
    path: Path,
    *,
    candidate: str,
    gate: str,
    status: str,
    asof: str = "2026-08-13",
) -> None:
    fields = ["ticker", "portfolio_candidate_gate", *LINEAGE_FIELDS]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "CAE",
                "portfolio_candidate_gate": candidate,
                "financial_lineage_checked_asof_date": asof,
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


def test_historical_artifact_verification_uses_historical_lineage_policy(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(PROJECT_ROOT / "orchestration" / "run_all.py"))
    verify = namespace["verify_published_artifact_for_date"]
    clear_resolved = namespace["clear_resolved_auto_gap_records"]
    load_registry = namespace["load_registry"]
    health_spec = namespace["HealthSpec"]
    verify_globals = verify.__globals__
    prior_root = verify_globals["PROJECT_ROOT"]
    asof = "2026-08-26"

    rank_path = tmp_path / "published" / asof / "rank.csv"
    lineage_path = tmp_path / "lineage" / asof / "lineage.csv"
    rank_path.parent.mkdir(parents=True)
    lineage_path.parent.mkdir(parents=True)
    _write(
        rank_path,
        candidate="1",
        gate="0",
        status="REVIEW_REQUIRED",
        asof=asof,
    )
    _write(
        lineage_path,
        candidate="1",
        gate="0",
        status="REVIEW_REQUIRED",
        asof=asof,
    )
    operator_date = "2026-08-27"
    operator_rank_path = tmp_path / "published" / operator_date / "rank.csv"
    operator_lineage_path = tmp_path / "lineage" / operator_date / "lineage.csv"
    operator_rank_path.parent.mkdir(parents=True)
    operator_lineage_path.parent.mkdir(parents=True)
    _write(
        operator_rank_path,
        candidate="1",
        gate="0",
        status="REVIEW_REQUIRED",
        asof=operator_date,
    )
    _write(
        operator_lineage_path,
        candidate="1",
        gate="0",
        status="REVIEW_REQUIRED",
        asof=operator_date,
    )

    registry = load_registry(PROJECT_ROOT / "orchestration" / "registry.yaml")
    med_devices = replace(
        registry.by_name("med_devices"),
        publish_glob="published/{date}/rank.csv",
        require_oos_valid=False,
        health=health_spec(manifest=None, status_keys=[]),
        financial_lineage_artifact="lineage/{date}/lineage.csv",
    )

    try:
        verify_globals["PROJECT_ROOT"] = tmp_path
        production_ok, production_reasons = verify(
            med_devices,
            asof,
            verify_manifest=False,
            policy_context="production",
        )
        historical_ok, historical_reasons = verify(
            med_devices,
            asof,
            verify_manifest=False,
            policy_context="historical",
        )
        marker_path = tmp_path / "markers.json"
        marker_path.write_text(
            json.dumps(
                {
                    "sectors": {
                        "med_devices": {
                            asof: {"source": "auto", "permanent": True},
                            operator_date: {
                                "source": "operator",
                                "permanent": True,
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        resolved = clear_resolved(
            med_devices,
            operator_date,
            markers_path=marker_path,
        )
        remaining_markers = json.loads(marker_path.read_text(encoding="utf-8"))
    finally:
        verify_globals["PROJECT_ROOT"] = prior_root

    assert not production_ok
    assert any("candidate_has_unresolved" in reason for reason in production_reasons)
    assert historical_ok
    assert historical_reasons == []
    assert resolved == [asof]
    assert asof not in remaining_markers["sectors"]["med_devices"]
    assert operator_date in remaining_markers["sectors"]["med_devices"]
