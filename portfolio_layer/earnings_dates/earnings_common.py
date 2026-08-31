"""Shared schema and helpers for the point-in-time earnings-date calendar.

The calendar is not an allocation signal, but it is a required input to the expectations
monitor, levels engine, and enriched final report. Every sync appends to persistent
history so date changes are tracked from the first observation without backdating later
provider knowledge into an earlier portfolio run.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from portfolio_layer.core.contracts import (
    manifest_acceptance_value,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_csv,
)


# One row per Stage 1 contract ticker, keyed to the sealed run it was joined against.
EARNINGS_CALENDAR_FIELDS = [
    "run_as_of_date",  # sealed Stage 1 run this row is keyed to
    "fetched_at_utc",  # provider fetch timestamp (PIT stamp)
    "ticker",  # Stage 1 contract ticker
    "query_symbol",  # provider symbol actually matched/queried
    "investable_eligible",  # 0/1 carried from stocks_scores
    "source_pipeline",
    "sector",
    "next_earnings_date",  # YYYY-MM-DD; empty when no reliable date found
    "days_until",  # calendar days from fetch date; empty when unknown
    "fiscal_date_ending",  # Alpha Vantage bulk fiscalDateEnding when available
    "av_eps_estimate",  # Alpha Vantage bulk EPS estimate when available
    "source",  # alpha_vantage_bulk | yahoo_finance | gemini_search_grounded | none
    "confidence",  # gemini-reported confidence (high|medium|low) when applicable
    "source_urls",  # gemini grounding URLs, " | " joined
    "prior_next_earnings_date",  # latest earlier history snapshot's date for this ticker
    "date_changed_flag",  # 0/1 next date differs from the prior snapshot
    "error_detail",  # per-provider error trail when no date found
]

ALLOWED_SOURCES = (
    "alpha_vantage_bulk",
    "yahoo_finance",
    "gemini_search_grounded",
    # Prior still-future date retained when every provider was unavailable this
    # run (budget exhaustion / errors); never used after a definitive empty.
    "carried_forward_prior",
    "none",
)

HISTORY_RELATIVE_PATH = Path("earnings_dates") / "earnings_calendar_history.csv"
RUN_ARTIFACT_NAME = "earnings_calendar.csv"
RUN_MANIFEST_NAME = "earnings_manifest.json"

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def coerce_iso_date(raw: Any) -> str | None:
    """Extract a valid YYYY-MM-DD from arbitrary provider output, or None."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    match = _DATE_RE.search(text)
    if not match:
        return None
    candidate = match.group(0)
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def active_query_symbol(ticker: str, aliases: dict[str, Any], run_as_of: str) -> str:
    """Map a contract ticker onto its active market-data symbol (Stage 2 alias rule)."""
    entry = aliases.get(ticker)
    if not isinstance(entry, dict):
        return ticker
    active = str(entry.get("active_ticker", "")).strip().upper()
    effective = str(entry.get("effective_date", "")).strip()
    if not active:
        return ticker
    if effective and run_as_of < effective:
        return ticker
    return active


def symbol_variants(symbol: str) -> list[str]:
    """Punctuation variants providers disagree on (class shares: HEI-A vs HEI.A)."""
    upper = symbol.strip().upper()
    variants = [upper, upper.replace("-", "."), upper.replace(".", "-")]
    out: list[str] = []
    for v in variants:
        if v and v not in out:
            out.append(v)
    return out


def latest_run_with_artifact(runs_root: Path, artifact: str) -> str | None:
    """Latest YYYY-MM-DD run directory containing `artifact`."""
    candidates: list[str] = []
    children = runs_root.iterdir() if runs_root.exists() else []
    for child in children:
        if not child.is_dir():
            continue
        try:
            date.fromisoformat(child.name)
        except ValueError:
            continue
        if (child / artifact).exists():
            candidates.append(child.name)
    return max(candidates) if candidates else None


