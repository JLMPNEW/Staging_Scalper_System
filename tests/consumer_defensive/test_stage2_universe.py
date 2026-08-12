from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect, init_db
from consumer_defensive.core.norgate_membership import Candidate, load_candidates, load_historical_ciks, load_norgate_membership, resolve_candidate
from consumer_defensive.core.source_registry import load_source_registry, upsert_source_registry
from consumer_defensive.core.universe import (
    load_current_universe,
    load_policy,
    upsert_stage2_sources,
)
from consumer_defensive.core.universe_validation import validate_stage2


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "consumer_defensive"
CONFIG_PATH = PACKAGE_ROOT / "config.yaml"
POLICY_PATH = PACKAGE_ROOT / "data" / "consumer_defensive_universe_policy.yaml"
STAGE2_SOURCES = PACKAGE_ROOT / "data" / "stage2_source_registry.yaml"


def initialize_stage2(conn: sqlite3.Connection) -> tuple[object, object]:
    bundle = load_config(CONFIG_PATH)
    policy = load_policy(POLICY_PATH)
    init_db(conn)
    upsert_source_registry(
        conn,
        load_source_registry(
            resolve_path(cfg_get(bundle.payload, "source_registry.path"), base_dir=bundle.base_dir)
        ),
    )
    upsert_stage2_sources(conn, load_source_registry(STAGE2_SOURCES))
    return bundle, policy


def test_stage2_policy_records_adopted_membership_and_lineage_decisions() -> None:
    policy = load_policy(POLICY_PATH)
    assert policy.payload["recognized_membership_required"] is True
    assert policy.payload["recognized_membership_source_id"] == "norgate_us_equities_pit_membership"
    assert {
        row["vehicle_id"] for row in policy.payload["approved_membership_vehicles"]
    } == {
        "russell_3000",
        "sp_composite_1500",
        "nyse_composite",
        "nasdaq_composite",
    }
    assert policy.payload["current_holdings_validation_only"] == ["XLP", "IYK", "FSTA"]
    assert policy.payload["delisted_security_exclusions"] == ["CCE", "DPS"]


def test_historical_sec_identifier_mapping_is_complete_and_reviewed() -> None:
    mapping = load_historical_ciks(load_policy(POLICY_PATH))
    assert len(mapping) == 11
    assert all(value and len(value) == 10 and value.isdigit() for value in mapping.values())
    assert mapping["CORE"] == "0001318084"
    assert mapping["WBA"] == "0001618921"


def test_delisted_candidates_are_exact_terminal_scope_and_use_terminal_eligibility(tmp_path: Path) -> None:
    with connect(tmp_path / "scope.sqlite") as conn:
        _, policy = initialize_stage2(conn)
        load_current_universe(conn, policy)
        candidates, excluded = load_candidates(conn, policy)
        historical = {row.ticker: row for row in candidates if row.source_set == "delisted"}
        assert set(historical) == {"WBA", "SPTN", "SVU", "CORE", "AVP", "VGR", "K", "TWNK", "SAFM", "DF", "LNCE"}
        assert {ticker for ticker, row in historical.items() if not row.calibration_eligible} == {"WBA"}
        assert historical["DF"].exit_year == "2021"
        assert sum(row["status"] == "outside_reconciled_terminal_scope" for row in excluded) == 22


def test_explicit_provider_symbol_is_fail_closed() -> None:
    candidate = Candidate(
        ticker="DF", company_name="Dean Foods", cohort_id="packaged_foods",
        cohort_name="Packaged Foods", source_set="delisted", exchange="NYSE",
        listing_country="United States", currency="USD", security_type="Common Stock",
        explicit_price_symbol="DOES-NOT-EXIST",
    )
    resolved = resolve_candidate(object(), candidate, {"DF"}, {"DF-202106"})
    assert resolved.symbol == ""
    assert resolved.method == "explicit_price_source_symbol_not_found"


def test_stage2_current_load_is_exact_and_aliases_do_not_create_securities(tmp_path: Path) -> None:
    db_path = tmp_path / "consumer_defensive.sqlite"
    with connect(db_path) as conn:
        _, policy = initialize_stage2(conn)
        stats = load_current_universe(conn, policy)
        result = validate_stage2(conn, policy, require_pit_membership=False)
        assert stats == {
            "current_rows": 108,
            "vehicles": 4,
            "aliases": 2,
            "events": 2,
            "stale_taxonomy_rows_removed": 0,
        }
        assert result["status"] == "PASS", result
        assert result["cohort_counts"] == {
            "beverages": 22,
            "consumer_staples_distribution_retail": 22,
            "household_personal_tobacco": 25,
            "packaged_foods_agricultural_products": 39,
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_security WHERE listing_status='active'"
        ).fetchone()[0] == 108
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_security WHERE ticker IN ('CCE','DPS')"
        ).fetchone()[0] == 0
        assert dict(
            conn.execute(
                "SELECT alias_ticker, canonical_ticker FROM dim_security_alias ORDER BY alias_ticker"
            ).fetchall()
        ) == {"CCE": "CCEP", "DPS": "KDP"}
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_security WHERE ticker='CENTA'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_security WHERE ticker='CENT'"
        ).fetchone()[0] == 0


