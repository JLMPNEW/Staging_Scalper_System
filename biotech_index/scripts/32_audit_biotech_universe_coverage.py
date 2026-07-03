#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import scoring_market_sources  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("audit_biotech_universe_coverage")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "asof_date",
    "ticker",
    "company_id",
    "company_name",
    "sector",
    "industry",
    "source_screen_decision",
    "reason_codes",
    "coverage_status",
    "recommended_cleanup_action",
    "cleanup_reason",
    "in_final_universe",
    "final_status",
    "scoring_include",
    "final_status_reason",
    "verified_qualifying_active_trial_count",
    "phase2_3_active_trials",
    "active_nonqualifying_device_trials",
    "company_diagnostic_like",
    "primary_nct",
    "market_asof",
    "market_data_quality",
    "market_cap",
    "avg_dollar_volume_20d",
    "commercial_asof",
    "commercial_data_quality",
    "guidance_asof",
    "guidance_data_quality",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit active biotech companies that are not in the scored universe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Audit date in YYYY-MM-DD. Defaults to latest daily_scores date.")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def latest_score_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM daily_scores").fetchone()
    asof = str(row["asof_date"] or "") if row else ""
    if not asof:
        raise ValueError("No daily_scores rows available to infer audit asof date")
    return asof


def csv_rows_by_ticker(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        LOGGER.warning("Optional universe audit source CSV missing: %s", path)
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            ticker: row
            for row in csv.DictReader(handle)
            if (ticker := normalize_ticker(row.get("ticker")))
        }


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def connect_readonly(db_path: Path, *, timeout_sec: float) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def fnum(raw: object, default: float = 0.0) -> float:
    try:
        return float(str(raw or "").strip())
    except ValueError:
        return default


def cleanup_classification(
    row: sqlite3.Row,
    final_row: dict[str, str],
) -> tuple[str, str]:
    ticker = normalize_ticker(row["ticker"])
    sector = str(row["sector"] or "").lower()
    industry = str(row["industry"] or "").lower()
    company_name = str(row["company_name"] or "").lower()
    final_status = str(final_row.get("final_status") or "")
    scoring_include = as_bool(final_row.get("scoring_include"))
    active_q = fnum(final_row.get("verified_qualifying_active_trial_count"))
    phase_q = fnum(final_row.get("phase2_3_active_trials"))
    device_like = (
        fnum(final_row.get("active_nonqualifying_device_trials")) > 0
        or str(final_row.get("company_diagnostic_like") or "").lower() == "true"
    )

    known_device_or_diagnostic = {
        "ABT",
        "AXGN",
        "BFLY",
        "DGX",
        "GEHC",
        "HAE",
        "NNOX",
        "QDEL",
        "QSI",
        "STIM",
        "SYK",
        "TNDM",
    }
    device_keywords = (
        "device",
        "equipment",
        "diagnostic",
        "diagnostics",
        "imaging",
        "surgical",
        "orthopedic",
        "robotic",
        "laboratory",
        "health care equipment",
    )
    service_keywords = ("contract research", "research services", "laboratory services", "healthcare services")
    haystack = f"{sector} {industry} {company_name}"
    if ticker in known_device_or_diagnostic:
        return "move_to_med_devices_or_diagnostics", "obvious device/diagnostic/general-healthcare ticker"
    if any(token in haystack for token in device_keywords):
        return "move_to_med_devices_or_diagnostics", "sector/industry/name is device/diagnostic-heavy"
    if any(token in haystack for token in service_keywords):
        return "deactivate_from_biotech_or_separate_services", "healthcare services/CRO profile, not biotech therapeutics"
    if final_row and not scoring_include and final_status in {"remove", "remove_candidate"}:
        return "keep_active_but_not_scored_review", f"final universe status={final_status}"
    if final_row and active_q <= 0 and phase_q <= 0:
        return "review_biotech_reactivation", "active DB company lacks verified active qualifying CTGov evidence"
    if not final_row:
        return "missing_from_final_universe_review", "active DB company not present in final CTGov scoring universe"
    if device_like:
        return "move_to_med_devices_or_diagnostics", "CTGov audit marks active device/diagnostic-like trials"
    return "manual_review", "active in companies but not scored"


