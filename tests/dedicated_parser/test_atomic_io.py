from __future__ import annotations

import os
from pathlib import Path

import pytest

from dedicated_parser.atomic_io import atomic_write_text
from dedicated_parser.providers.edgartools_provider import inspect_full_submission


def test_atomic_writer_does_not_follow_target_or_legacy_temp_hardlinks(
    tmp_path: Path,
) -> None:
    outside_target = tmp_path / 'outside-target.txt'
    outside_temp = tmp_path / 'outside-temp.txt'
    outside_target.write_text('target-secret', encoding='utf-8')
    outside_temp.write_text('temp-secret', encoding='utf-8')
    destination = tmp_path / 'report.json'
    legacy_temporary = destination.with_suffix(destination.suffix + '.tmp')
    try:
        os.link(outside_target, destination)
        os.link(outside_temp, legacy_temporary)
    except OSError as exc:
        pytest.skip(f'hardlinks are unavailable: {exc}')
    atomic_write_text(destination, 'published\n')
    assert destination.read_text(encoding='utf-8') == 'published\n'
    assert outside_target.read_text(encoding='utf-8') == 'target-secret'
    assert outside_temp.read_text(encoding='utf-8') == 'temp-secret'
    assert legacy_temporary.read_text(encoding='utf-8') == 'temp-secret'
    assert sorted(tmp_path.glob('.report.json.*.tmp')) == []


def test_atomic_writer_cleans_only_its_unique_temporary_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / 'report.json'
    destination.write_text('last-good', encoding='utf-8')

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError('injected replace failure')

    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError, match='injected replace failure'):
        atomic_write_text(destination, 'new-value')
    assert destination.read_text(encoding='utf-8') == 'last-good'
    assert sorted(tmp_path.glob('.report.json.*.tmp')) == []


def test_edgartools_worker_state_rejects_precreated_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / 'state'
    outside = tmp_path / 'outside'
    state.mkdir()
    outside.mkdir()
    monkeypatch.setattr(os, 'getpid', lambda: 424242)
    worker = state / 'worker-424242'
    try:
        worker.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'directory symlinks are unavailable: {exc}')
    with pytest.raises(RuntimeError, match='symlinked identity'):
        inspect_full_submission(
            tmp_path / 'unused-submission.txt', state_dir=state
        )
    assert list(outside.iterdir()) == []
