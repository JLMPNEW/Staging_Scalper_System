from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable


MODEL_FAMILY = "defense"
DEFAULT_FORWARD_DAYS = 63
DEFAULT_EMBARGO_DAYS = 21
PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY = "dashboard_rank_snapshot_current_universe_replay"
PANEL_SOURCE_SURVIVORSHIP_CORRECTED = "survivorship_corrected_pit_membership_score_recompute"
PILLAR_SCORE_FIELDS = [
    "valuation_score",
    "quality_score",
    "risk_control_score",
    "positioning_score",
    "market_behavior_score",
    "growth_score",
    "sector_cycle_score",
    "defense_budget_backlog_score",
]
DEFAULT_PILLAR_WEIGHTS = {
    "valuation_score": 0.16,
    "quality_score": 0.18,
    "risk_control_score": 0.18,
    "positioning_score": 0.12,
    "market_behavior_score": 0.20,
    "growth_score": 0.16,
    "sector_cycle_score": 0.0,
    "defense_budget_backlog_score": 0.0,
}


@dataclass(frozen=True)
class PricePoint:
    bar_date: date
    value: float
    source_id: str
    price_basis: str
    price_adjustment: str


def parse_date(raw: object, *, field: str = "date") -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid {field}: {raw!r}") from exc


def parse_required_date(raw: object, *, field: str = "date") -> date:
    parsed = parse_date(raw, field=field)
    if parsed is None:
        raise ValueError(f"Missing required {field}")
    return parsed


def fmt(value: object, digits: int = 8) -> str:
    number = as_float(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def as_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def as_int(raw: object, default: int = 0) -> int:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(k): str(v or "") for k, v in row.items()} for row in csv.DictReader(handle)]


def csv_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(next(csv.reader(handle)))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
            tmp_name = handle.name
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name and Path(tmp_name).exists():
            Path(tmp_name).unlink()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def command_line() -> str:
    return " ".join(str(part) for part in sys.argv)


