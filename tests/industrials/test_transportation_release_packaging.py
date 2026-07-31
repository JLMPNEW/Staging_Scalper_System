from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from industrials.transportation.release_contract import (
    DEFAULT_RELEASE_NAME,
    required_release_source_paths,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "scripts"
    / "23_audit_transportation_release_integrity.py"
)


def _audit_module():
    spec = importlib.util.spec_from_file_location(
        "transportation_release_integrity_packaging_test",
        AUDIT_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v3_source_census_includes_shared_and_independent_dependencies() -> None:
    paths = set(required_release_source_paths(PROJECT_ROOT))
    assert DEFAULT_RELEASE_NAME == "code_aligned_zero_overlay_v3"
    assert "industrials/config.yaml" in paths
    assert "industrials/core/score_history.py" in paths
    assert "industrials/core/historical_score_history.py" in paths
    assert "industrials/core/market_feature_history.py" in paths
    assert "industrials/core/oos_research.py" in paths
    assert "industrials/core/production_lock.py" in paths
    assert "industrials/scripts/08_build_industrials_financial_features.py" in paths
    assert "dedicated_parser/cli.py" in paths
    assert "portfolio_layer/scores/adapters.py" in paths
    assert "portfolio_layer/config.yaml" not in paths
    assert "industrials/transportation/release_contract.py" in paths
    assert (
        "tests/industrials/test_transportation_release_packaging.py" in paths
    )


def test_recursive_audit_honors_non_recursive_predecessor_reference(
    tmp_path: Path,
) -> None:
    module = _audit_module()
    missing = tmp_path / "missing.csv"
    predecessor = tmp_path / "predecessor.json"
    predecessor.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "stale_live_reference": {
                    "path": str(missing),
                    "sha256": "0" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(predecessor.read_bytes()).hexdigest()
    release = tmp_path / "release.json"
    release.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "predecessor": {
                    "path": str(predecessor),
                    "sha256": digest,
                    "recurse": False,
                },
            }
        ),
        encoding="utf-8",
    )
    results, errors = module.recursive_artifact_audit(
        [release],
        repair_aliases={},
    )
    assert errors == []
    assert len(results) == 1
    assert results[0]["status"] == "PASS"
