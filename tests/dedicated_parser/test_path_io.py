from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from dedicated_parser.contracts import file_sha256
from dedicated_parser.path_io import (
    filesystem_path,
    is_dir,
    is_file,
    lexists_path,
    link_path,
    mkdir_path,
    open_path,
    path_exists,
    read_bytes,
    replace_path,
    resolve_path,
    runtime_path,
    stat_path,
    unlink_path,
)
from dedicated_parser.sec_paths import resolve_sec_document_path


def _long_accession_directory(tmp_path: Path) -> Path:
    directory = tmp_path
    while len(str(directory / "filing.htm")) <= 280:
        directory /= "cache-segment-0123456789"
    return directory


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path regression")
def test_extended_length_path_supports_full_file_lifecycle(tmp_path: Path) -> None:
    accession = _long_accession_directory(tmp_path)
    assert len(str(accession / "filing.htm")) > 260
    mkdir_path(accession, parents=True)
    assert is_dir(accession)

    document = accession / "filing.htm"
    payload = b"immutable SEC filing bytes"
    with open_path(document, "wb") as handle:
        handle.write(payload)

    assert str(filesystem_path(document)).startswith("\\\\?\\")
    assert str(runtime_path(document)).startswith("\\\\?\\")
    assert runtime_path(tmp_path) == tmp_path.absolute()
    assert lexists_path(document)
    assert path_exists(document)
    assert is_file(document)
    assert stat_path(document).st_size == len(payload)
    assert read_bytes(document) == payload
    assert file_sha256(document) == hashlib.sha256(payload).hexdigest()

    resolved = resolve_sec_document_path(
        accession,
        "filing.htm",
        containment_root=tmp_path,
        require_file=True,
    )
    assert resolved == resolve_path(document, strict=True)

    linked = accession / "linked.htm"
    link_path(document, linked)
    assert read_bytes(linked) == payload

    replacement = accession / "replacement.htm"
    with open_path(replacement, "wb") as handle:
        handle.write(b"replacement")
    replace_path(replacement, linked)
    assert read_bytes(linked) == b"replacement"
    assert not path_exists(replacement)

    unlink_path(linked)
    assert not lexists_path(linked)
    unlink_path(linked, missing_ok=True)
