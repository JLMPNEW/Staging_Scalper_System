#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db, utc_now  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.financial_contract import (  # noqa: E402
    MetricDefinition,
    load_metric_registry,
    registry_summary,
)
from industrials.transportation.disclosure_candidates import EXTRACTION_METHOD  # noqa: E402
from industrials.transportation.reviewed_annual_metrics import (  # noqa: E402
    AnnualMetricResolution,
    load_reviewed_annual_resolutions,
    load_scope_pairs,
)
from industrials.transportation.reviewed_operand_repair import (  # noqa: E402
    SOURCE_ID as REVIEWED_OPERAND_SOURCE_ID,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


FIELDS = [
    "ticker",
    "asof_date",
    "model_family",
    "calibration_cohort",
    "industry",
    "metric_name",
    "component",
    "availability_status",
    "metric_value",
    "unit",
    "source_id",
    "accession_number",
    "filing_date",
    "period_start",
    "period_end",
    "taxonomy",
    "concept_name",
    "extraction_method",
    "confidence",
    "status_reason",
    "provenance_json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cohort-aware transportation metric availability.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument(
        "--include-historical",
        action="store_true",
        help="Build the point-in-time active-plus-delisted membership at --asof.",
    )
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def parse_date(value: object) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def number(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def latest_asof(conn: Any) -> str:
    row = conn.execute(
        """
        SELECT MAX(asof_date) FROM (
            SELECT asof_date FROM feature_market_technical WHERE model_family = ?
            UNION ALL
            SELECT asof_date FROM feature_financial_statement WHERE model_family = ?
        )
        """,
        (MODEL_FAMILY, MODEL_FAMILY),
    ).fetchone()
    return str(row[0] or "") if row else ""


def load_members(
    conn: Any,
    *,
    asof: str,
    active_source_id: str,
    include_historical: bool = False,
) -> list[dict[str, Any]]:
    if include_historical:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT m.ticker, c.company_name, t.industry,
                       t.calibration_cohort_id, t.calibration_cohort,
                       t.development_stage
                FROM dim_universe_membership AS m
                JOIN dim_company AS c ON c.company_id = m.company_id
                JOIN dim_industrials_taxonomy AS t
                  ON t.ticker = m.ticker AND t.model_family = m.model_family
                WHERE m.model_family = ?
                  AND m.start_date <= ?
                  AND COALESCE(m.end_date, '9999-12-31') >= ?
                GROUP BY m.ticker
                ORDER BY m.ticker
                """,
                (MODEL_FAMILY, asof, asof),
            ).fetchall()
        ]
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT m.ticker, c.company_name, t.industry, t.calibration_cohort_id,
                   t.calibration_cohort, t.development_stage
            FROM dim_universe_membership AS m
            JOIN dim_company AS c ON c.company_id = m.company_id
            JOIN dim_industrials_taxonomy AS t
              ON t.ticker = m.ticker AND t.model_family = m.model_family
            WHERE m.model_family = ?
              AND m.membership_source_id = ?
              AND m.start_date <= ?
              AND COALESCE(m.end_date, '9999-12-31') >= ?
              AND m.membership_status = 'active'
            ORDER BY m.ticker
            """,
            (MODEL_FAMILY, active_source_id, asof, asof),
        ).fetchall()
    ]


def latest_row(conn: Any, *, table: str, ticker: str, asof: str) -> dict[str, Any]:
    if table not in {"feature_market_technical", "feature_financial_statement"}:
        raise ValueError(f"Unsupported feature table={table}")
    row = conn.execute(
        f"""
        SELECT * FROM {table}
        WHERE ticker = ? AND model_family = ? AND asof_date <= ?
        ORDER BY asof_date DESC, source_id ASC LIMIT 1
        """,
        (ticker, MODEL_FAMILY, asof),
    ).fetchone()
    return dict(row) if row is not None else {}


def load_reviewed_metric_overrides(
    conn: Any,
    *,
    asof: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    exists = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table'
          AND name='sec_parser_production_metric_override'
        """
    ).fetchone()
    if exists is None:
        return {}
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT ticker, metric_name, availability_status, status_reason,
               evidence_key, valid_from
        FROM sec_parser_production_metric_override
        WHERE model_family=? AND active=1
          AND valid_from<=?
          AND COALESCE(valid_to, '9999-12-31')>=?
        ORDER BY ticker, metric_name, valid_from, evidence_key
        """,
        (MODEL_FAMILY, asof, asof),
    ).fetchall():
        output[(str(row["ticker"]), str(row["metric_name"]))] = dict(row)
    return output


