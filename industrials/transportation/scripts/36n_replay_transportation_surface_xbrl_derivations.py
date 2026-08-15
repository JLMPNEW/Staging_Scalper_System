#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import FilingRef, MetricRequest, NormalizedFact, WorkItem  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    ADAPTER_VERSION,
    _surface_xbrl_rules,
    postprocess_metric_evidence,
)
from industrials.transportation.surface_metric_parser import derive_surface_xbrl_evidence  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
)
TARGET_METRICS = ("operating_ratio", "purchased_transportation_ratio")
FIELDS = (
    "source_run_id",
    "source_adapter_version",
    "replay_adapter_version",
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "metric_id",
    "value",
    "unit",
    "period_start",
    "period_end",
    "status",
    "reason",
    "confidence",
    "source_document",
    "formula",
    "paired_context_id",
    "numerator_concept",
    "denominator_concept",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay corrected same-context surface XBRL ratio derivations from "
            "persisted normalized facts; no source document is reopened or reparsed."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--source-run-id", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _fact(row: sqlite3.Row) -> NormalizedFact:
    return NormalizedFact(
        taxonomy=str(row["taxonomy"]),
        concept_name=str(row["concept_name"]),
        value_text=str(row["value_text"] or ""),
        numeric_value=(float(row["numeric_value"]) if row["numeric_value"] is not None else None),
        unit=str(row["unit"] or ""),
        period_start=str(row["period_start"] or ""),
        period_end=str(row["period_end"] or ""),
        context_id=str(row["context_id"] or ""),
        dimensions_json=str(row["dimensions_json"] or "{}"),
        scope=str(row["scope"] or "unknown"),
        source_document=str(row["source_document"] or ""),
        provider=str(row["provider"] or ""),
        decimals=str(row["decimals"] or ""),
        concept_metadata_json=str(row["concept_metadata_json"] or "{}"),
    )


def _item(row: sqlite3.Row, *, company_currency: str) -> WorkItem:
    return WorkItem(
        model_family="transportation",
        adapter_path="industrials.transportation.dedicated_parser_adapter:extract_metric_evidence",
        adapter_version=ADAPTER_VERSION,
        filing=FilingRef(
            ticker=str(row["ticker"]),
            cik=str(row["cik"]),
            accession_number=str(row["accession_number"]),
            form_type=str(row["form_type"]),
            filing_date=str(row["filing_date"] or ""),
            accepted_at=str(row["accepted_at"] or ""),
            report_date=str(row["report_date"] or ""),
            primary_document=str(row["source_document"] or ""),
            source_id="persisted_run_normalized_fact_replay",
            company_currency=company_currency,
        ),
        documents=(),
        requested_metrics=tuple(MetricRequest(metric) for metric in TARGET_METRICS),
        enable_arelle=False,
        enable_edgartools=False,
    )


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    run = connection.execute(
        "SELECT * FROM sec_parser_run WHERE run_id=?",
        (args.source_run_id,),
    ).fetchone()
    if (
        run is None
        or str(run["model_family"]) != "transportation"
        or str(run["status"]) != "COMPLETED"
        or int(run["failed_work_count"] or 0) != 0
        or str(run["asof_date"]) != args.asof
    ):
        raise ValueError("source run must be a matching completed zero-failure transportation run")
    source_adapter_version = str(run["adapter_version"])

    currency_by_ticker = {
        str(row[0]).upper(): str(row[1] or "USD").upper()
        for row in connection.execute(
            "SELECT ticker, COALESCE(NULLIF(currency,''),'USD') FROM dim_company"
        )
    }
    rows = connection.execute(
        "SELECT fact.* FROM sec_parser_run_normalized_fact AS relation "
        "JOIN sec_parser_normalized_fact_shadow AS fact "
        "ON fact.fact_fingerprint=relation.fact_fingerprint "
        "WHERE relation.run_id=? ORDER BY fact.work_key, fact.fact_fingerprint",
        (args.source_run_id,),
    ).fetchall()
    grouped: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["work_key"])].append(row)

    output: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for work_key in sorted(grouped):
        group = grouped[work_key]
        first = group[0]
        item = _item(
            first,
            company_currency=currency_by_ticker.get(str(first["ticker"]).upper(), "USD"),
        )
        derived = derive_surface_xbrl_evidence(
            item,
            tuple(_fact(row) for row in group),
            requested_metrics=set(TARGET_METRICS),
            rules_by_metric=_surface_xbrl_rules(),
        )
        for evidence in postprocess_metric_evidence(item, derived):
            provenance = evidence.provenance or {}
            key = (
                item.filing.ticker,
                evidence.metric_name,
                evidence.period_start,
                evidence.period_end,
                round(float(evidence.value or 0.0), 12),
                provenance.get("paired_context_id", ""),
                evidence.source_document,
            )
            if key in seen:
                continue
            seen.add(key)
            output.append(
                {
                    "source_run_id": args.source_run_id,
                    "source_adapter_version": source_adapter_version,
                    "replay_adapter_version": ADAPTER_VERSION,
                    "ticker": item.filing.ticker,
                    "cik": item.filing.cik,
                    "accession_number": item.filing.accession_number,
                    "form_type": item.filing.form_type,
                    "filing_date": item.filing.filing_date,
                    "accepted_at": item.filing.accepted_at,
                    "metric_id": evidence.metric_name,
                    "value": evidence.value,
                    "unit": evidence.unit,
                    "period_start": evidence.period_start,
                    "period_end": evidence.period_end,
                    "status": evidence.status,
                    "reason": evidence.reason,
                    "confidence": evidence.confidence,
                    "source_document": evidence.source_document,
                    "formula": provenance.get("formula", ""),
                    "paired_context_id": provenance.get("paired_context_id", ""),
                    "numerator_concept": provenance.get("numerator_concept", ""),
                    "denominator_concept": provenance.get("denominator_concept", ""),
                }
            )

    output.sort(
        key=lambda row: (
            str(row["metric_id"]),
            str(row["ticker"]),
            str(row["period_end"]),
            str(row["accession_number"]),
            str(row["paired_context_id"]),
        )
    )
    csv_path = output_dir / "transportation_surface_xbrl_derivation_replay.csv"
    write_csv_atomic(csv_path, FIELDS, output)
    metric_counts = Counter(str(row["metric_id"]) for row in output)
    metric_issuers = {
        metric: len({str(row["ticker"]) for row in output if row["metric_id"] == metric})
        for metric in TARGET_METRICS
    }
    summary: dict[str, Any] = {
        "acceptance": "PASS",
        "asof_date": args.asof,
        "source_run_id": args.source_run_id,
        "source_adapter_version": source_adapter_version,
        "replay_adapter_version": ADAPTER_VERSION,
        "source_normalized_fact_count": len(rows),
        "source_document_reparse_count": 0,
        "derived_evidence_count": len(output),
        "derived_evidence_counts_by_metric": dict(sorted(metric_counts.items())),
        "derived_issuer_counts_by_metric": metric_issuers,
        "status_counts": dict(sorted(Counter(str(row["status"]) for row in output).items())),
        "output_csv": str(csv_path),
        "canonical_candidate_mutation": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "SEMANTICALLY_VALIDATE_REPLAYED_RATIO_DEFINITIONS",
    }
    write_text_atomic(
        output_dir / "transportation_surface_xbrl_derivation_replay.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
