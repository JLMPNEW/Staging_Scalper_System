#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect, init_db  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CANDIDATES = PACKAGE_ROOT / "data" / "delisted_biotech_calibration_candidates.csv"
DEFAULT_MAPPING = PACKAGE_ROOT / "data" / "delisted_biotech_calibration_universe.csv"
DEFAULT_SEC_UNIVERSE = PACKAGE_ROOT / "data" / "delisted_biotech_sec_sync_universe.csv"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "biotech_index_reports" / "delisted_calibration_universe"

ALLOWED_CALIBRATION_COHORTS = frozenset(
    {
        "commercial_profitable_quality_or_mature",
        "commercial_turnaround_or_unprofitable_growth",
        "late_clinical_pivotal_or_registrational",
        "platform_partnered_modality_pipeline",
        "early_clinical_speculative_or_single_asset_pipeline",
    }
)

EXCLUDED_VERIFICATION_STATUSES = frozenset(
    {
        "active_stock_exception_do_not_promote",
        "identity_conflict_do_not_promote",
    }
)

MAPPING_FIELDS = [
    "ticker",
    "calibration_company_ticker",
    "company_name",
    "cik",
    "cusip",
    "share_class_figi",
    "norgate_symbol",
    "cohort",
    "price_start_date",
    "price_end_date",
    "terminal_date",
    "recovery_type",
    "equity_recovery",
    "terminal_recovery_effective",
    "terminal_treatment_status",
    "terminal_consideration",
    "drop_otc_tape",
    "verification_status",
    "source_candidate_row_status",
]

SEC_UNIVERSE_FIELDS = [
    "ticker",
    "scoring_include",
    "original_ticker",
    "company_name",
    "cik",
    "calibration_only",
    "source",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze delisted biotech candidates into a calibration-only universe, "
            "upsert inactive company rows keyed by Norgate symbols, and write the "
            "SEC sync universe used for delisted filing/companyfacts backfills."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--mapping-csv", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--sec-universe-csv", type=Path, default=DEFAULT_SEC_UNIVERSE)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def configure_logging() -> None:
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_asof(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return date.today().isoformat()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10]).isoformat()


def compact_date(raw: str) -> str:
    return parse_asof(raw).replace("-", "")


def normalize_cik(raw: object) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits.zfill(10) if digits else ""


def normalize_cusip(raw: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())[:9]


