#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.csv_utils import read_csv_flexible, row_get  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_defense_stage0_4_production_readiness")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_REPORT = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage4" / "production_readiness_stage0_4.csv"
REQUIRED_TABLES = [
    "source_registry",
    "dim_company",
    "dim_security",
    "dim_identifier",
    "dim_ticker_alias",
    "dim_industrials_taxonomy",
    "dim_universe_membership",
    "dim_delisted_calibration_seed",
    "fact_price_ohlcv",
    "fact_market_snapshot",
    "feature_market_technical",
    "dim_issuer_reporting_profile",
    "fact_sec_filing",
    "fact_sec_xbrl_fact_raw",
    "fact_sec_xbrl_fact",
    "fact_financial_statement_canonical",
    "feature_financial_statement",
    "data_quality_issues",
    "runs",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate production readiness for defense Stages 0-4.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None, help="Optional DB override. Fails unless --allow-scratch-db is set.")
    parser.add_argument("--model-family", default="", help="Industrials model family to validate, e.g. defense.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--allow-scratch-db", action="store_true", help="Allow validation against a non-configured or scratch DB path.")
    parser.add_argument("--require-live-sec-facts", action="store_true", help="Fail when no raw/mapped SEC XBRL facts have been loaded.")
    parser.add_argument("--require-delisted-price-history", action="store_true", help="Fail when Norgate delisted price history is incomplete.")
    return parser.parse_args()


def normalize_path(path: Path) -> str:
    return str(path.expanduser()).replace("/", "\\").rstrip("\\").lower()


def is_scratch_path(path: Path, project_root: Path) -> bool:
    normalized = normalize_path(path)
    project_output = normalize_path(project_root / "output")
    return "\\tmp\\" in normalized or normalized.startswith("c:\\tmp\\") or normalized.startswith(project_output)


def connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def value(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def csv_rows_by_ticker(path: Path, *, ticker_field: str = "ticker") -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    duplicates: list[str] = []
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, ticker_field, "ticker"))
        if not ticker:
            continue
        if ticker in rows:
            duplicates.append(ticker)
        rows[ticker] = row
    if duplicates:
        raise ValueError(f"{path} contains duplicate tickers: {sorted(set(duplicates))}")
    return rows


def load_cik_overrides(path: Path) -> dict[str, tuple[str, str]]:
    if not path.exists():
        return {}
    overrides: dict[str, tuple[str, str]] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker"))
        cik = normalize_cik(row_get(row, "cik"))
        applies_to = (row_get(row, "applies_to") or "both").lower()
        if ticker and cik:
            if ticker in overrides:
                raise ValueError(f"{path}: duplicate CIK override ticker={ticker}")
            overrides[ticker] = (cik, applies_to)
    return overrides


def override_cik_for_row(ticker: str, row: dict[str, str], overrides: dict[str, tuple[str, str]], *, scope: str) -> str:
    candidates = [ticker]
    exit_year = row_get(row, "exit_year")
    if exit_year.isdigit():
        candidates.insert(0, f"{ticker}-DEL{int(exit_year)}")
    for candidate in candidates:
        override = overrides.get(candidate)
        if override is not None:
            cik, applies_to = override
            if applies_to in {scope, "both"}:
                return cik
    return normalize_cik(row_get(row, "cik"))


def apply_cik_overrides(
    rows: dict[str, dict[str, str]],
    overrides: dict[str, tuple[str, str]],
    *,
    scope: str,
) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for ticker, row in rows.items():
        new_row = dict(row)
        new_row["cik"] = override_cik_for_row(ticker, row, overrides, scope=scope)
        out[ticker] = new_row
    return out


