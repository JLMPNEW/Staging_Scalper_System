from __future__ import annotations

import csv
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from biotech_index.core.pipeline_guards import (
    read_final_scoring_tickers,
    validate_full_universe_coverage,
    validate_layer_freshness,
    validate_output_coverage,
)
from tests.biotech.conftest import load_script_module


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_quality_gate_honors_writable_pytest_temp_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script_module("00_run_biotech_quality_gate.py", "biotech_quality_gate_temp_regression")
    exact_path = tmp_path / "quality_gate" / "pytest_base"
    monkeypatch.setenv("BIOTECH_PYTEST_TMP", str(exact_path))

    assert module.resolve_pytest_temp_dir() == exact_path.resolve()


def test_report_qa_uses_report_form4_provenance_not_arbitrary_state_row(tmp_path: Path) -> None:
    module = load_script_module("42_audit_biotech_daily_report_quality.py", "report_form4_state_regression")
    form4_db = tmp_path / "STAGING" / "sec_insider.sqlite"
    form4_db.parent.mkdir(parents=True)
    with sqlite3.connect(form4_db) as conn:
        conn.execute("CREATE TABLE sec_form4_daily_state(process_name TEXT, last_index_date TEXT)")
        conn.executemany(
            "INSERT INTO sec_form4_daily_state VALUES (?, ?)",
            [("old", "2026-06-05"), ("current", "2026-07-09")],
        )

    state = module.load_form4_staging_state(
        tmp_path / "missing_biotech.sqlite",
        {"governance_events": {"form4_db_path": str(form4_db)}},
        report_rows=[{"insider_data_asof_date": "2026-07-07"}],
    )

    assert state["form4_snapshot_date"] == "2026-07-07"
    assert state["form4_snapshot_source"] == "biotech_daily_scores.insider_data_asof_date"
    assert state["form4_db_latest_snapshot_date"] == "2026-07-09"


def test_final_scoring_tickers_exclude_manual_removed_tickers() -> None:
    universe_csv = Path(__file__).with_name("_tmp_final_scoring_universe.csv")
    try:
        write_csv(
            universe_csv,
            [
                {"ticker": "AAA", "scoring_include": "true", "decision": "keep"},
                {"ticker": "GLPG", "scoring_include": "false", "decision": "remove"},
                {"ticker": "TERN", "scoring_include": "false", "decision": "remove"},
            ],
            ["ticker", "scoring_include", "decision"],
        )

        tickers = read_final_scoring_tickers(universe_csv)

        assert tickers == {"AAA"}
        assert {"GLPG", "TERN"}.isdisjoint(tickers)
    finally:
        universe_csv.unlink(missing_ok=True)


def test_output_coverage_fails_when_scores_do_not_match_universe() -> None:
    expected = {"AAA", "BBB"}

    with pytest.raises(RuntimeError, match="output missing 1 final-universe ticker"):
        validate_output_coverage(
            expected_tickers=expected,
            output_tickers=["AAA"],
            context="score output",
            subset_mode=False,
        )


def test_full_universe_coverage_allows_subset_without_hiding_requested_validation() -> None:
    coverage = validate_full_universe_coverage(
        expected_tickers={"AAA", "BBB"},
        observed_tickers=["AAA"],
        context="subset smoke",
        subset_mode=True,
    )

    assert coverage.missing_tickers == ("BBB",)


def test_layer_freshness_catches_stale_missing_and_future_rows() -> None:
    base_rows = [
        {"company_id": 1, "ticker": "AAA"},
        {"company_id": 2, "ticker": "BBB"},
        {"company_id": 3, "ticker": "CCC"},
    ]
    layer_rows = {
        1: {"asof_date": "2026-05-06"},
        3: {"asof_date": "2026-05-09"},
    }

    with pytest.raises(RuntimeError) as exc_info:
        validate_layer_freshness(
            base_rows=base_rows,
            layer_rows_by_company=layer_rows,
            asof_date="2026-05-08",
            context="layer invariant",
            max_staleness_days=0,
        )

    message = str(exc_info.value)
    assert "stale 1 ticker" in message
    assert "missing 1 ticker" in message
    assert "future-dated 1 ticker" in message


