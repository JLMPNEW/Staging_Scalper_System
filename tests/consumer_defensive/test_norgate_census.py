from __future__ import annotations

from pathlib import Path

import pytest

from consumer_defensive.core.norgate_census import (
    CLASSIFICATION_TIME_BASIS,
    catalog_status,
    discover_candidate_census,
)
from consumer_defensive.core.norgate_pit_census import (
    enrich_candidate_pit_membership,
    membership_dates_flags,
)
from consumer_defensive.core.universe import load_policy


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = PROJECT_ROOT / "consumer_defensive" / "data" / "consumer_defensive_universe_policy.yaml"


class FakeCensusNorgate:
    def __init__(self) -> None:
        self.active = {"ACTIVE", "STAPLES"}
        self.delisted = {"OLD"}

    def last_database_update_time(self, database: str) -> str:
        return "2026-08-25T19:27:33-05:00"

    def database_symbols(self, database: str) -> list[str]:
        return sorted(self.active if database == "US Equities" else self.delisted)

    def watchlist_symbols(self, watchlist: str) -> list[str]:
        values = {
            "Russell 3000 Current & Past": ["STAPLES", "OLD"],
            "S&P Composite 1500 Current & Past": ["ACTIVE", "STAPLES"],
            "NYSE Composite Current & Past": ["MISSING"],
            "Nasdaq Composite Current & Past": ["OLD"],
        }
        return values[watchlist]

    def assetid(self, symbol: str) -> str:
        return f"asset-{symbol}"

    def security_name(self, symbol: str) -> str:
        return f"{symbol} Holdings"

    def first_quoted_date(self, symbol: str) -> str:
        return "2017-11-28"

    def last_quoted_date(self, symbol: str) -> str:
        return "" if symbol != "OLD" else "2021-01-01"

    def classification(self, symbol: str, scheme: str, result_type: str) -> str:
        assert (scheme, result_type) == ("GICS", "name")
        return "Household Products" if symbol == "STAPLES" else "Industrial Conglomerates"

    def classification_at_level(
        self, symbol: str, scheme: str, result_type: str, level: int
    ) -> str:
        assert (scheme, result_type, level) == ("GICS", "name", 1)
        return "Consumer Staples" if symbol == "STAPLES" else "Industrials"

    def index_constituent_timeseries(
        self,
        symbol: str,
        index_name: str,
        *,
        start_date: str,
        end_date: str,
        timeseriesformat: str,
    ):
        import pandas as pd

        assert start_date <= end_date
        assert timeseriesformat == "pandas-dataframe"
        values = [1, 1, 0] if (
            symbol == "STAPLES" and index_name == "Russell 3000"
        ) else [0, 0, 0]
        return pd.DataFrame(
            {"Member": values},
            index=pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"]),
        )


def test_catalog_status_is_explicit_about_collisions_and_absence() -> None:
    assert catalog_status("A", {"A"}, set()) == "active"
    assert catalog_status("D", set(), {"D"}) == "delisted"
    assert catalog_status("B", {"B"}, {"B"}) == "active_and_delisted_catalog_collision"
    assert catalog_status("X", set(), set()) == "absent_from_equity_catalogs"


def test_candidate_discovery_enumerates_watchlist_union_and_marks_non_pit() -> None:
    rows, summary = discover_candidate_census(FakeCensusNorgate(), load_policy(POLICY_PATH))
    by_symbol = {str(row["provider_symbol"]): row for row in rows}

    assert set(by_symbol) == {"ACTIVE", "MISSING", "OLD", "STAPLES"}
    assert by_symbol["STAPLES"]["candidate_consumer_defensive"] == 1
    assert by_symbol["STAPLES"]["approved_vehicle_ids"] == "russell_3000;sp_composite_1500"
    assert by_symbol["STAPLES"]["classification_is_point_in_time"] == 0
    assert by_symbol["STAPLES"]["pit_membership_verified"] == 0
    assert by_symbol["MISSING"]["status"] == "catalog_anomaly"
    assert summary["status"] == "CANDIDATE_DISCOVERY_ONLY"
    assert summary["point_in_time_survivorship_complete"] is False
    assert summary["classification_is_point_in_time"] is False
    assert summary["classification_time_basis"] == CLASSIFICATION_TIME_BASIS
    assert summary["watchlist_union_count"] == 4
    assert summary["complete_union_examined"] is True
    assert summary["candidate_consumer_defensive_count"] == 1


def test_candidate_discovery_limited_run_is_explicitly_incomplete() -> None:
    _, summary = discover_candidate_census(
        FakeCensusNorgate(), load_policy(POLICY_PATH), max_symbols=2
    )
    assert summary["symbols_examined"] == 2
    assert summary["watchlist_union_count"] == 4
    assert summary["complete_union_examined"] is False


def test_candidate_discovery_rejects_nonpositive_limit() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        discover_candidate_census(FakeCensusNorgate(), load_policy(POLICY_PATH), max_symbols=0)


def test_pit_membership_enrichment_preserves_taxonomy_review_lock() -> None:
    provider = FakeCensusNorgate()
    rows, _ = discover_candidate_census(provider, load_policy(POLICY_PATH))
    enriched, summary = enrich_candidate_pit_membership(
        provider,
        load_policy(POLICY_PATH),
        rows,
        start_date="2019-01-02",
        end_date="2026-08-14",
    )

    assert len(enriched) == 1
    row = enriched[0]
    assert row["provider_symbol"] == "STAPLES"
    assert row["pit_index_membership_overlap_flag"] == 1
    assert row["pit_index_membership_first_date"] == "2020-01-02"
    assert row["pit_index_membership_last_date"] == "2020-01-03"
    assert row["pit_index_membership_session_count"] == 2
    assert row["pit_index_membership_vehicle_ids"] == "russell_3000"
    assert row["pit_index_membership_overlap_verified"] == 1
    assert row["pit_membership_verified"] == 0
    assert row["point_in_time_taxonomy_verified"] == 0
    assert row["candidate_discovery_only"] == 1
    assert row["status"] == "pit_membership_overlap_review_required"
    assert summary["pit_index_membership_overlap_count"] == 1
    assert summary["point_in_time_taxonomy_verified"] is False
    assert summary["point_in_time_survivorship_complete"] is False
    assert summary["production_or_calibration_use_allowed"] is False


def test_pit_membership_enrichment_rejects_bad_window() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        enrich_candidate_pit_membership(
            FakeCensusNorgate(),
            load_policy(POLICY_PATH),
            [],
            start_date="2026-08-14",
            end_date="2019-01-02",
        )


def test_membership_series_rejects_nonbinary_values() -> None:
    import pandas as pd

    frame = pd.DataFrame(
        {"Member": [0, 2]},
        index=pd.to_datetime(["2020-01-02", "2020-01-03"]),
    )
    with pytest.raises(ValueError, match="non-binary"):
        membership_dates_flags(frame)