def write_report(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["status", "gate", "detail"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def add_check(rows: list[dict[str, str]], *, status: str, gate: str, detail: str) -> None:
    rows.append({"status": status, "gate": gate, "detail": detail})


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row["name"]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def compare_active_identity(
    conn: sqlite3.Connection,
    *,
    active_rows: dict[str, dict[str, str]],
    model_family: str,
) -> list[str]:
    tickers = sorted(active_rows)
    ph = placeholders(tickers)
    db_rows = conn.execute(
        f"""
        SELECT c.ticker, c.cik, c.company_name
        FROM dim_company c
        JOIN dim_industrials_taxonomy t
          ON t.company_id = c.company_id
         AND t.model_family = ?
        WHERE c.is_active = 1
          AND c.ticker IN ({ph})
        """,
        (model_family, *tickers),
    ).fetchall()
    db_by_ticker = {str(row["ticker"]): row for row in db_rows}
    errors: list[str] = []
    missing = sorted(set(tickers).difference(db_by_ticker))
    if missing:
        errors.append(f"active tickers missing from DB: {missing[:20]}")
    for ticker, csv_row in active_rows.items():
        db_row = db_by_ticker.get(ticker)
        if db_row is None:
            continue
        csv_cik = normalize_cik(row_get(csv_row, "cik"))
        db_cik = str(db_row["cik"] or "")
        if csv_cik and db_cik != csv_cik:
            errors.append(f"{ticker}: active CIK mismatch db={db_cik!r} csv={csv_cik!r}")
        csv_name = row_get(csv_row, "company_name")
        db_name = str(db_row["company_name"] or "")
        if csv_name and db_name != csv_name:
            errors.append(f"{ticker}: active company_name mismatch db={db_name!r} csv={csv_name!r}")
    return errors


def compare_delisted_identity(
    conn: sqlite3.Connection,
    *,
    delisted_rows: dict[str, dict[str, str]],
    model_family: str,
) -> list[str]:
    tickers = sorted(delisted_rows)
    ph = placeholders(tickers)
    db_rows = conn.execute(
        f"""
        SELECT ticker, internal_ticker, company_name, cik
        FROM dim_delisted_calibration_seed
        WHERE model_family = ?
          AND ticker IN ({ph})
        """,
        (model_family, *tickers),
    ).fetchall()
    db_by_ticker = {str(row["ticker"]): row for row in db_rows}
    errors: list[str] = []
    missing = sorted(set(tickers).difference(db_by_ticker))
    if missing:
        errors.append(f"delisted tickers missing from seed table: {missing[:20]}")
    for ticker, csv_row in delisted_rows.items():
        db_row = db_by_ticker.get(ticker)
        if db_row is None:
            continue
        csv_cik = normalize_cik(row_get(csv_row, "cik"))
        db_cik = str(db_row["cik"] or "")
        if csv_cik and db_cik != csv_cik:
            errors.append(f"{ticker}: delisted CIK mismatch db={db_cik!r} csv={csv_cik!r}")
        csv_name = row_get(csv_row, "company", "company_name")
        db_name = str(db_row["company_name"] or "")
        if csv_name and db_name != csv_name:
            errors.append(f"{ticker}: delisted company_name mismatch db={db_name!r} csv={csv_name!r}")
    return errors


def validate() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    configured_db = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    db_path = args.db.expanduser().resolve() if args.db else configured_db
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else DEFAULT_REPORT
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    active_csv = resolve_path(cfg_get(config, "industrials_universe.seed_csv"), base_dir=base_dir)
    delisted_csv = resolve_path(cfg_get(config, "industrials_universe.delisted_seed_csv"), base_dir=base_dir)
    cik_overrides_csv = resolve_path(cfg_get(config, "industrials_universe.cik_ticker_overrides_csv"), base_dir=base_dir)
    historical_csv = resolve_path(cfg_get(config, "industrials_universe.historical_membership_csv"), base_dir=base_dir)
    aliases_csv = resolve_path(cfg_get(config, "industrials_universe.ticker_aliases_csv"), base_dir=base_dir)
    expected_active = int(cfg_get(config, "industrials_universe.expected_ticker_count", 95) or 95)
    active_source_id = str(cfg_get(config, "industrials_universe.seed_source_id", "defense_ticker_seed"))
    delisted_source_id = str(cfg_get(config, "industrials_universe.delisted_source_id", "defense_delisted_calibration_seed"))
    alias_source_id = str(cfg_get(config, "industrials_universe.ticker_aliases_source_id", "defense_ticker_alias_seed"))
    market_source_id = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    fallback_sources = cfg_get(config, "market_data_policy.scoring_fallback_sources", ["norgate_us_equities_total_return"])
    if isinstance(fallback_sources, list) and fallback_sources:
        norgate_source_id = str(fallback_sources[0])
    else:
        norgate_source_id = str(fallback_sources or "norgate_us_equities_total_return")
    submissions_source_id = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions") or "sec_submissions")
    companyfacts_source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    benchmark_tickers = [normalize_ticker(ticker) for ticker in (cfg_get(config, "industrials_universe.benchmark_tickers", []) or [])]
    benchmark_tickers = [ticker for ticker in benchmark_tickers if ticker]

    checks: list[dict[str, str]] = []
    cik_overrides = load_cik_overrides(cik_overrides_csv)
    active_rows = apply_cik_overrides(csv_rows_by_ticker(active_csv), cik_overrides, scope="active")
    delisted_rows = apply_cik_overrides(csv_rows_by_ticker(delisted_csv), cik_overrides, scope="delisted")
    historical_rows = read_csv_flexible(historical_csv)
    alias_rows = [row for row in read_csv_flexible(aliases_csv) if normalize_ticker(row_get(row, "contract_ticker"))]

    if db_path != configured_db and not args.allow_scratch_db:
        add_check(
            checks,
            status="fail",
            gate="production_db_path",
            detail=f"DB override {db_path} does not match configured production DB {configured_db}. Use --allow-scratch-db only for test validation.",
        )
    elif is_scratch_path(db_path, PROJECT_ROOT) and not args.allow_scratch_db:
        add_check(
            checks,
            status="fail",
            gate="production_db_path",
            detail=f"DB path looks like scratch/output path: {db_path}. Use configured production DB for readiness.",
        )
    else:
        add_check(checks, status="pass", gate="production_db_path", detail=str(db_path))

    if not db_path.exists():
        add_check(checks, status="fail", gate="production_db_exists", detail=f"Missing configured DB: {db_path}")
        write_report(output_csv, checks)
        for row in checks:
            log = LOGGER.error if row["status"] == "fail" else LOGGER.info
            log("%s %s: %s", row["status"].upper(), row["gate"], row["detail"])
        LOGGER.error("Defense Stage 0-4 production readiness failed: DB does not exist.")
        return 1
    add_check(checks, status="pass", gate="production_db_exists", detail=str(db_path))

    errors = 0
    with connect_readonly(db_path) as conn:
        tables = table_names(conn)
        missing_tables = [table for table in REQUIRED_TABLES if table not in tables]
        if missing_tables:
            add_check(checks, status="fail", gate="required_tables", detail=f"Missing tables: {missing_tables}")
            write_report(output_csv, checks)
            for row in checks:
                log = LOGGER.error if row["status"] == "fail" else LOGGER.info
                log("%s %s: %s", row["status"].upper(), row["gate"], row["detail"])
            LOGGER.error("Defense Stage 0-4 production readiness failed: required tables are missing.")
            return 1
        else:
            add_check(checks, status="pass", gate="required_tables", detail=f"tables={len(tables)}")

        for source_id, required_status in [
            (active_source_id, "active"),
            (delisted_source_id, "active"),
            (alias_source_id, "active"),
            (market_source_id, "active"),
            (submissions_source_id, "active"),
            (companyfacts_source_id, "active"),
        ]:
            status = value(conn, "SELECT status FROM source_registry WHERE source_id = ?", (source_id,))
            if status == required_status:
                add_check(checks, status="pass", gate=f"source_registry:{source_id}", detail=f"status={status}")
            else:
                add_check(checks, status="fail", gate=f"source_registry:{source_id}", detail=f"expected={required_status} actual={status!r}")

        if len(active_rows) == expected_active:
            add_check(checks, status="pass", gate="active_csv_count", detail=f"rows={len(active_rows)}")
        else:
            add_check(checks, status="fail", gate="active_csv_count", detail=f"expected={expected_active} actual={len(active_rows)}")
        if len(delisted_rows) > 0:
            add_check(checks, status="pass", gate="delisted_csv_count", detail=f"rows={len(delisted_rows)}")
        else:
            add_check(checks, status="fail", gate="delisted_csv_count", detail="delisted calibration CSV is empty")
        add_check(checks, status="pass", gate="historical_membership_csv_count", detail=f"rows={len(historical_rows)}")

        active_tickers = sorted(active_rows)
        active_ph = placeholders(active_tickers)
        active_company_count = scalar(
            conn,
            f"""
            SELECT COUNT(DISTINCT c.ticker)
            FROM dim_company c
            JOIN dim_industrials_taxonomy t
              ON t.company_id = c.company_id
             AND t.model_family = ?
            WHERE c.is_active = 1
              AND c.ticker IN ({active_ph})
            """,
            (model_family, *active_tickers),
        )
        active_security_count = scalar(conn, f"SELECT COUNT(DISTINCT ticker) FROM dim_security WHERE ticker IN ({active_ph})", tuple(active_tickers))
        active_membership_count = scalar(
            conn,
            f"""
            SELECT COUNT(DISTINCT ticker)
            FROM dim_universe_membership
            WHERE model_family = ?
              AND membership_source_id = ?
              AND is_current_member = 1
              AND ticker IN ({active_ph})
            """,
            (model_family, active_source_id, *active_tickers),
        )
        if active_company_count == len(active_rows) == expected_active:
            add_check(checks, status="pass", gate="active_company_rows", detail=f"rows={active_company_count}")
        else:
            add_check(checks, status="fail", gate="active_company_rows", detail=f"expected={len(active_rows)} actual={active_company_count}")
        if active_security_count == len(active_rows):
            add_check(checks, status="pass", gate="active_security_rows", detail=f"rows={active_security_count}")
        else:
            add_check(checks, status="fail", gate="active_security_rows", detail=f"expected={len(active_rows)} actual={active_security_count}")
        if active_membership_count == len(active_rows):
            add_check(checks, status="pass", gate="active_current_membership", detail=f"rows={active_membership_count}")
        else:
            add_check(checks, status="fail", gate="active_current_membership", detail=f"expected={len(active_rows)} actual={active_membership_count}")

        active_identity_errors = compare_active_identity(conn, active_rows=active_rows, model_family=model_family)
        if active_identity_errors:
            add_check(checks, status="fail", gate="active_identity_matches_csv", detail="; ".join(active_identity_errors[:10]))
        else:
            add_check(checks, status="pass", gate="active_identity_matches_csv", detail=f"tickers={len(active_rows)}")

        delisted_tickers = sorted(delisted_rows)
        delisted_ph = placeholders(delisted_tickers)
        delisted_seed_count = scalar(
            conn,
            f"SELECT COUNT(*) FROM dim_delisted_calibration_seed WHERE model_family = ? AND ticker IN ({delisted_ph})",
            (model_family, *delisted_tickers),
        )
        internal_tickers = [
            str(row["internal_ticker"])
            for row in conn.execute(
                f"""
                SELECT internal_ticker
                FROM dim_delisted_calibration_seed
                WHERE model_family = ?
                  AND ticker IN ({delisted_ph})
                ORDER BY ticker
                """,
                (model_family, *delisted_tickers),
            ).fetchall()
        ]
        if delisted_seed_count == len(delisted_rows):
            add_check(checks, status="pass", gate="delisted_seed_rows", detail=f"rows={delisted_seed_count}")
        else:
            add_check(checks, status="fail", gate="delisted_seed_rows", detail=f"expected={len(delisted_rows)} actual={delisted_seed_count}")
        if len(set(internal_tickers)) == len(delisted_rows):
            add_check(checks, status="pass", gate="delisted_internal_ticker_uniqueness", detail=f"internal_tickers={len(set(internal_tickers))}")
        else:
            add_check(checks, status="fail", gate="delisted_internal_ticker_uniqueness", detail=f"expected={len(delisted_rows)} actual_unique={len(set(internal_tickers))}")
        if internal_tickers:
            internal_ph = placeholders(internal_tickers)
            delisted_membership_count = scalar(
                conn,
                f"""
                SELECT COUNT(DISTINCT ticker)
                FROM dim_universe_membership
                WHERE model_family = ?
                  AND membership_source_id = ?
                  AND membership_basis = 'delisted_calibration_seed'
                  AND is_current_member = 0
                  AND ticker IN ({internal_ph})
                """,
                (model_family, delisted_source_id, *internal_tickers),
            )
            inactive_company_count = scalar(
                conn,
                f"SELECT COUNT(DISTINCT ticker) FROM dim_company WHERE is_active = 0 AND ticker IN ({internal_ph})",
                tuple(internal_tickers),
            )
        else:
            delisted_membership_count = 0
            inactive_company_count = 0
        if delisted_membership_count == len(delisted_rows):
            add_check(checks, status="pass", gate="delisted_membership_rows", detail=f"rows={delisted_membership_count}")
        else:
            add_check(checks, status="fail", gate="delisted_membership_rows", detail=f"expected={len(delisted_rows)} actual={delisted_membership_count}")
        if inactive_company_count == len(delisted_rows):
            add_check(checks, status="pass", gate="delisted_inactive_company_rows", detail=f"rows={inactive_company_count}")
        else:
            add_check(checks, status="fail", gate="delisted_inactive_company_rows", detail=f"expected={len(delisted_rows)} actual={inactive_company_count}")

        delisted_identity_errors = compare_delisted_identity(conn, delisted_rows=delisted_rows, model_family=model_family)
        if delisted_identity_errors:
            add_check(checks, status="fail", gate="delisted_identity_matches_csv", detail="; ".join(delisted_identity_errors[:10]))
        else:
            add_check(checks, status="pass", gate="delisted_identity_matches_csv", detail=f"tickers={len(delisted_rows)}")

        alias_count = scalar(conn, "SELECT COUNT(*) FROM dim_ticker_alias WHERE source_id = ?", (alias_source_id,))
        if alias_count == len(alias_rows):
            add_check(checks, status="pass", gate="ticker_alias_rows", detail=f"rows={alias_count}")
        else:
            add_check(checks, status="fail", gate="ticker_alias_rows", detail=f"expected={len(alias_rows)} actual={alias_count}")

        open_identity_errors = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM data_quality_issues
            WHERE severity = 'error'
              AND resolution_status = 'open'
              AND stage IN (
                    'defense_universe_load',
                    'defense_historical_membership_load',
                    'defense_ticker_alias_load',
                    'defense_identity_reconciliation'
              )
            """,
        )
        if open_identity_errors == 0:
            add_check(checks, status="pass", gate="open_identity_errors", detail="open_errors=0")
        else:
            add_check(checks, status="fail", gate="open_identity_errors", detail=f"open_errors={open_identity_errors}")

        market_symbols = sorted(set(active_tickers + benchmark_tickers))
        market_ph = placeholders(market_symbols)
        price_symbol_count = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_price_ohlcv WHERE source_id = ? AND ticker IN ({market_ph})",
            (market_source_id, *market_symbols),
        )
        snapshot_symbol_count = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_market_snapshot WHERE source_id = ? AND ticker IN ({market_ph})",
            (market_source_id, *market_symbols),
        )
        market_feature_asof = value(
            conn,
            "SELECT MAX(asof_date) FROM feature_market_technical WHERE source_id = ? AND model_family = ?",
            (market_source_id, model_family),
        )
        if market_feature_asof:
            market_feature_count = scalar(
                conn,
                f"""
                SELECT COUNT(DISTINCT ticker)
                FROM feature_market_technical
                WHERE source_id = ?
                  AND model_family = ?
                  AND asof_date = ?
                  AND ticker IN ({active_ph})
                """,
                (market_source_id, model_family, market_feature_asof, *active_tickers),
            )
        else:
            market_feature_count = 0
        if price_symbol_count == len(market_symbols):
            add_check(checks, status="pass", gate="stage3_price_symbol_coverage", detail=f"symbols={price_symbol_count} asof_symbols={len(market_symbols)}")
        else:
            add_check(checks, status="fail", gate="stage3_price_symbol_coverage", detail=f"expected={len(market_symbols)} actual={price_symbol_count}")
        if snapshot_symbol_count == len(market_symbols):
            add_check(checks, status="pass", gate="stage3_market_snapshot_coverage", detail=f"symbols={snapshot_symbol_count}")
        else:
            add_check(checks, status="fail", gate="stage3_market_snapshot_coverage", detail=f"expected={len(market_symbols)} actual={snapshot_symbol_count}")
        if market_feature_count == len(active_rows):
            add_check(checks, status="pass", gate="stage3_market_feature_rows", detail=f"asof={market_feature_asof} rows={market_feature_count}")
        else:
            add_check(checks, status="fail", gate="stage3_market_feature_rows", detail=f"asof={market_feature_asof or ''} expected={len(active_rows)} actual={market_feature_count}")

        profile_count = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM dim_issuer_reporting_profile WHERE model_family = ? AND ticker IN ({active_ph})",
            (model_family, *active_tickers),
        )
        financial_asof = value(
            conn,
            "SELECT MAX(asof_date) FROM feature_financial_statement WHERE source_id = ? AND model_family = ?",
            (companyfacts_source_id, model_family),
        )
        if financial_asof:
            financial_feature_count = scalar(
                conn,
                f"""
                SELECT COUNT(DISTINCT ticker)
                FROM feature_financial_statement
                WHERE source_id = ?
                  AND model_family = ?
                  AND asof_date = ?
                  AND ticker IN ({active_ph})
                """,
                (companyfacts_source_id, model_family, financial_asof, *active_tickers),
            )
            neutral_count = scalar(
                conn,
                f"""
                SELECT COUNT(*)
                FROM feature_financial_statement
                WHERE source_id = ?
                  AND model_family = ?
                  AND asof_date = ?
                  AND data_quality_status = 'neutral_low_confidence'
                  AND ticker IN ({active_ph})
                """,
                (companyfacts_source_id, model_family, financial_asof, *active_tickers),
            )
        else:
            financial_feature_count = 0
            neutral_count = 0
        raw_sec_count = scalar(conn, "SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE source_id = ?", (companyfacts_source_id,))
        mapped_sec_count = scalar(conn, "SELECT COUNT(*) FROM fact_sec_xbrl_fact WHERE source_id = ?", (companyfacts_source_id,))
        if profile_count == len(active_rows):
            add_check(checks, status="pass", gate="stage4_reporting_profiles", detail=f"rows={profile_count}")
        else:
            add_check(checks, status="fail", gate="stage4_reporting_profiles", detail=f"expected={len(active_rows)} actual={profile_count}")
        if financial_feature_count == len(active_rows):
            add_check(checks, status="pass", gate="stage4_financial_feature_rows", detail=f"asof={financial_asof} rows={financial_feature_count}")
        else:
            add_check(checks, status="fail", gate="stage4_financial_feature_rows", detail=f"asof={financial_asof or ''} expected={len(active_rows)} actual={financial_feature_count}")
        if raw_sec_count > 0 and mapped_sec_count > 0:
            add_check(checks, status="pass", gate="stage4_live_sec_fact_rows", detail=f"raw={raw_sec_count} mapped={mapped_sec_count}")
        elif args.require_live_sec_facts:
            add_check(checks, status="fail", gate="stage4_live_sec_fact_rows", detail=f"raw={raw_sec_count} mapped={mapped_sec_count}")
        else:
            add_check(checks, status="warn", gate="stage4_live_sec_fact_rows", detail=f"raw={raw_sec_count} mapped={mapped_sec_count}; financial rows may be fallback-only")
        if financial_feature_count == len(active_rows) and neutral_count == len(active_rows):
            add_check(checks, status="warn", gate="stage4_financial_fallback_mix", detail=f"all {neutral_count} rows are neutral_low_confidence")
        else:
            add_check(checks, status="pass", gate="stage4_financial_fallback_mix", detail=f"neutral_low_confidence={neutral_count} total={financial_feature_count}")

        if internal_tickers:
            internal_ph = placeholders(internal_tickers)
            norgate_delisted_coverage = scalar(
                conn,
                f"SELECT COUNT(DISTINCT ticker) FROM fact_price_ohlcv WHERE source_id = ? AND ticker IN ({internal_ph})",
                (norgate_source_id, *internal_tickers),
            )
        else:
            norgate_delisted_coverage = 0
        if norgate_delisted_coverage == len(delisted_rows):
            add_check(checks, status="pass", gate="stage8_delisted_norgate_price_history", detail=f"covered={norgate_delisted_coverage}")
        elif args.require_delisted_price_history:
            add_check(checks, status="fail", gate="stage8_delisted_norgate_price_history", detail=f"expected={len(delisted_rows)} actual={norgate_delisted_coverage}")
        else:
            add_check(
                checks,
                status="warn",
                gate="stage8_delisted_norgate_price_history",
                detail=f"expected={len(delisted_rows)} actual={norgate_delisted_coverage}; pending for true OOS calibration",
            )

    write_report(output_csv, checks)
    for row in checks:
        if row["status"] == "fail":
            errors += 1
            LOGGER.error("%s %s: %s", row["status"].upper(), row["gate"], row["detail"])
        elif row["status"] == "warn":
            LOGGER.warning("%s %s: %s", row["status"].upper(), row["gate"], row["detail"])
        else:
            LOGGER.info("%s %s: %s", row["status"].upper(), row["gate"], row["detail"])
    LOGGER.info("Wrote production readiness report: %s", output_csv)
    if errors:
        LOGGER.error("Defense Stage 0-4 production readiness failed: errors=%d", errors)
        return 1
    LOGGER.info("Defense Stage 0-4 production readiness passed with warnings allowed: checks=%d", len(checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(validate())
