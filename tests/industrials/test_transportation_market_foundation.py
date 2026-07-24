from __future__ import annotations

import csv
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from industrials.core.config import family_config, load_yaml
from industrials.core.db import connect, init_db, utc_now
from industrials.core.family_universe import load_active_universe, load_historical_and_delisted
from industrials.core.source_registry import load_source_registry, upsert_source_registry
from industrials.transportation.scripts import _shared


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDUSTRIALS_ROOT = PROJECT_ROOT / "industrials"
CONFIG_PATH = INDUSTRIALS_ROOT / "config.yaml"
SYSTEM_CSVS = INDUSTRIALS_ROOT / "transportation" / "system_csvs"


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_script(name: str):
    path = INDUSTRIALS_ROOT / "transportation" / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"transportation_{name.replace('.', '_')}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_scratch_foundation(db_path: Path) -> None:
    config = load_yaml(CONFIG_PATH)
    family = family_config(config, "transportation")
    universe = family["universe"]
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(INDUSTRIALS_ROOT / config["source_registry"]["path"]))
        load_active_universe(
            conn,
            active_path=INDUSTRIALS_ROOT / universe["seed_csv"],
            delisted_path=INDUSTRIALS_ROOT / universe["delisted_seed_csv"],
            listing_path=INDUSTRIALS_ROOT / universe["listing_dates_csv"],
            cohort_path=INDUSTRIALS_ROOT / universe["cohort_path"],
            policy_path=INDUSTRIALS_ROOT / universe["policy_path"],
            model_family="transportation",
            seed_source_id=universe["seed_source_id"],
            cohort_source_id=universe["cohort_source_id"],
        )
        load_historical_and_delisted(
            conn,
            historical_path=INDUSTRIALS_ROOT / universe["historical_membership_csv"],
            delisted_path=INDUSTRIALS_ROOT / universe["delisted_seed_csv"],
            cohort_path=INDUSTRIALS_ROOT / universe["cohort_path"],
            model_family="transportation",
            historical_source_id=universe["historical_membership_source_id"],
            delisted_source_id=universe["delisted_source_id"],
            default_start_date=universe["delisted_default_start_date"],
        )


def test_norgate_identity_contract_is_reviewed_and_fail_closed() -> None:
    mapping = rows(SYSTEM_CSVS / "transportation_norgate_symbol_map.csv")
    history = rows(SYSTEM_CSVS / "transportation_historical_membership.csv")
    assert len(mapping) == 160
    assert sum(row["calibration_usable_flag"] == "1" for row in mapping) == 158
    assert not [row for row in mapping if row["review_status"] == "review_required"]
    excluded = {row["actual_ticker"]: row for row in mapping if row["calibration_usable_flag"] == "0"}
    assert set(excluded) == {"CGI", "RRTS"}
    assert {row["mapping_status"] for row in excluded.values()} == {"verified_excluded"}
    active_history = [row for row in history if row["membership_status"] == "active"]
    delisted_history = [row for row in history if row["membership_status"] == "delisted"]
    assert len(active_history) == 112
    assert len(delisted_history) == 46
    mapped_ends = {
        row["internal_ticker"]: row["last_quoted_date"]
        for row in mapping
        if row["calibration_usable_flag"] == "1" and row["exit_year"]
    }
    assert {row["internal_ticker"]: row["end_date"] for row in delisted_history} == mapped_ends


def test_delisted_loader_uses_exact_dates_and_retains_excluded_seeds(tmp_path: Path) -> None:
    db_path = tmp_path / "transportation.sqlite"
    load_scratch_foundation(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_delisted_calibration_seed WHERE model_family='transportation'"
        ).fetchone()[0] == 48
        exact = conn.execute(
            """
            SELECT COUNT(*) FROM dim_universe_membership
            WHERE model_family='transportation'
              AND membership_source_id='transportation_delisted_calibration_seed'
              AND reason LIKE '%Norgate-resolved final quoted date%'
            """
        ).fetchone()[0]
        provisional = conn.execute(
            """
            SELECT ticker FROM dim_universe_membership
            WHERE model_family='transportation'
              AND membership_source_id='transportation_delisted_calibration_seed'
              AND reason LIKE '%excluded from calibration%'
            ORDER BY ticker
            """
        ).fetchall()
    assert exact == 46
    assert [row[0] for row in provisional] == ["CGI", "RRTS"]


