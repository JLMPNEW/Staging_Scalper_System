#!/usr/bin/env python3
"""Build long-only advisory valuation ranges and execution zones."""

from __future__ import annotations

import argparse
import json
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
from portfolio_layer.levels.levels_common import (  # noqa: E402
    LEVEL_FIELDS,
    LEVELS_MODEL_VERSION,
    VALUATION_CONTRACT_VERSION,
    band_geometry,
    financial_risk_penalty,
    digest,
    market_structure,
    optional_float,
    utc_now,
    uncertainty_penalty,
    valuation_range,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
COMPONENT_FIELDS = [
    "ticker", "valuation_methods_json", "market_structure_json", "margin_components_json",
    "activation_gates_json", "input_digest",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--levels-dir", type=Path)
    parser.add_argument("--monitor-dir", type=Path)
    parser.add_argument("--market-data-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _manifest_file(
    manifest_path: Path,
    filename: str,
    accepted: set[str],
    *,
    as_of: str,
) -> Path:
    manifest = read_manifest(manifest_path)
    if manifest.get("acceptance") not in accepted:
        raise ValueError(f"Upstream manifest did not pass: {manifest_path}")
    manifest_date = str(
        manifest.get("as_of_date", manifest.get("run_as_of", ""))
    )
    if not manifest_date or manifest_date != as_of:
        raise ValueError(
            f"Upstream manifest date mismatch: {manifest_path}; "
            f"actual={manifest_date or 'MISSING'} expected={as_of}"
        )
    path = manifest_path.parent / filename
    output_hashes = dict(manifest.get("outputs_sha256", {}))
    provenance_hashes = dict(manifest.get("provenance_sha256", {}))
    expected_hash = output_hashes.get(filename) or provenance_hashes.get(filename)
    if not expected_hash or expected_hash != sha256_file(path):
        raise ValueError(f"Upstream artifact hash mismatch: {path}")
    return path


def _recommended_state(
    expectation_action: str,
    *,
    is_holding: bool,
    active: bool,
    market_data_current: bool,
    latest_price: float | None,
    entry_ceiling: float | None,
) -> str:
    if expectation_action in {"exit_review", "deteriorating", "suspend_adds"}:
        return expectation_action
    if not market_data_current or latest_price is None:
        return "suspend_adds" if is_holding else "watch"
    if active and entry_ceiling is not None and latest_price <= entry_ceiling:
        return "add_candidate" if is_holding else "buy_candidate"
    return "hold" if is_holding else "watch"


def run_selftest() -> None:
    assert _recommended_state("hold", is_holding=False, active=True, market_data_current=True, latest_price=9, entry_ceiling=10) == "buy_candidate"
    assert _recommended_state("exit_review", is_holding=True, active=False, market_data_current=False, latest_price=None, entry_ceiling=None) == "exit_review"
    assert _recommended_state("hold", is_holding=True, active=False, market_data_current=False, latest_price=None, entry_ceiling=None) == "suspend_adds"
    print("levels build selftest: PASS")


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
    levels_cfg = cfg_get(config, "levels", {})
    if not isinstance(levels_cfg, dict):
        raise ValueError("levels config must be a mapping")
    if str(levels_cfg.get("policy_version", "")) != LEVELS_MODEL_VERSION:
        raise ValueError(
            "levels.policy_version must equal the frozen levels model version "
            f"{LEVELS_MODEL_VERSION}"
        )
    if bool(levels_cfg.get("enabled_in_production", True)):
        raise ValueError("Levels must remain shadow-only until separately promoted")
    if not bool(levels_cfg.get("broker_execution_prohibited", False)):
        raise ValueError("levels.broker_execution_prohibited must be true")
    if float(levels_cfg.get("lambda_expectations", 0.0)) != 0.0:
        raise ValueError(
            "levels.lambda_expectations must remain 0.0 until an expectations "
            "price adjustment is separately calibrated and promoted"
        )
    if not bool(levels_cfg.get("require_explicit_valuation_currency", False)):
        raise ValueError("levels.require_explicit_valuation_currency must be true")
    levels_dir = args.levels_dir or paths.output_dir / "runs" / as_of / "levels"
    output_subdir = str(
        cfg_get(config, "expectations_monitor.output_subdir", "expectations_monitor")
    ).strip()
    if not output_subdir or Path(output_subdir).is_absolute() or ".." in Path(output_subdir).parts:
        raise ValueError("expectations_monitor.output_subdir must be a safe relative path")
    monitor_dir = args.monitor_dir or paths.output_dir / "runs" / as_of / output_subdir
    market_dir = args.market_data_dir or monitor_dir / "market_data"
    valuation_manifest_path = levels_dir / "valuation_inputs_manifest.json"
    state_manifest_path = monitor_dir / "expectations_state_manifest.json"
    state_validation_path = monitor_dir / "validation" / "expectations_state_validation_manifest.json"
    market_manifest_path = market_dir / "monitor_ohlcv_manifest.json"
    market_validation_path = market_dir / "monitor_ohlcv_validation_manifest.json"
    valuation_path = _manifest_file(
        valuation_manifest_path,
        "valuation_inputs.csv",
        {"PASS", "PASS_WITH_DEFERRED"},
        as_of=as_of,
    )
    state_path = _manifest_file(
        state_manifest_path, "expectations_state.csv", {"PASS"}, as_of=as_of
    )
    state_validation = read_manifest(state_validation_path)
    if state_validation.get("acceptance") != "PASS" or state_validation.get("as_of_date") != as_of:
        raise ValueError("Validated same-date expectations state is required")
    selected_path = _manifest_file(
        market_manifest_path,
        SELECTED_OHLCV_FILENAME,
        {"PASS", "PASS_WITH_WARNINGS"},
        as_of=as_of,
    )
    market_manifest = read_manifest(market_manifest_path)
    required_market_date = str(market_manifest.get("final_market_date", ""))
    if not required_market_date:
        raise ValueError("Monitor OHLCV manifest lacks final_market_date")
    market_validation = read_manifest(market_validation_path)
    if (
        market_validation.get("acceptance") not in {"PASS", "PASS_WITH_WARNINGS"}
        or market_validation.get("as_of_date") != as_of
        or market_validation.get("producer_manifest_sha256")
        != sha256_file(market_manifest_path)
    ):
        raise ValueError("Validated monitor OHLCV is required")
    valuation_rows = {row["ticker"]: row for row in read_csv(valuation_path)}
    state_rows = {row["ticker"]: row for row in read_csv(state_path)}
    ohlcv = pd.DataFrame(read_gzip_csv(selected_path))
    ohlcv["date"] = pd.to_datetime(ohlcv["date"], errors="raise")
    earnings_path = paths.output_dir / "runs" / as_of / "earnings_dates" / "earnings_calendar.csv"
    earnings_manifest_path = paths.output_dir / "runs" / as_of / "earnings_dates" / "earnings_manifest.json"
    sealed_earnings = _manifest_file(
        earnings_manifest_path,
        earnings_path.name,
        {"PASS", "PASS_WITH_WARNINGS"},
        as_of=as_of,
    )
    earnings = {row["ticker"]: row for row in read_csv(sealed_earnings)}
    macro_path = _manifest_file(
        paths.output_dir / "runs" / as_of / "macro" / "macro_manifest.json",
        "macro_regime.csv",
        {"PASS"},
        as_of=as_of,
    )
    macro_rows = read_csv(macro_path)
    if len(macro_rows) != 1 or str(macro_rows[0].get("coverage_flag", "")) not in {"1", "1.0"}:
        raise ValueError("Covered same-date macro regime is required")
    active_regime = str(macro_rows[0].get("active_current_regime", "")).strip()
    multiples_raw = levels_cfg.get("valuation_multiples", {})
    if not isinstance(multiples_raw, dict):
        raise ValueError("levels.valuation_multiples must be a mapping")
    multiples = {
        str(key): {str(name): float(value) for name, value in dict(raw).items()}
        for key, raw in multiples_raw.items()
        if isinstance(raw, dict)
    }
    margins = dict(levels_cfg.get("margin_of_safety", {}))
    geometry_raw = levels_cfg.get("band_geometry", {})
    if not isinstance(geometry_raw, dict):
        raise ValueError("levels.band_geometry must be a mapping")
    geometry = dict(geometry_raw)
    minimum_adv = float(levels_cfg.get("minimum_adv_usd", 5_000_000.0))
    rows: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    available_at = utc_now()
    for ticker, state in sorted(state_rows.items()):
        valuation = valuation_rows.get(ticker)
        if valuation is None:
            raise ValueError(f"Missing valuation contract for {ticker}")
        frame = ohlcv.loc[ohlcv["ticker"] == ticker].set_index("date")
        market = (
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
        market_data_status = str(state.get("market_data_status", ""))
        if market_data_status not in {"current", "missing_latest"}:
            raise ValueError(
                f"Invalid market data status for {ticker}: {market_data_status}"
            )
        market_data_current = (
            market_data_status == "current"
            and str(market["last_market_date"]) == required_market_date
        )
        (
            method_values,
            low,
            base,
            high,
            disagreement,
            confidence,
        ) = valuation_range(valuation, multiples)
        valuation_currency = str(valuation.get("currency", "")).strip().upper()
        currency_aligned = (
            valuation_currency == str(levels_cfg.get("price_currency", "USD")).upper()
        )
        if not currency_aligned:
            method_values = {}
            low = base = high = None
            disagreement = confidence = 0.0
        valuation_valid = (
            valuation["contract_status"] == "valid"
            and base is not None
            and currency_aligned
        )
        company_type = str(valuation["company_type"])
        base_map = margins.get("base_by_company_type", {})
        base_margin = float(dict(base_map).get(company_type, margins.get("default_base", 0.15)))
        uncertainty = uncertainty_penalty(
            valuation,
            anchor_count=len(method_values),
            disagreement=disagreement,
            as_of=args.as_of,
            margins=margins,
        )
        financial_risk = financial_risk_penalty(valuation, margins)
        adv = optional_float(market.get("avg_dollar_volume_60d"))
        liquidity_penalty = 0.0 if adv is not None and adv >= minimum_adv else float(margins.get("low_liquidity_penalty", 0.05))
        earnings_row = earnings.get(ticker, {})
        earnings_covered = bool(earnings_row)
        days_until = optional_float(earnings_row.get("days_until"))
        earnings_block = not earnings_covered or days_until is None or 0 <= days_until <= int(levels_cfg.get("earnings_suspend_days", 5))
        catalyst_date = str(valuation.get("next_catalyst_date", "")).strip()
        catalyst_days: int | None = None
        if catalyst_date:
            catalyst_days = (date.fromisoformat(catalyst_date) - args.as_of).days
        catalyst_block = catalyst_days is not None and 0 <= catalyst_days <= int(
            levels_cfg.get("catalyst_suspend_days", 10)
        )
        event_gap = (
            float(margins.get("event_gap_penalty", 0.05))
            if earnings_block or catalyst_block
            else 0.0
        )
        regime_additions_raw = margins.get("regime_additions", {})
        regime_additions = (
            dict(regime_additions_raw) if isinstance(regime_additions_raw, dict) else {}
        )
        regime_addition = float(regime_additions.get(active_regime, 0.0))
        total_margin = min(
            float(margins.get("maximum_total_margin", 0.50)),
            base_margin
            + uncertainty
            + financial_risk
            + liquidity_penalty
            + event_gap
            + regime_addition,
        )
        entry_ceiling = None if not valuation_valid or base is None else base * (1.0 - total_margin)
        latest_market_price = optional_float(market["latest_price"])
        starter_low = starter_high = add_low = add_high = trim_low = trim_high = None
        band_basis = ""
        band_reference_status = "unavailable"
        if market_data_current and latest_market_price is not None:
            intrinsic = entry_ceiling is not None and base is not None
            market_reference = optional_float(market.get("market_reference"))
            center = entry_ceiling if intrinsic else market_reference
            if (
                center is not None
                and center > 0
                and (
                    intrinsic
                    or bool(
                        levels_cfg.get(
                            "emit_market_reference_bands_when_intrinsic_missing", True
                        )
                    )
                )
            ):
                intrinsic_anchor = high if high is not None else base
                if intrinsic:
                    if intrinsic_anchor is None:
                        raise ValueError(f"Intrinsic trim anchor missing for {ticker}")
                    trim_anchor = intrinsic_anchor
                else:
                    trim_anchor = center
                bands = band_geometry(
                    center=center,
                    trim_anchor=trim_anchor,
                    latest_price=latest_market_price,
                    market=market,
                    geometry=geometry,
                    intrinsic=intrinsic,
                    expectations_state=str(state["internal_state"]),
                )
                starter_low = bands["starter_band_low"]
                starter_high = bands["starter_band_high"]
                add_low = bands["add_band_low"]
                add_high = bands["add_band_high"]
                trim_low = bands["trim_band_low"]
                trim_high = bands["trim_band_high"]
                band_basis = (
                    "fundamental_nominal_raw_price"
                    if intrinsic
                    else "market_reference_only_raw_price"
                )
                band_reference_status = (
                    "intrinsic_candidate"
                    if intrinsic
                    else "diagnostic_only_missing_intrinsic"
                )
        internal_state = state["internal_state"]
        expectation_action = state["action_state"]
        stable_support = internal_state != "stable" or (
            market_data_current
            and latest_market_price is not None
            and (
                optional_float(market["ma50"]) is not None
                and latest_market_price >= float(optional_float(market["ma50"]) or 0.0)
                or (
                    optional_float(market["return_5d"]) is not None
                    and float(optional_float(market["return_5d"]) or 0.0) > 0
                )
            )
        )
        gates = {
            "valuation_valid": valuation_valid,
            "valuation_currency_aligned": currency_aligned,
            "investable": int(float(state["investable_eligible"])) == 1,
            "expectations_supportive": internal_state in {"green", "stable"},
            "stable_has_stabilization": stable_support,
            "earnings_covered_and_clear": not earnings_block,
            "catalyst_clear": not catalyst_block,
            "liquidity_pass": adv is not None and adv >= minimum_adv,
            "market_data_current": market_data_current,
        }
        active = all(gates.values()) and entry_ceiling is not None and starter_low is not None
        failed = [name for name, passed in gates.items() if not passed]
        inactive_reason = "" if active else ";".join(failed) or "no_executable_zone"
        level_status = "active" if active else (
            "inactive_no_valuation_anchor" if not valuation_valid else
            "inactive_thesis_suspended" if not gates["expectations_supportive"] else
            "inactive"
        )
        recommended = _recommended_state(
            expectation_action,
            is_holding=int(float(state["is_holding"])) == 1,
            active=active,
            market_data_current=market_data_current,
            latest_price=latest_market_price,
            entry_ceiling=entry_ceiling,
        )
        source_digest = digest(
            {
                "valuation": valuation,
                "state_digest": state["input_digest"],
                "market": market,
                "earnings": earnings_row,
            }
        )
        row = {
            "as_of_date": as_of, "available_at_utc": available_at, "ticker": ticker,
            "source_pipeline": state["source_pipeline"], "universe_tier": state["tier"],
            "is_holding": state["is_holding"], "is_target": state["is_target"],
            "investable_eligible": state["investable_eligible"],
            "valuation_status": "valid" if valuation_valid else "invalid",
            "valuation_low": low, "valuation_base": base, "valuation_high": high,
            "valuation_methods_json": json.dumps(method_values, sort_keys=True, separators=(",", ":")),
            "anchor_disagreement": disagreement, "valuation_confidence": confidence,
            "valuation_currency": valuation_currency,
            "price_basis": str(levels_cfg.get("price_basis", "")),
            "band_basis": band_basis,
            "band_reference_status": band_reference_status,
            "market_reference": market["market_reference"],
            "market_structure_json": json.dumps(market, sort_keys=True, separators=(",", ":")),
            "base_margin": base_margin, "uncertainty_penalty": uncertainty,
            "financial_risk_penalty": financial_risk, "liquidity_penalty": liquidity_penalty,
            "event_gap_penalty": event_gap, "margin_of_safety": total_margin,
            "long_entry_ceiling": entry_ceiling, "starter_band_low": starter_low,
            "starter_band_high": starter_high, "add_band_low": add_low, "add_band_high": add_high,
            "trim_band_low": trim_low, "trim_band_high": trim_high, "level_status": level_status,
            "inactive_reason": inactive_reason, "expectations_state": expectation_action,
            "internal_expectations_state": internal_state,
            "event_state": "blocked" if earnings_block or catalyst_block else "clear",
            "recommended_state": recommended,
            "data_freshness_json": json.dumps(
                {
                    "market_date": market["last_market_date"],
                    "required_market_date": required_market_date,
                    "market_data_status": market_data_status,
                    "valuation_available_at": valuation["available_at_utc"],
                    "earnings_covered": earnings_covered,
                    "active_macro_regime": active_regime,
                    "regime_margin_addition": regime_addition,
                    "next_catalyst_date": catalyst_date,
                    "next_catalyst_type": valuation.get("next_catalyst_type", ""),
                },
                sort_keys=True, separators=(",", ":"),
            ),
            "valuation_contract_version": VALUATION_CONTRACT_VERSION,
            "levels_model_version": LEVELS_MODEL_VERSION,
            "input_digest": source_digest,
        }
        rows.append(row)
        components.append(
            {
                "ticker": ticker,
                "valuation_methods_json": row["valuation_methods_json"],
                "market_structure_json": row["market_structure_json"],
                "margin_components_json": json.dumps(
                    {name: row[name] for name in ("base_margin", "uncertainty_penalty", "financial_risk_penalty", "liquidity_penalty", "event_gap_penalty")},
                    sort_keys=True, separators=(",", ":"),
                ),
                "activation_gates_json": json.dumps(gates, sort_keys=True, separators=(",", ":")),
                "input_digest": source_digest,
            }
        )
    levels_path = levels_dir / "levels.csv"
    components_path = levels_dir / "levels_components.csv"
    meta_path = levels_dir / "levels_meta.json"
    manifest_path = levels_dir / "levels_build_manifest.json"
    fail_if_exists(
        [levels_path, components_path, meta_path, manifest_path], force=args.force
    )
    write_csv(levels_path, LEVEL_FIELDS, rows)
    write_csv(components_path, COMPONENT_FIELDS, components)
    input_paths = [
        config_path, Path(__file__).resolve(), Path(__file__).with_name("levels_common.py"),
        valuation_manifest_path, state_manifest_path, state_validation_path,
        market_manifest_path, market_validation_path, earnings_manifest_path,
        macro_path, paths.output_dir / "runs" / as_of / "macro" / "macro_manifest.json",
    ]
    active_count = sum(row["level_status"] == "active" for row in rows)
    valid_count = sum(row["valuation_status"] == "valid" for row in rows)
    acceptance = "PASS" if active_count > 0 else "PASS_WITH_DEFERRED"
    write_manifest(
        meta_path,
        {
            "schema_version": "levels_meta_v3",
            "as_of_date": as_of,
            "model_version": LEVELS_MODEL_VERSION,
            "price_basis": str(levels_cfg.get("price_basis", "")),
            "band_geometry": geometry,
            "lambda_expectations": float(
                levels_cfg.get("lambda_expectations", 0.0)
            ),
            "liquidity_metric": "sealed_ohlcv_mean_dollar_volume_60d",
            "row_count": len(rows),
            "active_count": active_count,
            "valid_intrinsic_count": valid_count,
            "market_reference_only_count": sum(
                row["band_reference_status"]
                == "diagnostic_only_missing_intrinsic"
                for row in rows
            ),
            "shadow_only": True,
            "broker_execution_prohibited": True,
        },
    )
    write_manifest(
        manifest_path,
        {
            "schema_version": "levels_build_manifest_v2",
            "acceptance": acceptance,
            "as_of_date": as_of,
            "row_count": len(rows),
            "active_count": active_count,
            "valid_valuation_count": valid_count,
            "market_reference_band_count": sum(
                row["band_reference_status"] == "diagnostic_only_missing_intrinsic"
                for row in rows
            ),
            "earnings_dependency_status": "sealed_same_date",
            "active_macro_regime": active_regime,
            "deferred_reason": "no_active_validated_levels" if active_count == 0 else "",
            "shadow_only": True,
            "broker_execution_prohibited": True,
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {
                levels_path.name: sha256_file(levels_path),
                components_path.name: sha256_file(components_path),
                meta_path.name: sha256_file(meta_path),
            },
        },
    )
    print(f"LEVELS BUILD: {acceptance}")
    print(f"rows={len(rows)}; active={active_count}; manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
