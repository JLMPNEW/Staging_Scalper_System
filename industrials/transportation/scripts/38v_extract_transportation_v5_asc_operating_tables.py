#!/usr/bin/env python3
"""Extract ASC annual operating-result tables from the sealed local filing cache."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402


DEFAULT_CENSUS = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_dedicated_parser_source_census.csv"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "asc_operating_bridge" / "2026-08-15"
FIELDS = ("report_date", "filing_date", "accession_number", "document_name", "document_sha256", "table_index", "anchor_count", "row_index", "cells_json", "ix_facts_json")
ANCHORS = (
    "revenue, net", "voyage expenses", "vessel operating expenses",
    "depreciation", "amortization of deferred drydock", "corporate",
    "commercial and chartering", "interest expense", "income before income taxes",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    with args.census.expanduser().resolve().open("r", encoding="utf-8-sig", newline="") as handle:
        census = list(csv.DictReader(handle))
    documents = [
        row for row in census
        if row.get("ticker") == "ASC"
        and row.get("form_type") == "20-F"
        and row.get("is_primary") == "1"
        and "2019-12-31" <= str(row.get("report_date") or "") <= "2025-12-31"
    ]
    output_rows = []
    document_results = []
    errors = []
    for document in sorted(documents, key=lambda row: str(row["report_date"])):
        path = Path(str(document["local_path"])).resolve()
        expected_hash = str(document["content_sha256"])
        actual_hash = sha256(path)
        if actual_hash != expected_hash:
            errors.append(f"document hash mismatch={path}")
            continue
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
        ranked = []
        for table_index, table in enumerate(soup.find_all("table")):
            text = normalize(table.get_text(" ", strip=True)).lower()
            anchor_count = sum(anchor in text for anchor in ANCHORS)
            if anchor_count >= 3:
                ranked.append((anchor_count, table_index, table))
        if not ranked:
            errors.append(f"no operating table={path}")
            continue
        anchor_count, table_index, table = max(ranked, key=lambda item: (item[0], -item[1]))
        table_rows = []
        for row_index, tr in enumerate(table.find_all("tr")):
            cells = [normalize(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
            cells = [cell for cell in cells if cell]
            if not cells:
                continue
            ix_facts = []
            for fact in tr.find_all(lambda tag: tag.name and tag.name.lower().endswith("nonfraction")):
                ix_facts.append({
                    "name": str(fact.get("name") or ""),
                    "contextref": str(fact.get("contextref") or ""),
                    "unitref": str(fact.get("unitref") or ""),
                    "scale": str(fact.get("scale") or ""),
                    "sign": str(fact.get("sign") or ""),
                    "text": normalize(fact.get_text(" ", strip=True)),
                })
            table_rows.append(cells)
            output_rows.append({
                "report_date": document["report_date"],
                "filing_date": document["filing_date"],
                "accession_number": document["accession_number"],
                "document_name": document["document_name"],
                "document_sha256": actual_hash,
                "table_index": table_index,
                "anchor_count": anchor_count,
                "row_index": row_index,
                "cells_json": json.dumps(cells, ensure_ascii=False),
                "ix_facts_json": json.dumps(ix_facts, ensure_ascii=False),
            })
        document_results.append({
            "report_date": document["report_date"],
            "filing_date": document["filing_date"],
            "accepted_at": document["accepted_at"],
            "accession_number": document["accession_number"],
            "form_type": document["form_type"],
            "path": str(path),
            "sha256": actual_hash,
            "table_index": table_index,
            "anchor_count": anchor_count,
            "rows": table_rows,
        })
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "transportation_v5_asc_operating_tables.csv"
    json_path = output_dir / "transportation_v5_asc_operating_tables.json"
    write_csv_atomic(csv_path, FIELDS, output_rows)
    payload = {
        "acceptance": "PASS" if not errors and len(document_results) == 7 else "FAIL",
        "contract_version": "transportation_v5_asc_operating_table_extract_v1",
        "document_count": len(document_results),
        "documents": document_results,
        "errors": errors,
        "network_requests": 0,
        "parser_invocations": len(document_results),
        "database_mutations": 0,
        "csv": str(csv_path),
    }
    write_text_atomic(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "acceptance": payload["acceptance"],
        "document_count": len(document_results),
        "row_count": len(output_rows),
        "errors": errors,
        "output": str(json_path),
    }, indent=2))
    return 0 if payload["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
