from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from consumer_defensive.core.config import load_config
from consumer_defensive.core.production_scores_v3 import publisher_bindings


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "consumer_defensive"
    / "scripts"
    / "31_publish_consumer_defensive_production_scores_v3.py"
)
CONFIG = ROOT / "consumer_defensive" / "config.yaml"
COMPLETED_STAGE6A_DB = publisher_bindings(load_config(CONFIG))["source_database_path"]


def _module():
    spec = importlib.util.spec_from_file_location("consumer_score_publisher_v3", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_script31_exposes_orchestration_contract() -> None:
    parser = _module()._parser()
    actions = {action.dest: action for action in parser._actions}
    assert actions["asof"].required is True
    assert {
        "signal_asof_date",
        "config",
        "db",
        "output_root",
        "activation_registry",
        "trusted_activation_registry_file_sha256",
        "candidate_registry",
        "trusted_candidate_registry_file_sha256",
    }.issubset(actions)
    assert "network" not in actions
    assert "force" not in actions


def test_default_signal_query_cannot_select_same_day_or_future() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE feature_scoring_input(model_family TEXT,asof_date TEXT)"
    )
    connection.executemany(
        "INSERT INTO feature_scoring_input VALUES ('consumer_defensive',?)",
        [("2026-08-27",), ("2026-08-28",), ("2026-09-01",)],
    )
    assert _module()._latest_signal_date(connection, allocation_asof_date="2026-08-28") == (
        "2026-08-27"
    )
    connection.close()


def test_script31_publishes_from_one_read_only_snapshot(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--asof",
            "2026-08-28",
            "--signal-asof-date",
            "2026-08-27",
            "--db",
            str(COMPLETED_STAGE6A_DB),
            "--output-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    status = json.loads(completed.stdout)
    assert status["status"] == "PASS"
    manifest_path = (
        tmp_path
        / "consumer_defensive"
        / "dashboard"
        / "2026-08-28"
        / "consumer_defensive_production_score_manifest_v3.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_database_path"] == COMPLETED_STAGE6A_DB.resolve().as_posix()
    assert len(manifest["source_database_file_sha256"]) == 64
    assert manifest["database_access_mode"] == "read_only"
    assert manifest["database_write_count"] == 0
    assert manifest["rank_row_count"] == 79
    assert manifest["published_ticker_count"] == 79
    assert manifest["observed_excluded_ticker_count"] == 31
