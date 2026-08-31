#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.oos_research import artifact_sha256  # noqa: E402
from industrials.core.production_lock import (  # noqa: E402
    PRODUCTION_LOCK_FIELDS,
    append_production_lock,
    load_effective_production_lock,
)
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.legacy_production_routes import (  # noqa: E402
    block_legacy_route,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402
from industrials.transportation.subgroup_production_lock import (  # noqa: E402
    validate_subgroup_lock_payload,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Activate a sealed transportation generic-OOS promotion through "
            "the shared effective-dated industrials production-lock contract."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--research-asof", default="2026-07-30")
    parser.add_argument("--effective-date", default="2026-07-31")
    parser.add_argument("--allow-existing-identical", action="store_true")
    return parser.parse_args()


def read_registry(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if list(reader.fieldnames or []) != PRODUCTION_LOCK_FIELDS:
            raise ValueError(f"Production lock registry header mismatch: {path}")
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def verify_artifact(path: Path, expected_hash: object, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    expected = str(expected_hash or "").strip().lower()
    actual = artifact_sha256(path)
    if not expected or actual != expected:
        raise ValueError(
            f"{label} hash mismatch expected={expected!r} actual={actual}"
        )


def relative_to_config(path: Path, *, base_dir: Path) -> str:
    return Path(os.path.relpath(path, start=base_dir)).as_posix()


def validate_activation_scoring_mode(
    decision: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    scoring_mode = str(decision.get("scoring_mode") or "")
    if scoring_mode == "subgroup_v8":
        # Structural validation remains useful to diagnostics, but this legacy
        # generic-lock writer may never activate either scoring mode.
        validate_subgroup_lock_payload(payload)
    block_legacy_route(
        "31_activate_transportation_oos_production:"
        + (scoring_mode or "missing_scoring_mode")
    )


def main() -> int:
    block_legacy_route("31_activate_transportation_oos_production")
    args = parse_args()
    research = date.fromisoformat(args.research_asof[:10])
    effective = date.fromisoformat(args.effective_date[:10])
    if effective <= research:
        raise ValueError("--effective-date must follow --research-asof")

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    standards = cfg_get(
        config,
        "oos_calibration_standards.families.transportation",
    )
    if not isinstance(standards, dict):
        raise ValueError("Missing transportation OOS standards")

    promotion_root = resolve_path(
        standards["promotion_output_root"],
        base_dir=base_dir,
    )
    output_dir = promotion_root / effective.isoformat()
    decision_path = (
        output_dir / "transportation_production_promotion_manifest.json"
    )
    if not decision_path.is_file():
        raise FileNotFoundError(decision_path)
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    payload = decision.get("promotion_payload") or {}
    if (
        decision.get("model_family") != "transportation"
        or decision.get("status") != "pass"
        or decision.get("promoted") is not True
        or decision.get("activated") is not False
        or decision.get("asof_date") != effective.isoformat()
        or decision.get("research_asof_date") != research.isoformat()
        or payload.get("effective_date") != effective.isoformat()
        or payload.get("research_asof_date") != research.isoformat()
    ):
        raise ValueError("Promotion decision does not match activation dates")
    validate_activation_scoring_mode(decision, payload)

    readiness_path = Path(str(payload.get("readiness_audit_path") or ""))
    calibration_path = Path(str(payload.get("calibration_manifest_path") or ""))
    rank_path = Path(str(decision.get("rank_table") or ""))
    rank_manifest_path = Path(str(decision.get("rank_manifest") or ""))
    verify_artifact(
        readiness_path,
        payload.get("readiness_audit_sha256"),
        label="readiness audit",
    )
    verify_artifact(
        calibration_path,
        payload.get("calibration_manifest_sha256"),
        label="calibration manifest",
    )
    verify_artifact(
        rank_path,
        decision.get("rank_table_sha256"),
        label="promoted rank table",
    )
    verify_artifact(
        rank_manifest_path,
        decision.get("rank_manifest_sha256"),
        label="promoted rank manifest",
    )

    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    if (
        readiness.get("promotion_readiness") != "PASS"
        or readiness.get("promotion_eligible") is not True
        or readiness.get("asof_date") != research.isoformat()
        or calibration.get("promotion_eligible") is not True
    ):
        raise ValueError("Promotion evidence is not activation eligible")

    rank_rows = read_rows(rank_path)
    if not rank_rows:
        raise ValueError("Promoted rank table is empty")
    if {row.get("asof_date") for row in rank_rows} != {effective.isoformat()}:
        raise ValueError("Promoted rank rows do not match effective date")
    candidate_count = sum(
        row.get("portfolio_candidate_gate") == "1" for row in rank_rows
    )
    oos_count = sum(
        row.get("oos_score_valid_flag") == "1" for row in rank_rows
    )
    if candidate_count <= 0 or oos_count <= 0:
        raise ValueError("Promoted rank has no investable OOS-valid rows")

    train_start = date.fromisoformat(str(payload["train_start_date"]))
    train_end = date.fromisoformat(str(payload["train_end_date"]))
    if not train_start <= train_end <= research < effective:
        raise ValueError("Activation date lineage is out of order")
    lock_id = str(decision.get("lock_id") or "")
    registry_path = resolve_path(
        standards["production_lock_registry_csv"],
        base_dir=base_dir,
    )
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lock_row: dict[str, Any] = {
        "lock_id": lock_id,
        "effective_from": effective.isoformat(),
        "effective_to": "",
        "lock_date": research.isoformat(),
        "train_start_date": train_start.isoformat(),
        "train_end_date": train_end.isoformat(),
        "scoring_mode": str(decision.get("scoring_mode") or ""),
        "score_model_version": str(
            decision.get("score_model_version") or ""
        ),
        "validation_method": str(payload.get("validation_method") or ""),
        "decision_manifest_path": relative_to_config(
            decision_path,
            base_dir=base_dir,
        ),
        "decision_manifest_sha256": artifact_sha256(decision_path),
        "enabled": "1",
        "created_at_utc": created,
    }
    existing = next(
        (
            row
            for row in read_registry(registry_path)
            if row.get("lock_id") == lock_id
        ),
        None,
    )
    if existing is not None:
        expected = {
            field: str(lock_row.get(field) or "")
            for field in PRODUCTION_LOCK_FIELDS
            if field != "created_at_utc"
        }
        mismatches = {
            field: (existing.get(field, ""), value)
            for field, value in expected.items()
            if existing.get(field, "") != value
        }
        if mismatches or not args.allow_existing_identical:
            raise ValueError(
                f"Existing production lock blocks activation: {mismatches}"
            )
    else:
        append_production_lock(registry_path=registry_path, row=lock_row)

    loaded = load_effective_production_lock(
        config,
        model_family="transportation",
        base_dir=base_dir,
        asof=effective.isoformat(),
    )
    if loaded is None or loaded.lock_id != lock_id:
        raise ValueError("Shared production-lock loader did not resolve activation")

    result = {
        "artifact_family": "transportation_production_activation",
        "model_family": "transportation",
        "acceptance": "PASS",
        "activated": True,
        "research_asof_date": research.isoformat(),
        "effective_date": effective.isoformat(),
        "lock_id": lock_id,
        "registry_path": str(registry_path),
        "registry_sha256": artifact_sha256(registry_path),
        "decision_manifest_path": str(decision_path),
        "decision_manifest_sha256": artifact_sha256(decision_path),
        "portfolio_candidate_rows": candidate_count,
        "oos_score_valid_rows": oos_count,
        "created_at_utc": created,
    }
    activation_path = (
        output_dir / "transportation_production_activation_manifest.json"
    )
    write_text_atomic(
        activation_path,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
