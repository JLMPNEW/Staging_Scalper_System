from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from industrials.transportation.contracts import file_sha256


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "industrials"
    / "transportation"
    / "scripts"
    / "41_build_transportation_v8_subgroup_scores.py"
)


def load_script():
    spec = importlib.util.spec_from_file_location(
        "transportation_conflict_bridge_test",
        SCRIPT,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_replay(path: Path, lane: str) -> list[dict[str, str]]:
    rows = [
        {
            "ticker": "AAA",
            "metric_id": "operating_ratio",
            "value": "0.80",
            "conflict_group_id": f"{lane}-resolved",
            "conflict_resolution_status": "RESOLVED_DETERMINISTIC",
        },
        {
            "ticker": "AAA",
            "metric_id": "operating_ratio",
            "value": "0.85",
            "conflict_group_id": "shared-residual",
            "conflict_resolution_status": "FAIL_CLOSED_REVIEW_REQUIRED",
        },
        {
            "ticker": "AAA",
            "metric_id": "operating_ratio",
            "value": "0.90",
            "conflict_group_id": "shared-residual",
            "conflict_resolution_status": "FAIL_CLOSED_REVIEW_REQUIRED",
        },
        {
            "ticker": "BBB",
            "metric_id": "operating_ratio",
            "value": "0.70",
            "conflict_group_id": "",
            "conflict_resolution_status": "NOT_CONFLICTED",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def bridge_fixture(tmp_path: Path):
    module = load_script()
    original_surface = tmp_path / "surface-original.csv"
    original_tanker = tmp_path / "tanker-original.csv"
    original_surface.write_text("surface original\n", encoding="utf-8")
    original_tanker.write_text("tanker original\n", encoding="utf-8")
    surface = tmp_path / "surface-normalized.csv"
    tanker = tmp_path / "tanker-normalized.csv"
    rows = write_replay(surface, "surface") + write_replay(tanker, "tanker")
    coverage = {
        "input_hashes": {
            "surface_replay": file_sha256(original_surface),
            "tanker_replay": file_sha256(original_tanker),
        }
    }
    audit = {
        "acceptance": "PASS",
        "resolver_conflict_count_before": 4,
        "deterministic_false_conflict_count": 3,
        "resolver_conflict_count_after": 1,
        "unresolved_fail_closed_count": 1,
        "normalized_row_count": len(rows),
        "resolution_count_by_rule": {"deterministic_test_rule": 3},
        "residual_count_by_classification": {"test_ambiguity": 1},
        "conflict_count_after_by_metric": {"operating_ratio": 1},
        "unresolved_conflicts_fail_closed": True,
        "source_conflicts_are_never_averaged": True,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "lineage": {
            "surface_accepted_replay": {
                "path": str(original_surface),
                "sha256": file_sha256(original_surface),
            },
            "tanker_accepted_replay": {
                "path": str(original_tanker),
                "sha256": file_sha256(original_tanker),
            },
        },
        "artifacts": {
            "surface_normalized_replay": {
                "path": str(surface.resolve()),
                "sha256": file_sha256(surface),
            },
            "tanker_normalized_replay": {
                "path": str(tanker.resolve()),
                "sha256": file_sha256(tanker),
            },
        },
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    arguments = {
        "audit_path": audit_path,
        "audit": audit,
        "coverage": coverage,
        "replay_paths": {
            "surface_replay": surface,
            "tanker_replay": tanker,
        },
        "accepted_rows": rows,
    }
    return module, arguments


def test_conflict_audit_bridge_binds_original_and_normalized_lineage(
    tmp_path: Path,
) -> None:
    module, arguments = bridge_fixture(tmp_path)
    result = module.validate_conflict_audit_bridge(**arguments)
    assert result["status"] == "VERIFIED"
    assert result["deterministic_false_conflict_count"] == 3
    assert result["unresolved_fail_closed_count"] == 1
    assert result["audit_sha256"] == file_sha256(arguments["audit_path"])


def test_conflict_audit_bridge_rejects_original_lineage_mismatch(
    tmp_path: Path,
) -> None:
    module, arguments = bridge_fixture(tmp_path)
    arguments["coverage"]["input_hashes"]["surface_replay"] = "0" * 64
    with pytest.raises(ValueError, match="original hash"):
        module.validate_conflict_audit_bridge(**arguments)


def test_conflict_audit_bridge_rejects_normalized_hash_mismatch(
    tmp_path: Path,
) -> None:
    module, arguments = bridge_fixture(tmp_path)
    arguments["audit"]["artifacts"]["tanker_normalized_replay"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="normalized replay hash"):
        module.validate_conflict_audit_bridge(**arguments)


def test_conflict_audit_bridge_rejects_non_fail_closed_audit(
    tmp_path: Path,
) -> None:
    module, arguments = bridge_fixture(tmp_path)
    arguments["audit"]["unresolved_conflicts_fail_closed"] = False
    with pytest.raises(ValueError, match="does not fail closed"):
        module.validate_conflict_audit_bridge(**arguments)


def test_conflict_audit_bridge_rejects_residual_row_count_mismatch(
    tmp_path: Path,
) -> None:
    module, arguments = bridge_fixture(tmp_path)
    arguments["audit"]["resolver_conflict_count_before"] = 5
    arguments["audit"]["resolver_conflict_count_after"] = 2
    arguments["audit"]["unresolved_fail_closed_count"] = 2
    arguments["audit"]["residual_count_by_classification"] = {"test_ambiguity": 2}
    arguments["audit"]["conflict_count_after_by_metric"] = {"operating_ratio": 2}
    with pytest.raises(ValueError, match="residual group count"):
        module.validate_conflict_audit_bridge(**arguments)
