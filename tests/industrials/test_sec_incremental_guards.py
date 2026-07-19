from __future__ import annotations

import csv
import importlib
import sqlite3
from pathlib import Path

import pytest


sec_sync = importlib.import_module("industrials.scripts.07_sync_industrials_sec_fundamentals")
financial_validator = importlib.import_module("industrials.scripts.08_validate_industrials_financial_stage")


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


def test_targeted_sec_report_preserves_unselected_tickers(tmp_path: Path) -> None:
    path = tmp_path / "coverage.csv"
    original = [
        {field: "" for field in sec_sync.REPORT_FIELDS} | {"ticker": "AAA", "status": "success"},
        {field: "" for field in sec_sync.REPORT_FIELDS} | {"ticker": "BBB", "status": "review"},
    ]
    sec_sync.write_report(path, original)
    replacement = [
        {field: "" for field in sec_sync.REPORT_FIELDS} | {"ticker": "BBB", "status": "success"},
    ]
    sec_sync.write_report(path, replacement, preserve_existing_tickers=True)

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["ticker"] for row in rows] == ["AAA", "BBB"]
    assert {row["ticker"]: row["status"] for row in rows} == {"AAA": "success", "BBB": "success"}


def test_sec_cache_atomic_write_retries_transient_file_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cache" / "filing.htm"
    real_replace = sec_sync.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("transient OneDrive lock")
        real_replace(source, destination)

    monkeypatch.setattr(sec_sync.os, "replace", flaky_replace)
    sec_sync.write_cache_atomic(target, "complete filing")

    assert attempts == 3
    assert target.read_text(encoding="utf-8") == "complete filing"
    assert list(target.parent.glob("*.tmp")) == []


def test_successful_sec_retry_resolves_prior_open_failure() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE data_quality_issues (
            stage TEXT,
            model_family TEXT,
            ticker TEXT,
            source_id TEXT,
            issue_type TEXT,
            resolution_status TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO data_quality_issues VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            sec_sync.RUN_TYPE,
            "machinery",
            "TEST",
            "sec_companyfacts",
            "sec_sync_failed",
            "open",
            "old",
        ),
    )

    resolved = sec_sync.resolve_successful_sync_issues(
        conn,
        ticker="TEST",
        model_family="machinery",
        source_id="sec_companyfacts",
    )

    assert resolved == 1
    status = conn.execute(
        "SELECT resolution_status FROM data_quality_issues"
    ).fetchone()[0]
    assert status == "resolved_by_successful_retry"


def test_financial_review_issue_parity_is_scoped_to_model_family() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE data_quality_issues (
            stage TEXT,
            model_family TEXT,
            ticker TEXT,
            issue_type TEXT,
            resolution_status TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO data_quality_issues VALUES (?, ?, ?, ?, ?)",
        [
            (financial_validator.FEATURE_STAGE, "defense", "LMT", "financial_feature_review", "open"),
            (financial_validator.FEATURE_STAGE, "defense", "OLDDEF", "financial_feature_review", "open"),
            (financial_validator.FEATURE_STAGE, "machinery", "XE", "financial_feature_review", "open"),
        ],
    )

    review, stale = financial_validator.load_open_financial_review_issue_tickers(
        conn,
        model_family="defense",
        universe=["LMT"],
    )

    assert review == {"LMT"}
    assert stale == ["OLDDEF"]