def latest_accepted_stock_ledger(runs_root: Path, run_as_of: str) -> Path | None:
    """Return the latest sealed PASS stock ledger on or before ``run_as_of``."""
    children = runs_root.iterdir() if runs_root.exists() else []
    candidates: list[Path] = []
    for child in children:
        if not child.is_dir() or child.name > run_as_of:
            continue
        try:
            date.fromisoformat(child.name)
        except ValueError:
            continue
        if (child / "ledger" / "ledger_manifest.json").is_file():
            candidates.append(child)
    for child in sorted(candidates, key=lambda path: path.name, reverse=True):
        ledger_dir = child / "ledger"
        artifact = ledger_dir / "broker_net_stock_positions.csv"
        manifest_path = ledger_dir / "ledger_manifest.json"
        try:
            manifest = read_manifest(manifest_path)
            if manifest_acceptance_value(manifest) != "PASS":
                continue
            errors = sealed_artifact_errors(
                manifest,
                artifact,
                "broker_net_stock_positions",
                "broker_net_stock_positions.csv",
                run_as_of=child.name,
            )
        except (OSError, ValueError):
            continue
        if not errors:
            return child
    return None


def load_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return read_csv(path)


def latest_prior_dates(history_rows: list[dict[str, str]]) -> dict[str, str]:
    """ticker -> next_earnings_date from the most recent prior snapshot carrying a date."""
    best: dict[str, tuple[str, str]] = {}
    for row in history_rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        fetched = str(row.get("fetched_at_utc", "")).strip()
        next_date = str(row.get("next_earnings_date", "")).strip()
        if not ticker or not next_date:
            continue
        current = best.get(ticker)
        if current is None or fetched > current[0]:
            best[ticker] = (fetched, next_date)
    return {ticker: stamp[1] for ticker, stamp in best.items()}


def pipeline_coverage_summary(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return deterministic earnings-date coverage by Stage 1 source pipeline."""
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        pipeline = str(row.get("source_pipeline", "")).strip()
        counts = grouped.setdefault(
            pipeline,
            {
                "universe_count": 0,
                "investable_count": 0,
                "investable_with_date": 0,
            },
        )
        counts["universe_count"] += 1
        if str(row.get("investable_eligible", "")).strip() != "1":
            continue
        counts["investable_count"] += 1
        if str(row.get("next_earnings_date", "")).strip():
            counts["investable_with_date"] += 1

    summary: list[dict[str, Any]] = []
    for pipeline, counts in sorted(grouped.items()):
        investable = counts["investable_count"]
        dated = counts["investable_with_date"]
        summary.append(
            {
                "source_pipeline": pipeline,
                **counts,
                "investable_coverage_fraction": (round(dated / investable, 6) if investable else 0.0),
            }
        )
    return summary


def assess_pipeline_coverage(
    rows: list[dict[str, Any]],
    *,
    minimum_investable_count: int,
    minimum_coverage_fraction: float,
    provider_network_calls_allowed: bool,
) -> dict[str, Any]:
    """Classify per-pipeline coverage without treating PIT deferral as live success."""
    if minimum_investable_count < 1:
        raise ValueError("minimum_investable_count must be >= 1")
    if not 0.0 <= minimum_coverage_fraction <= 1.0:
        raise ValueError("minimum_coverage_fraction must be in [0, 1]")

    summary = pipeline_coverage_summary(rows)
    eligible = [item for item in summary if int(item["investable_count"]) >= minimum_investable_count]
    zero_coverage = [str(item["source_pipeline"]) for item in eligible if int(item["investable_with_date"]) == 0]
    below_floor = [
        str(item["source_pipeline"])
        for item in eligible
        if int(item["investable_with_date"]) > 0
        and float(item["investable_coverage_fraction"]) < minimum_coverage_fraction
    ]
    deferred = bool(zero_coverage) and not provider_network_calls_allowed
    if zero_coverage and provider_network_calls_allowed:
        status = "FAIL"
    elif zero_coverage or below_floor:
        status = "WARN"
    else:
        status = "PASS"
    return {
        "status": status,
        "deferred": deferred,
        "zero_coverage_pipelines": zero_coverage,
        "below_floor_pipelines": below_floor,
        "pipeline_coverage": summary,
    }


def append_history(path: Path, existing: list[dict[str, str]], new_rows: list[dict[str, Any]]) -> int:
    """Atomic append: rewrite the full history file with the new snapshot included."""
    combined: list[dict[str, Any]] = list(existing)
    combined.extend(new_rows)
    return write_csv(path, EARNINGS_CALENDAR_FIELDS, combined)


def source_hashes(package_root: Path, files: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in files:
        path = package_root / "earnings_dates" / name
        if path.exists():
            out[name] = sha256_file(path)
    return out