class FakeAdjustment:
    NONE = "none"


class FakeNorgate:
    StockPriceAdjustmentType = FakeAdjustment

    def __init__(
        self,
        active_symbols: set[str],
        delisted_symbols: set[str] | None = None,
    ) -> None:
        self.active_symbols = active_symbols
        self.delisted_symbols = delisted_symbols or set()

    def database_symbols(self, database: str) -> list[str]:
        if database == "US Equities":
            return sorted(self.active_symbols)
        if database == "US Equities Delisted":
            return sorted(self.delisted_symbols)
        return []

    def last_database_update_time(self, database: str) -> str:
        return "2026-08-10T17:30:14-05:00"

    def assetid(self, symbol: str) -> str:
        return f"fake-{symbol}"

    def security_name(self, symbol: str) -> str:
        return symbol

    def first_quoted_date(self, symbol: str) -> str:
        return "2017-11-28"

    def last_quoted_date(self, symbol: str) -> None:
        return None

    @staticmethod
    def _frame(value: int):
        pd = pytest.importorskip("pandas")
        return pd.DataFrame(
            {"value": [value, value, value]},
            index=pd.to_datetime(["2017-11-28", "2019-01-02", "2026-08-07"]),
        )

    def price_timeseries(self, *args, **kwargs):
        return self._frame(10)

    def major_exchange_listed_timeseries(self, *args, **kwargs):
        return self._frame(1)

    def index_constituent_timeseries(self, *args, **kwargs):
        return self._frame(1)


class DriftingFakeNorgate(FakeNorgate):
    """Changes fingerprint after the first candidate snapshot is extracted."""

    def __init__(self, active_symbols: set[str], drift_database: str) -> None:
        super().__init__(active_symbols)
        self.drift_database = drift_database
        self._index_calls = 0
        self._drifted = False

    def last_database_update_time(self, database: str) -> str:
        if self._drifted and database == self.drift_database:
            return "2026-08-10T17:31:00-05:00"
        return super().last_database_update_time(database)

    def index_constituent_timeseries(self, *args, **kwargs):
        frame = super().index_constituent_timeseries(*args, **kwargs)
        self._index_calls += 1
        if self._index_calls == 4:
            self._drifted = True
        return frame


@pytest.mark.parametrize(
    "drift_database",
    ["US Equities", "US Equities Delisted", "US Indices"],
)
def test_norgate_fingerprint_drift_publishes_neither_database_rows_nor_reports(
    tmp_path: Path,
    drift_database: str,
) -> None:
    pytest.importorskip("pandas")
    db_path = tmp_path / "consumer_defensive.sqlite"
    output_dir = tmp_path / "stage2_report"
    with connect(db_path) as conn:
        _, policy = initialize_stage2(conn)
        load_current_universe(conn, policy)
        active_symbols = {
            str(row[0])
            for row in conn.execute(
                "SELECT ticker FROM dim_security WHERE listing_status='active'"
            ).fetchall()
        }
        before = {
            "major_exchange": conn.execute(
                "SELECT COUNT(*) FROM fact_major_exchange_listing_daily"
            ).fetchone()[0],
            "vehicle_membership": conn.execute(
                "SELECT COUNT(*) FROM fact_recognized_vehicle_membership_daily"
            ).fetchone()[0],
            "universe_membership": conn.execute(
                "SELECT COUNT(*) FROM dim_universe_membership"
            ).fetchone()[0],
            "norgate_identifiers": conn.execute(
                "SELECT COUNT(*) FROM dim_identifier WHERE identifier_type='norgate_assetid'"
            ).fetchone()[0],
            "security_state": conn.execute(
                """
                SELECT security_id, provider_price_symbol, listing_start_date, listing_end_date
                FROM dim_security ORDER BY security_id
                """
            ).fetchall(),
        }

        with pytest.raises(RuntimeError, match="provider databases changed"):
            load_norgate_membership(
                conn,
                policy,
                provider=DriftingFakeNorgate(active_symbols, drift_database),
                as_of="2026-08-10",
                output_dir=output_dir,
            )

        after = {
            "major_exchange": conn.execute(
                "SELECT COUNT(*) FROM fact_major_exchange_listing_daily"
            ).fetchone()[0],
            "vehicle_membership": conn.execute(
                "SELECT COUNT(*) FROM fact_recognized_vehicle_membership_daily"
            ).fetchone()[0],
            "universe_membership": conn.execute(
                "SELECT COUNT(*) FROM dim_universe_membership"
            ).fetchone()[0],
            "norgate_identifiers": conn.execute(
                "SELECT COUNT(*) FROM dim_identifier WHERE identifier_type='norgate_assetid'"
            ).fetchone()[0],
            "security_state": conn.execute(
                """
                SELECT security_id, provider_price_symbol, listing_start_date, listing_end_date
                FROM dim_security ORDER BY security_id
                """
            ).fetchall(),
        }
        assert after == before

    assert not (output_dir / "norgate_membership_resolution.csv").exists()
    assert not (output_dir / "daily_cohort_breadth.csv").exists()
    assert not (output_dir / "summary.json").exists()


