#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import logging
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import calibration_market_sources  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_forward_catalyst_calendar")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
METRIC_KEYS = [
    "n",
    "mean_return_pct",
    "median_return_pct",
    "hit_rate_pct",
    "loss_rate_pct",
    "winsorized_mean_return_pct",
    "stdev_return_pct",
    "downside_deviation_pct",
    "lcb_return_pct",
    "cvar_5_return_pct",
    "sharpe_like",
    "sortino_like",
    "profit_factor",
    "profit_factor_configured",
    "omega_configured",
    "omega_0",
    "top3_gain_contribution_pct",
    "worst_return_pct",
    "best_return_pct",
    "p05_return_pct",
    "p10_return_pct",
    "large_loss_20pct_rate_pct",
    "large_loss_40pct_rate_pct",
    "large_gain_20pct_rate_pct",
]
CATALYST_FLAG_KEYS = [
    "forward_catalyst_any_flag",
    "forward_catalyst_high_flag",
    "forward_catalyst_sec_flag",
    "forward_catalyst_manual_flag",
    "forward_catalyst_sec_or_manual_flag",
    "forward_catalyst_ctgov_flag",
    "forward_catalyst_ctgov_high_flag",
    "forward_catalyst_ctgov_low_confidence_flag",
    "forward_catalyst_guardrail_pass_flag",
]
SOURCE_ORDER = {
    "ALL": -1,
    "no_forward_catalyst": 0,
    "sec": 1,
    "manual": 2,
    "ctgov": 3,
    "unknown_forward_catalyst": 98,
}
BUCKET_ORDER = {
    "missing": -1,
    "000_zero": 0,
    "001_0_to_20": 1,
    "002_20_to_40": 2,
    "003_40_to_60": 3,
    "004_60_to_80": 4,
    "005_80_to_100": 5,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Source-aware validation for the forward catalyst calendar. The script "
            "compares SEC, CTGov, and manual catalyst sources by forward return, "
            "cohort, IC, and score monotonicity. It is report-only and does not "
            "mutate production scoring."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None, help="Biotech SQLite DB path.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", "--asof", dest="end_asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="")
    parser.add_argument("--market-sources", type=str, default="")
    parser.add_argument("--max-snapshots", type=int, default=0)
    parser.add_argument("--include-non-fridays", action="store_true")
    parser.add_argument("--strict-feature-lag", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--next-bar-entry", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--train-fraction", type=float, default=None)
    parser.add_argument("--embargo-days", type=int, default=None)
    return parser.parse_args()


def load_calibration_module() -> Any:
    path = PACKAGE_ROOT / "scripts" / "28_calibrate_biotech_opportunity.py"
    spec = importlib.util.spec_from_file_location("biotech_forward_catalyst_calibration_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import calibration module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_date(raw: object) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def to_float(raw: object, default: float | None = None) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def parse_int_list(raw: object, default: list[int]) -> list[int]:
    if isinstance(raw, (list, tuple, set)):
        out = [int(item) for item in raw if str(item).strip()]
        return out or list(default)
    text = str(raw or "").strip()
    if not text:
        return list(default)
    out: list[int] = []
    for part in text.replace(";", ",").replace("|", ",").split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out or list(default)


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def pct(numerator: int, denominator: int) -> float | str:
    if denominator <= 0:
        return ""
    return round(100.0 * numerator / denominator, 6)


def score_bucket(value: object) -> str:
    numeric = to_float(value)
    if numeric is None:
        return "missing"
    if numeric <= 0.0:
        return "000_zero"
    if numeric < 20.0:
        return "001_0_to_20"
    if numeric < 40.0:
        return "002_20_to_40"
    if numeric < 60.0:
        return "003_40_to_60"
    if numeric < 80.0:
        return "004_60_to_80"
    return "005_80_to_100"


def confidence_bucket(value: object) -> str:
    numeric = to_float(value)
    if numeric is None:
        return "missing"
    if numeric <= 0.0:
        return "000_zero"
    if numeric < 0.25:
        return "001_0_to_25"
    if numeric < 0.50:
        return "002_25_to_50"
    if numeric < 0.75:
        return "003_50_to_75"
    return "004_75_to_100"


def canonical_source(raw: object) -> str:
    text = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return ""
    if "ctgov" in text or "clinicaltrials" in text:
        return "ctgov"
    if "manual" in text or "override" in text:
        return "manual"
    if text == "sec" or "sec_event" in text or text.startswith("sec_"):
        return "sec"
    return text[:80]


def validation_forward_catalyst_score(row: dict[str, Any]) -> float:
    return to_float(
        row.get("forward_catalyst_unfiltered_score"),
        to_float(row.get("forward_catalyst_score"), 0.0),
    ) or 0.0


def source_group(row: dict[str, Any]) -> str:
    score = validation_forward_catalyst_score(row)
    if score <= 0.0:
        return "no_forward_catalyst"
    return canonical_source(row.get("forward_catalyst_source")) or "unknown_forward_catalyst"


def completed_rows(rows: list[dict[str, Any]], ret_key: str) -> list[dict[str, Any]]:
    return [row for row in rows if to_float(row.get(ret_key)) is not None]


def split_train_test(
    rows: list[dict[str, Any]],
    *,
    train_fraction: float,
    embargo_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dates = sorted({parsed for row in rows if (parsed := parse_date(row.get("asof_date"))) is not None})
    if len(dates) < 2:
        return rows, []
    train_idx = max(0, min(len(dates) - 1, int(len(dates) * train_fraction) - 1))
    train_end = dates[train_idx]
    test_start = train_end + timedelta(days=max(0, embargo_days))
    train_rows = [row for row in rows if (parsed := parse_date(row.get("asof_date"))) is not None and parsed <= train_end]
    test_rows = [row for row in rows if (parsed := parse_date(row.get("asof_date"))) is not None and parsed > test_start]
    return train_rows, test_rows


def metric_row(
    *,
    calibration: Any,
    rows: list[dict[str, Any]],
    params: Any,
    ret_key: str,
    prefix: dict[str, Any],
) -> dict[str, Any]:
    metrics = calibration.summarize_return_risk(calibration.numeric_values(rows, ret_key), params=params)
    return {**prefix, **metrics}


def cohort_groups(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("biotech_primary_cohort") or "unclassified")].append(row)
    return [("ALL", rows), *sorted(groups.items())]


def source_groups(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("forward_catalyst_source_group") or source_group(row))].append(row)
    return sorted(groups.items(), key=lambda item: (SOURCE_ORDER.get(item[0], 50), item[0]))


def average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    idx = 0
    while idx < len(ordered):
        end = idx + 1
        while end < len(ordered) and ordered[end][1] == ordered[idx][1]:
            end += 1
        avg_rank = (idx + 1 + end) / 2.0
        for original_idx, _value in ordered[idx:end]:
            ranks[original_idx] = avg_rank
        idx = end
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    dx = [value - mean_x for value in xs]
    dy = [value - mean_y for value in ys]
    denom_x = math.sqrt(sum(value * value for value in dx))
    denom_y = math.sqrt(sum(value * value for value in dy))
    if denom_x <= 0.0 or denom_y <= 0.0:
        return None
    return sum(left * right for left, right in zip(dx, dy)) / (denom_x * denom_y)


def correlation_summary(rows: list[dict[str, Any]], ret_key: str) -> dict[str, Any]:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        score = validation_forward_catalyst_score(row)
        ret = to_float(row.get(ret_key))
        if score is not None and ret is not None:
            pairs.append((score, ret))
    if len(pairs) < 3:
        return {
            "ic_n": len(pairs),
            "mean_forward_catalyst_score": "",
            "pearson_ic": "",
            "spearman_ic": "",
        }
    scores = [pair[0] for pair in pairs]
    returns = [pair[1] for pair in pairs]
    pearson_ic = pearson(scores, returns)
    spearman_ic = pearson(average_ranks(scores), average_ranks(returns))
    return {
        "ic_n": len(pairs),
        "mean_forward_catalyst_score": round(sum(scores) / len(scores), 6),
        "pearson_ic": "" if pearson_ic is None else round(pearson_ic, 6),
        "spearman_ic": "" if spearman_ic is None else round(spearman_ic, 6),
    }


def monotonicity_summary(
    rows: list[dict[str, Any]],
    ret_key: str,
    *,
    calibration: Any,
    params: Any,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("forward_catalyst_score_bucket") or score_bucket(validation_forward_catalyst_score(row)))].append(row)
    ordered_metrics: list[tuple[str, dict[str, Any]]] = []
    for bucket, bucket_rows in sorted(buckets.items(), key=lambda item: (BUCKET_ORDER.get(item[0], 99), item[0])):
        if bucket == "missing":
            continue
        metrics = calibration.summarize_return_risk(calibration.numeric_values(bucket_rows, ret_key), params=params)
        if int(to_float(metrics.get("n"), 0.0) or 0.0) > 0:
            ordered_metrics.append((bucket, metrics))
    def step_count(metric_key: str) -> tuple[int, int]:
        values: list[float] = []
        for _bucket, metrics in ordered_metrics:
            value = to_float(metrics.get(metric_key))
            if value is not None:
                values.append(value)
        if len(values) < 2:
            return 0, 0
        total = len(values) - 1
        passing = sum(1 for left, right in zip(values, values[1:]) if right >= left)
        return passing, total

    lcb_pass, lcb_total = step_count("lcb_return_pct")
    mean_pass, mean_total = step_count("mean_return_pct")
    return {
        "bucket_count": len(ordered_metrics),
        "lcb_monotonic_positive_steps": lcb_pass,
        "lcb_monotonic_total_steps": lcb_total,
        "lcb_monotonic_pass_rate_pct": pct(lcb_pass, lcb_total),
        "mean_monotonic_positive_steps": mean_pass,
        "mean_monotonic_total_steps": mean_total,
        "mean_monotonic_pass_rate_pct": pct(mean_pass, mean_total),
    }