def parse_float(raw: object) -> float | None:
    text = str(raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def read_candidates(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Candidate CSV has no header: {path}")
        rows: list[dict[str, str]] = []
        for row in reader:
            clean = {str(key): str(value or "").strip() for key, value in row.items()}
            clean["ticker"] = normalize_ticker(clean.get("ticker"))
            clean["cik"] = normalize_cik(clean.get("cik"))
            clean["cusip"] = normalize_cusip(clean.get("cusip") or clean.get("cusip_or_figi"))
            clean["norgate_symbol"] = normalize_ticker(clean.get("norgate_symbol"))
            rows.append(clean)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def strict_scope_reasons(row: dict[str, str]) -> list[str]:
    reasons: list[str] = []
    if not row.get("ticker"):
        reasons.append("missing_ticker")
    if row.get("proposed_cohort") not in ALLOWED_CALIBRATION_COHORTS:
        reasons.append("invalid_cohort")
    if row.get("verification_status") in EXCLUDED_VERIFICATION_STATUSES:
        reasons.append(str(row.get("verification_status")))
    if not row.get("norgate_symbol"):
        reasons.append("missing_norgate_symbol")
    if not row.get("cik"):
        reasons.append("missing_cik")
    if not row.get("cusip"):
        reasons.append("missing_cusip")
    try:
        if int(float(row.get("norgate_bar_count") or 0)) < 252:
            reasons.append("insufficient_norgate_bars")
    except ValueError:
        reasons.append("invalid_norgate_bar_count")
    return reasons


def parsed_cash_only_recovery(row: dict[str, str]) -> tuple[float | None, str]:
    explicit = parse_float(row.get("equity_recovery"))
    recovery_type = str(row.get("recovery_type") or "").strip().lower()
    if explicit is not None:
        return max(0.0, explicit), "explicit_equity_recovery"
    if recovery_type == "wipeout":
        return 0.0, "wipeout_hard_zero"
    component_cash = parse_float(row.get("terminal_cash_per_share"))
    component_stock_ratio = parse_float(row.get("terminal_stock_exchange_ratio"))
    component_stock_price = parse_float(row.get("terminal_stock_reference_price"))
    component_cvr_value = parse_float(row.get("terminal_cvr_value_per_share"))
    has_component = any(
        value is not None
        for value in (component_cash, component_stock_ratio, component_stock_price, component_cvr_value)
    )
    if has_component:
        if component_stock_ratio is not None and component_stock_ratio > 0.0 and component_stock_price is None:
            return None, "requires_explicit_terminal_recovery"
        recovery = float(component_cash or 0.0)
        recovery += float(component_stock_ratio or 0.0) * float(component_stock_price or 0.0)
        recovery += float(component_cvr_value or 0.0)
        return max(0.0, recovery), "component_terminal_recovery"
    consideration = str(row.get("terminal_consideration") or "").strip().lower()
    if "cash per share" in consideration and not any(token in consideration for token in ("plus", "stock", "cvr", "mix")):
        match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", str(row.get("terminal_consideration") or ""))
        if match:
            return float(match.group(1)), "parsed_cash_only_consideration"
    return None, "requires_explicit_terminal_recovery"


def build_mapping_rows(candidates: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mapping_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    seen_company_tickers: set[str] = set()
    for row in candidates:
        reasons = strict_scope_reasons(row)
        if reasons:
            rejected_rows.append({"ticker": row.get("ticker", ""), "reasons": "|".join(reasons)})
            continue
        calibration_company_ticker = row["norgate_symbol"]
        if calibration_company_ticker in seen_company_tickers:
            raise ValueError(f"Duplicate calibration company ticker generated: {calibration_company_ticker}")
        seen_company_tickers.add(calibration_company_ticker)
        effective_recovery, recovery_status = parsed_cash_only_recovery(row)
        mapping_rows.append(
            {
                "ticker": row["ticker"],
                "calibration_company_ticker": calibration_company_ticker,
                "company_name": row.get("company_name", ""),
                "cik": row.get("cik", ""),
                "cusip": row.get("cusip", ""),
                "share_class_figi": row.get("share_class_figi", ""),
                "norgate_symbol": row.get("norgate_symbol", ""),
                "cohort": row.get("proposed_cohort", ""),
                "price_start_date": row.get("price_start_date", ""),
                "price_end_date": row.get("price_end_date", ""),
                "terminal_date": row.get("terminal_date", ""),
                "recovery_type": row.get("recovery_type", ""),
                "equity_recovery": row.get("equity_recovery", ""),
                "terminal_recovery_effective": "" if effective_recovery is None else f"{effective_recovery:.6f}",
                "terminal_treatment_status": recovery_status,
                "terminal_consideration": row.get("terminal_consideration", ""),
                "drop_otc_tape": str(as_bool(row.get("drop_otc_tape"))).lower(),
                "verification_status": row.get("verification_status", ""),
                "source_candidate_row_status": "strict_usable",
            }
        )
    return sorted(mapping_rows, key=lambda item: str(item["ticker"])), rejected_rows


def ensure_delisted_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delisted_calibration_universe (
            ticker TEXT PRIMARY KEY,
            calibration_company_ticker TEXT NOT NULL UNIQUE,
            company_id INTEGER,
            company_name TEXT NOT NULL,
            cik TEXT NOT NULL,
            cusip TEXT NOT NULL,
            share_class_figi TEXT,
            norgate_symbol TEXT NOT NULL,
            cohort TEXT NOT NULL,
            price_start_date TEXT NOT NULL,
            price_end_date TEXT NOT NULL,
            terminal_date TEXT,
            recovery_type TEXT,
            equity_recovery REAL,
            terminal_recovery_effective REAL,
            terminal_treatment_status TEXT NOT NULL,
            terminal_consideration TEXT,
            drop_otc_tape INTEGER NOT NULL DEFAULT 0,
            verification_status TEXT,
            source_candidate_row_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE SET NULL
        )
        """
    )


def upsert_company(conn: sqlite3.Connection, row: dict[str, Any], now: str) -> int:
    existing = conn.execute(
        "SELECT company_id, is_active, universe_status FROM companies WHERE ticker = ?",
        (row["calibration_company_ticker"],),
    ).fetchone()
    if existing is not None and int(existing["is_active"] or 0) > 0 and str(existing["universe_status"] or "") != "delisted_calibration":
        raise ValueError(
            f"Refusing to overwrite active company row for calibration ticker {row['calibration_company_ticker']}"
        )
    conn.execute(
        """
        INSERT INTO companies(
            ticker, cik, company_name, exchange, sector, industry, industry_aggregate,
            security_type, is_primary_listing, listing_status, country, currency,
            manual_include, manual_exclude, manual_review, notes, universe_status,
            is_active, source_screen_decision, reason_codes, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, '', 'Healthcare', 'Biotechnology', 'Biotechnology',
            'Common Stock', 'true', 'delisted', 'US', 'USD',
            'false', 'true', 'false', ?, 'delisted_calibration',
            0, 'delisted_calibration_only', ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik = excluded.cik,
            company_name = excluded.company_name,
            listing_status = 'delisted',
            universe_status = 'delisted_calibration',
            is_active = 0,
            source_screen_decision = 'delisted_calibration_only',
            reason_codes = excluded.reason_codes,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            row["calibration_company_ticker"],
            row["cik"],
            row["company_name"],
            f"Calibration-only delisted row for {row['ticker']}; not investable/current production.",
            f"original_ticker={row['ticker']}|cohort={row['cohort']}|norgate_symbol={row['norgate_symbol']}",
            now,
            now,
        ),
    )
    result = conn.execute("SELECT company_id FROM companies WHERE ticker = ?", (row["calibration_company_ticker"],)).fetchone()
    if result is None:
        raise RuntimeError(f"Failed to create calibration company row for {row['calibration_company_ticker']}")
    return int(result["company_id"])


def upsert_mapping_table(conn: sqlite3.Connection, rows: list[dict[str, Any]], now: str) -> None:
    ensure_delisted_table(conn)
    for row in rows:
        company_id = upsert_company(conn, row, now)
        row["company_id"] = company_id
        conn.execute(
            """
            INSERT INTO delisted_calibration_universe(
                ticker, calibration_company_ticker, company_id, company_name, cik, cusip,
                share_class_figi, norgate_symbol, cohort, price_start_date, price_end_date,
                terminal_date, recovery_type, equity_recovery, terminal_recovery_effective,
                terminal_treatment_status, terminal_consideration, drop_otc_tape,
                verification_status, source_candidate_row_status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                calibration_company_ticker = excluded.calibration_company_ticker,
                company_id = excluded.company_id,
                company_name = excluded.company_name,
                cik = excluded.cik,
                cusip = excluded.cusip,
                share_class_figi = excluded.share_class_figi,
                norgate_symbol = excluded.norgate_symbol,
                cohort = excluded.cohort,
                price_start_date = excluded.price_start_date,
                price_end_date = excluded.price_end_date,
                terminal_date = excluded.terminal_date,
                recovery_type = excluded.recovery_type,
                equity_recovery = excluded.equity_recovery,
                terminal_recovery_effective = excluded.terminal_recovery_effective,
                terminal_treatment_status = excluded.terminal_treatment_status,
                terminal_consideration = excluded.terminal_consideration,
                drop_otc_tape = excluded.drop_otc_tape,
                verification_status = excluded.verification_status,
                source_candidate_row_status = excluded.source_candidate_row_status,
                updated_at = excluded.updated_at
            """,
            (
                row["ticker"],
                row["calibration_company_ticker"],
                company_id,
                row["company_name"],
                row["cik"],
                row["cusip"],
                row["share_class_figi"],
                row["norgate_symbol"],
                row["cohort"],
                row["price_start_date"],
                row["price_end_date"],
                row["terminal_date"],
                row["recovery_type"],
                parse_float(row["equity_recovery"]),
                parse_float(row["terminal_recovery_effective"]),
                row["terminal_treatment_status"],
                row["terminal_consideration"],
                1 if as_bool(row["drop_otc_tape"]) else 0,
                row["verification_status"],
                row["source_candidate_row_status"],
                now,
                now,
            ),
        )


def build_sec_universe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "ticker": row["calibration_company_ticker"],
            "scoring_include": "true",
            "original_ticker": row["ticker"],
            "company_name": row["company_name"],
            "cik": row["cik"],
            "calibration_only": "true",
            "source": "delisted_biotech_calibration_universe",
        }
        for row in rows
    ]


