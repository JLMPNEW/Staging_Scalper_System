#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
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
SCORE_FIELDS = [
    "asof_date",
    "scoring_model_version",
    "rank",
    "ticker",
    "company_name",
    "subsector",
    "composite_score",
    "fundamental_quality_score",
    "durable_growth_score",
    "fda_product_score",
    "reimbursement_score",
    "valuation_score",
    "technical_entry_score",
    "sentiment_catalyst_score",
    "value_trap_score",
    "data_completeness_score",
    "live_component_count",
    "classification",
    "gate_status",
    "review_reason",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "fda_state",
    "fda_data_available",
    "top_positive_drivers",
    "top_negative_drivers",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish post-change med-device score review pack.")
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


def load_score_rows(conn: Any, *, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_fda AS (
            SELECT f.*
            FROM feature_fda_product_risk f
            JOIN (
                SELECT company_id, MAX(asof_date) AS asof_date
                FROM feature_fda_product_risk
                WHERE asof_date <= ?
                GROUP BY company_id
            ) latest
              ON latest.company_id = f.company_id
             AND latest.asof_date = f.asof_date
        )
        SELECT
            s.*,
            c.ticker,
            c.company_name,
            c.subsector,
            COALESCE(latest_fda.review_adjusted_fda_state, '') AS fda_state,
            COALESCE(latest_fda.fda_data_available, 0) AS fda_data_available
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        LEFT JOIN latest_fda ON latest_fda.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY s.rank
        """,
        (asof, asof),
    ).fetchall()
    return [dict(row) for row in rows]


def decode_driver_list(raw: object) -> str:
    try:
        value = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return str(raw or "")
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return str(value)


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["top_positive_drivers"] = decode_driver_list(item.get("top_positive_drivers_json"))
    item["top_negative_drivers"] = decode_driver_list(item.get("top_negative_drivers_json"))
    return {field: item.get(field, "") for field in SCORE_FIELDS}


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def classification_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        classification = str(row.get("classification") or "unclassified")
        counts[classification] = counts.get(classification, 0) + 1
    return [
        {"classification": classification, "count": count}
        for classification, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def write_markdown(path: Path, *, rows: list[dict[str, Any]], counts: list[dict[str, Any]], asof: str) -> None:
    model_version = str(rows[0].get("scoring_model_version") or "") if rows else ""
    tier1 = [row for row in rows if row.get("classification") == "tier_1_long_candidate"]
    manual = [row for row in rows if row.get("classification") == "manual_review_regulatory_risk"]
    top25 = rows[:25]
    bottom25 = list(reversed(rows[-25:]))

    def line_items(items: list[dict[str, Any]], *, include_reason: bool = False) -> list[str]:
        out: list[str] = []
        for row in items:
            base = (
                f"- {row.get('rank')}. {row.get('ticker')} "
                f"{float(row.get('composite_score') or 0.0):.2f} "
                f"({row.get('classification')})"
            )
            if include_reason:
                base += f" - {row.get('review_reason') or row.get('hard_red_flag_reasons') or 'no reason'}"
            out.append(base)
        return out

    content = [
        f"# Med Device Score Review Pack - {asof}",
        "",
        f"Scoring model version: `{model_version}`",
        "",
        "## Classification Counts",
        *[f"- {row['classification']}: {row['count']}" for row in counts],
        "",
        "## Tier-1 Long Candidates",
        *(line_items(tier1) or ["- None"]),
        "",
        "## Manual Regulatory Risk",
        *(line_items(manual, include_reason=True) or ["- None"]),
        "",
        "## Top 25",
        *line_items(top25),
        "",
        "## Bottom 25",
        *line_items(bottom25, include_reason=True),
        "",
    ]
    path.write_text("\n".join(content), encoding="utf-8")


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"), base_dir=base_dir)
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        asof = args.asof.strip() or latest_score_asof(conn)
        rows = load_score_rows(conn, asof=asof)
        if not rows:
            raise RuntimeError(f"No med_device_daily_scores rows found for {asof}")
        counts = classification_counts(rows)
        clean_rows = [clean_row(row) for row in rows]
        tier1 = [row for row in clean_rows if row["classification"] == "tier_1_long_candidate"]
        manual = [row for row in clean_rows if row["classification"] == "manual_review_regulatory_risk"]
        top25 = clean_rows[:25]
        bottom25 = list(reversed(clean_rows[-25:]))

        suffix = asof.replace("-", "")
        write_csv(output_dir / f"med_device_score_review_all_{suffix}.csv", clean_rows, SCORE_FIELDS)
        write_csv(output_dir / f"med_device_score_review_tier1_{suffix}.csv", tier1, SCORE_FIELDS)
        write_csv(output_dir / f"med_device_score_review_manual_regulatory_{suffix}.csv", manual, SCORE_FIELDS)
        write_csv(output_dir / f"med_device_score_review_top25_{suffix}.csv", top25, SCORE_FIELDS)
        write_csv(output_dir / f"med_device_score_review_bottom25_{suffix}.csv", bottom25, SCORE_FIELDS)
        write_csv(output_dir / f"med_device_score_review_classification_counts_{suffix}.csv", counts, ["classification", "count"])
        write_markdown(output_dir / f"med_device_score_review_pack_{suffix}.md", rows=clean_rows, counts=counts, asof=asof)
        print(f"review_pack_dir={output_dir} asof={asof} rows={len(rows)} tier1={len(tier1)} manual_regulatory={len(manual)}")


if __name__ == "__main__":
    main()