def enrich_forward_catalyst_diagnostics(rows: list[dict[str, Any]], *, validation_cfg: dict[str, Any]) -> None:
    high_score_min = float(validation_cfg.get("high_score_min", 40.0))
    ctgov_guardrail_score_min = float(validation_cfg.get("ctgov_guardrail_score_min", 60.0))
    ctgov_confidence_min = float(validation_cfg.get("ctgov_confidence_min", 0.50))
    for row in rows:
        score = validation_forward_catalyst_score(row)
        source = source_group(row)
        confidence = to_float(row.get("forward_catalyst_confidence"), 0.0) or 0.0
        has_signal = score > 0.0
        is_ctgov = source == "ctgov"
        is_sec = source == "sec"
        is_manual = source == "manual"
        ctgov_high = is_ctgov and score >= ctgov_guardrail_score_min
        ctgov_low_confidence = is_ctgov and has_signal and confidence < ctgov_confidence_min
        guardrail_pass = has_signal and (not is_ctgov or ctgov_high)
        row["forward_catalyst_source_group"] = source
        row["forward_catalyst_score_bucket"] = score_bucket(score)
        row["forward_catalyst_confidence_bucket"] = confidence_bucket(confidence)
        row["forward_catalyst_any_flag"] = 1.0 if has_signal else 0.0
        row["forward_catalyst_high_flag"] = 1.0 if score >= high_score_min else 0.0
        row["forward_catalyst_sec_flag"] = 1.0 if is_sec and has_signal else 0.0
        row["forward_catalyst_manual_flag"] = 1.0 if is_manual and has_signal else 0.0
        row["forward_catalyst_sec_or_manual_flag"] = 1.0 if (is_sec or is_manual) and has_signal else 0.0
        row["forward_catalyst_ctgov_flag"] = 1.0 if is_ctgov and has_signal else 0.0
        row["forward_catalyst_ctgov_high_flag"] = 1.0 if ctgov_high else 0.0
        row["forward_catalyst_ctgov_low_confidence_flag"] = 1.0 if ctgov_low_confidence else 0.0
        row["forward_catalyst_guardrail_pass_flag"] = 1.0 if guardrail_pass else 0.0


