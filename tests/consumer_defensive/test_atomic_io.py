from __future__ import annotations

import os
from pathlib import Path

import consumer_defensive.core.atomic_io as atomic_io


def test_atomic_writer_uses_bounded_same_directory_temp_for_long_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_path_length = 250
    destination_name_length = (
        target_path_length - len(os.fspath(tmp_path)) - len(os.sep)
    )
    assert destination_name_length > len('.json')
    destination_name = (
        'x' * (destination_name_length - len('.json')) + '.json'
    )
    destination = tmp_path / destination_name
    replaced: dict[str, Path] = {}
    original_replace = os.replace

    def observed_replace(source: object, target: object) -> None:
        source_path = Path(source)
        target_path = Path(target)
        assert source_path.exists()
        replaced['source'] = source_path
        replaced['target'] = target_path
        original_replace(source_path, target_path)

    monkeypatch.setattr(atomic_io.os, 'replace', observed_replace)
    atomic_io.atomic_write_text(destination, 'factor-validation\n')

    temporary = replaced['source']
    assert len(os.fspath(destination)) == target_path_length
    assert temporary.parent == destination.parent
    assert temporary.name.startswith(atomic_io._ATOMIC_TEMP_PREFIX)
    assert temporary.name.endswith(atomic_io._ATOMIC_TEMP_SUFFIX)
    assert len(temporary.name) < len(destination.name)
    assert len(os.fspath(temporary)) < target_path_length
    assert replaced['target'] == destination
    assert destination.read_text(encoding='utf-8') == 'factor-validation\n'
    assert not temporary.exists()
    assert not list(tmp_path.glob(f'{atomic_io._ATOMIC_TEMP_PREFIX}*'))
