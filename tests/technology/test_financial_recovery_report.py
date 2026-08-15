from __future__ import annotations

import csv
import importlib.util
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_recovery_report_accepts_mixed_status_schemas(tmp_path: Path) -> None:
    script = PROJECT_ROOT / "technology" / "scripts" / "07b_recover_technology_6k_financials.py"
    spec = importlib.util.spec_from_file_location("technology_6k_recovery_report_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "recovery.csv"

    module._write_csv(
        output,
        [
            {
                "ticker": "SAFE",
                "accession_number": "a1",
                "status": "RECOVERED",
                "structured_fact_count": 8,
            },
            {
                "ticker": "MISS",
                "accession_number": "a2",
                "status": "CACHE_MISSING",
            },
        ],
    )

    with output.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["structured_fact_count"] == "8"
    assert rows[1]["structured_fact_count"] == ""
