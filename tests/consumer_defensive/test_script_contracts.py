from __future__ import annotations

import importlib.util
import copy
import csv
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from consumer_defensive.core.config import load_config, load_yaml
from consumer_defensive.core.db import connect
from consumer_defensive.core.market_data import (
    MarketDataPolicy,
    ensure_stage3_schema,
    load_market_policy,
    price_coverage,
    selected_price_rows,
    write_csv,
    write_json,
)
from consumer_defensive.core.norgate_membership import _frame_values
from consumer_defensive.core.script_runtime import (
    assert_stage4_universe_ready,
    cache_only_environment,
    iso_date,
    parse_ticker_csv,
    require_date_window,
    require_known_tickers,
)
from consumer_defensive.core.stage4 import (
    DISCLOSURE_SOURCE,
    bootstrap_stage4,
    ensure_stage4_schema,
    validate_stage4,
)
from consumer_defensive.core.stage3_runtime import assert_stage2_ready
from consumer_defensive.core.universe import load_current_universe, load_policy
from consumer_defensive.core.yahoo_prices import load_yahoo_prices


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "consumer_defensive"
SCRIPTS = PACKAGE / "scripts"
CONFIG = PACKAGE / "config.yaml"
POLICY = PACKAGE / "data" / "consumer_defensive_universe_policy.yaml"
MARKET_POLICY = PACKAGE / "data" / "consumer_defensive_market_data_policy.yaml"
DISCLOSURE_TERMS = PACKAGE / "data" / "consumer_defensive_specialized_disclosure_terms.yaml"


