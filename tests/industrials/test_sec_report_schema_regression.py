from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py"


def load_module():
    spec = importlib.util.spec_from_file_location("sec_sync_report_schema_regression", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_preserved_legacy_report_columns_are_filtered(tmp_path: Path) -> None:
    module = load_module()
    output = tmp_path / "coverage.csv"
    legacy = {field: "" for field in module.REPORT_FIELDS}
    legacy.update(
        {
            "ticker": "LEGACY",
            "status": "success",
            "archive_request_count": "12",
            "cached_fact_candidate_count": "34",
            "error": "old schema",
        }
    )
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*module.REPORT_FIELDS, "archive_request_count", "cached_fact_candidate_count", "error"])
        writer.writeheader()
        writer.writerow(legacy)

    current = {field: "" for field in module.REPORT_FIELDS}
    current.update({"ticker": "CURRENT", "status": "success"})
    module.write_report(output, [current], preserve_existing_tickers=True)

    with output.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        assert reader.fieldnames == list(module.REPORT_FIELDS)
    assert [row["ticker"] for row in rows] == ["CURRENT", "LEGACY"]