def test_final_validation_table_coverage_rejects_extra_score_ticker() -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_validation_regression")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE companies(company_id INTEGER PRIMARY KEY, ticker TEXT NOT NULL);
        CREATE TABLE daily_scores(asof_date TEXT NOT NULL, company_id INTEGER NOT NULL);
        INSERT INTO companies(company_id, ticker) VALUES (1, 'AAA'), (2, 'ZZZ');
        INSERT INTO daily_scores(asof_date, company_id) VALUES ('2026-05-08', 1), ('2026-05-08', 2);
        """
    )

    with pytest.raises(RuntimeError, match="extra 1"):
        module.validate_table_coverage(
            conn,
            table="daily_scores",
            asof="2026-05-08",
            expected_tickers={"AAA"},
        )


def test_final_validation_market_snapshot_uses_latest_nonfuture_trading_date() -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_market_snapshot_regression")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE market_features_daily(
            asof_date TEXT NOT NULL,
            source TEXT NOT NULL,
            last_bar_date TEXT
        );
        INSERT INTO market_features_daily VALUES ('2026-08-21', 'yahoo_adjusted', '2026-08-21');
        INSERT INTO market_features_daily VALUES ('2026-08-23', 'yahoo_adjusted', '2026-08-23');
        """
    )

    snapshot = module.validated_market_feature_snapshot_asof(
        conn,
        source="yahoo_adjusted",
        report_asof="2026-08-22",
        max_lag_calendar_days=4,
    )

    assert snapshot == "2026-08-21"
    with pytest.raises(RuntimeError, match="maximum=0"):
        module.validated_market_feature_snapshot_asof(
            conn,
            source="yahoo_adjusted",
            report_asof="2026-08-22",
            max_lag_calendar_days=0,
        )
    conn.execute(
        "UPDATE market_features_daily SET last_bar_date='2026-08-10' WHERE asof_date='2026-08-21'"
    )
    with pytest.raises(RuntimeError, match="latest underlying bar"):
        module.validated_market_feature_snapshot_asof(
            conn,
            source="yahoo_adjusted",
            report_asof="2026-08-22",
            max_lag_calendar_days=4,
        )
    conn.execute(
        "UPDATE market_features_daily SET last_bar_date='2026-08-24' WHERE asof_date='2026-08-21'"
    )
    with pytest.raises(RuntimeError, match="-2 calendar day"):
        module.validated_market_feature_snapshot_asof(
            conn,
            source="yahoo_adjusted",
            report_asof="2026-08-22",
            max_lag_calendar_days=4,
        )


def test_ibkr_preflight_fallback_routes_only_market_prices_offline() -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_ibkr_offline_fallback")
    steps = module.pipeline_steps(
        "daily_delta",
        skip_ctgov=False,
        skip_ib=False,
        skip_yahoo=False,
        skip_market_positioning=False,
    )

    rewritten = module.apply_ibkr_market_fallback(
        steps,
        {"status": "warning", "failed_labels": ["ib_market_data"]},
    )

    original_by_name = {step.name: step for step in steps}
    rewritten_by_name = {step.name: step for step in rewritten}
    assert rewritten_by_name["ib_market"].args == (
        *original_by_name["ib_market"].args,
        "--offline-existing-bars",
    )
    assert rewritten_by_name["market_positioning"] == original_by_name["market_positioning"]
    assert (
        module.apply_ibkr_market_fallback(
            rewritten,
            {"status": "warning", "failed_labels": ["ib_market_data"]},
        )
        == rewritten
    )
    assert (
        module.apply_ibkr_market_fallback(
            steps,
            {"status": "warning", "failed_labels": ["market_positioning.ibkr_borrow"]},
        )
        == steps
    )
    assert module.apply_ibkr_market_fallback(
        steps,
        {"status": "success", "failed_labels": []},
    ) == steps


