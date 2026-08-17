#!/usr/bin/env python3
"""Audit the five v4 surface-freight re-entry candidates without reparsing.

The audit is deliberately outcome-blind and read-only.  It consumes the
already-built market, financial, metric-availability, positioning, and SEC
fact stores.  A candidate can pass only when its current score inputs, two
distinct financial periods, financial-policy status, positioning inputs, and
canonical-fact integrity all pass.  Specialized disclosure depth is reported
for the later group-level domain audit but is not misapplied as an individual
issuer gate.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.canonical_fact_overrides import (  # noqa: E402
    CanonicalConceptOverride,
    load_canonical_concept_overrides,
)
from industrials.core.policy_loader import load_eligibility_policy, resolve_policy  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    load_investable_universe_policy,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


MODEL_FAMILY = "transportation"
CANDIDATES = ("CVLG", "FWRD", "HTLD", "MRTN", "WERN")
OBSERVED_STATUSES = frozenset({"REPORTED", "DERIVED", "PROXY"})
OPTIONAL_SOLVENCY_METRICS = (
    "net_debt_to_ebitda",
    "interest_coverage",
    "fcf_conversion",
)
POSITIONING_FIELDS = (
    "insider_net_value_90d",
    "insider_cluster_buyers_90d",
    "institutional_ownership_delta_pct",
    "short_interest_change_3m",
)
OUTPUT_FIELDS = (
    "asof_date",
    "ticker",
    "overall_status",
    "active_membership_gate",
    "market_gate",
    "market_data_quality",
    "latest_bar_date",
    "avg_dollar_volume_60d",
    "financial_policy_gate",
    "reporting_profile",
    "financial_confidence",
    "financial_data_quality_status",
    "current_required_metric_gate",
    "missing_current_required_metrics",
    "required_financial_history_gate",
    "complete_required_financial_period_count",
    "complete_required_financial_periods",
    "solvency_evidence_gate",
    "unresolved_solvency_metrics",
    "positioning_gate",
    "positioning_observed_count",
    "positioning_applicable_count",
    "annual_revenue_integrity_gate",
    "annual_revenue_period_end",
    "annual_revenue_candidate_count",
    "annual_revenue_value_spread",
    "annual_revenue_resolution",
    "annual_revenue_selected_concept",
    "annual_revenue_selected_value",
    "specialized_accepted_metrics",
    "specialized_accepted_periods_json",
    "blockers",
    "next_action",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the bounded, no-network surface-freight re-entry gate for "
            "CVLG, FWRD, HTLD, MRTN, and WERN."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _latest(
    connection: sqlite3.Connection,
    *,
    table: str,
    ticker: str,
    asof: str,
) -> dict[str, Any]:
    if table not in {"feature_market_technical", "feature_financial_statement"}:
        raise ValueError(f"unsupported feature table={table}")
    row = connection.execute(
        f"""
        SELECT * FROM {table}
        WHERE model_family=? AND ticker=? AND asof_date<=?
        ORDER BY asof_date DESC, source_id ASC LIMIT 1
        """,
        (MODEL_FAMILY, ticker, asof),
    ).fetchone()
    return dict(row) if row is not None else {}


def _metric_rows(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    asof: str,
) -> dict[str, dict[str, Any]]:
    return {
        str(row["metric_name"]): dict(row)
        for row in connection.execute(
            """
            SELECT * FROM feature_financial_metric_availability
            WHERE model_family=? AND ticker=? AND asof_date=?
            ORDER BY metric_name
            """,
            (MODEL_FAMILY, ticker, asof),
        ).fetchall()
    }


def _observed(row: Mapping[str, Any] | None) -> bool:
    return bool(
        row
        and str(row.get("availability_status") or "") in OBSERVED_STATUSES
        and _number(row.get("metric_value")) is not None
    )


def _interest_coverage_resolved(row: Mapping[str, Any] | None) -> bool:
    if _observed(row):
        return True
    return bool(
        row
        and str(row.get("availability_status") or "") == "NOT_APPLICABLE"
        and str(row.get("status_reason") or "")
        == "issuer_has_explicit_zero_debt_and_no_interest_expense"
    )


def decide_candidate(gates: Mapping[str, bool]) -> tuple[str, tuple[str, ...]]:
    blockers = tuple(sorted(name for name, passed in gates.items() if not passed))
    return ("PASS", ()) if not blockers else ("BLOCKED", blockers)


def _annual_revenue_integrity(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    asof: str,
    canonical_overrides: Mapping[
        tuple[str, str], CanonicalConceptOverride
    ],
) -> dict[str, object]:
    latest = connection.execute(
        """
        SELECT r.period_end, r.accession_number, r.filing_date
        FROM fact_sec_xbrl_fact_raw AS r
        JOIN dim_xbrl_concept_map AS m
          ON m.taxonomy=r.taxonomy AND m.concept_name=r.concept_name
         AND m.active_flag=1 AND m.canonical_metric='revenue'
        WHERE r.ticker=? AND r.filing_date<=? AND r.fiscal_period='FY'
          AND r.raw_value>0
        ORDER BY r.period_end DESC, r.filing_date DESC,
                 r.accession_number DESC
        LIMIT 1
        """,
        (ticker, asof),
    ).fetchone()
    if latest is None:
        return {
            "passed": False,
            "period_end": "",
            "candidate_count": 0,
            "spread": None,
            "resolution": "missing_annual_revenue_candidates",
            "selected_concept": "",
            "selected_value": None,
        }
    values = [
        float(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT r.raw_value
            FROM fact_sec_xbrl_fact_raw AS r
            JOIN dim_xbrl_concept_map AS m
              ON m.taxonomy=r.taxonomy AND m.concept_name=r.concept_name
             AND m.active_flag=1 AND m.canonical_metric='revenue'
            WHERE r.ticker=? AND r.period_end=? AND r.accession_number=?
              AND r.filing_date<=? AND r.fiscal_period='FY'
              AND r.raw_value>0
            ORDER BY r.raw_value
            """,
            (
                ticker,
                str(latest["period_end"]),
                str(latest["accession_number"]),
                asof,
            ),
        ).fetchall()
    ]
    spread = max(values) / min(values) if values else None
    canonical = connection.execute(
        """
        SELECT taxonomy, concept_name, value, canonical_quality
        FROM fact_financial_statement_canonical
        WHERE ticker=? AND model_family=? AND canonical_metric='revenue'
          AND period_end=? AND accession_number=?
        ORDER BY source_priority ASC, concept_name ASC
        LIMIT 1
        """,
        (
            ticker,
            MODEL_FAMILY,
            str(latest["period_end"]),
            str(latest["accession_number"]),
        ),
    ).fetchone()
    override = canonical_overrides.get((ticker, "revenue"))
    selected_value = _number(canonical["value"]) if canonical is not None else None
    reviewed_resolution = bool(
        canonical is not None
        and override is not None
        and override.matches(
            taxonomy=str(canonical["taxonomy"] or ""),
            concept_name=str(canonical["concept_name"] or ""),
        )
        and str(canonical["canonical_quality"] or "")
        == "mapped_xbrl_preferred_concept"
        and selected_value in values
    )
    # A greater-than-2x conflict in one annual accession is a deterministic
    # review trigger: the shared priority resolver may have selected a segment
    # or subset fact as consolidated revenue.  This is not an economic screen.
    return {
        "passed": bool(
            values
            and spread is not None
            and (spread <= 2.0 or reviewed_resolution)
        ),
        "period_end": str(latest["period_end"] or ""),
        "candidate_count": len(values),
        "spread": spread,
        "resolution": (
            "reviewed_preferred_consolidated_concept"
            if reviewed_resolution
            else "unambiguous_raw_candidates"
            if spread is not None and spread <= 2.0
            else "unresolved_raw_candidate_conflict"
        ),
        "selected_concept": str(canonical["concept_name"] or "") if canonical else "",
        "selected_value": selected_value,
    }


