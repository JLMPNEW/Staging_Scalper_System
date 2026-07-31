#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.machinery.financial_contract import required_metric_names  # noqa: E402
from industrials.machinery.scoring import (  # noqa: E402
    parse_asof,
    validate_metric_availability_contract,
)
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


CALIBRATION_FINANCIAL_FIELDS = {
    "accession_number",
    "form_type",
    "fiscal_period_end",
    "fiscal_year",
    "fiscal_period",
    "reporting_standard",
    "reporting_profile",
    "financial_frequency",
    "reported_currency",
    "fx_conversion_status",
    "canonical_quality",
    "data_quality_status",
    "review_reason",
    "revenue_stub_annualized_usd",
    "revenue_stub_period_days",
    "revenue_stub_quality",
    "orders_ttm_usd",
    "funded_backlog_usd",
    "reported_backlog_usd",
    "remaining_performance_obligation_usd",
    "rpo_current_usd",
    "orders_yoy_growth",
    "book_to_bill",
    "backlog_yoy_growth",
    "backlog_to_revenue",
    "reported_backlog_yoy_growth",
    "reported_backlog_to_revenue",
    "rpo_yoy_growth",
    "rpo_to_revenue",
    "rpo_implied_orders_usd",
    "rpo_implied_book_to_bill",
    "roic",
    "roic_not_meaningful_flag",
    "asset_turnover",
    "incremental_operating_margin",
    "inventory_sales_growth_spread",
    "cash_conversion_cycle_change",
    "net_debt_to_ebitda",
    "negative_ebitda_leverage_flag",
    "interest_coverage",
    "cash_runway_years",
    "capital_raise_dependence",
    "diluted_shares_yoy_growth",
    "financial_metric_reported_count",
    "financial_metric_proxy_count",
    "financial_metric_unavailable_count",
    "financial_metric_classified_fraction",
    "financial_metric_availability_asof_date",
    *(f"{metric_name}_availability_status" for metric_name in required_metric_names()),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate machinery ingestion through the portfolio industrial adapter.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Accepted for orchestrator consistency.")
    parser.add_argument("--asof", required=True)
    parser.add_argument("--sector-output-root", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--expect-research-eligible", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--expect-production",
        action="store_true",
        help=(
            "Require the validated production sleeve instead of the default "
            "shadow-only contract."
        ),
    )
    mode.add_argument(
        "--expect-shadow",
        action="store_true",
        help="Require a non-investable shadow contract.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    errors: list[str] = []
    result = None
    try:
        result = run_adapter(
            {
                "model_family": "machinery",
                "adapter": "industrial_family",
                "file_mode": "dated",
                "file_path": "industrials/machinery/dashboard/{yyyy-mm-dd}/machinery_final_rank_table.csv",
                "sector": "Industrials",
                "industry": "Machinery",
                "industry_aggregate": "Machinery",
                "require_oos_score_valid": True,
            },
            args.sector_output_root.expanduser().resolve(),
            asof,
        )
    except (FileNotFoundError, ValueError) as exc:
        # First-ever runs against an empty dashboard should produce the
        # structured FAIL summary, not a traceback.
        errors.append(f"portfolio adapter failed: {type(exc).__name__}: {exc}")
    rank_path = (
        args.sector_output_root.expanduser().resolve()
        / f"industrials/machinery/dashboard/{asof}/machinery_final_rank_table.csv"
    )
    manifest_path = rank_path.with_name(
        "machinery_final_rank_table_manifest.json"
    )
    try:
        dashboard_manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        dashboard_manifest = {}
    production_expected = bool(
        args.expect_production
        or (
            not args.expect_shadow
            and dashboard_manifest.get("production_policy_active") is True
        )
    )
    if not rank_path.exists():
        errors.append(f"machinery final-rank file not found: {rank_path}")
        rank_fields: set[str] = set()
        rank_rows: list[dict[str, str]] = []
    else:
        with rank_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rank_fields = {str(field or "") for field in (reader.fieldnames or [])}
            rank_rows = [
                {str(key): str(value or "") for key, value in row.items() if key is not None}
                for row in reader
            ]
        missing_financial_fields = sorted(CALIBRATION_FINANCIAL_FIELDS - rank_fields)
        if missing_financial_fields:
            errors.append(
                "machinery final-rank file missing calibration financial fields: "
                + ",".join(missing_financial_fields)
            )
        else:
            errors.extend(validate_metric_availability_contract(rank_rows, asof=asof))
        if production_expected:
            selection_fields = {
                "portfolio_universe_eligible_flag",
                "portfolio_selection_policy",
                "portfolio_sleeve_selected_flag",
                "portfolio_sleeve_target_weight",
            }
            missing_selection_fields = sorted(
                selection_fields - rank_fields
            )
            if missing_selection_fields:
                errors.append(
                    "machinery production rank file missing selection fields: "
                    + ",".join(missing_selection_fields)
                )
    adapter_rows = result.rows if result is not None else []
    if result is not None:
        if not adapter_rows:
            errors.append("portfolio adapter returned no machinery rows")
        if result.source_asof_date != asof:
            errors.append(f"source_asof_date={result.source_asof_date} expected={asof}")
    research_eligible_tickers = {
        row.ticker
        for row in adapter_rows
        if row.calibration_research_eligible
    }
    research_eligible = len(research_eligible_tickers)
    investable = {
        row.ticker for row in adapter_rows if row.investable_eligible
    }
    if production_expected:
        selected = {
            str(row.get("ticker") or "").strip().upper()
            for row in rank_rows
            if str(row.get("portfolio_sleeve_selected_flag") or "") == "1"
        }
        broad_eligible = {
            str(row.get("ticker") or "").strip().upper()
            for row in rank_rows
            if str(row.get("portfolio_universe_eligible_flag") or "") == "1"
        }
        expected_research_eligible = {
            str(row.get("ticker") or "").strip().upper()
            for row in rank_rows
            if str(
                row.get("stage11_calibration_input_eligible_flag") or ""
            )
            == "1"
        }
        if not selected:
            errors.append("production machinery sleeve is empty")
        if investable != selected:
            errors.append(
                "production adapter membership does not match selected sleeve"
            )
        if research_eligible_tickers != expected_research_eligible:
            missing_research = sorted(
                expected_research_eligible - research_eligible_tickers
            )
            unexpected_research = sorted(
                research_eligible_tickers - expected_research_eligible
            )
            errors.append(
                "production adapter research population does not match the "
                "Stage 11 research contract "
                f"missing={missing_research} unexpected={unexpected_research}"
            )
        try:
            target_weights = [
                float(str(row.get("portfolio_sleeve_target_weight") or "0"))
                for row in rank_rows
                if str(row.get("portfolio_sleeve_selected_flag") or "") == "1"
            ]
        except ValueError:
            target_weights = []
            errors.append("production sleeve contains an invalid target weight")
        if target_weights and abs(sum(target_weights) - 1.0) > 1e-8:
            errors.append("production sleeve target weights do not sum to 1")
        if (
            target_weights
            and max(target_weights) - min(target_weights) > 1e-10
        ):
            errors.append("production machinery sleeve is not equal weighted")
    else:
        if investable:
            errors.append("shadow machinery rows must not be investable")
        if any(row.oos_score_valid_flag for row in adapter_rows):
            errors.append("shadow machinery rows must not be OOS valid")
        if args.expect_research_eligible and research_eligible == 0:
            errors.append(
                "expected survivorship-corrected research rows but adapter "
                "returned zero"
            )
        if not args.expect_research_eligible and research_eligible:
            errors.append(
                "live shadow dashboard unexpectedly exposed research "
                "calibration rows"
            )
    summary = {
        "acceptance": "PASS" if not errors else "FAIL",
        "adapter": result.adapter if result is not None else "",
        "source_pipeline": result.source_pipeline if result is not None else "",
        "source_asof_date": result.source_asof_date if result is not None else "",
        "rows": len(adapter_rows),
        "contract_mode": "production" if production_expected else "shadow",
        "investable_rows": len(investable),
        "research_eligible_rows": research_eligible,
        "broad_eligible_rows": len(broad_eligible)
        if production_expected
        else 0,
        "calibration_financial_field_count": len(CALIBRATION_FINANCIAL_FIELDS & rank_fields),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
