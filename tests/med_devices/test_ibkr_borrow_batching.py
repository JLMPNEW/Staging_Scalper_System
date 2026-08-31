from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    path = PROJECT_ROOT / "med_devices" / "scripts" / "53_sync_med_device_ibkr_borrow.py"
    spec = importlib.util.spec_from_file_location("med_device_ibkr_borrow_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ibkr_borrow_requests_are_bounded_and_cancelled_by_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()

    class FakeContract:
        def __init__(self, symbol: str, exchange: str, currency: str) -> None:
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency

    class FakeIB:
        instance: "FakeIB | None" = None

        def __init__(self) -> None:
            type(self).instance = self
            self.active = 0
            self.max_active = 0
            self.sleeps: list[float] = []
            self.disconnected = False

        def connect(self, *args: Any, **kwargs: Any) -> None:
            del args
            assert kwargs["readonly"] is True

        def qualifyContracts(self, *contracts: FakeContract) -> list[FakeContract]:
            return list(contracts)

        def reqMktData(self, contract: FakeContract, **kwargs: Any) -> SimpleNamespace:
            del contract, kwargs
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            return SimpleNamespace(
                shortable=3.0,
                shortableShares=1000.0,
                feeRate=0.01,
                rebateRate=None,
                ticks=[],
            )

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)

        def cancelMktData(self, contract: FakeContract) -> None:
            del contract
            self.active -= 1

        def disconnect(self) -> None:
            self.disconnected = True

    fake_module = ModuleType("ib_insync")
    fake_module.IB = FakeIB  # type: ignore[attr-defined]
    fake_module.Stock = FakeContract  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)
    companies = [
        {"company_id": index, "ticker": f"T{index}"}
        for index in range(1, 6)
    ]
    config = {
        "ibkr_borrow_ingestion": {
            "batch_size": 2,
            "snapshot_timeout_sec": 4.0,
            "sleep_sec": 0.25,
        }
    }

    rows = module.fetch_ib_rows(
        companies,
        asof="2026-08-28",
        source_id="ibkr_borrow",
        config=config,
    )

    client = FakeIB.instance
    assert client is not None
    assert len(rows) == 5
    assert client.max_active == 2
    assert client.active == 0
    assert client.sleeps.count(4.0) == 3
    assert client.sleeps.count(0.25) == 2
    assert client.disconnected is True


@pytest.mark.parametrize("batch_size", [0, 91, "not-an-int"])
def test_ibkr_borrow_rejects_unsafe_batch_sizes(batch_size: object) -> None:
    module = _load()

    with pytest.raises(ValueError, match="batch_size"):
        module.ibkr_market_data_batch_size(
            {"ibkr_borrow_ingestion": {"batch_size": batch_size}}
        )
