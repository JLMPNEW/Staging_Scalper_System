#!/usr/bin/env python3
"""Stage 10 software-infrastructure dashboard/report publisher."""
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
import shutil
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.software_infrastructure.optuna_calibration import write_csv  # noqa: E402


LOGGER = logging.getLogger("software_infrastructure_dashboard_reports")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "software_infrastructure_dashboard_reports"
COMPONENT_NAMES = ["quality", "valuation", "growth", "market_behavior", "positioning", "risk_control"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Stage 10 software-infrastructure dashboard reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Dashboard as-of date. Defaults to latest production output.")
    parser.add_argument(
        "--historical-mode",
        action="store_true",
        help="Publish a point-in-time historical snapshot and omit non-PIT research/backtest sections.",
    )
    return parser.parse_args()


def readonly_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db_path.expanduser().resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def safe_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        numeric = float(value)
        if math.isnan(numeric) or math.isinf(numeric):
            return None
        return numeric
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"json_error": f"Could not parse {path}"}


def sec_url(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    cik = str(row.get("cik") or "").lstrip("0")
    accession = str(row.get("accession_number") or "")
    primary = str(row.get("primary_document") or "")
    if not cik or not accession:
        return ""
    accession_clean = accession.replace("-", "")
    if primary:
        return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/{primary}"
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/"


def latest_filings(conn: sqlite3.Connection, source_id: str, *, asof: str = "") -> dict[str, dict[str, Any]]:
    asof_clause = "AND filing_date <= ?" if asof else ""
    params: tuple[Any, ...] = (source_id, asof) if asof else (source_id,)
    rows = fetch_dicts(
        conn,
        f"""
        SELECT ticker, cik, accession_number, form_type, filing_date, primary_document
        FROM fact_sec_filing
        WHERE source_id = ?
          AND form_type IN ('10-K', '10-Q', '20-F', '40-F', '10-K/A', '10-Q/A', '20-F/A', '40-F/A')
          {asof_clause}
        ORDER BY ticker, filing_date DESC, accession_number DESC
        """,
        params,
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out.setdefault(str(row["ticker"]), row)
    return out


def load_latest_score_rows(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    baseline_source_id: str,
    model_family: str,
    asof: str = "",
) -> list[dict[str, Any]]:
    asof = asof.strip() or scalar(
        conn,
        """
        SELECT MAX(asof_date)
        FROM feature_scoring_model_output
        WHERE source_id = ? AND model_family = ?
        """,
        (source_id, model_family),
    )
    if not asof:
        return []
    return fetch_dicts(
        conn,
        """
        SELECT o.ticker,
               o.asof_date,
               o.source_id AS score_model_version,
               o.model_family,
               o.model_version,
               o.final_rank,
               o.final_percentile,
               o.final_score,
               o.core_score,
               o.sector_overlay_score,
               o.data_quality_confidence,
               o.rank_ready_flag,
               o.calibration_eligible_flag,
               o.model_status,
               o.review_reason,
               c.company_name,
               c.sector,
               c.industry,
               c.subsector,
               c.country,
               c.currency,
               i.calibration_cohort_id,
               i.calibration_cohort,
               i.scoring_contract_version,
               i.latest_price,
               i.market_cap,
               i.revenue_yoy_growth,
               i.gross_profit_yoy_growth,
               i.operating_income_yoy_growth,
               i.free_cash_flow_yoy_growth,
               i.revenue_acceleration,
               i.gross_margin,
               i.operating_margin,
               i.fcf_margin,
               i.fcf_to_net_income,
               i.net_cash_to_assets,
               i.sbc_pct_revenue,
               i.r_and_d_pct_revenue,
               i.share_count_yoy_growth,
               i.ev_gross_profit,
               i.ev_operating_income,
               i.fcf_yield,
               i.ret_3m,
               i.ret_12m_ex_1m,
               i.rel_strength_bench_3m,
               i.realized_vol_60d,
               i.max_drawdown_12m,
               i.distance_from_52w_high,
               i.avg_dollar_volume_60d,
               i.low_liquidity_flag,
               i.insider_net_value_90d,
               i.insider_cluster_buyers_90d,
               i.institutional_ownership_delta_pct,
               i.latest_short_interest_pct_float,
               i.short_interest_change_3m,
               i.latest_days_to_cover,
               i.latest_borrow_fee_rate,
               i.market_quality,
               i.financial_quality,
               i.positioning_quality,
               i.core_available_component_count,
               i.core_missing_component_count,
               i.core_data_quality_confidence
        FROM feature_scoring_model_output o
        LEFT JOIN feature_scoring_input i
          ON i.ticker = o.ticker
         AND i.asof_date = o.asof_date
         AND i.model_family = o.model_family
         AND i.source_id = ?
        LEFT JOIN dim_company c
          ON c.ticker = o.ticker
        WHERE o.source_id = ?
          AND o.model_family = ?
          AND o.asof_date = ?
        ORDER BY o.final_rank IS NULL, o.final_rank, o.ticker
        """,
        (baseline_source_id, source_id, model_family, asof),
    )


def component_rows(conn: sqlite3.Connection, *, source_id: str, model_family: str, asof: str) -> list[dict[str, Any]]:
    return fetch_dicts(
        conn,
        """
        SELECT ticker,
               component_name,
               component_group,
               component_score,
               universe_percentile,
               cohort_percentile,
               component_quality,
               component_status,
               available_subfeature_count,
               missing_subfeature_count,
               default_applied,
               review_reason
        FROM feature_scoring_component
        WHERE source_id = ?
          AND model_family = ?
          AND asof_date = ?
        ORDER BY ticker, component_name
        """,
        (source_id, model_family, asof),
    )


def component_pivot(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["ticker"]), {})[str(row["component_name"])] = row
    return out


def rank_table(
    rows: list[dict[str, Any]],
    components: dict[str, dict[str, dict[str, Any]]],
    filings: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row["ticker"])
        item = {
            "ticker": ticker,
            "asof_date": row.get("asof_date"),
            "score_model_version": row.get("score_model_version"),
            "model_family": row.get("model_family"),
            "model_version": row.get("model_version"),
            "scoring_contract_version": row.get("scoring_contract_version"),
            "company_name": row.get("company_name"),
            "sector": row.get("sector"),
            "industry": row.get("industry"),
            "subsector": row.get("subsector"),
            "country": row.get("country"),
            "currency": row.get("currency"),
            "final_rank": row.get("final_rank"),
            "final_percentile": row.get("final_percentile"),
            "final_score": row.get("final_score"),
            "core_score": row.get("core_score"),
            "data_quality_confidence": row.get("data_quality_confidence"),
            "rank_ready_flag": row.get("rank_ready_flag"),
            "calibration_eligible_flag": row.get("calibration_eligible_flag"),
            "model_status": row.get("model_status"),
            "review_reason": row.get("review_reason"),
            "calibration_cohort_id": row.get("calibration_cohort_id"),
            "calibration_cohort": row.get("calibration_cohort"),
            "market_cap": row.get("market_cap"),
            "latest_price": row.get("latest_price"),
            "avg_dollar_volume_60d": row.get("avg_dollar_volume_60d"),
            "revenue_yoy_growth": row.get("revenue_yoy_growth"),
            "gross_margin": row.get("gross_margin"),
            "operating_margin": row.get("operating_margin"),
            "fcf_margin": row.get("fcf_margin"),
            "sbc_pct_revenue": row.get("sbc_pct_revenue"),
            "fcf_yield": row.get("fcf_yield"),
            "ev_gross_profit": row.get("ev_gross_profit"),
            "ret_12m_ex_1m": row.get("ret_12m_ex_1m"),
            "rel_strength_bench_3m": row.get("rel_strength_bench_3m"),
            "realized_vol_60d": row.get("realized_vol_60d"),
            "max_drawdown_12m": row.get("max_drawdown_12m"),
            "latest_short_interest_pct_float": row.get("latest_short_interest_pct_float"),
            "latest_borrow_fee_rate": row.get("latest_borrow_fee_rate"),
            "institutional_ownership_delta_pct": row.get("institutional_ownership_delta_pct"),
            "latest_sec_form": filings.get(ticker, {}).get("form_type", ""),
            "latest_sec_filing_date": filings.get(ticker, {}).get("filing_date", ""),
            "latest_sec_url": sec_url(filings.get(ticker)),
        }
        for component in COMPONENT_NAMES:
            comp = components.get(ticker, {}).get(component, {})
            item[f"{component}_score"] = comp.get("component_score", "")
            item[f"{component}_quality"] = comp.get("component_quality", "")
            item[f"{component}_status"] = comp.get("component_status", "")
        out.append(item)
    return out


def snapshot_outputs(output_dir: Path, asof: str, outputs: dict[str, Path], *, manifest_key: str) -> tuple[Path, dict[str, str]]:
    if output_dir.name == asof:
        return output_dir, {key: str(path) for key, path in outputs.items()}
    snapshot_dir = output_dir / asof
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_map: dict[str, str] = {}
    for key, path in outputs.items():
        if key == manifest_key or not path.exists() or not path.is_file():
            continue
        target = snapshot_dir / path.name
        shutil.copy2(path, target)
        snapshot_map[key] = str(target)
    snapshot_map[manifest_key] = str(snapshot_dir / outputs[manifest_key].name)
    return snapshot_dir, snapshot_map


def scorecards(rank_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = [
        "final_rank",
        "ticker",
        "final_score",
        "calibration_cohort_id",
        "market_cap",
        "latest_price",
        "avg_dollar_volume_60d",
        "revenue_yoy_growth",
        "gross_margin",
        "operating_margin",
        "fcf_margin",
        "sbc_pct_revenue",
        "fcf_yield",
        "ev_gross_profit",
        "ret_12m_ex_1m",
        "rel_strength_bench_3m",
        "quality_score",
        "valuation_score",
        "growth_score",
        "market_behavior_score",
        "positioning_score",
        "risk_control_score",
        "data_quality_confidence",
        "latest_sec_filing_date",
        "latest_sec_url",
    ]
    return [{field: row.get(field, "") for field in fields} for row in rank_rows]


def cohort_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("calibration_cohort_id") or "unknown"), []).append(row)
    out: list[dict[str, Any]] = []
    for cohort, cohort_rows in sorted(buckets.items()):
        scores = [value for value in (safe_float(row.get("final_score")) for row in cohort_rows) if value is not None]
        ranked = sorted(cohort_rows, key=lambda row: (safe_int(row.get("final_rank")) == 0, safe_int(row.get("final_rank")), str(row.get("ticker"))))
        rank_values = sorted([safe_int(row.get("final_rank")) for row in cohort_rows if safe_int(row.get("final_rank")) > 0])
        out.append(
            {
                "calibration_cohort_id": cohort,
                "calibration_cohort": ranked[0].get("calibration_cohort", "") if ranked else "",
                "ticker_count": len(cohort_rows),
                "rank_ready_count": sum(1 for row in cohort_rows if safe_int(row.get("rank_ready_flag")) == 1),
                "calibration_eligible_count": sum(1 for row in cohort_rows if safe_int(row.get("calibration_eligible_flag")) == 1),
                "avg_final_score": sum(scores) / len(scores) if scores else "",
                "top_ticker": ranked[0].get("ticker", "") if ranked else "",
                "top_score": ranked[0].get("final_score", "") if ranked else "",
                "median_rank": rank_values[len(rank_values) // 2] if rank_values else "",
            }
        )
    return out


def component_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("component_name")), []).append(row)
    out: list[dict[str, Any]] = []
    for component, component_rows_for_name in sorted(buckets.items()):
        scores = [value for value in (safe_float(row.get("component_score")) for row in component_rows_for_name) if value is not None]
        qualities = [value for value in (safe_float(row.get("component_quality")) for row in component_rows_for_name) if value is not None]
        out.append(
            {
                "component_name": component,
                "ticker_count": len(component_rows_for_name),
                "avg_component_score": sum(scores) / len(scores) if scores else "",
                "avg_component_quality": sum(qualities) / len(qualities) if qualities else "",
                "review_count": sum(1 for row in component_rows_for_name if str(row.get("component_status") or "").lower() != "complete"),
                "default_applied_count": sum(1 for row in component_rows_for_name if safe_int(row.get("default_applied")) == 1),
                "total_missing_subfeatures": sum(safe_int(row.get("missing_subfeature_count")) for row in component_rows_for_name),
            }
        )
    return out