def test_stage2_norgate_contract_persists_four_series_and_union_membership(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    db_path = tmp_path / "consumer_defensive.sqlite"
    output_dir = tmp_path / "stage2_report"
    with connect(db_path) as conn:
        _, policy = initialize_stage2(conn)
        load_current_universe(conn, policy)
        active_symbols = {
            str(row[0])
            for row in conn.execute(
                "SELECT ticker FROM dim_security WHERE listing_status='active'"
            ).fetchall()
        }
        candidates, _ = load_candidates(conn, policy)
        historical_candidates = {
            candidate.ticker: candidate
            for candidate in candidates
            if candidate.source_set == "delisted"
        }
        historical_provider_symbols = {
            candidate.explicit_price_symbol or candidate.ticker
            for candidate in historical_candidates.values()
        }
        summary = load_norgate_membership(
            conn,
            policy,
            provider=FakeNorgate(active_symbols, historical_provider_symbols),
            as_of="2026-08-10",
            output_dir=output_dir,
        )
        result = validate_stage2(conn, policy, require_pit_membership=True)
        assert summary["current_loaded"] == 108
        assert summary["current_latest_eligible"] == 108
        assert summary["historical_expected"] == len(historical_candidates)
        assert summary["historical_required_in_window"] == len(historical_candidates)
        assert summary["historical_loaded"] == len(historical_candidates)
        assert summary["historical_recognized_members"] == len(historical_candidates)
        assert result["status"] == "PASS", result
        assert result["norgate_asset_identities"] == 108
        assert result["complete_four_index_daily_series"] == 108
        assert result["recognized_current_members"] == 108
        assert result["historical_candidates_expected"] == len(historical_candidates)
        assert result["historical_taxonomy_rows"] == len(historical_candidates)
        assert result["historical_norgate_asset_identities"] == len(historical_candidates)
        assert result["historical_complete_four_index_daily_series"] == len(
            historical_candidates
        )
        assert result["historical_recognized_members"] == len(historical_candidates)
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_recognized_vehicle_membership_daily"
        ).fetchone()[0] == (108 + len(historical_candidates)) * 4 * 3
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_major_exchange_listing_daily"
        ).fetchone()[0] == (108 + len(historical_candidates)) * 3
        assert (output_dir / "norgate_membership_resolution.csv").exists()
        assert (output_dir / "daily_cohort_breadth.csv").exists()


def test_stage2_norgate_contract_rejects_one_missing_historical_candidate(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pandas")
    output_dir = tmp_path / "stage2_report"
    with connect(tmp_path / "consumer_defensive.sqlite") as conn:
        _, policy = initialize_stage2(conn)
        load_current_universe(conn, policy)
        candidates, _ = load_candidates(conn, policy)
        active_symbols = {
            candidate.ticker
            for candidate in candidates
            if candidate.source_set == "current"
        }
        historical_candidates = {
            candidate.ticker: candidate
            for candidate in candidates
            if candidate.source_set == "delisted"
        }
        assert historical_candidates
        omitted = sorted(historical_candidates)[0]
        historical_provider_symbols = {
            candidate.explicit_price_symbol or candidate.ticker
            for ticker, candidate in historical_candidates.items()
            if ticker != omitted
        }
        provider = FakeNorgate(active_symbols, historical_provider_symbols)

        with pytest.raises(
            RuntimeError,
            match="Historical recognized-membership gate failed",
        ):
            load_norgate_membership(
                conn,
                policy,
                provider=provider,
                as_of="2026-08-10",
                output_dir=output_dir,
            )

        assert conn.execute(
            "SELECT COUNT(*) FROM fact_recognized_vehicle_membership_daily"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_major_exchange_listing_daily"
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT COUNT(*) FROM dim_security
               WHERE listing_status='delisted'"""
        ).fetchone()[0] == 0
        resolution = (output_dir / "norgate_membership_resolution.csv").read_text(
            encoding="utf-8"
        )
        assert omitted in resolution
        assert "unresolved" in resolution
