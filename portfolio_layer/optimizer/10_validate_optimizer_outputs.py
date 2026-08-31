#!/usr/bin/env python3
"""Stage 3 - validate the AQR-only baseline book and seal a provenance-hashed manifest."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402

from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    manifest_acceptance_value,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.optimizer.optimizer_core import (  # noqa: E402
    constraint_aware_invested_gross,
    maximum_investable_gross,
)
from portfolio_layer.risk.liquidity import load_spread_snapshot  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("validate_optimizer_outputs")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate the Stage 3 baseline book.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def finite_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if np.isfinite(parsed) else None


def manifest_hash(manifest: dict, rel_path: str) -> str | None:
    return ((manifest.get("files") or {}).get(rel_path) or {}).get("sha256")


def stage1_config_binding(
    stage1_manifest: dict[str, Any], config_path: Path
) -> tuple[bool, str]:
    expected = str(
        (((stage1_manifest.get("provenance") or {}).get("config_yaml") or {}).get("sha256"))
        or ""
    ).strip()
    actual = sha256_file(config_path) if config_path.is_file() else ""
    valid = bool(expected) and actual == expected
    return valid, f"expected={expected or '<missing>'}; actual={actual or '<missing>'}"


def median_half_spread_bps(row: dict[str, str] | None) -> float | None:
    """Parse median_half_spread_bps from a spread_snapshot row; None when absent/blank/non-finite.

    Mirrors the optimizer (09) helper so this validator independently reproduces the liquidity floor.
    """
    if not row:
        return None
    raw = row.get("median_half_spread_bps")
    if raw in (None, ""):
        return None
    try:
        parsed = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if np.isfinite(parsed) else None


def evaluate_scope_weight_caps(
    rows: list[dict[str, str]],
    scores: dict[str, dict[str, str]],
    scope_caps: dict[str, dict[str, float]],
    *,
    gross: float,
    tolerance: float,
) -> tuple[list[str], list[str]]:
    """Independently reproduce per-pipeline/per-scope live-book caps.

    Membership comes from the sealed Stage 1 score contract, not mutable output labels. Once a
    pipeline declares scope caps, every optimizer row in that pipeline must map to exactly one
    configured scope; an unknown/blank scope is a fail-closed coverage error.
    """
    details: list[str] = []
    violations: list[str] = []
    for pipeline, raw_scopes in sorted(scope_caps.items()):
        scopes = {str(scope): float(cap) for scope, cap in raw_scopes.items()}
        realized = {scope: 0.0 for scope in scopes}
        unknown: list[str] = []
        for row in rows:
            ticker = str(row.get("ticker", "")).strip()
            contract = scores.get(ticker, row)
            if str(contract.get("source_pipeline", "")).strip() != pipeline:
                continue
            scope = str(contract.get("model_scope_id", "")).strip()
            if scope not in scopes:
                unknown.append(f"{ticker}:{scope or '<blank>'}")
                continue
            weight = finite_float(row.get("weight"))
            realized[scope] += weight if weight is not None else 0.0
        if unknown:
            violations.append(f"{pipeline}:unconfigured_scopes={unknown[:10]}")
        for scope, cap in sorted(scopes.items()):
            limit = cap * float(gross)
            value = realized[scope]
            details.append(f"{pipeline}::{scope}={value:.6f}<=cap {limit:.6f}")
            if not np.isfinite(cap) or cap < 0.0:
                violations.append(f"{pipeline}::{scope}:invalid_cap={cap!r}")
            elif value > limit + tolerance:
                violations.append(
                    f"{pipeline}::{scope}:weight={value:.6f}>cap={limit:.6f}"
                )
    return details, violations


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    risk_dir = run_dir / "risk"
    opt_dir = run_dir / "optimizer"
    weights_path = opt_dir / "target_weights.csv"
    excluded_path = opt_dir / "risk_excluded_candidates.csv"
    meta_path = opt_dir / "optimizer_meta.json"
    validation_path = opt_dir / "validation" / "optimizer_validation.csv"
    manifest_path = opt_dir / "optimizer_manifest.json"
    scores_path = run_dir / "stocks_scores.csv"
    coverage_path = risk_dir / "risk_coverage.csv"
    covariance_path = risk_dir / "covariance.csv"
    stage1_manifest_path = run_dir / "manifest.json"
    risk_manifest_path = risk_dir / "risk_manifest.json"
    monitor_overlay_path = opt_dir / "monitor_eligibility_overlay.csv"
    monitor_manifest_path = opt_dir / "monitor_eligibility_manifest.json"

    for required in (
        weights_path, excluded_path, meta_path, scores_path, coverage_path, covariance_path,
        stage1_manifest_path, risk_manifest_path,
    ):
        if not required.exists():
            LOGGER.error("Missing required Stage 3 validation input: %s", required)
            return 1
    if args.force:
        invalidate_dependents(run_dir, "optimizer")
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    oc = cfg_get(config, "optimizer", {})
    gross = float(oc.get("gross_exposure", 1.0))
    allow_constraint_cash = oc.get("allow_constraint_cash") is True
    max_weight = float(oc.get("max_weight_per_name", 0.05))
    min_hold = float(oc.get("min_weight_to_hold", 0.0005))
    # Reproduce the pre-solve liquidity floor so universe/exclusion checks stay exact (fail-open).
    max_half_spread = finite_float(oc.get("max_half_spread_bps", 0.0)) or 0.0
    spread_rows = load_spread_snapshot(risk_dir / "spread_snapshot.csv") if max_half_spread > 0 else {}

    def liquidity_excluded(ticker: str) -> bool:
        if max_half_spread <= 0:
            return False
        spread_bps = median_half_spread_bps(spread_rows.get(ticker))
        return spread_bps is not None and spread_bps > max_half_spread

    weight_tol = 1e-6
    cap_tol = 1e-8
    band_tol = 1e-8

    rows = read_csv(weights_path)
    excluded_rows = read_csv(excluded_path)
    excluded = {str(r.get("ticker", "")).strip() for r in excluded_rows if str(r.get("ticker", "")).strip()}
    meta = load_json(meta_path)
    monitor_meta = (
        meta.get("monitor_entry_policy", {})
        if isinstance(meta.get("monitor_entry_policy"), dict)
        else {}
    )
    monitor_status = str(monitor_meta.get("status", ""))
    monitor_overlay: dict[str, dict[str, str]] = {}
    monitor_seal_errors: list[str] = []
    if monitor_status == "applied":
        if not monitor_overlay_path.is_file() or not monitor_manifest_path.is_file():
            monitor_seal_errors.append("monitor_overlay_or_manifest_missing")
        else:
            monitor_manifest = read_manifest(monitor_manifest_path)
            if (
                manifest_acceptance_value(monitor_manifest) != "PASS"
                or str(monitor_manifest.get("run_as_of", "")) != run_as_of
                or monitor_manifest.get("production_entry_gate") is not True
            ):
                monitor_seal_errors.append("monitor_manifest_not_same_date_production_pass")
            monitor_seal_errors.extend(
                sealed_artifact_errors(
                    monitor_manifest,
                    monitor_overlay_path,
                    monitor_overlay_path.name,
                    run_as_of=run_as_of,
                )
            )
            for row in read_csv(monitor_overlay_path):
                ticker = str(row.get("ticker", "")).strip().upper()
                if not ticker or ticker in monitor_overlay:
                    monitor_seal_errors.append(
                        f"monitor_overlay_blank_or_duplicate={ticker!r}"
                    )
                    continue
                monitor_overlay[ticker] = row
            if str(monitor_meta.get("overlay_sha256", "")) != sha256_file(
                monitor_overlay_path
            ):
                monitor_seal_errors.append("optimizer_meta_overlay_hash_mismatch")
            if str(monitor_meta.get("manifest_sha256", "")) != sha256_file(
                monitor_manifest_path
            ):
                monitor_seal_errors.append("optimizer_meta_monitor_manifest_hash_mismatch")
    elif monitor_status != "bootstrap_ignored":
        monitor_seal_errors.append(f"unknown_monitor_policy_status={monitor_status!r}")
    stage1_manifest = load_json(stage1_manifest_path)
    risk_manifest = load_json(risk_manifest_path)
    scores = {r["ticker"]: r for r in read_csv(scores_path)}
    coverage = {r["ticker"]: r for r in read_csv(coverage_path)}
    cov_first_col = read_csv(covariance_path)
    cov_tickers = {str(r.get("ticker") or r.get("") or "").strip() for r in cov_first_col}
    cov_tickers.discard("")

    checks: list[dict] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    config_bound, config_detail = stage1_config_binding(stage1_manifest, config_path)
    rec(
        "config_hash_matches_stage1_manifest",
        "PASS" if config_bound else "FAIL",
        config_detail,
    )

    rec(
        "monitor_policy_mode_sealed",
        "PASS" if not monitor_seal_errors else "FAIL",
        (
            f"status={monitor_status}; deployable={monitor_status == 'applied'}"
            if not monitor_seal_errors
            else str(monitor_seal_errors[:20])
        ),
    )

    universe = [str(r.get("ticker", "")).strip() for r in rows]
    universe_counts = Counter(universe)
    duplicate_universe = sorted(t for t, count in universe_counts.items() if t and count > 1)
    book_set = {t for t in universe if t}
    held = []
    for r in rows:
        parsed_weight = finite_float(r.get("weight"))
        if parsed_weight is not None and parsed_weight > 0:
            held.append(r)

    # 1. exact optimizer universe: every eligible scored/risk-covered/cov-backed name is present once,
    # and no other name is in the optimizer book.
    def monitor_entry_eligible(ticker: str) -> bool:
        if monitor_status == "bootstrap_ignored":
            return True
        return str(
            monitor_overlay.get(ticker, {}).get("optimizer_entry_eligible", "")
        ).strip() == "1"

    expected_universe = {
        t for t, s in scores.items()
        if str(s.get("investable_eligible", "")).strip() == "1"
        and monitor_entry_eligible(t)
        and str(coverage.get(t, {}).get("risk_eligible", "")).strip() == "1"
        and str(coverage.get(t, {}).get("role", "")).strip() == "scored"
        and t in cov_tickers
        and not liquidity_excluded(t)
    }
    missing_book = sorted(expected_universe - book_set)
    extra_book = sorted(book_set - expected_universe)
    rec(
        "optimizer_universe_exact",
        "PASS" if not (missing_book or extra_book or duplicate_universe) else "FAIL",
        (
            f"universe={len(book_set)} exactly matches expected scored/monitor/risk/cov names"
            if not (missing_book or extra_book or duplicate_universe)
            else f"missing={missing_book[:10]} extra={extra_book[:10]} duplicates={duplicate_universe[:10]}"
        ),
    )

    # 2. exact exclusion report: every investable name outside the optimizer universe is named, and no
    # optimizer-eligible name is wrongly reported as excluded.
    investable = {t for t, r in scores.items() if str(r.get("investable_eligible", "")).strip() == "1"}
    expected_excluded = investable - expected_universe
    missing_exclusions = sorted(expected_excluded - excluded)
    extra_exclusions = sorted(excluded - expected_excluded)
    rec(
        "optimizer_exclusions_exact",
        "PASS" if not (missing_exclusions or extra_exclusions) else "FAIL",
        (
            f"excluded={len(excluded)} exactly matches eligible names outside optimizer universe"
            if not (missing_exclusions or extra_exclusions)
            else f"missing={missing_exclusions[:10]} extra={extra_exclusions[:10]}"
        ),
    )

    if monitor_status == "applied":
        investable_missing_overlay = sorted(investable - set(monitor_overlay))
        reason_by_ticker = {
            str(row.get("ticker", "")).strip(): str(
                row.get("exclusion_reason", "")
            ).strip()
            for row in excluded_rows
        }
        expected_monitor_excluded = {
            ticker
            for ticker in investable
            if not monitor_entry_eligible(ticker)
        }
        wrong_monitor_reasons = sorted(
            ticker
            for ticker in expected_monitor_excluded
            if reason_by_ticker.get(ticker) != "monitor_entry_policy"
        )
        extra_monitor_reasons = sorted(
            ticker
            for ticker, reason in reason_by_ticker.items()
            if reason == "monitor_entry_policy"
            and ticker not in expected_monitor_excluded
        )
        monitor_count_matches = int(monitor_meta.get("n_excluded", -1)) == len(
            expected_monitor_excluded
        )
        rec(
            "monitor_entry_exclusions_exact",
            (
                "PASS"
                if not (
                    investable_missing_overlay
                    or wrong_monitor_reasons
                    or extra_monitor_reasons
                )
                and monitor_count_matches
                else "FAIL"
            ),
            (
                f"excluded={len(expected_monitor_excluded)}; all reasons and meta agree"
                if not (
                    investable_missing_overlay
                    or wrong_monitor_reasons
                    or extra_monitor_reasons
                )
                and monitor_count_matches
                else (
                    f"missing_overlay={investable_missing_overlay[:10]} "
                    f"wrong_reason={wrong_monitor_reasons[:10]} "
                    f"extra_reason={extra_monitor_reasons[:10]} "
                    f"meta_count={monitor_meta.get('n_excluded')}"
                )
            ),
        )

    # 2b. liquidity floor: every name above the half-spread ceiling is excluded with the right reason,
    # and the optimizer_meta liquidity_floor block agrees with the reproduced exclusion count.
    if max_half_spread > 0:
        reason_by_ticker = {
            str(r.get("ticker", "")).strip(): str(r.get("exclusion_reason", "")).strip()
            for r in excluded_rows
        }
        # Monitor policy is evaluated before liquidity in Stage 3. A name failing
        # both gates must retain the monitor reason, so only monitor-eligible names
        # belong to the liquidity-reason set.
        expected_liquidity = {
            t
            for t in expected_excluded
            if monitor_entry_eligible(t) and liquidity_excluded(t)
        }
        liq_bad = []
        for t in sorted(expected_liquidity):
            if reason_by_ticker.get(t) != "liquidity_spread":
                liq_bad.append(f"{t}:reason={reason_by_ticker.get(t)!r}")
        wrong_reason = sorted(
            t for t, reason in reason_by_ticker.items()
            if reason == "liquidity_spread" and not liquidity_excluded(t)
        )
        if wrong_reason:
            liq_bad.append(f"reason_liquidity_but_not_over_ceiling={wrong_reason[:10]}")
        meta_liq = meta.get("liquidity_floor", {}) if isinstance(meta.get("liquidity_floor"), dict) else {}
        n_excluded_liq = sum(1 for r in excluded_rows if str(r.get("exclusion_reason", "")).strip() == "liquidity_spread")
        if int(meta_liq.get("n_excluded", -1)) != n_excluded_liq:
            liq_bad.append(f"meta.n_excluded={meta_liq.get('n_excluded')}!={n_excluded_liq}")
        rec(
            "liquidity_floor_exclusions_reasoned",
            "PASS" if not liq_bad else "FAIL",
            f"{n_excluded_liq} names excluded>{max_half_spread}bps, reasons + meta agree"
            if not liq_bad else f"{liq_bad[:10]}",
        )

    # 3. numeric fields are finite. NaN/Inf here would corrupt optimizer handoff.
    numeric_fields = ["final_score", "score_confidence", "mu_raw", "mu_used",
                      "weight", "weight_band_low", "weight_band_high"]
    bad_numeric = []
    for r in rows:
        ticker = str(r.get("ticker", ""))
        for field in numeric_fields:
            if finite_float(r.get(field)) is None:
                bad_numeric.append(f"{ticker}:{field}")
    rec(
        "numeric_fields_finite",
        "PASS" if not bad_numeric else "FAIL",
        "all optimizer numeric fields are finite" if not bad_numeric else f"non-finite: {bad_numeric[:10]}",
    )

    # Recompute hard-cap capacity independently from the producer. Caps remain
    # absolute NAV fractions even when an unavailable sector forces residual cash.
    solver_universe = sorted(expected_universe)
    solver_index = {ticker: index for index, ticker in enumerate(solver_universe)}
    capacity_group_caps: list[tuple[list[int], float]] = []
    for pipeline, cap in sorted(
        {str(k): float(v) for k, v in (oc.get("sector_weight_caps") or {}).items()}.items()
    ):
        indices = [
            solver_index[ticker]
            for ticker in solver_universe
            if str(scores[ticker].get("source_pipeline", "")).strip() == pipeline
        ]
        if indices:
            capacity_group_caps.append((indices, cap))
    for pipeline, raw_scopes in sorted(
        dict(oc.get("scope_weight_caps") or {}).items()
    ):
        for scope, raw_cap in sorted(dict(raw_scopes or {}).items()):
            indices = [
                solver_index[ticker]
                for ticker in solver_universe
                if str(scores[ticker].get("source_pipeline", "")).strip() == str(pipeline)
                and str(scores[ticker].get("model_scope_id", "")).strip() == str(scope)
            ]
            if indices:
                capacity_group_caps.append((indices, float(raw_cap)))
    fixed_equal_sleeves = {
        str(value).strip()
        for value in (oc.get("fixed_equal_weight_sleeves") or [])
        if str(value).strip()
    }
    capacity_equal_groups = [
        [
            solver_index[ticker]
            for ticker in solver_universe
            if str(scores[ticker].get("source_pipeline", "")).strip() == pipeline
        ]
        for pipeline in sorted(fixed_equal_sleeves)
    ]
    capacity_equal_groups = [indices for indices in capacity_equal_groups if indices]
    capacity_errors: list[str] = []
    maximum_gross = float("nan")
    expected_invested_gross = gross
    expected_constraint_cash = False
    capacity_attempts: list[str] = []
    try:
        maximum_gross, capacity_attempts = maximum_investable_gross(
            len(solver_universe),
            group_caps=capacity_group_caps or None,
            cap_base_gross=gross,
            max_weight=max_weight,
            equal_weight_groups=capacity_equal_groups or None,
        )
        expected_invested_gross, expected_constraint_cash = (
            constraint_aware_invested_gross(
                requested_gross=gross,
                capacity=maximum_gross,
                allow_constraint_cash=allow_constraint_cash,
            )
        )
    except ValueError as exc:
        capacity_errors.append(str(exc))

    policy_meta = (
        meta.get("constraint_cash_policy", {})
        if isinstance(meta.get("constraint_cash_policy"), dict)
        else {}
    )
    if policy_meta.get("enabled") is not allow_constraint_cash:
        capacity_errors.append(
            f"policy_enabled={policy_meta.get('enabled')!r}!={allow_constraint_cash!r}"
        )
    if policy_meta.get("triggered") is not expected_constraint_cash:
        capacity_errors.append(
            f"policy_triggered={policy_meta.get('triggered')!r}!={expected_constraint_cash!r}"
        )
    for field, expected in (
        ("requested_gross", gross),
        ("maximum_investable_gross", maximum_gross),
        ("invested_gross", expected_invested_gross),
        ("cash_weight_before_cost_overlay", gross - expected_invested_gross),
    ):
        actual = finite_float(policy_meta.get(field))
        if actual is None or not np.isfinite(expected) or abs(actual - expected) > 5e-6:
            capacity_errors.append(f"{field}={actual!r}!={expected!r}")
    if list(policy_meta.get("capacity_solver_attempts") or []) != capacity_attempts:
        capacity_errors.append("capacity_solver_attempts_mismatch")
    if str(policy_meta.get("cap_reference", "")) != "configured_gross_nav_fraction":
        capacity_errors.append("cap_reference_not_absolute_nav")
    rec(
        "constraint_cash_policy_valid",
        "PASS" if not capacity_errors else "FAIL",
        (
            f"requested={gross:.6f}; capacity={maximum_gross:.6f}; "
            f"invested={expected_invested_gross:.6f}; "
            f"cash={gross - expected_invested_gross:.6f}; "
            f"triggered={expected_constraint_cash}"
            if not capacity_errors
            else str(capacity_errors[:20])
        ),
    )

    # 4. weights valid: long-only, capped, no dust holdings, and sum to the independently
    # recomputed feasible invested gross with strict tolerance.
    w = np.array([finite_float(r.get("weight")) if finite_float(r.get("weight")) is not None else np.nan for r in rows])
    bad_w = []
    if len(w) == 0:
        bad_w.append("empty_weights")
    if not np.isfinite(w).all():
        bad_w.append("nonfinite_weights")
    else:
        if (w < -cap_tol).any():
            bad_w.append("negative_weights")
        if (w > max_weight + cap_tol).any():
            bad_w.append(f"cap_breach>{max_weight}")
        if min_hold > 0 and ((w > cap_tol) & (w < min_hold - cap_tol)).any():
            bad_w.append(f"dust_weight_below_min_hold>{min_hold}")
        if abs(float(w.sum()) - expected_invested_gross) > weight_tol:
            bad_w.append(
                f"sum={float(w.sum()):.10f}!={expected_invested_gross}"
            )
    rec(
        "weights_valid",
        "PASS" if not bad_w else "FAIL",
        (
            f"long-only, <= {max_weight}, no dust, "
            f"sum={float(np.nansum(w)):.10f}, requested_gross={gross:.10f}"
            if not bad_w else f"{bad_w}"
        ),
    )

    # 4b. explicit sleeve budget caps respected in the LIVE book (optimizer.sector_weight_caps).
    sector_caps = {str(k): float(v) for k, v in (oc.get("sector_weight_caps") or {}).items()}
    if sector_caps:
        cap_bad: list[str] = []
        cap_detail: list[str] = []
        for pipeline, cap in sorted(sector_caps.items()):
            realized = 0.0
            for r in rows:
                ticker = str(r.get("ticker", "")).strip()
                contract = scores.get(ticker, r)
                if str(contract.get("source_pipeline", "")).strip() == pipeline:
                    wt = finite_float(r.get("weight"))
                    realized += wt if wt is not None else 0.0
            cap_detail.append(f"{pipeline}={realized:.6f}<=cap {cap * gross:.6f}")
            if not np.isfinite(cap) or cap < 0.0:
                cap_bad.append(f"{pipeline}:invalid_cap={cap!r}")
            elif realized > cap * gross + weight_tol:
                cap_bad.append(f"{pipeline}:weight={realized:.6f}>cap={cap * gross:.6f}")
        rec(
            "sector_weight_caps_respected",
            "PASS" if not cap_bad else "FAIL",
            "; ".join(cap_detail) if not cap_bad else f"violations: {cap_bad}",
        )

    # 4c. cohort/model-scope caps are independent constraints, not implied by the sector cap.
    scope_caps = {
        str(pipeline): {
            str(scope): float(cap)
            for scope, cap in dict(raw_scopes or {}).items()
        }
        for pipeline, raw_scopes in dict(oc.get("scope_weight_caps") or {}).items()
    }
    if scope_caps:
        scope_detail, scope_bad = evaluate_scope_weight_caps(
            rows,
            scores,
            scope_caps,
            gross=gross,
            tolerance=weight_tol,
        )
        rec(
            "scope_weight_caps_respected",
            "PASS" if not scope_bad else "FAIL",
            "; ".join(scope_detail) if not scope_bad else f"violations: {scope_bad}",
        )

    # 5. sensitivity bands are post-finalization bands, so the published weight must be contained.
    band_bad = []
    for r in rows:
        t = str(r.get("ticker", ""))
        wt = finite_float(r.get("weight"))
        low = finite_float(r.get("weight_band_low"))
        high = finite_float(r.get("weight_band_high"))
        if wt is None or low is None or high is None:
            band_bad.append(f"{t}:nonfinite")
        elif low > high + band_tol:
            band_bad.append(f"{t}:low_gt_high")
        elif wt < low - band_tol or wt > high + band_tol:
            band_bad.append(f"{t}:weight_outside_band")
    rec(
        "weight_bands_contain_weights",
        "PASS" if not band_bad else "FAIL",
        "all weights fall inside finalized sensitivity bands" if not band_bad else f"violations: {band_bad[:10]}",
    )

    # 6. positive mu->weight relationship among held; negative-mu holdings allowed but reported.
    held_pairs = [
        (finite_float(r.get("mu_used")), finite_float(r.get("weight")), r["ticker"])
        for r in held
    ]
    valid_pairs = [(mu, wt, ticker) for mu, wt, ticker in held_pairs if mu is not None and wt is not None]
    mu = np.array([p[0] for p in valid_pairs], dtype=float)
    wh = np.array([p[1] for p in valid_pairs], dtype=float)
    rho = float(cast(Any, spearmanr(mu, wh)).correlation) if len(valid_pairs) > 2 else float("nan")
    neg_mu_held = [ticker for mu_val, _, ticker in valid_pairs if float(mu_val) < 0]
    ok_rel = bool(np.isfinite(rho) and rho > 0)
    rec(
        "mu_weight_relationship",
        "PASS" if ok_rel else "FAIL",
        f"spearman(mu_used,weight)={round(rho, 3)}; held_with_negative_mu={len(neg_mu_held)} "
        f"(diversifiers; allowed); undefined correlation fails closed",
    )

    # 7. optimizer meta matches current output files.
    meta_bad = []
    if int(meta.get("universe_size", -1)) != len(book_set):
        meta_bad.append(f"universe_size={meta.get('universe_size')}!={len(book_set)}")
    if int(meta.get("n_held", -1)) != len(held):
        meta_bad.append(f"n_held={meta.get('n_held')}!={len(held)}")
    if int(meta.get("n_excluded_candidates", -1)) != len(excluded):
        meta_bad.append(f"n_excluded_candidates={meta.get('n_excluded_candidates')}!={len(excluded)}")
    if finite_float(meta.get("gross_exposure")) != gross:
        meta_bad.append(f"gross_exposure={meta.get('gross_exposure')}!={gross}")
    meta_invested_gross = finite_float(meta.get("invested_gross"))
    if meta_invested_gross is None or abs(meta_invested_gross - expected_invested_gross) > 5e-6:
        meta_bad.append(
            f"invested_gross={meta.get('invested_gross')}!={expected_invested_gross}"
        )
    meta_constraint_cash = finite_float(meta.get("constraint_cash_weight"))
    expected_cash = gross - expected_invested_gross
    if meta_constraint_cash is None or abs(meta_constraint_cash - expected_cash) > 5e-6:
        meta_bad.append(
            f"constraint_cash_weight={meta.get('constraint_cash_weight')}!={expected_cash}"
        )
    if np.isfinite(w).all() and abs(float(meta.get("sum_weights", np.nan)) - float(w.sum())) > 5e-6:
        meta_bad.append(f"sum_weights_meta={meta.get('sum_weights')} csv={float(w.sum()):.10f}")
    if np.isfinite(w).all() and abs(float(meta.get("max_weight_actual", np.nan)) - float(w.max(initial=0.0))) > 5e-6:
        meta_bad.append(f"max_weight_meta={meta.get('max_weight_actual')} csv={float(w.max(initial=0.0)):.10f}")
    rec(
        "optimizer_meta_matches_outputs",
        "PASS" if not meta_bad else "FAIL",
        "optimizer_meta agrees with target/exclusion outputs" if not meta_bad else f"{meta_bad}",
    )

    # 8. upstream sealed inputs are still exactly the artifacts Stage 1/2 accepted.
    stocks_hash = sha256_file(scores_path)
    coverage_hash = sha256_file(coverage_path)
    covariance_hash = sha256_file(covariance_path)
    upstream_bad = []
    if stage1_manifest.get("hard_gate_acceptance") != "PASS":
        upstream_bad.append(f"stage1_hard_gate_acceptance={stage1_manifest.get('hard_gate_acceptance')}")
    if stocks_hash != manifest_hash(stage1_manifest, "stocks_scores.csv"):
        upstream_bad.append("stocks_scores_hash_mismatch_stage1_manifest")
    if risk_manifest.get("acceptance") != "PASS":
        upstream_bad.append(f"stage2_acceptance={risk_manifest.get('acceptance')}")
    if coverage_hash != manifest_hash(risk_manifest, "risk_coverage.csv"):
        upstream_bad.append("risk_coverage_hash_mismatch_stage2_manifest")
    if covariance_hash != manifest_hash(risk_manifest, "covariance.csv"):
        upstream_bad.append("covariance_hash_mismatch_stage2_manifest")
    rec(
        "upstream_manifests_valid",
        "PASS" if not upstream_bad else "FAIL",
        "Stage 1/2 manifests pass and hashes match consumed artifacts" if not upstream_bad else f"{upstream_bad}",
    )

    # 9. covariance injected from the accepted Stage 2 artifact, never rebuilt inside Stage 3.
    cov_match_current = meta.get("covariance_sha256") == covariance_hash
    cov_match_stage2 = meta.get("covariance_sha256") == manifest_hash(risk_manifest, "covariance.csv")
    rec(
        "covariance_injected_from_stage2",
        "PASS" if cov_match_current and cov_match_stage2 and meta.get("covariance_source") == "stage2_risk_covariance_csv" else "FAIL",
        f"covariance_source={meta.get('covariance_source')} current_hash_match={cov_match_current} "
        f"stage2_hash_match={cov_match_stage2}",
    )

    # 10. solver converged.
    solver = meta.get("solver", {})
    rec(
        "solver_optimal",
        "PASS" if solver.get("status") in ("optimal", "optimal_inaccurate") else "FAIL",
        f"solver={solver.get('solver_used')} status={solver.get('status')} obj={solver.get('objective_value')}",
    )

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)

    passed = all(c["status"] == "PASS" for c in checks)
    # Provenance: hash every input that determines the book, incl. upstream manifests + vendored source.
    provenance_paths = {
        "stocks_scores.csv": scores_path,
        "risk_coverage.csv": coverage_path,
        "covariance.csv": covariance_path,
        "stage1_manifest.json": stage1_manifest_path,
        "stage2_risk_manifest.json": risk_manifest_path,
        "config.yaml": config_path,
        "target_weights.csv": weights_path,
        "risk_excluded_candidates.csv": excluded_path,
        "optimizer_meta.json": meta_path,
        "validation/optimizer_validation.csv": validation_path,
        "vendored/tier1_portfolio_optimizer.py": PACKAGE_ROOT / "optimizer" / "tier1_portfolio_optimizer.py",
        "vendored/tier1_common.py": PACKAGE_ROOT / "optimizer" / "tier1_common.py",
        "optimizer_core.py": PACKAGE_ROOT / "optimizer" / "optimizer_core.py",
        "08_build_monitor_eligibility_overlay.py": PACKAGE_ROOT / "optimizer" / "08_build_monitor_eligibility_overlay.py",
        "09_run_portfolio_optimizer.py": PACKAGE_ROOT / "optimizer" / "09_run_portfolio_optimizer.py",
        "10_validate_optimizer_outputs.py": Path(__file__).resolve(),
    }
    if monitor_status == "applied":
        provenance_paths["monitor_eligibility_overlay.csv"] = monitor_overlay_path
        provenance_paths["monitor_eligibility_manifest.json"] = monitor_manifest_path
    provenance = {name: sha256_file(p) for name, p in provenance_paths.items() if p.exists()}
    manifest = {
        "run_as_of": run_as_of,
        "stage": "stage3_aqr_baseline",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "deployable": passed and monitor_status == "applied",
        "universe_size": len(book_set),
        "n_held": len(held),
        "n_excluded_candidates": len(excluded),
        "optimizer_config": oc,
        "monitor_entry_policy": monitor_meta,
        "provenance_sha256": provenance,
        "checks": checks,
    }
    write_manifest(manifest_path, manifest)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if passed:
        LOGGER.info("STAGE 3 ACCEPTANCE: PASS (as_of=%s, %d held) -> %s", run_as_of, len(held), manifest_path)
        return 0
    LOGGER.error("STAGE 3 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