def parse_date_text(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def risk_flags(rows: list[dict[str, Any]], *, current_asof: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    asof_date = parse_date_text(current_asof)

    def add(row: dict[str, Any], flag: str, severity: str, detail: str) -> None:
        out.append(
            {
                "ticker": row.get("ticker"),
                "asof_date": row.get("asof_date"),
                "calibration_cohort_id": row.get("calibration_cohort_id"),
                "avg_dollar_volume_60d": row.get("avg_dollar_volume_60d"),
                "severity": severity,
                "flag": flag,
                "detail": detail,
            }
        )

    for row in rows:
        if safe_int(row.get("rank_ready_flag")) != 1:
            add(row, "not_rank_ready", "error", str(row.get("review_reason") or "rank_ready_flag=0"))
        confidence = safe_float(row.get("data_quality_confidence"))
        if confidence is not None and confidence < 0.60:
            add(row, "low_data_quality", "warning", f"data_quality_confidence={confidence:.3f}")
        if str(row.get("model_status") or "").lower() not in {"rank_ready", "active", "complete"}:
            add(row, "model_status_review", "warning", str(row.get("model_status") or ""))
        if safe_int(row.get("low_liquidity_flag")) == 1:
            add(row, "low_liquidity", "warning", "low_liquidity_flag=1")
        borrow = safe_float(row.get("latest_borrow_fee_rate"))
        if borrow is not None and borrow >= 0.10:
            add(row, "high_borrow_fee", "warning", f"latest_borrow_fee_rate={borrow:.3f}")
        short_pct = safe_float(row.get("latest_short_interest_pct_float"))
        if short_pct is not None and short_pct >= 0.15:
            add(row, "high_short_interest", "warning", f"latest_short_interest_pct_float={short_pct:.3f}")
        days_to_cover = safe_float(row.get("latest_days_to_cover"))
        if days_to_cover is not None and days_to_cover >= 5.0:
            add(row, "high_days_to_cover", "warning", f"latest_days_to_cover={days_to_cover:.2f}")
        drawdown = safe_float(row.get("max_drawdown_12m"))
        if drawdown is not None and drawdown <= -0.50:
            add(row, "large_12m_drawdown", "warning", f"max_drawdown_12m={drawdown:.3f}")
        sbc = safe_float(row.get("sbc_pct_revenue"))
        if sbc is not None and sbc >= 0.20:
            add(row, "high_sbc_pct_revenue", "warning", f"sbc_pct_revenue={sbc:.3f}")
        share_growth = safe_float(row.get("share_count_yoy_growth"))
        if share_growth is not None and share_growth >= 0.05:
            add(row, "share_dilution", "warning", f"share_count_yoy_growth={share_growth:.3f}")
        fcf_margin = safe_float(row.get("fcf_margin"))
        if fcf_margin is not None and fcf_margin < 0.0:
            add(row, "negative_fcf_margin", "warning", f"fcf_margin={fcf_margin:.3f}")
        operating_margin = safe_float(row.get("operating_margin"))
        if operating_margin is not None and operating_margin < 0.0:
            add(row, "negative_operating_margin", "warning", f"operating_margin={operating_margin:.3f}")
        ev_gp = safe_float(row.get("ev_gross_profit"))
        if ev_gp is not None and ev_gp >= 20.0:
            add(row, "high_ev_gross_profit", "info", f"ev_gross_profit={ev_gp:.2f}")
        filing_date = parse_date_text(row.get("latest_sec_filing_date"))
        if asof_date and filing_date and (asof_date - filing_date).days > 550:
            add(row, "stale_sec_filing", "warning", f"latest_sec_filing_date={filing_date.isoformat()}")
        if row.get("review_reason"):
            add(row, "review_reason_present", "info", str(row.get("review_reason")))

    severity_order = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda row: (severity_order.get(str(row["severity"]), 9), str(row["ticker"]), str(row["flag"])))
    return out


def review_queue(flags: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ticker = {str(row["ticker"]): row for row in rows}
    out: list[dict[str, Any]] = []
    for flag in flags:
        source = by_ticker.get(str(flag["ticker"]), {})
        out.append(
            {
                **flag,
                "final_rank": source.get("final_rank", ""),
                "final_score": source.get("final_score", ""),
                "avg_dollar_volume_60d": source.get("avg_dollar_volume_60d", ""),
                "model_status": source.get("model_status", ""),
                "review_reason": source.get("review_reason", ""),
            }
        )
    return out


def backtest_leaders(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: safe_float(row.get("annualized_return")) or -999.0, reverse=True)
    fields = [
        "model_name",
        "portfolio_name",
        "weight_method",
        "exposure_mode",
        "annualized_return",
        "annualized_vol",
        "sharpe",
        "max_drawdown",
        "avg_excess_return_vs_qqq",
        "avg_excess_return_vs_equal_weight",
        "avg_turnover",
        "avg_total_cost",
        "avg_max_cohort_share",
    ]
    return [{field: row.get(field, "") for field in fields} for row in ordered[:limit]]


def calibration_summary(config: dict[str, Any], base_dir: Path) -> list[dict[str, Any]]:
    stage8a = read_json(resolve_path(cfg_get(config, f"{CONFIG_KEY}.signal_diagnostics_summary_json"), base_dir=base_dir))
    walk_forward = read_json(resolve_path(cfg_get(config, f"{CONFIG_KEY}.walk_forward_summary_json"), base_dir=base_dir))
    weights = read_json(resolve_path(cfg_get(config, f"{CONFIG_KEY}.optuna_weights_json"), base_dir=base_dir))
    optuna_rows = read_csv_rows(resolve_path(cfg_get(config, f"{CONFIG_KEY}.optuna_summary_csv"), base_dir=base_dir))
    rows: list[dict[str, Any]] = [
        {
            "section": "stage8a_signal_diagnostics",
            "metric": "panel_rows",
            "value": stage8a.get("usable_panel_rows", stage8a.get("panel_rows", "")),
            "detail": f"dates={stage8a.get('panel_dates', '')}",
        },
        {
            "section": "stage8a_signal_diagnostics",
            "metric": "date_range",
            "value": f"{stage8a.get('start_date', stage8a.get('min_panel_date', ''))}..{stage8a.get('end_date', stage8a.get('max_panel_date', ''))}",
            "detail": "",
        },
        {
            "section": "stage8b_optuna",
            "metric": "promotion_candidate",
            "value": weights.get("promotion_candidate", ""),
            "detail": f"source={weights.get('source_id', '')}",
        },
        {
            "section": "stage8c_walk_forward",
            "metric": "procedure_adds_value",
            "value": walk_forward.get("procedure_adds_value", ""),
            "detail": f"blocks={walk_forward.get('n_blocks', '')} win_rate={walk_forward.get('refit_win_rate', '')}",
        },
        {
            "section": "stage8c_walk_forward",
            "metric": "mean_objective_improvement",
            "value": walk_forward.get("mean_objective_improvement", ""),
            "detail": f"gate_pass_rate={walk_forward.get('promotion_gate_pass_rate', '')}",
        },
    ]
    for row in optuna_rows:
        rows.append(
            {
                "section": "stage8b_optuna_summary",
                "metric": str(row.get("model") or ""),
                "value": row.get("holdout_objective", ""),
                "detail": (
                    f"ic21={row.get('holdout_mean_ic_21', '')} "
                    f"ic63={row.get('holdout_mean_ic_63', '')} "
                    f"spread21_net={row.get('holdout_mean_spread_net_21', '')}"
                ),
            }
        )
    return rows


def fmt(value: Any) -> str:
    numeric = safe_float(value)
    if numeric is not None:
        if abs(numeric) >= 1_000_000_000:
            return f"{numeric / 1_000_000_000:.1f}B"
        if abs(numeric) >= 1_000_000:
            return f"{numeric / 1_000_000:.1f}M"
        if abs(numeric) >= 1000:
            return f"{numeric:,.0f}"
        return f"{numeric:.3f}"
    return str(value or "")


def html_table(rows: list[dict[str, Any]], columns: list[str], limit: int | None = None) -> str:
    subset = rows[:limit] if limit else rows
    head = "".join(f"<th>{html.escape(col)}</th>" for col in columns)
    body_rows = []
    for row in subset:
        cells = "".join(f"<td>{html.escape(fmt(row.get(col, '')))}</td>" for col in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def write_html(
    path: Path,
    *,
    rank_rows: list[dict[str, Any]],
    stage8_rows: list[dict[str, Any]],
    cohort_rows: list[dict[str, Any]],
    component_rows_for_summary: list[dict[str, Any]],
    backtest_rows: list[dict[str, Any]],
    calibration_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
    top_n: int,
    review_n: int,
    historical_mode: bool,
) -> None:
    historical_notice = (
        "<p class=\"meta\">Historical point-in-time mode: current full-history Stage 8/backtest research sections are omitted.</p>"
        if historical_mode
        else ""
    )
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Software Infrastructure Dashboard</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #1f2933; }}
    h1, h2 {{ margin-bottom: 8px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 28px; font-size: 13px; }}
    th, td {{ border: 1px solid #d7dde5; padding: 6px 8px; text-align: left; }}
    th {{ background: #f2f5f8; }}
    .meta {{ color: #5b6775; margin-bottom: 20px; }}
  </style>
</head>
<body>
  <h1>Software Infrastructure Dashboard</h1>
  <div class="meta">Generated {html.escape(datetime.now(timezone.utc).isoformat(timespec="seconds"))}</div>
  {historical_notice}
  <h2>Production Rank Table</h2>
  {html_table(rank_rows, ["final_rank", "ticker", "final_score", "calibration_cohort_id", "data_quality_confidence", "model_status"], top_n)}
  <h2>{'Stage 8 Promotion Evidence Omitted' if historical_mode else 'Stage 8 Promotion Evidence Rank Table'}</h2>
  {html_table(stage8_rows, ["stage8_candidate_rank", "ticker", "stage8_candidate_score", "stage7_rank", "stage7_score", "stage8_quality"], top_n)}
  <h2>Cohort Summary</h2>
  {html_table(cohort_rows, ["calibration_cohort_id", "ticker_count", "rank_ready_count", "avg_final_score", "top_ticker", "top_score"])}
  <h2>Component Summary</h2>
  {html_table(component_rows_for_summary, ["component_name", "ticker_count", "avg_component_score", "avg_component_quality", "review_count", "default_applied_count"])}
  <h2>{'Backtest Leaders Omitted' if historical_mode else 'Backtest Leaders'}</h2>
  {html_table(backtest_rows, ["model_name", "portfolio_name", "weight_method", "exposure_mode", "annualized_return", "sharpe", "max_drawdown", "avg_excess_return_vs_equal_weight", "avg_turnover"])}
  <h2>Calibration Evidence</h2>
  {html_table(calibration_rows, ["section", "metric", "value", "detail"])}
  <h2>Review Queue</h2>
  {html_table(review_rows, ["severity", "ticker", "flag", "detail", "final_rank", "final_score"], review_n)}
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/software_infrastructure/dashboard"),
        base_dir=base_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    model_family = str(cfg_get(config, f"{CONFIG_KEY}.model_family", "software_infrastructure"))
    production_source = str(
        cfg_get(
            config,
            f"{CONFIG_KEY}.production_source_id",
            cfg_get(config, f"{CONFIG_KEY}.stage7_source_id", "software_infrastructure_calibrated_score_v1"),
        )
    )
    baseline_source = str(cfg_get(config, f"{CONFIG_KEY}.baseline_feature_source_id", "software_infrastructure_scoring_contract"))
    filing_source = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions"))

    with readonly_connect(db_path) as conn:
        score_rows = load_latest_score_rows(
            conn,
            source_id=production_source,
            baseline_source_id=baseline_source,
            model_family=model_family,
            asof=str(args.asof or ""),
        )
        if not score_rows:
            raise RuntimeError("No software-infrastructure Stage 7 model output rows found for dashboard publishing.")
        asof = str(score_rows[0]["asof_date"])
        component_detail_rows = component_rows(conn, source_id=production_source, model_family=model_family, asof=asof)
        components = component_pivot(component_detail_rows)
        filings = latest_filings(conn, filing_source, asof=asof)

    ranks = rank_table(score_rows, components, filings)
    cards = scorecards(ranks)
    cohorts = cohort_summary(score_rows)
    component_summaries = component_summary(component_detail_rows)
    flags = risk_flags(score_rows, current_asof=asof)
    queue = review_queue(flags, score_rows)

    if args.historical_mode:
        backtest_rows = []
        backtest_top = []
        stage8_candidate_rows = []
        calibration_rows = [
            {
                "section": "historical_mode",
                "metric": "non_point_in_time_research_sections",
                "value": "omitted",
                "detail": "Backtest, Stage 8 candidate, Optuna, and walk-forward sections are current full-history artifacts, not point-in-time.",
            }
        ]
    else:
        backtest_path = resolve_path(cfg_get(config, f"{CONFIG_KEY}.backtest_summary_csv"), base_dir=base_dir)
        backtest_rows = read_csv_rows(backtest_path)
        backtest_top = backtest_leaders(backtest_rows, limit=int(cfg_get(config, f"{CONFIG_KEY}.backtest_leader_rows", 16)))
        stage8_candidate_path = resolve_path(cfg_get(config, f"{CONFIG_KEY}.optuna_candidate_scores_csv"), base_dir=base_dir)
        stage8_candidate_rows = read_csv_rows(stage8_candidate_path)
        calibration_rows = calibration_summary(config, base_dir)

    outputs = {
        "rank_table": output_dir / "software_infrastructure_final_rank_table.csv",
        "scorecards": output_dir / "software_infrastructure_company_scorecards.csv",
        "cohort_summary": output_dir / "software_infrastructure_cohort_rank_summary.csv",
        "component_summary": output_dir / "software_infrastructure_component_summary.csv",
        "risk_flags": output_dir / "software_infrastructure_risk_flags.csv",
        "review_queue": output_dir / "software_infrastructure_review_queue.csv",
        "calibration_summary": output_dir / "software_infrastructure_calibration_summary.csv",
        "backtest_leaders": output_dir / "software_infrastructure_backtest_leaders.csv",
        "stage8_candidate_rank_table": output_dir / "software_infrastructure_stage8_candidate_rank_table.csv",
        "html": output_dir / "index.html",
        "manifest": output_dir / "software_infrastructure_dashboard_manifest.json",
    }
    write_csv(outputs["rank_table"], ranks)
    write_csv(outputs["scorecards"], cards)
    write_csv(outputs["cohort_summary"], cohorts)
    write_csv(outputs["component_summary"], component_summaries)
    write_csv(outputs["risk_flags"], flags)
    write_csv(outputs["review_queue"], queue)
    write_csv(outputs["calibration_summary"], calibration_rows)
    write_csv(outputs["backtest_leaders"], backtest_top)
    write_csv(outputs["stage8_candidate_rank_table"], stage8_candidate_rows)
    write_html(
        outputs["html"],
        rank_rows=ranks,
        stage8_rows=stage8_candidate_rows,
        cohort_rows=cohorts,
        component_rows_for_summary=component_summaries,
        backtest_rows=backtest_top,
        calibration_rows=calibration_rows,
        review_rows=queue,
        top_n=int(cfg_get(config, f"{CONFIG_KEY}.top_rank_rows_in_html", 25)),
        review_n=int(cfg_get(config, f"{CONFIG_KEY}.max_review_rows_in_html", 50)),
        historical_mode=bool(args.historical_mode),
    )
    snapshot_dir, snapshot_map = snapshot_outputs(output_dir, asof, outputs, manifest_key="manifest")

    rank_ready_count = sum(1 for row in score_rows if safe_int(row.get("rank_ready_flag")) == 1)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database_path": str(db_path),
        "model_family": model_family,
        "production_source_id": production_source,
        "baseline_feature_source_id": baseline_source,
        "asof_date": asof,
        "report_mode": "historical" if args.historical_mode else "current",
        "non_point_in_time_sections": "omitted" if args.historical_mode else "included",
        "rank_rows": len(ranks),
        "rank_ready_count": rank_ready_count,
        "risk_flags": len(flags),
        "review_queue_rows": len(queue),
        "cohort_rows": len(cohorts),
        "component_summary_rows": len(component_summaries),
        "backtest_summary_rows": len(backtest_rows),
        "backtest_leader_rows": len(backtest_top),
        "stage8_candidate_rows": len(stage8_candidate_rows),
        "outputs": {key: str(path) for key, path in outputs.items() if key != "manifest"},
        "snapshot_dir": str(snapshot_dir),
        "snapshot_outputs": snapshot_map,
    }
    outputs["manifest"].write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest_copy = snapshot_dir / outputs["manifest"].name
    if outputs["manifest"].resolve() != manifest_copy.resolve():
        shutil.copy2(outputs["manifest"], manifest_copy)
    LOGGER.info(
        "Stage 10 software-infrastructure dashboard published: asof=%s rows=%d rank_ready=%d output=%s",
        asof,
        len(ranks),
        rank_ready_count,
        output_dir,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
