#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.oos_research import artifact_sha256  # noqa: E402
from industrials.core.production_lock import ProductionLock  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.scoring import (  # noqa: E402
    finalize_rank_rows,
    publish_dashboard,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a sealed transportation production candidate bundle "
            "only when the independent readiness audit passes. This stage "
            "does not activate the effective-dated lock."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", default="2026-07-30")
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    effective = date.fromisoformat(args.asof[:10])
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, "transportation")
    standards = cfg_get(
        config,
        "oos_calibration_standards.families.transportation",
    )
    research_root = resolve_path(
        standards["research_output_root"],
        base_dir=base_dir,
    )
    readiness_path = (
        research_root
        / "transportation_production_readiness_audit.json"
    )
    calibration_path = (
        research_root
        / "transportation_generic_oos_calibration_manifest.json"
    )
    if not readiness_path.is_file() or not calibration_path.is_file():
        raise FileNotFoundError(
            "Readiness audit and calibration manifest are required"
        )
    readiness = json.loads(
        readiness_path.read_text(encoding="utf-8")
    )
    calibration = json.loads(
        calibration_path.read_text(encoding="utf-8")
    )
    if (
        readiness.get("asof_date") != effective.isoformat()
        or readiness.get("promotion_readiness") != "PASS"
        or readiness.get("promotion_eligible") is not True
        or calibration.get("promotion_eligible") is not True
    ):
        raise ValueError(
            "Transportation production promotion blocked: "
            + "; ".join(readiness.get("issues") or [])
        )
    source_rank = resolve_path(
        family["scoring"]["dashboard_root"],
        base_dir=base_dir,
    ) / effective.isoformat() / "transportation_final_rank_table.csv"
    if not source_rank.is_file():
        raise FileNotFoundError(source_rank)
    source_rows = read_rows(source_rank)
    if any(
        row.get("oos_score_valid_flag") != "0"
        or row.get("portfolio_candidate_gate") != "0"
        for row in source_rows
    ):
        raise ValueError("Promotion source is not a fail-closed shadow")
    weights = {
        str(field): float(value)
        for field, value in (
            calibration.get("selected_weights") or {}
        ).items()
    }
    train_start = date.fromisoformat(calibration["train_start_date"])
    train_end = date.fromisoformat(calibration["train_end_date"])
    lock_id = (
        f"transportation_generic_oos_v1_"
        f"{effective.strftime('%Y%m%d')}"
    )
    lock = ProductionLock(
        model_family="transportation",
        lock_id=lock_id,
        effective_from=effective,
        effective_to=None,
        lock_date=effective,
        train_start_date=train_start,
        train_end_date=train_end,
        scoring_mode="generic_oos",
        score_model_version="transportation_generic_oos_v1",
        validation_method=str(
            standards["calibration_validation_method"]
        ),
        decision_manifest_path=Path("pending_activation"),
        decision_manifest_sha256="",
        weights=weights,
    )
    promoted_rows = finalize_rank_rows(
        source_rows,
        score_model_version=str(
            family["scoring"]["score_model_version"]
        ),
        model_version=str(family["scoring"]["model_version"]),
        scoring_contract_version=str(
            family["scoring"]["scoring_contract_version"]
        ),
        production_lock=lock,
    )
    output_dir = (
        resolve_path(
            standards["promotion_output_root"],
            base_dir=base_dir,
        )
        / effective.isoformat()
    )
    decision_path = (
        output_dir
        / "transportation_production_promotion_manifest.json"
    )
    if decision_path.exists() and not args.allow_overwrite:
        raise FileExistsError(
            f"Promotion bundle already sealed: {decision_path}"
        )
    rank_manifest = publish_dashboard(
        output_dir=output_dir,
        rows=promoted_rows,
        asof=effective.isoformat(),
        allow_overwrite=args.allow_overwrite,
    )
    promoted_rank = (
        output_dir / "transportation_final_rank_table.csv"
    )
    created = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    payload = {
        "weights": weights,
        "lock_id": lock_id,
        "scoring_mode": "generic_oos",
        "score_model_version": lock.score_model_version,
        "validation_method": lock.validation_method,
        "train_start_date": train_start.isoformat(),
        "train_end_date": train_end.isoformat(),
        "readiness_audit_path": str(readiness_path),
        "readiness_audit_sha256": artifact_sha256(readiness_path),
        "calibration_manifest_path": str(calibration_path),
        "calibration_manifest_sha256": artifact_sha256(
            calibration_path
        ),
        "source_shadow_rank_path": str(source_rank),
        "source_shadow_rank_sha256": artifact_sha256(source_rank),
    }
    decision = {
        "artifact_family": "transportation_production_promotion",
        "model_family": "transportation",
        "status": "pass",
        "promoted": True,
        "activated": False,
        "asof_date": effective.isoformat(),
        "lock_id": lock_id,
        "scoring_mode": "generic_oos",
        "score_model_version": lock.score_model_version,
        "rank_table": str(promoted_rank),
        "rank_table_sha256": artifact_sha256(promoted_rank),
        "rank_manifest": str(
            output_dir
            / "transportation_final_rank_table_manifest.json"
        ),
        "rank_manifest_sha256": artifact_sha256(
            output_dir
            / "transportation_final_rank_table_manifest.json"
        ),
        "portfolio_candidate_rows": int(
            rank_manifest["portfolio_candidate_count"]
        ),
        "promotion_payload": payload,
        "created_at_utc": created,
    }
    if decision["portfolio_candidate_rows"] <= 0:
        raise ValueError(
            "Passing promotion produced zero transportation candidates"
        )
    write_text_atomic(
        decision_path,
        json.dumps(decision, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(decision, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
