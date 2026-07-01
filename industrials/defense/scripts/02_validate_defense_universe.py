#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.csv_utils import load_yaml_map, read_csv_flexible, row_get  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.text_norm import as_bool, normalize_cik, normalize_org_name, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_defense_universe")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LOAD_STAGE = "defense_universe_load"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the loaded active defense universe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--universe-csv", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--cohorts", type=Path, default=None)
    parser.add_argument("--model-family", default="")
    return parser.parse_args()


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def load_cohort_tickers(path: Path) -> dict[str, str]:
    data = load_yaml_map(path)
    cohorts = data.get("cohorts")
    if not isinstance(cohorts, list):
        raise ValueError(f"{path} must contain a cohorts list.")
    out: dict[str, str] = {}
    for raw in cohorts:
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid cohort mapping in {path}")
        cohort_id = str(raw.get("cohort_id") or "").strip()
        for raw_ticker in raw.get("tickers") or []:
            ticker = normalize_ticker(raw_ticker)
            if not ticker:
                continue
            if ticker in out:
                raise ValueError(f"Ticker {ticker} appears in multiple cohorts.")
            out[ticker] = cohort_id
    return out


def csv_rows_by_ticker(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    rows = read_csv_flexible(path)
    out: dict[str, dict[str, str]] = {}
    duplicates: list[str] = []
    for row in rows:
        ticker = normalize_ticker(row_get(row, "ticker"))
        if not ticker:
            continue
        if ticker in out:
            duplicates.append(ticker)
        out[ticker] = row
    return out, duplicates


def validate_shared_cik_groups(rows_by_ticker: dict[str, dict[str, str]]) -> list[str]:
    errors: list[str] = []
    by_cik: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows_by_ticker.values():
        cik = normalize_cik(row_get(row, "cik"))
        if cik:
            by_cik[cik].append(row)
    for cik, rows in sorted(by_cik.items()):
        if len(rows) <= 1:
            continue
        names = {normalize_org_name(row_get(row, "company_name", "company")) for row in rows}
        primary_count = sum(1 for row in rows if as_bool(row_get(row, "is_primary_listing"), default=True))
        tickers = [normalize_ticker(row_get(row, "ticker")) for row in rows]
        if len(names) > 1:
            errors.append(f"Shared active CIK {cik} has conflicting company names: {tickers}")
        if primary_count != 1:
            errors.append(f"Shared active CIK {cik} should have exactly one primary listing; found {primary_count}: {tickers}")
    return errors


def load_cik_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker"))
        cik = normalize_cik(row_get(row, "cik"))
        applies_to = (row_get(row, "applies_to") or "both").lower()
        if ticker and cik and applies_to in {"active", "both"}:
            if ticker in out:
                raise ValueError(f"{path}: duplicate CIK override ticker={ticker}")
            out[ticker] = cik
    return out


def apply_active_cik_overrides(rows_by_ticker: dict[str, dict[str, str]], overrides: dict[str, str]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for ticker, row in rows_by_ticker.items():
        new_row = dict(row)
        if ticker in overrides:
            new_row["cik"] = overrides[ticker]
        out[ticker] = new_row
    return out


def validate_universe() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = args.universe_csv.expanduser().resolve() if args.universe_csv else resolve_path(cfg_get(config, "industrials_universe.seed_csv"), base_dir=base_dir)
    policy_path = args.policy.expanduser().resolve() if args.policy else resolve_path(cfg_get(config, "industrials_universe.policy_path"), base_dir=base_dir)
    cohort_path = args.cohorts.expanduser().resolve() if args.cohorts else resolve_path(cfg_get(config, "industrials_universe.cohort_path"), base_dir=base_dir)
    cik_overrides_path = resolve_path(cfg_get(config, "industrials_universe.cik_ticker_overrides_csv"), base_dir=base_dir)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense")).strip()
    seed_source_id = str(cfg_get(config, "industrials_universe.seed_source_id", "defense_ticker_seed"))
    policy = load_yaml_map(policy_path)
    rows_by_ticker, duplicates = csv_rows_by_ticker(universe_csv)
    rows_by_ticker = apply_active_cik_overrides(rows_by_ticker, load_cik_overrides(cik_overrides_path))
    unique_tickers = sorted(rows_by_ticker)
    ticker_params = tuple(unique_tickers)
    ph = placeholders(unique_tickers)
    cohort_map = load_cohort_tickers(cohort_path)
    required_non_cik = [str(field) for field in policy.get("required_non_cik_fields", [])]
    expected_ticker_count = int(policy.get("expected_ticker_count") or cfg_get(config, "industrials_universe.expected_ticker_count", 0) or 0)
    allow_unassigned = bool(policy.get("allow_unassigned_cohort", False))
    unassigned_id = str(policy.get("default_unassigned_cohort_id") or "defense_unassigned").strip()

    errors: list[str] = []
    infos: list[str] = []
    if duplicates:
        errors.append(f"Duplicate CSV tickers: {sorted(set(duplicates))}")
    if expected_ticker_count > 0 and len(unique_tickers) != expected_ticker_count:
        errors.append(f"Source-of-truth ticker count mismatch: expected={expected_ticker_count} actual={len(unique_tickers)}")
    csv_required_missing: list[str] = []
    for ticker, row in rows_by_ticker.items():
        for field in required_non_cik:
            if field == "ticker":
                continue
            if allow_unassigned and field in {"defense_calibration_cohort", "calibration_cohort"}:
                continue
            if not row_get(row, field, field.title()):
                csv_required_missing.append(f"{ticker}:{field}")
    if csv_required_missing:
        errors.append(f"CSV missing required non-CIK fields: {csv_required_missing[:20]}")
    cohort_missing_from_csv = sorted(set(cohort_map).difference(unique_tickers))
    if cohort_missing_from_csv:
        errors.append(f"Cohort tickers missing from CSV: {cohort_missing_from_csv}")
    csv_missing_from_cohorts = sorted(set(unique_tickers).difference(cohort_map))
    if csv_missing_from_cohorts and not allow_unassigned:
        errors.append(f"CSV tickers missing from cohort YAML: {csv_missing_from_cohorts}")
    elif csv_missing_from_cohorts:
        infos.append(f"CSV tickers assigned to unassigned cohort {unassigned_id}: {csv_missing_from_cohorts}")
    errors.extend(validate_shared_cik_groups(rows_by_ticker))

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        company_count = scalar(conn, f"SELECT COUNT(*) FROM dim_company WHERE ticker IN ({ph})", ticker_params)
        security_count = scalar(conn, f"SELECT COUNT(*) FROM dim_security WHERE ticker IN ({ph})", ticker_params)
        taxonomy_count = scalar(
            conn,
            f"SELECT COUNT(*) FROM dim_industrials_taxonomy WHERE model_family = ? AND ticker IN ({ph})",
            (model_family, *ticker_params),
        )
        current_membership_count = scalar(
            conn,
            f"""
            SELECT COUNT(DISTINCT ticker)
            FROM dim_universe_membership
            WHERE model_family = ?
              AND membership_source_id = ?
              AND is_current_member = 1
              AND ticker IN ({ph})
            """,
            (model_family, seed_source_id, *ticker_params),
        )
        if company_count != len(unique_tickers):
            errors.append(f"dim_company count mismatch: db={company_count} csv_unique={len(unique_tickers)}")
        if security_count != len(unique_tickers):
            errors.append(f"dim_security count mismatch: db={security_count} csv_unique={len(unique_tickers)}")
        if taxonomy_count != len(unique_tickers):
            errors.append(f"dim_industrials_taxonomy count mismatch: db={taxonomy_count} csv_unique={len(unique_tickers)}")
        if current_membership_count != len(unique_tickers):
            errors.append(f"current membership count mismatch: db={current_membership_count} csv_unique={len(unique_tickers)}")

        db_rows = conn.execute(
            f"""
            SELECT c.ticker, c.cik, c.company_name, c.sector, c.industry, c.subsector,
                   c.country, c.currency, c.universe_status, c.is_active,
                   s.exchange, s.security_type, s.listing_status, s.is_primary_listing,
                   t.calibration_cohort_id, t.development_stage
            FROM dim_company c
            JOIN dim_security s ON s.ticker = c.ticker
            JOIN dim_industrials_taxonomy t ON t.ticker = c.ticker AND t.model_family = ?
            WHERE c.ticker IN ({ph})
            """,
            (model_family, *ticker_params),
        ).fetchall()
        db_by_ticker = {str(row["ticker"]): row for row in db_rows}
        for ticker, csv_row in rows_by_ticker.items():
            db_row = db_by_ticker.get(ticker)
            if db_row is None:
                continue
            expected_values = {
                "cik": normalize_cik(row_get(csv_row, "cik")),
                "company_name": row_get(csv_row, "company_name"),
                "sector": row_get(csv_row, "sector"),
                "industry": row_get(csv_row, "industry"),
                "subsector": row_get(csv_row, "subsector"),
                "country": row_get(csv_row, "country"),
                "currency": row_get(csv_row, "currency"),
                "exchange": row_get(csv_row, "exchange"),
                "security_type": row_get(csv_row, "security_type"),
                "listing_status": row_get(csv_row, "listing_status"),
                "calibration_cohort_id": cohort_map.get(ticker)
                or (unassigned_id if allow_unassigned else row_get(csv_row, "defense_calibration_cohort")),
            }
            for field, expected in expected_values.items():
                actual = str(db_row[field] or "")
                if actual != expected:
                    errors.append(f"{ticker}: DB {field}={actual!r} does not match CSV {expected!r}")
            expected_primary = 1 if as_bool(row_get(csv_row, "is_primary_listing"), default=True) else 0
            if int(db_row["is_primary_listing"]) != expected_primary:
                errors.append(f"{ticker}: DB is_primary_listing={db_row['is_primary_listing']} does not match CSV {expected_primary}")
            expected_cohort = cohort_map.get(ticker)
            if expected_cohort is None and allow_unassigned:
                expected_cohort = unassigned_id
            if expected_cohort != str(db_row["calibration_cohort_id"]):
                errors.append(f"{ticker}: DB cohort {db_row['calibration_cohort_id']} does not match expected cohort {expected_cohort}")

        missing_cik_count = scalar(conn, f"SELECT COUNT(*) FROM dim_company WHERE ticker IN ({ph}) AND COALESCE(cik, '') = ''", ticker_params)
        incomplete_issue_count = scalar(
            conn,
            f"""
            SELECT COUNT(*) FROM data_quality_issues
            WHERE stage = ? AND issue_type = 'incomplete_identity' AND ticker IN ({ph})
            """,
            (LOAD_STAGE, *ticker_params),
        )
        if incomplete_issue_count != missing_cik_count:
            errors.append(f"incomplete_identity issue mismatch: issues={incomplete_issue_count} missing_cik={missing_cik_count}")

        infos.append(f"CSV rows={len(rows_by_ticker)} unique_tickers={len(unique_tickers)}")
        infos.append(f"DB companies={company_count} securities={security_count} taxonomy={taxonomy_count}")
        infos.append(f"Current membership rows={current_membership_count}")
        infos.append(f"Missing CIK rows={missing_cik_count}")

    for message in infos:
        LOGGER.info(message)
    if errors:
        for message in errors:
            LOGGER.error(message)
        return 1
    LOGGER.info("Defense universe validation passed for model_family=%s", model_family)
    return 0


if __name__ == "__main__":
    raise SystemExit(validate_universe())
