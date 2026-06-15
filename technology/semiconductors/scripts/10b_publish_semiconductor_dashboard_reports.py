#!/usr/bin/env python3
"""Stage 10 semiconductor dashboard/report publisher."""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.semiconductors.optuna_calibration import write_csv  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_dashboard_reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Stage 10 semiconductor dashboard reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
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
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [dict(row) for row in csv.DictReader(f)]


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


def latest_filings(conn: sqlite3.Connection, source_id: str) -> dict[str, dict[str, Any]]:
    rows = fetch_dicts(
        conn,
        """
        SELECT ticker, cik, accession_number, form_type, filing_date, primary_document
        FROM fact_sec_filing
        WHERE source_id = ?
          AND form_type IN ('10-K', '10-Q', '20-F', '40-F', '10-K/A', '10-Q/A', '20-F/A')
        ORDER BY ticker, filing_date DESC, accession_number DESC
        """,
        (source_id,),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out.setdefault(str(row["ticker"]), row)
    return out


def load_latest_score_rows(conn: sqlite3.Connection, *, source_id: str, baseline_source: str, model_family: str) -> list[dict[str, Any]]:
    asof = scalar(
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
               i.calibration_cohort_id,
               i.calibration_cohort,
               i.latest_price,
               i.market_cap,
               i.revenue_yoy_growth,
               i.gross_margin,
               i.operating_margin,
               i.fcf_margin,
               i.fcf_yield,
               i.ev_gross_profit,
               i.ret_3m,
               i.ret_12m_ex_1m,
               i.realized_vol_60d,
               i.max_drawdown_12m,
               i.avg_dollar_volume_60d,
               i.low_liquidity_flag,
               i.latest_short_interest_pct_float,
               i.short_interest_change_3m,
               i.latest_days_to_cover,
               i.latest_borrow_fee_rate,
               i.sector_cycle_score,
               i.big_tech_capex_score,
               i.sector_overlay_quality
        FROM feature_scoring_model_output o
        LEFT JOIN feature_scoring_input i
          ON i.ticker = o.ticker
         AND i.asof_date = o.asof_date
         AND i.model_family = o.model_family
         AND i.source_id = ?
        WHERE o.source_id = ?
          AND o.model_family = ?
          AND o.asof_date = ?
        ORDER BY o.final_rank IS NULL, o.final_rank, o.ticker
        """,
        (baseline_source, source_id, model_family, asof),
    )


def component_pivot(conn: sqlite3.Connection, *, source_id: str, model_family: str, asof: str) -> dict[str, dict[str, dict[str, Any]]]:
    rows = fetch_dicts(
        conn,
        """
        SELECT ticker, component_name, component_score, component_quality, component_status, review_reason
        FROM feature_scoring_component
        WHERE source_id = ?
          AND model_family = ?
          AND asof_date = ?
        ORDER BY ticker, component_name
        """,
        (source_id, model_family, asof),
    )
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["ticker"]), {})[str(row["component_name"])] = row
    return out


def rank_table(rows: list[dict[str, Any]], components: dict[str, dict[str, dict[str, Any]]], filings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    component_names = ["valuation", "quality", "risk_control", "positioning", "market_behavior", "growth", "sector_cycle", "big_tech_capex"]
    for row in rows:
        ticker = str(row["ticker"])
        item = {
            "ticker": ticker,
            "asof_date": row.get("asof_date"),
            "final_rank": row.get("final_rank"),
            "final_percentile": row.get("final_percentile"),
            "final_score": row.get("final_score"),
            "core_score": row.get("core_score"),
            "sector_overlay_score": row.get("sector_overlay_score"),
            "data_quality_confidence": row.get("data_quality_confidence"),
            "rank_ready_flag": row.get("rank_ready_flag"),
            "model_status": row.get("model_status"),
            "calibration_cohort_id": row.get("calibration_cohort_id"),
            "calibration_cohort": row.get("calibration_cohort"),
            "latest_sec_form": filings.get(ticker, {}).get("form_type", ""),
            "latest_sec_filing_date": filings.get(ticker, {}).get("filing_date", ""),
            "latest_sec_url": sec_url(filings.get(ticker)),
        }
        for component in component_names:
            comp = components.get(ticker, {}).get(component, {})
            item[f"{component}_score"] = comp.get("component_score", "")
            item[f"{component}_quality"] = comp.get("component_quality", "")
            item[f"{component}_status"] = comp.get("component_status", "")
        out.append(item)
    return out


def cohort_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("calibration_cohort_id") or "unknown"), []).append(row)
    out: list[dict[str, Any]] = []
    for cohort, cohort_rows in sorted(buckets.items()):
        scores = [safe_float(row.get("final_score")) for row in cohort_rows]
        scores = [value for value in scores if value is not None]
        ranked = sorted(cohort_rows, key=lambda row: safe_int(row.get("final_rank")))
        out.append(
            {
                "calibration_cohort_id": cohort,
                "calibration_cohort": ranked[0].get("calibration_cohort", "") if ranked else "",
                "ticker_count": len(cohort_rows),
                "rank_ready_count": sum(1 for row in cohort_rows if safe_int(row.get("rank_ready_flag")) == 1),
                "avg_final_score": sum(scores) / len(scores) if scores else "",
                "top_ticker": ranked[0].get("ticker", "") if ranked else "",
                "top_score": ranked[0].get("final_score", "") if ranked else "",
                "median_rank": sorted([safe_int(row.get("final_rank")) for row in cohort_rows])[len(cohort_rows) // 2] if cohort_rows else "",
            }
        )
    return out


def risk_flags(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(row: dict[str, Any], flag: str, severity: str, detail: str) -> None:
        out.append(
            {
                "ticker": row.get("ticker"),
                "asof_date": row.get("asof_date"),
                "calibration_cohort_id": row.get("calibration_cohort_id"),
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
        overlay_quality = safe_float(row.get("sector_overlay_quality"))
        if overlay_quality is not None and overlay_quality < 0.50:
            add(row, "low_sector_overlay_quality", "info", f"sector_overlay_quality={overlay_quality:.3f}")
        if row.get("review_reason"):
            add(row, "review_reason_present", "info", str(row.get("review_reason")))
    severity_order = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda row: (severity_order.get(str(row["severity"]), 9), str(row["ticker"]), str(row["flag"])))
    return out


def overlay_summary(conn: sqlite3.Connection, *, model_family: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    sector_source = str(cfg_get(config, "semiconductor_sector_overlays.wsts.feature_source_id", "semiconductor_sector_cycle"))
    capex_source = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.feature_source_id", "semiconductor_big_tech_capex_cycle"))
    rows: list[dict[str, Any]] = []
    sector = fetch_dicts(
        conn,
        """
        SELECT *
        FROM feature_semiconductor_sector_cycle
        WHERE source_id = ? AND model_family = ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (sector_source, model_family),
    )
    if sector:
        row = sector[0]
        rows.append(
            {
                "overlay": "wsts_sector_cycle",
                "asof_date": row.get("asof_date"),
                "score": row.get("sector_cycle_score"),
                "quality": row.get("component_quality"),
                "status": row.get("data_quality_status"),
                "latest_month": row.get("latest_month"),
                "detail": row.get("reason"),
            }
        )
    capex = fetch_dicts(
        conn,
        """
        SELECT *
        FROM feature_big_tech_capex_cycle
        WHERE source_id = ? AND model_family = ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (capex_source, model_family),
    )
    if capex:
        row = capex[0]
        rows.append(
            {
                "overlay": "big_tech_capex",
                "asof_date": row.get("asof_date"),
                "score": row.get("big_tech_capex_score"),
                "quality": row.get("component_quality"),
                "status": row.get("data_quality_status"),
                "latest_month": row.get("latest_period"),
                "detail": row.get("reason"),
            }
        )
    return rows


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
                "model_status": source.get("model_status", ""),
                "review_reason": source.get("review_reason", ""),
            }
        )
    return out


def fmt(value: Any) -> str:
    numeric = safe_float(value)
    if numeric is not None:
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
    cohort_rows: list[dict[str, Any]],
    backtest_rows: list[dict[str, Any]],
    overlay_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
    top_n: int,
    review_n: int,
) -> None:
    best_backtests = sorted(backtest_rows, key=lambda row: safe_float(row.get("annualized_return")) or -999, reverse=True)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Semiconductor Dashboard</title>
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
  <h1>Semiconductor Dashboard</h1>
  <div class="meta">Generated {html.escape(datetime.now(timezone.utc).isoformat(timespec="seconds"))}</div>
  <h2>Top Ranked Companies</h2>
  {html_table(rank_rows, ["final_rank", "ticker", "final_score", "calibration_cohort_id", "data_quality_confidence", "model_status"], top_n)}
  <h2>Cohort Summary</h2>
  {html_table(cohort_rows, ["calibration_cohort_id", "ticker_count", "rank_ready_count", "avg_final_score", "top_ticker", "top_score"])}
  <h2>Backtest Summary</h2>
  {html_table(best_backtests, ["model_name", "portfolio_name", "weight_method", "exposure_mode", "annualized_return", "annualized_vol", "sharpe", "max_drawdown", "avg_excess_return_vs_equal_weight", "avg_turnover", "avg_total_cost", "avg_max_cohort_share"], 24)}
  <h2>Sector Overlays</h2>
  {html_table(overlay_rows, ["overlay", "asof_date", "score", "quality", "status", "latest_month", "detail"])}
  <h2>Review Queue</h2>
  {html_table(review_rows, ["severity", "ticker", "flag", "detail", "final_rank", "final_score"], review_n)}
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/dashboard"),
        base_dir=base_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    stage7_source = str(cfg_get(config, f"{CONFIG_KEY}.stage7_source_id", "semiconductor_calibrated_score_v1"))
    baseline_source = str(cfg_get(config, f"{CONFIG_KEY}.baseline_feature_source_id", "semiconductor_scoring_contract"))
    filing_source = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions"))

    with readonly_connect(db_path) as conn:
        score_rows = load_latest_score_rows(conn, source_id=stage7_source, baseline_source=baseline_source, model_family=model_family)
        if not score_rows:
            raise RuntimeError("No Stage 7 model output rows found for dashboard publishing.")
        asof = str(score_rows[0]["asof_date"])
        components = component_pivot(conn, source_id=stage7_source, model_family=model_family, asof=asof)
        filings = latest_filings(conn, filing_source)
        overlay_rows = overlay_summary(conn, model_family=model_family, config=config)

    ranks = rank_table(score_rows, components, filings)
    cohorts = cohort_summary(score_rows)
    flags = risk_flags(score_rows)
    queue = review_queue(flags, score_rows)
    backtest_path = resolve_path(cfg_get(config, f"{CONFIG_KEY}.backtest_summary_csv"), base_dir=base_dir)
    backtest_rows = read_csv_rows(backtest_path)

    write_csv(output_dir / "semiconductor_final_rank_table.csv", ranks)
    write_csv(output_dir / "semiconductor_company_scorecards.csv", ranks)
    write_csv(output_dir / "semiconductor_cohort_rank_summary.csv", cohorts)
    write_csv(output_dir / "semiconductor_risk_flags.csv", flags)
    write_csv(output_dir / "semiconductor_review_queue.csv", queue)
    write_csv(output_dir / "semiconductor_overlay_summary.csv", overlay_rows)
    write_html(
        output_dir / "index.html",
        rank_rows=ranks,
        cohort_rows=cohorts,
        backtest_rows=backtest_rows,
        overlay_rows=overlay_rows,
        review_rows=queue,
        top_n=int(cfg_get(config, f"{CONFIG_KEY}.top_rank_rows_in_html", 25)),
        review_n=int(cfg_get(config, f"{CONFIG_KEY}.max_review_rows_in_html", 50)),
    )
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database_path": str(db_path),
        "stage7_source_id": stage7_source,
        "baseline_feature_source_id": baseline_source,
        "asof_date": asof,
        "rank_rows": len(ranks),
        "risk_flags": len(flags),
        "review_queue_rows": len(queue),
        "backtest_summary_rows": len(backtest_rows),
        "outputs": {
            "rank_table": str(output_dir / "semiconductor_final_rank_table.csv"),
            "scorecards": str(output_dir / "semiconductor_company_scorecards.csv"),
            "cohort_summary": str(output_dir / "semiconductor_cohort_rank_summary.csv"),
            "risk_flags": str(output_dir / "semiconductor_risk_flags.csv"),
            "review_queue": str(output_dir / "semiconductor_review_queue.csv"),
            "overlay_summary": str(output_dir / "semiconductor_overlay_summary.csv"),
            "html": str(output_dir / "index.html"),
        },
    }
    (output_dir / "semiconductor_dashboard_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