def main() -> int:
    configure_logging()
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else DEFAULT_OUTPUT_ROOT / compact_date(asof)
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates = read_candidates(args.candidates.expanduser().resolve())
    mapping_rows, rejected_rows = build_mapping_rows(candidates)
    if len(mapping_rows) != 48:
        raise ValueError(f"Expected frozen delisted calibration universe to contain 48 rows, found {len(mapping_rows)}")

    mapping_csv = args.mapping_csv.expanduser().resolve()
    sec_universe_csv = args.sec_universe_csv.expanduser().resolve()
    write_csv(mapping_csv, mapping_rows, MAPPING_FIELDS)
    write_csv(sec_universe_csv, build_sec_universe_rows(mapping_rows), SEC_UNIVERSE_FIELDS)

    now = utc_now()
    if not args.dry_run:
        with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
            init_db(conn)
            upsert_mapping_table(conn, mapping_rows, now)
            conn.commit()

    treatment_counts: dict[str, int] = {}
    for row in mapping_rows:
        key = str(row.get("terminal_treatment_status") or "")
        treatment_counts[key] = treatment_counts.get(key, 0) + 1

    summary = {
        "created_at": now,
        "asof_date": asof,
        "dry_run": bool(args.dry_run),
        "db_path": str(db_path),
        "candidate_count": len(candidates),
        "frozen_universe_count": len(mapping_rows),
        "rejected_count": len(rejected_rows),
        "mapping_csv": str(mapping_csv),
        "sec_sync_universe_csv": str(sec_universe_csv),
        "terminal_treatment_counts": dict(sorted(treatment_counts.items())),
        "rejected_rows": rejected_rows,
    }
    summary_path = output_dir / "delisted_calibration_universe_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
