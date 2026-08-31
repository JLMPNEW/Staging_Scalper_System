from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "archive/consumer_defensive_retired_generated_evidence_20260826"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_retired_generated_evidence_is_quarantined_and_hash_preserved() -> None:
    expected = {
        "artifacts/future_only_evidence/2026-08-26/consumer_defensive_preflight_v3.json": (
            "2d7615b4b9447b2b4e33bfdb8a3a7d7d15a6be921bc1243cc7b779de99ef461e"
        ),
        "output/consumer_defensive/future_oos_preflight/2026-08-25/v1/consumer_defensive_future_oos_preflight.json": (
            "ce6506a6743262678cac2837a4a31b26b7dda39f48c59dcee4de21814815503b"
        ),
        "output/consumer_defensive/future_oos_preflight/2026-08-25/v2/consumer_defensive_future_oos_preflight.json": (
            "347b715bbdf814a4e0a0ddbdffb0a880a24e30c31e3a1dab9d6e63bd6f6e7024"
        ),
    }
    for former_path, digest in expected.items():
        assert not (ROOT / former_path).exists()
        archived = ARCHIVE / former_path
        assert archived.is_file()
        assert _sha256(archived) == digest
    assert (ARCHIVE / "MANIFEST.md").is_file()
