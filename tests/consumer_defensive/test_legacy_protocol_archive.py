from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "archive/consumer_defensive_legacy_future_evidence_20260826"


def test_legacy_consumer_protocol_archive_is_complete_and_recoverable() -> None:
    manifest = json.loads((ARCHIVE / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "retired_audit_only"
    assert manifest["admissible_for_v2_evidence"] is False
    assert manifest["transportation_files_moved"] == 0
    assert manifest["file_count"] == 37
    assert len(manifest["files"]) == 37
    for row in manifest["files"]:
        archived = ROOT / row["archived_path"]
        original = ROOT / row["original_path"]
        assert archived.is_file()
        assert original.exists() is False
        assert archived.stat().st_size == row["bytes"]
        assert hashlib.sha256(archived.read_bytes()).hexdigest() == row["sha256"]


def test_retired_routes_are_not_registered_for_execution() -> None:
    registry = (ROOT / "orchestration/registry.yaml").read_text(encoding="utf-8")
    assert "28_run_consumer_defensive_stage12_pipeline.py" not in registry
    assert "29_backfill_consumer_defensive_stage11.py" not in registry
    assert "27_run_consumer_defensive_v2_foundation.py" not in registry
    assert "32_run_consumer_defensive_production_refresh_v3.py" in registry
