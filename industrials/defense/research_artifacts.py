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


PRODUCTION_PROMOTION_STATUS = "production_oos_validated"
PRODUCTION_PROMOTION_METHOD = "weekly_pit_panel_validation_ic_holdout_backtest"
PRODUCTION_SCORING_CONTRACT_VERSION = "tech_family_final_rank_table_v1_production"
LOCK_CONFIG_PREFIX = "oos_calibration_standards.families.defense"
PRODUCTION_LOCK_REGISTRY_FIELDS = [
    "lock_id",
    "effective_from",
    "effective_to",
    "lock_date",
    "train_start_date",
    "train_end_date",
    "scoring_mode",
    "score_model_version",
    "validation_method",
    "decision_manifest_path",
    "decision_manifest_sha256",
    "enabled",
    "created_at_utc",
]


def _validated_production_lock(
    *,
    lock_id: str,
    effective_from_raw: str,
    effective_to_raw: str,
    lock_date_raw: str,
    train_start_raw: str,
    train_end_raw: str,
    scoring_mode: str,
    score_model_version: str,
    validation_method: str,
    decision_path: Path,
    expected_decision_sha256: str = "",
) -> dict[str, Any]:
    effective_from = parse_required_date(
        effective_from_raw,
        field=f"{lock_id}.effective_from",
    )
    effective_to = (
        parse_required_date(effective_to_raw, field=f"{lock_id}.effective_to")
        if effective_to_raw
        else None
    )
    lock_date = parse_required_date(lock_date_raw, field=f"{lock_id}.lock_date")
    train_start = parse_required_date(
        train_start_raw,
        field=f"{lock_id}.train_start_date",
    )
    train_end = parse_required_date(
        train_end_raw,
        field=f"{lock_id}.train_end_date",
    )
    if train_end < train_start or lock_date < train_end or effective_from < lock_date:
        raise ValueError(
            f"Defense lock {lock_id!r} dates out of order: train "
            f"{train_start}..{train_end}, lock {lock_date}, effective "
            f"{effective_from}..{effective_to or 'open'}"
        )
    if effective_to is not None and effective_to < effective_from:
        raise ValueError(
            f"Defense lock {lock_id!r} effective_to precedes effective_from"
        )
    if scoring_mode not in {"baseline", "specialized_v1"}:
        raise ValueError(
            f"Defense lock {lock_id!r} has unsupported scoring_mode={scoring_mode!r}"
        )
    if not score_model_version:
        raise ValueError(f"Defense lock {lock_id!r} has no score_model_version")
    if not decision_path.is_file():
        raise FileNotFoundError(
            f"Defense lock {lock_id!r} decision manifest not found: {decision_path}"
        )
    decision_sha256 = sha256_file(decision_path)
    if expected_decision_sha256 and decision_sha256 != expected_decision_sha256.lower():
        raise ValueError(
            f"Defense lock {lock_id!r} decision-manifest hash mismatch: "
            f"expected {expected_decision_sha256.lower()} actual {decision_sha256}"
        )
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    if decision.get("promoted") is not True or str(decision.get("status") or "") != "pass":
        raise ValueError(
            f"Promotion decision at {decision_path} is not a passing promotion"
        )
    if str(decision.get("asof_date") or "") != effective_from.isoformat():
        raise ValueError(
            f"Promotion decision asof {decision.get('asof_date')!r} does not "
            f"match effective_from {effective_from}"
        )
    payload = decision.get("promotion_payload") or {}
    decision_mode = str(
        decision.get("scoring_mode") or payload.get("scoring_mode") or ""
    )
    decision_version = str(
        decision.get("score_model_version")
        or payload.get("score_model_version")
        or ""
    )
    decision_lock_id = str(
        decision.get("lock_id") or payload.get("lock_id") or ""
    )
    if decision_mode and decision_mode != scoring_mode:
        raise ValueError(
            f"Defense lock {lock_id!r} scoring_mode disagrees with decision "
            f"manifest: {scoring_mode!r} != {decision_mode!r}"
        )
    if decision_version and decision_version != score_model_version:
        raise ValueError(
            f"Defense lock {lock_id!r} score_model_version disagrees with "
            f"decision manifest: {score_model_version!r} != {decision_version!r}"
        )
    if decision_lock_id and decision_lock_id != lock_id:
        raise ValueError(
            f"Defense lock id disagrees with decision manifest: "
            f"{lock_id!r} != {decision_lock_id!r}"
        )
    raw_weights = payload.get("weights") or {}
    if not isinstance(raw_weights, dict) or not raw_weights:
        raise ValueError(
            f"Promotion decision at {decision_path} carries no promoted weights"
        )
    weights = normalize_weights(
        {str(key): float(value) for key, value in raw_weights.items()}
    )
    return {
        "lock_id": lock_id,
        "lock_date": lock_date.isoformat(),
        "production_start_date": effective_from.isoformat(),
        "effective_from": effective_from.isoformat(),
        "effective_to": effective_to.isoformat() if effective_to else "",
        "train_start_date": train_start.isoformat(),
        "train_end_date": train_end.isoformat(),
        "weights": weights,
        "scoring_mode": scoring_mode,
        "score_model_version": score_model_version,
        "validation_method": validation_method or PRODUCTION_PROMOTION_METHOD,
        "decision_manifest_path": str(decision_path),
        "decision_manifest_sha256": decision_sha256,
    }