def test_weekly_reconcile_does_not_force_sec_event_full_rescan() -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_weekly_sec_events_args")

    def sec_event_args_for(mode: str) -> tuple[str, ...]:
        steps = module.pipeline_steps(
            mode,
            skip_ctgov=False,
            skip_ib=False,
            skip_yahoo=False,
            skip_market_positioning=False,
        )
        return next(step.args for step in steps if step.name == "sec_events")

    assert sec_event_args_for("daily_delta") == ("--skip-parser-signature-reparse",)
    assert sec_event_args_for("weekly_reconcile") == ("--skip-parser-signature-reparse",)
    assert sec_event_args_for("full_backfill") == ("--full-rescan",)


def test_historical_restatement_rebuilds_ctgov_before_scoring_universe() -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_historical_ctgov_order")

    names = [step.name for step in module.historical_restatement_steps()]

    assert names[:2] == ["ctgov_audit", "historical_scoring_universe"]


def test_historical_restatement_never_refetches_live_adcom_calendar() -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_historical_adcom_freeze")

    names = [step.name for step in module.historical_restatement_steps()]

    assert "fda_adcom_calendar" not in names
    assert "biotech_features" in names


def test_historical_sqlite_lock_retry_is_narrow_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_historical_lock_retry")
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_run_step(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(1)
        if len(calls) == 1:
            return {"status": "failed", "stderr_tail": "sqlite3.OperationalError: database is locked"}
        return {"status": "success", "elapsed_sec": 0.1}

    monkeypatch.setattr(module, "run_step", fake_run_step)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    result = module.run_step_with_sqlite_lock_retry(
        module.Step("test", "test.py"),
        command=["python", "test.py"],
        mode="history_restatement",
        run_started_at="2026-09-01T00:00:00Z",
        timeout_sec=60.0,
        max_retries=2,
        retry_delay_sec=3.0,
    )

    assert result["status"] == "success"
    assert result["retry_count"] == 1
    assert len(calls) == 2
    assert sleeps == [3.0]

    calls.clear()

    def fail_non_lock(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(1)
        return {"status": "failed", "stderr_tail": "ValueError: invalid score contract"}

    monkeypatch.setattr(module, "run_step", fail_non_lock)
    result = module.run_step_with_sqlite_lock_retry(
        module.Step("test", "test.py"),
        command=["python", "test.py"],
        mode="history_restatement",
        run_started_at="2026-09-01T00:00:00Z",
        timeout_sec=60.0,
        max_retries=2,
        retry_delay_sec=3.0,
    )

    assert result["status"] == "failed"
    assert result["retry_count"] == 0
    assert len(calls) == 1


def test_historical_market_steps_use_bounded_rolling_window() -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_historical_market_window")
    steps = {step.name: step for step in module.historical_restatement_steps(market_start_asof="2017-11-10")}

    adjusted = module.rolling_historical_market_step(
        steps["yahoo_market_adjusted"],
        run_asof="2026-08-31",
        floor_start_asof="2017-11-10",
        lookback_days=550,
    )
    start_index = adjusted.args.index("--start-date")

    assert adjusted.args[start_index + 1] == "2025-02-27"
    assert "--offline-existing-bars" in adjusted.args
    with pytest.raises(ValueError, match="at least 420"):
        module.rolling_historical_market_step(
            steps["yahoo_market_adjusted"],
            run_asof="2026-08-31",
            floor_start_asof="2017-11-10",
            lookback_days=365,
        )


def test_partial_historical_feature_restatement_requires_positioning_export() -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_historical_dependencies")

    with pytest.raises(ValueError, match="requires market_positioning_export"):
        module.validate_historical_step_selection({"financial_survival", "biotech_features"})

    module.validate_historical_step_selection(
        {"market_positioning_export", "financial_survival", "biotech_features"}
    )


def test_historical_norgate_routing_requires_calibration_only_member(tmp_path: Path) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_historical_norgate_routing")
    output_root = tmp_path / "reports"
    dated_dir = output_root / "20260710"
    dated_dir.mkdir(parents=True)
    universe_path = dated_dir / "ctgov_final_scoring_universe.csv"
    config = {"biotech_scoring": {"output_dir": str(output_root)}}

    universe_path.write_text("ticker,calibration_only\nAAA,false\n", encoding="utf-8")
    assert not module.historical_universe_requires_norgate(
        config,
        base_dir=tmp_path,
        asof="2026-07-10",
    )

    universe_path.write_text("ticker,calibration_only\nAAA,false\nOLD,true\n", encoding="utf-8")
    assert module.historical_universe_requires_norgate(
        config,
        base_dir=tmp_path,
        asof="2026-07-10",
    )


def test_snapshot_copies_only_current_run_outputs_and_never_prunes_shared_history(tmp_path: Path) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_snapshot_boundary_regression")
    source_dir = tmp_path / "biotech_reports"
    source_dir.mkdir()
    historical_dir = source_dir / "20190104"
    historical_dir.mkdir()
    (historical_dir / "biotech_daily_scores.csv").write_text("ticker\nAAA\n", encoding="utf-8")
    stale = source_dir / "old_calibration_audit.csv"
    stale.write_text("status\nold\n", encoding="utf-8")
    run_start = datetime.now(timezone.utc) - timedelta(seconds=2)
    old_timestamp = (run_start - timedelta(days=1)).timestamp()
    os.utime(stale, (old_timestamp, old_timestamp))
    fresh = source_dir / "biotech_daily_scores.csv"
    fresh.write_text("ticker\nAAA\n", encoding="utf-8")

    result = module.snapshot_direct_output_files(
        {
            "biotech_reports": {"output_dir": str(source_dir)},
            "biotech_refresh": {
                "max_snapshot_history": 1,
                "snapshot_outputs": {
                    "source_dir": str(source_dir),
                    "root_dir": str(source_dir),
                    "include_extensions": [".csv", ".json"],
                    "copy_only_refreshed_since_run_start": True,
                    "mtime_tolerance_sec": 0,
                    "prune_old_snapshot_dirs": True,
                },
            },
        },
        base_dir=tmp_path,
        asof="2026-07-07",
        run_started_at=run_start.isoformat(),
        mode="daily_delta",
        selected_steps=set(),
    )

    snapshot_dir = source_dir / "20260707"
    assert result["status"] == "success"
    assert (snapshot_dir / "biotech_daily_scores.csv").exists()
    assert not (snapshot_dir / "old_calibration_audit.csv").exists()
    assert historical_dir.exists()
    manifest = module.json.loads((snapshot_dir / "snapshot_manifest.json").read_text(encoding="utf-8"))
    assert manifest["skipped_stale_source_files"] == ["old_calibration_audit.csv"]


def test_form4_preflight_accepts_fresh_staging_copy(tmp_path: Path) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_form4_preflight_fresh")
    form4_db = tmp_path / "sec_insider.sqlite"
    conn = sqlite3.connect(form4_db)
    try:
        conn.execute("CREATE TABLE sec_form4_daily_state(last_index_date TEXT)")
        conn.execute("INSERT INTO sec_form4_daily_state(last_index_date) VALUES ('2026-05-08')")
        conn.commit()
    finally:
        conn.close()

    row = module.validate_form4_preflight(
        {
            "governance_events": {
                "form4_db_path": str(form4_db),
                "form4_snapshot_table": "sec_form4_daily_state",
            },
            "biotech_refresh": {"form4_preflight": {"max_staleness_days": 2}},
        },
        base_dir=tmp_path,
        asof="2026-05-10",
        run_started_at="2026-05-10T00:00:00+00:00",
        mode="daily_delta",
    )

    assert row["status"] == "success"
    assert "snapshot_date=2026-05-08" in row["command"]


def test_form4_preflight_rejects_stale_staging_copy(tmp_path: Path) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_form4_preflight_stale")
    form4_db = tmp_path / "sec_insider.sqlite"
    conn = sqlite3.connect(form4_db)
    try:
        conn.execute("CREATE TABLE sec_form4_daily_state(last_index_date TEXT)")
        conn.execute("INSERT INTO sec_form4_daily_state(last_index_date) VALUES ('2026-05-01')")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="Form 4 snapshot is stale"):
        module.validate_form4_preflight(
            {
                "governance_events": {
                    "form4_db_path": str(form4_db),
                    "form4_snapshot_table": "sec_form4_daily_state",
                },
                "biotech_refresh": {"form4_preflight": {"max_staleness_days": 2}},
            },
            base_dir=tmp_path,
            asof="2026-05-10",
            run_started_at="2026-05-10T00:00:00+00:00",
            mode="daily_delta",
        )


def test_form4_preflight_historical_run_ignores_future_database_rows(tmp_path: Path) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_form4_preflight_pit")
    form4_db = tmp_path / "sec_insider.sqlite"
    conn = sqlite3.connect(form4_db)
    try:
        conn.execute("CREATE TABLE sec_form4_daily_state(last_index_date TEXT)")
        conn.executemany(
            "INSERT INTO sec_form4_daily_state(last_index_date) VALUES (?)",
            [("2026-07-20",), ("2026-07-21",)],
        )
        conn.execute("CREATE TABLE sec_ownership_submission(filing_date TEXT)")
        conn.executemany(
            "INSERT INTO sec_ownership_submission(filing_date) VALUES (?)",
            [("20-JUL-2026",), ("21-JUL-2026",)],
        )
        conn.commit()
    finally:
        conn.close()

    row = module.validate_form4_preflight(
        {
            "governance_events": {
                "form4_db_path": str(form4_db),
                "form4_snapshot_table": "sec_form4_daily_state",
            },
            "biotech_refresh": {
                "form4_preflight": {
                    "max_staleness_days": 2,
                    "raw_filing_date_sources": ["sec_ownership_submission.filing_date"],
                }
            },
        },
        base_dir=tmp_path,
        asof="2026-07-20",
        run_started_at="2026-07-22T00:00:00+00:00",
        mode="daily_delta",
    )

    assert row["status"] == "success"
    assert "snapshot_date=2026-07-20" in row["command"]
    assert "raw_filing_date=20-JUL-2026" in row["command"]


def test_resume_marker_output_current_rejects_stale_universe_coverage(tmp_path: Path) -> None:
    module = load_script_module(
        "24_run_biotech_refresh_pipeline.py",
        "pipeline_resume_output_coverage_regression",
    )
    universe_csv = tmp_path / "universe.csv"
    write_csv(
        universe_csv,
        [
            {"ticker": "AAA", "scoring_include": "1"},
            {"ticker": "BBB", "scoring_include": "1"},
        ],
        ["ticker", "scoring_include"],
    )
    db_path = tmp_path / "biotech.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE companies(company_id INTEGER PRIMARY KEY, ticker TEXT)")
        conn.executemany(
            "INSERT INTO companies(company_id, ticker) VALUES (?, ?)",
            [(1, "AAA"), (2, "BBB")],
        )
        conn.execute(
            "CREATE TABLE financial_survival_features(asof_date TEXT, company_id INTEGER)"
        )
        conn.execute(
            "INSERT INTO financial_survival_features(asof_date, company_id) VALUES (?, ?)",
            ("2026-08-21", 1),
        )
        conn.commit()
    finally:
        conn.close()

    config = {
        "biotech_features": {"final_scoring_universe_csv": str(universe_csv)}
    }
    step = module.Step(
        "financial_survival",
        "16_build_financial_survival_features.py",
    )
    assert not module.step_marker_output_current(
        config=config,
        base_dir=tmp_path,
        db_path=db_path,
        step=step,
        asof="2026-08-21",
    )

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO financial_survival_features(asof_date, company_id) VALUES (?, ?)",
            ("2026-08-21", 2),
        )
        conn.commit()
    finally:
        conn.close()

    assert module.step_marker_output_current(
        config=config,
        base_dir=tmp_path,
        db_path=db_path,
        step=step,
        asof="2026-08-21",
    )