def build_source_performance_rows(
    *,
    calibration: Any,
    rows: list[dict[str, Any]],
    horizons: list[int],
    params: Any,
    train_fraction: float,
    embargo_days: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        horizon_rows = completed_rows(rows, ret_key)
        train_rows, test_rows = split_train_test(horizon_rows, train_fraction=train_fraction, embargo_days=embargo_days)
        for sample, sample_rows in (("all", horizon_rows), ("train", train_rows), ("test", test_rows)):
            for cohort, cohort_rows in cohort_groups(sample_rows):
                for source, source_rows in source_groups(cohort_rows):
                    out.append(
                        metric_row(
                            calibration=calibration,
                            rows=source_rows,
                            params=params,
                            ret_key=ret_key,
                            prefix={
                                "sample": sample,
                                "horizon_days": horizon,
                                "return_key": ret_key,
                                "cohort": cohort,
                                "forward_catalyst_source_group": source,
                            },
                        )
                    )
    return out


def build_bucket_performance_rows(
    *,
    calibration: Any,
    rows: list[dict[str, Any]],
    horizons: list[int],
    params: Any,
    train_fraction: float,
    embargo_days: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        horizon_rows = completed_rows(rows, ret_key)
        train_rows, test_rows = split_train_test(horizon_rows, train_fraction=train_fraction, embargo_days=embargo_days)
        for sample, sample_rows in (("all", horizon_rows), ("train", train_rows), ("test", test_rows)):
            for cohort, cohort_rows in cohort_groups(sample_rows):
                source_scopes = [("ALL", cohort_rows), *source_groups(cohort_rows)]
                for source, source_rows in source_scopes:
                    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for row in source_rows:
                        bucket = str(row.get("forward_catalyst_score_bucket") or score_bucket(validation_forward_catalyst_score(row)))
                        buckets[bucket].append(row)
                    for bucket, bucket_rows in sorted(buckets.items(), key=lambda item: (BUCKET_ORDER.get(item[0], 99), item[0])):
                        out.append(
                            metric_row(
                                calibration=calibration,
                                rows=bucket_rows,
                                params=params,
                                ret_key=ret_key,
                                prefix={
                                    "sample": sample,
                                    "horizon_days": horizon,
                                    "return_key": ret_key,
                                    "cohort": cohort,
                                    "forward_catalyst_source_group": source,
                                    "bucket_source": "forward_catalyst_score",
                                    "bucket": bucket,
                                },
                            )
                        )
    return out


def build_flag_performance_rows(
    *,
    calibration: Any,
    rows: list[dict[str, Any]],
    horizons: list[int],
    params: Any,
    train_fraction: float,
    embargo_days: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        horizon_rows = completed_rows(rows, ret_key)
        train_rows, test_rows = split_train_test(horizon_rows, train_fraction=train_fraction, embargo_days=embargo_days)
        for sample, sample_rows in (("all", horizon_rows), ("train", train_rows), ("test", test_rows)):
            for cohort, cohort_rows in cohort_groups(sample_rows):
                for flag in CATALYST_FLAG_KEYS:
                    for flag_value in (0.0, 1.0):
                        selected = [row for row in cohort_rows if (to_float(row.get(flag), 0.0) or 0.0) == flag_value]
                        out.append(
                            metric_row(
                                calibration=calibration,
                                rows=selected,
                                params=params,
                                ret_key=ret_key,
                                prefix={
                                    "sample": sample,
                                    "horizon_days": horizon,
                                    "return_key": ret_key,
                                    "cohort": cohort,
                                    "flag_name": flag,
                                    "flag_value": int(flag_value),
                                },
                            )
                        )
    return out


def build_ic_monitor_rows(
    *,
    calibration: Any,
    rows: list[dict[str, Any]],
    horizons: list[int],
    params: Any,
    train_fraction: float,
    embargo_days: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        horizon_rows = completed_rows(rows, ret_key)
        train_rows, test_rows = split_train_test(horizon_rows, train_fraction=train_fraction, embargo_days=embargo_days)
        for sample, sample_rows in (("all", horizon_rows), ("train", train_rows), ("test", test_rows)):
            for cohort, cohort_rows in cohort_groups(sample_rows):
                source_scopes = [("ALL", cohort_rows), *source_groups(cohort_rows)]
                for source, source_rows in source_scopes:
                    out.append(
                        {
                            "sample": sample,
                            "horizon_days": horizon,
                            "return_key": ret_key,
                            "cohort": cohort,
                            "forward_catalyst_source_group": source,
                            **correlation_summary(source_rows, ret_key),
                            **monotonicity_summary(source_rows, ret_key, calibration=calibration, params=params),
                        }
                    )
    return out


def numeric_metric(row: dict[str, Any], key: str) -> float | None:
    return to_float(row.get(key))


def metric_delta(left: dict[str, Any], right: dict[str, Any], key: str) -> float | None:
    left_value = numeric_metric(left, key)
    right_value = numeric_metric(right, key)
    if left_value is None or right_value is None:
        return None
    return left_value - right_value


def build_guardrail_recommendations(
    flag_rows: list[dict[str, Any]],
    *,
    min_n: int,
    lcb_degradation_tolerance_pct: float,
    loss20_degradation_tolerance_pct: float,
) -> list[dict[str, Any]]:
    indexed: dict[tuple[str, int, str, str, int], dict[str, Any]] = {}
    for row in flag_rows:
        indexed[
            (
                str(row.get("sample") or ""),
                int(to_float(row.get("horizon_days"), 0.0) or 0.0),
                str(row.get("cohort") or ""),
                str(row.get("flag_name") or ""),
                int(to_float(row.get("flag_value"), 0.0) or 0.0),
            )
        ] = row
    out: list[dict[str, Any]] = []
    keys = sorted({(sample, horizon, cohort) for sample, horizon, cohort, _flag, _value in indexed if sample in {"all", "test"}})
    for sample, horizon, cohort in keys:
        ctgov = indexed.get((sample, horizon, cohort, "forward_catalyst_ctgov_flag", 1), {})
        non_ctgov = indexed.get((sample, horizon, cohort, "forward_catalyst_ctgov_flag", 0), {})
        ctgov_high = indexed.get((sample, horizon, cohort, "forward_catalyst_ctgov_high_flag", 1), {})
        sec_or_manual = indexed.get((sample, horizon, cohort, "forward_catalyst_sec_or_manual_flag", 1), {})
        guardrail_pass = indexed.get((sample, horizon, cohort, "forward_catalyst_guardrail_pass_flag", 1), {})
        ctgov_n = int(to_float(ctgov.get("n"), 0.0) or 0.0)
        non_ctgov_n = int(to_float(non_ctgov.get("n"), 0.0) or 0.0)
        lcb_delta = metric_delta(ctgov, non_ctgov, "lcb_return_pct") if non_ctgov else None
        loss20_delta = metric_delta(ctgov, non_ctgov, "large_loss_20pct_rate_pct") if non_ctgov else None
        high_lcb_delta = metric_delta(ctgov_high, non_ctgov, "lcb_return_pct") if ctgov_high and non_ctgov else None
        high_loss20_delta = (
            metric_delta(ctgov_high, non_ctgov, "large_loss_20pct_rate_pct") if ctgov_high and non_ctgov else None
        )
        if ctgov_n < min_n:
            recommendation = "insufficient_ctgov_evidence_keep_shadow"
        elif non_ctgov_n < min_n:
            recommendation = "insufficient_baseline_evidence_keep_shadow"
        elif (
            (lcb_delta is not None and lcb_delta < -abs(lcb_degradation_tolerance_pct))
            or (loss20_delta is not None and loss20_delta > abs(loss20_degradation_tolerance_pct))
        ):
            if (
                int(to_float(ctgov_high.get("n"), 0.0) or 0.0) >= min_n
                and (high_lcb_delta is None or high_lcb_delta >= -abs(lcb_degradation_tolerance_pct))
                and (high_loss20_delta is None or high_loss20_delta <= abs(loss20_degradation_tolerance_pct))
            ):
                recommendation = "tighten_ctgov_to_high_score_guardrail"
            else:
                recommendation = "keep_ctgov_discovery_only_or_disable_until_revalidated"
        else:
            recommendation = "ctgov_signal_ok_for_shadow_validation"
        out.append(
            {
                "sample": sample,
                "horizon_days": horizon,
                "cohort": cohort,
                "ctgov_n": ctgov_n,
                "non_ctgov_n": non_ctgov_n,
                "ctgov_lcb_return_pct": ctgov.get("lcb_return_pct", ""),
                "non_ctgov_lcb_return_pct": non_ctgov.get("lcb_return_pct", ""),
                "ctgov_lcb_delta_pct": "" if lcb_delta is None else round(lcb_delta, 6),
                "ctgov_loss20_pct": ctgov.get("large_loss_20pct_rate_pct", ""),
                "non_ctgov_loss20_pct": non_ctgov.get("large_loss_20pct_rate_pct", ""),
                "ctgov_loss20_delta_pct": "" if loss20_delta is None else round(loss20_delta, 6),
                "ctgov_high_n": ctgov_high.get("n", 0),
                "ctgov_high_lcb_return_pct": ctgov_high.get("lcb_return_pct", ""),
                "ctgov_high_loss20_pct": ctgov_high.get("large_loss_20pct_rate_pct", ""),
                "sec_or_manual_n": sec_or_manual.get("n", 0),
                "sec_or_manual_lcb_return_pct": sec_or_manual.get("lcb_return_pct", ""),
                "guardrail_pass_n": guardrail_pass.get("n", 0),
                "guardrail_pass_lcb_return_pct": guardrail_pass.get("lcb_return_pct", ""),
                "recommendation": recommendation,
            }
        )
    return out


def build_feature_qa_rows(
    *,
    rows: list[dict[str, Any]],
    snapshot_dates: list[str],
    horizons: list[int],
    calibration: Any,
    params: Any,
) -> list[dict[str, Any]]:
    tickers = {normalize_ticker(row.get("ticker")) for row in rows if normalize_ticker(row.get("ticker"))}
    scored_rows = [row for row in rows if validation_forward_catalyst_score(row) > 0.0]
    completed_by_horizon = {}
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        completed_by_horizon[f"completed_return_rows_{horizon}d"] = len(completed_rows(rows, ret_key))
    out = [
        {
            "qa_item": "coverage",
            "snapshot_date_count": len(snapshot_dates),
            "first_snapshot_date": snapshot_dates[0] if snapshot_dates else "",
            "last_snapshot_date": snapshot_dates[-1] if snapshot_dates else "",
            "observation_rows": len(rows),
            "ticker_count": len(tickers),
            "forward_catalyst_scored_rows": len(scored_rows),
            "forward_catalyst_scored_pct": pct(len(scored_rows), len(rows)),
            **completed_by_horizon,
        }
    ]
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_source[str(row.get("forward_catalyst_source_group") or source_group(row))].append(row)
    for source, source_rows in sorted(by_source.items(), key=lambda item: (SOURCE_ORDER.get(item[0], 50), item[0])):
        scores = [validation_forward_catalyst_score(row) for row in source_rows]
        scores = [score for score in scores if score is not None]
        confidences = [to_float(row.get("forward_catalyst_confidence")) for row in source_rows]
        confidences = [confidence for confidence in confidences if confidence is not None]
        out.append(
            {
                "qa_item": f"source_coverage:{source}",
                "source_group": source,
                "observation_rows": len(source_rows),
                "observation_pct": pct(len(source_rows), len(rows)),
                "ticker_count": len({normalize_ticker(row.get("ticker")) for row in source_rows if normalize_ticker(row.get("ticker"))}),
                "score_mean": "" if not scores else round(sum(scores) / len(scores), 6),
                "score_max": "" if not scores else round(max(scores), 6),
                "confidence_mean": "" if not confidences else round(sum(confidences) / len(confidences), 6),
                "confidence_max": "" if not confidences else round(max(confidences), 6),
            }
        )
    return out


def build_top_ticker_rows(rows: list[dict[str, Any]], *, calibration: Any, params: Any, horizons: list[int]) -> list[dict[str, Any]]:
    ret_keys = [calibration.objective_return_key(horizon, params) for horizon in horizons]
    top_rows = sorted(
        rows,
        key=lambda row: (
            -validation_forward_catalyst_score(row),
            str(row.get("forward_catalyst_source_group") or ""),
            str(row.get("ticker") or ""),
        ),
    )[:250]
    out: list[dict[str, Any]] = []
    for row in top_rows:
        payload = {
            "asof_date": row.get("asof_date", ""),
            "ticker": row.get("ticker", ""),
            "company_name": row.get("company_name", ""),
            "biotech_primary_cohort": row.get("biotech_primary_cohort", ""),
            "forward_catalyst_source_group": row.get("forward_catalyst_source_group", ""),
            "forward_catalyst_source": row.get("forward_catalyst_source", ""),
            "forward_catalyst_event_type": row.get("forward_catalyst_event_type", ""),
            "forward_catalyst_nearest_days": row.get("forward_catalyst_nearest_days", ""),
            "forward_catalyst_confidence": row.get("forward_catalyst_confidence", ""),
            "forward_catalyst_score": row.get("forward_catalyst_score", ""),
            "forward_catalyst_unfiltered_score": row.get("forward_catalyst_unfiltered_score", ""),
            "ctgov_forward_catalyst_score": row.get("ctgov_forward_catalyst_score", ""),
            "ctgov_forward_catalyst_guardrail_pass": row.get("ctgov_forward_catalyst_guardrail_pass", ""),
            "forward_catalyst_score_bucket": row.get("forward_catalyst_score_bucket", ""),
            "forward_catalyst_source_url": row.get("forward_catalyst_source_url", ""),
            "sec_catalyst_score_used": row.get("sec_catalyst_score_used", ""),
            "catalyst_score_raw": row.get("catalyst_score_raw", ""),
            "risk_for_penalty_score_raw": row.get("risk_for_penalty_score_raw", ""),
        }
        for ret_key in ret_keys:
            payload[ret_key] = row.get(ret_key, "")
        out.append(payload)
    return out


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    calibration = load_calibration_module()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db or resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    validation_cfg = cfg_get(config, "biotech_reports.forward_catalyst_validation", {}) or {}
    if not isinstance(validation_cfg, dict):
        validation_cfg = {}
    output_dir = args.output_dir or resolve_path(
        validation_cfg.get("output_dir", "../output/biotech_index_reports/forward_catalyst_validation"),
        base_dir=base_dir,
    )
    start_asof = parse_date(args.start_asof)
    end_asof = parse_date(args.end_asof)
    horizons = parse_int_list(args.horizons or validation_cfg.get("horizons"), [20, 60, 120])
    max_snapshots = max(0, int(args.max_snapshots or validation_cfg.get("max_snapshots", 0) or 0))
    strict_feature_lag = (
        args.strict_feature_lag
        if args.strict_feature_lag is not None
        else as_bool(cfg_get(config, "calibration.tier1.strict_feature_lag", True), True)
    )
    next_bar_entry = (
        args.next_bar_entry
        if args.next_bar_entry is not None
        else as_bool(cfg_get(config, "calibration.tier1.next_bar_entry", True), True)
    )
    train_fraction = float(args.train_fraction or validation_cfg.get("train_fraction", 0.70))
    train_fraction = max(0.10, min(0.90, train_fraction))
    # Horizons are trading bars; convert to calendar days for the embargo default
    # so forward-return overlap cannot leak across the split (see scripts 27/28).
    default_embargo_days = math.ceil(max(horizons) * 365.25 / 252.0) + 10
    embargo_days = int(
        args.embargo_days if args.embargo_days is not None else validation_cfg.get("embargo_days", default_embargo_days)
    )
    if embargo_days < default_embargo_days:
        LOGGER.warning(
            "Configured embargo_days=%d is below the leakage-safe calendar-day default %d for a %d-bar horizon; honoring configured value.",
            embargo_days,
            default_embargo_days,
            max(horizons),
        )
    market_sources_raw = args.market_sources if str(args.market_sources or "").strip() else None
    market_sources = [
        str(source).strip()
        for source in calibration.normalize_string_list(market_sources_raw, calibration_market_sources(config))
        if str(source).strip()
    ]
    if not market_sources:
        raise ValueError("No market sources configured for forward catalyst validation.")
    params = calibration.load_calibration_params(config)
    min_addv20 = float(
        cfg_get(
            config,
            "biotech_scoring.core_structural_veto.min_addv20",
            cfg_get(config, "multibagger.min_addv20", 1_000_000.0),
        )
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        snapshot_dates = calibration.load_snapshot_dates(
            conn,
            start_asof=start_asof,
            end_asof=end_asof,
            fridays_only=not args.include_non_fridays,
            max_snapshots=max_snapshots,
        )
        if not snapshot_dates:
            raise ValueError("No daily_features snapshot dates found for forward catalyst validation.")
        excluded_tickers = calibration.load_excluded_tickers(
            conn,
            exclude_current_removals=False,
            extra=set(),
        )
        rows = calibration.load_observations(
            conn,
            snapshot_dates,
            excluded_tickers,
            config,
            min_addv20=min_addv20,
            strict_feature_lag=strict_feature_lag,
            growth_drag_curve=params.growth_drag_curve,
            use_decomposed_risk_for_penalty=params.use_decomposed_risk_for_penalty,
        )
        if not rows:
            raise ValueError("No observations remain for forward catalyst validation.")
        asof_dates = [parsed for row in rows if (parsed := parse_date(row.get("asof_date"))) is not None]
        if not asof_dates:
            raise ValueError("Forward catalyst validation observations have no valid as-of dates.")
        tickers = {normalize_ticker(row.get("ticker")) for row in rows if normalize_ticker(row.get("ticker"))}
        benchmark_ticker = params.benchmark_ticker if params.alpha_adjustment_enabled else ""
        price_ticker_alias = calibration.load_calibration_ticker_alias_map(conn)
        market_tickers = set(tickers)
        for observation_ticker in tickers:
            canonical_price_ticker = price_ticker_alias.get(observation_ticker)
            if canonical_price_ticker:
                market_tickers.add(canonical_price_ticker)
        if benchmark_ticker:
            market_tickers.add(benchmark_ticker)
        bars_by_ticker = calibration.load_bars(
            conn,
            tickers=market_tickers,
            min_date=min(asof_dates),
            market_sources=market_sources,
        )
        calibration.apply_delisted_price_series_overlay(
            conn,
            bars_by_ticker,
            price_ticker_alias=price_ticker_alias,
            min_date=min(asof_dates),
            config=config,
        )

    enrich_forward_catalyst_diagnostics(rows, validation_cfg=validation_cfg)
    calibration.add_forward_returns(
        rows,
        bars_by_ticker,
        horizons,
        round_trip_cost_bps=params.round_trip_cost_bps,
        next_bar_entry=next_bar_entry,
        benchmark_ticker=params.benchmark_ticker if params.alpha_adjustment_enabled else "",
        benchmark_bars=bars_by_ticker.get(params.benchmark_ticker, []) if params.alpha_adjustment_enabled else [],
        price_ticker_alias=price_ticker_alias,
    )

    feature_qa_rows = build_feature_qa_rows(
        rows=rows,
        snapshot_dates=snapshot_dates,
        horizons=horizons,
        calibration=calibration,
        params=params,
    )
    source_rows = build_source_performance_rows(
        calibration=calibration,
        rows=rows,
        horizons=horizons,
        params=params,
        train_fraction=train_fraction,
        embargo_days=embargo_days,
    )
    bucket_rows = build_bucket_performance_rows(
        calibration=calibration,
        rows=rows,
        horizons=horizons,
        params=params,
        train_fraction=train_fraction,
        embargo_days=embargo_days,
    )
    flag_rows = build_flag_performance_rows(
        calibration=calibration,
        rows=rows,
        horizons=horizons,
        params=params,
        train_fraction=train_fraction,
        embargo_days=embargo_days,
    )
    ic_rows = build_ic_monitor_rows(
        calibration=calibration,
        rows=rows,
        horizons=horizons,
        params=params,
        train_fraction=train_fraction,
        embargo_days=embargo_days,
    )
    recommendation_rows = build_guardrail_recommendations(
        flag_rows,
        min_n=int(validation_cfg.get("source_min_n", 30)),
        lcb_degradation_tolerance_pct=float(validation_cfg.get("lcb_degradation_tolerance_pct", 0.75)),
        loss20_degradation_tolerance_pct=float(validation_cfg.get("loss20_degradation_tolerance_pct", 5.0)),
    )
    top_rows = build_top_ticker_rows(rows, calibration=calibration, params=params, horizons=horizons)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv_rows(
        output_dir / "forward_catalyst_feature_qa.csv",
        sorted({key for row in feature_qa_rows for key in row}),
        feature_qa_rows,
    )
    write_csv_rows(
        output_dir / "forward_catalyst_source_performance.csv",
        ["sample", "horizon_days", "return_key", "cohort", "forward_catalyst_source_group", *METRIC_KEYS],
        source_rows,
    )
    write_csv_rows(
        output_dir / "forward_catalyst_bucket_performance.csv",
        [
            "sample",
            "horizon_days",
            "return_key",
            "cohort",
            "forward_catalyst_source_group",
            "bucket_source",
            "bucket",
            *METRIC_KEYS,
        ],
        bucket_rows,
    )
    write_csv_rows(
        output_dir / "forward_catalyst_flag_performance.csv",
        ["sample", "horizon_days", "return_key", "cohort", "flag_name", "flag_value", *METRIC_KEYS],
        flag_rows,
    )
    write_csv_rows(
        output_dir / "forward_catalyst_ic_monitor.csv",
        [
            "sample",
            "horizon_days",
            "return_key",
            "cohort",
            "forward_catalyst_source_group",
            "ic_n",
            "mean_forward_catalyst_score",
            "pearson_ic",
            "spearman_ic",
            "bucket_count",
            "lcb_monotonic_positive_steps",
            "lcb_monotonic_total_steps",
            "lcb_monotonic_pass_rate_pct",
            "mean_monotonic_positive_steps",
            "mean_monotonic_total_steps",
            "mean_monotonic_pass_rate_pct",
        ],
        ic_rows,
    )
    write_csv_rows(
        output_dir / "forward_catalyst_guardrail_recommendations.csv",
        [
            "sample",
            "horizon_days",
            "cohort",
            "ctgov_n",
            "non_ctgov_n",
            "ctgov_lcb_return_pct",
            "non_ctgov_lcb_return_pct",
            "ctgov_lcb_delta_pct",
            "ctgov_loss20_pct",
            "non_ctgov_loss20_pct",
            "ctgov_loss20_delta_pct",
            "ctgov_high_n",
            "ctgov_high_lcb_return_pct",
            "ctgov_high_loss20_pct",
            "sec_or_manual_n",
            "sec_or_manual_lcb_return_pct",
            "guardrail_pass_n",
            "guardrail_pass_lcb_return_pct",
            "recommendation",
        ],
        recommendation_rows,
    )
    top_fields = [
        "asof_date",
        "ticker",
        "company_name",
        "biotech_primary_cohort",
        "forward_catalyst_source_group",
        "forward_catalyst_source",
        "forward_catalyst_event_type",
        "forward_catalyst_nearest_days",
        "forward_catalyst_confidence",
        "forward_catalyst_score",
        "forward_catalyst_unfiltered_score",
        "ctgov_forward_catalyst_score",
        "ctgov_forward_catalyst_guardrail_pass",
        "forward_catalyst_score_bucket",
        "forward_catalyst_source_url",
        "sec_catalyst_score_used",
        "catalyst_score_raw",
        "risk_for_penalty_score_raw",
        *[calibration.objective_return_key(horizon, params) for horizon in horizons],
    ]
    write_csv_rows(output_dir / "forward_catalyst_top_ticker_diagnostics.csv", top_fields, top_rows)
    LOGGER.info(
        "Forward catalyst validation complete: observations=%d snapshots=%d output_dir=%s shadow_only=true",
        len(rows),
        len(snapshot_dates),
        output_dir,
    )


if __name__ == "__main__":
    main()
