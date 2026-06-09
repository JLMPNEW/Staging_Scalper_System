#!/usr/bin/env python3
"""Analyze why calibrated selection policies underperform raw policy rankings.

This script compares selected ticker/date rows from
``tier1_selected_ticker_diagnostics.csv``.  For each candidate/horizon/top-N, it
uses a raw policy such as ``raw_legacy_score`` as the reference and compares
each guardrail policy against it:

* raw-only rows: names the guardrail removed
* policy-only rows: replacements the guardrail added
* overlap rows: names selected by both

The output is diagnostic only.  It does not change production scoring.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CALIBRATION_DIR = (
    PROJECT_ROOT
    / "output"
    / "biotech_index_reports"
    / "clean_historical_sequence"
    / "20210827_20260605"
    / "candidate_calibration_scoped_optuna_20260608"
)
DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\STAGING\DB\biotech_index.sqlite")
DEFAULT_HORIZONS = "60,120"
DEFAULT_TOP_N = "10,20"
DEFAULT_RAW_POLICY = "raw_legacy_score"
DEFAULT_RETURN_COLUMN = "net_benchmark_alpha_return_pct"
REASON_COLUMNS = (
    "hard_weakness_reasons",
    "core_hard_weakness_reasons",
    "event_hard_weakness_reasons",
    "soft_weakness_reasons",
    "toxic_soft_weakness_reasons",
    "mild_soft_weakness_reasons",
    "commercial_risk_overlay_reasons",
    "commercial_deterioration_reasons",
    "valuation_growth_mismatch_reasons",
    "transient_revenue_anchor_reasons",
    "commercial_business_shock_reasons",
    "rank_quality_cap_reasons",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose raw-vs-guardrail selection-policy failure modes from Tier-1 calibration diagnostics."
    )
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--sample", type=str, default="all")
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--horizons", type=str, default=DEFAULT_HORIZONS)
    parser.add_argument("--top-n", type=str, default=DEFAULT_TOP_N)
    parser.add_argument("--raw-policy", type=str, default=DEFAULT_RAW_POLICY)
    parser.add_argument("--return-column", type=str, default=DEFAULT_RETURN_COLUMN)
    parser.add_argument("--lcb-z", type=float, default=1.0)
    parser.add_argument("--max-example-rows", type=int, default=5000)
    return parser.parse_args()


def parse_int_set(raw: str) -> set[int]:
    values: set[int] = set()
    for part in str(raw or "").replace(";", ",").replace("|", ",").split(","):
        text = part.strip()
        if text:
            values.add(int(float(text)))
    return values


def to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    try:
        text = str(raw).strip().replace(",", "")
        if not text:
            return default
        value = float(text)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper()


def filesystem_path(path: Path) -> str:
    text = str(path.resolve())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        seen: set[str] = set()
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
    with open(filesystem_path(path), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(filesystem_path(path), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def reason_tokens(raw: object) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").replace(";", "|").replace(",", "|").split("|"):
        token = part.strip()
        if token:
            out.append(token)
    return out


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def rounded(value: float | None, digits: int = 6) -> float | str:
    return "" if value is None else round(value, digits)


def return_values(rows: Iterable[dict[str, Any]], return_column: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = to_float(row.get(return_column))
        if value is not None:
            values.append(value)
    return values


def summarize_rows(rows: list[dict[str, Any]], return_column: str, *, lcb_z: float) -> dict[str, Any]:
    values = return_values(rows, return_column)
    avg = mean(values)
    sigma = stdev(values)
    lcb = None
    if avg is not None:
        lcb = avg if sigma is None else avg - max(0.0, lcb_z) * sigma / math.sqrt(float(len(values)))
    return {
        "n": len(rows),
        "return_n": len(values),
        "mean_return_pct": rounded(avg),
        "median_return_pct": rounded(median(values)),
        "lcb_return_pct": rounded(lcb),
        "hit_rate_pct": rounded(100.0 * sum(1 for value in values if value > 0.0) / len(values) if values else None),
        "loss20_rate_pct": rounded(100.0 * sum(1 for value in values if value <= -20.0) / len(values) if values else None),
        "loss40_rate_pct": rounded(100.0 * sum(1 for value in values if value <= -40.0) / len(values) if values else None),
        "best_return_pct": rounded(max(values) if values else None),
        "worst_return_pct": rounded(min(values) if values else None),
    }


def numeric_summary_value(summary: dict[str, Any], key: str) -> float | None:
    return to_float(summary.get(key))


def load_selected_rows(
    diagnostics_path: Path,
    *,
    sample: str,
    split: str,
    horizons: set[int],
    top_ns: set[int],
) -> tuple[dict[tuple[str, str, int, int, str], dict[str, dict[tuple[str, str], dict[str, Any]]]], set[str], set[str]]:
    grouped: dict[tuple[str, str, int, int, str], dict[str, dict[tuple[str, str], dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    tickers: set[str] = set()
    dates: set[str] = set()
    with open(filesystem_path(diagnostics_path), newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("sample") or "") != sample:
                continue
            if str(row.get("evaluation_split") or "") != split:
                continue
            horizon = int(to_float(row.get("horizon_days"), -1.0) or -1)
            top_n = int(to_float(row.get("top_n"), -1.0) or -1)
            if horizons and horizon not in horizons:
                continue
            if top_ns and top_n not in top_ns:
                continue
            ticker = normalize_ticker(row.get("ticker"))
            asof_date = str(row.get("asof_date") or "").strip()
            policy = str(row.get("selection_policy_name") or "").strip()
            candidate_name = str(row.get("candidate_name") or "").strip()
            if not ticker or not asof_date or not policy or not candidate_name:
                continue
            row["ticker"] = ticker
            key = (sample, split, horizon, top_n, candidate_name)
            selection_key = (asof_date, ticker)
            grouped[key][policy][selection_key] = row
            tickers.add(ticker)
            dates.add(asof_date)
    return grouped, tickers, dates


def load_cohort_map(
    db_path: Path,
    *,
    tickers: set[str],
    dates: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not db_path.exists() or not tickers or not dates:
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    ticker_list = sorted(tickers)
    date_list = sorted(dates)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        date_placeholders = ",".join("?" for _ in date_list)
        for i in range(0, len(ticker_list), 250):
            batch = ticker_list[i : i + 250]
            ticker_placeholders = ",".join("?" for _ in batch)
            query = f"""
                SELECT
                    upper(ticker) AS ticker,
                    asof_date,
                    biotech_calibration_cohort,
                    biotech_primary_cohort,
                    allocation_bucket,
                    bucket,
                    biotech_cohort_reason_codes
                FROM daily_scores
                WHERE upper(ticker) IN ({ticker_placeholders})
                  AND asof_date IN ({date_placeholders})
            """
            for row in conn.execute(query, [*batch, *date_list]):
                out[(str(row["asof_date"]), str(row["ticker"]))] = dict(row)
    finally:
        conn.close()
    return out


def enrich_rows(rows: Iterable[dict[str, Any]], cohort_map: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        enriched = dict(row)
        metadata = cohort_map.get((str(enriched.get("asof_date") or ""), normalize_ticker(enriched.get("ticker"))), {})
        enriched.update(
            {
                "biotech_calibration_cohort": metadata.get("biotech_calibration_cohort", ""),
                "biotech_primary_cohort": metadata.get("biotech_primary_cohort", ""),
                "allocation_bucket": metadata.get("allocation_bucket", metadata.get("bucket", "")),
                "biotech_cohort_reason_codes": metadata.get("biotech_cohort_reason_codes", ""),
            }
        )
        out.append(enriched)
    return out


def reason_breakdown_rows(
    rows: list[dict[str, Any]],
    *,
    base_payload: dict[str, Any],
    side: str,
    return_column: str,
    lcb_z: float,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for column in REASON_COLUMNS:
            for reason in reason_tokens(row.get(column)):
                grouped[(column, reason)].append(row)
    out: list[dict[str, Any]] = []
    for (reason_column, reason), reason_rows in sorted(grouped.items()):
        summary = summarize_rows(reason_rows, return_column, lcb_z=lcb_z)
        out.append(
            {
                **base_payload,
                "side": side,
                "reason_column": reason_column,
                "reason": reason,
                **{f"reason_{key}": value for key, value in summary.items()},
            }
        )
    return out


def cohort_breakdown_rows(
    rows: list[dict[str, Any]],
    *,
    base_payload: dict[str, Any],
    side: str,
    return_column: str,
    lcb_z: float,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cohort = str(row.get("biotech_calibration_cohort") or row.get("biotech_primary_cohort") or "unknown")
        grouped[cohort].append(row)
    out: list[dict[str, Any]] = []
    for cohort, cohort_rows in sorted(grouped.items()):
        summary = summarize_rows(cohort_rows, return_column, lcb_z=lcb_z)
        out.append(
            {
                **base_payload,
                "side": side,
                "cohort": cohort,
                **{f"cohort_{key}": value for key, value in summary.items()},
            }
        )
    return out


def example_rows(
    rows: list[dict[str, Any]],
    *,
    base_payload: dict[str, Any],
    side: str,
    return_column: str,
    max_rows: int,
    reverse: bool,
) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=lambda row: to_float(row.get(return_column), -1e9) or -1e9, reverse=reverse)
    out: list[dict[str, Any]] = []
    for row in ranked[: max(0, max_rows)]:
        out.append(
            {
                **base_payload,
                "side": side,
                "asof_date": row.get("asof_date", ""),
                "ticker": row.get("ticker", ""),
                "company_name": row.get("company_name", ""),
                "selected_rank_within_date": row.get("selected_rank_within_date", ""),
                "candidate_selection_score": row.get("candidate_selection_score", ""),
                "return_pct": row.get(return_column, ""),
                "net_forward_return_pct": row.get("net_forward_return_pct", ""),
                "benchmark_forward_return_pct": row.get("benchmark_forward_return_pct", ""),
                "biotech_calibration_cohort": row.get("biotech_calibration_cohort", ""),
                "biotech_primary_cohort": row.get("biotech_primary_cohort", ""),
                "allocation_bucket": row.get("allocation_bucket", ""),
                "hard_weakness_reasons": row.get("hard_weakness_reasons", ""),
                "soft_weakness_reasons": row.get("soft_weakness_reasons", ""),
                "commercial_risk_overlay_reasons": row.get("commercial_risk_overlay_reasons", ""),
                "rank_quality_cap_reasons": row.get("rank_quality_cap_reasons", ""),
            }
        )
    return out


def main() -> None:
    args = parse_args()
    calibration_dir = args.calibration_dir.resolve()
    diagnostics_path = calibration_dir / "tier1_selected_ticker_diagnostics.csv"
    if not diagnostics_path.exists():
        raise FileNotFoundError(diagnostics_path)
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else calibration_dir / "policy_failure_analysis"
    )
    horizons = parse_int_set(args.horizons)
    top_ns = parse_int_set(args.top_n)
    grouped, tickers, dates = load_selected_rows(
        diagnostics_path,
        sample=str(args.sample),
        split=str(args.split),
        horizons=horizons,
        top_ns=top_ns,
    )
    cohort_map = load_cohort_map(args.db, tickers=tickers, dates=dates)

    summary_rows: list[dict[str, Any]] = []
    reason_rows: list[dict[str, Any]] = []
    cohort_rows: list[dict[str, Any]] = []
    examples: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    example_budget_per_side = max(1, int(args.max_example_rows) // 100)
    for key, policy_rows in sorted(grouped.items(), key=lambda item: item[0]):
        sample, split, horizon, top_n, candidate_name = key
        raw_rows_by_key = policy_rows.get(str(args.raw_policy), {})
        if not raw_rows_by_key:
            skipped.append(
                {
                    "sample": sample,
                    "split": split,
                    "horizon_days": horizon,
                    "top_n": top_n,
                    "candidate_name": candidate_name,
                    "skip_reason": "raw_policy_missing",
                }
            )
            continue
        raw_keys = set(raw_rows_by_key)
        for policy_name, rows_by_key in sorted(policy_rows.items()):
            if policy_name == str(args.raw_policy):
                continue
            policy_keys = set(rows_by_key)
            overlap_keys = raw_keys.intersection(policy_keys)
            raw_only_keys = raw_keys - policy_keys
            policy_only_keys = policy_keys - raw_keys
            raw_rows = enrich_rows(raw_rows_by_key.values(), cohort_map)
            policy_selected_rows = enrich_rows(rows_by_key.values(), cohort_map)
            overlap_rows = enrich_rows((raw_rows_by_key[item] for item in overlap_keys), cohort_map)
            raw_only_rows = enrich_rows((raw_rows_by_key[item] for item in raw_only_keys), cohort_map)
            policy_only_rows = enrich_rows((rows_by_key[item] for item in policy_only_keys), cohort_map)
            raw_summary = summarize_rows(raw_rows, args.return_column, lcb_z=float(args.lcb_z))
            policy_summary = summarize_rows(policy_selected_rows, args.return_column, lcb_z=float(args.lcb_z))
            raw_only_summary = summarize_rows(raw_only_rows, args.return_column, lcb_z=float(args.lcb_z))
            policy_only_summary = summarize_rows(policy_only_rows, args.return_column, lcb_z=float(args.lcb_z))
            overlap_summary = summarize_rows(overlap_rows, args.return_column, lcb_z=float(args.lcb_z))

            raw_only_mean = numeric_summary_value(raw_only_summary, "mean_return_pct")
            policy_only_mean = numeric_summary_value(policy_only_summary, "mean_return_pct")
            raw_only_lcb = numeric_summary_value(raw_only_summary, "lcb_return_pct")
            policy_only_lcb = numeric_summary_value(policy_only_summary, "lcb_return_pct")
            raw_mean = numeric_summary_value(raw_summary, "mean_return_pct")
            policy_mean = numeric_summary_value(policy_summary, "mean_return_pct")

            base_payload = {
                "sample": sample,
                "split": split,
                "horizon_days": horizon,
                "top_n": top_n,
                "candidate_name": candidate_name,
                "raw_policy_name": str(args.raw_policy),
                "comparison_policy_name": policy_name,
            }
            summary_rows.append(
                {
                    **base_payload,
                    "raw_n": raw_summary["n"],
                    "policy_n": policy_summary["n"],
                    "overlap_n": len(overlap_keys),
                    "raw_only_n": len(raw_only_keys),
                    "policy_only_n": len(policy_only_keys),
                    "overlap_pct_of_raw": rounded(100.0 * len(overlap_keys) / len(raw_keys) if raw_keys else None),
                    "raw_mean_return_pct": raw_summary["mean_return_pct"],
                    "policy_mean_return_pct": policy_summary["mean_return_pct"],
                    "policy_minus_raw_mean_return_pct": rounded(
                        policy_mean - raw_mean if policy_mean is not None and raw_mean is not None else None
                    ),
                    "raw_lcb_return_pct": raw_summary["lcb_return_pct"],
                    "policy_lcb_return_pct": policy_summary["lcb_return_pct"],
                    "raw_hit_rate_pct": raw_summary["hit_rate_pct"],
                    "policy_hit_rate_pct": policy_summary["hit_rate_pct"],
                    "raw_loss20_rate_pct": raw_summary["loss20_rate_pct"],
                    "policy_loss20_rate_pct": policy_summary["loss20_rate_pct"],
                    "raw_only_mean_return_pct": raw_only_summary["mean_return_pct"],
                    "policy_only_mean_return_pct": policy_only_summary["mean_return_pct"],
                    "policy_only_minus_raw_only_mean_return_pct": rounded(
                        policy_only_mean - raw_only_mean
                        if policy_only_mean is not None and raw_only_mean is not None
                        else None
                    ),
                    "raw_only_lcb_return_pct": raw_only_summary["lcb_return_pct"],
                    "policy_only_lcb_return_pct": policy_only_summary["lcb_return_pct"],
                    "policy_only_minus_raw_only_lcb_return_pct": rounded(
                        policy_only_lcb - raw_only_lcb
                        if policy_only_lcb is not None and raw_only_lcb is not None
                        else None
                    ),
                    "raw_only_hit_rate_pct": raw_only_summary["hit_rate_pct"],
                    "policy_only_hit_rate_pct": policy_only_summary["hit_rate_pct"],
                    "raw_only_loss20_rate_pct": raw_only_summary["loss20_rate_pct"],
                    "policy_only_loss20_rate_pct": policy_only_summary["loss20_rate_pct"],
                    "overlap_mean_return_pct": overlap_summary["mean_return_pct"],
                    "overlap_lcb_return_pct": overlap_summary["lcb_return_pct"],
                    "guardrail_hurt_replacement_mean": bool(
                        policy_only_mean is not None and raw_only_mean is not None and policy_only_mean < raw_only_mean
                    ),
                    "guardrail_hurt_replacement_lcb": bool(
                        policy_only_lcb is not None and raw_only_lcb is not None and policy_only_lcb < raw_only_lcb
                    ),
                }
            )
            reason_rows.extend(
                reason_breakdown_rows(
                    raw_only_rows,
                    base_payload=base_payload,
                    side="raw_only_removed",
                    return_column=args.return_column,
                    lcb_z=float(args.lcb_z),
                )
            )
            reason_rows.extend(
                reason_breakdown_rows(
                    policy_only_rows,
                    base_payload=base_payload,
                    side="policy_only_added",
                    return_column=args.return_column,
                    lcb_z=float(args.lcb_z),
                )
            )
            cohort_rows.extend(
                cohort_breakdown_rows(
                    raw_only_rows,
                    base_payload=base_payload,
                    side="raw_only_removed",
                    return_column=args.return_column,
                    lcb_z=float(args.lcb_z),
                )
            )
            cohort_rows.extend(
                cohort_breakdown_rows(
                    policy_only_rows,
                    base_payload=base_payload,
                    side="policy_only_added",
                    return_column=args.return_column,
                    lcb_z=float(args.lcb_z),
                )
            )
            examples.extend(
                example_rows(
                    raw_only_rows,
                    base_payload=base_payload,
                    side="raw_only_removed_best",
                    return_column=args.return_column,
                    max_rows=example_budget_per_side,
                    reverse=True,
                )
            )
            examples.extend(
                example_rows(
                    policy_only_rows,
                    base_payload=base_payload,
                    side="policy_only_added_worst",
                    return_column=args.return_column,
                    max_rows=example_budget_per_side,
                    reverse=False,
                )
            )

    summary_rows.sort(
        key=lambda row: (
            to_float(row.get("policy_only_minus_raw_only_mean_return_pct"), 0.0) or 0.0,
            to_float(row.get("policy_only_minus_raw_only_lcb_return_pct"), 0.0) or 0.0,
        )
    )
    write_csv(output_dir / "policy_failure_summary.csv", summary_rows)
    write_csv(output_dir / "policy_failure_reason_breakdown.csv", reason_rows)
    write_csv(output_dir / "policy_failure_cohort_breakdown.csv", cohort_rows)
    write_csv(output_dir / "policy_failure_ticker_examples.csv", examples[: max(0, int(args.max_example_rows))])
    write_csv(output_dir / "policy_failure_skipped.csv", skipped)
    policy_counts = Counter(row["comparison_policy_name"] for row in summary_rows)
    hurt_count = sum(1 for row in summary_rows if str(row.get("guardrail_hurt_replacement_mean")) == "True")
    write_json(
        output_dir / "policy_failure_manifest.json",
        {
            "status": "success",
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "calibration_dir": str(calibration_dir),
            "diagnostics_path": str(diagnostics_path),
            "db_path": str(args.db),
            "output_dir": str(output_dir),
            "sample": args.sample,
            "split": args.split,
            "horizons": sorted(horizons),
            "top_n": sorted(top_ns),
            "raw_policy": args.raw_policy,
            "return_column": args.return_column,
            "comparison_count": len(summary_rows),
            "skipped_count": len(skipped),
            "policy_counts": dict(sorted(policy_counts.items())),
            "guardrail_hurt_replacement_mean_count": hurt_count,
            "cohort_enrichment_rows": len(cohort_map),
            "notes": [
                "raw_only_removed rows are ticker/date selections chosen by raw policy but not by the comparison policy.",
                "policy_only_added rows are replacements selected by the comparison policy but not by raw policy.",
                "A negative policy_only_minus_raw_only_mean_return_pct means the guardrail replacements underperformed removed raw selections.",
                "This is diagnostic output only; it does not alter production scoring or calibration gates.",
            ],
        },
    )
    print(f"policy_failure_summary_rows={len(summary_rows)} output_dir={output_dir}")


if __name__ == "__main__":
    main()
