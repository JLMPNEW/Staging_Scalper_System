#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    ADAPTER_VERSION,
    metric_search_aliases,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


TARGET_METRICS = (
    "revenue_days",
    "fleet_age",
    "charter_coverage_next_12m",
    "contracted_revenue_backlog",
)
OUTPUT_FIELDS = (
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "document_name",
    "document_description",
    "document_sha256",
    "file_size",
    "matched_metric_ids",
    "matched_anchors",
)
_ALLOWED_SUFFIXES = frozenset({".htm", ".html", ".xhtml", ".txt"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the complete already-cached tanker 6-K/8-K candidate set "
            "for four additional specialized-metric anchor families."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _event_candidates(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    # Scan both already-included and still-excluded supplemental events.  If
    # included positives were omitted, rewriting the audit would drop the
    # prior positive set and make consecutive census runs oscillate.
    return [
        row
        for row in rows
        if row.get("candidate_type") == "supplemental_event"
        and row.get("index_status") == "CACHED"
    ]


def _normalized_phrase(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _patterns() -> dict[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    aliases = metric_search_aliases()
    extras = {
        "revenue_days": (
            ("available days and off-hire days", ("available days", "off hire days")),
            ("available days and drydock days", ("available days", "drydock days")),
        ),
        "fleet_age": (
            ("year built and dwt", ("year built", "dwt")),
            ("year built and capacity", ("year built", "capacity")),
        ),
        "charter_coverage_next_12m": (
            ("percent covered", ("percent covered",)),
            ("fixed days and available days", ("fixed days", "available days")),
        ),
        "contracted_revenue_backlog": (
            ("minimum lease payments receivable", ("minimum lease payments receivable",)),
            ("future charter hire receipts", ("future charter hire receipts",)),
        ),
    }
    output: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {}
    for metric in TARGET_METRICS:
        rules = [
            (alias, (_normalized_phrase(alias),))
            for alias in aliases.get(metric, ())
            if _normalized_phrase(alias)
        ]
        rules.extend(extras[metric])
        output[metric] = tuple(rules)
    return output


def _index_items(accession_dir: Path) -> list[dict[str, str]]:
    path = accession_dir / "index.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    output: list[dict[str, str]] = []
    for raw in ((payload.get("directory") or {}).get("item") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name or Path(name).name != name or Path(name).suffix.lower() not in _ALLOWED_SUFFIXES:
            continue
        output.append(
            {
                "name": name,
                "description": str(raw.get("description") or raw.get("title") or "").strip(),
            }
        )
    return output


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, "transportation")
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            family["dedicated_parser"]["tanker_delta_output_root"],
            base_dir=config_path.parent,
        )
        / args.asof
    )
    decisions_path = output_dir / "transportation_tanker_delta_source_decisions.csv"
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=config_path.parent,
    )
    patterns = _patterns()
    candidates = _event_candidates(_rows(decisions_path))

    output: list[dict[str, object]] = []
    scanned_documents = 0
    scanned_bytes = 0
    for row in candidates:
        cik = str(row["cik"])
        accession = str(row["accession_number"])
        accession_dir = (
            cache_dir
            / "sec_archive_xbrl"
            / f"CIK{cik}"
            / accession.replace("-", "")
        )
        for item in _index_items(accession_dir):
            path = accession_dir / item["name"]
            if not path.is_file():
                continue
            payload = path.read_bytes()
            scanned_documents += 1
            scanned_bytes += len(payload)
            # This is an anchor census, not value extraction.  Avoid the
            # expensive semantic parser and search a whitespace-normalized
            # rendering of the already-cached bytes.  The selected hits are
            # parsed semantically only after they enter the governed corpus.
            text = payload.decode("utf-8", errors="ignore").casefold()
            text = " ".join(re.findall(r"[a-z0-9]+", text))
            matched: dict[str, list[str]] = {}
            for metric, rules in patterns.items():
                labels = [
                    label
                    for label, required_phrases in rules
                    if all(phrase in text for phrase in required_phrases)
                ]
                if labels:
                    matched[metric] = labels
            if not matched:
                continue
            output.append(
                {
                    "ticker": row["ticker"],
                    "cik": cik,
                    "accession_number": accession,
                    "form_type": row["form_type"],
                    "filing_date": row["filing_date"],
                    "document_name": item["name"],
                    "document_description": item["description"],
                    "document_sha256": hashlib.sha256(payload).hexdigest(),
                    "file_size": len(payload),
                    "matched_metric_ids": "|".join(sorted(matched)),
                    "matched_anchors": json.dumps(matched, sort_keys=True, separators=(",", ":")),
                }
            )

    output.sort(
        key=lambda row: (
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
            str(row["document_name"]),
        )
    )
    csv_path = output_dir / "transportation_tanker_excluded_event_anchor_audit.csv"
    manifest_path = output_dir / "transportation_tanker_excluded_event_anchor_audit.json"
    write_csv_atomic(csv_path, OUTPUT_FIELDS, output)
    manifest = {
        "acceptance": "PASS",
        "adapter_version": ADAPTER_VERSION,
        "asof_date": args.asof,
        "network_requests": 0,
        "audited_cached_accession_count": len(candidates),
        "included_candidate_accession_count": sum(
            row.get("decision") == "INCLUDE" for row in candidates
        ),
        "excluded_candidate_accession_count": sum(
            row.get("decision", "").startswith("EXCLUDE") for row in candidates
        ),
        "scanned_document_count": scanned_documents,
        "scanned_bytes": scanned_bytes,
        "positive_document_count": len(output),
        "positive_accession_count": len({(row["ticker"], row["accession_number"]) for row in output}),
        "positive_metric_document_counts": dict(
            sorted(
                Counter(
                    metric
                    for row in output
                    for metric in str(row["matched_metric_ids"]).split("|")
                    if metric
                ).items()
            )
        ),
        "scan_method": "raw_cached_text_anchor_census_complete_event_candidate_set",
        "output_csv": str(csv_path),
        "semantic_validation_authorized": True,
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
    }
    write_text_atomic(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
