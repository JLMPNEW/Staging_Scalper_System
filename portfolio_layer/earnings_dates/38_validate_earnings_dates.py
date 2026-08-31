#!/usr/bin/env python3
"""Validate the sealed earnings-date calendar for a run.

Hard gates (FAIL): manifest/hash integrity, join integrity against the scored-plus-held
equity universe,
date validity (ISO, not in the past at fetch time, within horizon), no duplicate
tickers, only allowed sources, and zero coverage for a material source pipeline on
a live provider refresh. Soft gates (WARN): low investable coverage, PIT-unavailable
pipeline coverage during historical catch-up, and dates that moved vs the prior
snapshot (informational).
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
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.earnings_dates.earnings_common import (  # noqa: E402
    ALLOWED_SOURCES,
    RUN_ARTIFACT_NAME,
    RUN_MANIFEST_NAME,
    assess_pipeline_coverage,
    coerce_iso_date,
    latest_accepted_stock_ledger,
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
    pipeline_assessment: dict[str, Any] | None = None
    network_allowed = False

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

    network_raw = manifest.get("provider_network_calls_allowed")
    roll_forward_raw = manifest.get("roll_forward_only")
    provider_mode_ok = (
        isinstance(network_raw, bool) and isinstance(roll_forward_raw, bool) and roll_forward_raw is (not network_raw)
    )
    if isinstance(network_raw, bool):
        network_allowed = network_raw
    add_check(
        checks,
        "provider_mode_contract",
        "PASS" if provider_mode_ok else "FAIL",
        (f"network_calls_allowed={network_raw!r} roll_forward_only={roll_forward_raw!r}"),
    )

    rows = read_csv(artifact_path) if artifact_path.exists() else []
    if not rows:
        add_check(checks, "artifact_rows", "FAIL", f"missing or empty {artifact_path}")

    # 2. Join integrity: exactly the configured scored universe plus sealed stock holdings.
    if scores_path.exists() and rows:
        score_rows = read_csv(scores_path)
        include_all = bool(cfg_get(config, "earnings_dates.include_non_investable", True))
        scored_meta: dict[str, dict[str, str]] = {}
        for score_row in score_rows:
            ticker = str(score_row.get("ticker", "")).strip().upper()
            investable_flag = "1" if str(score_row.get("investable_eligible", "")).strip() == "1" else "0"
            if not ticker or ticker in scored_meta or (not include_all and investable_flag != "1"):
                continue
            scored_meta[ticker] = {
                "investable_eligible": investable_flag,
                "source_pipeline": str(score_row.get("source_pipeline", "")).strip(),
                "sector": str(score_row.get("sector", "")).strip(),
            }
        scored = set(scored_meta)
        ledger_run = latest_accepted_stock_ledger(runs_root, run_as_of)
        held: set[str] = set()
        if ledger_run is not None:
            held = {
                str(r.get("symbol", "")).strip().upper()
                for r in read_csv(ledger_run / "ledger" / "broker_net_stock_positions.csv")
            }
            held.discard("")
        expected = scored | held
        got = [str(r.get("ticker", "")).strip().upper() for r in rows]
        got_set = set(got)
        got_by_ticker = {
            str(row.get("ticker", "")).strip().upper(): row for row in rows if str(row.get("ticker", "")).strip()
        }
        duplicates = len(got) - len(got_set)
        missing = sorted(expected - got_set)
        extras = sorted(got_set - expected)
        lineage_mismatches: list[str] = []
        for ticker, expected_meta in scored_meta.items():
            actual = got_by_ticker.get(ticker)
            if actual is None:
                continue
            for field, expected_value in expected_meta.items():
                actual_value = str(actual.get(field, "")).strip()
                if actual_value != expected_value:
                    lineage_mismatches.append(f"{ticker}:{field}:{actual_value!r}!={expected_value!r}")
        manifest_ledger_as_of = str(manifest.get("ledger_as_of", ""))
        expected_ledger_as_of = ledger_run.name if ledger_run is not None else ""
        ledger_lineage_ok = manifest_ledger_as_of == expected_ledger_as_of
        join_ok = not extras and not duplicates and not missing and not lineage_mismatches and ledger_lineage_ok
        detail = (
            f"rows={len(got)} scored={len(scored)} held={len(held)} "
            f"expected={len(expected)} missing={len(missing)} extras={len(extras)} "
            f"dups={duplicates} lineage_mismatches={len(lineage_mismatches)} "
            f"ledger={manifest_ledger_as_of or 'none'}"
        )
        if missing[:5]:
            detail += f" missing_sample={missing[:5]}"
        if extras[:5]:
            detail += f" extras_sample={extras[:5]}"
        if lineage_mismatches[:5]:
            detail += f" lineage_sample={lineage_mismatches[:5]}"
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
            f"unknown_sources={bad_sources}"
            if bad_sources
            else f"allowed={sorted(set(str(r.get('source')) for r in rows))}",
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

        # 6. Pipeline-level coverage. Aggregate coverage can conceal a newly
        # activated source pipeline with no dates at all. A live provider run
        # must fail in that state; a historical replay cannot legally fetch
        # newer data, so it is explicitly deferred rather than backdated.
        minimum_pipeline_count = int(
            cfg_get(
                config,
                "earnings_dates.validation.minimum_pipeline_investable_count",
                3,
            )
        )
        pipeline_floor = float(
            cfg_get(
                config,
                "earnings_dates.validation.min_coverage_fraction_by_pipeline",
                floor_fraction,
            )
        )
        pipeline_assessment = assess_pipeline_coverage(
            rows,
            minimum_investable_count=minimum_pipeline_count,
            minimum_coverage_fraction=pipeline_floor,
            provider_network_calls_allowed=network_allowed,
        )
        pipeline_coverage = pipeline_assessment["pipeline_coverage"]
        sealed_pipeline_coverage = manifest.get("pipeline_coverage")
        pipeline_manifest_ok = sealed_pipeline_coverage == pipeline_coverage
        add_check(
            checks,
            "pipeline_coverage_manifest",
            "PASS" if pipeline_manifest_ok else "FAIL",
            "sealed summary matches calendar"
            if pipeline_manifest_ok
            else "missing or stale per-pipeline coverage summary",
        )
        coverage_parts = [
            (f"{item['source_pipeline'] or '<missing>'}:{item['investable_with_date']}/{item['investable_count']}")
            for item in pipeline_coverage
            if int(item["investable_count"]) > 0
        ]
        add_check(
            checks,
            "pipeline_investable_coverage",
            str(pipeline_assessment["status"]),
            (
                f"live_provider_refresh={network_allowed} "
                f"minimum_names={minimum_pipeline_count} floor={pipeline_floor:.3f} "
                f"zero={pipeline_assessment['zero_coverage_pipelines']} "
                f"below_floor={pipeline_assessment['below_floor_pipelines']} "
                f"coverage={coverage_parts}"
            ),
        )

        # 7. Stability diff (informational).
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
    pipeline_deferred = bool(rows and pipeline_assessment is not None and bool(pipeline_assessment["deferred"]))
    if failures:
        acceptance = "FAIL"
    elif pipeline_deferred:
        acceptance = "PASS_WITH_DEFERRED"
    else:
        acceptance = "PASS"

    validation_dir = run_dir / "earnings_dates" / "validation"
    write_csv(validation_dir / "earnings_validation.csv", VALIDATION_FIELDS, checks)
    pipeline_coverage_value = pipeline_assessment["pipeline_coverage"] if pipeline_assessment is not None else []
    deferred_pipeline_value = (
        pipeline_assessment["zero_coverage_pipelines"] if pipeline_assessment is not None and pipeline_deferred else []
    )
    summary: dict[str, Any] = {
        "stage": "earnings_dates_validate",
        "run_as_of": run_as_of,
        "acceptance": acceptance,
        "fail_count": len(failures),
        "warn_count": len(warnings),
        "checks": checks,
        "artifact": str(artifact_path),
        "manifest_acceptance": str(manifest.get("acceptance", "")) if manifest else "",
        "provider_network_calls_allowed": network_allowed,
        "pipeline_coverage": pipeline_coverage_value,
        "deferred_pipeline_coverage": deferred_pipeline_value,
        "inputs_sha256": {
            str(artifact_path.resolve()): sha256_file(artifact_path),
            str(manifest_path.resolve()): sha256_file(manifest_path),
            str(scores_path.resolve()): sha256_file(scores_path),
        }
        if artifact_path.is_file() and manifest_path.is_file() and scores_path.is_file()
        else {},
        "provenance": {
            "validator_source": {
                "path": str(Path(__file__).resolve()),
                "sha256": sha256_file(Path(__file__).resolve()),
            },
            "sync_source": {
                "path": str((PACKAGE_ROOT / "earnings_dates" / "37_sync_earnings_dates.py").resolve()),
                "sha256": sha256_file(PACKAGE_ROOT / "earnings_dates" / "37_sync_earnings_dates.py"),
            },
            "common_source": {
                "path": str((PACKAGE_ROOT / "earnings_dates" / "earnings_common.py").resolve()),
                "sha256": sha256_file(PACKAGE_ROOT / "earnings_dates" / "earnings_common.py"),
            },
        },
    }
    write_manifest(validation_dir / "earnings_validation_summary.json", summary)

    for check in checks:
        LOGGER.info("[%s] %s -- %s", check["status"], check["check"], check["detail"])
    LOGGER.info(
        "EARNINGS DATES VALIDATION: %s (run=%s, fails=%d, warns=%d)",
        acceptance,
        run_as_of,
        len(failures),
        len(warnings),
    )
    return 0 if acceptance.startswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
