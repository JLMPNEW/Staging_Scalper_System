from __future__ import annotations

import argparse
import json
import runpy
from pathlib import Path

import pytest

from SEC_FORM4_Runner.process_lock import WaitFileLock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_wait_file_lock_blocks_and_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "shared.sqlite.writer.lock"
    with WaitFileLock(
        lock_path,
        timeout_seconds=0,
        poll_seconds=0.01,
        owner="first",
    ):
        with pytest.raises(TimeoutError, match="Timed out"):
            with WaitFileLock(
                lock_path,
                timeout_seconds=0,
                poll_seconds=0.01,
                owner="second",
            ):
                raise AssertionError("contended lock must not be acquired")
    metadata = json.loads(lock_path.read_bytes()[1:].decode("utf-8"))
    assert metadata["owner"] == "first"
    assert int(metadata["pid"]) > 0

    with WaitFileLock(
        lock_path,
        timeout_seconds=0,
        poll_seconds=0.01,
        owner="second",
    ):
        pass
    metadata = json.loads(lock_path.read_bytes()[1:].decode("utf-8"))
    assert metadata["owner"] == "second"


@pytest.mark.parametrize(
    ("timeout_seconds", "poll_seconds"),
    [(-1, 1), (1, 0), (1, -1)],
)
def test_wait_file_lock_rejects_invalid_timing(
    tmp_path: Path,
    timeout_seconds: float,
    poll_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        WaitFileLock(
            tmp_path / "invalid.lock",
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )


def test_staging_runner_derives_lock_from_form4_database() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "SEC_FORM4_Runner" / "run_sec_form4_orchestrator.py")
    )
    config_path = (
        PROJECT_ROOT
        / "SEC_FORM4_Runner"
        / "config_sec_form4_orchestrator_staging.yaml"
    )
    args = argparse.Namespace(
        config=config_path,
        target=None,
        profile=None,
        as_of_date="2026-08-31",
        skip_alignment=False,
        dry_run=False,
    )
    lock_path, timeout_seconds, poll_seconds = namespace["writer_lock_settings"](args)
    assert lock_path is not None
    assert lock_path.name == "sec_insider.sqlite.writer.lock"
    assert timeout_seconds == 1800
    assert poll_seconds == 1

    args.dry_run = True
    dry_lock_path, _, _ = namespace["writer_lock_settings"](args)
    assert dry_lock_path is None
