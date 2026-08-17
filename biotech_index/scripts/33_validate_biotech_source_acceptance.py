#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PRIMARY_DATE_COLUMNS = (
    "source_snapshot_asof_date",
    "feature_data_asof_date",
    "clinical_data_asof_date",
    "financial_data_asof_date",
    "insider_data_asof_date",
)
ALL_LINEAGE_DATE_COLUMNS = (
    *PRIMARY_DATE_COLUMNS,
    "price_data_asof_date",
    "short_interest_asof_date",
    "institutional_data_asof_date",
    "borrow_data_asof_date",
    "float_shares_asof_date",
    "float_shares_source_asof_date",
    "forward_catalyst_asof_date",
)
FINANCIAL_FORMS = (
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
    "6-K",
    "6-K/A",
)
CORE_FACT_COLUMNS = (
    "cash_and_investments",
    "total_assets",
    "total_debt",
    "revenue",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "shares_outstanding",
)


@dataclass(frozen=True)
class Check:
    name: str
    source: str
    role: str
    required: bool
    status: str
    detail: str
    evidence: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Biotech source freshness and score lineage, then write the production acceptance manifest."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True, help="Production as-of date in YYYY-MM-DD.")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_db_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text]
    if "T" in text:
        candidates.append(text.split("T", 1)[0])
    elif len(text) > 10:
        candidates.append(text[:10])
    for candidate in candidates:
        parsed = parse_date(candidate)
        if parsed is not None:
            return parsed
    for fmt in ("%Y%m%d", "%Y/%m/%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def latest_parsed_db_date(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    asof: date,
) -> date | None:
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL AND {column} <> ''''"
    ).fetchall()
    parsed = [value for row in rows if (value := parse_db_date(row[0] if row else None)) is not None and value <= asof]
    return max(parsed) if parsed else None


def parse_timestamp(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "1.0", "true", "yes", "y"}


def finite_float(raw: object) -> float | None:
    try:
        value = float(str(raw or "").strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def score_path(config: dict[str, Any], *, base_dir: Path, asof: date) -> Path:
    output_dir = resolve_path(
        cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"),
        base_dir=base_dir,
    )
    filename = str(cfg_get(config, "biotech_scoring.output_csv", "biotech_daily_scores.csv"))
    dated = output_dir / asof.strftime("%Y%m%d") / filename
    direct = output_dir / filename
    if dated.exists():
        return dated
    if direct.exists():
        return direct
    raise FileNotFoundError(f"Biotech score output is missing: {direct} or {dated}")


def add_check(
    checks: list[Check],
    *,
    name: str,
    source: str,
    role: str,
    required: bool,
    passed: bool,
    detail: str,
    evidence: dict[str, Any],
) -> None:
    checks.append(
        Check(
            name=name,
            source=source,
            role=role,
            required=required,
            status="PASS" if passed else ("FAIL" if required else "ADVISORY"),
            detail=detail,
            evidence=evidence,
        )
    )


def latest_run(conn: sqlite3.Connection, run_type: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT run_type, started_at, completed_at, status, row_count, message
        FROM runs
        WHERE run_type = ?
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (run_type,),
    ).fetchone()
    return dict(row) if row is not None else None


def validate_provider_runs(
    conn: sqlite3.Connection,
    checks: list[Check],
    *,
    max_age_hours: float,
) -> None:
    providers = (
        ("ctgov_refresh", "ClinicalTrials.gov API v2", "primary", True, "sync_ctgov_trials"),
        ("sec_filing_refresh", "SEC EDGAR submissions/archives", "primary", True, "sync_sec_filings"),
        ("companyfacts_refresh", "SEC XBRL Companyfacts", "primary", True, "sync_sec_companyfacts_history"),
        ("ib_market_refresh", "Interactive Brokers/TWS", "fallback/live validation", True, "sync_market_data_ib"),
        ("yahoo_market_refresh", "Yahoo adjusted prices", "primary", True, "sync_market_data_yahoo_adjusted"),
        ("fda_adcom_refresh", "FDA Advisory Committee calendar", "shadow", False, "sync_fda_adcom_calendar"),
    )
    now = datetime.now(timezone.utc)
    for name, source, role, required, run_type in providers:
        row = latest_run(conn, run_type)
        completed = parse_timestamp((row or {}).get("completed_at"))
        age_hours = (now - completed).total_seconds() / 3600.0 if completed is not None else None
        status = str((row or {}).get("status") or "").lower()
        passed = status in {"success", "partial"} and age_hours is not None and age_hours <= max_age_hours
        add_check(
            checks,
            name=name,
            source=source,
            role=role,
            required=required,
            passed=passed,
            detail="latest provider synchronization completed within the production freshness window",
            evidence={"run": row or {}, "age_hours": round(age_hours, 3) if age_hours is not None else None},
        )


def validate_score_lineage(rows: list[dict[str, str]], checks: list[Check], *, asof: date) -> list[dict[str, str]]:
    if not rows:
        add_check(
            checks,
            name="score_rows",
            source="Biotech score output",
            role="primary",
            required=True,
            passed=False,
            detail="score output has rows",
            evidence={"row_count": 0},
        )
        return []
    current_rows = [row for row in rows if not as_bool(row.get("calibration_only"))]
    candidates = [row for row in current_rows if as_bool(row.get("portfolio_candidate_gate"))]
    add_check(
        checks,
        name="score_rows",
        source="Biotech score output",
        role="primary",
        required=True,
        passed=bool(current_rows) and bool(candidates),
        detail="current-universe score and investable-candidate rows are present",
        evidence={"score_rows": len(rows), "current_rows": len(current_rows), "candidate_rows": len(candidates)},
    )

    mismatches: dict[str, list[str]] = {}
    for column in PRIMARY_DATE_COLUMNS:
        mismatches[column] = sorted(
            str(row.get("ticker") or "") for row in current_rows if str(row.get(column) or "") != asof.isoformat()
        )
    bad_primary = {column: tickers for column, tickers in mismatches.items() if tickers}
    add_check(
        checks,
        name="primary_score_lineage_dates",
        source="CTGov/SEC/Companyfacts/Form 4/feature builders",
        role="primary",
        required=True,
        passed=not bad_primary,
        detail="every current score row is stamped with current primary-source feature snapshots",
        evidence={"mismatches": bad_primary},
    )

    future_dates: dict[str, list[str]] = {}
    for column in ALL_LINEAGE_DATE_COLUMNS:
        offenders = sorted(
            str(row.get("ticker") or "")
            for row in current_rows
            if (parsed := parse_date(row.get(column))) is not None and parsed > asof
        )
        if offenders:
            future_dates[column] = offenders
    add_check(
        checks,
        name="point_in_time_date_guard",
        source="All score-input sources",
        role="primary",
        required=True,
        passed=not future_dates,
        detail="no score-input lineage date is after the production as-of date",
        evidence={"future_dates": future_dates},
    )

    stale_candidate_prices = sorted(
        str(row.get("ticker") or "")
        for row in candidates
        if str(row.get("price_data_asof_date") or "") != asof.isoformat()
    )
    add_check(
        checks,
        name="candidate_price_freshness",
        source="Yahoo adjusted prices with IBKR fallback",
        role="primary",
        required=True,
        passed=not stale_candidate_prices,
        detail="every investable candidate uses an as-of-date market snapshot",
        evidence={"stale_candidate_tickers": stale_candidate_prices},
    )

    invalid_borrow = sorted(
        str(row.get("ticker") or "")
        for row in candidates
        if as_bool(row.get("borrow_fee_data_available_flag"))
        and (
            as_bool(row.get("borrow_fee_stale_flag"))
            or (finite_float(row.get("borrow_fee_staleness_days")) or 0.0) > 10.0
        )
    )
    add_check(
        checks,
        name="candidate_borrow_freshness",
        source="Interactive Brokers/TWS borrow data",
        role="primary risk overlay",
        required=True,
        passed=not invalid_borrow,
        detail="available candidate borrow observations pass their staleness policy",
        evidence={"invalid_candidate_tickers": invalid_borrow},
    )
    for name, column, source, max_age_days in (
        ("short_interest_reporting_age", "short_interest_asof_date", "FINRA short interest", 35),
        ("institutional_13f_reporting_age", "institutional_data_asof_date", "SEC Form 13F", 140),
    ):
        observed_dates = [parsed for row in current_rows if (parsed := parse_date(row.get(column))) is not None]
        latest = max(observed_dates) if observed_dates else None
        age_days = (asof - latest).days if latest is not None else None
        add_check(
            checks,
            name=name,
            source=source,
            role="primary",
            required=True,
            passed=age_days is not None and 0 <= age_days <= max_age_days,
            detail="latest reported observation is within its publication-cadence tolerance",
            evidence={
                "latest_observation_date": latest.isoformat() if latest is not None else None,
                "age_days": age_days,
                "max_age_days": max_age_days,
            },
        )

    stale_float_candidates = sorted(
        str(row.get("ticker") or "")
        for row in candidates
        if (float_date := parse_date(row.get("float_shares_source_asof_date") or row.get("float_shares_asof_date")))
        is not None
        and (asof - float_date).days > 550
    )
    add_check(
        checks,
        name="float_denominator_age",
        source="SEC disclosures plus manual proxies",
        role="primary denominator proxy",
        required=False,
        passed=not stale_float_candidates,
        detail="candidate float denominators are no older than the bounded SEC reprocessing window",
        evidence={"stale_candidate_tickers": stale_float_candidates, "max_age_days": 550},
    )
    return candidates


def core_fact_count(row: sqlite3.Row) -> int:
    return sum(row[column] is not None for column in CORE_FACT_COLUMNS)


def validate_financial_lineage(
    conn: sqlite3.Connection,
    candidates: list[dict[str, str]],
    checks: list[Check],
    *,
    asof: date,
) -> None:
    issues: dict[str, list[str]] = {}
    reviewed = 0
    for candidate in candidates:
        ticker = str(candidate.get("ticker") or "").strip().upper()
        company_id = int(float(str(candidate.get("company_id") or "0")))
        if not ticker or company_id <= 0:
            issues.setdefault("invalid_candidate_identity", []).append(ticker or "<blank>")
            continue
        reviewed += 1
        placeholders = ",".join("?" for _ in FINANCIAL_FORMS)
        latest_filing = conn.execute(
            f"""
            SELECT MAX(filing_date)
            FROM sec_filings
            WHERE company_id = ? AND filing_date <= ? AND form IN ({placeholders})
            """,
            (company_id, asof.isoformat(), *FINANCIAL_FORMS),
        ).fetchone()[0]
        sync_state = conn.execute(
            "SELECT latest_source_filing_date, sync_status FROM company_facts_sync_state WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        if latest_filing and (sync_state is None or str(sync_state[0] or "") < str(latest_filing)):
            issues.setdefault("companyfacts_not_refreshed_after_latest_filing", []).append(ticker)

        fact_rows = conn.execute(
            f"""
            SELECT period_end, {", ".join(CORE_FACT_COLUMNS)}
            FROM company_facts_quarterly
            WHERE company_id = ? AND filed_date <= ? AND period_end <= ?
            ORDER BY period_end DESC, filed_date DESC
            """,
            (company_id, asof.isoformat(), asof.isoformat()),
        ).fetchall()
        canonical_period = next((str(row["period_end"] or "") for row in fact_rows if core_fact_count(row) >= 2), "")
        feature_row = conn.execute(
            """
            SELECT f.latest_period_end AS survival_period, c.latest_period_end AS commercial_period
            FROM financial_survival_features f
            JOIN commercial_value_features_daily c
              ON c.asof_date = f.asof_date AND c.company_id = f.company_id
            WHERE f.asof_date = ? AND f.company_id = ?
            """,
            (asof.isoformat(), company_id),
        ).fetchone()
        feature_period = max(
            str((feature_row or {}).get("survival_period") or "")
            if isinstance(feature_row, dict)
            else str(feature_row[0] or "")
            if feature_row
            else "",
            str((feature_row or {}).get("commercial_period") or "")
            if isinstance(feature_row, dict)
            else str(feature_row[1] or "")
            if feature_row
            else "",
        )
        if not canonical_period:
            issues.setdefault("no_canonical_core_financial_period", []).append(ticker)
        elif feature_period < canonical_period:
            issues.setdefault("features_lag_latest_canonical_period", []).append(ticker)

    add_check(
        checks,
        name="candidate_financial_lineage",
        source="SEC filings and SEC XBRL Companyfacts",
        role="primary",
        required=True,
        passed=not issues,
        detail="candidate financial features use the latest canonical core-fact period available by as-of",
        evidence={"candidate_count": reviewed, "issues": issues},
    )


def validate_positioning_feed_state(
    db_path: Path,
    checks: list[Check],
    *,
    max_age_hours: float,
) -> None:
    now = datetime.now(timezone.utc)
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        states = {
            str(row["feed_name"]): dict(row) for row in conn.execute("SELECT * FROM market_positioning_feed_state")
        }
    for feed_name, source, role, required in (
        ("short_interest", "FINRA short interest", "primary", True),
        ("institutional_13f", "SEC Form 13F", "primary", True),
        ("ibkr_borrow_availability", "Interactive Brokers/TWS borrow", "primary risk overlay", True),
        ("sec_public_float_proxy", "SEC public-float disclosures", "primary denominator proxy", False),
        ("company_fact_share_proxy", "SEC Companyfacts shares", "primary denominator proxy", False),
    ):
        state = states.get(feed_name)
        synced = parse_timestamp((state or {}).get("last_success_at"))
        age_hours = (now - synced).total_seconds() / 3600.0 if synced is not None else None
        passed = age_hours is not None and age_hours <= max_age_hours
        add_check(
            checks,
            name=f"{feed_name}_refresh",
            source=source,
            role=role,
            required=required,
            passed=passed,
            detail="positioning collector completed within the production freshness window",
            evidence={"state": state or {}, "age_hours": round(age_hours, 3) if age_hours is not None else None},
        )


def business_day_age(start: date, end: date) -> int:
    if start > end:
        return -business_day_age(end, start)
    current = start
    count = 0
    while current < end:
        current = current.fromordinal(current.toordinal() + 1)
        if current.weekday() < 5:
            count += 1
    return count


def validate_form4_source(
    config: dict[str, Any],
    *,
    base_dir: Path,
    checks: list[Check],
    asof: date,
) -> None:
    db_path = resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir)
    snapshot_date: date | None = None
    raw_dates: list[date] = []
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "sec_form4_daily_state" in tables:
            row = conn.execute("SELECT MAX(last_index_date) FROM sec_form4_daily_state").fetchone()
            snapshot_date = parse_date(row[0] if row else None)
        for table, column in (
            ("sec_ownership_submission", "filing_date"),
            ("sec_form4_daily_ingest_log", "filing_date"),
            ("form4_events_tier1", "filing_date"),
            ("form4_buy_events_v1", "filing_date"),
        ):
            if table not in tables:
                continue
            columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                continue
            parsed = latest_parsed_db_date(conn, table=table, column=column, asof=asof)
            if parsed is not None and parsed <= asof:
                raw_dates.append(parsed)
    latest_raw = max(raw_dates) if raw_dates else None
    max_raw_lag = int(cfg_get(config, "biotech_refresh.form4_preflight.max_raw_filing_lag_days", 5))
    raw_lag = business_day_age(latest_raw, asof) if latest_raw is not None else None
    passed = snapshot_date == asof and raw_lag is not None and 0 <= raw_lag <= max_raw_lag
    add_check(
        checks,
        name="form4_source_freshness",
        source="SEC Form 4 staging database",
        role="primary",
        required=True,
        passed=passed,
        detail="Form 4 snapshot is as-of aligned and raw filing ingestion is within its business-day lag policy",
        evidence={
            "database_path": str(db_path),
            "snapshot_date": snapshot_date.isoformat() if snapshot_date is not None else None,
            "latest_raw_filing_date": latest_raw.isoformat() if latest_raw is not None else None,
            "raw_filing_lag_business_days": raw_lag,
            "max_raw_filing_lag_business_days": max_raw_lag,
        },
    )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    asof = parse_date(args.asof)
    if asof is None:
        raise ValueError(f"Invalid --asof: {args.asof}")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    positioning_db = resolve_path(cfg_get(config, "market_positioning.database_path"), base_dir=base_dir)
    universe_path = resolve_path(cfg_get(config, "paths.screen_results_csv"), base_dir=base_dir)
    manifest_path = resolve_path(
        cfg_get(
            config,
            "source_acceptance.output_manifest",
            "../output/biotech_index_reports/biotech_refresh_acceptance_manifest.json",
        ),
        base_dir=base_dir,
    )
    max_run_age = float(cfg_get(config, "source_acceptance.max_provider_run_age_hours", 48.0))
    max_universe_age = int(cfg_get(config, "source_acceptance.max_universe_age_days", 8))
    checks: list[Check] = []

    universe_age = (
        datetime.now(timezone.utc) - datetime.fromtimestamp(universe_path.stat().st_mtime, timezone.utc)
    ).days
    add_check(
        checks,
        name="universe_freshness",
        source="biotech_screen_results_all.csv",
        role="primary universe",
        required=True,
        passed=universe_age <= max_universe_age,
        detail="base universe was refreshed on the weekly production cadence",
        evidence={
            "path": str(universe_path),
            "age_days": universe_age,
            "max_age_days": max_universe_age,
            "sha256": file_sha256(universe_path),
        },
    )

    scores = score_path(config, base_dir=base_dir, asof=asof)
    score_rows = read_csv(scores)
    candidates = validate_score_lineage(score_rows, checks, asof=asof)
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        validate_provider_runs(conn, checks, max_age_hours=max_run_age)
        snapshot_date = conn.execute("SELECT MAX(asof_date) FROM trial_snapshot_daily").fetchone()[0]
        add_check(
            checks,
            name="ctgov_snapshot_date",
            source="ClinicalTrials.gov API v2",
            role="primary",
            required=True,
            passed=str(snapshot_date or "") == asof.isoformat(),
            detail="clinical trial snapshot is point-in-time aligned to the score date",
            evidence={"latest_snapshot_date": snapshot_date, "expected": asof.isoformat()},
        )
        validate_financial_lineage(conn, candidates, checks, asof=asof)
    validate_positioning_feed_state(positioning_db, checks, max_age_hours=max_run_age)
    validate_form4_source(config, base_dir=base_dir, checks=checks, asof=asof)

    norgate_in_primary = "norgate_us_equities_total_return" in {
        str(cfg_get(config, "market_data_policy.scoring_primary_source", "")),
        *[str(value) for value in cfg_get(config, "market_data_policy.scoring_fallback_sources", [])],
    }
    add_check(
        checks,
        name="norgate_role_boundary",
        source="Norgate total-return data",
        role="calibration-only delisted history",
        required=True,
        passed=not norgate_in_primary,
        detail="Norgate is excluded from current production ranking and retained for delisted calibration only",
        evidence={"used_for_current_ranking": norgate_in_primary},
    )

    shadow_enabled = not bool(cfg_get(config, "fda_adcom_calendar.include_in_primary_score", False)) and not bool(
        cfg_get(config, "fda_designations.include_in_primary_score", False)
    )
    add_check(
        checks,
        name="fda_shadow_boundary",
        source="FDA AdCom calendar and SEC-parsed FDA designations",
        role="shadow",
        required=True,
        passed=shadow_enabled,
        detail="unvalidated FDA overlays remain excluded from the production score",
        evidence={"shadow_only": shadow_enabled},
    )

    blocking = [check.name for check in checks if check.required and check.status != "PASS"]
    advisory = [check.name for check in checks if not check.required and check.status != "PASS"]
    status = "PASS" if not blocking else "FAIL"
    payload = {
        "status": status,
        "acceptance": status,
        "asof_date": asof.isoformat(),
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "score_path": str(scores),
        "score_sha256": file_sha256(scores),
        "source_policy": {
            "primary_scoring": [
                "biotech_screen_results_all.csv",
                "ClinicalTrials.gov API v2",
                "SEC EDGAR filings",
                "SEC XBRL Companyfacts",
                "SEC Form 4",
                "SEC Form 13F",
                "FINRA short interest",
                "Interactive Brokers/TWS borrow and market fallback",
                "Yahoo adjusted prices",
                "SEC/manual float proxies",
            ],
            "shadow_only": ["FDA Advisory Committee calendar", "SEC-parsed FDA designations"],
            "calibration_only": ["Norgate delisted total-return history"],
        },
        "blocking_failures": blocking,
        "advisories": advisory,
        "checks": [asdict(check) for check in checks],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    if blocking:
        raise RuntimeError(f"Biotech source acceptance failed: {', '.join(blocking)}; manifest={manifest_path}")
    print(f"Biotech source acceptance PASS: checks={len(checks)} advisories={len(advisory)} manifest={manifest_path}")


if __name__ == "__main__":
    main()
