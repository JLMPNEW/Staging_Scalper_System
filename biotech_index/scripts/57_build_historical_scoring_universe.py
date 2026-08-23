#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect, init_db  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.pipeline_guards import normalize_ticker  # noqa: E402
from biotech_index.core.report_inputs import dated_output_dir  # noqa: E402
from biotech_index.core.security_identity import (  # noqa: E402
    SecurityIdentityRule,
    load_security_identity_rules,
)


LOGGER = logging.getLogger("build_historical_scoring_universe")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a point-in-time biotech final scoring universe for historical "
            "recomputation.  The output is written to the dated report folder so "
            "downstream feature/scoring scripts do not use the current universe by accident."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, required=True)
    parser.add_argument("--root-universe-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--max-price-staleness-days", type=int, default=10)
    parser.add_argument(
        "--force-current-root-reconstruction",
        action="store_true",
        help=(
            "Ignore a dated CTGov universe when no trial snapshot existed by --asof; reconstruct neutral survivor "
            "membership from the configured root plus price windows and curated delisted rows."
        ),
    )
    parser.add_argument("--include-delisted", action="store_true", default=True)
    parser.add_argument("--exclude-delisted", action="store_false", dest="include_delisted")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def as_bool(raw: object, default: bool = False) -> bool:
    text = str(raw if raw is not None else "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y"}


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        rows = [{str(k): str(v or "").strip() for k, v in row.items()} for row in reader]
        return [str(field or "") for field in reader.fieldnames], rows


