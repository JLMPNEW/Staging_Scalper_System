from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestration_contracts.financial_lineage import (
    LINEAGE_FIELDS,
    evaluate_financial_lineage_rows,
    evaluation_manifest,
    policy_for_model_family,
)
from technology.core.financial_filing_lineage import build_financial_filing_lineage


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _readonly_connect(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _write_atomic(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix or ".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with open(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _csv_text(fieldnames: Iterable[str], rows: Iterable[Mapping[str, Any]]) -> str:
    from io import StringIO

    handle = StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue()


def read_rank_rows(path: Path, *, model_family: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError(f"{model_family} rank table is empty: {path}")
    required = {
        "ticker",
        "asof_date",
        "portfolio_candidate_gate",
        "rank_ready_flag",
    }
    missing = sorted(required.difference(rows[0]))
    if missing:
        raise ValueError(f"{model_family} rank table lacks shadow lineage fields={missing}: {path}")
    tickers = [str(row.get("ticker") or "").strip().upper() for row in rows]
    if any(not ticker for ticker in tickers):
        raise ValueError(f"{model_family} rank table contains a blank ticker: {path}")
    if len(tickers) != len(set(tickers)):
        raise ValueError(f"{model_family} rank table contains duplicate tickers: {path}")
    return rows


def build_financial_lineage_shadow(
    *,
    db_path: Path,
    rank_table_path: Path,
    output_dir: Path,
    model_family: str = "semiconductors",
    expected_asof: str = "",
    policy_context: str = "research",
    retrospective_source_discovery_max_days: int = 0,
) -> dict[str, Any]:
    if retrospective_source_discovery_max_days < 0:
        raise ValueError("retrospective_source_discovery_max_days must be non-negative")
    rank_rows = read_rank_rows(rank_table_path, model_family=model_family)
    row_dates = {str(row.get("asof_date") or "").strip() for row in rank_rows}
    if len(row_dates) != 1:
        raise ValueError(f"{model_family} rank table must contain one as-of date; found={sorted(row_dates)}")
    asof = next(iter(row_dates))
    if expected_asof and asof != expected_asof:
        raise ValueError(
            f"{model_family} shadow as-of mismatch rank={asof} expected={expected_asof}"
        )
    tickers = [str(row["ticker"]).strip().upper() for row in rank_rows]
    with _readonly_connect(db_path) as conn:
        lineage = build_financial_filing_lineage(
            conn,
            model_family=model_family,
            asof=asof,
            tickers=tickers,
            retrospective_source_discovery_max_days=(
                retrospective_source_discovery_max_days
            ),
        )

    shadow_rows: list[dict[str, str]] = []
    for source in rank_rows:
        ticker = str(source["ticker"]).strip().upper()
        evidence = lineage.get(ticker)
        if evidence is None:
            raise ValueError(
                f"Shared lineage producer omitted {model_family} ticker={ticker}"
            )
        item = dict(source)
        item.update({field: str(evidence.get(field) or "") for field in LINEAGE_FIELDS})
        shadow_rows.append(item)

    policy = policy_for_model_family(model_family)
    candidate_fields = ("portfolio_candidate_gate", "rank_ready_flag")
    evaluation = evaluate_financial_lineage_rows(
        shadow_rows,
        policy_mode=policy.mode_for_asof(policy_context, asof),
        expected_asof=asof,
        min_core_metric_count=policy.min_core_metric_count,
        candidate_fields=candidate_fields,
    )
    fields = list(rank_rows[0])
    fields.extend(field for field in LINEAGE_FIELDS if field not in fields)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_stem = "semiconductor" if model_family == "semiconductors" else model_family
    csv_path = output_dir / f"{artifact_stem}_financial_lineage_shadow.csv"
    manifest_path = output_dir / f"{artifact_stem}_financial_lineage_shadow.json"
    _write_atomic(csv_path, _csv_text(fields, shadow_rows))
    candidate_rows = [
        row for row in shadow_rows if any(str(row.get(field) or "").strip() == "1" for field in candidate_fields)
    ]
    manifest = {
        "schema_version": f"{artifact_stem}_financial_lineage_shadow_v1",
        "generated_at_utc": _utc_now(),
        "model_family": model_family,
        "asof_date": asof,
        "database_path": str(db_path.expanduser().resolve()),
        "database_access": "sqlite_read_only_query_only",
        "source_rank_table": str(rank_table_path.expanduser().resolve()),
        "output_path": str(csv_path.resolve()),
        "output_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "production_rank_table_modified": False,
        "candidate_definition_fields": list(candidate_fields),
        "candidate_count": len(candidate_rows),
        "candidate_incorporated_count": sum(
            str(row.get("financial_lineage_gate") or "") == "1" for row in candidate_rows
        ),
        "retrospective_source_discovery_max_days": (
            retrospective_source_discovery_max_days
        ),
        "retrospective_source_discovery_count": sum(
            "retrospective_sec_submissions_discovery_confirmed"
            in str(row.get("financial_lineage_reason") or "")
            for row in shadow_rows
        ),
        **evaluation_manifest(evaluation, policy=policy, context=policy_context),
    }
    _write_atomic(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest
