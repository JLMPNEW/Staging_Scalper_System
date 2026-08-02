#!/usr/bin/env python3
"""Independently validate and seal the expectations-monitor OHLCV panel."""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_csv,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path  # noqa: E402
from portfolio_layer.expectations_monitor.market_data_common import (  # noqa: E402
    BENCHMARK_OHLCV_FILENAME,
    SELECTED_OHLCV_FILENAME,
    VALIDATION_FIELDS,
    load_sealed_universe,
    market_artifact_dir,
    market_policy_errors,
    read_gzip_csv,
    row_digest,
    tier0_coverage_status,
)
from portfolio_layer.risk.ohlcv_sources import (  # noqa: E402
    SOURCE_PRIORITY,
    arbitrate_observations,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
EXPECTED_BUILD_OUTPUTS = {
    BENCHMARK_OHLCV_FILENAME,
    SELECTED_OHLCV_FILENAME,
    "monitor_ohlcv_source_observations.csv.gz",
    "monitor_ohlcv_coverage.csv",
    "monitor_ohlcv_disagreements.csv",
    "monitor_ohlcv_fetch_results.csv",
    "monitor_ohlcv_validation.csv",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _check(
    rows: list[dict[str, str]],
    check: str,
    status: str,
    detail: str,
) -> None:
    rows.append({"check": check, "status": status, "detail": detail})


def _float_equal(left: Any, right: Any, *, tolerance: float = 1e-9) -> bool:
    try:
        left_value = float(left)
        right_value = float(right)
    except (TypeError, ValueError):
        return False
    return math.isfinite(left_value) and math.isfinite(right_value) and math.isclose(
        left_value,
        right_value,
        rel_tol=tolerance,
        abs_tol=tolerance,
    )


def _selection_errors(
    selected: list[dict[str, str]],
    observations: list[dict[str, str]],
    *,
    warn_bps: float,
    fail_bps: float,
) -> list[str]:
    recomputed, _diagnostics = arbitrate_observations(
        observations,
        disagreement_warn_bps=warn_bps,
        disagreement_fail_bps=fail_bps,
    )
    actual = {(row["ticker"], row["date"]): row for row in selected}
    expected = {(str(row["ticker"]), str(row["date"])): row for row in recomputed}
    errors: list[str] = []
    if actual.keys() != expected.keys():
        errors.append("selected_key_set_mismatch")
        return errors
    fields = (
        "source",
        "source_count",
        "sources_observed",
        "disagreement_status",
    )
    numeric = (
        "open",
        "high",
        "low",
        "close",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        "raw_volume",
        "volume",
        "adjustment_factor",
        "split_factor",
        "dividend_cash",
        "max_adj_close_disagreement_bps",
    )
    for key in sorted(actual):
        for field in fields:
            if str(actual[key].get(field, "")) != str(expected[key].get(field, "")):
                errors.append(f"{key}:{field}_mismatch")
        for field in numeric:
            if not _float_equal(actual[key].get(field), expected[key].get(field)):
                errors.append(f"{key}:{field}_mismatch")
        if errors:
            break
    return errors


def _input_hash_errors(manifest: dict[str, Any]) -> list[str]:
    inputs = manifest.get("inputs_sha256", {})
    if not isinstance(inputs, dict) or not inputs:
        return ["inputs_sha256_missing"]
    errors: list[str] = []
    for raw_path, expected in inputs.items():
        path = Path(str(raw_path))
        if not path.is_file():
            errors.append(f"input_missing:{path}")
        elif sha256_file(path) != str(expected):
            errors.append(f"input_hash_mismatch:{path}")
    return errors


def run_selftest() -> None:
    observations = [
        {
            "date": "2026-07-30",
            "ticker": "TEST",
            "source": source,
            "open": "10",
            "high": "11",
            "low": "9",
            "close": close,
            "adj_open": "10",
            "adj_high": "11",
            "adj_low": "9",
            "adj_close": close,
            "raw_volume": "100",
            "volume": "100",
            "adjustment_factor": "1",
            "split_factor": "1",
            "dividend_cash": "0",
        }
        for source, close in (("tiingo", "10.01"), ("yahoo", "10"))
    ]
    selected, _diagnostics = arbitrate_observations(observations)
    selected[0]["session_final"] = 1
    assert not _selection_errors(
        [{key: str(value) for key, value in selected[0].items()}],
        observations,
        warn_bps=25.0,
        fail_bps=100.0,
    )
    selected[0]["source"] = "tiingo"
    assert _selection_errors(
        [{key: str(value) for key, value in selected[0].items()}],
        observations,
        warn_bps=25.0,
        fail_bps=100.0,
    )
    print("monitor OHLCV validator selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    policy_errors = market_policy_errors(config)
    if policy_errors:
        raise ValueError(f"Invalid monitor market-data policy: {policy_errors}")
    market = cfg_get(config, "expectations_monitor.market_data", {})
    assert isinstance(market, dict)
    universe_as_of = (args.universe_as_of or args.as_of).isoformat()
    universe, universe_manifest, _sources, _db = load_sealed_universe(
        config,
        config_path,
        universe_as_of=universe_as_of,
    )
    if args.symbols:
        requested = {symbol.strip().upper() for symbol in args.symbols}
        universe = [row for row in universe if str(row["ticker"]) in requested]
        if {str(row["ticker"]) for row in universe} != requested:
            raise ValueError("Validator canary symbols are not all in the sealed universe")
    input_dir = ensure_not_prod_path(
        args.input_dir.resolve()
        if args.input_dir
        else market_artifact_dir(config, config_path, as_of=args.as_of.isoformat()),
        label="monitor market-data validation input",
    )
    manifest_path = input_dir / "monitor_ohlcv_manifest.json"
    selected_path = input_dir / SELECTED_OHLCV_FILENAME
    benchmark_path = input_dir / BENCHMARK_OHLCV_FILENAME
    observations_path = input_dir / "monitor_ohlcv_source_observations.csv.gz"
    coverage_path = input_dir / "monitor_ohlcv_coverage.csv"
    disagreements_path = input_dir / "monitor_ohlcv_disagreements.csv"
    producer_validation_path = input_dir / "monitor_ohlcv_validation.csv"
    fetch_path = input_dir / "monitor_ohlcv_fetch_results.csv"
    validation_path = input_dir / "monitor_ohlcv_independent_validation.csv"
    validation_manifest_path = input_dir / "monitor_ohlcv_validation_manifest.json"
    fail_if_exists([validation_path, validation_manifest_path], force=args.force)
    manifest = read_manifest(manifest_path)
    checks: list[dict[str, str]] = []
    identity_ok = (
        manifest.get("schema_version") == "monitor_ohlcv_manifest_v1"
        and manifest.get("acceptance") in {"PASS", "PASS_WITH_WARNINGS"}
        and manifest.get("as_of_date") == args.as_of.isoformat()
        and manifest.get("universe_as_of") == universe_as_of
        and manifest.get("source_priority") == list(SOURCE_PRIORITY)
        and manifest.get("prices_averaged") is False
    )
    _check(checks, "producer_manifest_identity", "PASS" if identity_ok else "FAIL", str(identity_ok))
    outputs = manifest.get("outputs_sha256", {})
    output_errors: list[str] = []
    if not isinstance(outputs, dict) or set(outputs) != EXPECTED_BUILD_OUTPUTS:
        output_errors.append("output_set_mismatch")
    else:
        for filename, expected in outputs.items():
            path = input_dir / filename
            if not path.is_file():
                output_errors.append(f"missing:{filename}")
            elif sha256_file(path) != str(expected):
                output_errors.append(f"hash_mismatch:{filename}")
    _check(
        checks,
        "producer_outputs_hash_sealed",
        "PASS" if not output_errors else "FAIL",
        str(output_errors[:20]),
    )
    input_errors = _input_hash_errors(manifest)
    _check(
        checks,
        "producer_inputs_and_code_current",
        "PASS" if not input_errors else "FAIL",
        str(input_errors[:20]),
    )
    selected = read_gzip_csv(selected_path)
    benchmarks = read_gzip_csv(benchmark_path)
    observations = read_gzip_csv(observations_path)
    coverage = read_csv(coverage_path)
    disagreements = read_csv(disagreements_path)
    producer_checks = read_csv(producer_validation_path)
    fetch_rows = read_csv(fetch_path)
    digest_errors: list[str] = []
    if row_digest(selected) != manifest.get("selected_row_digest"):
        digest_errors.append("selected_digest_mismatch")
    if row_digest(observations) != manifest.get("source_observation_digest"):
        digest_errors.append("observations_digest_mismatch")
    if row_digest(benchmarks) != manifest.get("benchmark_row_digest"):
        digest_errors.append("benchmark_digest_mismatch")
    _check(
        checks,
        "semantic_row_digests",
        "PASS" if not digest_errors else "FAIL",
        str(digest_errors),
    )
    selection_errors = _selection_errors(
        selected,
        observations,
        warn_bps=float(market.get("disagreement_warn_bps", 25.0)),
        fail_bps=float(market.get("disagreement_fail_bps", 100.0)),
    )
    _check(
        checks,
        "selection_recomputed_from_sources",
        "PASS" if not selection_errors else "FAIL",
        str(selection_errors[:20]),
    )
    final_date = str(manifest.get("final_market_date", ""))
    date_errors = sum(
        str(row.get("date", "")) > final_date or row.get("session_final") != "1"
        for row in selected
    )
    _check(
        checks,
        "daily_bar_finality",
        "PASS" if final_date and not date_errors else "FAIL",
        f"final_market_date={final_date}; invalid_rows={date_errors}",
    )
    required_benchmarks = {
        "SPY",
        *(
            str(value).strip().upper()
            for value in dict(
                cfg_get(config, "risk_panel.sector_etf_map", {}) or {}
            ).values()
            if str(value).strip()
        ),
    }
    benchmark_names = {str(row.get("ticker", "")) for row in benchmarks}
    benchmark_latest = {
        str(row.get("ticker", ""))
        for row in benchmarks
        if str(row.get("date", "")) == final_date
    }
    benchmark_errors = sum(
        str(row.get("date", "")) > final_date
        or row.get("session_final") != "1"
        or row.get("source") != "yahoo"
        for row in benchmarks
    )
    benchmark_ok = (
        benchmark_names == required_benchmarks
        and benchmark_latest == required_benchmarks
        and not benchmark_errors
        and set(manifest.get("benchmark_tickers", [])) == required_benchmarks
    )
    _check(
        checks,
        "same_date_benchmarks_sealed",
        "PASS" if benchmark_ok else "FAIL",
        (
            f"required={sorted(required_benchmarks)}; "
            f"names={sorted(benchmark_names)}; latest={sorted(benchmark_latest)}; "
            f"invalid_rows={benchmark_errors}"
        ),
    )
    adjustment_errors = sum(
        float(row["adjustment_factor"]) <= 0
        or float(row["split_factor"]) <= 0
        or float(row["dividend_cash"]) < 0
        or not math.isclose(
            float(row["adj_close"]),
            float(row["close"]) * float(row["adjustment_factor"]),
            rel_tol=1e-6,
            abs_tol=1e-8,
        )
        for row in selected
    )
    _check(
        checks,
        "corporate_action_adjustment_consistency",
        "PASS" if not adjustment_errors else "FAIL",
        f"invalid_rows={adjustment_errors}",
    )
    selected_names = {row.get("ticker", "") for row in selected}
    universe_names = {str(row["ticker"]) for row in universe}
    coverage_names = {row.get("ticker", "") for row in coverage}
    universe_ok = coverage_names == universe_names and selected_names <= universe_names
    _check(
        checks,
        "sealed_universe_coverage_identity",
        "PASS" if universe_ok else "FAIL",
        f"universe={len(universe_names)}; coverage={len(coverage_names)}; selected={len(selected_names)}",
    )
    tier0 = [row for row in coverage if row.get("tier") == "tier0"]
    tier0_latest = (
        sum(row.get("latest_session_present") == "1" for row in tier0) / len(tier0)
        if tier0
        else 1.0
    )
    tier0_floor = float(market.get("tier0_latest_coverage_floor", 0.98))
    tier0_hard_floor = float(market.get("tier0_latest_coverage_hard_floor", 0.90))
    _check(
        checks,
        "tier0_latest_coverage_recomputed",
        tier0_coverage_status(
            tier0_latest, target=tier0_floor, hard_floor=tier0_hard_floor
        ),
        (
            f"coverage={tier0_latest:.4f}; target={tier0_floor:.4f}; "
            f"hard_floor={tier0_hard_floor:.4f}"
        ),
    )
    latest_selected = {
        str(row.get("ticker", ""))
        for row in selected
        if str(row.get("date", "")) == final_date
    }
    latest_status_errors = [
        str(row.get("ticker", ""))
        for row in coverage
        if (row.get("latest_session_present") == "1")
        != (str(row.get("ticker", "")) in latest_selected)
    ]
    _check(
        checks,
        "per_name_latest_status_recomputed",
        "PASS" if not latest_status_errors else "FAIL",
        f"mismatches={latest_status_errors[:20]}",
    )
    failed_conflicts = sum(
        row.get("status") == "FAIL" and row.get("date") == final_date
        for row in disagreements
    )
    historical_conflicts = sum(
        row.get("status") == "FAIL" and row.get("date") != final_date
        for row in disagreements
    )
    _check(
        checks,
        "latest_provider_conflicts_fail_closed",
        "PASS" if not failed_conflicts else "FAIL",
        f"latest_failed_conflicts={failed_conflicts}",
    )
    _check(
        checks,
        "historical_provider_conflicts_retained",
        "PASS",
        f"historical_failed_threshold_rows={historical_conflicts}",
    )
    producer_failures = [row for row in producer_checks if row.get("status") == "FAIL"]
    _check(
        checks,
        "producer_hard_gates_clean",
        "PASS" if not producer_failures else "FAIL",
        f"producer_failures={len(producer_failures)}",
    )
    providers = {row.get("provider", "") for row in fetch_rows}
    _check(
        checks,
        "provider_attempts_audited",
        "PASS" if "yahoo" in providers else "FAIL",
        f"providers={sorted(providers)}",
    )
    failures = [row for row in checks if row["status"] == "FAIL"]
    warnings = [row for row in [*producer_checks, *checks] if row.get("status") == "WARN"]
    acceptance = "FAIL" if failures else "PASS_WITH_WARNINGS" if warnings else "PASS"
    write_csv(validation_path, VALIDATION_FIELDS, checks)
    write_manifest(
        validation_manifest_path,
        {
            "schema_version": "monitor_ohlcv_validation_manifest_v1",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "universe_as_of": universe_as_of,
            "final_market_date": final_date,
            "latest_deferred_tickers": sorted(
                str(row.get("ticker", ""))
                for row in coverage
                if row.get("latest_session_present") != "1"
            ),
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "producer_manifest_path": str(manifest_path),
            "producer_manifest_sha256": sha256_file(manifest_path),
            "universe_manifest_path": str(universe_manifest),
            "universe_manifest_sha256": sha256_file(universe_manifest),
            "inputs_sha256": {
                str(config_path): sha256_file(config_path),
                str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
                str(Path(__file__).with_name("market_data_common.py").resolve()): sha256_file(
                    Path(__file__).with_name("market_data_common.py").resolve()
                ),
                str(PACKAGE_ROOT / "risk" / "ohlcv_sources.py"): sha256_file(
                    PACKAGE_ROOT / "risk" / "ohlcv_sources.py"
                ),
            },
            "outputs_sha256": {validation_path.name: sha256_file(validation_path)},
        },
    )
    print(f"MONITOR OHLCV VALIDATION: {acceptance}")
    print(f"checks={len(checks)}; failures={len(failures)}; manifest={validation_manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
