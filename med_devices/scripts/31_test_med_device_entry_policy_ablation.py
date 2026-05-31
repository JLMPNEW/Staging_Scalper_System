#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.fda_states import MANUAL_FDA_REVIEW_STATES as MANUAL_FDA_STATES  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
ENTRY_POLICIES = [
    "entry_eligible_only",
    "entry_eligible_or_setup",
    "allow_not_entry_ready_but_exclude_breakdown",
    "ignore_entry_status",
]
REVIEW_CLASSIFICATIONS = {
    "manual_review_regulatory_risk",
    "avoid_confirmed_regulatory_risk",
    "data_review_required",
}
METRIC_KEYS = ("count", "unique_tickers", "mean", "median", "hit_rate", "lcb", "sortino", "profit_factor")
ABLATION_BASE_FIELDS = [
    "calibration_cohort",
    "sample",
    "entry_status_policy",
    "gate_source",
    "selected_tickers",
    "selected_ticker_coverage_120d",
    "improved_selected_ticker_rate_120d",
]
BUCKET_FIELDS = [
    "calibration_cohort",
    "sample",
    "bucket_type",
    "bucket",
    "score_min",
    "score_max",
    "selected_tickers",
    "count_120d",
    "unique_tickers_120d",
    "mean_120d",
    "median_120d",
    "hit_rate_120d",
    "lcb_120d",
    "profit_factor_120d",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test technical-entry policy ablations for one med-device cohort.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cohort", type=str, default="")
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--gate-csv", type=Path, default=None)
    parser.add_argument("--ablation-csv", type=Path, default=None)
    parser.add_argument("--entry-bucket-csv", type=Path, default=None)
    parser.add_argument("--technical-quintile-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def int_flag(raw: object) -> int:
    text = str(raw or "").strip().lower()
    return 1 if text in {"1", "true", "yes", "y", "on"} or raw == 1 else 0


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def parse_date(raw: object) -> datetime | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def effective_train_end(train_end_asof: str, validation_start_asof: str, embargo_days: int) -> str:
    train_end = parse_date(train_end_asof)
    validation_start = parse_date(validation_start_asof)
    if train_end is None or validation_start is None or embargo_days <= 0:
        return train_end_asof
    return min(train_end, validation_start - timedelta(days=embargo_days)).strftime("%Y-%m-%d")


def return_horizons(rows: list[dict[str, str]]) -> list[int]:
    if not rows:
        return []
    out: list[int] = []
    for key in rows[0]:
        if key.startswith("cohort_excess_return_") and key.endswith("d"):
            text = key[len("cohort_excess_return_") : -1]
            if text.isdigit():
                out.append(int(text))
    return sorted(out)


def metrics(rows: list[dict[str, str]], *, horizon: int) -> dict[str, Any]:
    values: list[float] = []
    tickers: set[str] = set()
    for row in rows:
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if value is None:
            continue
        values.append(value)
        ticker = str(row.get("ticker") or "")
        if ticker:
            tickers.add(ticker)
    if not values:
        return {key: "" for key in METRIC_KEYS} | {"count": 0, "unique_tickers": 0}
    avg = mean(values)
    if len(values) == 1:
        lcb = values[0]
    else:
        variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
        lcb = avg - 1.64 * math.sqrt(variance) / math.sqrt(len(values))
    downside = [value for value in values if value < 0]
    if downside:
        downside_dev = math.sqrt(sum(value * value for value in downside) / len(downside))
        sortino = avg / downside_dev if downside_dev > 1e-12 else 999.0
    else:
        sortino = 999.0 if avg > 0 else 0.0
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    profit_factor = 999.0 if losses <= 1e-12 and gains > 0 else (gains / losses if losses > 1e-12 else 0.0)
    return {
        "count": len(values),
        "unique_tickers": len(tickers),
        "mean": f"{avg:.6f}",
        "median": f"{median(values):.6f}",
        "hit_rate": f"{sum(1 for value in values if value > 0) / len(values):.4f}",
        "lcb": f"{lcb:.6f}",
        "sortino": f"{sortino:.4f}",
        "profit_factor": f"{profit_factor:.4f}",
    }


def ticker_set(rows: list[dict[str, str]], *, horizon: int = 120) -> set[str]:
    out: set[str] = set()
    for row in rows:
        if to_float(row.get(f"cohort_excess_return_{horizon}d")) is not None and str(row.get("ticker") or ""):
            out.add(str(row["ticker"]))
    return out


def improved_ticker_rate(rows: list[dict[str, str]], *, horizon: int = 120) -> float | None:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        ticker = str(row.get("ticker") or "")
        if value is not None and ticker:
            grouped.setdefault(ticker, []).append(value)
    if not grouped:
        return None
    return sum(1 for values in grouped.values() if median(values) > 0) / len(grouped)


def reimbursement_live(row: dict[str, str]) -> bool:
    status = str(row.get("reimbursement_status") or "").strip().lower()
    if int_flag(row.get("unknown_reimbursement_flag")) or status in {"", "unknown", "cms_data_not_loaded"}:
        return False
    return True


def has_reimbursement_evidence(row: dict[str, str]) -> bool:
    return any(
        int_flag(row.get(field))
        for field in (
            "direct_code_evidence",
            "payment_rate_evidence",
            "coverage_policy_evidence",
            "procedure_bundled_flag",
            "capital_equipment_flag",
            "diagnostics_lab_flag",
        )
    )


def gate_from_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        "raw_score_min": to_float(row.get("raw_score_min")) or 55.0,
        "cohort_percentile_min": to_float(row.get("cohort_percentile_min")) or 60.0,
        "value_trap_max": to_float(row.get("value_trap_max")) or 40.0,
        "min_avg_dollar_volume_60d": to_float(row.get("min_avg_dollar_volume_60d")) or 0.0,
        "data_completeness_min": to_float(row.get("data_completeness_min")) or 90.0,
        "fda_review_policy": str(row.get("fda_review_policy") or "exclude_manual_hard_red"),
        "reimbursement_policy": str(row.get("reimbursement_policy") or "all_known"),
    }


def passes_non_entry_gates(row: dict[str, str], gate: dict[str, Any]) -> bool:
    if str(row.get("classification") or "") in REVIEW_CLASSIFICATIONS:
        return False
    checks = [
        ("raw_composite_score", float(gate["raw_score_min"])),
        ("cohort_percentile", float(gate["cohort_percentile_min"])),
        ("avg_dollar_volume_60d", float(gate["min_avg_dollar_volume_60d"])),
        ("data_completeness_score", float(gate["data_completeness_min"])),
    ]
    for field, threshold in checks:
        value = to_float(row.get(field))
        if field == "avg_dollar_volume_60d" and threshold <= 0 and value is None:
            continue
        if value is None or value < threshold:
            return False
    value_trap = to_float(row.get("value_trap_score"))
    if value_trap is not None and value_trap > float(gate["value_trap_max"]):
        return False

    fda_state = str(row.get("fda_review_state") or "").strip().lower()
    fda_policy = str(gate["fda_review_policy"])
    if fda_policy == "clean_or_cleared_only" and fda_state not in {"", "clean", "cleared", "low_materiality"}:
        return False
    if fda_policy == "exclude_manual_hard_red" and (fda_state in MANUAL_FDA_STATES or int_flag(row.get("hard_red_flag"))):
        return False

    reimbursement_policy = str(gate["reimbursement_policy"])
    if reimbursement_policy == "all_known" and not reimbursement_live(row):
        return False
    if reimbursement_policy == "live_evidence_only" and (not reimbursement_live(row) or not has_reimbursement_evidence(row)):
        return False
    if reimbursement_policy == "direct_or_bundled_or_capital" and not any(
        int_flag(row.get(field))
        for field in ("direct_code_evidence", "payment_rate_evidence", "procedure_bundled_flag", "capital_equipment_flag")
    ):
        return False
    return True


def passes_entry_policy(row: dict[str, str], policy: str) -> bool:
    entry = str(row.get("entry_status") or "")
    if policy == "entry_eligible_only":
        return entry == "entry_eligible"
    if policy == "entry_eligible_or_setup":
        return entry in {"entry_eligible", "watch_for_setup"}
    if policy == "allow_not_entry_ready_but_exclude_breakdown":
        return entry != "avoid_technical_breakdown"
    if policy == "ignore_entry_status":
        return True
    raise ValueError(f"Unknown entry_status policy: {policy}")


def output_path(base_dir: Path, config: dict[str, Any], key: str, cohort: str, explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    raw = str(cfg_get(config, key, ""))
    if raw:
        return resolve_path(raw.format(cohort=cohort), base_dir=base_dir)
    filename = key.rsplit(".", 1)[-1].replace("_csv", "")
    return resolve_path(f"../output/med_devices_reports/calibration/med_device_{cohort}_{filename}.csv", base_dir=base_dir)


def ablation_fields(horizons: list[int]) -> list[str]:
    fields = list(ABLATION_BASE_FIELDS)
    for horizon in horizons:
        for key in METRIC_KEYS:
            fields.append(f"{key}_{horizon}d")
    return fields


def ablation_rows(
    *,
    cohort: str,
    gate_source: str,
    all_rows: list[dict[str, str]],
    train_end: str,
    validation_start: str,
    validation_end: str,
    gate: dict[str, Any],
    horizons: list[int],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    validation_all = [
        row
        for row in all_rows
        if str(row.get("calibration_cohort") or "") == cohort and validation_start <= str(row.get("asof_date") or "") <= validation_end
    ]
    validation_tickers = ticker_set(validation_all, horizon=max(horizons))
    for sample, sample_rows in (
        (
            "train",
            [row for row in all_rows if str(row.get("calibration_cohort") or "") == cohort and str(row.get("asof_date") or "") <= train_end],
        ),
        ("validation", validation_all),
    ):
        base_rows = [row for row in sample_rows if passes_non_entry_gates(row, gate)]
        for policy in ENTRY_POLICIES:
            selected = [row for row in base_rows if passes_entry_policy(row, policy)]
            selected_tickers = ticker_set(selected, horizon=max(horizons))
            item: dict[str, Any] = {
                "calibration_cohort": cohort,
                "sample": sample,
                "entry_status_policy": policy,
                "gate_source": gate_source,
                "selected_tickers": ";".join(sorted(selected_tickers)),
                "selected_ticker_coverage_120d": (
                    f"{len(selected_tickers) / len(validation_tickers):.4f}" if sample == "validation" and validation_tickers else ""
                ),
                "improved_selected_ticker_rate_120d": (
                    "" if sample != "validation" or improved_ticker_rate(selected, horizon=max(horizons)) is None else f"{improved_ticker_rate(selected, horizon=max(horizons)):.4f}"
                ),
            }
            for horizon in horizons:
                payload = metrics(selected, horizon=horizon)
                for key, value in payload.items():
                    item[f"{key}_{horizon}d"] = value
            out.append(item)
    return out


def bucket_row(
    *,
    cohort: str,
    sample: str,
    bucket_type: str,
    bucket: str,
    rows: list[dict[str, str]],
    score_min: float | None = None,
    score_max: float | None = None,
) -> dict[str, Any]:
    payload = metrics(rows, horizon=120)
    return {
        "calibration_cohort": cohort,
        "sample": sample,
        "bucket_type": bucket_type,
        "bucket": bucket,
        "score_min": "" if score_min is None else f"{score_min:.4f}",
        "score_max": "" if score_max is None else f"{score_max:.4f}",
        "selected_tickers": ";".join(sorted(ticker_set(rows, horizon=120))),
        "count_120d": payload["count"],
        "unique_tickers_120d": payload["unique_tickers"],
        "mean_120d": payload["mean"],
        "median_120d": payload["median"],
        "hit_rate_120d": payload["hit_rate"],
        "lcb_120d": payload["lcb"],
        "profit_factor_120d": payload["profit_factor"],
    }


def entry_bucket_rows(
    *,
    cohort: str,
    validation_rows: list[dict[str, str]],
    gate: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    samples = {
        "all_validation_rows": validation_rows,
        "base_gate_without_entry": [row for row in validation_rows if passes_non_entry_gates(row, gate)],
    }
    for sample, rows in samples.items():
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("entry_status") or "<blank>"), []).append(row)
        for bucket, bucket_rows in sorted(grouped.items()):
            out.append(bucket_row(cohort=cohort, sample=sample, bucket_type="entry_status", bucket=bucket, rows=bucket_rows))
    return out


def technical_quintile_rows(
    *,
    cohort: str,
    validation_rows: list[dict[str, str]],
    gate: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    samples = {
        "all_validation_rows": validation_rows,
        "base_gate_without_entry": [row for row in validation_rows if passes_non_entry_gates(row, gate)],
    }
    for sample, rows in samples.items():
        valid = [(row, to_float(row.get("technical_entry_score"))) for row in rows]
        valid = [(row, score) for row, score in valid if score is not None]
        valid.sort(key=lambda item: item[1])
        n = len(valid)
        if not n:
            continue
        for idx in range(5):
            start = math.floor(idx * n / 5)
            end = math.floor((idx + 1) * n / 5)
            bucket_items = valid[start:end]
            if not bucket_items:
                continue
            bucket_rows = [item[0] for item in bucket_items]
            scores = [float(item[1]) for item in bucket_items]
            out.append(
                bucket_row(
                    cohort=cohort,
                    sample=sample,
                    bucket_type="technical_entry_score_quintile",
                    bucket=f"q{idx + 1}",
                    rows=bucket_rows,
                    score_min=min(scores),
                    score_max=max(scores),
                )
            )
    return out


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    cohort = args.cohort.strip() or str(cfg_get(config, "calibration.entry_policy_test.default_cohort", ""))
    if not cohort:
        raise ValueError("Provide --cohort or calibration.entry_policy_test.default_cohort")
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    gate_csv = (
        args.gate_csv.expanduser().resolve()
        if args.gate_csv
        else resolve_path(cfg_get(config, "calibration.entry_policy_test.gate_csv"), base_dir=base_dir)
    )
    ablation_csv = output_path(base_dir, config, "calibration.entry_policy_test.ablation_csv", cohort, args.ablation_csv)
    entry_bucket_csv = output_path(base_dir, config, "calibration.entry_policy_test.entry_bucket_csv", cohort, args.entry_bucket_csv)
    technical_quintile_csv = output_path(
        base_dir,
        config,
        "calibration.entry_policy_test.technical_quintile_csv",
        cohort,
        args.technical_quintile_csv,
    )
    rows = read_csv(input_csv)
    horizons = return_horizons(rows)
    gate_rows = read_csv(gate_csv)
    if not gate_rows:
        raise ValueError(f"No gate rows found in {gate_csv}")
    gate_cohort = str(gate_rows[0].get("calibration_cohort") or "")
    if gate_cohort and gate_cohort != cohort:
        raise ValueError(f"Gate file cohort {gate_cohort!r} does not match requested cohort {cohort!r}")
    gate = gate_from_row(gate_rows[0])
    train_end = effective_train_end(
        str(cfg_get(config, "calibration.train_end_asof", "2025-05-30")),
        str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06")),
        int(cfg_get(config, "calibration.embargo_days", 120)),
    )
    validation_start = str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06"))
    validation_end = str(cfg_get(config, "calibration.validation_end_asof", "2025-11-28"))
    validation_rows = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort and validation_start <= str(row.get("asof_date") or "") <= validation_end
    ]
    ablations = ablation_rows(
        cohort=cohort,
        gate_source=str(gate_csv),
        all_rows=rows,
        train_end=train_end,
        validation_start=validation_start,
        validation_end=validation_end,
        gate=gate,
        horizons=horizons,
    )
    entry_buckets = entry_bucket_rows(cohort=cohort, validation_rows=validation_rows, gate=gate)
    technical_quintiles = technical_quintile_rows(cohort=cohort, validation_rows=validation_rows, gate=gate)
    write_csv(ablation_csv, ablations, ablation_fields(horizons))
    write_csv(entry_bucket_csv, entry_buckets, BUCKET_FIELDS)
    write_csv(technical_quintile_csv, technical_quintiles, BUCKET_FIELDS)
    print(f"entry_policy_ablation_csv={ablation_csv} rows={len(ablations)}")
    print(f"entry_status_bucket_csv={entry_bucket_csv} rows={len(entry_buckets)}")
    print(f"technical_quintile_csv={technical_quintile_csv} rows={len(technical_quintiles)}")


if __name__ == "__main__":
    main()
