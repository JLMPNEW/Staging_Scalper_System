#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
QA_FIELDS = [
    "asof_date",
    "rank",
    "ticker",
    "company_name",
    "subsector",
    "classification",
    "raw_composite_score",
    "composite_percentile",
    "valuation_score",
    "market_cap",
    "latest_close",
    "shares_outstanding",
    "current_shares_outstanding",
    "diluted_weighted_average_shares",
    "basic_weighted_average_shares",
    "shares_source_concept",
    "shares_source_form",
    "shares_source_period",
    "market_cap_validated_flag",
    "market_cap_qa_status",
    "data_quality_status",
    "missing_fields",
]
TEMPLATE_FIELDS = [
    "ticker",
    "current_shares_outstanding",
    "asof_date",
    "source",
    "note",
    "rank",
    "raw_composite_score",
    "existing_shares_outstanding",
    "existing_shares_source_concept",
    "market_cap",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish med-device share-count and market-cap QA report.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def latest_score_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM med_device_daily_scores").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    if not asof:
        raise RuntimeError("No med_device_daily_scores rows found; run script 13 first.")
    return asof


def market_cap_qa_status(row: dict[str, Any]) -> str:
    if int(row.get("market_cap_validated_flag") or 0):
        return "validated_current_shares"
    concept = str(row.get("shares_source_concept") or "")
    if row.get("diluted_weighted_average_shares") not in {None, ""} or row.get("basic_weighted_average_shares") not in {
        None,
        "",
    }:
        return "needs_current_share_validation"
    if row.get("market_cap") in {None, ""}:
        return "missing_market_cap"
    if "WeightedAverage" in concept:
        return "needs_current_share_validation"
    if not concept:
        return "missing_share_source_concept"
    return "review_share_source"


def load_rows(conn: Any, *, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_financial AS (
            SELECT fv.*
            FROM feature_financial_valuation fv
            JOIN (
                SELECT company_id, MAX(asof_date) AS asof_date
                FROM feature_financial_valuation
                WHERE asof_date <= ?
                GROUP BY company_id
            ) latest
              ON latest.company_id = fv.company_id
             AND latest.asof_date = fv.asof_date
        )
        SELECT
            s.asof_date,
            s.rank,
            c.ticker,
            c.company_name,
            c.subsector,
            s.classification,
            s.raw_composite_score,
            s.composite_percentile,
            s.valuation_score,
            fv.market_cap,
            fv.latest_close,
            fv.shares_outstanding,
            fv.current_shares_outstanding,
            fv.diluted_weighted_average_shares,
            fv.basic_weighted_average_shares,
            fv.shares_source_concept,
            fv.shares_source_form,
            fv.shares_source_period,
            fv.market_cap_validated_flag,
            fv.data_quality_status,
            fv.missing_fields
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        LEFT JOIN latest_financial fv ON fv.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY
            COALESCE(fv.market_cap_validated_flag, 0) ASC,
            s.rank ASC
        """,
        (asof, asof),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["market_cap_qa_status"] = market_cap_qa_status(item)
        out.append(item)
    return out


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def dated_output_dir(base_output_dir: Path, asof: str) -> Path:
    return base_output_dir if base_output_dir.name == asof else base_output_dir / asof


def override_template_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("market_cap_qa_status") != "needs_current_share_validation":
            continue
        out.append(
            {
                "ticker": row.get("ticker") or "",
                "current_shares_outstanding": "",
                "asof_date": row.get("asof_date") or "",
                "source": "",
                "note": "Fill current shares outstanding; copy vetted rows into med_devices/data/share_count_overrides.csv",
                "rank": row.get("rank") or "",
                "raw_composite_score": row.get("raw_composite_score") or "",
                "existing_shares_outstanding": row.get("shares_outstanding") or "",
                "existing_shares_source_concept": row.get("shares_source_concept") or "",
                "market_cap": row.get("market_cap") or "",
            }
        )
    return out


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_base_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"), base_dir=base_dir)
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        asof = args.asof.strip() or latest_score_asof(conn)
        output_dir = dated_output_dir(output_base_dir, asof)
        rows = load_rows(conn, asof=asof)
        qa_csv = output_dir / "med_device_share_count_qa.csv"
        template_csv = output_dir / "med_device_share_count_override_template.csv"
        write_csv(qa_csv, rows, QA_FIELDS)
        template_rows = override_template_rows(rows)
        write_csv(template_csv, template_rows, TEMPLATE_FIELDS)
        needs_review = sum(1 for row in rows if row.get("market_cap_qa_status") == "needs_current_share_validation")
        print(
            f"share_count_qa_csv={qa_csv} asof={asof} rows={len(rows)} "
            f"needs_current_share_validation={needs_review} override_template={template_csv}"
        )


if __name__ == "__main__":
    main()
