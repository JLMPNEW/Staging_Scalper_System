from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from industrials.core.config import family_config, load_yaml, resolve_path
from industrials.transportation.source_census import (
    CENSUS_FIELDS,
    DECISION_FIELDS,
    GAP_FIELDS,
    _inside_registration_window,
    _filing_rows,
    canonical_rows_hash,
    read_csv,
    validate_written_source_census,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDUSTRIALS_ROOT = PROJECT_ROOT / "industrials"
CONFIG_PATH = INDUSTRIALS_ROOT / "config.yaml"


def census_paths() -> dict[str, Path]:
    config = load_yaml(CONFIG_PATH)
    parser_cfg = family_config(config, "transportation")["dedicated_parser"]
    return {
        "census": resolve_path(
            parser_cfg["source_census_csv"],
            base_dir=INDUSTRIALS_ROOT,
        ),
        "decisions": resolve_path(
            parser_cfg["source_decisions_csv"],
            base_dir=INDUSTRIALS_ROOT,
        ),
        "gaps": resolve_path(
            parser_cfg["source_cache_gaps_csv"],
            base_dir=INDUSTRIALS_ROOT,
        ),
        "manifest": resolve_path(
            parser_cfg["source_census_manifest_json"],
            base_dir=INDUSTRIALS_ROOT,
        ),
    }


def test_canonical_row_hash_is_stable_across_csv_string_round_trip() -> None:
    fields = ("count", "flag", "text")
    typed = [{"count": 7, "flag": 0, "text": "value"}]
    strings = [{"count": "7", "flag": "0", "text": "value"}]
    assert canonical_rows_hash(typed, fields=fields) == canonical_rows_hash(
        strings,
        fields=fields,
    )


def test_registration_window_is_bounded_after_listing() -> None:
    from datetime import date

    anchor = date(2024, 7, 25)
    assert _inside_registration_window("2022-07-26", anchor=anchor)
    assert not _inside_registration_window("2022-07-25", anchor=anchor)
    assert _inside_registration_window("2024-10-23", anchor=anchor)
    assert not _inside_registration_window("2024-10-24", anchor=anchor)
    assert not _inside_registration_window("invalid", anchor=anchor)
    assert not _inside_registration_window("2024-07-25", anchor=None)


def test_source_window_keeps_active_lead_in_and_inactive_lifetime() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE fact_sec_filing(
            ticker TEXT, cik TEXT, source_id TEXT,
            accession_number TEXT, form_type TEXT, filing_date TEXT,
            accepted_at TEXT, report_date TEXT, primary_document TEXT
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO fact_sec_filing
        VALUES (?, '0000000001', 'sec_submissions', ?, '10-K', ?,
                ?, ?, 'annual.htm')
        """,
        [
            ("ACTIVE", "old-active", "2010-03-01", "2010-03-01", "2009-12-31"),
            ("ACTIVE", "new-active", "2018-03-01", "2018-03-01", "2017-12-31"),
            ("OLD", "legacy-old", "2005-03-01", "2005-03-01", "2004-12-31"),
            ("OLD", "post-exit", "2012-03-01", "2012-03-01", "2011-12-31"),
        ],
    )
    rows = _filing_rows(
        connection,
        tickers=("ACTIVE", "OLD"),
        members={
            "ACTIVE": {
                "universe_role": "active",
                "membership_end_date": "",
            },
            "OLD": {
                "universe_role": "delisted_usable",
                "membership_end_date": "2010-12-31",
            },
        },
        source_id="sec_submissions",
        start_date="2017-11-28",
        legacy_inactive_start_date="2000-01-01",
        asof_date="2026-07-22",
    )
    assert {
        (row["ticker"], row["accession_number"]) for row in rows
    } == {
        ("ACTIVE", "new-active"),
        ("OLD", "legacy-old"),
    }


def test_committed_dp3_source_census_is_internally_valid() -> None:
    paths = census_paths()
    assert (
        validate_written_source_census(
            census_path=paths["census"],
            decisions_path=paths["decisions"],
            gaps_path=paths["gaps"],
            manifest_path=paths["manifest"],
        )
        == []
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["database_mode"] == "read_only"
    assert manifest["network_requests"] == 0
    assert manifest["parser_execution_authorized"] is False
    assert manifest["identity_count"] == 160
    assert manifest["active_identity_count"] == 112
    assert manifest["inactive_identity_count"] == 48
    assert manifest["selected_identity_count"] == 160
    assert manifest["identities_without_selected_sources"] == []
    assert manifest["parser_metric_count"] == 84
    assert manifest["base_accession_count"] == 4_267
    assert manifest["base_accession_count"] == manifest["expected_base_accession_count"]
    assert manifest["selected_accession_count"] >= manifest["base_accession_count"]
    assert (
        manifest["cached_document_row_count"] + manifest["missing_document_row_count"]
        == manifest["selected_document_row_count"]
    )
    assert manifest["acceptance"] == ("PASS" if manifest["unresolved_gap_count"] == 0 else "NO_GO")


def test_source_census_has_exact_fields_and_all_metric_scopes() -> None:
    paths = census_paths()
    rows = read_csv(paths["census"])
    assert rows
    assert tuple(rows[0]) == CENSUS_FIELDS
    assert len({row["row_key"] for row in rows}) == len(rows)
    assert {row["universe_role"] for row in rows} <= {
        "active",
        "delisted_usable",
    }
    assert all(int(row["applicable_metric_count"]) > 0 for row in rows)
    assert all(row["applicable_metric_ids"] for row in rows)
    assert all(row["applicable_metric_packs"] for row in rows)
    assert all(len(row["content_sha256"]) == 64 for row in rows if row["cache_status"] == "CACHED_HASHED")


def test_supplemental_policy_is_positive_signal_only_and_bounded() -> None:
    paths = census_paths()
    decisions = read_csv(paths["decisions"])
    assert decisions
    assert tuple(decisions[0]) == DECISION_FIELDS
    assert not any(row["decision"] == "REVIEW_METADATA_GAP" for row in decisions)
    assert any(
        row["selection_rule"] == "supplemental_event_positive_metadata_only"
        and row["decision"] == "EXCLUDE_NO_METADATA_SIGNAL"
        for row in decisions
    )
    included_supplemental = [
        row for row in decisions if row["decision"] == "INCLUDE" and row["candidate_type"].startswith("supplemental_")
    ]
    assert included_supplemental
    assert len(included_supplemental) < 500
    assert {row["selection_rule"] for row in included_supplemental} <= {
        "supplemental_earnings_item_2_02_or_7_01",
        "supplemental_event_index_metadata",
        "supplemental_registration_listing_window",
    }


def test_cache_gaps_are_only_exact_selected_documents() -> None:
    paths = census_paths()
    gaps = read_csv(paths["gaps"])
    census = read_csv(paths["census"])
    if gaps:
        assert tuple(gaps[0]) == GAP_FIELDS
    missing_keys = {
        (row["ticker"], row["accession_number"], row["document_name"])
        for row in census
        if row["cache_status"] != "CACHED_HASHED"
    }
    gap_keys = {(row["ticker"], row["accession_number"], row["document_name"]) for row in gaps}
    assert gap_keys == missing_keys
    assert all(row["gap_type"] == "SOURCE_DOCUMENT" for row in gaps)
    assert all(row["required_action"] == "HYDRATE_SEALED_DOCUMENT" for row in gaps)
