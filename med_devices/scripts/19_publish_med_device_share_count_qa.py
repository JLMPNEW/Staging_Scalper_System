#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("publish_med_device_share_count_qa")
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
    "feature_asof_date",
    "feature_market_cap",
    "feature_current_shares_outstanding",
    "feature_shares_source_concept",
    "feature_market_cap_validated_flag",
    "feature_snapshot_divergence",
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
    parser.add_argument(
        "--max-needs-validation",
        type=int,
        default=-1,
        help="Fail (non-zero exit) when needs_current_share_validation rows exceed this count. Negative disables the gate.",
    )
    return parser.parse_args()


def parse_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def latest_score_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM med_device_daily_scores").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    if not asof:
        raise RuntimeError("No med_device_daily_scores rows found; run script 13 first.")
    return asof


def market_cap_qa_status(row: dict[str, Any]) -> str:
    if int(row.get("market_cap_validated_flag") or 0):
        return "validated_current_shares"
    if row.get("market_cap") in {None, ""}:
        return "missing_market_cap"
    concept = str(row.get("shares_source_concept") or "")
    if not concept:
        return "missing_share_source_concept"
    if (
        row.get("diluted_weighted_average_shares") not in {None, ""}
        or row.get("basic_weighted_average_shares") not in {None, ""}
        or "WeightedAverage" in concept
    ):
        return "needs_current_share_validation"
    return "review_share_source"


def feature_snapshot_divergence(row: dict[str, Any]) -> str:
    """Compare the scoring-time share/market-cap snapshot against the current feature row.

    The scores table snapshots these columns at scoring time; feature rows can be rebuilt
    afterwards, so divergence is a reportable QA condition rather than an invisible one.
    """
    if row.get("feature_asof_date") in {None, ""}:
        return "feature_row_missing"
    diverged: list[str] = []
    for score_field, feature_field in (
        ("market_cap", "feature_market_cap"),
        ("current_shares_outstanding", "feature_current_shares_outstanding"),
    ):
        score_value = parse_float(row.get(score_field))
        feature_value = parse_float(row.get(feature_field))
        if score_value is None and feature_value is None:
            continue
        if score_value is None or feature_value is None or not math.isclose(score_value, feature_value, rel_tol=1e-9, abs_tol=1e-6):
            diverged.append(score_field)
    if str(row.get("shares_source_concept") or "") != str(row.get("feature_shares_source_concept") or ""):
        diverged.append("shares_source_concept")
    if int(row.get("market_cap_validated_flag") or 0) != int(row.get("feature_market_cap_validated_flag") or 0):
        diverged.append("market_cap_validated_flag")
    return "diverged:" + "|".join(diverged) if diverged else "match"


def load_rows(conn: Any, *, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_financial AS (
            SELECT fv.*
            FROM feature_financial_valuation fv
            WHERE fv.rowid = (
                SELECT fv2.rowid
                FROM feature_financial_valuation fv2
                WHERE fv2.company_id = fv.company_id
                  AND fv2.asof_date <= ?
                ORDER BY fv2.asof_date DESC, fv2.rowid DESC
                LIMIT 1
            )
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
            s.market_cap,
            fv.latest_close,
            fv.shares_outstanding,
            s.current_shares_outstanding,
            s.diluted_weighted_average_shares,
            s.basic_weighted_average_shares,
            s.shares_source_concept,
            s.shares_source_form,
            s.shares_source_period,
            s.market_cap_validated_flag,
            fv.asof_date AS feature_asof_date,
            fv.market_cap AS feature_market_cap,
            fv.current_shares_outstanding AS feature_current_shares_outstanding,
            fv.shares_source_concept AS feature_shares_source_concept,
            fv.market_cap_validated_flag AS feature_market_cap_validated_flag,
            fv.data_quality_status,
            fv.missing_fields
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        LEFT JOIN latest_financial fv ON fv.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY
            COALESCE(s.market_cap_validated_flag, 0) ASC,
            s.rank ASC
        """,
        (asof, asof),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["market_cap_qa_status"] = market_cap_qa_status(item)
        item["feature_snapshot_divergence"] = feature_snapshot_divergence(item)
        out.append(item)
    return out


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        tmp_name = handle.name
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fieldnames} for row in rows])
    os.replace(tmp_name, path)


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
        run_id = start_run(conn, run_type="publish_med_device_share_count_qa", input_path=config_path)
        try:
            asof = args.asof.strip() or latest_score_asof(conn)
            rows = load_rows(conn, asof=asof)
            if not rows:
                raise RuntimeError(f"No med_device_daily_scores rows found for asof={asof}")
            output_dir = dated_output_dir(output_base_dir, asof)
            qa_csv = output_dir / "med_device_share_count_qa.csv"
            template_csv = output_dir / "med_device_share_count_override_template.csv"
            write_csv(qa_csv, rows, QA_FIELDS)
            template_rows = override_template_rows(rows)
            write_csv(template_csv, template_rows, TEMPLATE_FIELDS)
            needs_review = sum(1 for row in rows if row.get("market_cap_qa_status") == "needs_current_share_validation")
            diverged = sum(1 for row in rows if str(row.get("feature_snapshot_divergence") or "") not in {"", "match"})
            message = (
                f"asof={asof} rows={len(rows)} needs_current_share_validation={needs_review} "
                f"feature_snapshot_divergence={diverged} share_count_qa_csv={qa_csv} override_template={template_csv}"
            )
            if args.max_needs_validation >= 0 and needs_review > args.max_needs_validation:
                raise RuntimeError(
                    f"needs_current_share_validation={needs_review} exceeds --max-needs-validation={args.max_needs_validation} ({message})"
                )
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=message)
            LOGGER.info("Share-count QA complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
