#!/usr/bin/env python3
"""Compute and validate ASC annual operating-income bridges from extracted tables."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402


DEFAULT_EXTRACT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "asc_operating_bridge" / "2026-08-15" / "transportation_v5_asc_operating_tables.json"
DEFAULT_OUTPUT_DIR = DEFAULT_EXTRACT.parent
FIELDS = ("report_date", "operating_income", "component_count", "revenue", "scale", "validation_status")
KNOWN_FY2025 = 47_189_000.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract", type=Path, default=DEFAULT_EXTRACT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def normalized_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u200b", " ")).strip()


def first_current_value(cells: list[str]) -> tuple[float, str] | None:
    for raw in cells[1:]:
        value = normalized_label(raw)
        if not value or value in {"$", "USD", "US$"}:
            continue
        if "%" in value:
            continue
        if not re.search(r"[0-9]", value):
            return 0.0, value
        negative = value.startswith("(")
        cleaned = re.sub(r"[^0-9.]", "", value)
        if not cleaned:
            continue
        number = float(cleaned)
        return (-number if negative else number), value
    return None


def compute_bridge(document: dict[str, Any]) -> dict[str, Any]:
    rows = [list(row) for row in document["rows"]]
    labels = [normalized_label(str(row[0])).lower() if row else "" for row in rows]
    revenue_index = next(index for index, label in enumerate(labels) if label.startswith("revenue, net"))
    interest_index = next(index for index, label in enumerate(labels) if label.startswith("interest expense"))
    pretax_index = next(index for index, label in enumerate(labels) if "before tax" in label)
    if not revenue_index < interest_index < pretax_index:
        raise ValueError(f"{document['report_date']}: invalid bridge row order")
    scale = 1000.0 if any("in thousands" in label for label in labels[: revenue_index + 1]) else 1.0
    components = []
    for row in rows[revenue_index:interest_index]:
        parsed = first_current_value(row)
        if parsed is None:
            continue
        value, display = parsed
        components.append({
            "label": normalized_label(str(row[0])),
            "display_value": display,
            "signed_value": value * scale,
        })
    revenue = next((item["signed_value"] for item in components if item["label"].lower().startswith("revenue, net")), None)
    if revenue is None or revenue <= 0 or len(components) < 7:
        raise ValueError(f"{document['report_date']}: incomplete operating bridge")
    operating_income = sum(float(item["signed_value"]) for item in components)

    cross = []
    for row in rows[interest_index : pretax_index + 1]:
        label = normalized_label(str(row[0]))
        parsed = first_current_value(row)
        if parsed is None:
            continue
        value, display = parsed
        normalized = label.lower()
        if normalized.startswith("interest expense"):
            signed = abs(value)
        elif "extinguishment" in normalized:
            signed = -value
        elif normalized.startswith("interest income"):
            signed = -value
        elif "before tax" in normalized:
            signed = value
        else:
            continue
        cross.append({
            "label": label,
            "display_value": display,
            "signed_value": signed * scale,
        })
    if len(cross) < 3:
        raise ValueError(f"{document['report_date']}: incomplete independent cross-check")
    cross_value = sum(float(item["signed_value"]) for item in cross)
    if abs(cross_value - operating_income) >= 0.5:
        raise ValueError(
            f"{document['report_date']}: operating bridge={operating_income} cross={cross_value}"
        )
    return {
        "report_date": document["report_date"],
        "filing_date": document["filing_date"],
        "accepted_at": document["accepted_at"],
        "accession_number": document["accession_number"],
        "form_type": document["form_type"],
        "document_path": document["path"],
        "document_sha256": document["sha256"],
        "table_index": document["table_index"],
        "scale": scale,
        "revenue": revenue,
        "operating_income": operating_income,
        "components": components,
        "cross_check_components": cross,
    }


def main() -> int:
    args = parse_args()
    extract_path = args.extract.expanduser().resolve()
    extract = json.loads(extract_path.read_text(encoding="utf-8"))
    if extract.get("acceptance") != "PASS":
        raise ValueError("ASC table extract is not accepted")
    bridges = [compute_bridge(dict(document)) for document in extract["documents"]]
    current = next(bridge for bridge in bridges if bridge["report_date"] == "2025-12-31")
    current_matches = abs(float(current["operating_income"]) - KNOWN_FY2025) < 0.5
    errors = [] if current_matches else [
        f"FY2025 bridge={current['operating_income']} expected={KNOWN_FY2025}"
    ]
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "transportation_v5_asc_operating_bridge_values.csv"
    json_path = output_dir / "transportation_v5_asc_operating_bridge_validation.json"
    write_csv_atomic(csv_path, FIELDS, [
        {
            "report_date": bridge["report_date"],
            "operating_income": bridge["operating_income"],
            "component_count": len(bridge["components"]),
            "revenue": bridge["revenue"],
            "scale": bridge["scale"],
            "validation_status": "PASS" if bridge["report_date"] != "2025-12-31" or current_matches else "FAIL",
        }
        for bridge in bridges
    ])
    payload = {
        "acceptance": "PASS" if not errors else "FAIL",
        "review_status": "ACCEPTED" if not errors else "REJECTED",
        "contract_version": "transportation_v5_asc_operating_bridge_validation_v1",
        "bridges": bridges,
        "fy2025_known_reviewed_value": KNOWN_FY2025,
        "fy2025_exact_reconciliation": current_matches,
        "errors": errors,
        "network_requests": 0,
        "parser_invocations": 0,
        "database_mutations": 0,
        "source_extract": str(extract_path),
    }
    write_text_atomic(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "acceptance": payload["acceptance"],
        "bridge_count": len(bridges),
        "fy2025_exact_reconciliation": current_matches,
        "values": {bridge["report_date"]: bridge["operating_income"] for bridge in bridges},
        "output": str(json_path),
    }, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
