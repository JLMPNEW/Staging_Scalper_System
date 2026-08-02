#!/usr/bin/env python3
"""Independently validate and seal advisory execution levels."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, read_manifest, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.market_data_common import (  # noqa: E402
    SELECTED_OHLCV_FILENAME,
    read_gzip_csv,
)
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    monitor_output_subdir,
)
from portfolio_layer.levels.levels_common import (  # noqa: E402
    LEVEL_FIELDS,
    LEVELS_MODEL_VERSION,
    VALUATION_FIELDS,
    band_geometry,
    financial_risk_penalty,
    market_structure,
    optional_float,
    uncertainty_penalty,
    valuation_lineage_errors,
    valuation_range,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
VALIDATION_FIELDS = ["check", "status", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--market-data-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _number(row: dict[str, str], field: str) -> float | None:
    text = str(row.get(field, "")).strip()
    return None if not text else float(text)


def _missing_columns(
    rows: list[dict[str, str]], required: list[str]
) -> list[str]:
    available = set(rows[0]) if rows else set()
    return sorted(set(required) - available)


def _market_data_checks(rows: list[dict[str, str]]) -> tuple[bool, bool]:
    status_ok = True
    isolation_ok = True
    for row in rows:
        try:
            freshness = json.loads(row["data_freshness_json"])
        except (KeyError, TypeError, json.JSONDecodeError):
            status_ok = False
            isolation_ok = False
            continue
        status = freshness.get("market_data_status")
        status_ok &= status in {"current", "missing_latest"}
        if status == "missing_latest":
            isolation_ok &= (
                row["level_status"] != "active"
                and row["recommended_state"]
                in {"watch", "suspend_adds", "deteriorating", "exit_review"}
                and all(
                    _number(row, field) is None
                    for field in (
                        "starter_band_low", "starter_band_high",
                        "add_band_low", "add_band_high",
                    )
                )
            )
        elif status == "current":
            status_ok &= (
                freshness.get("market_date")
                == freshness.get("required_market_date")
            )
    return status_ok, isolation_ok


def validate_rows(
    rows: list[dict[str, str]],
    valuations: dict[str, dict[str, str]],
    multiples: dict[str, dict[str, float]],
    *,
    levels_cfg: dict[str, Any],
    as_of: date,
    ohlcv: pd.DataFrame,
    required_market_date: str,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def rec(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    tickers = [row["ticker"] for row in rows]
    complete = (
        bool(rows)
        and len(tickers) == len(set(tickers)) == len(valuations)
        and set(tickers) == set(valuations)
    )
    rec("complete_unique", complete, f"levels={len(rows)}; valuations={len(valuations)}")
    rec(
        "active_requires_all_gates",
        all(
            row["level_status"] != "active"
            or (
                int(float(row["investable_eligible"])) == 1
                and row["valuation_status"] == "valid"
                and row["internal_expectations_state"] in {"green", "stable"}
                and row["event_state"] == "clear"
            )
            for row in rows
        ),
        "active bands require investability, valuation, supportive thesis, clear event window",
    )
    market_status_ok, market_isolation_ok = _market_data_checks(rows)
    rec(
        "market_data_status_current_recomputed",
        market_status_ok,
        "current rows match the sealed required market date",
    )
    rec(
        "missing_market_data_inactive_per_name",
        market_isolation_ok,
        "missing names retain rows but cannot activate entry/add bands",
    )
    ceiling_ok = True
    order_ok = True
    trim_ok = True
    basis_ok = True
    for row in rows:
        ceiling = _number(row, "long_entry_ceiling")
        starter_low = _number(row, "starter_band_low")
        starter_high = _number(row, "starter_band_high")
        add_low = _number(row, "add_band_low")
        add_high = _number(row, "add_band_high")
        trim_low = _number(row, "trim_band_low")
        trim_high = _number(row, "trim_band_high")
        if ceiling is not None:
            ceiling_ok &= all(value is None or value <= ceiling + 1e-9 for value in (starter_low, starter_high, add_low, add_high))
        order_ok &= (
            (starter_low is None and starter_high is None or starter_low is not None and starter_high is not None and 0 <= starter_low <= starter_high)
            and (add_low is None and add_high is None or add_low is not None and add_high is not None and 0 <= add_low <= add_high)
        )
        trim_ok &= (
            trim_low is None
            and trim_high is None
            or trim_low is not None
            and trim_high is not None
            and 0 <= trim_low <= trim_high
            and (
                row.get("band_basis") != "fundamental_nominal_raw_price"
                or _number(row, "valuation_base") is not None
                and trim_low >= float(_number(row, "valuation_base") or 0.0)
            )
        )
        basis_ok &= (
            row.get("price_basis") == "raw_unadjusted_nominal"
            and row.get("band_basis")
            in {
                "",
                "fundamental_nominal_raw_price",
                "market_reference_only_raw_price",
            }
            and (
                row["valuation_status"] != "valid"
                or row.get("valuation_currency") == "USD"
            )
        )
    rec("execution_never_exceeds_valuation_ceiling", ceiling_ok, "entry/add bounds <= ceiling")
    rec("band_order_nonnegative", order_ok, "all emitted bands are ordered and non-negative")
    rec(
        "trim_band_order_and_anchor",
        trim_ok,
        "trim bounds are ordered and intrinsic trims sit at/above valuation base",
    )
    rec(
        "price_currency_share_basis_aligned",
        basis_ok,
        "raw nominal prices and USD intrinsic values share one per-share basis",
    )
    rec(
        "inactive_levels_retained",
        all(row["level_status"] == "active" or row["inactive_reason"] for row in rows),
        "every inactive row carries a reason",
    )
    rec(
        "invalid_contract_has_no_intrinsic_range",
        all(
            row["valuation_status"] == "valid"
            or (
                all(
                    _number(row, field) is None
                    for field in (
                        "valuation_low", "valuation_base", "valuation_high"
                    )
                )
                and json.loads(row["valuation_methods_json"]) == {}
            )
            for row in rows
        ),
        "invalid contracts cannot publish diagnostic intrinsic values",
    )
    recompute_ok = complete
    margins = dict(levels_cfg.get("margin_of_safety", {}))
    geometry = dict(levels_cfg.get("band_geometry", {}))
    minimum_adv = float(levels_cfg.get("minimum_adv_usd", 5_000_000.0))
    for row in rows:
        valuation = valuations.get(row["ticker"])
        if valuation is None:
            recompute_ok = False
            continue
        methods, low, base, high, disagreement, confidence = valuation_range(
            valuation, multiples
        )
        currency_aligned = str(valuation.get("currency", "")).upper() == str(
            levels_cfg.get("price_currency", "USD")
        ).upper()
        if not currency_aligned:
            methods = {}
            low = base = high = None
            disagreement = confidence = 0.0
        expected = (low, base, high, disagreement, confidence)
        actual = (
            _number(row, "valuation_low"), _number(row, "valuation_base"),
            _number(row, "valuation_high"), float(row["anchor_disagreement"]),
            float(row["valuation_confidence"]),
        )
        recompute_ok &= all(
            left is None and right is None
            or left is not None and right is not None and math.isclose(left, right, rel_tol=0, abs_tol=1e-8)
            for left, right in zip(expected, actual, strict=True)
        )
        frame = ohlcv.loc[ohlcv["ticker"] == row["ticker"]].set_index("date")
        expected_market = (
            market_structure(frame)
            if not frame.empty
            else {
                "last_market_date": "",
                "latest_price": None,
                "volume_weighted_daily_price_63": None,
                "avg_dollar_volume_60d": None,
                "ma50": None,
                "ma200": None,
                "atr20": None,
                "atr60": None,
                "sigma20": None,
                "volatility_unit": 0.0,
                "market_reference": None,
                "return_5d": None,
            }
        )
        market = json.loads(row["market_structure_json"])
        for field, expected_value in expected_market.items():
            actual_value = market.get(field)
            if isinstance(expected_value, float):
                recompute_ok &= (
                    optional_float(actual_value) is not None
                    and math.isclose(
                        expected_value,
                        float(optional_float(actual_value) or 0.0),
                        rel_tol=0,
                        abs_tol=1e-8,
                    )
                )
            else:
                recompute_ok &= actual_value == expected_value
        freshness = json.loads(row["data_freshness_json"])
        recompute_ok &= (
            str(freshness.get("market_date", ""))
            == str(expected_market.get("last_market_date", ""))
            and str(freshness.get("required_market_date", ""))
            == required_market_date
        )
        base_map_raw = margins.get("base_by_company_type", {})
        base_map = dict(base_map_raw) if isinstance(base_map_raw, dict) else {}
        expected_base_margin = float(
            base_map.get(
                str(valuation.get("company_type", "unknown")),
                margins.get("default_base", 0.15),
            )
        )
        actual_base_margin = _number(row, "base_margin")
        recompute_ok &= (
            actual_base_margin is not None
            and math.isclose(
                expected_base_margin,
                actual_base_margin,
                rel_tol=0,
                abs_tol=1e-8,
            )
        )
        expected_uncertainty = uncertainty_penalty(
            valuation,
            anchor_count=len(methods),
            disagreement=disagreement,
            as_of=as_of,
            margins=margins,
        )
        expected_financial = financial_risk_penalty(valuation, margins)
        adv = optional_float(expected_market.get("avg_dollar_volume_60d"))
        expected_liquidity = (
            0.0
            if adv is not None and adv >= minimum_adv
            else float(margins.get("low_liquidity_penalty", 0.05))
        )
        expected_event = (
            float(margins.get("event_gap_penalty", 0.05))
            if row["event_state"] == "blocked"
            else 0.0
        )
        regime_additions = dict(margins.get("regime_additions", {}))
        expected_total = min(
            float(margins.get("maximum_total_margin", 0.50)),
            expected_base_margin
            + expected_uncertainty
            + expected_financial
            + expected_liquidity
            + expected_event
            + float(
                regime_additions.get(
                    str(freshness.get("active_macro_regime", "")), 0.0
                )
            ),
        )
        for expected_value, field in (
            (expected_uncertainty, "uncertainty_penalty"),
            (expected_financial, "financial_risk_penalty"),
            (expected_liquidity, "liquidity_penalty"),
            (expected_event, "event_gap_penalty"),
            (expected_total, "margin_of_safety"),
        ):
            actual_value = _number(row, field)
            recompute_ok &= (
                actual_value is not None
                and math.isclose(
                    expected_value, actual_value, rel_tol=0, abs_tol=1e-8
                )
            )
        expected_ceiling = (
            base * (1.0 - expected_total)
            if base is not None and row["valuation_status"] == "valid"
            else None
        )
        actual_ceiling = _number(row, "long_entry_ceiling")
        recompute_ok &= (
            expected_ceiling is None
            and actual_ceiling is None
            or expected_ceiling is not None
            and actual_ceiling is not None
            and math.isclose(
                expected_ceiling, actual_ceiling, rel_tol=0, abs_tol=1e-8
            )
        )
        latest = optional_float(expected_market.get("latest_price"))
        market_reference = optional_float(expected_market.get("market_reference"))
        center = expected_ceiling if expected_ceiling is not None else market_reference
        if latest is not None and center is not None and row.get("band_basis"):
            intrinsic_anchor = high if high is not None else base
            if expected_ceiling is not None:
                if intrinsic_anchor is None:
                    recompute_ok = False
                    continue
                trim_anchor = intrinsic_anchor
            else:
                trim_anchor = center
            expected_bands = band_geometry(
                center=center,
                trim_anchor=trim_anchor,
                latest_price=latest,
                market=expected_market,
                geometry=geometry,
                intrinsic=expected_ceiling is not None,
                expectations_state=row["internal_expectations_state"],
            )
            for field in (
                "starter_band_low",
                "starter_band_high",
                "add_band_low",
                "add_band_high",
                "trim_band_low",
                "trim_band_high",
            ):
                actual_value = _number(row, field)
                recompute_ok &= (
                    actual_value is not None
                    and math.isclose(
                        expected_bands[field],
                        actual_value,
                        rel_tol=0,
                        abs_tol=1e-8,
                    )
                )
    rec(
        "deterministic_full_recompute",
        recompute_ok,
        "valuation, margins, ceiling, and every band reproduce from sealed inputs",
    )
    rec(
        "no_direct_market_price_anchor",
        not any(valuation_lineage_errors(row) for row in valuations.values()),
        "every intrinsic method has explicit non-market-price input lineage",
    )
    rec(
        "recommendation_closed_contract",
        all(row["recommended_state"] in {"buy_candidate", "add_candidate", "hold", "watch", "deteriorating", "suspend_adds", "exit_review"} for row in rows),
        "recommendations use the approved seven states",
    )
    rec(
        "valuation_pit_available",
        all(
            valuation["contract_status"] != "valid"
            or (
                str(valuation["as_of_date"]) == as_of.isoformat()
                and str(valuation["available_at_utc"])[:10] <= as_of.isoformat()
            )
            for valuation in valuations.values()
        ),
        "valid valuation inputs were available no later than their as-of date",
    )
    return checks


def run_selftest() -> None:
    assert _number({"x": ""}, "x") is None
    assert _number({"x": "1.5"}, "x") == 1.5
    missing = {
        "ticker": "AAA",
        "level_status": "inactive",
        "recommended_state": "watch",
        "starter_band_low": "",
        "starter_band_high": "",
        "add_band_low": "",
        "add_band_high": "",
        "data_freshness_json": json.dumps(
            {
                "market_data_status": "missing_latest",
                "market_date": "2026-07-30",
                "required_market_date": "2026-07-31",
            }
        ),
    }
    assert _market_data_checks([missing]) == (True, True)
    missing["level_status"] = "active"
    assert _market_data_checks([missing]) == (True, False)
    print("levels validator selftest: PASS")


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
    levels_cfg = cfg_get(config, "levels", {})
    if not isinstance(levels_cfg, dict):
        raise ValueError("levels config must be a mapping")
    if str(levels_cfg.get("policy_version", "")) != LEVELS_MODEL_VERSION:
        raise ValueError("Levels policy/model version mismatch")
    if float(levels_cfg.get("lambda_expectations", 0.0)) != 0.0:
        raise ValueError("Uncalibrated levels.lambda_expectations must remain 0.0")
    if not bool(levels_cfg.get("require_explicit_valuation_currency", False)):
        raise ValueError("levels.require_explicit_valuation_currency must be true")
    input_dir = args.input_dir or paths.output_dir / "runs" / args.as_of.isoformat() / "levels"
    build_manifest_path = input_dir / "levels_build_manifest.json"
    valuation_manifest_path = input_dir / "valuation_inputs_manifest.json"
    levels_path = input_dir / "levels.csv"
    valuation_path = input_dir / "valuation_inputs.csv"
    components_path = input_dir / "levels_components.csv"
    meta_path = input_dir / "levels_meta.json"
    validation_dir = input_dir / "validation"
    checks_path = validation_dir / "levels_validation.csv"
    manifest_path = input_dir / "levels_manifest.json"
    fail_if_exists([checks_path, manifest_path], force=args.force)
    build_manifest = read_manifest(build_manifest_path)
    valuation_manifest = read_manifest(valuation_manifest_path)
    if (
        build_manifest.get("acceptance") not in {"PASS", "PASS_WITH_DEFERRED"}
        or valuation_manifest.get("acceptance") not in {"PASS", "PASS_WITH_DEFERRED"}
    ):
        raise ValueError("Accepted levels build and valuation input manifests are required")
    if build_manifest.get("as_of_date") != args.as_of.isoformat():
        raise ValueError("Levels build date mismatch")
    if valuation_manifest.get("as_of_date") != args.as_of.isoformat():
        raise ValueError("Valuation input manifest date mismatch")
    for manifest, artifacts in (
        (build_manifest, (levels_path, components_path, meta_path)),
        (valuation_manifest, (valuation_path,)),
    ):
        output_hashes = dict(manifest.get("outputs_sha256", {}))
        for path in artifacts:
            if not path.is_file():
                raise FileNotFoundError(path)
            if output_hashes.get(path.name) != sha256_file(path):
                raise ValueError(f"Artifact hash mismatch: {path}")
    for source, expected in dict(build_manifest.get("inputs_sha256", {})).items():
        path = Path(source)
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"Levels input drift: {path}")
    source_artifact_errors: list[str] = []
    for source, expected in dict(valuation_manifest.get("source_artifacts_sha256", {})).items():
        path = Path(source)
        if not path.is_file():
            source_artifact_errors.append(f"missing:{path}")
        elif sha256_file(path) != expected:
            source_artifact_errors.append(f"hash_mismatch:{path}")
    monitor_dir = (
        paths.output_dir
        / "runs"
        / args.as_of.isoformat()
        / monitor_output_subdir(config)
    )
    market_dir = args.market_data_dir or monitor_dir / "market_data"
    market_manifest_path = market_dir / "monitor_ohlcv_manifest.json"
    market_validation_path = market_dir / "monitor_ohlcv_validation_manifest.json"
    market_manifest = read_manifest(market_manifest_path)
    market_validation = read_manifest(market_validation_path)
    selected_path = market_dir / SELECTED_OHLCV_FILENAME
    if (
        market_manifest.get("acceptance") not in {"PASS", "PASS_WITH_WARNINGS"}
        or market_validation.get("acceptance")
        not in {"PASS", "PASS_WITH_WARNINGS"}
        or market_manifest.get("as_of_date") != args.as_of.isoformat()
        or market_validation.get("as_of_date") != args.as_of.isoformat()
        or market_validation.get("producer_manifest_sha256")
        != sha256_file(market_manifest_path)
        or dict(market_manifest.get("outputs_sha256", {})).get(
            selected_path.name
        )
        != sha256_file(selected_path)
    ):
        raise ValueError("Validated same-date monitor OHLCV is required")
    required_market_date = str(market_manifest.get("final_market_date", ""))
    if not required_market_date:
        raise ValueError("Monitor OHLCV manifest lacks final_market_date")
    ohlcv = pd.DataFrame(read_gzip_csv(selected_path))
    ohlcv["date"] = pd.to_datetime(ohlcv["date"], errors="raise")
    rows = read_csv(levels_path)
    valuation_rows = read_csv(valuation_path)
    level_missing = _missing_columns(rows, LEVEL_FIELDS)
    valuation_missing = _missing_columns(valuation_rows, VALUATION_FIELDS)
    checks: list[dict[str, str]] = [
        {
            "check": "levels_schema_complete",
            "status": "PASS" if not level_missing else "FAIL",
            "detail": (
                "all required levels columns present"
                if not level_missing
                else f"missing={level_missing}"
            ),
        },
        {
            "check": "valuation_schema_complete",
            "status": "PASS" if not valuation_missing else "FAIL",
            "detail": (
                "all required valuation columns present"
                if not valuation_missing
                else f"missing={valuation_missing}"
            ),
        },
    ]
    levels_meta = read_manifest(meta_path)
    expected_geometry = dict(levels_cfg.get("band_geometry", {}))
    meta_ok = (
        levels_meta.get("as_of_date") == args.as_of.isoformat()
        and levels_meta.get("model_version") == LEVELS_MODEL_VERSION
        and levels_meta.get("band_geometry") == expected_geometry
        and float(levels_meta.get("lambda_expectations", math.nan)) == 0.0
        and levels_meta.get("liquidity_metric")
        == "sealed_ohlcv_mean_dollar_volume_60d"
    )
    checks.append(
        {
            "check": "levels_meta_contract_recomputed",
            "status": "PASS" if meta_ok else "FAIL",
            "detail": "date, model, geometry, expectations lambda, and liquidity metric match config",
        }
    )
    valuations = (
        {row["ticker"]: row for row in valuation_rows}
        if not valuation_missing
        else {}
    )
    multiples = {
        str(key): {str(name): float(value) for name, value in dict(raw).items()}
        for key, raw in dict(levels_cfg.get("valuation_multiples", {})).items()
        if isinstance(raw, dict)
    }
    if not level_missing and not valuation_missing:
        try:
            checks.extend(
                validate_rows(
                    rows,
                    valuations,
                    multiples,
                    levels_cfg=levels_cfg,
                    as_of=args.as_of,
                    ohlcv=ohlcv,
                    required_market_date=required_market_date,
                )
            )
        except Exception as exc:  # noqa: BLE001 - validator must emit FAIL evidence.
            checks.append(
                {
                    "check": "validator_execution",
                    "status": "FAIL",
                    "detail": f"{type(exc).__name__}:{exc}",
                }
            )
    checks.append(
        {
            "check": "valuation_source_artifacts_intact",
            "status": "PASS" if not source_artifact_errors else "FAIL",
            "detail": "all Stage 1-sealed sector sources intact"
            if not source_artifact_errors
            else str(source_artifact_errors),
        }
    )
    prohibited: list[str] = []
    for path in Path(__file__).resolve().parent.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in ("place" + "Order", "cancel" + "Order", "reqGlobal" + "Cancel"):
            if token in source:
                prohibited.append(f"{path.name}:{token}")
    checks.append(
        {"check": "broker_execution_prohibited", "status": "PASS" if not prohibited else "FAIL", "detail": str(prohibited) if prohibited else "no order API methods in levels package"}
    )
    failures = [row for row in checks if row["status"] == "FAIL"]
    write_csv(checks_path, VALIDATION_FIELDS, checks)
    acceptance = (
        "FAIL"
        if failures
        else "PASS"
        if any(row.get("level_status") == "active" for row in rows)
        else "PASS_WITH_DEFERRED"
    )
    input_paths = [
        config_path,
        Path(__file__).resolve(),
        Path(__file__).with_name("levels_common.py"),
        build_manifest_path,
        valuation_manifest_path,
        components_path,
        meta_path,
        market_manifest_path,
        market_validation_path,
        selected_path,
    ]
    write_manifest(
        manifest_path,
        {
            "schema_version": "levels_manifest_v2",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "row_count": len(rows),
            "active_count": sum(
                row.get("level_status") == "active" for row in rows
            ),
            "shadow_only": True,
            "broker_execution_prohibited": True,
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {levels_path.name: sha256_file(levels_path), checks_path.name: sha256_file(checks_path)},
        },
    )
    print(f"LEVELS VALIDATION: {acceptance}")
    print(f"checks={len(checks)}; manifest={manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
