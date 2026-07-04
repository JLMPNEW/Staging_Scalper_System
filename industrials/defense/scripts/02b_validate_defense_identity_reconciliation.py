#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.csv_utils import read_csv_flexible, row_get  # noqa: E402
from industrials.core.db import connect, init_db, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.text_norm import as_bool, normalize_cik, normalize_org_name, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_defense_identity_reconciliation")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LOAD_STAGE = "defense_identity_reconciliation"
# EL-11: the only override kinds the CIK/ticker overrides CSV is allowed to carry.
# Extend this set deliberately when a new override kind is introduced.
VALID_OVERRIDE_TYPES = frozenset({"verified_sec_cik_correction", "verified_sec_cik_confirmed"})


@dataclass(frozen=True)
class WarningRecord:
    message: str
    tickers: tuple[str, ...] = ()
    issue_type: str = "identity_reconciliation_warning"
    severity: str = "warning"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate defense ticker/CIK/company identity reconciliation.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--active-csv", type=Path, default=None)
    parser.add_argument("--delisted-csv", type=Path, default=None)
    parser.add_argument("--aliases-csv", type=Path, default=None)
    parser.add_argument("--overrides-csv", type=Path, default=None)
    parser.add_argument("--sec-company-tickers-json", type=Path, default=None)
    parser.add_argument("--asof", default="", help="As-of date for effective-date alias checks. Defaults to today UTC.")
    return parser.parse_args()


def parse_asof(raw: str) -> str:
    if raw:
        try:
            datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"Invalid --asof date: {raw}") from exc
        return raw[:10]
    return date.today().isoformat()


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def rows_by_ticker(path: Path, *, ticker_field: str = "ticker") -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    duplicates: list[str] = []
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, ticker_field, "ticker"))
        if not ticker:
            continue
        if ticker in out:
            duplicates.append(ticker)
        out[ticker] = row
    if duplicates:
        raise ValueError(f"{path} contains duplicate tickers: {sorted(set(duplicates))}")
    return out


def load_overrides(path: Path) -> set[tuple[str, str]]:
    overrides: set[tuple[str, str]] = set()
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker"))
        override_type = row_get(row, "override_type")
        applies_to = (row_get(row, "applies_to") or "both").lower()
        if ticker and override_type:
            overrides.add((ticker, applies_to))
            overrides.add((ticker, "both"))
    return overrides


def load_cik_overrides(path: Path) -> dict[str, tuple[str, str]]:
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


def validate_override_approvals(path: Path) -> list[WarningRecord]:
    """EL-11: value checks for the overrides CSV approval metadata.

    approved_by must be non-empty, approved_date must parse as YYYY-MM-DD and not
    postdate today (UTC wall clock: approvals are file hygiene, not PIT-gated), and
    override_type must be one of VALID_OVERRIDE_TYPES. Failures are review-level
    data-quality issues persisted per ticker, never silent passes.
    """
    warnings: list[WarningRecord] = []
    today = date.today()
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker"))
        if not ticker:
            continue
        problems: list[str] = []
        if not str(row_get(row, "approved_by") or "").strip():
            problems.append("approved_by is empty")
        approved_raw = str(row_get(row, "approved_date") or "").strip()
        approved_date: date | None = None
        try:
            approved_date = datetime.strptime(approved_raw[:10], "%Y-%m-%d").date()
        except ValueError:
            problems.append(f"approved_date {approved_raw!r} is not a parseable YYYY-MM-DD date")
        if approved_date is not None and approved_date > today:
            problems.append(f"approved_date {approved_date.isoformat()} is in the future (today={today.isoformat()})")
        override_type = str(row_get(row, "override_type") or "").strip()
        if override_type not in VALID_OVERRIDE_TYPES:
            problems.append(f"override_type {override_type!r} not in {sorted(VALID_OVERRIDE_TYPES)}")
        if problems:
            warnings.append(
                WarningRecord(
                    message=f"{path.name} {ticker}: override approval metadata review: {'; '.join(problems)}",
                    tickers=(ticker,),
                    issue_type="override_approval_metadata_review",
                )
            )
    return warnings


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


def has_override(overrides: set[tuple[str, str]], ticker: str, scope: str) -> bool:
    return (ticker, scope) in overrides or (ticker, "both") in overrides


def add_issue(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    issue_type: str,
    issue_detail: str,
    severity: str = "warning",
) -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ? LIMIT 1", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, LOAD_STAGE, ticker, company_id, issue_type, issue_detail, now, now),
    )