def load_active_unscored_rows(conn: sqlite3.Connection, asof_date: str, market_source: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.company_id, c.ticker, c.company_name, c.sector, c.industry, c.source_screen_decision,
               c.reason_codes,
               mf.asof_date AS market_asof, mf.market_data_quality, mf.market_cap, mf.avg_dollar_volume_20d,
               fg.asof_date AS guidance_asof, fg.data_quality AS guidance_data_quality,
               cv.asof_date AS commercial_asof, cv.data_quality AS commercial_data_quality
        FROM companies c
        LEFT JOIN daily_scores s ON s.company_id = c.company_id AND s.asof_date = ?
        LEFT JOIN market_features_daily mf
            ON mf.company_id = c.company_id
           AND mf.source = ?
           AND mf.asof_date = (
                SELECT MAX(mf2.asof_date)
                FROM market_features_daily mf2
                WHERE mf2.company_id = c.company_id
                  AND mf2.source = ?
                  AND mf2.asof_date <= ?
           )
        LEFT JOIN forward_guidance_features_daily fg ON fg.company_id = c.company_id AND fg.asof_date = ?
        LEFT JOIN commercial_value_features_daily cv ON cv.company_id = c.company_id AND cv.asof_date = ?
        WHERE c.is_active = 1 AND s.company_id IS NULL
        ORDER BY c.ticker
        """,
        (asof_date, market_source, market_source, asof_date, asof_date, asof_date),
    ).fetchall()


def build_rows(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
    final_universe: dict[str, dict[str, str]],
    market_source: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in load_active_unscored_rows(conn, asof_date, market_source):
        ticker = normalize_ticker(row["ticker"])
        final_row = final_universe.get(ticker, {})
        action, reason = cleanup_classification(row, final_row)
        coverage_status = "active_not_scored"
        out.append(
            {
                "asof_date": asof_date,
                "ticker": ticker,
                "company_id": row["company_id"],
                "company_name": row["company_name"],
                "sector": row["sector"],
                "industry": row["industry"],
                "source_screen_decision": row["source_screen_decision"],
                "reason_codes": row["reason_codes"],
                "coverage_status": coverage_status,
                "recommended_cleanup_action": action,
                "cleanup_reason": reason,
                "in_final_universe": 1 if final_row else 0,
                "final_status": final_row.get("final_status", ""),
                "scoring_include": final_row.get("scoring_include", ""),
                "final_status_reason": final_row.get("final_status_reason", ""),
                "verified_qualifying_active_trial_count": final_row.get("verified_qualifying_active_trial_count", ""),
                "phase2_3_active_trials": final_row.get("phase2_3_active_trials", ""),
                "active_nonqualifying_device_trials": final_row.get("active_nonqualifying_device_trials", ""),
                "company_diagnostic_like": final_row.get("company_diagnostic_like", ""),
                "primary_nct": final_row.get("primary_nct", ""),
                "market_asof": row["market_asof"] or "",
                "market_data_quality": row["market_data_quality"] or "",
                "market_cap": row["market_cap"] or "",
                "avg_dollar_volume_20d": row["avg_dollar_volume_20d"] or "",
                "commercial_asof": row["commercial_asof"] or "",
                "commercial_data_quality": row["commercial_data_quality"] or "",
                "guidance_asof": row["guidance_asof"] or "",
                "guidance_data_quality": row["guidance_data_quality"] or "",
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_root = resolve_path(cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    final_universe_csv = resolve_path(
        cfg_get(config, "biotech_features.final_scoring_universe_csv"),
        base_dir=base_dir,
    )
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    market_sources = scoring_market_sources(config)
    market_source = (
        market_sources[0]
        if market_sources
        else str(cfg_get(config, "commercial_value.preferred_market_source", "yahoo_adjusted"))
    )
    conn = connect_readonly(db_path, timeout_sec=sqlite_timeout_sec)
    try:
        asof_date = str(args.asof or latest_score_date(conn))[:10]
        final_universe = csv_rows_by_ticker(final_universe_csv)
        rows = build_rows(conn, asof_date=asof_date, final_universe=final_universe, market_source=market_source)
    finally:
        conn.close()

    default_output = output_root / asof_date.replace("-", "") / "biotech_universe_coverage_audit.csv"
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else default_output
    write_csv(output_csv, rows)
    summary: dict[str, int] = {}
    for row in rows:
        key = str(row["recommended_cleanup_action"])
        summary[key] = summary.get(key, 0) + 1
    LOGGER.info(
        "Universe coverage audit complete: rows=%s output=%s summary=%s",
        len(rows),
        output_csv,
        json.dumps(summary, sort_keys=True),
    )


if __name__ == "__main__":
    main()
