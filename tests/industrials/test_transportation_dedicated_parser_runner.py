from __future__ import annotations

import runpy
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "industrials"
    / "transportation"
    / "scripts"
    / "08f_run_transportation_dedicated_parser_shadow.py"
)


def _script() -> dict[str, object]:
    return runpy.run_path(str(SCRIPT))


def test_exact_gap_hydration_command_is_cache_only_and_unlimited(
    tmp_path: Path,
) -> None:
    module = _script()
    gaps = tmp_path / "gaps.csv"
    command = module["build_hydration_command"](
        config_path=Path("industrials/config.yaml"),
        db_path=Path("industrials.sqlite"),
        asof_date="2026-07-22",
        gaps_path=gaps,
        output_csv=tmp_path / "sync.csv",
        tickers=["HAFN", "ECO"],
        workers=4,
    )

    assert command[command.index("--model-family") + 1] == "transportation"
    assert command[command.index("--tickers") + 1] == "ECO,HAFN"
    assert "--archive-cache-only" in command
    assert "--archive-selected" in command
    assert "--archive-scan-all-documents" in command
    assert command[command.index("--archive-max-filings-per-ticker") + 1] == "0"
    assert command[command.index("--archive-max-documents-per-filing") + 1] == "0"
    assert command[command.index("--archive-accession-scope-csv") + 1] == str(gaps)
    assert "--archive-document-keywords" in command
    assert "--skip-source-registry" in command
    assert "--force" not in command


def test_dp4_plan_gate_accepts_complete_all_metric_manifest_plan() -> None:
    module = _script()
    source_manifest = {
        "identity_count": 160,
        "selected_identity_count": 160,
        "selected_accession_count": 3_224,
        "selected_document_row_count": 3_329,
        "parser_metric_count": 84,
    }
    payload = {
        "mode": "plan_only",
        "summary": {
            "requested_tickers": 160,
            "scheduled_accessions": 3_224,
            "scheduled_documents": 3_329,
            "skipped_completed_accessions": 0,
            "missing_cache_accessions": 0,
            "execution_scope": {
                "all_metrics": True,
                "max_filings_per_ticker": 0,
                "max_documents_per_filing": 0,
                "enable_arelle": True,
                "enable_edgartools": True,
                "enable_pdf_ocr": False,
                "source_manifest": {
                    "sha256": "abc",
                    "row_count": 3_329,
                },
            },
        },
        "work_keys": [str(index) for index in range(3_224)],
    }

    assert (
        module["validate_plan_payload"](
            payload=payload,
            source_manifest=source_manifest,
            source_manifest_sha256="abc",
            parser_metric_count=84,
        )
        == []
    )


def test_dp4_plan_gate_rejects_partial_or_unsealed_plan() -> None:
    module = _script()
    errors = module["validate_plan_payload"](
        payload={
            "mode": "plan_only",
            "summary": {
                "requested_tickers": 160,
                "scheduled_accessions": 3_223,
                "scheduled_documents": 3_328,
                "skipped_completed_accessions": 0,
                "missing_cache_accessions": 1,
                "execution_scope": {
                    "all_metrics": False,
                    "max_filings_per_ticker": 8,
                    "max_documents_per_filing": 16,
                    "enable_arelle": True,
                    "enable_edgartools": True,
                    "enable_pdf_ocr": True,
                    "source_manifest": {
                        "sha256": "wrong",
                        "row_count": 3_328,
                    },
                },
            },
            "work_keys": [],
        },
        source_manifest={
            "identity_count": 160,
            "selected_identity_count": 160,
            "selected_accession_count": 3_224,
            "selected_document_row_count": 3_329,
            "parser_metric_count": 84,
        },
        source_manifest_sha256="expected",
        parser_metric_count=84,
    )

    assert errors
    assert any("missing or manifest-mismatched" in error for error in errors)
    assert any("all parser metrics" in error for error in errors)
    assert any("source-manifest hash" in error for error in errors)
