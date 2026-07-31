from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from industrials.core.config import cfg_get, resolve_path
from industrials.core.oos_research import artifact_sha256, parse_date
from industrials.core.reports import write_csv_atomic


PRODUCTION_LOCK_FIELDS = [
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


@dataclass(frozen=True)
class ProductionLock:
    model_family: str
    lock_id: str
    effective_from: date
    effective_to: date | None
    lock_date: date
    train_start_date: date
    train_end_date: date
    scoring_mode: str
    score_model_version: str
    validation_method: str
    decision_manifest_path: Path
    decision_manifest_sha256: str
    weights: dict[str, float]


def _read_registry(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Production lock registry not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if list(reader.fieldnames or []) != PRODUCTION_LOCK_FIELDS:
            raise ValueError(f"Production lock registry header mismatch: {path}")
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in reader
        ]


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _validated_ranges(
    rows: list[dict[str, str]],
) -> list[tuple[date, date | None, dict[str, str]]]:
    output: list[tuple[date, date | None, dict[str, str]]] = []
    seen: set[str] = set()
    for row in rows:
        if not _enabled(row.get("enabled")):
            continue
        lock_id = row.get("lock_id", "")
        if not lock_id or lock_id in seen:
            raise ValueError(f"Blank or duplicate production lock id={lock_id!r}")
        seen.add(lock_id)
        start = parse_date(
            row.get("effective_from"),
            field=f"{lock_id}.effective_from",
        )
        end = (
            parse_date(
                row.get("effective_to"),
                field=f"{lock_id}.effective_to",
            )
            if row.get("effective_to")
            else None
        )
        if end is not None and end < start:
            raise ValueError(f"Production lock {lock_id} range is reversed")
        output.append((start, end, row))
    output.sort(key=lambda item: item[0])
    for previous, current in zip(output, output[1:]):
        previous_start, previous_end, previous_row = previous
        current_start, current_end, current_row = current
        if previous_end is None or previous_end >= current_start:
            raise ValueError(
                "Production lock ranges overlap: "
                f"{previous_row['lock_id']}={previous_start}.."
                f"{previous_end or 'open'} and "
                f"{current_row['lock_id']}={current_start}.."
                f"{current_end or 'open'}"
            )
    return output


def load_effective_production_lock(
    config: Mapping[str, Any],
    *,
    model_family: str,
    base_dir: Path,
    asof: str,
) -> ProductionLock | None:
    prefix = f"oos_calibration_standards.families.{model_family}"
    registry_value = str(
        cfg_get(
            dict(config),
            f"{prefix}.production_lock_registry_csv",
            "",
        )
        or ""
    ).strip()
    if not registry_value:
        return None
    registry_path = resolve_path(registry_value, base_dir=base_dir)
    target = parse_date(asof, field="production lock asof")
    matches = [
        row
        for start, end, row in _validated_ranges(
            _read_registry(registry_path)
        )
        if start <= target and (end is None or target <= end)
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            f"Multiple {model_family} production locks match {asof}"
        )
    row = matches[0]
    lock_id = row["lock_id"]
    effective_from = parse_date(
        row["effective_from"],
        field=f"{lock_id}.effective_from",
    )
    effective_to = (
        parse_date(
            row["effective_to"],
            field=f"{lock_id}.effective_to",
        )
        if row["effective_to"]
        else None
    )
    lock_date = parse_date(
        row["lock_date"],
        field=f"{lock_id}.lock_date",
    )
    train_start = parse_date(
        row["train_start_date"],
        field=f"{lock_id}.train_start_date",
    )
    train_end = parse_date(
        row["train_end_date"],
        field=f"{lock_id}.train_end_date",
    )
    if not (
        train_start <= train_end <= lock_date <= effective_from
    ):
        raise ValueError(
            f"Production lock {lock_id} dates are out of order"
        )
    decision_path = resolve_path(
        row["decision_manifest_path"],
        base_dir=base_dir,
    )
    if not decision_path.is_file():
        raise FileNotFoundError(
            f"Production decision manifest not found: {decision_path}"
        )
    actual_hash = artifact_sha256(decision_path)
    if actual_hash != row["decision_manifest_sha256"].lower():
        raise ValueError(
            f"Production decision hash mismatch for {lock_id}"
        )
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    if (
        decision.get("model_family") != model_family
        or decision.get("status") != "pass"
        or decision.get("promoted") is not True
    ):
        raise ValueError(
            f"Production decision is not a passing {model_family} promotion"
        )
    if str(decision.get("asof_date") or "") != effective_from.isoformat():
        raise ValueError(
            f"Production decision effective date disagrees with {lock_id}"
        )
    payload = decision.get("promotion_payload") or {}
    weights = payload.get("weights") or decision.get("weights") or {}
    if not isinstance(weights, dict) or not weights:
        raise ValueError(
            f"Production decision {decision_path} has no weights"
        )
    parsed_weights = {
        str(field): float(value)
        for field, value in weights.items()
    }
    if (
        any(value < 0 for value in parsed_weights.values())
        or abs(sum(parsed_weights.values()) - 1.0) > 1e-9
    ):
        raise ValueError(
            f"Production decision {decision_path} has invalid weights"
        )
    return ProductionLock(
        model_family=model_family,
        lock_id=lock_id,
        effective_from=effective_from,
        effective_to=effective_to,
        lock_date=lock_date,
        train_start_date=train_start,
        train_end_date=train_end,
        scoring_mode=row["scoring_mode"],
        score_model_version=row["score_model_version"],
        validation_method=row["validation_method"],
        decision_manifest_path=decision_path,
        decision_manifest_sha256=actual_hash,
        weights=parsed_weights,
    )


def append_production_lock(
    *,
    registry_path: Path,
    row: Mapping[str, object],
) -> None:
    existing = (
        _read_registry(registry_path)
        if registry_path.is_file()
        else []
    )
    candidate = {
        field: str(row.get(field, "") or "").strip()
        for field in PRODUCTION_LOCK_FIELDS
    }
    if not candidate["lock_id"]:
        raise ValueError("Production lock id is required")
    if any(
        item.get("lock_id") == candidate["lock_id"]
        for item in existing
    ):
        raise ValueError(
            f"Production lock id already exists: {candidate['lock_id']}"
        )
    combined = [*existing, candidate]
    _validated_ranges(combined)
    write_csv_atomic(
        registry_path,
        PRODUCTION_LOCK_FIELDS,
        combined,
    )
