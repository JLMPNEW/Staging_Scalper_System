from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from industrials.transportation.legacy_production_routes import (
    LegacyTransportationRouteDisabled,
    route_diagnostic as transportation_route_diagnostic,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(relative_path: str):
    path = PROJECT_ROOT / relative_path
    name = "legacy_lockdown_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "relative_path",
    [
        "industrials/transportation/scripts/27_promote_transportation_oos_production.py",
        "industrials/transportation/scripts/31_activate_transportation_oos_production.py",
        "industrials/transportation/scripts/33_package_transportation_production_release.py",
        "industrials/transportation/scripts/35_run_transportation_production_release_acceptance.py",
        "industrials/transportation/scripts/45_run_transportation_future_oos_protocol.py",
        "industrials/transportation/scripts/45a_preflight_transportation_future_oos.py",
        "industrials/transportation/scripts/45b_evaluate_transportation_future_oos.py",
        "industrials/transportation/scripts/45c_preflight_transportation_future_oos.py",
        "industrials/transportation/scripts/45d_capture_transportation_future_oos.py",
        "industrials/transportation/scripts/45e_capture_transportation_future_oos.py",
        "industrials/transportation/scripts/45f_evaluate_transportation_future_oos.py",
        "industrials/transportation/scripts/45g_capture_transportation_future_oos.py",
    ],
)
def test_legacy_transportation_clis_fail_before_mutation(
    relative_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(relative_path)
    monkeypatch.setattr(sys, "argv", [Path(relative_path).name])
    with pytest.raises(
        LegacyTransportationRouteDisabled,
        match="SUPERSEDED_FAIL_CLOSED",
    ):
        module.main()


def test_direct_legacy_activation_and_packaging_helpers_are_blocked() -> None:
    activation = _load(
        "industrials/transportation/scripts/"
        "31_activate_transportation_oos_production.py"
    )
    with pytest.raises(LegacyTransportationRouteDisabled):
        activation.validate_activation_scoring_mode(
            {"scoring_mode": "generic_oos"},
            {},
        )

    packaging = _load(
        "industrials/transportation/scripts/"
        "33_package_transportation_production_release.py"
    )
    with pytest.raises(LegacyTransportationRouteDisabled):
        packaging.portfolio_contract()
    with pytest.raises(LegacyTransportationRouteDisabled):
        packaging.validate_evidence("2026-08-25")


def test_legacy_route_diagnostics_can_never_authorize_capital() -> None:
    for payload in (transportation_route_diagnostic("transportation_test"),):
        assert payload["route_status"] == "SUPERSEDED_FAIL_CLOSED"
        assert payload["production_promotion_eligible"] is False
        assert payload["production_activation_authorized"] is False
        assert payload["portfolio_allocation_authorized"] is False
        assert payload["canonical_requirements"]


def test_transportation_adapter_cli_audits_zero_cap_inactive_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "industrials/transportation/scripts/"
        "32_validate_transportation_portfolio_adapter_production.py"
    )
    output = tmp_path / "inactive_route.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate.py",
            "--asof",
            "2026-08-25",
            "--output-json",
            str(output),
        ],
    )
    monkeypatch.setattr(
        module,
        "run_adapter",
        lambda *_args, **_kwargs: pytest.fail(
            "inactive-route audit must not consume production rows"
        ),
    )
    assert module.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["acceptance"] == "PASS"
    assert payload["acceptance_scope"] == "INACTIVE_ROUTE_SAFETY_ONLY"
    assert payload["sector_weight_cap"] == 0.0
    assert payload["source"]["enabled"] is False
    assert payload["source"]["required"] is False
    assert payload["production_activation_authorized"] is False
    assert payload["portfolio_allocation_authorized"] is False
