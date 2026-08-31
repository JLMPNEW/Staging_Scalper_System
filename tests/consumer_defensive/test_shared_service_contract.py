from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from consumer_defensive.core.config import load_config
from consumer_defensive.core.shared_services import (
    audit_config_connections,
    load_shared_service_contract,
    shared_service_contract_sha256,
    validate_shared_service_contract,
)
from consumer_defensive.core.stage5_import import _ro_connect


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "consumer_defensive/data/consumer_defensive_shared_service_contract_v1.yaml"


def test_frozen_shared_service_contract_covers_all_approved_boundaries() -> None:
    contract = load_shared_service_contract(CONTRACT)
    assert len(shared_service_contract_sha256(contract)) == 64
    assert set(contract["code_services"]) == {"dedicated_parser", "factor_validation"}
    assert set(contract["read_only_data_services"]) == {"sec_insider", "market_positioning"}
    assert contract["downstream_portfolio_services"]["access_via"] == "portfolio_layer_only"


def test_consumer_config_matches_frozen_shared_service_contract() -> None:
    bundle = load_config(ROOT / "consumer_defensive/config.yaml")
    audit = audit_config_connections(bundle.payload, repository_root=ROOT)
    assert audit["status"] == "PASS"
    assert audit["platform_services"] == ["global_orchestrator", "portfolio_layer"]


def test_stage5_shared_databases_are_opened_read_only(tmp_path: Path) -> None:
    database = tmp_path / "upstream.sqlite"
    with sqlite3.connect(database) as writable:
        writable.execute("CREATE TABLE evidence(value INTEGER NOT NULL)")
        writable.execute("INSERT INTO evidence VALUES (1)")
    with _ro_connect(database) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO evidence VALUES (2)")


def test_contract_rejects_direct_downstream_service_access() -> None:
    contract = load_shared_service_contract(CONTRACT)
    contract["downstream_portfolio_services"]["access_via"] = "direct"
    with pytest.raises(ValueError, match="Portfolio Layer"):
        validate_shared_service_contract(contract)

