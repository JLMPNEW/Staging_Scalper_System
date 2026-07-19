from __future__ import annotations

import csv
import os
import sqlite3
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
