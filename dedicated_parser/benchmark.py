from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

from dedicated_parser.atomic_io import atomic_text_writer, atomic_write_text
from typing import Any, Iterable

from dedicated_parser.adapters import load_ticker_selector
from dedicated_parser.contracts import AdapterRegistry, stable_hash
from dedicated_parser.planner import (
    active_tickers,
    unresolved_requests,
)


def _database_has_source_fact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    metric_name: str,
    asof_date: str,
) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM fact_sec_xbrl_fact
            WHERE ticker = ? AND canonical_metric = ?
              AND SUBSTR(
                    COALESCE(NULLIF(accepted_at, ''), filing_date),
                    1,
                    10
                  ) <= ?
              AND value IS NOT NULL
            LIMIT 1
            """,
            (ticker, metric_name, asof_date),
        ).fetchone()
        is not None
    )


def rank_missing_metric_tickers(
    conn: sqlite3.Connection,
    *,
    registry: AdapterRegistry,
    adapter_path: str,
    asof_date: str,
    limit: int,
    tickers: Iterable[str] | None = None,
    minimum_missing: int = 1,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("Benchmark cohort limit must be at least 1")
    if minimum_missing < 0:
        raise ValueError("minimum_missing cannot be negative")
    selector = load_ticker_selector(adapter_path)
    selected_universe = sorted(
        {
            str(ticker).strip().upper()
            for ticker in (
                tickers
                if tickers is not None
                else selector(conn, asof_date)
                if selector is not None
                else active_tickers(
                    conn,
                    model_family=registry.model_family,
                    asof_date=asof_date,
                )
            )
            if str(ticker).strip()
        }
    )
    unresolved = unresolved_requests(
        conn,
        registry=registry,
        asof_date=asof_date,
        tickers=selected_universe,
    )
    ranked: list[dict[str, Any]] = []
    for ticker in selected_universe:
        metrics = sorted(
            metric_name
            for metric_name in unresolved.get(ticker, set())
            if not _database_has_source_fact(
                conn,
                ticker=ticker,
                metric_name=metric_name,
                asof_date=asof_date,
            )
        )
        ranked.append(
            {
                "ticker": ticker,
                "missing_metric_count": len(metrics),
                "missing_metrics": metrics,
            }
        )
    ranked.sort(
        key=lambda row: (
            -int(row["missing_metric_count"]),
            str(row["ticker"]),
        )
    )
    eligible = [
        row
        for row in ranked
        if int(row["missing_metric_count"]) >= minimum_missing
    ]
    if len(eligible) < limit:
        raise ValueError(
            f"Only {len(eligible)} tickers meet minimum_missing="
            f"{minimum_missing}; cannot build a {limit}-ticker cohort"
        )
    selected = [
        {**row, "rank": rank}
        for rank, row in enumerate(eligible[:limit], start=1)
    ]
    metric_counts = Counter(
        metric_name
        for row in selected
        for metric_name in row["missing_metrics"]
    )
    distribution = Counter(
        int(row["missing_metric_count"]) for row in ranked
    )
    payload = {
        "cohort_id": (
            f"{registry.model_family}_most_missing_parser_metrics_"
            f"{limit}_{asof_date}"
        ),
        "model_family": registry.model_family,
        "asof_date": asof_date,
        "adapter_version": registry.adapter_version,
        "selection_rule": (
            "active universe ranked by descending unresolved applicable "
            "parser source-metric count, then ticker ascending"
        ),
        "supported_source_metrics": [
            request.metric_name for request in registry.source_metrics
        ],
        "universe_ticker_count": len(selected_universe),
        "cohort_size": len(selected),
        "minimum_missing": minimum_missing,
        "missing_count_distribution": {
            str(count): ticker_count
            for count, ticker_count in sorted(distribution.items())
        },
        "selected_missing_metric_counts": dict(sorted(metric_counts.items())),
        "selected_tickers": [str(row["ticker"]) for row in selected],
        "rows": selected,
    }
    payload["selection_sha256"] = stable_hash(payload)
    return payload


def load_cohort_tickers(path: Path) -> list[str]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Ticker cohort does not exist: {resolved}")
    if resolved.suffix.lower() == ".json":
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{resolved}: cohort JSON must be an object")
        values = payload.get("selected_tickers")
        if not isinstance(values, list):
            raise ValueError(
                f"{resolved}: cohort JSON requires selected_tickers"
            )
    else:
        with resolved.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if "ticker" not in set(reader.fieldnames or ()):
                raise ValueError(f"{resolved}: cohort CSV requires ticker column")
            values = [
                str(row.get("ticker") or "")
                for row in reader
                if str(row.get("enabled") or "true").strip().lower()
                not in {"0", "false", "no", "n"}
            ]
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = str(value).strip().upper()
        if ticker and ticker not in seen:
            output.append(ticker)
            seen.add(ticker)
    if not output:
        raise ValueError(f"{resolved}: cohort contains no enabled tickers")
    return output


def write_benchmark_cohort(
    *,
    payload: dict[str, Any],
    json_path: Path,
    csv_path: Path,
) -> None:
    atomic_write_text(
        json_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )

    with atomic_text_writer(csv_path, newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "rank",
                "ticker",
                "missing_metric_count",
                "missing_metrics",
                "enabled",
            ),
        )
        writer.writeheader()
        for row in payload["rows"]:
            writer.writerow(
                {
                    "rank": row["rank"],
                    "ticker": row["ticker"],
                    "missing_metric_count": row[
                        "missing_metric_count"
                    ],
                    "missing_metrics": "|".join(row["missing_metrics"]),
                    "enabled": "true",
                }
            )