def _required_history(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    metric_ids: tuple[str, ...],
    asof: str,
) -> tuple[str, ...]:
    periods_by_metric: dict[str, set[str]] = {}
    for metric_id in metric_ids:
        periods_by_metric[metric_id] = {
            str(row[0])[:10]
            for row in connection.execute(
                """
                SELECT DISTINCT period_end
                FROM feature_financial_metric_availability
                WHERE model_family=? AND ticker=? AND metric_name=?
                  AND asof_date<=?
                  AND availability_status IN ('REPORTED','DERIVED','PROXY')
                  AND COALESCE(period_end, '')<>''
                """,
                (MODEL_FAMILY, ticker, metric_id, asof),
            ).fetchall()
        }
    if not periods_by_metric:
        return ()
    return tuple(sorted(set.intersection(*periods_by_metric.values())))


def _specialized_depth(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    asof: str,
) -> dict[str, int]:
    return {
        str(row["metric_name"]): int(row["period_count"])
        for row in connection.execute(
            """
            SELECT metric_name, COUNT(DISTINCT period_end) AS period_count
            FROM fact_sec_metric_disclosure_candidate
            WHERE model_family=? AND ticker=? AND filing_date<=?
              AND candidate_status='ACCEPTED'
              AND COALESCE(period_end, '')<>''
            GROUP BY metric_name ORDER BY metric_name
            """,
            (MODEL_FAMILY, ticker, asof),
        ).fetchall()
    }


