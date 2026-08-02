#!/usr/bin/env python3
"""Reconcile FMP and Alpha estimates without averaging either provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.estimate_policy import (  # noqa: E402
    CanonicalEstimate,
    canonicalize_snapshot,
    currency_comparison,
    relative_difference,
    select_conservative,
)
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    artifact_snapshot_dependency_errors,
    connect_monitor_db,
    record_snapshot_dependencies,
    supersede_artifact_dependencies,
    writer_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OUTPUT_FIELDS = [
    "reconciliation_cycle",
    "universe_as_of",
    "ticker",
    "sector",
    "source_pipeline",
    "metric",
    "canonical_period",
    "fiscal_period_end",
    "currency_comparison",
    "comparison_status",
    "alpha_snapshot_id",
    "alpha_estimate_average",
    "alpha_estimate_low",
    "alpha_estimate_high",
    "alpha_analyst_count",
    "alpha_quality_status",
    "fmp_snapshot_id",
    "fmp_estimate_average",
    "fmp_estimate_low",
    "fmp_estimate_high",
    "fmp_analyst_count",
    "fmp_quality_status",
    "pair_fetch_skew_minutes",
    "absolute_difference",
    "relative_difference",
    "disagreement_flag",
    "conservative_candidate_provider",
    "conservative_candidate_snapshot_id",
    "conservative_candidate_estimate_average",
    "conservative_candidate_reason",
    "conservative_candidate_computed",
    "downside_guard_eligible",
    "downside_guard_ineligibility_reasons",
    "permitted_use",
    "provider_range_status",
    "agreement_range_lower",
    "agreement_range_upper",
    "outer_envelope_lower",
    "outer_envelope_upper",
    "symmetric_action_boundary_eligible",
    "boundary_permitted_use",
    "central_estimate_status",
    "accuracy_status",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--retrieval-cycle")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _optional(value: Any) -> Any:
    return "" if value is None else value


def _index(
    rows: list[Any],
) -> tuple[dict[str, dict[tuple[str, str, str, str], CanonicalEstimate]], list[str]]:
    result: dict[str, dict[tuple[str, str, str, str], CanonicalEstimate]] = {
        "alpha_vantage": {},
        "fmp": {},
    }
    errors: list[str] = []
    for raw in rows:
        row = canonicalize_snapshot(raw)
        prior = result[row.provider].get(row.key)
        if prior is not None:
            errors.append(f"duplicate_canonical_identity:{row.provider}:{'|'.join(row.key)}")
            continue
        result[row.provider][row.key] = row
    return result, errors


def _fetch_skew_minutes(alpha: CanonicalEstimate, fmp: CanonicalEstimate) -> float:
    alpha_time = datetime.fromisoformat(alpha.fetched_at_utc)
    fmp_time = datetime.fromisoformat(fmp.fetched_at_utc)
    if alpha_time.tzinfo is None or fmp_time.tzinfo is None:
        raise ValueError("Provider fetched_at_utc values must be timezone-aware")
    return abs((alpha_time - fmp_time).total_seconds()) / 60.0


def _provider_boundaries(
    alpha: CanonicalEstimate | None,
    fmp: CanonicalEstimate | None,
) -> dict[str, float | str]:
    if alpha is None or fmp is None:
        return {
            "status": "SINGLE_SOURCE_OR_MISSING",
            "agreement_lower": "",
            "agreement_upper": "",
            "envelope_lower": "",
            "envelope_upper": "",
        }
    if alpha.quality_status == "FAIL" or fmp.quality_status == "FAIL":
        return {
            "status": "QUALITY_FAILURE",
            "agreement_lower": "",
            "agreement_upper": "",
            "envelope_lower": "",
            "envelope_upper": "",
        }
    if any(
        value is None
        for value in (
            alpha.estimate_low,
            alpha.estimate_high,
            fmp.estimate_low,
            fmp.estimate_high,
        )
    ):
        return {
            "status": "TWO_PROVIDER_RANGE_INCOMPLETE",
            "agreement_lower": "",
            "agreement_upper": "",
            "envelope_lower": "",
            "envelope_upper": "",
        }
    assert alpha.estimate_low is not None
    assert alpha.estimate_high is not None
    assert fmp.estimate_low is not None
    assert fmp.estimate_high is not None
    agreement_lower = max(alpha.estimate_low, fmp.estimate_low)
    agreement_upper = min(alpha.estimate_high, fmp.estimate_high)
    return {
        "status": (
            "TWO_PROVIDER_RANGE_OVERLAP"
            if agreement_lower <= agreement_upper
            else "TWO_PROVIDER_RANGE_NO_OVERLAP"
        ),
        "agreement_lower": agreement_lower if agreement_lower <= agreement_upper else "",
        "agreement_upper": agreement_upper if agreement_lower <= agreement_upper else "",
        "envelope_lower": min(alpha.estimate_low, fmp.estimate_low),
        "envelope_upper": max(alpha.estimate_high, fmp.estimate_high),
    }


def reconcile(
    rows: list[Any],
    *,
    universe: dict[str, dict[str, str]],
    retrieval_cycle: str,
    universe_as_of: str,
    minimum_fiscal_period_end: date,
    tie_break_provider: str,
    warn_relative: dict[str, float],
    difference_floor: dict[str, float],
    minimum_analyst_count: int,
    maximum_pair_fetch_skew_minutes: float,
    require_verified_currency: bool,
    require_range_overlap_for_action_boundary: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if minimum_analyst_count < 1:
        raise ValueError("minimum_analyst_count must be positive")
    if maximum_pair_fetch_skew_minutes < 0:
        raise ValueError("maximum_pair_fetch_skew_minutes must be non-negative")
    active_rows = [
        row
        for row in rows
        if date.fromisoformat(str(row["fiscal_period_end"])) >= minimum_fiscal_period_end
    ]
    indexed, errors = _index(active_rows)
    keys = sorted(set(indexed["alpha_vantage"]) | set(indexed["fmp"]))
    output: list[dict[str, Any]] = []
    for key in keys:
        alpha = indexed["alpha_vantage"].get(key)
        fmp = indexed["fmp"].get(key)
        ticker, metric, canonical_period, fiscal_period_end = key
        both = alpha is not None and fmp is not None
        if alpha is not None and fmp is not None:
            currency_state = currency_comparison(alpha, fmp)
            both_valid = alpha.quality_status != "FAIL" and fmp.quality_status != "FAIL"
        else:
            currency_state = "SINGLE_SOURCE"
            both_valid = False
        if (
            both_valid
            and currency_state != "MISMATCH"
            and alpha is not None
            and fmp is not None
        ):
            comparison_status = (
                "COMPARABLE_BOTH"
                if currency_state == "MATCH"
                else "COMPARABLE_CURRENCY_UNVERIFIED"
            )
        elif both and currency_state == "MISMATCH":
            comparison_status = "NOT_COMPARABLE_CURRENCY_MISMATCH"
        elif both:
            comparison_status = "NOT_COMPARABLE_QUALITY_FAILURE"
        elif alpha is not None:
            comparison_status = "ALPHA_ONLY"
        else:
            comparison_status = "FMP_ONLY"

        pair_fetch_skew: float | str = ""
        if alpha is not None and fmp is not None:
            pair_fetch_skew = _fetch_skew_minutes(alpha, fmp)

        candidate: CanonicalEstimate | None = None
        candidate_reason = "requires_two_comparable_sources"
        candidate_computed = False
        if (
            both_valid
            and currency_state != "MISMATCH"
            and alpha is not None
            and fmp is not None
        ):
            candidate, candidate_reason, candidate_computed = select_conservative(
                alpha,
                fmp,
                tie_break_provider=tie_break_provider,
            )

        ineligibility_reasons: list[str] = []
        if not candidate_computed:
            ineligibility_reasons.append("no_two_source_conservative_candidate")
        if require_verified_currency and currency_state != "MATCH":
            ineligibility_reasons.append(f"currency_{currency_state.casefold()}")
        if alpha is None or alpha.analyst_count is None:
            ineligibility_reasons.append("alpha_analyst_count_missing")
        elif alpha.analyst_count < minimum_analyst_count:
            ineligibility_reasons.append("alpha_analyst_count_below_minimum")
        if fmp is None or fmp.analyst_count is None:
            ineligibility_reasons.append("fmp_analyst_count_missing")
        elif fmp.analyst_count < minimum_analyst_count:
            ineligibility_reasons.append("fmp_analyst_count_below_minimum")
        if (
            isinstance(pair_fetch_skew, float)
            and pair_fetch_skew > maximum_pair_fetch_skew_minutes
        ):
            ineligibility_reasons.append("pair_fetch_skew_above_maximum")
        downside_guard_eligible = int(not ineligibility_reasons)
        boundaries = _provider_boundaries(alpha, fmp)
        symmetric_boundary_eligible = int(
            bool(downside_guard_eligible)
            and (
                boundaries["status"] == "TWO_PROVIDER_RANGE_OVERLAP"
                or not require_range_overlap_for_action_boundary
            )
            and boundaries["status"]
            in {"TWO_PROVIDER_RANGE_OVERLAP", "TWO_PROVIDER_RANGE_NO_OVERLAP"}
        )

        absolute: float | str = ""
        relative: float | str = ""
        disagreement = 0
        if (
            both_valid
            and currency_state != "MISMATCH"
            and alpha is not None
            and fmp is not None
        ):
            assert alpha.estimate_average is not None
            assert fmp.estimate_average is not None
            absolute = abs(alpha.estimate_average - fmp.estimate_average)
            relative = relative_difference(
                alpha.estimate_average,
                fmp.estimate_average,
                floor=float(difference_floor[metric]),
            )
            disagreement = int(relative >= float(warn_relative[metric]))

        info = universe.get(ticker, {})
        output.append(
            {
                "reconciliation_cycle": retrieval_cycle,
                "universe_as_of": universe_as_of,
                "ticker": ticker,
                "sector": info.get("sector", ""),
                "source_pipeline": info.get("source_pipeline", ""),
                "metric": metric,
                "canonical_period": canonical_period,
                "fiscal_period_end": fiscal_period_end,
                "currency_comparison": currency_state,
                "comparison_status": comparison_status,
                "alpha_snapshot_id": alpha.snapshot_id if alpha else "",
                "alpha_estimate_average": _optional(alpha.estimate_average if alpha else None),
                "alpha_estimate_low": _optional(alpha.estimate_low if alpha else None),
                "alpha_estimate_high": _optional(alpha.estimate_high if alpha else None),
                "alpha_analyst_count": _optional(alpha.analyst_count if alpha else None),
                "alpha_quality_status": alpha.quality_status if alpha else "MISSING",
                "fmp_snapshot_id": fmp.snapshot_id if fmp else "",
                "fmp_estimate_average": _optional(fmp.estimate_average if fmp else None),
                "fmp_estimate_low": _optional(fmp.estimate_low if fmp else None),
                "fmp_estimate_high": _optional(fmp.estimate_high if fmp else None),
                "fmp_analyst_count": _optional(fmp.analyst_count if fmp else None),
                "fmp_quality_status": fmp.quality_status if fmp else "MISSING",
                "pair_fetch_skew_minutes": pair_fetch_skew,
                "absolute_difference": absolute,
                "relative_difference": relative,
                "disagreement_flag": disagreement,
                "conservative_candidate_provider": candidate.provider if candidate else "",
                "conservative_candidate_snapshot_id": candidate.snapshot_id if candidate else "",
                "conservative_candidate_estimate_average": _optional(
                    candidate.estimate_average if candidate else None
                ),
                "conservative_candidate_reason": candidate_reason,
                "conservative_candidate_computed": int(candidate_computed),
                "downside_guard_eligible": downside_guard_eligible,
                "downside_guard_ineligibility_reasons": ",".join(ineligibility_reasons),
                "permitted_use": (
                    "downside_guard_shadow_only"
                    if downside_guard_eligible
                    else "diagnostic_only"
                ),
                "provider_range_status": boundaries["status"],
                "agreement_range_lower": boundaries["agreement_lower"],
                "agreement_range_upper": boundaries["agreement_upper"],
                "outer_envelope_lower": boundaries["envelope_lower"],
                "outer_envelope_upper": boundaries["envelope_upper"],
                "symmetric_action_boundary_eligible": symmetric_boundary_eligible,
                "boundary_permitted_use": (
                    "symmetric_action_shadow_only"
                    if symmetric_boundary_eligible
                    else "diagnostic_only"
                ),
                "central_estimate_status": "unassigned_pending_prospective_accuracy",
                "accuracy_status": "pending_realized_actual",
            }
        )

    comparable = [
        row for row in output if str(row["comparison_status"]).startswith("COMPARABLE")
    ]
    summary = {
        "row_count": len(output),
        "historical_reference_excluded_count": len(rows) - len(active_rows),
        "minimum_fiscal_period_end": minimum_fiscal_period_end.isoformat(),
        "comparable_count": len(comparable),
        "disagreement_count": sum(int(row["disagreement_flag"]) for row in output),
        "conservative_candidate_count": sum(
            int(row["conservative_candidate_computed"]) for row in output
        ),
        "downside_guard_eligible_count": sum(
            int(row["downside_guard_eligible"]) for row in output
        ),
        "two_provider_range_overlap_count": sum(
            row["provider_range_status"] == "TWO_PROVIDER_RANGE_OVERLAP"
            for row in output
        ),
        "two_provider_range_no_overlap_count": sum(
            row["provider_range_status"] == "TWO_PROVIDER_RANGE_NO_OVERLAP"
            for row in output
        ),
        "symmetric_action_boundary_eligible_count": sum(
            int(row["symmetric_action_boundary_eligible"]) for row in output
        ),
        "single_source_count": sum(
            row["comparison_status"] in {"ALPHA_ONLY", "FMP_ONLY"} for row in output
        ),
        "central_estimate_assigned_count": 0,
        "errors": errors,
        "acceptance": "PASS" if output and not errors else "FAIL",
    }
    return output, summary


def _digest(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_digest(rows: list[Any]) -> str:
    payload = [
        (str(row["snapshot_id"]), str(row["normalized_sha256"]))
        for row in sorted(rows, key=lambda item: str(item["snapshot_id"]))
    ]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _universe_digest(rows: list[Any]) -> str:
    payload = [
        (str(row["ticker"]), str(row["sector"]), str(row["source_pipeline"]))
        for row in rows
    ]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def run_selftest() -> None:
    def row(provider: str, value: float, snapshot: str) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot * 64,
            "provider": provider,
            "ticker": "AAA",
            "estimate_type": (
                "eps_fiscal_year" if provider == "alpha_vantage" else "eps_annual"
            ),
            "fiscal_period": "fiscal_year" if provider == "alpha_vantage" else "annual",
            "fiscal_period_end": "2026-12-31",
            "estimate_average": value,
            "estimate_low": value - 0.5,
            "estimate_high": value + 0.5,
            "analyst_count": 5,
            "currency": "USD",
            "fetched_at_utc": "2026-07-31T22:00:00+00:00",
            "retrieval_cycle": "cycle",
        }

    output, summary = reconcile(
        [row("alpha_vantage", 2.0, "a"), row("fmp", 2.5, "b")],
        universe={"AAA": {"sector": "Test", "source_pipeline": "test"}},
        retrieval_cycle="cycle",
        universe_as_of="2026-07-24",
        minimum_fiscal_period_end=date(2026, 5, 2),
        tie_break_provider="fmp",
        warn_relative={"eps": 0.10, "revenue": 0.05},
        difference_floor={"eps": 0.01, "revenue": 1.0},
        minimum_analyst_count=2,
        maximum_pair_fetch_skew_minutes=360.0,
        require_verified_currency=True,
        require_range_overlap_for_action_boundary=True,
    )
    assert summary["acceptance"] == "PASS"
    assert output[0]["conservative_candidate_provider"] == "alpha_vantage"
    assert output[0]["conservative_candidate_estimate_average"] == 2.0
    assert output[0]["conservative_candidate_computed"] == 1
    assert output[0]["downside_guard_eligible"] == 1
    assert output[0]["provider_range_status"] == "TWO_PROVIDER_RANGE_OVERLAP"
    assert output[0]["agreement_range_lower"] == 2.0
    assert output[0]["agreement_range_upper"] == 2.5
    assert output[0]["outer_envelope_lower"] == 1.5
    assert output[0]["outer_envelope_upper"] == 3.0
    assert output[0]["symmetric_action_boundary_eligible"] == 1
    assert output[0]["central_estimate_status"] == "unassigned_pending_prospective_accuracy"
    assert output[0]["disagreement_flag"] == 1
    assert _digest(output) == _digest(output)
    print("provider estimate reconciliation selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if not args.retrieval_cycle or args.as_of is None or args.universe_as_of is None:
        raise ValueError("--retrieval-cycle, --as-of, and --universe-as-of are required")

    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    policy = (
        monitor_cfg.get("provider_reconciliation", {})
        if isinstance(monitor_cfg, dict)
        else {}
    )
    if not isinstance(policy, dict) or policy.get("policy_version") != "provider_reconciliation_v2":
        raise ValueError("provider_reconciliation_v2 config is required")
    if (
        policy.get("no_averaging") is not True
        or policy.get("selection_mode") != "downside_guard_candidate_only"
        or policy.get("central_estimate_policy")
        != "unassigned_pending_prospective_accuracy"
        or policy.get("require_both_providers_for_candidate") is not True
    ):
        raise ValueError(
            "Provider reconciliation must prohibit averaging, require both providers, "
            "and leave the central estimate unassigned"
        )
    tie_break = str(
        policy.get("deterministic_tie_break_provider", "fmp")
    ).strip().casefold()
    warn_relative = policy.get("disagreement_warn_relative", {})
    difference_floor = policy.get("relative_difference_floor", {})
    active_period_grace_days = int(policy.get("active_period_grace_days", 90))
    minimum_analyst_count = int(policy.get("minimum_analyst_count_per_provider", 2))
    maximum_pair_fetch_skew_minutes = float(
        policy.get("maximum_pair_fetch_skew_minutes", 360.0)
    )
    require_verified_currency = bool(
        policy.get("require_verified_currency_for_downside_guard", True)
    )
    boundary_policy = policy.get("boundary_policy", {})
    if not isinstance(boundary_policy, dict):
        raise ValueError("provider_reconciliation.boundary_policy must be a mapping")
    if (
        boundary_policy.get("mode") != "symmetric_provider_ranges_then_empirical"
        or boundary_policy.get("provider_ranges_role")
        != "diagnostic_until_calibrated"
        or boundary_policy.get("require_two_complete_ranges_for_cross_provider_band")
        is not True
    ):
        raise ValueError("Unsupported or fail-open provider boundary policy")
    require_range_overlap = bool(
        boundary_policy.get("require_range_overlap_for_action_boundary", True)
    )
    if active_period_grace_days < 0:
        raise ValueError("active_period_grace_days must be non-negative")
    if set(warn_relative) != {"eps", "revenue"} or set(difference_floor) != {
        "eps",
        "revenue",
    }:
        raise ValueError(
            "Provider reconciliation thresholds must cover eps and revenue exactly"
        )

    db_path = ensure_not_prod_path(
        args.db.resolve()
        if args.db
        else resolve_path(
            monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="expectations monitor database",
    )
    timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        runs = conn.execute(
            "SELECT provider,status,completed_at_utc FROM provider_snapshot_runs "
            "WHERE retrieval_cycle=? ORDER BY provider",
            (args.retrieval_cycle,),
        ).fetchall()
        if {str(row["provider"]): str(row["status"]) for row in runs} != {
            "alpha_vantage": "PASS",
            "fmp": "PASS",
        }:
            raise ValueError(
                "Reconciliation requires exact-cycle PASS runs from both providers"
            )
        raw_rows = conn.execute(
            "SELECT * FROM provider_estimate_snapshots WHERE retrieval_cycle=? "
            "ORDER BY provider,ticker,fiscal_period_end,estimate_type",
            (args.retrieval_cycle,),
        ).fetchall()
        universe_rows = conn.execute(
            "SELECT ticker,sector,source_pipeline FROM monitor_universe "
            "WHERE run_as_of=? ORDER BY ticker",
            (args.universe_as_of.isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    completed_at = max(str(row["completed_at_utc"]) for row in runs)
    if not universe_rows:
        raise ValueError(
            f"No sealed monitor universe in SQLite for {args.universe_as_of}"
        )
    universe = {
        str(row["ticker"]): {
            "sector": str(row["sector"]),
            "source_pipeline": str(row["source_pipeline"]),
        }
        for row in universe_rows
    }
    output, summary = reconcile(
        list(raw_rows),
        universe=universe,
        retrieval_cycle=args.retrieval_cycle,
        universe_as_of=args.universe_as_of.isoformat(),
        minimum_fiscal_period_end=args.as_of - timedelta(days=active_period_grace_days),
        tie_break_provider=tie_break,
        warn_relative={key: float(value) for key, value in warn_relative.items()},
        difference_floor={key: float(value) for key, value in difference_floor.items()},
        minimum_analyst_count=minimum_analyst_count,
        maximum_pair_fetch_skew_minutes=maximum_pair_fetch_skew_minutes,
        require_verified_currency=require_verified_currency,
        require_range_overlap_for_action_boundary=require_range_overlap,
    )

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / "provider_reconciliation" / args.retrieval_cycle
    )
    decisions_path = output_dir / "provider_estimate_reconciliation.csv"
    summary_path = output_dir / "provider_estimate_reconciliation_summary.json"
    manifest_path = output_dir / "provider_estimate_reconciliation_manifest.json"
    fail_if_exists([decisions_path, summary_path, manifest_path], force=args.force)
    write_csv(decisions_path, OUTPUT_FIELDS, output)
    source_snapshot_digest = _source_digest(list(raw_rows))
    universe_digest = _universe_digest(list(universe_rows))
    write_manifest(
        summary_path,
        {
            "schema_version": "provider_estimate_reconciliation_summary_v2",
            "acceptance": summary["acceptance"],
            "as_of_date": args.as_of.isoformat(),
            "universe_as_of": args.universe_as_of.isoformat(),
            "retrieval_cycle": args.retrieval_cycle,
            "generated_at_utc": completed_at,
            "policy_version": policy["policy_version"],
            "selection_mode": policy["selection_mode"],
            "central_estimate_policy": policy["central_estimate_policy"],
            "boundary_policy": boundary_policy,
            "no_averaging": True,
            "accuracy_status": "pending_realized_outcomes",
            "row_digest": _digest(output),
            "source_snapshot_count": len(raw_rows),
            "source_snapshot_digest": source_snapshot_digest,
            "universe_row_count": len(universe_rows),
            "universe_digest": universe_digest,
            **summary,
        },
    )
    inputs = [
        config_path,
        Path(__file__).resolve(),
        Path(__file__).with_name("estimate_policy.py"),
        Path(__file__).with_name("monitor_common.py"),
    ]
    write_manifest(
        manifest_path,
        {
            "schema_version": "provider_estimate_reconciliation_manifest_v2",
            "acceptance": summary["acceptance"],
            "as_of_date": args.as_of.isoformat(),
            "retrieval_cycle": args.retrieval_cycle,
            "universe_as_of": args.universe_as_of.isoformat(),
            "shadow_only": True,
            "no_averaging": True,
            "central_estimate_assigned": False,
            "symmetric_boundaries_shadow_only": True,
            "source_snapshot_count": len(raw_rows),
            "source_snapshot_digest": source_snapshot_digest,
            "universe_row_count": len(universe_rows),
            "universe_digest": universe_digest,
            "inputs_sha256": {str(path): sha256_file(path) for path in inputs},
            "outputs_sha256": {
                decisions_path.name: sha256_file(decisions_path),
                summary_path.name: sha256_file(summary_path),
            },
        },
    )

    snapshot_ids = [
        str(row[field])
        for row in output
        for field in ("alpha_snapshot_id", "fmp_snapshot_id")
        if str(row[field])
    ]
    lock_path = db_path.with_suffix(db_path.suffix + ".writer.lock")
    with writer_lock(lock_path, timeout_sec=timeout):
        conn = connect_monitor_db(db_path, timeout_sec=timeout)
        try:
            for path in (decisions_path, summary_path, manifest_path):
                artifact_sha256 = sha256_file(path)
                supersede_artifact_dependencies(
                    conn,
                    artifact_path=str(path),
                    current_artifact_sha256=artifact_sha256,
                )
                record_snapshot_dependencies(
                    conn,
                    artifact_path=str(path),
                    artifact_sha256=artifact_sha256,
                    snapshot_ids=snapshot_ids,
                )
                errors = artifact_snapshot_dependency_errors(
                    conn,
                    artifact_path=str(path),
                    artifact_sha256=artifact_sha256,
                )
                valid_count = conn.execute(
                    "SELECT COUNT(*) FROM provider_snapshot_dependencies "
                    "WHERE artifact_path=? AND artifact_sha256=? AND status='valid'",
                    (str(path), artifact_sha256),
                ).fetchone()[0]
                stale_valid_count = conn.execute(
                    "SELECT COUNT(*) FROM provider_snapshot_dependencies "
                    "WHERE artifact_path=? AND artifact_sha256<>? AND status='valid'",
                    (str(path), artifact_sha256),
                ).fetchone()[0]
                if errors or int(valid_count) != len(set(snapshot_ids)) or stale_valid_count:
                    raise RuntimeError(
                        f"Invalid reconciliation-artifact snapshot lineage for {path}: "
                        f"errors={errors}; valid={valid_count}; "
                        f"expected={len(set(snapshot_ids))}; stale_valid={stale_valid_count}"
                    )
        finally:
            conn.close()
    print(f"PROVIDER ESTIMATE RECONCILIATION: {summary['acceptance']}")
    print(
        f"rows={summary['row_count']}; comparable={summary['comparable_count']}; "
        f"disagreements={summary['disagreement_count']}; "
        f"candidates={summary['conservative_candidate_count']}; "
        f"downside_eligible={summary['downside_guard_eligible_count']}; "
        f"boundary_eligible={summary['symmetric_action_boundary_eligible_count']}"
    )
    return 0 if summary["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
