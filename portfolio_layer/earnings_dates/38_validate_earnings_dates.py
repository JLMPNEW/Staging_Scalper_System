#!/usr/bin/env python3
"""Validate the sealed earnings-date calendar for a run.

Hard gates (FAIL): manifest/hash integrity, join integrity against stocks_scores,
date validity (ISO, not in the past at fetch time, within horizon), no duplicate
tickers, only allowed sources. Soft gates (WARN): investable coverage below the
configured floor, dates that moved vs the prior snapshot (informational).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.earnings_dates.earnings_common import (  # noqa: E402
    ALLOWED_SOURCES,
    RUN_ARTIFACT_NAME,
    RUN_MANIFEST_NAME,
    coerce_iso_date,
    latest_run_with_artifact,
)


LOGGER = logging.getLogger("validate_earnings_dates")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

VALIDATION_FIELDS = ["check", "status", "detail"]


def add_check(checks: list[dict[str, str]], name: str, status: str, detail: str) -> None:
    checks.append({"check": name, "status": status, "detail": detail})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", default="", help="Run date (default: latest run with an earnings artifact)")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_utc_logging(getattr(logging, str(args.log_level).upper(), logging.INFO))
    config = load_yaml(args.config)
    paths = resolve_runtime_paths(config, args.config.resolve())
    runs_root = paths.output_dir / "runs"

    run_as_of = str(args.as_of).strip() or latest_run_with_artifact(
        runs_root, str(Path("earnings_dates") / RUN_ARTIFACT_NAME)
    )
    if not run_as_of:
        LOGGER.error("No run directory with earnings_dates/%s under %s", RUN_ARTIFACT_NAME, runs_root)
        return 1

    run_dir = runs_root / run_as_of
    artifact_path = run_dir / "earnings_dates" / RUN_ARTIFACT_NAME
    manifest_path = run_dir / "earnings_dates" / RUN_MANIFEST_NAME
    scores_path = run_dir / "stocks_scores.csv"
    checks: list[dict[str, str]] = []

    # 1. Manifest + artifact hash integrity.
    try:
        manifest = read_manifest(manifest_path)
        seal_errors = sealed_artifact_errors(manifest, artifact_path, RUN_ARTIFACT_NAME, run_as_of=run_as_of)
        add_check(
            checks,
            "manifest_seal",
            "FAIL" if seal_errors else "PASS",
            "; ".join(seal_errors) if seal_errors else "acceptance/run/hash verified",
        )
    except (ValueError, OSError) as exc:
        manifest = {}
        add_check(checks, "manifest_seal", "FAIL", str(exc))

    rows = read_csv(artifact_path) if artifact_path.exists() else []
    if not rows:
        add_check(checks, "artifact_rows", "FAIL", f"missing or empty {artifact_path}")

    # 2. Join integrity: exactly the scored universe, no extras, no duplicates.
    if scores_path.exists() and rows:
        scored = {str(r.get("ticker", "")).strip().upper() for r in read_csv(scores_path)}
        scored.discard("")
        got = [str(r.get("ticker", "")).strip().upper() for r in rows]
        got_set = set(got)
        duplicates = len(got) - len(got_set)
        missing = sorted(scored - got_set)
        extras = sorted(got_set - scored)
        include_all = bool(cfg_get(config, "earnings_dates.include_non_investable", True))
        join_ok = not extras and not duplicates and (not missing if include_all else True)
        detail = f"rows={len(got)} scored={len(scored)} missing={len(missing)} extras={len(extras)} dups={duplicates}"
        if missing[:5]:
            detail += f" missing_sample={missing[:5]}"
        if extras[:5]:
            detail += f" extras_sample={extras[:5]}"
        add_check(checks, "join_integrity", "PASS" if join_ok else "FAIL", detail)
    elif rows:
        add_check(checks, "join_integrity", "FAIL", f"missing {scores_path}")

    # 3. Date validity.
    if rows:
        fetched = coerce_iso_date(rows[0].get("fetched_at_utc")) or run_as_of
        fetch_day = date.fromisoformat(fetched)
        max_days = int(cfg_get(config, "earnings_dates.validation.max_days_until", 400))
        bad_iso = past = too_far = 0
        for row in rows:
            raw = str(row.get("next_earnings_date", "")).strip()
            if not raw:
                continue
            parsed = coerce_iso_date(raw)
            if parsed != raw:
                bad_iso += 1
                continue
            when = date.fromisoformat(raw)
            if when < fetch_day:
                past += 1
            elif (when - fetch_day).days > max_days:
                too_far += 1
        status = "FAIL" if (bad_iso or past or too_far) else "PASS"
        add_check(checks, "date_validity", status, f"bad_iso={bad_iso} in_past={past} beyond_{max_days}d={too_far}")

        # 4. Source sanity.
        bad_sources = sorted({str(r.get("source", "")) for r in rows} - set(ALLOWED_SOURCES))
        add_check(
            checks,
            "source_values",
            "FAIL" if bad_sources else "PASS",
            f"unknown_sources={bad_sources}" if bad_sources else f"allowed={sorted(set(str(r.get('source')) for r in rows))}",
        )

        # 5. Investable coverage (WARN-only: far-out reports are legitimately unpublished).
        investable = [r for r in rows if str(r.get("investable_eligible", "")) == "1"]
        dated = [r for r in investable if str(r.get("next_earnings_date", "")).strip()]
        floor_fraction = float(cfg_get(config, "earnings_dates.validation.min_coverage_fraction_investable", 0.60))
        fraction = (len(dated) / len(investable)) if investable else 0.0
        add_check(
            checks,
            "investable_coverage",
            "PASS" if fraction >= floor_fraction else "WARN",
            f"coverage={fraction:.3f} floor={floor_fraction} investable={len(investable)} dated={len(dated)}",
        )

        # 6. Stability diff (informational).
        moved = [r for r in rows if str(r.get("date_changed_flag", "")) == "1"]
        sample = [f"{r['ticker']}:{r['prior_next_earnings_date']}->{r['next_earnings_date']}" for r in moved[:8]]
        add_check(
            checks,
            "dates_changed_vs_prior",
            "PASS" if not moved else "WARN",
            f"changed={len(moved)}" + (f" sample={sample}" if sample else ""),
        )

    failures = [c for c in checks if c["status"] == "FAIL"]
    warnings = [c for c in checks if c["status"] == "WARN"]
    acceptance = "PASS" if not failures else "FAIL"

    validation_dir = run_dir / "earnings_dates" / "validation"
    write_csv(validation_dir / "earnings_validation.csv", VALIDATION_FIELDS, checks)
    summary: dict[str, Any] = {
        "stage": "earnings_dates_validate",
        "run_as_of": run_as_of,
        "acceptance": acceptance,
        "fail_count": len(failures),
        "warn_count": len(warnings),
        "checks": checks,
        "artifact": str(artifact_path),
        "manifest_acceptance": str(manifest.get("acceptance", "")) if manifest else "",
    }
    write_manifest(validation_dir / "earnings_validation_summary.json", summary)

    for check in checks:
        LOGGER.info("[%s] %s -- %s", check["status"], check["check"], check["detail"])
    LOGGER.info("EARNINGS DATES VALIDATION: %s (run=%s, fails=%d, warns=%d)", acceptance, run_as_of, len(failures), len(warnings))
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