def test_norgate_importer_only_loads_calibration_usable_delisted_members() -> None:
    module = load_script("15_import_transportation_norgate_delisted_prices.py")
    members = module.load_members(
        SYSTEM_CSVS / "transportation_norgate_symbol_map.csv",
        SYSTEM_CSVS / "transportation_historical_membership.csv",
    )
    assert len(members) == 46
    assert {member.actual_ticker for member in members}.isdisjoint({"CGI", "RRTS"})


def test_market_wrappers_pin_family_benchmarks_policy_and_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(path: str, *, run_name: str) -> None:
        captured["path"] = path
        captured["run_name"] = run_name
        captured["argv"] = list(sys.argv)

    monkeypatch.setattr(_shared.runpy, "run_path", fake_run)
    monkeypatch.setattr(sys, "argv", ["wrapper.py", "--asof", "2026-07-17"])
    _shared.run_market_shared("05_build_industrials_market_features.py")
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--model-family") + 1] == "transportation"
    assert argv[argv.index("--benchmark-tickers") + 1] == "IYT,XTN,SPY"
    assert argv[argv.index("--primary-benchmark") + 1] == "IYT"
    assert "output\\industrials\\transportation\\stage3" in argv[argv.index("--output-csv") + 1]
    monkeypatch.setattr(sys, "argv", ["wrapper.py", "--model-family=defense"])
    with pytest.raises(ValueError, match="pinned"):
        _shared.run_market_shared("03_sync_industrials_yahoo_adjusted_prices.py")


def test_shared_yahoo_loader_uses_family_membership_not_global_company_activity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "transportation.sqlite"
    load_scratch_foundation(db_path)
    module_path = INDUSTRIALS_ROOT / "scripts" / "03_sync_industrials_yahoo_adjusted_prices.py"
    spec = importlib.util.spec_from_file_location("shared_yahoo_membership_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with connect(db_path) as conn:
        conn.execute("UPDATE dim_company SET is_active=1 WHERE ticker='FLY'")
        jobs = module.load_universe_jobs(
            conn,
            model_family="transportation",
            ticker_filter=set(),
            max_tickers=0,
            asof=module.date(2026, 7, 22),
        )
    tickers = {job.ticker for job in jobs}
    assert len(tickers) == 112
    assert "FLY" not in tickers


def test_portfolio_layer_has_optional_transportation_shadow_source() -> None:
    config = load_yaml(PROJECT_ROOT / "portfolio_layer" / "config.yaml")
    sources = config["score_contract"]["sectors"]
    source = next(row for row in sources if row["model_family"] == "transportation")
    assert source["adapter"] == "industrial_family"
    assert source["enabled"] is True
    assert source["required"] is False
    assert source["require_oos_score_valid"] is True


def test_delisted_export_contract_on_scratch_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "transportation.sqlite"
    output_dir = tmp_path / "exports"
    load_scratch_foundation(db_path)
    now = utc_now()
    with connect(db_path) as conn:
        members = conn.execute(
            """
            SELECT ticker, end_date FROM dim_universe_membership
            WHERE model_family='transportation'
              AND membership_source_id='transportation_historical_membership_seed'
              AND membership_status='delisted'
            ORDER BY ticker
            """
        ).fetchall()
        assert len(members) == 46
        for member in members:
            conn.execute(
                """
                INSERT INTO fact_price_ohlcv(
                    ticker, bar_date, source_id, close, adj_close,
                    price_adjustment, is_adjusted, created_at, updated_at
                ) VALUES (?, ?, 'norgate_us_equities_total_return', 10.0, 10.0,
                          'synthetic_test', 1, ?, ?)
                """,
                (member["ticker"], member["end_date"], now, now),
            )
    module = load_script("28_export_transportation_delisted_price_contract.py")
    monkeypatch.setattr(
        sys,
        "argv",
        ["export.py", "--db", str(db_path), "--output-dir", str(output_dir)],
    )
    assert module.main() == 0
    assert len(rows(output_dir / "transportation_delisted_price_export.csv")) == 46
    events = rows(output_dir / "transportation_delisting_events.csv")
    assert len(events) == 46
    assert {row["ticker"] for row in events}.isdisjoint({"CGI", "RRTS"})
    assert all(row["delist_date"] for row in events)
