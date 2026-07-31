from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "scripts"
    / "23_audit_transportation_release_integrity.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "transportation_release_integrity_script",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_recursive_audit_accepts_exact_reconstructed_lineage(tmp_path: Path) -> None:
    module = _module()
    original = tmp_path / "residual.csv"
    recovered = tmp_path / "pre_repair_residual.csv"
    frozen = b"ticker,metric\nABC,fleet_utilization\n"
    recovered.write_bytes(frozen)
    original.write_bytes(b"overwritten,newer\n")
    root = tmp_path / "manifest.json"
    root.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "inputs": {
                    "residual": {
                        "path": str(original),
                        "sha256": _hash(frozen),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    results, errors = module.recursive_artifact_audit(
        [root],
        repair_aliases={_hash(frozen): recovered},
    )
    assert errors == []
    assert len(results) == 1
    assert results[0]["status"] == "REPAIRED_BY_ATTESTED_ALIAS"
    assert results[0]["resolved_path"] == str(recovered.resolve())


def test_recursive_audit_fails_unattested_hash_mismatch(tmp_path: Path) -> None:
    module = _module()
    artifact = tmp_path / "artifact.csv"
    artifact.write_bytes(b"changed\n")
    root = tmp_path / "manifest.json"
    root.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "artifact": {
                    "path": str(artifact),
                    "sha256": _hash(b"expected\n"),
                },
            }
        ),
        encoding="utf-8",
    )
    results, errors = module.recursive_artifact_audit(
        [root],
        repair_aliases={},
    )
    assert results[0]["status"] == "FAIL"
    assert len(errors) == 1
    assert "artifact hash mismatch" in errors[0]

def test_recursive_audit_accepts_explicit_pass_with_limitations(
    tmp_path: Path,
) -> None:
    module = _module()
    root = tmp_path / "bounded_execution.json"
    root.write_text(
        json.dumps(
            {
                "acceptance": "PASS_WITH_EXPLICIT_LIMITATIONS",
                "errors": [],
                "limitations": ["local OCR engine unavailable"],
            }
        ),
        encoding="utf-8",
    )
    results, errors = module.recursive_artifact_audit(
        [root],
        repair_aliases={},
    )
    assert results == []
    assert errors == []