def load_nonretained_ticker_actions(path: Path | None) -> dict[str, tuple[date, str]]:
    """Return the first effective membership-ending action for each predecessor."""
    if path is None or not path.exists():
        return {}
    _, rows = read_csv(path)
    actions: dict[str, tuple[date, str]] = {}
    for row in rows:
        ticker = normalize_ticker(row.get("old_ticker"))
        effective_date = parse_date(row.get("effective_date"))
        action = str(row.get("action") or "").strip().lower()
        if not ticker or effective_date is None or not action or as_bool(row.get("retain_in_biotech"), False):
            continue
        prior = actions.get(ticker)
        if prior is None or effective_date < prior[0]:
            actions[ticker] = (effective_date, action)
    return actions


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def company_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, cik, is_active, universe_status, listing_status
        FROM companies
        """
    ).fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows if normalize_ticker(row["ticker"])}


def price_windows(conn: sqlite3.Connection, asof: date, *, sources: list[str]) -> dict[str, dict[str, Any]]:
    source_clause = ""
    params: list[Any] = [asof.isoformat()]
    if sources:
        placeholders = ",".join("?" for _ in sources)
        source_clause = f"WHERE source IN ({placeholders})"
        params.extend(sources)
    rows = conn.execute(
        f"""
        SELECT
            ticker,
            MIN(bar_date) AS first_price_date,
            MAX(bar_date) AS last_price_date,
            MAX(CASE WHEN date(bar_date) <= date(?) THEN bar_date END) AS latest_price_date,
            COUNT(*) AS bar_count
        FROM market_bars_daily
        {source_clause}
        GROUP BY ticker
        """,
        tuple(params),
    ).fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows if normalize_ticker(row["ticker"])}


def usable_latest_price(window: dict[str, Any] | None, asof: date, *, max_staleness_days: int) -> tuple[bool, str]:
    if not window:
        return False, "missing_price_history"
    latest = parse_date(window.get("latest_price_date"))
    if latest is None:
        return False, "no_price_on_or_before_asof"
    if latest > asof:
        return False, "future_price_date"
    if (asof - latest).days > max_staleness_days:
        return False, f"stale_price:{latest.isoformat()}"
    return True, ""


def delisted_effective_end(row: dict[str, Any]) -> date | None:
    price_end = parse_date(row.get("price_end_date"))
    terminal = parse_date(row.get("terminal_date"))
    if price_end and terminal:
        return min(price_end, terminal)
    return terminal or price_end


def add_output_fields(fieldnames: list[str]) -> list[str]:
    extras = [
        "company_id",
        "original_ticker",
        "calibration_only",
        "historical_universe_source",
        "biotech_calibration_cohort",
        "price_start_date",
        "price_end_date",
        "terminal_date",
        "recovery_type",
        "equity_recovery",
        "drop_otc_tape",
        "historical_price_ticker",
        "latest_price_date",
        "membership_start_date",
        "membership_end_date",
        "historical_ciks",
    ]
    return [*fieldnames, *[field for field in extras if field not in fieldnames]]


def live_universe_rows(
    root_rows: list[dict[str, str]],
    *,
    companies: dict[str, dict[str, Any]],
    prices: dict[str, dict[str, Any]],
    asof: date,
    max_price_staleness_days: int,
    nonretained_ticker_actions: dict[str, tuple[date, str]] | None = None,
    security_identity_rules: dict[str, SecurityIdentityRule] | None = None,
    root_universe_is_pit: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    included: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    historical_asof = asof < date.today()
    for row in root_rows:
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker or not as_bool(row.get("scoring_include"), False):
            continue
        identity_rule = (security_identity_rules or {}).get(ticker)
        if identity_rule is not None and not identity_rule.contains(asof):
            audit.append(
                {"ticker": ticker, "decision": "exclude", "reason": "outside_security_membership_window"}
            )
            continue
        if historical_asof and identity_rule is not None and not root_universe_is_pit:
            audit.append(
                {
                    "ticker": ticker,
                    "decision": "delegate",
                    "reason": "historical_identity_registry_replaces_current_metadata",
                }
            )
            continue
        terminal_action = (nonretained_ticker_actions or {}).get(ticker)
        if terminal_action is not None and asof >= terminal_action[0]:
            audit.append(
                {
                    "ticker": ticker,
                    "decision": "exclude",
                    "reason": f"membership_ended:{terminal_action[1]}:{terminal_action[0].isoformat()}",
                }
            )
            continue
        company = companies.get(ticker)
        if not company:
            audit.append({"ticker": ticker, "decision": "exclude", "reason": "missing_company_row"})
            continue
        is_active = int(company.get("is_active") or 0) > 0
        if not is_active:
            # For historical asof dates, current is_active is survivorship-biased:
            # a company that was trading on asof but delisted later must stay in
            # the point-in-time universe.  Membership is decided by as-of price
            # bars; fall back to the is_active gate only when no usable bar data
            # exists on/near asof.
            if not historical_asof:
                audit.append({"ticker": ticker, "decision": "exclude", "reason": "inactive_current_company"})
                continue
            bars_ok, _ = usable_latest_price(prices.get(ticker), asof, max_staleness_days=max_price_staleness_days)
            if not bars_ok:
                audit.append(
                    {"ticker": ticker, "decision": "exclude", "reason": "inactive_current_company_no_asof_price_data"}
                )
                continue
        company_id = int(company["company_id"])
        price_ticker = ticker
        window = prices.get(price_ticker)
        ok, reason = usable_latest_price(window, asof, max_staleness_days=max_price_staleness_days)
        if not ok:
            audit.append({"ticker": ticker, "decision": "exclude", "reason": reason})
            continue
        # A current root can reconstruct membership from price windows, but its
        # CTGov/trial fields are not historical observations. Start from an empty
        # row so those fields serialize blank rather than leaking today's state.
        reconstructed_current_root = historical_asof and not root_universe_is_pit
        out: dict[str, Any] = {} if reconstructed_current_root else dict(row)
        out.update(
            {
                "ticker": ticker,
                "company_id": company_id,
                "company_name": row.get("company_name") or company.get("company_name") or ticker,
                "scoring_include": "true",
                "calibration_only": "false",
                "historical_universe_source": (
                    "current_survivor_price_window_reconstruction"
                    if reconstructed_current_root
                    else "dated_pit_final_scoring_universe"
                ),
                "price_start_date": window.get("first_price_date") if window else "",
                "price_end_date": "",
                "historical_price_ticker": price_ticker,
                "latest_price_date": window.get("latest_price_date") if window else "",
            }
        )
        included.append(out)
        audit.append(
            {
                "ticker": ticker,
                "decision": "include",
                "reason": "active_with_price_history" if is_active else "inactive_now_but_priced_on_asof",
            }
        )
    return included, audit


def historical_addition_rows(
    rules: dict[str, SecurityIdentityRule],
    *,
    pit_root_tickers: set[str],
    companies: dict[str, dict[str, Any]],
    prices: dict[str, dict[str, Any]],
    asof: date,
    max_price_staleness_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build neutral PIT rows for approved active names absent from old dated universes."""
    included: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for ticker, rule in sorted(rules.items()):
        if ticker in pit_root_tickers:
            audit.append({"ticker": ticker, "decision": "defer", "reason": "dated_pit_universe_owns_membership"})
            continue
        if not rule.contains(asof):
            audit.append({"ticker": ticker, "decision": "exclude", "reason": "outside_security_membership_window"})
            continue
        company = companies.get(ticker)
        if not company:
            audit.append({"ticker": ticker, "decision": "exclude", "reason": "missing_company_row"})
            continue
        company_id = int(company.get("company_id") or 0)
        if company_id <= 0:
            audit.append({"ticker": ticker, "decision": "exclude", "reason": "missing_company_id"})
            continue
        window = prices.get(rule.historical_price_ticker)
        ok, reason = usable_latest_price(window, asof, max_staleness_days=max_price_staleness_days)
        if not ok:
            audit.append({"ticker": ticker, "decision": "exclude", "reason": reason})
            continue
        included.append(
            {
                "ticker": ticker,
                "company_id": company_id,
                "company_name": rule.company_name,
                "universe_status": "historical_active_member",
                "recommended_status": "keep",
                "review_bucket": "pit_membership_registry",
                "root_cause_category": "historical_active_member",
                "recommended_fix": "none",
                "review_reason": "approved effective-dated active biotech membership",
                "source_reason_codes": "active_biotech_pit_membership_registry",
                "company_diagnostic_like": "false",
                "manual_verified_active_study": "false",
                "manual_override_applied": "false",
                "final_status": "keep",
                "final_status_reason": "effective_dated_active_membership",
                "scoring_include": "true",
                "calibration_only": "false",
                "historical_universe_source": "active_biotech_pit_membership_registry",
                "biotech_calibration_cohort": rule.calibration_cohort,
                "price_start_date": rule.membership_start_date.isoformat(),
                "price_end_date": rule.membership_end_date.isoformat() if rule.membership_end_date else "",
                "historical_price_ticker": rule.historical_price_ticker,
                "latest_price_date": window.get("latest_price_date") if window else "",
                "membership_start_date": rule.membership_start_date.isoformat(),
                "membership_end_date": rule.membership_end_date.isoformat() if rule.membership_end_date else "",
                "historical_ciks": ";".join(rule.historical_ciks),
            }
        )
        audit.append({"ticker": ticker, "decision": "include", "reason": "effective_dated_active_membership"})
    return included, audit


