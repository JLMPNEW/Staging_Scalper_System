from __future__ import annotations

import copy
import hashlib
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from consumer_defensive.core.config import (
    load_config,
    validate_config,
    validate_contract_bundle,
)
from consumer_defensive.core.db import connect, init_db
from consumer_defensive.core.input_manifest import validate_authoritative_input_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "consumer_defensive"
CONFIG_PATH = PACKAGE_ROOT / "config.yaml"
MANIFEST_PATH = PACKAGE_ROOT / "data" / "authoritative_input_manifest.yaml"


def _config_copy() -> dict[str, Any]:
    return copy.deepcopy(load_config(CONFIG_PATH).payload)


def _copy_manifest_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    repository_root = tmp_path / "repository"
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    rows = manifest.get("inputs")
    assert isinstance(rows, list) and rows

    fixture_manifest = (
        repository_root
        / "consumer_defensive"
        / "data"
        / "authoritative_input_manifest.yaml"
    )
    fixture_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MANIFEST_PATH, fixture_manifest)
    for row in rows:
        assert isinstance(row, dict)
        relative = Path(str(row["path"]))
        source = PROJECT_ROOT / relative
        destination = repository_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return fixture_manifest, repository_root, manifest


def test_nested_config_typo_is_rejected() -> None:
    config = _config_copy()
    config["runtime"]["sqlite_timeot_sec"] = config["runtime"]["sqlite_timeout_sec"]

    with pytest.raises(
        ValueError,
        match=r"Unknown Consumer Defensive config keys under runtime: sqlite_timeot_sec",
    ):
        validate_config(config)


def test_typo_inside_nested_membership_vehicle_is_rejected() -> None:
    config = _config_copy()
    vehicle = config["universe"]["recognized_membership_vehicles"][0]
    vehicle["norgate_indx_name"] = vehicle.pop("norgate_index_name")

    with pytest.raises(ValueError, match=r"recognized-membership vehicle must contain exactly"):
        validate_config(config)


@pytest.mark.parametrize(
    ("section", "field", "replacement", "contract_label"),
    [
        ("universe", "expected_current_rows", 109, "universe.expected_current_rows"),
        ("market_data_policy", "benchmark_tickers", ["SPY", "XLP"], "market.benchmarks"),
    ],
)
def test_duplicated_universe_and_market_contract_drift_is_rejected(
    section: str,
    field: str,
    replacement: object,
    contract_label: str,
) -> None:
    config = _config_copy()
    config[section][field] = replacement

    with pytest.raises(
        ValueError,
        match=rf"Consumer Defensive contract drift for {re.escape(contract_label)}:",
    ):
        validate_contract_bundle(config, base_dir=PACKAGE_ROOT)


def test_authoritative_input_manifest_verifies_current_inventory() -> None:
    result = validate_authoritative_input_manifest(
        MANIFEST_PATH,
        repository_root=PROJECT_ROOT,
    )

    expected_rows = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))["inputs"]
    assert result["verified_inputs"] == len(expected_rows) == 8
    assert len(result["manifest_sha256"]) == 64
    assert result["manifest_sha256"] == hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    assert {row["path"] for row in result["inputs"]} == {
        str(row["path"]).replace("\\", "/") for row in expected_rows
    }


def test_authoritative_input_manifest_rejects_tampered_file(tmp_path: Path) -> None:
    manifest_path, repository_root, manifest = _copy_manifest_fixture(tmp_path)
    first_input = repository_root / str(manifest["inputs"][0]["path"])
    first_input.write_bytes(first_input.read_bytes() + b"\n")

    with pytest.raises(ValueError, match=r"Authoritative input SHA-256 mismatch"):
        validate_authoritative_input_manifest(
            manifest_path,
            repository_root=repository_root,
        )


def test_authoritative_input_manifest_rejects_unlisted_csv(tmp_path: Path) -> None:
    manifest_path, repository_root, _ = _copy_manifest_fixture(tmp_path)
    unlisted = repository_root / "consumer_defensive" / "data" / "unlisted_authoritative.csv"
    unlisted.write_text("ticker\nUNLISTED\n", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"Authoritative Consumer Defensive CSV inventory differs from the manifest: .*unlisted_authoritative\.csv",
    ):
        validate_authoritative_input_manifest(
            manifest_path,
            repository_root=repository_root,
        )


@pytest.mark.parametrize(
    ("database_kind", "expected_error"),
    [
        ("foreign_identity", r"database identity is missing or inconsistent"),
        ("unowned", r"non-empty unowned database"),
    ],
)
def test_nonempty_foreign_or_unowned_database_is_rejected_without_mutation(
    tmp_path: Path,
    database_kind: str,
    expected_error: str,
) -> None:
    db_path = tmp_path / f"{database_kind}.sqlite"
    with sqlite3.connect(db_path) as raw:
        if database_kind == "foreign_identity":
            raw.execute(
                """CREATE TABLE sector_database_identity(
                       identity_id INTEGER PRIMARY KEY,
                       model_family TEXT NOT NULL,
                       internal_sector TEXT NOT NULL,
                       schema_owner TEXT NOT NULL
                   )"""
            )
            raw.execute(
                "INSERT INTO sector_database_identity VALUES(1,'technology','Technology','technology')"
            )
        else:
            raw.execute("CREATE TABLE unrelated_application_state(value TEXT)")
            raw.execute("INSERT INTO unrelated_application_state VALUES('preserve-me')")

    with pytest.raises(RuntimeError, match=expected_error):
        connect(db_path)

    with sqlite3.connect(db_path) as raw:
        with pytest.raises(RuntimeError, match=expected_error):
            init_db(raw)
        tables = {
            str(row[0])
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "runs" not in tables
        assert "dim_consumer_defensive_taxonomy" not in tables
        if database_kind == "unowned":
            assert raw.execute("SELECT value FROM unrelated_application_state").fetchone() == (
                "preserve-me",
            )