def _load_preflight_module():
    path = SCRIPTS / "00a_audit_norgate_history_access.py"
    spec = importlib.util.spec_from_file_location("consumer_defensive_norgate_preflight", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _current_only_db(tmp_path: Path):
    bundle = load_config(CONFIG)
    policy = load_policy(POLICY)
    conn = connect(tmp_path / "current_only.sqlite")
    bootstrap_stage4(conn, bundle)
    load_current_universe(conn, policy)
    return bundle, conn


def test_all_consumer_defensive_scripts_import_and_expose_help() -> None:
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert len(scripts) == 25
    for script in scripts:
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, f"{script.name}: {completed.stderr}"
        assert "usage:" in completed.stdout.casefold(), script.name


def test_reviewed_disclosure_terminology_contract_is_versioned_and_narrowed() -> None:
    bundle = load_config(CONFIG)
    terms = load_yaml(DISCLOSURE_TERMS)
    expected_version = "consumer_defensive_disclosure_census_v3"
    assert bundle.payload["specialized_disclosure_census"]["parser_version"] == expected_version
    assert terms["parser_version"] == expected_version
    triggers = terms["metrics"]["active_representative_growth_pct"]
    assert triggers == ["active representatives", "active distributors"]
    assert "sales leaders" not in triggers


def test_date_and_ticker_cli_contracts_are_fail_closed() -> None:
    assert iso_date("2026-08-11") == "2026-08-11"
    for invalid in ("20260811", "2026-02-30", "not-a-date", ""):
        with pytest.raises(Exception):
            iso_date(invalid)
    require_date_window("2019-01-02", "2026-08-11")
    with pytest.raises(ValueError, match="start .* after end"):
        require_date_window("2026-08-11", "2019-01-02")
    assert parse_ticker_csv(" ko,PEP,ko, ,BF-B ") == ["KO", "PEP", "BF-B"]
    assert parse_ticker_csv("") is None


def test_cache_only_cli_scope_restores_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variable = "CONSUMER_DEFENSIVE_CACHE_ONLY"
    monkeypatch.delenv(variable, raising=False)
    with cache_only_environment(True):
        assert os.environ[variable] == "1"
    assert variable not in os.environ

    monkeypatch.setenv(variable, "externally-managed")
    with cache_only_environment(True):
        assert os.environ[variable] == "1"
    assert os.environ[variable] == "externally-managed"


@pytest.mark.parametrize(
    "script_name",
    [
        "03_sync_consumer_defensive_adjusted_prices.py",
        "03c_reconcile_consumer_defensive_terminal_events.py",
    ],
)
def test_combined_price_entry_points_expose_cache_only_and_reject_force_refresh(
    script_name: str,
) -> None:
    help_result = subprocess.run(
        [sys.executable, str(SCRIPTS / script_name), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert help_result.returncode == 0
    assert "--cache-only" in help_result.stdout

    invalid = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / script_name),
            "--cache-only",
            "--force-refresh",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert invalid.returncode != 0
    assert "mutually exclusive" in invalid.stderr


def test_binary_provider_series_reject_fractional_null_and_non_numeric_values() -> None:
    index = pd.date_range("2024-01-02", periods=3, freq="D")
    dates, flags = _frame_values(pd.DataFrame({"flag": [0, 1, 0]}, index=index))
    assert len(dates) == 3
    assert flags == [0, 1, 0]
    for values in ([0, 0.5, 1], [0, None, 1], [0, "bad", 1]):
        with pytest.raises(ValueError, match="non-binary"):
            _frame_values(pd.DataFrame({"flag": values}, index=index))

    preflight = _load_preflight_module()
    valid, observed = preflight.binary_values(pd.DataFrame({"flag": [0, 0.5, 1]}, index=index))
    assert valid is False
    assert 0.5 in observed


def test_preflight_enforces_recognized_membership_when_policy_requires_it() -> None:
    preflight = _load_preflight_module()
    base = {
        "candidate_failures": 0,
        "asset_collisions": 0,
        "membership_failures": 0,
        "full_watchlists_scanned": False,
        "full_watchlist_failures": 0,
    }
    assert (
        preflight.overall_access_status(
            **base,
            candidate_nonmembers=1,
            recognized_membership_required=True,
        )
        == "FAIL"
    )
    assert (
        preflight.overall_access_status(
            **base,
            candidate_nonmembers=1,
            recognized_membership_required=False,
        )
        == "PASS"
    )


def test_targeted_scope_and_stage4_readiness_cannot_silently_noop(tmp_path: Path) -> None:
    bundle, conn = _current_only_db(tmp_path)
    try:
        assert require_known_tickers(conn, ["KO"]) == ["KO"]
        with pytest.raises(ValueError, match="NOT_A_TICKER"):
            require_known_tickers(conn, ["KO", "NOT_A_TICKER"])
        with pytest.raises(RuntimeError, match="complete current/historical taxonomy"):
            assert_stage4_universe_ready(conn, bundle)
    finally:
        conn.close()


def test_stage3_readiness_rejects_a_token_pit_membership_row(tmp_path: Path) -> None:
    bundle, conn = _current_only_db(tmp_path)
    try:
        security = conn.execute(
            """SELECT s.company_id,s.security_id
               FROM dim_security s WHERE s.ticker='KO' AND s.listing_status='active'"""
        ).fetchone()
        assert security is not None
        now = "2026-08-11T00:00:00Z"
        with conn:
            conn.execute(
                """INSERT INTO dim_universe_membership(
                       company_id,security_id,ticker,model_family,
                       membership_source_id,membership_basis,recognized_vehicle,
                       start_date,end_date,membership_status,is_current_member,
                       point_in_time_flag,live_investable_flag,
                       historical_calibration_eligible_flag,confidence,reason,
                       created_at,updated_at
                   ) VALUES(?,?,'KO','consumer_defensive',
                       'norgate_us_equities_pit_membership',
                       'recognized_index_union','approved_index_union',
                       '2017-11-28','2026-08-11','active',1,1,1,1,1.0,
                       'deliberately incomplete Stage 2 fixture',?,?)""",
                (int(security["company_id"]), int(security["security_id"]), now, now),
            )
        assert conn.execute(
            """SELECT COUNT(*) FROM dim_universe_membership
               WHERE membership_source_id='norgate_us_equities_pit_membership'"""
        ).fetchone()[0] == 1
        with pytest.raises(RuntimeError, match="recognized current membership"):
            assert_stage2_ready(conn, bundle)
    finally:
        conn.close()


def test_current_universe_reload_removes_stale_taxonomy_members(tmp_path: Path) -> None:
    bundle = load_config(CONFIG)
    policy = load_policy(POLICY)
    conn = connect(tmp_path / "reload.sqlite")
    source_path = policy.resolve("authoritative_current_csv")
    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0])
    old_ticker = str(rows[0]["ticker"])
    replacement = "ZZZZ"
    assert replacement not in {str(row["ticker"]) for row in rows}
    rows[0]["ticker"] = replacement
    rows[0]["company_name"] = "Consumer Defensive Reload Test"
    modified = tmp_path / "modified_universe.csv"
    with modified.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    try:
        bootstrap_stage4(conn, bundle)
        first = load_current_universe(conn, policy)
        assert first["stale_taxonomy_rows_removed"] == 0
        second = load_current_universe(conn, policy, modified)
        assert second["stale_taxonomy_rows_removed"] == 1
        taxonomy = {
            str(row[0])
            for row in conn.execute(
                "SELECT ticker FROM dim_consumer_defensive_taxonomy WHERE model_family='consumer_defensive'"
            )
        }
        assert len(taxonomy) == 108
        assert replacement in taxonomy
        assert old_ticker not in taxonomy
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_security WHERE ticker=? AND listing_status='active'",
            (old_ticker,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT listing_status FROM dim_security WHERE ticker=?",
            (old_ticker,),
        ).fetchone()[0] == "superseded"
        assert conn.execute(
            "SELECT is_active FROM dim_company WHERE primary_ticker=?",
            (old_ticker,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_stage4_builder_records_failed_prerequisite_instead_of_zero_work_success(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite"
    output_dir = tmp_path / "output"
    script = SCRIPTS / "08_build_consumer_defensive_financial_features.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--config",
            str(CONFIG),
            "--db",
            str(database),
            "--as-of",
            "2024-12-31",
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode != 0
    with sqlite3.connect(database) as conn:
        row = conn.execute(
            "SELECT status, row_count, message FROM runs WHERE run_type=? ORDER BY run_id DESC LIMIT 1",
            ("consumer_defensive_stage4_financial_features",),
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] == 0
    assert "complete current/historical taxonomy" in row[2]


def test_stage2_validator_records_and_reports_a_successful_identity_gate(tmp_path: Path) -> None:
    _, conn = _current_only_db(tmp_path)
    database = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    report = tmp_path / "stage2_validation.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "02_validate_consumer_defensive_universe.py"),
            "--config",
            str(CONFIG),
            "--policy",
            str(POLICY),
            "--db",
            str(database),
            "--identity-only",
            "--as-of",
            "2026-08-11",
            "--report",
            str(report),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert report.exists()
    with sqlite3.connect(database) as raw:
        row = raw.execute(
            "SELECT status,row_count FROM runs WHERE run_type='consumer_defensive_stage2_validation' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    assert row == ("success", 108)


def test_yahoo_worker_errors_are_reported_and_ingestion_run_is_closed(tmp_path: Path) -> None:
    _, conn = _current_only_db(tmp_path)
    source_policy = load_market_policy(MARKET_POLICY)
    payload = copy.deepcopy(source_policy.payload)
    payload["yahoo"]["cache_dir"] = str(tmp_path / "yahoo_cache")
    policy = MarketDataPolicy(source_policy.path, payload)

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated worker failure")

    try:
        result = load_yahoo_prices(
            conn,
            policy,
            start="2024-01-02",
            end="2024-01-05",
            tickers=["KO"],
            force_refresh=True,
            fetcher=explode,
        )
        assert result["tickers_requested"] == 3  # KO plus mandatory XLP/SPY benchmarks
        assert result["tickers_loaded"] == 0
        assert len(result["failures"]) == 3
        run = conn.execute(
            "SELECT status,request_count,row_count FROM ingestion_runs ORDER BY ingestion_run_id DESC LIMIT 1"
        ).fetchone()
        assert tuple(run) == ("partial", 3, 0)
    finally:
        conn.close()


def test_market_coverage_and_selection_are_strictly_point_in_time(tmp_path: Path) -> None:
    _, conn = _current_only_db(tmp_path)
    try:
        ensure_stage3_schema(conn)
        with conn:
            conn.execute(
                """INSERT INTO fact_price_ohlcv(
                       ticker,bar_date,source_id,close,adjusted_close,volume,total_return_basis,
                       source_timestamp,created_at
                   ) VALUES('KO','2026-01-05','yahoo_finance_adjusted',100,100,1000,
                            'yahoo_adjusted_close','2026-01-05T22:00:00Z','2026-01-05T22:00:00Z')"""
            )
            conn.execute(
                """INSERT INTO dim_price_series_selection(
                       ticker,purpose,selected_source_id,selection_asof_date,first_bar_date,last_bar_date,
                       bar_count,adjustment_basis,selection_reason,expected_start_date,expected_end_date,
                       coverage_status,created_at,updated_at
                   ) VALUES('KO','scoring_return_series','yahoo_finance_adjusted','2026-01-05',
                            '2026-01-05','2026-01-05',1,'yahoo_adjusted_close','test',
                            '2017-11-28','2026-01-05','complete','2026-01-05T22:00:00Z','2026-01-05T22:00:00Z')"""
            )
        assert price_coverage(conn, "KO", "yahoo_finance_adjusted")["rows"] == 1
        historical = price_coverage(
            conn,
            "KO",
            "yahoo_finance_adjusted",
            start="2017-11-28",
            end="2019-01-02",
        )
        assert historical == {"first": "", "last": "", "rows": 0, "invalid_adjusted": 0}
        source, rows = selected_price_rows(conn, "KO", as_of="2019-01-02")
        assert source == "" and rows == []
    finally:
        conn.close()


def test_stage4_validation_counts_only_the_configured_parser_version(tmp_path: Path) -> None:
    bundle = load_config(CONFIG)
    conn = connect(tmp_path / "parser_versions.sqlite")
    try:
        bootstrap_stage4(conn, bundle)
        load_current_universe(conn, load_policy(POLICY))
        metric_id = str(conn.execute("SELECT metric_id FROM dim_specialized_metric ORDER BY metric_id LIMIT 1").fetchone()[0])
        current_parser = str(bundle.payload["specialized_disclosure_census"]["parser_version"])
        now = "2026-08-11T00:00:00Z"
        rows = [
            ("KO", metric_id, "beverages", "all_operating_issuers", "2026-08-11", "not_applicable", 0, 0, "not_applicable", None, None, parser, DISCLOSURE_SOURCE, now)
            for parser in (current_parser, "retired_parser_v0")
        ]
        with conn:
            conn.executemany(
                "INSERT INTO fact_specialized_metric_disclosure_summary VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        result = validate_stage4(conn, bundle, as_of="2026-08-11")
        assert result["counts"]["census_summary"] == 1
    finally:
        conn.close()


def test_legacy_disclosure_summary_schema_migrates_without_losing_rows(tmp_path: Path) -> None:
    bundle = load_config(CONFIG)
    conn = connect(tmp_path / "legacy_census.sqlite")
    try:
        bootstrap_stage4(conn, bundle)
        metric_id = str(conn.execute("SELECT metric_id FROM dim_specialized_metric ORDER BY metric_id LIMIT 1").fetchone()[0])
        with conn:
            conn.execute("DROP TABLE fact_specialized_metric_disclosure_summary")
            conn.execute(
                """CREATE TABLE fact_specialized_metric_disclosure_summary (
                       ticker TEXT NOT NULL, metric_id TEXT NOT NULL,
                       calibration_cohort_id TEXT NOT NULL, applicability_subtype TEXT NOT NULL,
                       applicability_status TEXT NOT NULL, filings_searched INTEGER NOT NULL,
                       filings_with_hits INTEGER NOT NULL, disclosure_status TEXT NOT NULL,
                       first_disclosure_accepted_at TEXT, last_disclosure_accepted_at TEXT,
                       parser_version TEXT NOT NULL, source_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                       PRIMARY KEY(ticker, metric_id, parser_version)
                   )"""
            )
            conn.execute(
                "INSERT INTO fact_specialized_metric_disclosure_summary VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("KO", metric_id, "beverages", "non_alcohol", "applicable", 1, 1, "applicable_and_disclosed", None, None, "legacy_v0", DISCLOSURE_SOURCE, "2025-04-03T12:00:00Z"),
            )
        ensure_stage4_schema(conn)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(fact_specialized_metric_disclosure_summary)")}
        assert "asof_date" in columns
        row = conn.execute(
            "SELECT ticker,asof_date,parser_version FROM fact_specialized_metric_disclosure_summary"
        ).fetchone()
        assert tuple(row) == ("KO", "2025-04-03", "legacy_v0")
        primary_key = [
            str(row[1])
            for row in sorted(
                (row for row in conn.execute("PRAGMA table_info(fact_specialized_metric_disclosure_summary)") if int(row[5]) > 0),
                key=lambda row: int(row[5]),
            )
        ]
        assert primary_key == ["ticker", "metric_id", "parser_version", "asof_date"]
    finally:
        conn.close()


def test_artifact_writers_replace_atomically_and_leave_no_temporary_files(tmp_path: Path) -> None:
    json_path = tmp_path / "result.json"
    csv_path = tmp_path / "result.csv"
    write_json(json_path, {"status": "first"})
    write_json(json_path, {"status": "second"})
    write_csv(csv_path, [{"status": "PASS", "count": 1}])
    assert '"second"' in json_path.read_text(encoding="utf-8")
    assert "PASS" in csv_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.tmp"))


def test_artifact_writer_does_not_clobber_precreated_temp_hardlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path / 'outside.txt'
    outside.write_text('last-good', encoding='utf-8')
    legacy_temp = tmp_path / '.result.json.tmp'
    try:
        os.link(outside, legacy_temp)
    except (OSError, NotImplementedError):
        pytest.skip('hardlinks unavailable')
    result = tmp_path / 'result.json'
    write_json(result, {'status': 'new'})
    assert outside.read_text(encoding='utf-8') == 'last-good'
    assert legacy_temp.read_text(encoding='utf-8') == 'last-good'
    assert json.loads(result.read_text(encoding='utf-8')) == {'status': 'new'}