def load_sec_mapping(path: Path | None) -> dict[str, tuple[str, str]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    if isinstance(payload, dict) and isinstance(payload.get("fields"), list) and isinstance(payload.get("data"), list):
        fields = [str(field) for field in payload["fields"]]
        for raw_row in payload["data"]:
            row = {field: raw_row[idx] if idx < len(raw_row) else "" for idx, field in enumerate(fields)}
            ticker = normalize_ticker(row.get("ticker"))
            cik = normalize_cik(row.get("cik"))
            name = str(row.get("name") or row.get("title") or "")
            if ticker and cik:
                out[ticker] = (cik, name)
    elif isinstance(payload, dict):
        for raw in payload.values():
            if not isinstance(raw, dict):
                continue
            ticker = normalize_ticker(raw.get("ticker"))
            cik = normalize_cik(raw.get("cik_str") or raw.get("cik"))
            name = str(raw.get("title") or raw.get("name") or "")
            if ticker and cik:
                out[ticker] = (cik, name)
    else:
        raise ValueError(f"Unsupported SEC ticker mapping JSON shape: {path}")
    return out


def validate_active_rows(
    conn: sqlite3.Connection,
    *,
    active_rows: dict[str, dict[str, str]],
    overrides: set[tuple[str, str]],
    sec_mapping: dict[str, tuple[str, str]],
) -> tuple[list[str], list[WarningRecord]]:
    errors: list[str] = []
    warnings: list[WarningRecord] = []
    tickers = sorted(active_rows)
    ph = placeholders(tickers)
    db_rows = conn.execute(
        f"""
        SELECT ticker, cik, company_name, sector, industry, subsector, country, currency
        FROM dim_company
        WHERE ticker IN ({ph})
        """,
        tuple(tickers),
    ).fetchall()
    db_by_ticker = {str(row["ticker"]): row for row in db_rows}
    if len(db_by_ticker) != len(tickers):
        missing = sorted(set(tickers).difference(db_by_ticker))
        errors.append(f"Active tickers missing from dim_company: {missing}")
    for ticker, csv_row in active_rows.items():
        db_row = db_by_ticker.get(ticker)
        if db_row is None:
            continue
        expected_cik = normalize_cik(row_get(csv_row, "cik"))
        expected_name = row_get(csv_row, "company_name")
        mismatches: list[str] = []
        if str(db_row["cik"] or "") != expected_cik:
            mismatches.append(f"cik db={db_row['cik']!r} csv={expected_cik!r}")
        if str(db_row["company_name"] or "") != expected_name:
            mismatches.append(f"company_name db={db_row['company_name']!r} csv={expected_name!r}")
        if mismatches:
            message = f"{ticker}: active identity mismatch: {', '.join(mismatches)}"
            if has_override(overrides, ticker, "active"):
                warnings.append(WarningRecord(message=f"OVERRIDE: {message}", tickers=(ticker,), issue_type="identity_override_warning"))
            else:
                errors.append(message)
        if ticker in sec_mapping:
            sec_cik, sec_name = sec_mapping[ticker]
            if expected_cik and sec_cik and expected_cik != sec_cik:
                message = f"{ticker}: CSV CIK {expected_cik} does not match SEC mapping {sec_cik}"
                if has_override(overrides, ticker, "active"):
                    warnings.append(WarningRecord(message=f"OVERRIDE: {message}", tickers=(ticker,), issue_type="identity_override_warning"))
                else:
                    errors.append(message)
            if sec_name and expected_name and normalize_org_name(sec_name) != normalize_org_name(expected_name):
                warnings.append(
                    WarningRecord(
                        message=f"{ticker}: SEC name differs from CSV name: SEC={sec_name!r} CSV={expected_name!r}",
                        tickers=(ticker,),
                        issue_type="sec_name_differs_from_csv",
                    )
                )
    return errors, warnings


def validate_delisted_rows(
    conn: sqlite3.Connection,
    *,
    delisted_rows: dict[str, dict[str, str]],
    overrides: set[tuple[str, str]],
) -> tuple[list[str], list[WarningRecord]]:
    errors: list[str] = []
    warnings: list[WarningRecord] = []
    tickers = sorted(delisted_rows)
    ph = placeholders(tickers)
    seed_count = scalar(conn, f"SELECT COUNT(*) FROM dim_delisted_calibration_seed WHERE ticker IN ({ph})", tuple(tickers))
    if seed_count != len(tickers):
        errors.append(f"dim_delisted_calibration_seed count mismatch: db={seed_count} csv={len(tickers)}")
    db_rows = conn.execute(
        f"""
        SELECT ticker, company_name, calibration_cohort_id, cik, exit_year
        FROM dim_delisted_calibration_seed
        WHERE ticker IN ({ph})
        """,
        tuple(tickers),
    ).fetchall()
    db_by_ticker = {str(row["ticker"]): row for row in db_rows}
    for ticker, csv_row in delisted_rows.items():
        db_row = db_by_ticker.get(ticker)
        if db_row is None:
            continue
        expected = {
            "company_name": row_get(csv_row, "company", "company_name"),
            "calibration_cohort_id": row_get(csv_row, "cohort"),
            "cik": normalize_cik(row_get(csv_row, "cik")),
            "exit_year": row_get(csv_row, "exit_year"),
        }
        mismatches: list[str] = []
        for field, expected_value in expected.items():
            actual = str(db_row[field] or "")
            if field == "exit_year":
                actual = actual.split(".")[0]
            if actual != expected_value:
                mismatches.append(f"{field} db={actual!r} csv={expected_value!r}")
        if mismatches:
            message = f"{ticker}: delisted identity mismatch: {', '.join(mismatches)}"
            if has_override(overrides, ticker, "delisted"):
                warnings.append(WarningRecord(message=f"OVERRIDE: {message}", tickers=(ticker,), issue_type="identity_override_warning"))
            else:
                errors.append(message)
    return errors, warnings


def shared_cik_checks(
    *,
    active_rows: dict[str, dict[str, str]],
    delisted_rows: dict[str, dict[str, str]],
) -> tuple[list[str], list[WarningRecord]]:
    errors: list[str] = []
    warnings: list[WarningRecord] = []
    active_by_cik: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    delisted_by_cik: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ticker, row in active_rows.items():
        cik = normalize_cik(row_get(row, "cik"))
        if cik:
            active_by_cik[cik].append(
                (
                    ticker,
                    row_get(row, "company_name"),
                    1 if as_bool(row_get(row, "is_primary_listing"), default=True) else 0,
                )
            )
    for ticker, row in delisted_rows.items():
        cik = normalize_cik(row_get(row, "cik"))
        if cik:
            delisted_by_cik[cik].append((ticker, row_get(row, "company", "company_name")))
    for cik, values in sorted(active_by_cik.items()):
        if len(values) > 1:
            names = {normalize_org_name(name) for _, name, _ in values}
            tickers = tuple(ticker for ticker, _, _ in values)
            primary_count = sum(primary for _, _, primary in values)
            if len(names) > 1:
                errors.append(f"Shared active CIK {cik} has conflicting company names: {list(tickers)}")
            if primary_count != 1:
                errors.append(f"Shared active CIK {cik} should have exactly one primary listing; found {primary_count}: {list(tickers)}")
            if len(names) == 1 and primary_count == 1:
                warnings.append(
                    WarningRecord(
                        message=f"Shared active CIK {cik} treated as same-issuer share class: {list(tickers)}",
                        tickers=tickers,
                        issue_type="shared_active_cik_share_class",
                    )
                )
    for cik, values in sorted(delisted_by_cik.items()):
        if len(values) > 1:
            warnings.append(
                WarningRecord(
                    message=f"Shared delisted CIK {cik} treated as predecessor/successor lineage review: {values}",
                    tickers=tuple(ticker for ticker, _ in values),
                    issue_type="shared_delisted_cik_lineage",
                )
            )
    for cik in sorted(set(active_by_cik).intersection(delisted_by_cik)):
        active_values = [(ticker, name) for ticker, name, _ in active_by_cik[cik]]
        delisted_values = delisted_by_cik[cik]
        warnings.append(
            WarningRecord(
                message=f"Shared active/delisted CIK {cik} treated as lineage review: active={active_values} delisted={delisted_values}",
                tickers=tuple([ticker for ticker, _ in active_values] + [ticker for ticker, _ in delisted_values]),
                issue_type="shared_active_delisted_cik_lineage",
            )
        )
    return errors, warnings


def validate_aliases(
    conn: sqlite3.Connection,
    *,
    aliases_csv: Path,
    active_rows: dict[str, dict[str, str]],
    asof: str,
) -> tuple[list[str], list[WarningRecord]]:
    errors: list[str] = []
    warnings: list[WarningRecord] = []
    aliases = read_csv_flexible(aliases_csv)
    for raw in aliases:
        contract_ticker = normalize_ticker(row_get(raw, "contract_ticker"))
        active_ticker = normalize_ticker(row_get(raw, "active_ticker"))
        effective_date = row_get(raw, "effective_date")[:10]
        if not any((contract_ticker, active_ticker, effective_date)):
            continue
        if not contract_ticker or not active_ticker or not effective_date:
            errors.append(f"Alias row must include contract_ticker, active_ticker and effective_date: {raw}")
            continue
        db_row = conn.execute(
            """
            SELECT active_ticker, effective_date, verified_flag
            FROM dim_ticker_alias
            WHERE contract_ticker = ? AND effective_date = ?
            """,
            (contract_ticker, effective_date),
        ).fetchone()
        if db_row is None:
            errors.append(f"Alias {contract_ticker} effective {effective_date} was not loaded into dim_ticker_alias")
            continue
        if str(db_row["active_ticker"]) != active_ticker:
            errors.append(f"Alias {contract_ticker}: DB active_ticker={db_row['active_ticker']} CSV active_ticker={active_ticker}")
        verified = as_bool(row_get(raw, "verified_flag"))
        if verified and int(db_row["verified_flag"]) != 1:
            errors.append(f"Alias {contract_ticker}: CSV verified but DB verified_flag is not 1")
        if effective_date <= asof and contract_ticker in active_rows and active_ticker in active_rows and contract_ticker != active_ticker:
            errors.append(
                f"Alias {contract_ticker}->{active_ticker} is effective {effective_date}, but both tickers remain in active defense CSV as of {asof}."
            )
        elif effective_date <= asof:
            warnings.append(
                WarningRecord(
                    message=(
                        f"Alias {contract_ticker}->{active_ticker} is effective as of {effective_date}; "
                        "market-data and portfolio handoff should route through the active ticker."
                    ),
                    tickers=(contract_ticker,),
                    issue_type="effective_ticker_alias_routing",
                    severity="info",
                )
            )
        elif effective_date > asof:
            warnings.append(
                WarningRecord(
                    message=f"Alias {contract_ticker}->{active_ticker} is future-dated relative to asof {asof}; not active yet.",
                    tickers=(contract_ticker,),
                    issue_type="future_dated_ticker_alias",
                )
            )
    return errors, warnings


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    active_csv = args.active_csv.expanduser().resolve() if args.active_csv else resolve_path(cfg_get(config, "industrials_universe.seed_csv"), base_dir=base_dir)
    delisted_csv = args.delisted_csv.expanduser().resolve() if args.delisted_csv else resolve_path(cfg_get(config, "industrials_universe.delisted_seed_csv"), base_dir=base_dir)
    aliases_csv = args.aliases_csv.expanduser().resolve() if args.aliases_csv else resolve_path(cfg_get(config, "industrials_universe.ticker_aliases_csv"), base_dir=base_dir)
    overrides_csv = args.overrides_csv.expanduser().resolve() if args.overrides_csv else resolve_path(cfg_get(config, "industrials_universe.cik_ticker_overrides_csv"), base_dir=base_dir)
    sec_mapping_path = args.sec_company_tickers_json.expanduser().resolve() if args.sec_company_tickers_json else None
    active_rows = rows_by_ticker(active_csv)
    delisted_rows = rows_by_ticker(delisted_csv)
    overrides = load_overrides(overrides_csv)
    cik_overrides = load_cik_overrides(overrides_csv)
    active_rows = apply_cik_overrides(active_rows, cik_overrides, scope="active")
    delisted_rows = apply_cik_overrides(delisted_rows, cik_overrides, scope="delisted")
    sec_mapping = load_sec_mapping(sec_mapping_path)

    errors: list[str] = []
    warnings: list[WarningRecord] = []
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        with conn:
            conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", (LOAD_STAGE,))
        active_errors, active_warnings = validate_active_rows(conn, active_rows=active_rows, overrides=overrides, sec_mapping=sec_mapping)
        delisted_errors, delisted_warnings = validate_delisted_rows(conn, delisted_rows=delisted_rows, overrides=overrides)
        alias_errors, alias_warnings = validate_aliases(conn, aliases_csv=aliases_csv, active_rows=active_rows, asof=asof)
        errors.extend(active_errors)
        errors.extend(delisted_errors)
        errors.extend(alias_errors)
        warnings.extend(active_warnings)
        warnings.extend(delisted_warnings)
        warnings.extend(alias_warnings)
        shared_errors, shared_warnings = shared_cik_checks(active_rows=active_rows, delisted_rows=delisted_rows)
        errors.extend(shared_errors)
        warnings.extend(shared_warnings)
        warnings.extend(validate_override_approvals(overrides_csv))
        if not sec_mapping:
            warnings.append(
                WarningRecord(
                    message="No external SEC company_tickers JSON supplied; identity validation is DB-vs-system-CSV only for this run.",
                    issue_type="external_sec_mapping_not_supplied",
                )
            )
        with conn:
            for warning in warnings:
                tickers = warning.tickers or ("",)
                for ticker in tickers:
                    add_issue(
                        conn,
                        ticker=ticker,
                        issue_type=warning.issue_type,
                        issue_detail=warning.message,
                        severity=warning.severity,
                    )

    for warning in warnings:
        if warning.severity == "info":
            LOGGER.info(warning.message)
        else:
            LOGGER.warning(warning.message)
    if errors:
        for message in errors:
            LOGGER.error(message)
        return 1
    LOGGER.info(
        "Defense identity reconciliation passed: active=%d delisted=%d overrides=%d asof=%s",
        len(active_rows),
        len(delisted_rows),
        len(overrides),
        asof,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
