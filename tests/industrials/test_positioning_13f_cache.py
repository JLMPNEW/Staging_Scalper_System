from __future__ import annotations

import importlib
from pathlib import Path

import pytest


sync = importlib.import_module(
    "industrials.scripts.13_sync_industrials_positioning_upstream"
)


def test_cached_sec_13f_archives_selects_only_requested_years(
    tmp_path: Path,
) -> None:
    for name in (
        "2018q4_form13f.zip",
        "2019q1_form13f.zip",
        "01dec2025-28feb2026_form13f.zip",
        "unrelated.zip",
    ):
        (tmp_path / name).write_bytes(b"cached")

    selected = sync.cached_sec_13f_archives(
        tmp_path,
        start_year=2019,
        end_year=2026,
    )

    assert [path.name for path in selected] == [
        "2019q1_form13f.zip",
        "01dec2025-28feb2026_form13f.zip",
    ]


def test_cached_sec_13f_archives_rejects_empty_archive(
    tmp_path: Path,
) -> None:
    (tmp_path / "2026q1_form13f.zip").touch()
    with pytest.raises(ValueError, match="archive is empty"):
        sync.cached_sec_13f_archives(
            tmp_path,
            start_year=2019,
            end_year=2026,
        )
