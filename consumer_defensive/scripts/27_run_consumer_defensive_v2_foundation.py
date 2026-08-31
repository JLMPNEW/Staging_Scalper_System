#!/usr/bin/env python3
"""Validate and immutably publish Consumer Defensive v2 foundation status."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.config import load_config, resolve_path  # noqa: E402
from consumer_defensive.core.market_data import write_json  # noqa: E402
from consumer_defensive.core.promotion_framework_v2 import (  # noqa: E402
    REQUIRED_COHORTS,
    framework_sha256,
    load_framework,
)
from consumer_defensive.core.shared_services import (  # noqa: E402
    audit_config_connections,
    load_shared_service_contract,
    shared_service_contract_sha256,
)
from orchestration.run_all import is_trading_day  # noqa: E402


STATUS_SCHEMA = "consumer_defensive_v2_foundation_status_v2"
_STATUS_KEYS = frozenset(
    {
        "schema_version",
        "acceptance",
        "contract_validation_acceptance",
        "non_activation_guard",
        "operational_health",
        "model_family",
        "asof_date",
        "framework_status",
        "framework_sha256",
        "shared_service_contract_sha256",
        "shared_service_audit",
        "cohort_states",
        "recalibration_required",
        "production_ready",
        "portfolio_write_enabled",
        "active_cap",
        "legacy_protocol_status",
        "next_required_artifact",
        "payload_sha256",
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "consumer_defensive/config.yaml")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "output/consumer_defensive/framework_v2",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_asof(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("--asof must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError("--asof must be a canonical ISO date")
    if parsed > date.today():
        raise ValueError("--asof cannot be future-dated")
    if not is_trading_day(parsed):
        raise ValueError("--asof must be a completed US equity trading session")
    return parsed


def validate_foundation_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    status = dict(payload)
    if set(status) != _STATUS_KEYS:
        raise ValueError(f"foundation status must contain exactly {sorted(_STATUS_KEYS)}")
    if status["schema_version"] != STATUS_SCHEMA or status["model_family"] != "consumer_defensive":
        raise ValueError("unsupported Consumer foundation status")
    _canonical_asof(status["asof_date"])
    if status["acceptance"] != "PASS" or status["contract_validation_acceptance"] != "PASS":
        raise ValueError("foundation contract validation did not pass")
    if status["non_activation_guard"] != "PASS" or status["operational_health"] != "NOT_EVALUATED":
        raise ValueError("foundation status can only certify the non-activation guard")
    expected = {
        "framework_status": "recalibration_required",
        "recalibration_required": True,
        "production_ready": False,
        "portfolio_write_enabled": False,
        "active_cap": 0.0,
        "legacy_protocol_status": "retired_archived",
        "next_required_artifact": "consumer_defensive_calibration_decision_v2",
    }
    for key, value in expected.items():
        if status[key] != value:
            raise ValueError(f"foundation status.{key} is inconsistent")
    if set(status["cohort_states"]) != REQUIRED_COHORTS or set(status["cohort_states"].values()) != {
        "benchmark_production"
    }:
        raise ValueError("foundation cohort states must remain benchmark_production")
    if (
        not isinstance(status["shared_service_audit"], Mapping)
        or status["shared_service_audit"].get("status") != "PASS"
    ):
        raise ValueError("shared-service audit did not pass")
    for key in ("framework_sha256", "shared_service_contract_sha256", "payload_sha256"):
        value = status[key]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"{key} must be a lowercase SHA-256 digest")
    if status["payload_sha256"] != _canonical_sha256(status):
        raise ValueError("foundation status self-hash mismatch")
    return status


def build_foundation_status(*, asof_date: str, config_path: Path) -> dict[str, object]:
    parsed_asof = _canonical_asof(asof_date)
    bundle = load_config(config_path)
    framework_path = resolve_path(
        bundle.payload["promotion_framework_v2"]["framework_path"],
        base_dir=bundle.base_dir,
    )
    shared_path = resolve_path(
        bundle.payload["promotion_framework_v2"]["shared_service_contract_path"],
        base_dir=bundle.base_dir,
    )
    framework = load_framework(framework_path)
    shared = load_shared_service_contract(shared_path)
    shared_hash = shared_service_contract_sha256(shared)
    if shared_hash != framework["ownership"]["shared_service_contract_sha256"]:
        raise ValueError("framework/shared-service contract hash binding failed")
    connection_audit = audit_config_connections(bundle.payload, repository_root=ROOT)
    payload: dict[str, object] = {
        "schema_version": STATUS_SCHEMA,
        "acceptance": "PASS",
        "contract_validation_acceptance": "PASS",
        "non_activation_guard": "PASS",
        "operational_health": "NOT_EVALUATED",
        "model_family": "consumer_defensive",
        "asof_date": parsed_asof.isoformat(),
        "framework_status": "recalibration_required",
        "framework_sha256": framework_sha256(framework),
        "shared_service_contract_sha256": shared_hash,
        "shared_service_audit": connection_audit,
        "cohort_states": {cohort: "benchmark_production" for cohort in sorted(REQUIRED_COHORTS)},
        "recalibration_required": True,
        "production_ready": False,
        "portfolio_write_enabled": False,
        "active_cap": 0.0,
        "legacy_protocol_status": "retired_archived",
        "next_required_artifact": "consumer_defensive_calibration_decision_v2",
    }
    payload["payload_sha256"] = _canonical_sha256(payload)
    return validate_foundation_status(payload)


def _publish_idempotently(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        validate_foundation_status(existing)
        if existing != payload:
            raise FileExistsError(f"refusing to overwrite divergent same-asof foundation artifact: {path}")
        return
    write_json(path, payload)


def main() -> int:
    args = _parser().parse_args()
    payload = build_foundation_status(asof_date=args.asof, config_path=args.config)
    output = args.output_root.expanduser().resolve() / args.asof / "consumer_defensive_framework_status.json"
    if not args.dry_run:
        _publish_idempotently(output, payload)
    print(
        json.dumps(
            {**payload, "output": None if args.dry_run else str(output)},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
