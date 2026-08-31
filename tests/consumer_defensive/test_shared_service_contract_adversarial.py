from __future__ import annotations

import copy
from pathlib import Path

import pytest

from consumer_defensive.core.config import load_config
from consumer_defensive.core.shared_services import (
    audit_config_connections,
    audit_import_boundaries,
    load_shared_service_contract,
    validate_shared_service_contract,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "consumer_defensive/data/consumer_defensive_shared_service_contract_v1.yaml"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value["external_providers"]["sec_edgar"].update(
            {"access_mode": "arbitrary"}
        ),
        lambda value: value["code_services"]["dedicated_parser"].update(
            {"unexpected": "bypass"}
        ),
        lambda value: value["ownership"].update(
            {"cross_sector_code_imports_allowed": True}
        ),
    ],
)
def test_shared_service_contract_mutations_fail_closed(mutator) -> None:
    payload = copy.deepcopy(load_shared_service_contract(CONTRACT))
    mutator(payload)
    with pytest.raises(ValueError):
        validate_shared_service_contract(payload)


def test_import_boundary_census_is_enforced() -> None:
    result = audit_import_boundaries(repository_root=ROOT)
    assert result["checked_python_files"] > 0
    assert result["violations"] == 0


def test_connection_audit_rejects_missing_upstream_database(tmp_path: Path) -> None:
    bundle = load_config(ROOT / "consumer_defensive/config.yaml")
    payload = copy.deepcopy(bundle.payload)
    payload["positioning"]["form4_upstream_db"] = str(tmp_path / "missing.sqlite")
    with pytest.raises(ValueError, match="does not exist"):
        audit_config_connections(payload, repository_root=ROOT)