def test_resume_marker_uses_dated_universe_not_newer_root_membership(tmp_path: Path) -> None:
    module = load_script_module(
        "24_run_biotech_refresh_pipeline.py",
        "pipeline_resume_dated_universe_regression",
    )
    report_root = tmp_path / "reports"
    root_universe = report_root / "ctgov_final_scoring_universe.csv"
    dated_universe = report_root / "20260821" / root_universe.name
    dated_universe.parent.mkdir(parents=True)
    write_csv(
        root_universe,
        [
            {"ticker": "AAA", "scoring_include": "1"},
            {"ticker": "NEW", "scoring_include": "1"},
        ],
        ["ticker", "scoring_include"],
    )
    write_csv(
        dated_universe,
        [{"ticker": "AAA", "scoring_include": "1"}],
        ["ticker", "scoring_include"],
    )
    db_path = tmp_path / "biotech.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE companies(company_id INTEGER PRIMARY KEY, ticker TEXT)")
        conn.execute("INSERT INTO companies(company_id, ticker) VALUES (1, 'AAA')")
        conn.execute("CREATE TABLE financial_survival_features(asof_date TEXT, company_id INTEGER)")
        conn.execute(
            "INSERT INTO financial_survival_features(asof_date, company_id) VALUES (?, ?)",
            ("2026-08-21", 1),
        )

    assert module.step_marker_output_current(
        config={"biotech_features": {"final_scoring_universe_csv": str(root_universe)}},
        base_dir=tmp_path,
        db_path=db_path,
        step=module.Step("financial_survival", "16_build_financial_survival_features.py"),
        asof="2026-08-21",
    )

