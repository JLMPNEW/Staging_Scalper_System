#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.source_manifest import load_source_manifest  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


ADAPTER = (
    "industrials.transportation.required_metric_parser_adapter:"
    "extract_metric_evidence"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "required_metric_repair"
)
FIELDS = (
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "report_date",
    "primary_document",
    "document_name",
    "content_sha256",
    "cache_status",
    "local_path",
    "is_primary",
    "is_full_submission",
    "source_kind",
    "source_id",
    "company_currency",
    "requested_metric_ids",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze a primary-document/Arelle source manifest for only the "
            "unresolved transportation required financial dependencies."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in csv.DictReader(handle)
        ]


def _cached_primary(
    *,
    cache_root: Path,
    cik: str,
    accession_number: str,
    document_name: str,
) -> Path | None:
    directory = (
        cache_root
        / "sec_archive_xbrl"
        / f"CIK{int(cik):010d}"
        / accession_number.replace("-", "")
    )
    candidate = directory / Path(document_name).name
    if candidate.is_file():
        return candidate.resolve()
    target = Path(document_name).name.lower()
    if directory.is_dir():
        for path in directory.iterdir():
            if path.is_file() and path.name.lower() == target:
                return path.resolve()
    return None


def main() -> int:
    args = parse_args()
    asof_date = str(args.asof)[:10]
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    cache_root = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=config_path.parent,
    )
    output_dir = args.output_root.expanduser().resolve() / asof_date
    accession_path = (
        output_dir / "transportation_required_metric_repair_accessions.csv"
    )
    post_pair_path = (
        output_dir / "transportation_required_metric_repair_post_pairs.csv"
    )
    execution_path = (
        output_dir / "transportation_required_metric_repair_execution.json"
    )
    for path in (accession_path, post_pair_path, execution_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    if execution.get("acceptance") != "PASS_WITH_EXPLICIT_LIMITATIONS":
        raise ValueError("09x repair execution is not ready for residual parsing")
    if execution.get("errors"):
        raise ValueError("09x repair execution contains errors")
    registry = load_registry(ADAPTER)
    parser_metrics = {request.metric_name for request in registry.source_metrics}
    residual_pairs = [
        row
        for row in _read_csv(post_pair_path)
        if row["source_type"] == "financial"
        and row["repair_classification"] != "ALREADY_RESOLVED"
    ]
    requested_by_ticker: dict[str, set[str]] = {}
    for row in residual_pairs:
        dependencies = {
            value
            for value in row["required_dependencies"].split("|")
            if value
        }
        requested_by_ticker.setdefault(row["ticker"], set()).update(
            dependencies & parser_metrics
        )
    accession_rows = [
        row
        for row in _read_csv(accession_path)
        if row["ticker"] in requested_by_ticker
    ]
    connection = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        for accession in accession_rows:
            filing = connection.execute(
                """
                SELECT f.ticker, f.cik, f.accession_number, f.form_type,
                       f.filing_date, f.accepted_at, f.report_date,
                       f.primary_document, f.source_id,
                       COALESCE(NULLIF(c.currency, ''), 'USD') AS currency
                FROM fact_sec_filing AS f
                LEFT JOIN dim_company AS c ON c.ticker=f.ticker
                WHERE f.ticker=? AND f.accession_number=?
                ORDER BY f.filing_date DESC
                LIMIT 1
                """,
                (accession["ticker"], accession["accession_number"]),
            ).fetchone()
            if filing is None:
                errors.append(
                    "missing fact_sec_filing="
                    f"{accession['ticker']}/{accession['accession_number']}"
                )
                continue
            primary_document = str(
                filing["primary_document"]
                or accession["primary_document"]
                or ""
            )
            local_path = _cached_primary(
                cache_root=cache_root,
                cik=str(filing["cik"]),
                accession_number=str(filing["accession_number"]),
                document_name=primary_document,
            )
            if local_path is None:
                errors.append(
                    "missing cached primary document="
                    f"{accession['ticker']}/{accession['accession_number']}/"
                    f"{primary_document}"
                )
                continue
            rows.append(
                {
                    "ticker": filing["ticker"],
                    "cik": filing["cik"],
                    "accession_number": filing["accession_number"],
                    "form_type": filing["form_type"],
                    "filing_date": filing["filing_date"],
                    "accepted_at": filing["accepted_at"],
                    "report_date": filing["report_date"],
                    "primary_document": primary_document,
                    "document_name": local_path.name,
                    "content_sha256": file_sha256(local_path),
                    "cache_status": "CACHED_HASHED",
                    "local_path": str(local_path),
                    "is_primary": 1,
                    "is_full_submission": 0,
                    "source_kind": "sec_archive_primary",
                    "source_id": filing["source_id"],
                    "company_currency": str(
                        filing["currency"] or "USD"
                    ).upper(),
                    "requested_metric_ids": "|".join(
                        sorted(requested_by_ticker[accession["ticker"]])
                    ),
                }
            )
    finally:
        connection.close()
    source_path = (
        output_dir
        / "transportation_required_metric_parser_source_manifest.csv"
    )
    write_csv_atomic(source_path, FIELDS, rows)
    source = load_source_manifest(source_path) if rows else None
    expected_keys = {
        (row["ticker"], row["accession_number"]) for row in accession_rows
    }
    actual_keys = {
        (str(row["ticker"]), str(row["accession_number"])) for row in rows
    }
    if expected_keys != actual_keys:
        errors.append(
            "source manifest accession mismatch "
            f"missing={sorted(expected_keys - actual_keys)[:20]} "
            f"extra={sorted(actual_keys - expected_keys)[:20]}"
        )
    if len(residual_pairs) != 31:
        errors.append(
            f"financial residual pair count={len(residual_pairs)} expected=31"
        )
    if len(requested_by_ticker) != 18:
        errors.append(
            "financial residual ticker count="
            f"{len(requested_by_ticker)} expected=18"
        )
    if len(rows) != 178:
        errors.append(f"source document count={len(rows)} expected=178")
    request_counts = Counter(
        metric
        for metrics in requested_by_ticker.values()
        for metric in metrics
    )
    manifest_path = (
        output_dir / "transportation_required_metric_parser_plan.json"
    )
    payload: dict[str, Any] = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "TRANSPORTATION_REQUIRED_METRIC_PARSER_PLAN",
        "asof_date": asof_date,
        "adapter": ADAPTER,
        "adapter_version": registry.adapter_version,
        "database_read_only": True,
        "network_requests": 0,
        "parser_invocations": 0,
        "financial_residual_pair_count": len(residual_pairs),
        "financial_ticker_count": len(requested_by_ticker),
        "accession_count": len(actual_keys),
        "primary_document_count": len(rows),
        "requested_dependency_ticker_counts": dict(
            sorted(request_counts.items())
        ),
        "source_manifest": {
            "path": str(source_path.resolve()),
            "sha256": file_sha256(source_path),
            "row_count": len(rows),
            "direct_document_mode": bool(
                source and source.direct_document_mode
            ),
        },
        "sealed_inputs": {
            "post_pairs": {
                "path": str(post_pair_path.resolve()),
                "sha256": file_sha256(post_pair_path),
            },
            "accessions": {
                "path": str(accession_path.resolve()),
                "sha256": file_sha256(accession_path),
            },
            "execution": {
                "path": str(execution_path.resolve()),
                "sha256": file_sha256(execution_path),
            },
        },
        "automatic_extension_promotion_authorized": False,
        "production_promotion_authorized": False,
        "errors": errors,
        "next_gate": (
            "RUN_ONE_RESIDUAL_ARELLE_SHADOW_PARSE"
            if not errors
            else "REPAIR_REQUIRED_METRIC_PARSER_PLAN"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