def rank_values(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    idx = 0
    while idx < len(indexed):
        end = idx + 1
        while end < len(indexed) and indexed[end][1] == indexed[idx][1]:
            end += 1
        avg_rank = (idx + 1 + end) / 2.0
        for original_idx, _ in indexed[idx:end]:
            ranks[original_idx] = avg_rank
        idx = end
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    x_dev = [x - x_mean for x in xs]
    y_dev = [y - y_mean for y in ys]
    denom_x = math.sqrt(sum(x * x for x in x_dev))
    denom_y = math.sqrt(sum(y * y for y in y_dev))
    if denom_x == 0 or denom_y == 0:
        return None
    return sum(x * y for x, y in zip(x_dev, y_dev)) / (denom_x * denom_y)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    return pearson(rank_values(xs), rank_values(ys))


def percentile(values: list[float], pct: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = max(0.0, min(1.0, pct)) * (len(clean) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return clean[lower]
    fraction = position - lower
    return clean[lower] * (1.0 - fraction) + clean[upper] * fraction


def mean(values: Iterable[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    return sum(clean) / len(clean) if clean else None


def stdev(values: Iterable[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    if len(clean) < 2:
        return None
    avg = sum(clean) / len(clean)
    return math.sqrt(sum((value - avg) ** 2 for value in clean) / (len(clean) - 1))


def max_drawdown(returns: list[float]) -> float | None:
    if not returns:
        return None
    peak = 1.0
    equity = 1.0
    worst = 0.0
    for ret in returns:
        equity *= 1.0 + ret
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, equity / peak - 1.0)
    return worst


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    cleaned = {field: max(0.0, float(weights.get(field, 0.0))) for field in PILLAR_SCORE_FIELDS}
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_PILLAR_WEIGHTS)
    return {field: cleaned[field] / total for field in PILLAR_SCORE_FIELDS}


def weighted_score(row: dict[str, str], weights: dict[str, float]) -> float | None:
    normalized = normalize_weights(weights)
    weighted_total = 0.0
    weight_total = 0.0
    for field, weight in normalized.items():
        value = as_float(row.get(field))
        if value is None:
            continue
        weighted_total += value * weight
        weight_total += weight
    if weight_total <= 0:
        return as_float(row.get("final_score"))
    return max(0.0, min(100.0, weighted_total / weight_total))


def random_weights(seed: int, trials: int) -> list[dict[str, float]]:
    rng = random.Random(seed)
    weights = [dict(DEFAULT_PILLAR_WEIGHTS)]
    for _ in range(max(0, trials - 1)):
        draw = {field: rng.random() for field in PILLAR_SCORE_FIELDS}
        weights.append(normalize_weights(draw))
    return weights


def split_snapshot_dates(
    snapshot_dates: list[str],
    *,
    train_fraction: float = 0.60,
    validation_fraction: float = 0.20,
) -> dict[str, str]:
    dates = sorted(set(snapshot_dates))
    if len(dates) < 3:
        return {snapshot_date: "insufficient_history" for snapshot_date in dates}
    train_cut = max(1, int(math.floor(len(dates) * train_fraction)))
    validation_cut = max(train_cut + 1, int(math.floor(len(dates) * (train_fraction + validation_fraction))))
    validation_cut = min(validation_cut, len(dates) - 1)
    out: dict[str, str] = {}
    for idx, snapshot_date in enumerate(dates):
        if idx < train_cut:
            out[snapshot_date] = "train"
        elif idx < validation_cut:
            out[snapshot_date] = "validation"
        else:
            out[snapshot_date] = "holdout"
    return out


def forward_window_calendar_days(forward_days: int, embargo_days: int) -> int:
    """Approximate a trading-day forward window as calendar days, plus embargo.

    Forward returns are computed over ``forward_days`` trading bars; splits are
    keyed by calendar snapshot dates, so purging needs a calendar-day bound
    (5 trading days ~ 7 calendar days, rounded up).
    """
    return int(math.ceil(max(0, forward_days) * 7.0 / 5.0)) + max(0, embargo_days)


def purged_split_snapshot_dates(
    snapshot_dates: list[str],
    *,
    forward_days: int,
    embargo_days: int,
    train_fraction: float = 0.60,
    validation_fraction: float = 0.20,
) -> dict[str, str]:
    """Chronological train/validation/holdout split with purging at boundaries.

    A train (validation) snapshot whose forward-return window — plus embargo —
    reaches the first validation (holdout) snapshot date is relabelled
    ``embargo``: its label/outcome overlaps the later split, so keeping it in
    the earlier split leaks forward information into selection. Purged rows are
    excluded from every calibration set but stay in the panel for audit.
    """
    base = split_snapshot_dates(
        snapshot_dates,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    window_days = forward_window_calendar_days(forward_days, embargo_days)
    if window_days <= 0:
        return base
    boundary_after = {"train": "validation", "validation": "holdout"}
    first_date_of: dict[str, date] = {}
    for snapshot_date, split_name in base.items():
        parsed = parse_date(snapshot_date, field="snapshot_date")
        if parsed is None:
            continue
        current = first_date_of.get(split_name)
        if current is None or parsed < current:
            first_date_of[split_name] = parsed
    out: dict[str, str] = {}
    for snapshot_date, split_name in base.items():
        next_split = boundary_after.get(split_name)
        if not next_split or next_split not in first_date_of:
            out[snapshot_date] = split_name
            continue
        parsed = parse_date(snapshot_date, field="snapshot_date")
        if parsed is None:
            out[snapshot_date] = split_name
            continue
        if (first_date_of[next_split] - parsed).days <= window_days:
            out[snapshot_date] = "embargo"
        else:
            out[snapshot_date] = split_name
    return out


def split_rows(snapshot_dates: list[str], split_map: dict[str, str], *, embargo_days: int) -> list[dict[str, str]]:
    dates = sorted(set(snapshot_dates))
    out: list[dict[str, str]] = []
    for split_name in ["train", "validation", "holdout", "embargo", "insufficient_history"]:
        members = [snapshot_date for snapshot_date in dates if split_map.get(snapshot_date) == split_name]
        if not members:
            continue
        out.append(
            {
                "split_name": split_name,
                "start_date": members[0],
                "end_date": members[-1],
                "snapshot_count": str(len(members)),
                "embargo_days": str(embargo_days),
                "role": (
                    "not_calibratable"
                    if split_name == "insufficient_history"
                    else "purged_boundary_overlap"
                    if split_name == "embargo"
                    else "research_only"
                ),
            }
        )
    return out


def latest_valid_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload

