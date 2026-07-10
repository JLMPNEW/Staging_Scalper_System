#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import os
from pathlib import Path
from typing import Any


FIELDS = [
    "summary_type",
    "segment",
    "horizon_days",
    "before_count",
    "after_count",
    "before_unique_tickers",
    "after_unique_tickers",
    "before_mean_return",
    "after_mean_return",
    "delta_mean_return",
    "before_median_return",
    "after_median_return",
    "delta_median_return",
    "before_mean_excess_return",
    "after_mean_excess_return",
    "delta_mean_excess_return",
    "before_median_excess_return",
    "after_median_excess_return",
    "delta_median_excess_return",
    "before_hit_rate",
    "after_hit_rate",
    "delta_hit_rate",
    "before_excess_hit_rate",
    "after_excess_hit_rate",
    "delta_excess_hit_rate",
    "before_lcb_excess_return",
    "after_lcb_excess_return",
    "delta_lcb_excess_return",
    "before_sortino_excess",
    "after_sortino_excess",
    "delta_sortino_excess",
    "before_profit_factor_excess",
    "after_profit_factor_excess",
    "delta_profit_factor_excess",
]
NUMERIC_FIELDS = [
    "mean_return",
    "median_return",
    "mean_excess_return",
    "median_excess_return",
    "hit_rate",
    "excess_hit_rate",
    "lcb_excess_return",
    "sortino_excess",
    "profit_factor_excess",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two med-device cohort-neutral calibration summary files.")
    parser.add_argument("--before-csv", type=Path, required=True)
    parser.add_argument("--after-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


REQUIRED_INPUT_FIELDS = ["summary_type", "segment", "horizon_days", "count", "unique_tickers", *NUMERIC_FIELDS]


def read_rows(path: Path, *, label: str) -> dict[tuple[str, str, str], dict[str, str]]:
    if not path.exists():
        raise RuntimeError(f"{label} summary CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise RuntimeError(f"{label} summary CSV has no data rows: {path}")
    missing = [field for field in REQUIRED_INPUT_FIELDS if field not in rows[0]]
    if missing:
        raise RuntimeError(
            f"{label} summary CSV {path} is missing required cohort-neutral summary columns: {','.join(missing)}. "
            "Check that the path points at a cohort-neutral backtest summary file."
        )
    out: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        key = (str(row.get("summary_type") or ""), str(row.get("segment") or ""), str(row.get("horizon_days") or ""))
        if key in out:
            raise RuntimeError(
                f"{label} summary CSV {path} contains duplicate key "
                f"summary_type={key[0]!r} segment={key[1]!r} horizon_days={key[2]!r}; refusing to silently drop rows."
            )
        out[key] = row
    return out


def horizon_sort_key(raw: str) -> tuple[int, float, str]:
    value = to_float(raw)
    if value is None:
        return (1, 0.0, raw)
    return (0, value, raw)


def fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def compare(before: dict[tuple[str, str, str], dict[str, str]], after: dict[tuple[str, str, str], dict[str, str]]) -> list[dict[str, Any]]:
    keys = sorted(set(before) | set(after), key=lambda key: (key[0], key[1], horizon_sort_key(key[2])))
    out: list[dict[str, Any]] = []
    for summary_type, segment, horizon in keys:
        b = before.get((summary_type, segment, horizon), {})
        a = after.get((summary_type, segment, horizon), {})
        item: dict[str, Any] = {
            "summary_type": summary_type,
            "segment": segment,
            "horizon_days": horizon,
            "before_count": b.get("count", ""),
            "after_count": a.get("count", ""),
            "before_unique_tickers": b.get("unique_tickers", ""),
            "after_unique_tickers": a.get("unique_tickers", ""),
        }
        for field in NUMERIC_FIELDS:
            before_value = to_float(b.get(field))
            after_value = to_float(a.get(field))
            item[f"before_{field}"] = "" if before_value is None else f"{before_value:.6f}"
            item[f"after_{field}"] = "" if after_value is None else f"{after_value:.6f}"
            item[f"delta_{field}"] = fmt(after_value - before_value if before_value is not None and after_value is not None else None)
        out.append(item)
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def main() -> None:
    args = parse_args()
    rows = compare(
        read_rows(args.before_csv, label="before"),
        read_rows(args.after_csv, label="after"),
    )
    write_csv(args.output_csv, rows)
    print(f"comparison_csv={args.output_csv} rows={len(rows)}")


if __name__ == "__main__":
    raise SystemExit(main())
