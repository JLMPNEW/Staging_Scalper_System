from __future__ import annotations

import csv
from pathlib import Path
import runpy
import sqlite3

import yaml

from industrials.core.db import init_db


ROOT = Path(__file__).resolve().parents[2]
DEFENSE = ROOT / "industrials" / "defense"


def _rows(name: str) -> list[dict[str, str]]:
    with (DEFENSE / "system_csvs" / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _by_ticker(name: str) -> dict[str, dict[str, str]]:
    return {row["ticker"].strip().upper(): row for row in _rows(name)}


def test_issc_to_ia_transition_is_complete_and_date_effective() -> None:
    current = _by_ticker("defense_tickers.csv")
    assert "IA" in current
    assert "ISSC" not in current
    assert current["IA"]["cik"] == "836690"

    history = _by_ticker("defense_historical_membership.csv")
    assert history["ISSC"]["membership_end_date"] == "2026-08-17"
    assert history["ISSC"]["successor_ticker"] == "IA"
    assert history["ISSC"]["point_in_time_flag"] == "1"

    aliases = _rows("defense_ticker_aliases.csv")
    assert any(
        row["contract_ticker"] == "IA"
        and row["active_ticker"] == "IA"
        and row["predecessor_ticker"] == "ISSC"
        and row["effective_date"] == "2026-08-18"
        and row["verified_flag"] == "1"
        for row in aliases
    )

    listing = _by_ticker("defense_listing_dates.csv")
    assert listing["ISSC"]["last_eligible_date"] == "2026-08-17"
    assert listing["IA"]["first_eligible_date"] == "2026-08-18"


def test_ia_is_in_every_current_identity_contract() -> None:
    cohorts = yaml.safe_load((DEFENSE / "data" / "defense_cohorts.yaml").read_text(encoding="utf-8"))
    cohort_tickers = {
        str(ticker).strip().upper()
        for cohort in cohorts["cohorts"]
        for ticker in cohort["tickers"]
    }
    assert "IA" in cohort_tickers
    assert "ISSC" not in cohort_tickers

    positioning = _by_ticker("defense_positioning_overrides.csv")
    assert positioning["IA"]["cusip"] == "45769N105"
    assert positioning["IA"]["ibkr_ticker"] == "IA"

    policy = yaml.safe_load((DEFENSE / "data" / "defense_universe_policy.yaml").read_text(encoding="utf-8"))
    current = _by_ticker("defense_tickers.csv")
    assert len(current) == int(policy["expected_ticker_count"])

    publisher_source = (DEFENSE / "scripts" / "17_publish_defense_shadow_rank_table.py").read_text(
        encoding="utf-8"
    )
    current_membership_sql = publisher_source.split('if membership_mode == "current":', 1)[1].split(
        "    else:", 1
    )[0]
    assert "JOIN dim_universe_membership m" in current_membership_sql
    assert "LEFT JOIN dim_universe_membership m" not in current_membership_sql
    assert "m.is_current_member = 1" in current_membership_sql


def test_explicit_historical_transition_preserves_predecessor_data() -> None:
    namespace = runpy.run_path(str(DEFENSE / "scripts" / "01_load_defense_universe.py"))
    reset_stale = namespace["reset_stale_active_seed_entities"]
    now = "2026-08-29T00:00:00Z"

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url,
                subsector_scope, status, created_at, updated_at
            ) VALUES ('seed', 'stage_1', 'seed', 'csv', 'local', 'defense', 'active', ?, ?)
            """,
            (now, now),
        )
        for ticker in ("ISSC", "DROP"):
            company_id = conn.execute(
                """
                INSERT INTO dim_company(
                    ticker, company_name, universe_status, is_active,
                    first_seen_at, updated_at
                ) VALUES (?, ?, 'keep', 1, ?, ?)
                """,
                (ticker, ticker, now, now),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO dim_universe_membership(
                    company_id, ticker, model_family, membership_source_id,
                    membership_basis, start_date, membership_status,
                    is_current_member, point_in_time_flag, confidence,
                    created_at, updated_at
                ) VALUES (?, ?, 'defense', 'seed', 'current_source_of_truth',
                          '2010-01-04', 'active', 1, 1, 1.0, ?, ?)
                """,
                (company_id, ticker, now, now),
            )
            conn.execute(
                """
                INSERT INTO fact_price_ohlcv(
                    ticker, bar_date, source_id, close, adj_close,
                    is_adjusted, created_at, updated_at
                ) VALUES (?, '2026-08-17', 'seed', 20.0, 20.0, 1, ?, ?)
                """,
                (ticker, now, now),
            )

        removed = reset_stale(
            conn,
            model_family="defense",
            seed_source_id="seed",
            incoming_tickers={"IA"},
            preserved_historical_tickers={"ISSC"},
        )

        assert removed == 1
        assert conn.execute("SELECT COUNT(*) FROM dim_company WHERE ticker='ISSC'").fetchone()[0] == 1
        predecessor = conn.execute(
            "SELECT universe_status, is_active FROM dim_company WHERE ticker='ISSC'"
        ).fetchone()
        assert predecessor["universe_status"] == "historical_transition"
        assert predecessor["is_active"] == 0
        assert conn.execute("SELECT COUNT(*) FROM fact_price_ohlcv WHERE ticker='ISSC'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM dim_company WHERE ticker='DROP'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fact_price_ohlcv WHERE ticker='DROP'").fetchone()[0] == 0

        # Idempotent recovery after a partial prior load: the old current-seed
        # membership may already be gone while the company row is still active.
        conn.execute("DELETE FROM dim_universe_membership WHERE ticker='ISSC'")
        conn.execute(
            "UPDATE dim_company SET universe_status='keep', is_active=1 WHERE ticker='ISSC'"
        )
        removed_again = reset_stale(
            conn,
            model_family="defense",
            seed_source_id="seed",
            incoming_tickers={"IA"},
            preserved_historical_tickers={"ISSC"},
        )
        assert removed_again == 0
        recovered = conn.execute(
            "SELECT universe_status, is_active FROM dim_company WHERE ticker='ISSC'"
        ).fetchone()
        assert recovered["universe_status"] == "historical_transition"
        assert recovered["is_active"] == 0