def candidate_row(
    conn: Any,
    *,
    ticker: str,
    metric: str,
    asof: str,
    max_staleness_days: int,
) -> dict[str, Any]:
    staleness_modifier = f"-{max_staleness_days} days"
    row = conn.execute(
        """
        SELECT * FROM fact_sec_metric_disclosure_candidate
        WHERE ticker = ? AND model_family = ? AND metric_name = ?
          AND extraction_method = ?
          AND COALESCE(NULLIF(filing_date, ''), NULLIF(SUBSTR(accepted_at, 1, 10), '')) <= ?
          AND COALESCE(NULLIF(filing_date, ''), NULLIF(SUBSTR(accepted_at, 1, 10), ''))
              >= DATE(?, ?)
        ORDER BY
          COALESCE(NULLIF(filing_date, ''), NULLIF(SUBSTR(accepted_at, 1, 10), '')) DESC,
          CASE UPPER(candidate_status) WHEN 'ACCEPTED' THEN 0 WHEN 'PARSER_FAILURE' THEN 1 ELSE 2 END,
          confidence DESC,
          candidate_key ASC
        LIMIT 1
        """,
        (
            ticker,
            MODEL_FAMILY,
            metric,
            EXTRACTION_METHOD,
            asof,
            asof,
            staleness_modifier,
        ),
    ).fetchone()
    return dict(row) if row is not None else {}


def derived_value(metric: MetricDefinition, financial: dict[str, Any]) -> float | None:
    if metric.formula == "abs(capex_ttm_usd)/revenue_ttm_usd":
        capex = number(financial.get("capex_ttm_usd"))
        revenue = number(financial.get("revenue_ttm_usd"))
        return abs(capex) / revenue if capex is not None and revenue is not None and revenue != 0.0 else None
    raise ValueError(f"Unsupported transportation metric formula={metric.formula!r}")


