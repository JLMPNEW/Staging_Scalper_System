#!/usr/bin/env python3
"""Validate the med-devices Stage 1 adapter handoff to the portfolio layer.

This is a focused validation for days where the med-devices sector has refreshed before the
other required sectors. It exercises the real portfolio-layer adapter and verifies that the
optimizer-eligible med-device set is exactly the sector's published portfolio-candidate set.
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

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import read_csv, write_csv  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.scores.adapters import _truthy as adapter_truthy, run_adapter  # noqa: E402


LOGGER = logging.getLogger("validate_med_devices_adapter_handoff")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
NEGATIVE_ANALYST_DECISIONS = {"reject", "data_fix_needed"}
# The adapter demotes gate=1 rows fail-closed (pre-lock oos_score_valid=0, candidate status,
# missing-score sentinels). Candidate-set comparisons must recognize those reasons, or every
# historical pre-lock date false-fails (see 03.validate_med_devices_handoff).
DEMOTION_PREFIXES = ("not_oos_score_valid", "missing_score", "failed_portfolio_candidate_gate")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate med-devices portfolio-layer adapter handoff.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=iso_date_arg, required=True)
    parser.add_argument(
        "--expected-candidates",
        default="",
        help="Optional comma-separated ticker set expected to be optimizer-eligible.",
    )
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def truthy(value: Any) -> bool:
    # gate flags must parse with the exact adapter semantics, or the set comparisons below can
    # false-fail on values the adapter accepts (e.g. "2", padded "True")
    return adapter_truthy(value)


def ticker_set(rows: list[dict[str, str]], *, gate_only: bool = False) -> set[str]:
    out: set[str] = set()
    for row in rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        if gate_only and not truthy(row.get("portfolio_candidate_gate")):
            continue
        out.add(ticker)
    return out


def record(checks: list[dict[str, str]], check_id: str, passed: bool, details: str) -> None:
    checks.append({
        "check_id": check_id,
        "status": "PASS" if passed else "FAIL",
        "details": details,
    })


def find_med_devices_config(config: dict[str, Any]) -> dict[str, Any]:
    for sector in cfg_get(config, "score_contract.sectors", []) or []:
        if str(sector.get("model_family", "")).strip() == "med_devices":
            return dict(sector)
    raise ValueError("score_contract.sectors has no med_devices entry")


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    sector_root = resolve_path(
        cfg_get(config, "score_contract.sector_output_root", "../output"),
        base_dir=config_path.parent,
    )
    med_cfg = find_med_devices_config(config)
    if args.output_csv:
        output_csv = args.output_csv.expanduser().resolve()
    else:
        run_dir = paths.output_dir / "runs" / args.as_of
        # never create a phantom run directory for a date that has no Stage 1 run
        output_csv = (
            run_dir / "validation" / "med_devices_adapter_handoff_validation.csv"
            if run_dir.exists()
            else paths.output_dir / "validation" / f"med_devices_adapter_handoff_{args.as_of}.csv"
        )
    try:
        result = run_adapter(med_cfg, sector_root, args.as_of)
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.error("Med-devices adapter failed for %s: %s", args.as_of, exc)
        # a failing rerun must not leave a previous PASS artifact in place at the same path
        write_csv(
            output_csv,
            ["check_id", "status", "details"],
            [{"check_id": "adapter_execution", "status": "FAIL", "details": str(exc)[:300]}],
        )
        return 1
    source_rows = read_csv(result.source_file)
    review_dir = result.source_file.parent
    candidate_csv = review_dir / "med_device_score_review_portfolio_candidates.csv"
    candidate_rows = read_csv(candidate_csv) if candidate_csv.exists() else []
    expected = {
        item.strip().upper()
        for item in args.expected_candidates.split(",")
        if item.strip()
    }
    source_gate_tickers = ticker_set(source_rows, gate_only=True)
    candidate_tickers = ticker_set(candidate_rows)
    adapter_investable_tickers = {
        row.ticker for row in result.rows if int(row.investable_eligible) == 1
    }
    negative_source_tickers = sorted(
        str(row.get("ticker", "")).strip().upper()
        for row in source_rows
        if truthy(row.get("portfolio_candidate_gate"))
        and str(row.get("analyst_review_decision", "")).strip().lower() in NEGATIVE_ANALYST_DECISIONS
    )
    negative_candidate_tickers = sorted(
        str(row.get("ticker", "")).strip().upper()
        for row in candidate_rows
        if str(row.get("analyst_review_decision", "")).strip().lower() in NEGATIVE_ANALYST_DECISIONS
    )

    checks: list[dict[str, str]] = []
    record(checks, "source_csv_exists", result.source_file.exists(), str(result.source_file))
    record(
        checks,
        "source_csv_is_daily_composite",
        result.source_file.name == "med_device_daily_composite_scores.csv",
        f"source={result.source_file.name}",
    )
    record(checks, "candidate_csv_exists", candidate_csv.exists(), str(candidate_csv))
    # an empty candidate CSV is legitimate only when the daily surface also gates nothing;
    # a truncated candidate file on a day with gate=1 rows must still fail closed
    record(
        checks,
        "candidate_csv_rows_all_gate_true",
        (bool(candidate_rows) or not source_gate_tickers)
        and all(truthy(row.get("portfolio_candidate_gate")) for row in candidate_rows),
        f"rows={len(candidate_rows)}; daily_gate_rows={len(source_gate_tickers)}",
    )
    record(
        checks,
        "daily_gate_matches_candidate_csv",
        source_gate_tickers == candidate_tickers,
        f"daily_minus_candidate={sorted(source_gate_tickers - candidate_tickers)}; "
        f"candidate_minus_daily={sorted(candidate_tickers - source_gate_tickers)}",
    )
    # Demotion-aware candidate comparison: investable must never exceed the candidate set, and a
    # candidate missing from investable must carry a recognized fail-closed adapter demotion
    # (pre-lock oos=0, candidate status, missing-score sentinel) — otherwise the handoff leaked.
    adapter_reason_by_ticker = {row.ticker: str(row.eligibility_reason) for row in result.rows}
    extra_investable = sorted(adapter_investable_tickers - candidate_tickers)
    unexplained_demotions = sorted(
        f"{t}:reason={adapter_reason_by_ticker.get(t, '<dropped>')[:60]}"
        for t in candidate_tickers - adapter_investable_tickers
        if not adapter_reason_by_ticker.get(t, "").startswith(DEMOTION_PREFIXES)
    )
    record(
        checks,
        "adapter_investable_within_candidate_csv",
        not extra_investable,
        f"investable_beyond_candidates={extra_investable}",
    )
    record(
        checks,
        "candidate_demotions_recognized",
        not unexplained_demotions,
        f"unexplained={unexplained_demotions[:10]}"
        if unexplained_demotions else
        f"candidates={len(candidate_tickers)}; investable={len(adapter_investable_tickers)}; "
        f"demoted_fail_closed={len(candidate_tickers - adapter_investable_tickers)}",
    )
    if candidate_tickers and not adapter_investable_tickers:
        # all-demoted is legitimate on pre-lock historical dates but must never pass silently
        LOGGER.warning(
            "All %d candidate tickers demoted fail-closed; optimizer receives zero med-device names",
            len(candidate_tickers),
        )
    record(
        checks,
        "negative_analyst_decisions_excluded",
        not negative_source_tickers and not negative_candidate_tickers,
        f"source_gate_negative={negative_source_tickers}; candidate_negative={negative_candidate_tickers}",
    )
    if expected:
        record(
            checks,
            "adapter_investable_matches_expected",
            adapter_investable_tickers == expected,
            f"adapter={sorted(adapter_investable_tickers)}; expected={sorted(expected)}",
        )

    write_csv(output_csv, ["check_id", "status", "details"], checks)
    failed = [check for check in checks if check["status"] != "PASS"]
    LOGGER.info("source_file=%s", result.source_file)
    LOGGER.info("candidate_file=%s", candidate_csv)
    LOGGER.info("adapter_investable_tickers=%s", ",".join(sorted(adapter_investable_tickers)))
    LOGGER.info("validation_csv=%s", output_csv)
    if failed:
        LOGGER.error("Med-devices adapter handoff validation failed: %s", failed)
        return 1
    LOGGER.info("Med-devices adapter handoff validation PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
