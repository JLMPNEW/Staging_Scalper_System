"""End-to-end smoke test for the package-owned CLI sequence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "basic_materials" / "scripts"


def _run(script: str, *arguments: str) -> dict:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    return json.loads(completed.stdout)


def test_cli_stage_zero_through_financial_contract(tmp_path: Path) -> None:
    database = tmp_path / "basic_materials.sqlite"
    reports = tmp_path / "reports"

    independence = _run("00a_validate_basic_materials_independence.py")
    initialized = _run("00_init_basic_materials_db.py", "--db", str(database))
    loaded = _run("01_load_basic_materials_universe.py", "--db", str(database))
    validated = _run(
        "02_validate_basic_materials_universe.py",
        "--db",
        str(database),
        "--report-dir",
        str(reports),
    )
    historical_loaded = _run(
        "01b_load_basic_materials_historical_membership.py",
        "--db",
        str(database),
    )
    historical_validated = _run(
        "02c_validate_basic_materials_historical_membership.py",
        "--db",
        str(database),
        "--report-dir",
        str(reports / "historical"),
    )
    market_contract = _run(
        "03_load_basic_materials_market_contract.py",
        "--db",
        str(database),
    )
    financial_contract = _run(
        "05_load_basic_materials_financial_contract.py",
        "--db",
        str(database),
    )
    financial_validated = _run(
        "06_validate_basic_materials_financial_contract.py",
        "--db",
        str(database),
        "--report-dir",
        str(reports / "financial_contract"),
    )

    assert independence["passed"] is True
    assert initialized["schema_version"] == 6
    assert loaded["rows_loaded"] == 134
    assert loaded["calibration_groups_derived"] == 134
    assert validated["passed"] is True
    assert validated["warning_count"] == 1
    assert Path(validated["artifacts"]["artifact_manifest"]).is_file()
    assert historical_loaded["historical_memberships"] == 20
    assert historical_loaded["calibration_eligible_rows"] == 0
    assert historical_validated["passed"] is True
    assert historical_validated["unresolved_terminal_events"] == 20
    assert historical_validated["calibration_eligible_rows"] == 0
    assert Path(historical_validated["artifacts"]["artifact_manifest"]).is_file()
    assert market_contract["unique_instruments"] == 158
    assert market_contract["role_rows"] == 162
    assert market_contract["terminal_rules"] == 20
    assert financial_contract["profiles"] == 154
    assert financial_contract["metrics"] == 22
    assert financial_contract["concept_links"] == 66
    assert financial_validated["passed"] is True
    assert financial_validated["error_count"] == 0
    assert financial_validated["actual_counts"]["profiles"] == 154
    assert Path(financial_validated["artifacts"]["artifact_manifest"]).is_file()
