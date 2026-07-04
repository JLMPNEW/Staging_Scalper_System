from __future__ import annotations

import importlib

import pytest


sec_sync = importlib.import_module("industrials.scripts.07_sync_industrials_sec_fundamentals")


def test_incremental_new_filing_forces_companyfacts_refetch() -> None:
    new_keys = {("10-Q", "2026-06-30", "0000000000-26-000001")}

    assert not sec_sync.should_skip_incremental_companyfacts(
        incremental=True,
        new_filing_keys=new_keys,
        force_companyfacts=False,
        force_archive=False,
        prior_sync_failed=False,
        has_existing_state=True,
    )
    assert sec_sync.should_force_companyfacts_payload_fetch(incremental=True, force_companyfacts=False)


def test_incremental_can_skip_only_when_current_and_unforced() -> None:
    assert sec_sync.should_skip_incremental_companyfacts(
        incremental=True,
        new_filing_keys=set(),
        force_companyfacts=False,
        force_archive=False,
        prior_sync_failed=False,
        has_existing_state=True,
    )
    assert not sec_sync.should_skip_incremental_companyfacts(
        incremental=True,
        new_filing_keys=set(),
        force_companyfacts=False,
        force_archive=False,
        prior_sync_failed=True,
        has_existing_state=True,
    )
    assert not sec_sync.should_skip_incremental_companyfacts(
        incremental=True,
        new_filing_keys=set(),
        force_companyfacts=True,
        force_archive=False,
        prior_sync_failed=False,
        has_existing_state=True,
    )


def test_sec_user_agent_expands_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INDUSTRIALS_SEC_USER_AGENT", raising=False)
    config = {
        "sec_fundamentals": {
            "user_agent": "${INDUSTRIALS_SEC_USER_AGENT:-JL, Independent Research, jm.357@hotmail.com}"
        }
    }

    assert sec_sync.resolve_sec_user_agent(config) == "JL, Independent Research, jm.357@hotmail.com"


def test_sec_user_agent_rejects_unresolved_template_with_email_tripwire() -> None:
    config = {"sec_fundamentals": {"user_agent": "${BROKEN_TEMPLATE:-still_has_email@example.com"}}

    with pytest.raises(ValueError, match="must resolve"):
        sec_sync.resolve_sec_user_agent(config)
