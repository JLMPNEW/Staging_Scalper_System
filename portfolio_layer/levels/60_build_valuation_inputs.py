#!/usr/bin/env python3
"""Build fail-closed sector-owned valuation input contracts for the levels engine."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import connect_monitor_db, fetch_universe_snapshot  # noqa: E402
from portfolio_layer.levels.levels_common import (  # noqa: E402
    SUPPORTED_VALUATION_METHODS,
    VALUATION_CONTRACT_VERSION,
    VALUATION_FIELDS,
    build_valuation_contract_row,
    valuation_lineage_errors,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
VALIDATION_FIELDS = ["check", "status", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _raw_artifact_name(source_pipeline: str) -> str:
    if not source_pipeline or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in source_pipeline):
        raise ValueError(f"Unsafe source pipeline name: {source_pipeline!r}")
    return f"{source_pipeline}_scores.csv"


def _methods(row: dict[str, Any]) -> list[str]:
    parsed = json.loads(str(row.get("method_allowlist", "[]")))
    return [str(value) for value in parsed] if isinstance(parsed, list) else []


def _validate(rows: list[dict[str, Any]], expected: int) -> list[dict[str, str]]:
    tickers = [str(row["ticker"]) for row in rows]
    valid_count = sum(row["contract_status"] == "valid" for row in rows)
    lineage_errors: dict[str, list[str]] = {}
    for row in rows:
        errors = valuation_lineage_errors(row)
        if errors:
            lineage_errors[str(row["ticker"])] = errors
    return [
        {"check": "universe_complete_unique", "status": "PASS" if len(rows) == expected and len(tickers) == len(set(tickers)) else "FAIL", "detail": f"rows={len(rows)}; expected={expected}"},
        {"check": "contract_version", "status": "PASS" if all(row["valuation_contract_version"] == VALUATION_CONTRACT_VERSION for row in rows) else "FAIL", "detail": VALUATION_CONTRACT_VERSION},
        {"check": "valid_requires_method", "status": "PASS" if all(row["contract_status"] != "valid" or row["method_allowlist"] != "[]" for row in rows) else "FAIL", "detail": "valid rows have applicable absolute-value methods"},
        {
            "check": "methods_closed_contract",
            "status": "PASS" if all(set(_methods(row)) <= SUPPORTED_VALUATION_METHODS for row in rows) else "FAIL",
            "detail": "all valuation methods are explicitly supported",
        },
        {
            "check": "ttm_fcf_numerator_valid",
            "status": "PASS" if all(
                "fcf_yield_ttm" not in _methods(row)
                or float(row["fcf_per_share_ttm"]) > 0
                for row in rows
            ) else "FAIL",
            "detail": "trailing-FCF anchors require explicit FCF/share or a governed same-row PIT yield-price reconstruction",
        },
        {
            "check": "sector_specialist_range_complete",
            "status": "PASS" if all(
                "sector_specialist" not in _methods(row)
                or (
                    0 < float(row["sector_valuation_low"])
                    <= float(row["sector_valuation_base"])
                    <= float(row["sector_valuation_high"])
                    and 0 <= float(row["sector_valuation_confidence"]) <= 1
                    and bool(str(row["sector_valuation_method"]).strip())
                    and bool(str(row["sector_valuation_available_at_utc"]).strip())
                )
                for row in rows
            ) else "FAIL",
            "detail": "specialist anchors require an ordered range, confidence, method, and PIT timestamp",
        },
        {
            "check": "direct_market_price_excluded",
            "status": "PASS" if not lineage_errors else "FAIL",
            "detail": (
                "every valuation method has explicit non-market-price lineage"
                if not lineage_errors
                else json.dumps(lineage_errors, sort_keys=True)
            ),
        },
        {
            "check": "absolute_valuation_coverage",
            "status": "PASS" if valid_count > 0 else "DEFERRED",
            "detail": f"valid_contracts={valid_count}; supported PIT absolute valuation inputs are required",
        },
    ]


def run_selftest() -> None:
    assert _raw_artifact_name("technology_hardware") == "technology_hardware_scores.csv"
    try:
        _raw_artifact_name("../unsafe")
    except ValueError:
        pass
    else:
        raise AssertionError("Unsafe pipeline name was accepted")
    contract = build_valuation_contract_row(
        as_of="2026-07-31",
        ticker="TEST",
        source_pipeline="semiconductors",
        raw={"asof_date": "2026-07-30", "eps_forward": 2.0, "currency": "USD"},
        source_path=Path("unused_when_source_date_is_present.csv"),
        source_sha="a" * 64,
    )
    assert contract["contract_status"] == "valid"
    assert contract["available_at_utc"] == "2026-07-30"
    ttm = build_valuation_contract_row(
        as_of="2026-07-31",
        ticker="TTM",
        source_pipeline="semiconductors",
        raw={
            "asof_date": "2026-07-31",
            "latest_price": 100.0,
            "fcf_yield": 0.05,
        },
        source_path=Path("unused_when_source_date_is_present.csv"),
        source_sha="b" * 64,
        valuation_policy={
            "allow_ttm_fcf_per_share_reconstruction": True,
            "ttm_fcf_reconstruction_pipelines": ["semiconductors"],
            "maximum_source_fcf_yield": 1.0,
        },
    )
    assert ttm["contract_status"] == "valid"
    assert ttm["fcf_per_share_ttm"] == 5.0
    assert not valuation_lineage_errors(ttm)
    explicit_ttm = build_valuation_contract_row(
        as_of="2026-07-31",
        ticker="EXPLICIT",
        source_pipeline="semiconductors",
        raw={
            "asof_date": "2026-07-31",
            "latest_price": 100.0,
            "fcf_yield": 0.05,
            "fcf_per_share_ttm": 5.0,
        },
        source_path=Path("unused_when_source_date_is_present.csv"),
        source_sha="c" * 64,
    )
    assert explicit_ttm["contract_status"] == "valid"
    assert not valuation_lineage_errors(explicit_ttm)
    invalid_lineage = {
        **explicit_ttm,
        "valuation_input_lineage_json": json.dumps(
            {"fcf_yield_ttm": ["latest_price"]}
        ),
    }
    assert valuation_lineage_errors(invalid_lineage) == [
        "direct_market_price_input:fcf_yield_ttm",
        "ttm_fcf_missing_valid_numerator_source",
    ]
    assert ttm["contract_status"] == "valid"
    assert _methods(ttm) == ["fcf_yield_ttm"]
    assert not valuation_lineage_errors(ttm)
    print("valuation input contract selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    as_of = args.as_of.isoformat()
    universe_as_of = (args.universe_as_of or args.as_of).isoformat()
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    score_cfg = cfg_get(config, "score_contract", {})
    levels_cfg = cfg_get(config, "levels", {})
    if not all(
        isinstance(value, dict)
        for value in (monitor_cfg, score_cfg, levels_cfg)
    ):
        raise ValueError("monitor, score contract, and levels config must be mappings")
    valuation_policy_raw = levels_cfg.get("valuation_contract", {})
    if not isinstance(valuation_policy_raw, dict):
        raise ValueError("levels.valuation_contract must be a mapping")
    valuation_policy = dict(valuation_policy_raw)
    db_path = resolve_path(monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"), base_dir=config_path.parent)
    conn = connect_monitor_db(db_path, timeout_sec=float(monitor_cfg.get("writer_lock_timeout_sec", 30.0)))
    try:
        universe = fetch_universe_snapshot(conn, universe_as_of)
    finally:
        conn.close()
    if not universe:
        raise ValueError(f"No monitor universe for {universe_as_of}")
    stage1_manifest_path = paths.output_dir / "runs" / universe_as_of / "manifest.json"
    stage1_scores_path = paths.output_dir / "runs" / universe_as_of / "stocks_scores.csv"
    stage1_manifest = read_manifest(stage1_manifest_path)
    stage1_errors = sealed_artifact_errors(
        stage1_manifest,
        stage1_scores_path,
        "stocks_scores.csv",
        run_as_of=universe_as_of,
        allow_deferred=True,
    )
    if stage1_errors:
        raise ValueError(f"Sealed Stage 1 scores are required: {stage1_errors}")
    stage1_raw = stage1_manifest.get("raw", {})
    if not isinstance(stage1_raw, dict):
        raise ValueError("Stage 1 manifest raw-artifact map is missing")
    stage1_raw_dir = paths.output_dir / "runs" / universe_as_of / "raw"
    sectors = score_cfg.get("sectors", [])
    if not isinstance(sectors, list):
        raise ValueError("score_contract.sectors must be a list")
    sources: dict[str, tuple[Path, str, dict[str, dict[str, str]]]] = {}
    source_hashes: dict[str, str] = {}
    for sector in sectors:
        if not isinstance(sector, dict) or not bool(sector.get("enabled", False)):
            continue
        pipeline = str(sector.get("model_family", "")).strip()
        raw_name = _raw_artifact_name(pipeline)
        raw_entry = stage1_raw.get(raw_name)
        if not isinstance(raw_entry, dict):
            continue
        path = (stage1_raw_dir / raw_name).resolve()
        if not path.is_file():
            raise ValueError(f"Stage 1-sealed raw artifact is missing: {path}")
        file_sha = sha256_file(path)
        if file_sha != str(raw_entry.get("sha256", "")):
            raise ValueError(
                f"Stage 1 raw-artifact hash mismatch: {path}"
            )
        raw_rows = read_csv(path)
        by_ticker = {str(row.get("ticker", "")).strip().upper(): row for row in raw_rows if str(row.get("ticker", "")).strip()}
        sources[pipeline] = (path, file_sha, by_ticker)
        source_hashes[str(path)] = file_sha
    rows: list[dict[str, Any]] = []
    for member in sorted(universe, key=lambda row: str(row["ticker"])):
        ticker = str(member["ticker"])
        pipeline = str(member["source_pipeline"])
        source = sources.get(pipeline)
        if source is None or ticker not in source[2]:
            rows.append(
                {
                    **{field: None for field in VALUATION_FIELDS},
                    "as_of_date": as_of,
                    "available_at_utc": "",
                    "ticker": ticker,
                    "source_pipeline": pipeline,
                    "company_type": "unknown",
                    "currency": "",
                    "normalized_cyclical_flag": 0,
                    "method_allowlist": "[]",
                    "valuation_input_lineage_json": "{}",
                    "input_freshness_json": "{}",
                    "source_artifact_path": "",
                    "source_artifact_sha256": "",
                    "valuation_contract_version": VALUATION_CONTRACT_VERSION,
                    "contract_status": "invalid",
                    "contract_reason": "held_or_target_name_missing_sector_valuation_export" if not pipeline else "sector_source_or_ticker_missing",
                }
            )
            continue
        source_path, source_sha, raw_by_ticker = source
        rows.append(
            build_valuation_contract_row(
                as_of=as_of,
                ticker=ticker,
                source_pipeline=pipeline,
                raw=raw_by_ticker[ticker],
                source_path=source_path,
                source_sha=source_sha,
                valuation_policy=valuation_policy,
            )
        )
    checks = _validate(rows, len(universe))
    failures = [row for row in checks if row["status"] == "FAIL"]
    output_dir = args.output_dir or paths.output_dir / "runs" / as_of / "levels"
    input_path = output_dir / "valuation_inputs.csv"
    checks_path = output_dir / "valuation_input_validation.csv"
    manifest_path = output_dir / "valuation_inputs_manifest.json"
    fail_if_exists([input_path, checks_path, manifest_path], force=args.force)
    write_csv(input_path, VALUATION_FIELDS, rows)
    write_csv(checks_path, VALIDATION_FIELDS, checks)
    valid_count = sum(row["contract_status"] == "valid" for row in rows)
    method_counts = Counter(
        method
        for row in rows
        if row["contract_status"] == "valid"
        for method in _methods(row)
    )
    pipeline_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        pipeline = str(row["source_pipeline"])
        counts = pipeline_counts.setdefault(pipeline, {"rows": 0, "valid": 0})
        counts["rows"] += 1
        counts["valid"] += int(row["contract_status"] == "valid")
    acceptance = "FAIL" if failures else "PASS" if valid_count > 0 else "PASS_WITH_DEFERRED"
    input_files = [config_path, Path(__file__).resolve(), Path(__file__).with_name("levels_common.py"), stage1_manifest_path, stage1_scores_path]
    write_manifest(
        manifest_path,
        {
            "schema_version": "valuation_inputs_manifest_v3",
            "acceptance": acceptance,
            "as_of_date": as_of,
            "universe_as_of": universe_as_of,
            "row_count": len(rows),
            "valid_contract_count": valid_count,
            "valid_contracts_by_method": dict(sorted(method_counts.items())),
            "contract_coverage_by_pipeline": dict(sorted(pipeline_counts.items())),
            "deferred_reason": "" if valid_count else "sector_absolute_valuation_inputs_unavailable",
            "source_artifacts_sha256": source_hashes,
            "inputs_sha256": {str(path): sha256_file(path) for path in input_files},
            "outputs_sha256": {input_path.name: sha256_file(input_path), checks_path.name: sha256_file(checks_path)},
        },
    )
    print(f"VALUATION INPUT CONTRACTS: {acceptance}")
    print(f"rows={len(rows)}; valid={valid_count}; manifest={manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
