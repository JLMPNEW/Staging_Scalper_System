#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import load_yaml, resolve_path  # noqa: E402
from biotech_index.core.promotion_contract import (  # noqa: E402
    SUPPORTED_COHORT_CONTRACT_VERSION,
    SUPPORTED_CONTRACT_VERSION,
    SUPPORTED_CONTRACT_VERSIONS,
    PromotionContractError,
    sha256_file,
    validate_cohort_contract,
    validate_contract_scoring_parity,
    validate_monitoring_contract,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Activate an authorized biotech promotion candidate as an immutable, effective-dated live contract. "
            "This does not edit config; it writes a pinned contract and an activation receipt."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--candidate-contract", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--effective-date", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Immutable activation artifact already exists: {path}")
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def append_registry(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def fold_evidence_end_date(fold: object, *, context: str) -> date:
    if not isinstance(fold, dict):
        raise PromotionContractError(f"{context} lacks latest_primary_fold_contract")
    comparison = fold.get("outer_test_comparison_row") or fold.get("outer_test_comparison") or {}
    if not isinstance(comparison, dict):
        raise PromotionContractError(f"{context} lacks latest outer-test comparison")
    raw = str(comparison.get("paired_end_date") or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise PromotionContractError(f"{context} lacks a valid outer-test end date") from exc


def evidence_end_date(payload: dict[str, Any]) -> date:
    version = str(payload.get("contract_version") or "")
    if version == SUPPORTED_CONTRACT_VERSION:
        return fold_evidence_end_date(
            payload.get("latest_primary_fold_contract"),
            context="Candidate contract",
        )
    if version != SUPPORTED_COHORT_CONTRACT_VERSION:
        raise PromotionContractError(f"Unsupported candidate contract version: {version!r}")
    raw_contracts = payload.get("cohort_contracts") or {}
    if not isinstance(raw_contracts, dict):
        raise PromotionContractError("Candidate contract lacks cohort_contracts")
    evidence_dates = [
        fold_evidence_end_date(
            raw_contracts.get(cohort, {}).get("latest_primary_fold_contract")
            if isinstance(raw_contracts.get(cohort), dict)
            else None,
            context=f"Cohort {cohort}",
        )
        for cohort in sorted(raw_contracts)
    ]
    if not evidence_dates:
        raise PromotionContractError("Candidate contract contains no cohort evidence")
    return max(evidence_dates)


def validate_candidate_contract(
    payload: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str, tuple[str, ...]]:
    contract_version = str(payload.get("contract_version") or "")
    if contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        raise PromotionContractError(f"Unsupported candidate contract version: {contract_version!r}")
    if payload.get("production_promotion_authorized") is not True:
        raise PromotionContractError("Candidate contract is not authorized for production")
    if str(payload.get("activation_status") or "") != "candidate_requires_explicit_activation":
        raise PromotionContractError("Candidate contract is not in an activatable state")
    if contract_version == SUPPORTED_COHORT_CONTRACT_VERSION:
        active = validate_cohort_contract(payload)
        candidate_id = "cohort-suite"
        authorized_cohorts = tuple(sorted(active))
    else:
        validate_contract_scoring_parity(payload, config)
        fold = payload.get("latest_primary_fold_contract") or {}
        if not isinstance(fold, dict):
            raise PromotionContractError("Candidate contract lacks latest_primary_fold_contract")
        candidate_id = str(fold.get("candidate_id") or "").strip()
        authorized_cohorts = ()
    validate_monitoring_contract(payload)
    if not candidate_id:
        raise PromotionContractError("Candidate contract has no candidate_id")
    return candidate_id, authorized_cohorts


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    candidate_path = args.candidate_contract.expanduser().resolve()
    expected_sha = str(args.expected_sha256).strip().lower()
    actual_sha = sha256_file(candidate_path)
    if actual_sha.lower() != expected_sha:
        raise PromotionContractError(
            f"Candidate contract hash mismatch: expected={expected_sha} actual={actual_sha}"
        )
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PromotionContractError("Candidate contract root must be a JSON object")
    config = load_yaml(config_path)
    candidate_id, authorized_cohorts = validate_candidate_contract(payload, config)
    try:
        effective_date = date.fromisoformat(str(args.effective_date).strip())
    except ValueError as exc:
        raise PromotionContractError("--effective-date must be an ISO date") from exc
    outer_end = evidence_end_date(payload)
    if effective_date <= outer_end:
        raise PromotionContractError(
            f"Effective date {effective_date} must be after untouched evidence end date {outer_end}"
        )
    if effective_date < datetime.now(timezone.utc).date():
        raise PromotionContractError("Retroactive promotion activation is forbidden")
    approved_by = str(args.approved_by).strip()
    if not approved_by:
        raise PromotionContractError("--approved-by cannot be blank")
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else resolve_path(
            "../output/biotech_index_reports/promotion_contracts",
            base_dir=config_path.parent,
        )
    )
    contract_id = f"biotech-{effective_date.isoformat()}-{candidate_id}-{actual_sha[:12]}"
    active_payload = {
        **payload,
        "activation_status": "active",
        "contract_id": contract_id,
        "effective_date": effective_date.isoformat(),
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "approved_by": approved_by,
        "candidate_contract_path": str(candidate_path),
        "candidate_contract_sha256": actual_sha,
        "strict_oos_start_date": effective_date.isoformat(),
    }
    active_path = output_dir / f"{contract_id}.json"
    atomic_write_json(active_path, active_payload)
    active_sha = sha256_file(active_path)
    receipt = {
        "status": "activated",
        "contract_id": contract_id,
        "contract_version": str(payload.get("contract_version") or ""),
        "authorized_cohorts": list(authorized_cohorts),
        "active_contract_path": str(active_path),
        "active_contract_sha256": active_sha,
        "candidate_contract_sha256": actual_sha,
        "effective_date": effective_date.isoformat(),
        "approved_by": approved_by,
        "config_activation": {
            "biotech_scoring": {
                "adaptive_promotion_contract": {
                    "enabled": True,
                    "path": str(active_path),
                    "sha256": active_sha,
                }
            }
        },
    }
    receipt_path = output_dir / f"{contract_id}.activation_receipt.json"
    atomic_write_json(receipt_path, receipt)
    append_registry(output_dir / "activation_registry.jsonl", receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
