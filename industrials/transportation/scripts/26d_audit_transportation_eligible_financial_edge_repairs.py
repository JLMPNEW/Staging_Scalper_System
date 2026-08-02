#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.eligible_financial_edge_repair import (  # noqa: E402
    apply_metric_repairs,
    artifact_sha256,
    extract_chrw_interest_ttm_usd,
    extract_expd_current_debt_usd,
    recompute_surface_generic_scores,
    train_score_diagnostics,
)
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.surface_freight_research import (  # noqa: E402
    load_surface_freight_policy,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "review_policies"
    / "transportation_eligible_financial_edge_repairs_v1.yaml"
)
DEFAULT_SURFACE_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_surface_freight_research_policy.yaml"
)
DEFAULT_REGISTRY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_metric_registry.yaml"
)
DEFAULT_CURRENT_PANEL = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "dashboard"
    / "2026-07-30"
    / "transportation_stage11_survivorship_calibration_panel.csv"
)
DEFAULT_HISTORY_PANEL = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "research_redesign"
    / "surface_freight_v1"
    / "transportation_generic_oos_panel.csv"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "research_redesign"
    / "surface_freight_v1"
    / "eligible_financial_edge_repair_v1"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the three source-backed eligible-freight metric repairs and "
            "stop before a full rebuild unless a no-write materiality gate passes."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--surface-policy", type=Path, default=DEFAULT_SURFACE_POLICY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--current-panel", type=Path, default=DEFAULT_CURRENT_PANEL)
    parser.add_argument("--history-panel", type=Path, default=DEFAULT_HISTORY_PANEL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def _open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _policy_document(policy: Mapping[str, Any], key: str) -> tuple[Path, str]:
    payload = policy["source_documents"][key]
    return PROJECT_ROOT / str(payload["path"]), str(payload["sha256"])


def _latest_financial(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    asof: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM feature_financial_statement
        WHERE ticker = ? AND model_family = 'transportation' AND asof_date <= ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (ticker, asof),
    ).fetchone()
    if row is None:
        raise ValueError(f"missing transportation financial snapshot for {ticker}")
    if str(row["asof_date"]) != asof:
        raise ValueError(
            f"{ticker}: current financial snapshot is stale: {row['asof_date']} != {asof}"
        )
    return dict(row)


def _raw_duration_fact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    concept_name: str,
    period_start: str,
    period_end: str,
    asof: str,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT *
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND taxonomy = 'us-gaap'
          AND concept_name = ?
          AND period_start = ?
          AND period_end = ?
          AND COALESCE(
                NULLIF(SUBSTR(accepted_at, 1, 10), ''),
                filing_date,
                period_end
              ) <= ?
          AND raw_value IS NOT NULL
        ORDER BY COALESCE(NULLIF(SUBSTR(accepted_at, 1, 10), ''), filing_date) DESC,
                 accession_number DESC, raw_fact_id DESC
        """,
        (ticker, concept_name, period_start, period_end, asof),
    ).fetchall()
    if not rows:
        raise ValueError(
            f"missing raw fact {ticker}:{concept_name}:{period_start}:{period_end}"
        )
    latest_date = str(rows[0]["accepted_at"] or rows[0]["filing_date"] or "")[:10]
    latest = [row for row in rows if str(row["accepted_at"] or row["filing_date"] or "")[:10] == latest_date]
    values = {float(row["raw_value"]) for row in latest}
    if len(values) != 1:
        raise ValueError(
            f"conflicting latest raw facts {ticker}:{concept_name}:{period_end}={sorted(values)}"
        )
    payload = dict(latest[0])
    payload["raw_value"] = values.pop()
    return payload


def _number(row: Mapping[str, Any], field: str) -> float:
    value = row.get(field)
    if value is None or not math.isfinite(float(value)):
        raise ValueError(f"missing/nonfinite operand {row.get('ticker')}:{field}")
    return float(value)


def _ranked(rows: list[dict[str, object]]) -> tuple[dict[str, int], set[str]]:
    ordered = sorted(
        rows,
        key=lambda row: (
            -float(row["recomputed_generic_score"]),
            str(row.get("ticker") or ""),
        ),
    )
    ranks = {str(row["ticker"]): index + 1 for index, row in enumerate(ordered)}
    top_count = max(1, math.ceil(len(ordered) * 0.20))
    return ranks, {str(row["ticker"]) for row in ordered[:top_count]}


def _delta(left: object, right: object) -> float | None:
    if left is None or right is None:
        return None
    return float(right) - float(left)


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    policy_path = args.policy.expanduser().resolve()
    surface_policy_path = args.surface_policy.expanduser().resolve()
    registry_path = args.registry.expanduser().resolve()
    current_panel_path = args.current_panel.expanduser().resolve()
    history_panel_path = args.history_panel.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    outputs = {
        "repairs": output_dir / "transportation_eligible_financial_repairs.csv",
        "blast_radius": output_dir / "transportation_shared_alias_blast_radius.csv",
        "current": output_dir / "transportation_current_score_materiality.csv",
        "train": output_dir / "transportation_train_optimistic_stress.json",
        "manifest": output_dir / "transportation_eligible_financial_edge_repair_manifest.json",
    }
    if not args.allow_overwrite and any(path.exists() for path in outputs.values()):
        raise FileExistsError("edge-repair artifacts exist; use --allow-overwrite")
    for path in (policy_path, surface_policy_path, registry_path, current_panel_path, history_panel_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    policy = load_yaml(policy_path)
    if (
        policy.get("production_write_allowed") is not False
        or policy.get("promotion_evidence_allowed") is not False
        or policy.get("governance", {}).get("revealed_validation_or_holdout_used") is not False
    ):
        raise ValueError("edge-repair policy is not fail-closed")
    asof = str(policy["asof_date"])
    surface_policy = load_surface_freight_policy(surface_policy_path)
    _, definitions = load_metric_registry(registry_path)
    component_weights_raw = cfg_get(
        config,
        "model_families.transportation.scoring.component_weights",
    )
    if not isinstance(component_weights_raw, dict):
        raise ValueError("transportation component weights are missing")
    component_weights = {
        str(key): float(value) for key, value in component_weights_raw.items()
    }
    standards = cfg_get(config, "oos_calibration_standards.families.transportation")
    if not isinstance(standards, dict):
        raise ValueError("transportation OOS standards are missing")

    annual_path, annual_hash = _policy_document(policy, "chrw_2025_10k")
    chrw_q1_path, chrw_q1_hash = _policy_document(policy, "chrw_2026_q1")
    expd_q1_path, expd_q1_hash = _policy_document(policy, "expd_2026_q1")
    chrw_interest = extract_chrw_interest_ttm_usd(
        annual_path=annual_path,
        annual_sha256=annual_hash,
        quarter_path=chrw_q1_path,
        quarter_sha256=chrw_q1_hash,
    )
    expd_debt = extract_expd_current_debt_usd(
        quarter_path=expd_q1_path,
        quarter_sha256=expd_q1_hash,
    )

    with _open_readonly(db_path) as conn:
        financial = {
            ticker: _latest_financial(conn, ticker=ticker, asof=asof)
            for ticker in ("CHRW", "EXPD", "WERN")
        }
        wern_annual = _raw_duration_fact(
            conn,
            ticker="WERN",
            concept_name="OtherDepreciationAndAmortization",
            period_start="2025-01-01",
            period_end="2025-12-31",
            asof=asof,
        )
        wern_current = _raw_duration_fact(
            conn,
            ticker="WERN",
            concept_name="OtherDepreciationAndAmortization",
            period_start="2026-01-01",
            period_end="2026-03-31",
            asof=asof,
        )
        wern_prior = _raw_duration_fact(
            conn,
            ticker="WERN",
            concept_name="OtherDepreciationAndAmortization",
            period_start="2025-01-01",
            period_end="2025-03-31",
            asof=asof,
        )
        blast_rows: list[dict[str, object]] = []
        for alias in policy["shared_aliases"]:
            rows = conn.execute(
                """
                SELECT ticker, COUNT(*) AS raw_fact_count,
                       COUNT(DISTINCT period_end) AS distinct_period_count
                FROM fact_sec_xbrl_fact_raw
                WHERE taxonomy = ? AND concept_name = ?
                GROUP BY ticker
                ORDER BY ticker
                """,
                (alias["taxonomy"], alias["concept_name"]),
            ).fetchall()
            for row in rows:
                blast_rows.append(
                    {
                        "taxonomy": alias["taxonomy"],
                        "concept_name": alias["concept_name"],
                        "canonical_metric": alias["canonical_metric"],
                        "target_case": alias["target_case"],
                        "ticker": row["ticker"],
                        "raw_fact_count": row["raw_fact_count"],
                        "distinct_period_count": row["distinct_period_count"],
                    }
                )

    wern_da_ttm = (
        float(wern_annual["raw_value"])
        + float(wern_current["raw_value"])
        - float(wern_prior["raw_value"])
    )
    repairs = {
        ("CHRW", "interest_coverage"): (
            _number(financial["CHRW"], "operating_income_ttm_usd")
            / chrw_interest["interest_expense_ttm_usd"]
        ),
        ("EXPD", "net_debt_to_ebitda"): (
            (expd_debt - _number(financial["EXPD"], "cash_and_equivalents_usd"))
            / (
                _number(financial["EXPD"], "operating_income_ttm_usd")
                + _number(financial["EXPD"], "depreciation_and_amortization_ttm_usd")
            )
        ),
        ("WERN", "net_debt_to_ebitda"): (
            (
                _number(financial["WERN"], "total_debt_usd")
                - _number(financial["WERN"], "cash_and_equivalents_usd")
            )
            / (_number(financial["WERN"], "operating_income_ttm_usd") + wern_da_ttm)
        ),
    }
    repair_rows = [
        {
            "asof_date": asof,
            "ticker": "CHRW",
            "metric_id": "interest_coverage",
            "metric_value": repairs[("CHRW", "interest_coverage")],
            "status": "DERIVED",
            "source_method": "pinned_10k_plus_q1_interest_rollforward",
            "source_operands_json": json.dumps(chrw_interest, sort_keys=True),
            "production_write_allowed": 0,
        },
        {
            "asof_date": asof,
            "ticker": "EXPD",
            "metric_id": "net_debt_to_ebitda",
            "metric_value": repairs[("EXPD", "net_debt_to_ebitda")],
            "status": "DERIVED",
            "source_method": "pinned_q1_borrowings_and_existing_aligned_ttm_ebitda",
            "source_operands_json": json.dumps(
                {
                    "debt_usd": expd_debt,
                    "cash_usd": financial["EXPD"]["cash_and_equivalents_usd"],
                    "operating_income_ttm_usd": financial["EXPD"]["operating_income_ttm_usd"],
                    "depreciation_and_amortization_ttm_usd": financial["EXPD"]["depreciation_and_amortization_ttm_usd"],
                },
                sort_keys=True,
            ),
            "production_write_allowed": 0,
        },
        {
            "asof_date": asof,
            "ticker": "WERN",
            "metric_id": "net_debt_to_ebitda",
            "metric_value": repairs[("WERN", "net_debt_to_ebitda")],
            "status": "DERIVED",
            "source_method": "shared_other_da_alias_and_strict_ttm_rollforward",
            "source_operands_json": json.dumps(
                {
                    "debt_usd": financial["WERN"]["total_debt_usd"],
                    "cash_usd": financial["WERN"]["cash_and_equivalents_usd"],
                    "operating_income_ttm_usd": financial["WERN"]["operating_income_ttm_usd"],
                    "da_fy2025_usd": wern_annual["raw_value"],
                    "da_q1_2026_usd": wern_current["raw_value"],
                    "da_q1_2025_usd": wern_prior["raw_value"],
                    "da_ttm_usd": wern_da_ttm,
                },
                sort_keys=True,
            ),
            "production_write_allowed": 0,
        },
        {
            "asof_date": asof,
            "ticker": "EXPD",
            "metric_id": "interest_coverage",
            "metric_value": "",
            "status": "NOT_DISCLOSED",
            "source_method": "no_valid_current_standalone_interest_expense",
            "source_operands_json": "{}",
            "production_write_allowed": 0,
        },
    ]

    current_eligible = {
        str(ticker).upper() for ticker in policy["current_eligible_tickers"]
    }
    if len(current_eligible) != 24:
        raise ValueError("edge-repair policy must pin exactly 24 current eligible tickers")
    current_rows = [
        row
        for row in read_rows(current_panel_path)
        if str(row.get("ticker") or "").upper() in current_eligible
        and str(row.get("rank_ready_flag") or "") == "1"
        and str(row.get("research_calibration_input_eligible_flag") or "") == "1"
    ]
    if {str(row["ticker"]).upper() for row in current_rows} != current_eligible:
        missing = sorted(
            current_eligible - {str(row["ticker"]).upper() for row in current_rows}
        )
        raise ValueError(f"pinned current eligible tickers are not rank ready: {missing}")
    baseline_current = recompute_surface_generic_scores(
        current_rows,
        definitions=definitions,
        policy=surface_policy,
        component_weights=component_weights,
    )
    repaired_current = recompute_surface_generic_scores(
        apply_metric_repairs(current_rows, repairs),
        definitions=definitions,
        policy=surface_policy,
        component_weights=component_weights,
    )
    if len(baseline_current) < int(surface_policy["minimum_active_cohort_size"]):
        raise ValueError("surface-freight current cohort is below its governed minimum")
    baseline_by_ticker = {str(row["ticker"]): row for row in baseline_current}
    repaired_by_ticker = {str(row["ticker"]): row for row in repaired_current}
    baseline_ranks, baseline_top = _ranked(baseline_current)
    repaired_ranks, repaired_top = _ranked(repaired_current)
    current_materiality_rows: list[dict[str, object]] = []
    for ticker in sorted(baseline_by_ticker):
        baseline_score = baseline_by_ticker[ticker]["recomputed_generic_score"]
        repaired_score = repaired_by_ticker[ticker]["recomputed_generic_score"]
        current_materiality_rows.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "baseline_score": baseline_score,
                "repaired_score": repaired_score,
                "score_delta": _delta(baseline_score, repaired_score),
                "baseline_rank": baseline_ranks[ticker],
                "repaired_rank": repaired_ranks[ticker],
                "rank_change": baseline_ranks[ticker] - repaired_ranks[ticker],
                "applied_metric_repairs": ";".join(
                    metric for repair_ticker, metric in repairs if repair_ticker == ticker
                ),
            }
        )

    history_rows = [
        row for row in read_rows(history_panel_path) if row.get("horizon_sessions") == "63"
    ]
    baseline_history = recompute_surface_generic_scores(
        history_rows,
        definitions=definitions,
        policy=surface_policy,
        component_weights=component_weights,
    )
    # Deliberately unrealistic favorable extremes form an upper-bound screen.
    # They are never written as facts and only touch missing train-split cells.
    optimistic_repairs = {
        ("CHRW", "interest_coverage"): 1.0e12,
        ("EXPD", "net_debt_to_ebitda"): -1.0e12,
        ("WERN", "net_debt_to_ebitda"): -1.0e12,
    }
    optimistic_history = recompute_surface_generic_scores(
        apply_metric_repairs(history_rows, optimistic_repairs),
        definitions=definitions,
        policy=surface_policy,
        component_weights=component_weights,
    )
    minimum_cross_section = int(standards["minimum_cross_section"])
    top_fraction = float(standards["top_fraction"])
    baseline_train = train_score_diagnostics(
        baseline_history,
        minimum_cross_section=minimum_cross_section,
        top_fraction=top_fraction,
    )
    optimistic_train = train_score_diagnostics(
        optimistic_history,
        minimum_cross_section=minimum_cross_section,
        top_fraction=top_fraction,
    )
    train_payload = {
        "evidence_posture": "train_only_optimistic_upper_bound_not_data",
        "baseline": baseline_train,
        "optimistic_upper_bound": optimistic_train,
        "delta": {
            key: _delta(baseline_train.get(key), optimistic_train.get(key))
            for key in ("mean_ic", "mean_top_excess", "mean_bottom_excess", "mean_top_bottom_spread")
        },
    }

    gates = policy["materiality_gates"]
    max_abs_score_delta = max(abs(float(row["score_delta"])) for row in current_materiality_rows)
    max_abs_rank_change = max(abs(int(row["rank_change"])) for row in current_materiality_rows)
    top_membership_changes = len(baseline_top.symmetric_difference(repaired_top))
    current_material = (
        max_abs_score_delta >= float(gates["minimum_current_max_abs_score_delta"])
        or max_abs_rank_change >= int(gates["minimum_current_max_abs_rank_change"])
        or top_membership_changes >= int(gates["minimum_current_top_quintile_membership_changes"])
    )
    ic_improvement = train_payload["delta"]["mean_ic"]
    spread_improvement = train_payload["delta"]["mean_top_bottom_spread"]
    optimistic_improvement = (
        ic_improvement is not None
        and ic_improvement >= float(gates["minimum_optimistic_train_ic_improvement"])
    ) or (
        spread_improvement is not None
        and spread_improvement >= float(gates["minimum_optimistic_train_spread_improvement"])
    )
    bounded_rebuild_authorized = bool(current_material and optimistic_improvement)
    decision = (
        "AUTHORIZE_BOUNDED_3_TICKER_SHADOW_REBUILD"
        if bounded_rebuild_authorized
        else "STOP_NO_FULL_24_NAME_REBUILD_OR_RECALIBRATION"
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(outputs["repairs"], list(repair_rows[0]), repair_rows)
    write_csv_atomic(
        outputs["blast_radius"],
        list(blast_rows[0]) if blast_rows else ["taxonomy", "concept_name", "ticker"],
        blast_rows,
    )
    write_csv_atomic(outputs["current"], list(current_materiality_rows[0]), current_materiality_rows)
    write_text_atomic(outputs["train"], json.dumps(train_payload, indent=2, sort_keys=True) + "\n")
    manifest = {
        "policy_version": policy["policy_version"],
        "asof_date": asof,
        "acceptance": "PASS",
        "decision": decision,
        "bounded_rebuild_authorized": bounded_rebuild_authorized,
        "full_24_name_rebuild_authorized": False,
        "recalibration_authorized": False,
        "promotion_authorized": False,
        "surface_freight_member_count": len(baseline_current),
        "source_backed_repair_count": len(repairs),
        "unresolved_case_count": len(policy["unresolved_cases"]),
        "current_materiality": {
            "max_abs_score_delta": max_abs_score_delta,
            "max_abs_rank_change": max_abs_rank_change,
            "top_quintile_membership_changes": top_membership_changes,
            "gate_pass": current_material,
        },
        "optimistic_train_materiality": {
            "mean_ic_improvement": ic_improvement,
            "mean_top_bottom_spread_improvement": spread_improvement,
            "gate_pass": optimistic_improvement,
        },
        "governance": policy["governance"],
        "input_hashes": {
            "policy": artifact_sha256(policy_path),
            "surface_policy": artifact_sha256(surface_policy_path),
            "registry": artifact_sha256(registry_path),
            "current_panel": artifact_sha256(current_panel_path),
            "history_panel": artifact_sha256(history_panel_path),
            "chrw_2025_10k": artifact_sha256(annual_path),
            "chrw_2026_q1": artifact_sha256(chrw_q1_path),
            "expd_2026_q1": artifact_sha256(expd_q1_path),
        },
        "outputs": {key: str(path) for key, path in outputs.items() if key != "manifest"},
        "next_action": (
            "run_network_free_temp_db_rebuild_for_CHRW_EXPD_WERN_only"
            if bounded_rebuild_authorized
            else "retain_shared_alias_fixes_and_defer_full_rebuild_until_new_information"
        ),
    }
    write_text_atomic(outputs["manifest"], json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
