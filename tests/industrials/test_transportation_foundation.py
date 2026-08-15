from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from industrials.core.config import family_cfg_get, family_config, load_yaml
from industrials.core.db import connect, init_db, utc_now
from industrials.core.family_universe import (
    load_active_universe,
    load_aliases,
    load_historical_and_delisted,
    validate_database_contract,
    validate_identity_contract,
    validate_seed_contracts,
)
from industrials.core.source_registry import load_source_registry, upsert_source_registry
from industrials.transportation.security_continuity import (
    load_security_continuity_policies,
    upsert_security_continuity_policies,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "industrials" / "config.yaml"
MODEL_FAMILY = "transportation"


def resolved_paths() -> dict[str, Path | str]:
    config = load_yaml(CONFIG_PATH)
    universe = family_config(config, MODEL_FAMILY)["universe"]
    root = CONFIG_PATH.parent
    return {
        "active": root / universe["seed_csv"],
        "delisted": root / universe["delisted_seed_csv"],
        "historical": root / universe["historical_membership_csv"],
        "aliases": root / universe["ticker_aliases_csv"],
        "listing": root / universe["listing_dates_csv"],
        "policy": root / universe["policy_path"],
        "cohorts": root / universe["cohort_path"],
        "registry": root / config["source_registry"]["path"],
        "seed_source": universe["seed_source_id"],
        "cohort_source": universe["cohort_source_id"],
        "historical_source": universe["historical_membership_source_id"],
        "delisted_source": universe["delisted_source_id"],
        "alias_source": universe["ticker_aliases_source_id"],
        "default_start": universe["delisted_default_start_date"],
        "continuity": root / universe["security_continuity_overrides_csv"],
        "continuity_source": universe["security_continuity_source_id"],
    }


def load_foundation(conn: sqlite3.Connection) -> dict[str, Path | str]:
    paths = resolved_paths()
    init_db(conn)
    upsert_source_registry(conn, load_source_registry(Path(paths["registry"])))
    load_active_universe(
        conn,
        active_path=Path(paths["active"]),
        delisted_path=Path(paths["delisted"]),
        listing_path=Path(paths["listing"]),
        cohort_path=Path(paths["cohorts"]),
        policy_path=Path(paths["policy"]),
        model_family=MODEL_FAMILY,
        seed_source_id=str(paths["seed_source"]),
        cohort_source_id=str(paths["cohort_source"]),
    )
    load_historical_and_delisted(
        conn,
        historical_path=Path(paths["historical"]),
        delisted_path=Path(paths["delisted"]),
        cohort_path=Path(paths["cohorts"]),
        model_family=MODEL_FAMILY,
        historical_source_id=str(paths["historical_source"]),
        delisted_source_id=str(paths["delisted_source"]),
        default_start_date=str(paths["default_start"]),
    )
    load_aliases(conn, path=Path(paths["aliases"]), source_id=str(paths["alias_source"]))
    return paths


def test_family_config_is_explicit_and_fail_closed() -> None:
    config = load_yaml(CONFIG_PATH)
    assert family_cfg_get(config, MODEL_FAMILY, "universe.seed_csv", required=True) == (
        "transportation/system_csvs/transportation_tickers.csv"
    )
    assert family_config(config, "defense")["model_family"] == "defense"
    assert family_config(config, "machinery")["model_family"] == "machinery"
    with pytest.raises(KeyError, match="Unknown industrials model_family"):
        family_config(config, "not_a_family")


def test_transportation_seed_contract_counts_and_expected_warning() -> None:
    paths = resolved_paths()
    errors, warnings, counts = validate_seed_contracts(
        active_path=Path(paths["active"]),
        delisted_path=Path(paths["delisted"]),
        cohort_path=Path(paths["cohorts"]),
        policy_path=Path(paths["policy"]),
        model_family=MODEL_FAMILY,
    )
    assert errors == []
    assert counts == {"active": 120, "delisted": 48, "cohorts": 4}
    assert warnings == [
        "no curated delisted rows for cohort=development_stage_and_speculative_transport"
    ]


def test_transportation_foundation_load_and_validate(tmp_path: Path) -> None:
    db_path = tmp_path / "transportation.sqlite"
    with connect(db_path) as conn:
        paths = load_foundation(conn)
        errors = validate_database_contract(
            conn,
            model_family=MODEL_FAMILY,
            active_source_id=str(paths["seed_source"]),
            historical_source_id=str(paths["historical_source"]),
            delisted_source_id=str(paths["delisted_source"]),
            expected_active=120,
            expected_historical=167,
            expected_delisted=48,
        )
        assert errors == []
        assert validate_identity_contract(
            conn,
            model_family=MODEL_FAMILY,
            active_path=Path(paths["active"]),
            delisted_path=Path(paths["delisted"]),
        ) == []
        cohorts = dict(
            conn.execute(
                """
                SELECT calibration_cohort_id, COUNT(*)
                FROM dim_industrials_taxonomy
                WHERE model_family=? AND calibration_use<>'historical_research'
                GROUP BY calibration_cohort_id
                """,
                (MODEL_FAMILY,),
            ).fetchall()
        )
        assert cohorts == {
            "air_transport_and_aviation_services": 22,
            "development_stage_and_speculative_transport": 29,
            "marine_shipping_and_maritime": 29,
            "surface_freight_and_logistics": 40,
        }
        # Loading lifecycle history after the active seed must not erase the
        # detailed taxonomy used by specialized-metric applicability.
        active_industries = dict(
            conn.execute(
                """
                SELECT t.ticker, t.industry
                FROM dim_industrials_taxonomy AS t
                JOIN dim_universe_membership AS m
                  ON m.ticker=t.ticker AND m.model_family=t.model_family
                WHERE t.model_family=?
                  AND m.membership_source_id=?
                  AND m.membership_status='active'
                """,
                (MODEL_FAMILY, str(paths["seed_source"])),
            ).fetchall()
        )
        assert len(active_industries) == 120
        assert active_industries["UNP"] == "Railroads"
        assert active_industries["ODFL"] == "Trucking"
        assert active_industries["DAL"] == "Airlines"
        assert active_industries["AER"] == "Rental & Leasing Services"
        assert active_industries["ZIM"] == "Marine Shipping"
        assert "Transportation" not in set(active_industries.values())
        historical_industries = dict(
            conn.execute(
                """
                SELECT ticker, industry
                FROM dim_industrials_taxonomy
                WHERE model_family=? AND calibration_use='historical_research'
                """,
                (MODEL_FAMILY,),
            ).fetchall()
        )
        assert len(historical_industries) == 48
        assert historical_industries["KSU"] == "Railroads"
        assert historical_industries["SWFT"] == "Trucking"
        assert historical_industries["UTIW"] == "Integrated Freight & Logistics"
        assert historical_industries["LCC"] == "Airlines"
        assert historical_industries["AYR"] == "Rental & Leasing Services"
        assert historical_industries["DRYS"] == "Marine Shipping"
        assert "Transportation" not in set(historical_industries.values())


def test_security_continuity_contract_is_verified_and_persists(tmp_path: Path) -> None:
    paths = resolved_paths()
    policies = load_security_continuity_policies(Path(paths["continuity"]))
    assert set(policies) == {"AZUL", "ECO", "HAFN", "HSHP", "LTM", "PSIG"}
    assert {
        ticker: policy.current_security_start_date
        for ticker, policy in policies.items()
    } == {
        "AZUL": "2026-06-01",
        "ECO": "2023-12-11",
        "HAFN": "2024-04-09",
        "HSHP": "2023-04-03",
        "LTM": "2024-07-25",
        "PSIG": "2024-07-19",
    }
    assert policies["ECO"].required_fx_pair == "NOKUSD"
    assert policies["HAFN"].history_treatment == "separate_listing_optional_issuer_proxy"
    assert policies["AZUL"].history_treatment == "separate_regime_no_return_stitch"
    assert policies["PSIG"].history_treatment == "hard_boundary_no_spac_price_stitch"

    db_path = tmp_path / "continuity.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(Path(paths["registry"])))
        assert (
            upsert_security_continuity_policies(
                conn,
                policies=policies,
                source_id=str(paths["continuity_source"]),
            )
            == 6
        )
        rows = conn.execute(
            """
            SELECT ticker, continuity_policy, current_security_start_date,
                   evidence_label, review_status
            FROM dim_security_continuity_policy
            WHERE model_family='transportation'
            ORDER BY ticker
            """
        ).fetchall()
    assert len(rows) == 6
    assert all(row["evidence_label"] == "fact_source_reported" for row in rows)
    assert all(row["review_status"] == "primary_source_verified" for row in rows)


def test_transportation_load_preserves_other_family_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "cross_family.sqlite"
    paths = resolved_paths()
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(Path(paths["registry"])))
        now = utc_now()
        conn.execute(
            """
            INSERT INTO dim_company(
                ticker, cik, company_name, sector, industry, subsector, country,
                currency, universe_status, is_active, data_quality_status,
                first_seen_at, updated_at
            ) VALUES ('FTAI', '0001590364', 'FTAI Aviation Ltd.', 'Industrials',
                      'Aerospace & Defense', 'Defense', 'United States', 'USD',
                      'investable', 1, 'defense_sentinel', ?, ?)
            """,
            (now, now),
        )
        company_id = int(conn.execute("SELECT company_id FROM dim_company WHERE ticker='FTAI'").fetchone()[0])
        conn.execute(
            """
            INSERT INTO dim_industrials_taxonomy(
                company_id, ticker, model_family, sector, industry, subsector,
                calibration_cohort_id, calibration_cohort, calibration_use,
                development_stage, taxonomy_confidence, taxonomy_source,
                analyst_reviewed, updated_at
            ) VALUES (?, 'FTAI', 'defense', 'Industrials', 'Aerospace & Defense',
                      'Defense', 'defense_sentinel', 'Defense Sentinel', 'core',
                      'operating', 1.0, 'defense_cohort_policy', 1, ?)
            """,
            (company_id, now),
        )
        conn.execute(
            """
            INSERT INTO dim_universe_membership(
                company_id, ticker, model_family, membership_source_id,
                membership_basis, start_date, membership_status,
                is_current_member, point_in_time_flag, confidence, reason,
                created_at, updated_at
            ) VALUES (?, 'FTAI', 'defense', 'defense_ticker_seed',
                      'current_source_of_truth', '2015-05-14', 'active', 1, 1,
                      1.0, 'cross-family sentinel', ?, ?)
            """,
            (company_id, now, now),
        )
        before = tuple(
            conn.execute(
                "SELECT * FROM dim_industrials_taxonomy WHERE model_family='defense' AND ticker='FTAI'"
            ).fetchone()
        )
        membership_before = tuple(
            conn.execute(
                "SELECT * FROM dim_universe_membership WHERE model_family='defense' AND ticker='FTAI'"
            ).fetchone()
        )
        load_foundation(conn)
        after = tuple(
            conn.execute(
                "SELECT * FROM dim_industrials_taxonomy WHERE model_family='defense' AND ticker='FTAI'"
            ).fetchone()
        )
        membership_after = tuple(
            conn.execute(
                "SELECT * FROM dim_universe_membership WHERE model_family='defense' AND ticker='FTAI'"
            ).fetchone()
        )
        assert after == before
        assert membership_after == membership_before
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_industrials_taxonomy WHERE model_family='transportation' AND ticker='FTAI'"
        ).fetchone()[0] == 1