def load_production_lock(
    config: dict[str, Any],
    *,
    base_dir: Path,
    asof: str | None = None,
) -> dict[str, Any] | None:
    """Return the effective sealed defense model lock, or None before launch.

    The effective-dated registry is authoritative when configured. Its ranges
    must not overlap and every row pins the immutable promotion-decision hash.
    The legacy single-lock keys remain a compatibility fallback only.
    """
    from industrials.core.config import cfg_get, resolve_path

    def read(name: str) -> str:
        return str(cfg_get(config, f"{LOCK_CONFIG_PREFIX}.{name}", "") or "").strip()

    registry_raw = read("production_lock_registry_csv")
    if registry_raw:
        registry_path = resolve_path(registry_raw, base_dir=base_dir)
        if not registry_path.is_file():
            raise FileNotFoundError(
                f"Configured defense production lock registry not found: {registry_path}"
            )
        with registry_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if list(reader.fieldnames or []) != PRODUCTION_LOCK_REGISTRY_FIELDS:
                raise ValueError(
                    f"Defense production lock registry header mismatch: {registry_path}"
                )
            raw_rows = [
                {str(key): str(value or "").strip() for key, value in row.items()}
                for row in reader
                if str(row.get("enabled") or "").strip().lower()
                in {"1", "true", "yes", "y"}
            ]
        if not raw_rows:
            return None
        seen_ids: set[str] = set()
        ordered: list[tuple[date, date | None, dict[str, str]]] = []
        for row in raw_rows:
            lock_id = row["lock_id"]
            if not lock_id or lock_id in seen_ids:
                raise ValueError(
                    f"Defense production lock registry has blank/duplicate "
                    f"lock_id={lock_id!r}"
                )
            seen_ids.add(lock_id)
            effective_from = parse_required_date(
                row["effective_from"],
                field=f"{lock_id}.effective_from",
            )
            effective_to = (
                parse_required_date(
                    row["effective_to"],
                    field=f"{lock_id}.effective_to",
                )
                if row["effective_to"]
                else None
            )
            if effective_to is not None and effective_to < effective_from:
                raise ValueError(
                    f"Defense lock {lock_id!r} effective range is reversed"
                )
            ordered.append((effective_from, effective_to, row))
        ordered.sort(key=lambda item: item[0])
        for index, (effective_from, effective_to, row) in enumerate(ordered):
            if index == 0:
                continue
            previous_from, previous_to, previous = ordered[index - 1]
            if previous_to is None or previous_to >= effective_from:
                raise ValueError(
                    "Defense production lock ranges overlap: "
                    f"{previous['lock_id']}={previous_from}.."
                    f"{previous_to or 'open'} and "
                    f"{row['lock_id']}={effective_from}.."
                    f"{effective_to or 'open'}"
                )
        selected: dict[str, str] | None
        if asof:
            target = parse_required_date(asof, field="production lock asof")
            matches = [
                row
                for effective_from, effective_to, row in ordered
                if effective_from <= target
                and (effective_to is None or target <= effective_to)
            ]
            if len(matches) > 1:
                raise ValueError(
                    f"Multiple defense production locks match asof={asof}"
                )
            selected = matches[0] if matches else None
        else:
            selected = ordered[-1][2]
        if selected is None:
            return None
        return _validated_production_lock(
            lock_id=selected["lock_id"],
            effective_from_raw=selected["effective_from"],
            effective_to_raw=selected["effective_to"],
            lock_date_raw=selected["lock_date"],
            train_start_raw=selected["train_start_date"],
            train_end_raw=selected["train_end_date"],
            scoring_mode=selected["scoring_mode"],
            score_model_version=selected["score_model_version"],
            validation_method=selected["validation_method"],
            decision_path=resolve_path(
                selected["decision_manifest_path"],
                base_dir=base_dir,
            ),
            expected_decision_sha256=selected["decision_manifest_sha256"],
        )

    lock_raw = read("calibration_lock_date")
    if not lock_raw or lock_raw.upper().startswith("TBD"):
        return None
    required = {
        "calibration_production_start_date": read("calibration_production_start_date"),
        "calibration_train_start_date": read("calibration_train_start_date"),
        "calibration_train_end_date": read("calibration_train_end_date"),
        "production_promotion_decision_manifest": read(
            "production_promotion_decision_manifest"
        ),
    }
    missing = sorted(
        name
        for name, value in required.items()
        if not value or value.upper().startswith("TBD")
    )
    if missing:
        raise ValueError(
            f"{LOCK_CONFIG_PREFIX}.calibration_lock_date is set ({lock_raw}) but "
            f"companion keys are missing/TBD: {missing}"
        )
    return _validated_production_lock(
        lock_id="legacy_single_lock",
        effective_from_raw=required["calibration_production_start_date"],
        effective_to_raw="",
        lock_date_raw=lock_raw,
        train_start_raw=required["calibration_train_start_date"],
        train_end_raw=required["calibration_train_end_date"],
        scoring_mode="baseline",
        score_model_version=read("calibration_provenance_version")
        or "defense_shadow_v0.1.0",
        validation_method=read("calibration_validation_method")
        or PRODUCTION_PROMOTION_METHOD,
        decision_path=resolve_path(
            required["production_promotion_decision_manifest"],
            base_dir=base_dir,
        ),
    )

