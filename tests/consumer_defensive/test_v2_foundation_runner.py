from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "consumer_defensive/scripts/27_run_consumer_defensive_v2_foundation.py"


def _run(output_root: Path, *, asof: str = "2026-08-26") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--asof",
            asof,
            "--output-root",
            str(output_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_v2_foundation_runner_publishes_hashed_non_active_status_idempotently(
    tmp_path: Path,
) -> None:
    completed = _run(tmp_path)
    assert completed.returncode == 0, completed.stderr
    output = tmp_path / "2026-08-26/consumer_defensive_framework_status.json"
    original = output.read_bytes()
    payload = json.loads(original)
    assert payload["acceptance"] == "PASS"
    assert payload["contract_validation_acceptance"] == "PASS"
    assert payload["non_activation_guard"] == "PASS"
    assert payload["operational_health"] == "NOT_EVALUATED"
    assert payload["production_ready"] is False
    assert payload["portfolio_write_enabled"] is False
    assert payload["active_cap"] == 0.0
    assert len(payload["payload_sha256"]) == 64
    assert set(payload["cohort_states"].values()) == {"benchmark_production"}
    repeated = _run(tmp_path)
    assert repeated.returncode == 0, repeated.stderr
    assert output.read_bytes() == original


def test_v2_foundation_runner_refuses_divergent_same_asof_overwrite(tmp_path: Path) -> None:
    assert _run(tmp_path).returncode == 0
    output = tmp_path / "2026-08-26/consumer_defensive_framework_status.json"
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["production_ready"] = True
    output.write_text(json.dumps(payload), encoding="utf-8")
    completed = _run(tmp_path)
    assert completed.returncode != 0
    assert "inconsistent" in completed.stderr or "self-hash" in completed.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["production_ready"] is True


def test_v2_foundation_runner_rejects_future_and_non_session_dates(tmp_path: Path) -> None:
    future = _run(tmp_path, asof="2099-01-02")
    assert future.returncode != 0
    assert "future-dated" in future.stderr
    weekend = _run(tmp_path, asof="2026-08-22")
    assert weekend.returncode != 0
    assert "trading session" in weekend.stderr


def test_global_orchestrator_requires_production_scores_before_portfolio() -> None:
    registry = yaml.safe_load((ROOT / "orchestration/registry.yaml").read_text(encoding="utf-8"))
    consumer = next(item for item in registry["sectors"] if item["name"] == "consumer_defensive")
    portfolio = next(item for item in registry["sectors"] if item["name"] == "portfolio_layer")
    assert registry["group_order"]["consumer_defensive"] == ["consumer_defensive"]
    assert (
        consumer["entry_script"]
        == "consumer_defensive/scripts/32_run_consumer_defensive_production_refresh_v3.py"
    )
    assert (ROOT / consumer["entry_script"]).is_file()
    assert consumer["required"] is True
    assert consumer["network"] is True
    assert consumer["dependency_tier"] == 0
    assert portfolio["required"] is True
    assert portfolio["dependency_tier"] == 1
    assert consumer["args_template"] == ["--asof", "{date}"]
    # Scheduled refreshes are incremental. A forced full source reload would
    # defeat catch-up and needlessly refetch immutable history.
    assert consumer["force_args"] == []
    assert consumer["retry_args"] == ["--resume"]
    assert consumer["publish_glob"] == (
        "output/consumer_defensive/dashboard/{date}/consumer_defensive_final_rank_table.csv"
    )
    assert consumer["publish_epoch"] == "2026-08-28"
    assert consumer["oos_column"] == "oos_score_valid_flag"
    assert consumer["require_oos_valid"] is True
    assert consumer["staleness_tolerance_days"] == 3
    assert consumer["backfill"] is None
    assert consumer["repair"] is None
    assert consumer["health"] == {
        "manifest": (
            "output/consumer_defensive/orchestration/{date}/"
            "consumer_defensive_production_refresh_manifest_v3.json"
        ),
        "status_keys": ["status"],
    }
