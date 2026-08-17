from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def test_transactional_backup_is_verified_and_refuses_overwrite(tmp_path: Path) -> None:
    from importlib.util import module_from_spec, spec_from_file_location

    script = Path(__file__).resolve().parents[2] / "consumer_defensive" / "scripts" / "00b_create_transactional_sqlite_backup.py"
    spec = spec_from_file_location("consumer_defensive_transactional_backup", script)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    source = tmp_path / "source.sqlite"
    destination = tmp_path / "backup.sqlite"
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample(value) VALUES ('sealed')")

    result = module.create_backup(source, destination)
    assert result["quick_check"] == "ok"
    assert result["foreign_key_violations"] == 0
    assert len(str(result["sha256"])) == 64
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone() == ("sealed",)

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        module.create_backup(source, destination)