def lock_mode_for_asof(lock: dict[str, Any] | None, asof: str) -> str:
    """shadow (not locked) | pre_lock (locked, asof before production start) | production."""
    if lock is None:
        return "shadow"
    return "production" if asof >= str(lock["production_start_date"]) else "pre_lock"


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


def select_weekly_snapshot_dates(
    snapshot_dates: list[str],
    *,
    weekly_start_date: str,
    selection: str = "last",
) -> list[str]:
    """Select one available snapshot per weekly bucket.

    ``weekly_start_date`` defines the bucket anchor. A Sunday anchor such as
    2026-01-04 creates buckets [2026-01-04, 2026-01-10],
    [2026-01-11, 2026-01-17], and so on. The selected date must already exist
    in ``snapshot_dates``; this helper never fabricates market dates.
    """
    anchor = parse_required_date(weekly_start_date, field="weekly_start_date")
    if selection not in {"first", "last"}:
        raise ValueError(f"weekly selection must be 'first' or 'last', got {selection!r}")
    buckets: dict[int, list[str]] = {}
    for raw_date in sorted(set(snapshot_dates)):
        parsed = parse_date(raw_date, field="snapshot_date")
        if parsed is None or parsed < anchor:
            continue
        bucket = (parsed - anchor).days // 7
        buckets.setdefault(bucket, []).append(parsed.isoformat())
    out: list[str] = []
    for bucket in sorted(buckets):
        members = sorted(buckets[bucket])
        out.append(members[0] if selection == "first" else members[-1])
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
