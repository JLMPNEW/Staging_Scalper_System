#!/usr/bin/env python3
"""Audit cached ASC annual operating-income bridge components without parsing."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "asc_operating_bridge" / "2026-08-15"
FIELDS = (
    "period_end", "filing_date", "accepted_at", "accession_number", "form_type",
    "taxonomy", "concept_name", "unit", "raw_value", "source_detail", "frame",
)
TERMS = (
    "revenue", "voyage", "vesseloperating", "charterhire", "leaseexpense",
    "depreciation", "drydock", "generalandadministrative", "corporate",
    "commercial", "derivative", "pretax", "beforetax", "interestexpense",
    "interestincome", "debtextinguishment", "operatingincome",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT period_end,filing_date,accepted_at,accession_number,form_type,
               taxonomy,concept_name,unit,raw_value,source_detail,frame,period_start
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker='ASC' AND period_end BETWEEN '2019-12-31' AND '2024-12-31'
          AND raw_value IS NOT NULL
          AND form_type IN ('20-F','20-F/A')
        ORDER BY period_end,filing_date,taxonomy,concept_name
        """
    ).fetchall()
    connection.close()
    selected = []
    for row in rows:
        concept = "".join(character.lower() for character in str(row["concept_name"]) if character.isalnum())
        if not any(term in concept for term in TERMS):
            continue
        start = str(row["period_start"] or "")[:10]
        end = str(row["period_end"] or "")[:10]
        if start and end:
            try:
                from datetime import date
                days = (date.fromisoformat(end) - date.fromisoformat(start)).days
            except ValueError:
                days = 0
            if days and not 300 <= days <= 380:
                continue
        selected.append({field: row[field] for field in FIELDS})
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "transportation_v5_asc_operating_bridge_candidates.csv"
    json_path = output_dir / "transportation_v5_asc_operating_bridge_audit.json"
    write_csv_atomic(csv_path, FIELDS, selected)
    periods = sorted({str(row["period_end"]) for row in selected})
    concepts = sorted({f"{row['taxonomy']}:{row['concept_name']}" for row in selected})
    payload = {
        "acceptance": "PASS" if len(periods) == 6 else "FAIL",
        "contract_version": "transportation_v5_asc_operating_bridge_audit_v1",
        "periods": periods,
        "candidate_row_count": len(selected),
        "candidate_concepts": concepts,
        "network_requests": 0,
        "parser_invocations": 0,
        "database_mutations": 0,
        "csv": str(csv_path),
    }
    write_text_atomic(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
