#!/usr/bin/env python3
"""Seal an Alpha Vantage versus FMP estimate-capability pilot decision."""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.contracts import (  # noqa: E402
    sha256_file,
    write_csv,
    write_manifest,
    write_text_atomic,
)
from portfolio_layer.core.config import load_yaml  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.provider_common import load_entitlements  # noqa: E402


DEFAULT_ENTITLEMENTS = Path(__file__).with_name("provider_entitlements.yaml")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REVISION_FIELDS = frozenset(
    {
        "eps_estimate_average_7_days_ago",
        "eps_estimate_average_30_days_ago",
        "eps_estimate_average_60_days_ago",
        "eps_estimate_average_90_days_ago",
        "eps_estimate_revision_up_trailing_7_days",
        "eps_estimate_revision_down_trailing_7_days",
        "eps_estimate_revision_up_trailing_30_days",
        "eps_estimate_revision_down_trailing_30_days",
    }
)
OUTPUT_FIELDS = [
    "symbol",
    "alpha_status",
    "alpha_rows",
    "alpha_fields",
    "fmp_status",
    "fmp_rows",
    "fmp_fields",
    "alpha_nonempty",
    "fmp_nonempty",
    "alpha_unique_revision_fields",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--alpha-results", type=Path, nargs="+")
    parser.add_argument("--fmp-results", type=Path)
    parser.add_argument("--entitlements", type=Path, default=DEFAULT_ENTITLEMENTS)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _read_rows(paths: Iterable[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        with path.resolve().open("r", encoding="utf-8-sig", newline="") as handle:
            rows.extend(dict(row) for row in csv.DictReader(handle))
    return rows


def _latest_by_symbol(rows: Iterable[dict[str, str]], *, provider: str, capability: str) -> dict[str, dict[str, str]]:
    selected: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.get("provider") != provider or row.get("capability") != capability:
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        prior = selected.get(symbol)
        if prior is None or str(row.get("requested_at_utc", "")) >= str(prior.get("requested_at_utc", "")):
            selected[symbol] = row
    return selected


def _field_set(row: dict[str, str] | None) -> set[str]:
    if row is None:
        return set()
    return {value.strip() for value in str(row.get("field_names", "")).split(",") if value.strip()}


def _row_count(row: dict[str, str] | None) -> int:
    if row is None:
        return 0
    try:
        return int(row.get("row_count", "0"))
    except ValueError:
        return 0


def evaluate(
    alpha_rows: list[dict[str, str]],
    fmp_rows: list[dict[str, str]],
    *,
    retention_confirmed: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    alpha = _latest_by_symbol(alpha_rows, provider="alpha_vantage", capability="earnings_estimates")
    fmp = _latest_by_symbol(fmp_rows, provider="fmp", capability="analyst_estimates")
    symbols = sorted(set(alpha) | set(fmp))
    if not symbols:
        raise ValueError("No comparable Alpha Vantage/FMP estimate rows were found")

    comparison: list[dict[str, Any]] = []
    for symbol in symbols:
        alpha_row = alpha.get(symbol)
        fmp_row = fmp.get(symbol)
        alpha_fields = _field_set(alpha_row)
        fmp_fields = _field_set(fmp_row)
        alpha_nonempty = bool(alpha_row and alpha_row.get("status") == "AVAILABLE" and _row_count(alpha_row) > 0)
        fmp_nonempty = bool(fmp_row and fmp_row.get("status") == "AVAILABLE" and _row_count(fmp_row) > 0)
        comparison.append(
            {
                "symbol": symbol,
                "alpha_status": alpha_row.get("status", "MISSING") if alpha_row else "MISSING",
                "alpha_rows": _row_count(alpha_row),
                "alpha_fields": ",".join(sorted(alpha_fields)),
                "fmp_status": fmp_row.get("status", "MISSING") if fmp_row else "MISSING",
                "fmp_rows": _row_count(fmp_row),
                "fmp_fields": ",".join(sorted(fmp_fields)),
                "alpha_nonempty": int(alpha_nonempty),
                "fmp_nonempty": int(fmp_nonempty),
                "alpha_unique_revision_fields": ",".join(sorted((alpha_fields - fmp_fields) & REVISION_FIELDS)),
            }
        )

    total = len(comparison)
    alpha_nonempty_count = sum(int(row["alpha_nonempty"]) for row in comparison)
    fmp_nonempty_count = sum(int(row["fmp_nonempty"]) for row in comparison)
    unresolved_alpha = sum(row["alpha_status"] not in {"AVAILABLE", "EMPTY"} for row in comparison)
    unique_revision_fields = sorted(
        {field for row in comparison for field in str(row["alpha_unique_revision_fields"]).split(",") if field}
    )
    alpha_coverage = alpha_nonempty_count / total
    fmp_coverage = fmp_nonempty_count / total
    checks = [
        {
            "check": "alpha_nonempty_coverage",
            "status": "PASS" if alpha_coverage >= 0.90 else "FAIL",
            "detail": f"{alpha_nonempty_count}/{total}={alpha_coverage:.1%}; floor=90.0%",
        },
        {
            "check": "fmp_nonempty_coverage",
            "status": "PASS" if fmp_coverage >= 0.90 else "FAIL",
            "detail": f"{fmp_nonempty_count}/{total}={fmp_coverage:.1%}; floor=90.0%",
        },
        {
            "check": "alpha_no_unresolved_provider_errors",
            "status": "PASS" if unresolved_alpha == 0 else "FAIL",
            "detail": f"unresolved={unresolved_alpha}",
        },
        {
            "check": "alpha_adds_revision_fields",
            "status": "PASS" if unique_revision_fields else "FAIL",
            "detail": ",".join(unique_revision_fields) or "none",
        },
        {
            "check": "retention_rights_confirmed",
            "status": "PASS" if retention_confirmed else "FAIL",
            "detail": "confirmed" if retention_confirmed else "unconfirmed_do_not_retain",
        },
    ]
    coverage_pass = all(
        check["status"] == "PASS"
        for check in checks
        if check["check"]
        in {
            "alpha_nonempty_coverage",
            "alpha_no_unresolved_provider_errors",
        }
    )
    economic_value = bool(unique_revision_fields)
    if not coverage_pass or not economic_value:
        decision = "diagnostics_only"
    elif not retention_confirmed:
        decision = "qualified_pending_rights"
    else:
        decision = "approved_secondary_estimates_source"
    summary = {
        "schema_version": "estimate_provider_pilot_decision_v2",
        "acceptance": "PASS",
        "provider_role_decision": decision,
        "symbols": symbols,
        "symbol_count": total,
        "alpha_nonempty_count": alpha_nonempty_count,
        "alpha_nonempty_coverage": alpha_coverage,
        "fmp_nonempty_count": fmp_nonempty_count,
        "fmp_nonempty_coverage": fmp_coverage,
        "alpha_unique_revision_fields": unique_revision_fields,
        "retention_rights_confirmed": retention_confirmed,
        "checks": checks,
    }
    return comparison, summary


def _report(summary: dict[str, Any]) -> str:
    lines = [
        "# Estimate Provider Pilot Decision",
        "",
        f"- Acceptance: `{summary['acceptance']}`",
        f"- Provider role decision: `{summary['provider_role_decision']}`",
        f"- Symbols: `{summary['symbol_count']}`",
        f"- Alpha Vantage non-empty coverage: `{summary['alpha_nonempty_coverage']:.1%}`",
        f"- FMP non-empty coverage: `{summary['fmp_nonempty_coverage']:.1%}`",
        "- Raw provider payloads retained: `NO`",
        "",
        "| Gate | Status | Detail |",
        "|---|---|---|",
    ]
    for check in summary["checks"]:
        lines.append(f"| {check['check']} | {check['status']} | {check['detail']} |")
    lines.extend(
        [
            "",
            "The provider-role gate is fail-closed. A paid subscription and added fields do not",
            "compensate for insufficient coverage or unconfirmed retention rights.",
            "",
        ]
    )
    return "\n".join(lines)


def run_selftest() -> None:
    alpha = [
        {
            "provider": "alpha_vantage",
            "capability": "earnings_estimates",
            "symbol": symbol,
            "requested_at_utc": "2026-07-31T00:00:00+00:00",
            "status": "AVAILABLE",
            "row_count": "4",
            "field_names": "eps_estimate_average,eps_estimate_average_30_days_ago",
        }
        for symbol in ("AAA", "BBB")
    ]
    fmp = [
        {
            "provider": "fmp",
            "capability": "analyst_estimates",
            "symbol": symbol,
            "requested_at_utc": "2026-07-31T00:00:00+00:00",
            "status": "AVAILABLE",
            "row_count": "4",
            "field_names": "epsAvg,revenueAvg",
        }
        for symbol in ("AAA", "BBB")
    ]
    _, blocked = evaluate(alpha, fmp, retention_confirmed=False)
    assert blocked["provider_role_decision"] == "qualified_pending_rights"
    _, approved = evaluate(alpha, fmp, retention_confirmed=True)
    assert approved["provider_role_decision"] == "approved_secondary_estimates_source"


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    paths = resolve_runtime_paths(load_yaml(config_path), config_path)
    if args.selftest:
        run_selftest()
        print("estimate provider comparison selftest: PASS")
        return 0

    if not args.alpha_results or args.fmp_results is None or args.as_of is None:
        raise ValueError("--alpha-results, --fmp-results, and --as-of are required")

    entitlements_path = args.entitlements.resolve()
    entitlements = load_entitlements(entitlements_path)
    alpha_retention = entitlements["providers"]["alpha_vantage"].get("retention", {})
    retention_confirmed = str(alpha_retention.get("status", "")).casefold() == "confirmed"
    alpha_paths = [path.resolve() for path in args.alpha_results]
    fmp_path = args.fmp_results.resolve()
    comparison, summary = evaluate(
        _read_rows(alpha_paths),
        _read_rows([fmp_path]),
        retention_confirmed=retention_confirmed,
    )
    summary["as_of_date"] = args.as_of.isoformat()
    summary["generated_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / "provider_capabilities" / f"{args.as_of}-estimate-comparison"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "estimate_provider_comparison.csv"
    decision_path = output_dir / "estimate_provider_decision.json"
    report_path = output_dir / "estimate_provider_decision.md"
    manifest_path = output_dir / "estimate_provider_decision_manifest.json"
    write_csv(csv_path, OUTPUT_FIELDS, comparison)
    write_manifest(decision_path, summary)
    write_text_atomic(report_path, _report(summary))

    input_paths = [entitlements_path, Path(__file__).resolve(), *alpha_paths, fmp_path]
    write_manifest(
        manifest_path,
        {
            "schema_version": "estimate_provider_decision_manifest_v2",
            "acceptance": "PASS",
            "provider_role_decision": summary["provider_role_decision"],
            "as_of_date": args.as_of.isoformat(),
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {
                csv_path.name: sha256_file(csv_path),
                decision_path.name: sha256_file(decision_path),
                report_path.name: sha256_file(report_path),
            },
        },
    )
    print(f"ESTIMATE PROVIDER PILOT: {summary['provider_role_decision']}")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