def classify_metric(
    conn: Any,
    *,
    metric: MetricDefinition,
    member: dict[str, Any],
    market: dict[str, Any],
    financial: dict[str, Any],
    reviewed_overrides: dict[tuple[str, str], dict[str, Any]],
    reviewed_annual_resolutions: dict[
        tuple[str, str], AnnualMetricResolution
    ],
    asof: str,
    max_candidate_staleness_days: int,
) -> dict[str, Any]:
    ticker = str(member["ticker"])
    cohort = str(member["calibration_cohort_id"])
    industry = str(member["industry"])
    base = {
        "ticker": ticker,
        "asof_date": asof,
        "model_family": MODEL_FAMILY,
        "calibration_cohort": cohort,
        "industry": industry,
        "metric_name": metric.metric_id,
        "component": metric.component,
        "metric_value": "",
        "unit": metric.unit,
        "source_id": "",
        "accession_number": "",
        "filing_date": "",
        "period_start": "",
        "period_end": "",
        "taxonomy": "",
        "concept_name": metric.source_field or metric.candidate_metric or metric.formula,
        "confidence": 0.0,
        "provenance_json": "",
    }
    if not metric.applies_to(cohort=cohort, industry=industry):
        return {
            **base,
            "availability_status": "NOT_APPLICABLE",
            "extraction_method": "registry_applicability",
            "confidence": 1.0,
            "status_reason": "metric_not_applicable_to_cohort_or_industry",
        }
    if metric.birthdate and asof < metric.birthdate:
        return {
            **base,
            "availability_status": "NOT_APPLICABLE",
            "extraction_method": "feature_birthdate_gate",
            "confidence": 1.0,
            "status_reason": f"metric_birthdate={metric.birthdate}",
        }
    reviewed_override = reviewed_overrides.get((ticker, metric.metric_id))
    if reviewed_override is not None:
        status = str(reviewed_override["availability_status"])
        if status != "NOT_APPLICABLE":
            raise ValueError(
                f"Unsupported transportation reviewed override status={status}"
            )
        return {
            **base,
            "availability_status": status,
            "extraction_method": "reviewed_metric_availability_override",
            "confidence": 1.0,
            "status_reason": str(reviewed_override["status_reason"]),
            "provenance_json": json.dumps(
                {
                    "evidence_key": reviewed_override["evidence_key"],
                    "valid_from": reviewed_override["valid_from"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    reviewed_annual = reviewed_annual_resolutions.get(
        (ticker, metric.metric_id)
    )
    if reviewed_annual is not None:
        return {
            **base,
            "availability_status": reviewed_annual.availability_status,
            "metric_value": (
                reviewed_annual.metric_value
                if reviewed_annual.metric_value is not None
                else ""
            ),
            "unit": reviewed_annual.unit,
            "source_id": REVIEWED_OPERAND_SOURCE_ID,
            "accession_number": reviewed_annual.accession_number,
            "filing_date": reviewed_annual.filing_date,
            "period_start": reviewed_annual.period_start,
            "period_end": reviewed_annual.period_end,
            "taxonomy": reviewed_annual.taxonomy,
            "concept_name": reviewed_annual.concept_name,
            "extraction_method": "reviewed_aligned_annual_formula",
            "confidence": reviewed_annual.confidence,
            "status_reason": reviewed_annual.status_reason,
            "provenance_json": reviewed_annual.provenance_json,
        }
    cash_burn = number(financial.get("cash_burn_ttm_usd"))
    if (
        metric.metric_id == "cash_runway_years"
        and cash_burn is not None
        and cash_burn <= 0
    ):
        return {
            **base,
            "availability_status": "NOT_APPLICABLE",
            "source_id": str(financial.get("source_id") or ""),
            "extraction_method": "conditional_applicability",
            "confidence": 1.0,
            "status_reason": "issuer_cash_generative_runway_not_meaningful",
        }
    if metric.source in {"market", "financial"}:
        source = market if metric.source == "market" else financial
        # A reviewed 0.0 financial confidence (e.g. raw-archive profiles) must
        # survive as 0.0; only a missing value falls back to the 0.5 default.
        source_confidence = number(source.get("financial_confidence"))
        value = number(source.get(metric.source_field))
        history = int(number(market.get("trading_days_available")) or 0)
        if metric.source == "market" and history < metric.minimum_history_days:
            value = None
            reason = f"minimum_history_{metric.minimum_history_days}_actual_{history}"
        else:
            reason = "source_field_missing_or_non_numeric"
        if value is None:
            return {
                **base,
                "availability_status": "NOT_DISCLOSED",
                "source_id": str(source.get("source_id") or ""),
                "extraction_method": f"{metric.source}_feature_lookup",
                "status_reason": reason,
            }
        return {
            **base,
            "availability_status": "REPORTED",
            "metric_value": value,
            "source_id": str(source.get("source_id") or ""),
            "accession_number": str(source.get("accession_number") or ""),
            "filing_date": str(source.get("asof_date") or ""),
            "period_end": str(source.get("fiscal_period_end") or source.get("asof_date") or ""),
            "extraction_method": f"{metric.source}_feature_lookup",
            "confidence": 0.95
            if metric.source == "market"
            else (source_confidence if source_confidence is not None else 0.5),
            "status_reason": "observed_source_field",
        }
    if metric.source == "derived":
        derived_confidence = number(financial.get("financial_confidence"))
        value = derived_value(metric, financial)
        if value is None:
            return {
                **base,
                "availability_status": "NOT_DISCLOSED",
                "source_id": str(financial.get("source_id") or ""),
                "extraction_method": "registry_formula",
                "status_reason": "formula_operand_missing_or_invalid",
            }
        return {
            **base,
            "availability_status": "DERIVED",
            "metric_value": value,
            "source_id": str(financial.get("source_id") or ""),
            "accession_number": str(financial.get("accession_number") or ""),
            "filing_date": str(financial.get("asof_date") or ""),
            "period_end": str(financial.get("fiscal_period_end") or ""),
            "extraction_method": "registry_formula",
            "confidence": min(
                0.90,
                derived_confidence if derived_confidence is not None else 0.5,
            ),
            "status_reason": metric.formula,
        }
    candidate = candidate_row(
        conn,
        ticker=ticker,
        metric=metric.candidate_metric,
        asof=asof,
        max_staleness_days=max_candidate_staleness_days,
    )
    if not candidate:
        return {
            **base,
            "availability_status": "NOT_DISCLOSED",
            "extraction_method": "disclosure_candidate_lookup",
            "status_reason": "no_candidate_available_by_asof",
        }
    status = str(candidate.get("candidate_status") or "").upper()
    value = number(candidate.get("candidate_value"))
    if status == "ACCEPTED" and value is not None:
        availability = "REPORTED"
        reason = "reviewed_disclosure_candidate"
    elif status == "PARSER_FAILURE" or (status == "ACCEPTED" and value is None):
        availability = "PARSER_FAILURE"
        reason = str(candidate.get("status_reason") or "accepted_candidate_missing_value")
    else:
        availability = "DISCLOSED_UNPARSED"
        reason = str(candidate.get("status_reason") or f"candidate_status={status or 'UNKNOWN'}")
    return {
        **base,
        "availability_status": availability,
        "metric_value": value if availability == "REPORTED" else "",
        "unit": str(candidate.get("unit") or metric.unit),
        "source_id": str(candidate.get("source_id") or ""),
        "accession_number": str(candidate.get("accession_number") or ""),
        "filing_date": str(candidate.get("filing_date") or ""),
        "period_start": str(candidate.get("period_start") or ""),
        "period_end": str(candidate.get("period_end") or ""),
        "concept_name": str(candidate.get("concept_name") or metric.candidate_metric),
        "extraction_method": str(candidate.get("extraction_method") or "disclosure_candidate_lookup"),
        "confidence": float(number(candidate.get("confidence")) or 0.0),
        "status_reason": reason,
        "provenance_json": str(candidate.get("provenance_json") or ""),
    }


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    financial_config = family["financial"]
    specialized_config = family.get("specialized_disclosures") or {}
    max_candidate_staleness_days = int(
        specialized_config.get("max_candidate_staleness_days", 400) or 400
    )
    if max_candidate_staleness_days <= 0:
        raise ValueError("max_candidate_staleness_days must be positive")
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    registry_path = resolve_path(financial_config["metric_registry"], base_dir=base_dir)
    output_path = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(
        financial_config["metric_availability_output_csv"], base_dir=base_dir
    )
    registry_version, definitions = load_metric_registry(registry_path)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        init_db(conn)
        asof = str(args.asof or latest_asof(conn)).strip()
        if parse_date(asof) is None:
            raise ValueError(f"No valid transportation feature asof available: {asof!r}")
        members = load_members(
            conn,
            asof=asof,
            active_source_id=str(universe["seed_source_id"]),
            include_historical=bool(args.include_historical),
        )
        if not members:
            raise ValueError(f"No active transportation members at {asof}")
        reviewed_overrides = load_reviewed_metric_overrides(
            conn,
            asof=asof,
        )
        repair_scope_pairs = load_scope_pairs()
        reviewed_annual_resolutions = load_reviewed_annual_resolutions(
            conn,
            asof=asof,
            scope_pairs=repair_scope_pairs,
        )
        report: list[dict[str, Any]] = []
        for member in members:
            ticker = str(member["ticker"])
            market = latest_row(conn, table="feature_market_technical", ticker=ticker, asof=asof)
            financial = latest_row(conn, table="feature_financial_statement", ticker=ticker, asof=asof)
            for metric in definitions:
                report.append(
                    classify_metric(
                        conn,
                        metric=metric,
                        member=member,
                        market=market,
                        financial=financial,
                        reviewed_overrides=reviewed_overrides,
                        reviewed_annual_resolutions=reviewed_annual_resolutions,
                        asof=asof,
                        max_candidate_staleness_days=max_candidate_staleness_days,
                    )
                )
        if not args.dry_run:
            now = utc_now()
            with conn:
                conn.execute(
                    "DELETE FROM feature_financial_metric_availability WHERE model_family=? AND asof_date=?",
                    (MODEL_FAMILY, asof),
                )
                for row in report:
                    db_fields = [field for field in FIELDS if field not in {"calibration_cohort", "industry", "component"}]
                    conn.execute(
                        f"""
                        INSERT INTO feature_financial_metric_availability({', '.join(db_fields)}, created_at, updated_at)
                        VALUES ({', '.join('?' for _ in db_fields)}, ?, ?)
                        """,
                        tuple(row.get(field, "") for field in db_fields) + (now, now),
                    )
    write_csv_atomic(output_path, FIELDS, report)
    counts: dict[str, int] = {}
    for row in report:
        status = str(row["availability_status"])
        counts[status] = counts.get(status, 0) + 1
    summary = {
        "acceptance": "PASS",
        "asof_date": asof,
        "dry_run": bool(args.dry_run),
        "member_count": len(members),
        "include_historical": bool(args.include_historical),
        "max_candidate_staleness_days": max_candidate_staleness_days,
        "registry_version": registry_version,
        **registry_summary(definitions),
        "availability_counts": counts,
        "output_csv": str(output_path),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