def main() -> int:
    args = parse_args()
    asof = date.fromisoformat(str(args.asof)[:10]).isoformat()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    policy_path = resolve_path(
        family["universe"]["investable_universe_policy"],
        base_dir=base_dir,
    )
    policy = load_investable_universe_policy(policy_path)
    if policy.policy_version != "transportation_investable_universe_v4":
        raise ValueError("surface re-entry audit requires active v4 policy")
    selected = set(policy.selected_tickers)
    if selected.intersection(CANDIDATES):
        raise ValueError("re-entry candidates must remain outside active v4")

    registry_path = resolve_path(family["financial"]["metric_registry"], base_dir=base_dir)
    canonical_override_path = resolve_path(
        family["financial"]["canonical_concept_overrides_csv"],
        base_dir=base_dir,
    )
    canonical_overrides = load_canonical_concept_overrides(
        canonical_override_path,
        model_family=MODEL_FAMILY,
        asof=date.fromisoformat(asof),
    )
    _, definitions = load_metric_registry(registry_path)
    eligibility_path = resolve_path(
        cfg_get(config, "scoring_policy.families.transportation.eligibility_policy_csv"),
        base_dir=base_dir,
    )
    policies = load_eligibility_policy(eligibility_path, asof=asof)
    scoring = family["scoring"]
    min_adv = 5_000_000.0
    max_staleness = int(scoring["max_staleness_days"])
    positioning_source = str(scoring["positioning_feature_source_id"])
    min_positioning_fraction = float(scoring["minimum_positioning_input_coverage"])
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / "transportation"
        / "investable_v4"
        / "surface_reentry"
        / asof
    )
    output_csv = output_dir / "transportation_surface_reentry_audit.csv"
    output_json = output_dir / "transportation_surface_reentry_audit.json"

    rows: list[dict[str, object]] = []
    with _read_only(db_path) as connection:
        for ticker in CANDIDATES:
            member_row = connection.execute(
                """
                SELECT m.ticker, t.industry, t.calibration_cohort_id,
                       t.development_stage, t.calibration_use
                FROM dim_universe_membership AS m
                JOIN dim_industrials_taxonomy AS t
                  ON t.ticker=m.ticker AND t.model_family=m.model_family
                WHERE m.model_family=? AND m.membership_source_id=?
                  AND m.ticker=? AND m.membership_status='active'
                  AND m.start_date<=? AND COALESCE(m.end_date,'9999-12-31')>=?
                LIMIT 1
                """,
                (
                    MODEL_FAMILY,
                    str(family["universe"]["seed_source_id"]),
                    ticker,
                    asof,
                    asof,
                ),
            ).fetchone()
            member = dict(member_row) if member_row is not None else {}
            active_gate = bool(
                member
                and str(member.get("development_stage") or "") == "operating"
                and str(member.get("calibration_use") or "") == "core"
            )
            market = _latest(connection, table="feature_market_technical", ticker=ticker, asof=asof)
            latest_bar = str(market.get("latest_bar_date") or "")[:10]
            stale_days = (
                (date.fromisoformat(asof) - date.fromisoformat(latest_bar)).days
                if latest_bar
                else 999999
            )
            adv = _number(market.get("avg_dollar_volume_60d"))
            market_gate = bool(
                market
                and str(market.get("market_data_quality") or "") == "complete"
                and 0 <= stale_days <= max_staleness
                and adv is not None
                and adv >= min_adv
            )

            industry = str(member.get("industry") or "")
            cohort = str(member.get("calibration_cohort_id") or "")
            applicable_required = tuple(
                definition.metric_id
                for definition in definitions
                if definition.required_for_rank
                and definition.applies_to(cohort=cohort, industry=industry)
                and (not definition.birthdate or asof >= definition.birthdate)
            )
            required_financial = tuple(
                definition.metric_id
                for definition in definitions
                if definition.metric_id in applicable_required
                and definition.source in {"financial", "derived"}
            )
            metrics = _metric_rows(connection, ticker=ticker, asof=asof)
            missing_required = tuple(
                metric_id
                for metric_id in applicable_required
                if not _observed(metrics.get(metric_id))
            )
            current_required_gate = not missing_required
            complete_periods = _required_history(
                connection,
                ticker=ticker,
                metric_ids=required_financial,
                asof=asof,
            )
            history_gate = len(complete_periods) >= 2

            financial = _latest(
                connection,
                table="feature_financial_statement",
                ticker=ticker,
                asof=asof,
            )
            profile_name = str(financial.get("reporting_profile") or "NO_FINANCIALS_REVIEW")
            policy_row = resolve_policy(
                policies,
                profile_name,
                str(member.get("development_stage") or "operating"),
            )
            confidence = _number(financial.get("financial_confidence"))
            minimum_confidence = (
                _number(policy_row.get("minimum_financial_confidence"))
                if policy_row
                else None
            )
            financial_policy_gate = bool(
                policy_row
                and str(policy_row.get("rank_ready_policy") or "").startswith("eligible")
                and str(financial.get("data_quality_status") or "") == "complete"
                and confidence is not None
                and minimum_confidence is not None
                and confidence >= minimum_confidence
            )

            unresolved_solvency: list[str] = []
            negative_ebitda = int(financial.get("negative_ebitda_leverage_flag") or 0) == 1
            nonpositive_income = (
                _number(financial.get("net_income_ttm_usd")) is not None
                and float(financial["net_income_ttm_usd"]) <= 0.0
            )
            if not (_observed(metrics.get("net_debt_to_ebitda")) or negative_ebitda):
                unresolved_solvency.append("net_debt_to_ebitda")
            if not _interest_coverage_resolved(metrics.get("interest_coverage")):
                unresolved_solvency.append("interest_coverage")
            if not (_observed(metrics.get("fcf_conversion")) or nonpositive_income):
                unresolved_solvency.append("fcf_conversion")
            solvency_gate = not unresolved_solvency

            positioning_row = connection.execute(
                """
                SELECT * FROM feature_positioning
                WHERE model_family=? AND ticker=? AND asof_date=? AND source_id=?
                LIMIT 1
                """,
                (MODEL_FAMILY, ticker, asof, positioning_source),
            ).fetchone()
            positioning = dict(positioning_row) if positioning_row is not None else {}
            positioning_observed = sum(
                _number(positioning.get(field)) is not None
                for field in POSITIONING_FIELDS
            )
            positioning_applicable = len(POSITIONING_FIELDS)
            positioning_gate = bool(
                positioning
                and str(positioning.get("positioning_quality") or "") == "complete"
                and positioning_observed / positioning_applicable
                >= min_positioning_fraction
            )

            integrity = _annual_revenue_integrity(
                connection,
                ticker=ticker,
                asof=asof,
                canonical_overrides=canonical_overrides,
            )
            specialized = _specialized_depth(connection, ticker=ticker, asof=asof)
            gates = {
                "active_membership": active_gate,
                "annual_revenue_integrity": bool(integrity["passed"]),
                "current_required_metrics": current_required_gate,
                "financial_policy": financial_policy_gate,
                "market": market_gate,
                "positioning": positioning_gate,
                "required_financial_history": history_gate,
                "solvency_evidence": solvency_gate,
            }
            status, blockers = decide_candidate(gates)
            next_action = (
                "eligible_for_group_level_domain_audit"
                if status == "PASS"
                else "repair_loaded_fact_or_metric_gap_then_rerun_this_audit"
            )
            rows.append(
                {
                    "asof_date": asof,
                    "ticker": ticker,
                    "overall_status": status,
                    "active_membership_gate": "PASS" if active_gate else "FAIL",
                    "market_gate": "PASS" if market_gate else "FAIL",
                    "market_data_quality": str(market.get("market_data_quality") or ""),
                    "latest_bar_date": latest_bar,
                    "avg_dollar_volume_60d": adv if adv is not None else "",
                    "financial_policy_gate": "PASS" if financial_policy_gate else "FAIL",
                    "reporting_profile": profile_name,
                    "financial_confidence": confidence if confidence is not None else "",
                    "financial_data_quality_status": str(financial.get("data_quality_status") or ""),
                    "current_required_metric_gate": "PASS" if current_required_gate else "FAIL",
                    "missing_current_required_metrics": "|".join(missing_required),
                    "required_financial_history_gate": "PASS" if history_gate else "FAIL",
                    "complete_required_financial_period_count": len(complete_periods),
                    "complete_required_financial_periods": "|".join(complete_periods),
                    "solvency_evidence_gate": "PASS" if solvency_gate else "FAIL",
                    "unresolved_solvency_metrics": "|".join(unresolved_solvency),
                    "positioning_gate": "PASS" if positioning_gate else "FAIL",
                    "positioning_observed_count": positioning_observed,
                    "positioning_applicable_count": positioning_applicable,
                    "annual_revenue_integrity_gate": "PASS" if integrity["passed"] else "FAIL",
                    "annual_revenue_period_end": integrity["period_end"],
                    "annual_revenue_candidate_count": integrity["candidate_count"],
                    "annual_revenue_value_spread": (
                        round(float(integrity["spread"]), 8)
                        if integrity["spread"] is not None
                        else ""
                    ),
                    "annual_revenue_resolution": integrity["resolution"],
                    "annual_revenue_selected_concept": integrity["selected_concept"],
                    "annual_revenue_selected_value": (
                        integrity["selected_value"]
                        if integrity["selected_value"] is not None
                        else ""
                    ),
                    "specialized_accepted_metrics": len(specialized),
                    "specialized_accepted_periods_json": json.dumps(specialized, sort_keys=True),
                    "blockers": "|".join(blockers),
                    "next_action": next_action,
                }
            )

    write_csv_atomic(output_csv, OUTPUT_FIELDS, rows)
    passed = [str(row["ticker"]) for row in rows if row["overall_status"] == "PASS"]
    blocked = [str(row["ticker"]) for row in rows if row["overall_status"] == "BLOCKED"]
    manifest = {
        "acceptance": "PASS",
        "gate": "TRANSPORTATION_SURFACE_REENTRY_V1",
        "asof_date": asof,
        "candidate_tickers": list(CANDIDATES),
        "passed_tickers": passed,
        "blocked_tickers": blocked,
        "all_candidates_passed": not blocked,
        "universe_decision": (
            "ELIGIBLE_FOR_V5_GROUP_DOMAIN_AUDIT"
            if not blocked
            else "RETAIN_V4_AND_REPAIR_BLOCKED_CANDIDATES"
        ),
        "v5_activation_performed": False,
        "historical_reconstruction_performed": False,
        "calibration_performed": False,
        "parser_invocations": 0,
        "network_requests": 0,
        "database_writes": 0,
        "inputs": {
            "database_path": str(db_path),
            "investable_policy": {
                "path": str(policy_path),
                "sha256": file_sha256(policy_path),
            },
            "metric_registry": {
                "path": str(registry_path),
                "sha256": file_sha256(registry_path),
            },
            "eligibility_policy": {
                "path": str(eligibility_path),
                "sha256": file_sha256(eligibility_path),
            },
            "canonical_concept_overrides": {
                "path": str(canonical_override_path),
                "sha256": file_sha256(canonical_override_path),
            },
        },
        "output_csv": str(output_csv),
        "output_csv_sha256": file_sha256(output_csv),
        "next_gate": (
            "RUN_EXPANDED_SURFACE_DOMAIN_COVERAGE"
            if not blocked
            else "TARGETED_LOADED_FACT_REPAIR_ONLY"
        ),
    }
    write_text_atomic(output_json, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