def delisted_universe_rows(
    conn: sqlite3.Connection,
    *,
    prices: dict[str, dict[str, Any]],
    asof: date,
    max_price_staleness_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'delisted_calibration_universe'"
    ).fetchone()
    if table is None:
        return [], []
    rows = conn.execute("SELECT * FROM delisted_calibration_universe ORDER BY calibration_company_ticker").fetchall()
    included: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        ticker = normalize_ticker(row.get("calibration_company_ticker"))
        original = normalize_ticker(row.get("ticker"))
        start = parse_date(row.get("price_start_date"))
        end = delisted_effective_end(row)
        if not ticker or start is None or end is None:
            audit.append({"ticker": ticker or original, "decision": "exclude", "reason": "invalid_delisted_window"})
            continue
        if asof < start or asof > end:
            audit.append({"ticker": ticker, "decision": "exclude", "reason": "outside_alive_window"})
            continue
        company_id = int(row.get("company_id") or 0)
        if company_id <= 0:
            audit.append({"ticker": ticker, "decision": "exclude", "reason": "missing_company_id"})
            continue
        price_ticker = original or ticker
        window = prices.get(price_ticker)
        ok, reason = usable_latest_price(window, asof, max_staleness_days=max_price_staleness_days)
        if not ok:
            audit.append({"ticker": ticker, "decision": "exclude", "reason": reason})
            continue
        out = {
            "ticker": ticker,
            "company_id": company_id,
            "company_name": row.get("company_name") or ticker,
            "universe_status": "delisted_calibration",
            "recommended_status": "keep",
            "review_bucket": "delisted_calibration",
            "root_cause_category": "historical_calibration_delisted",
            "recommended_fix": "calibration_only",
            "review_reason": "strict delisted calibration universe alive on asof",
            "primary_nct": "",
            "primary_trial_title": "",
            "verified_qualifying_active_trial_count": "0",
            "phase2_3_active_trials": "0",
            "lead_phase2_3_active_trials": "0",
            "program_phase2_3_active_trials": "0",
            "collaborator_phase2_3_active_trials": "0",
            "effective_phase2_3_trials": "0",
            "active_lead_sponsor_trials": "0",
            "active_collaborator_trials": "0",
            "active_program_override_trials": "0",
            "company_diagnostic_like": "false",
            "source_reason_codes": "delisted_calibration_alive_window",
            "manual_root_cause": "historical_calibration_delisted",
            "manual_verified_active_study": "false",
            "manual_verdict": "manual_keep",
            "manual_override_applied": "true",
            "final_status": "keep",
            "final_status_reason": "delisted_calibration_alive_window",
            "scoring_include": "true",
            "original_ticker": original,
            "calibration_only": "true",
            "historical_universe_source": "delisted_biotech_calibration_universe",
            "biotech_calibration_cohort": row.get("cohort") or "",
            "price_start_date": row.get("price_start_date") or "",
            "price_end_date": row.get("price_end_date") or "",
            "terminal_date": row.get("terminal_date") or "",
            "recovery_type": row.get("recovery_type") or "",
            "equity_recovery": row.get("terminal_recovery_effective") or row.get("equity_recovery") or "",
            "drop_otc_tape": str(bool(row.get("drop_otc_tape"))).lower(),
            "historical_price_ticker": price_ticker,
            "latest_price_date": window.get("latest_price_date") if window else "",
        }
        included.append(out)
        audit.append({"ticker": ticker, "decision": "include", "reason": "delisted_alive_with_price_history"})
    return included, audit


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = parse_date(args.asof)
    if asof is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    base_output_dir = resolve_path(cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    output_dir = dated_output_dir(base_output_dir, asof.isoformat())
    configured_root_universe = resolve_path(
        cfg_get(config, "biotech_features.final_scoring_universe_csv"),
        base_dir=base_dir,
    )
    ticker_actions_path = resolve_path(
        cfg_get(config, "paths.company_ticker_actions_csv"),
        base_dir=base_dir,
    )
    identity_registry_path = resolve_path(
        cfg_get(config, "active_biotech_history.registry_csv", "data/active_biotech_historical_additions.csv"),
        base_dir=base_dir,
    )
    security_identity_rules = load_security_identity_rules(identity_registry_path)
    dated_pit_universe = output_dir / configured_root_universe.name
    root_universe = (
        args.root_universe_csv.expanduser().resolve()
        if args.root_universe_csv
        else configured_root_universe
        if args.force_current_root_reconstruction
        else dated_pit_universe
        if dated_pit_universe.exists()
        else configured_root_universe
    )
    root_universe_is_pit = (
        not args.force_current_root_reconstruction
        and dated_pit_universe.exists()
        and root_universe.resolve() == dated_pit_universe.resolve()
    )
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else output_dir / root_universe.name
    summary_csv = args.summary_csv.expanduser().resolve() if args.summary_csv else output_dir / "historical_scoring_universe_audit.csv"
    manifest_path = output_dir / "historical_scoring_universe_manifest.json"

    root_fields, root_rows = read_csv(root_universe)
    if root_universe_is_pit and not any(as_bool(row.get("scoring_include"), False) for row in root_rows):
        # CTGov snapshots did not exist for early history. An empty dated audit
        # means "trial state unavailable", not "no biotech companies existed".
        # Reconstruct survivor membership from observed price windows and add the
        # separately curated delisted panel below; live_universe_rows deliberately
        # strips all current CTGov metadata in this fallback mode.
        LOGGER.warning(
            "Dated CTGov universe has no scoring rows for asof=%s; using neutral current-survivor price-window reconstruction",
            asof.isoformat(),
        )
        root_universe = configured_root_universe
        root_universe_is_pit = False
        root_fields, root_rows = read_csv(root_universe)
    pit_root_tickers = (
        {ticker for row in root_rows if (ticker := normalize_ticker(row.get("ticker")))}
        if root_universe_is_pit
        else set()
    )
    nonretained_ticker_actions = load_nonretained_ticker_actions(ticker_actions_path)
    with connect(db_path) as conn:
        init_db(conn)
        companies = company_rows(conn)
        live_prices = price_windows(conn, asof, sources=["yahoo_adjusted", "interactive_brokers"])
        delisted_prices = price_windows(conn, asof, sources=["norgate_us_equities_total_return"])
        live_rows, live_audit = live_universe_rows(
            root_rows,
            companies=companies,
            prices=live_prices,
            asof=asof,
            max_price_staleness_days=max(0, int(args.max_price_staleness_days)),
            nonretained_ticker_actions=nonretained_ticker_actions,
            security_identity_rules=security_identity_rules,
            root_universe_is_pit=root_universe_is_pit,
        )
        addition_rows, addition_audit = historical_addition_rows(
            security_identity_rules,
            pit_root_tickers=pit_root_tickers,
            companies=companies,
            prices=live_prices,
            asof=asof,
            max_price_staleness_days=max(0, int(args.max_price_staleness_days)),
        )
        delisted_rows: list[dict[str, Any]] = []
        delisted_audit: list[dict[str, Any]] = []
        if args.include_delisted:
            delisted_rows, delisted_audit = delisted_universe_rows(
                conn,
                prices=delisted_prices,
                asof=asof,
                max_price_staleness_days=max(0, int(args.max_price_staleness_days)),
            )

    fieldnames = add_output_fields(root_fields)
    rows = sorted([*live_rows, *addition_rows, *delisted_rows], key=lambda row: str(row.get("ticker") or ""))
    tickers = [str(row.get("ticker") or "").upper() for row in rows]
    duplicates = sorted({ticker for ticker in tickers if tickers.count(ticker) > 1})
    if duplicates:
        raise RuntimeError("Historical scoring universe has duplicate ticker rows: " + ",".join(duplicates[:25]))
    if not rows:
        raise RuntimeError(f"Historical scoring universe is empty for asof={asof.isoformat()}")
    write_csv(output_csv, rows, fieldnames)
    audit_fieldnames = ["ticker", "decision", "reason"]
    write_csv(summary_csv, [*live_audit, *addition_audit, *delisted_audit], audit_fieldnames)
    manifest = {
        "asof_date": asof.isoformat(),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "output_csv": str(output_csv),
        "summary_csv": str(summary_csv),
        "root_universe_csv": str(root_universe),
        "row_count": len(rows),
        "live_row_count": len(live_rows),
        "active_historical_addition_row_count": len(addition_rows),
        "active_historical_registry_count": len(security_identity_rules),
        "active_historical_registry_csv": str(identity_registry_path),
        "root_universe_is_pit": root_universe_is_pit,
        "delisted_row_count": len(delisted_rows),
        "max_price_staleness_days": max(0, int(args.max_price_staleness_days)),
        "source_policy": {
            "delisted": "included only inside price_start_date..min(price_end_date,terminal_date) and with price history on/before asof",
            "current": (
                "included when active with price history on/before asof; for historical asof dates, "
                "currently-inactive names are kept when usable as-of price bars exist, but non-retained ticker "
                "actions end membership on their effective date. If the dated CTGov universe is empty because "
                "snapshots did not yet exist, membership is reconstructed from current survivors plus observed "
                "price windows with all current trial metadata stripped; curated delisted rows are added separately"
            ),
            "active_historical_additions": (
                "included only inside explicit membership windows; neutral universe metadata prevents current CTGov state "
                "from leaking into historical feature recomputation"
            ),
            "missing_historical_feeds": "left null/availability-flagged by downstream feature builders; never forward-filled from future dates",
        },
    }
    write_json(manifest_path, manifest)
    LOGGER.info(
        "Built historical scoring universe: asof=%s rows=%d live=%d active_additions=%d delisted=%d output=%s",
        asof.isoformat(),
        len(rows),
        len(live_rows),
        len(addition_rows),
        len(delisted_rows),
        output_csv,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