def test_explicit_weekend_asof_normalizes_to_prior_market_date(tmp_path: Path) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_weekend_asof_regression")

    normalized = module.normalize_requested_pipeline_asof(
        datetime(2026, 8, 22).date(),
        db_path=tmp_path / "missing.sqlite",
        config={},
    )

    assert normalized.isoformat() == "2026-08-21"


def test_explicit_xnys_session_is_not_backdated_by_missing_local_bars(tmp_path: Path) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_session_asof_regression")
    db_path = tmp_path / "stale.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE market_bars_daily(bar_date TEXT, source TEXT)")
        conn.execute(
            "INSERT INTO market_bars_daily(bar_date, source) VALUES ('2026-08-27', 'yahoo_adjusted')"
        )

    normalized = module.normalize_requested_pipeline_asof(
        datetime(2026, 8, 28).date(),
        db_path=db_path,
        config={},
    )

    assert normalized.isoformat() == "2026-08-28"


def test_explicit_weekday_exchange_holiday_uses_prior_xnys_session(tmp_path: Path) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_holiday_asof_regression")

    normalized = module.normalize_requested_pipeline_asof(
        datetime(2026, 12, 25).date(),
        db_path=tmp_path / "missing.sqlite",
        config={},
    )

    assert normalized.isoformat() == "2026-12-24"

def test_sec_filing_backfill_cap_override(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("06_sync_sec_filings.py", "sec_filing_backfill_cap_override")
    monkeypatch.setattr(
        sys,
        "argv",
        ["06_sync_sec_filings.py", "--max-filings-per-company", "500"],
    )

    args = module.parse_args()

    assert args.max_filings_per_company == 500

def test_sec_event_backfill_lookback_override(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("07_parse_sec_biotech_events.py", "sec_event_backfill_lookback_override")
    monkeypatch.setattr(
        sys,
        "argv",
        ["07_parse_sec_biotech_events.py", "--lookback-days", "3000"],
    )

    args = module.parse_args()

    assert args.lookback_days == 3000

def test_earliest_ctgov_snapshot_date_is_database_driven(tmp_path: Path) -> None:
    module = load_script_module("24_run_biotech_refresh_pipeline.py", "pipeline_ctgov_snapshot_floor")
    db_path = tmp_path / "ctgov.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE trial_snapshot_daily(asof_date TEXT)")
        conn.executemany(
            "INSERT INTO trial_snapshot_daily(asof_date) VALUES (?)",
            [("2026-05-02",), ("2026-04-19",)],
        )

    assert module.earliest_ctgov_snapshot_date(db_path) == "2026-04-19"
