from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from future_only_evidence.canonical_archive import archive_file_once


@pytest.mark.parametrize(
    "asof_date",
    ["2026-09-30T00:00:00Z", "2026-09-30junk", date(2026, 9, 30)],
)
def test_archive_rejects_noncanonical_or_nonstr_asof(
    tmp_path: Path,
    asof_date: object,
) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(b"{}")
    with pytest.raises(ValueError, match="exact YYYY-MM-DD string"):
        archive_file_once(
            source,
            expected_sha256=hashlib.sha256(b"{}").hexdigest(),
            archive_root=tmp_path / "archive",
            family="consumer_defensive",
            asof_date=asof_date,  # type: ignore[arg-type]
            role="rank_snapshot",
        )
