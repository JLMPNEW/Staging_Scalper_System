"""Deterministic in-memory evidence files for one registered validation cell."""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from factor_validation.acceptance import (
    AcceptanceRecord,
    build_acceptance_record,
    registered_fdr_decisions,
)
from factor_validation.core import FactorValidationResult, PerDateDiagnostic
from factor_validation.fdr import FDRDecision
from factor_validation.registry import CampaignRegistry, canonical_json_bytes


EVIDENCE_SCHEMA_VERSION = "factor_validation_evidence_v1"
CONTENT_FILE_NAMES = (
    "acceptance.json",
    "campaign_registry.json",
    "fdr_family.json",
    "per_date_ic.csv",
    "quantile_diagnostics.csv",
    "summary.json",
)
PER_DATE_IC_HEADER = (
    "as_of_date",
    "regime",
    "observation_count",
    "spearman_ic",
)
QUANTILE_DIAGNOSTICS_HEADER = (
    "as_of_date",
    "quantile_eligible",
    "quantile_failure_reason",
    "gross_top_minus_bottom",
    "net_top_minus_bottom",
    "quantile_monotonicity",
    "top_bucket_turnover",
    "two_leg_turnover",
    "quantile_bucket_counts",
)


@dataclass(frozen=True)
class EvidenceFile:
    name: str
    data: bytes


@dataclass(frozen=True)
class EvidenceFiles:
    acceptance: AcceptanceRecord
    family_decisions: tuple[FDRDecision, ...]
    files: tuple[EvidenceFile, ...]

    def by_name(self) -> dict[str, bytes]:
        return {item.name: item.data for item in self.files}


def _csv_value(value: Any) -> str | int:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("CSV evidence contains a non-finite float")
        return format(value, ".17g")
    return str(value)


def _csv_bytes(header: tuple[str, ...], rows: list[tuple[Any, ...]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        if len(row) != len(header):
            raise ValueError("evidence CSV row length does not match header")
        writer.writerow([_csv_value(value) for value in row])
    return output.getvalue().encode("utf-8")


def _per_date_ic_bytes(rows: tuple[PerDateDiagnostic, ...]) -> bytes:
    values = [
        (
            row.as_of_date.isoformat(),
            row.regime,
            row.observation_count,
            row.spearman_ic,
        )
        for row in rows
    ]
    return _csv_bytes(PER_DATE_IC_HEADER, values)


def _quantile_bytes(rows: tuple[PerDateDiagnostic, ...]) -> bytes:
    values = [
        (
            row.as_of_date.isoformat(),
            row.quantile_eligible,
            row.quantile_failure_reason,
            row.gross_top_minus_bottom,
            row.net_top_minus_bottom,
            row.quantile_monotonicity,
            row.top_bucket_turnover,
            row.two_leg_turnover,
            json.dumps(row.quantile_bucket_counts, separators=(",", ":")),
        )
        for row in rows
    ]
    return _csv_bytes(QUANTILE_DIAGNOSTICS_HEADER, values)


def _decision_payload(decision: FDRDecision) -> dict[str, Any]:
    return {
        "accepted": decision.accepted,
        "member_id": decision.member_id,
        "p_value": decision.p_value,
        "q_value": decision.q_value,
        "testable": decision.testable,
    }


def _family_payload(
    registry: CampaignRegistry,
    cell_id: str,
    decisions: tuple[FDRDecision, ...],
) -> dict[str, Any]:
    cell = registry.cell(cell_id)
    family = registry.family(cell.fdr_family_id)
    selected = next(item for item in decisions if item.member_id == cell.fdr_member_id)
    return {
        "alpha": family.alpha,
        "decision": _decision_payload(selected),
        "family_decisions": [_decision_payload(item) for item in decisions],
        "family_id": family.family_id,
        "family_registration_sha256": family.registration_sha256,
        "member_ids": sorted(family.member_ids),
        "schema_version": "factor_validation_fdr_evidence_v1",
    }


def build_evidence_files(
    registry: CampaignRegistry,
    *,
    cell_id: str,
    result: FactorValidationResult,
    family_results: Mapping[str, FactorValidationResult],
    supersedes_manifest_sha256: str | None = None,
) -> EvidenceFiles:
    """Build deterministic content files without performing filesystem I/O."""

    cell = registry.cell(cell_id)
    decisions = registered_fdr_decisions(
        registry,
        cell_id=cell_id,
        family_results=family_results,
    )
    acceptance = build_acceptance_record(
        registry,
        cell_id=cell_id,
        result=result,
        family_results=family_results,
        supersedes_manifest_sha256=supersedes_manifest_sha256,
    )
    summary = result.to_dict()
    summary.pop("per_date")
    summary.update(
        {
            "campaign_id": registry.campaign_id,
            "cell_id": cell.cell_id,
            "cell_registration_sha256": cell.registration_sha256,
            "declared_evaluation_step_trading_days": cell.evaluation_step_trading_days,
            "factor_direction": cell.factor_direction,
            "registry_sha256": registry.registration_sha256,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
        }
    )
    payloads = {
        "acceptance.json": canonical_json_bytes(acceptance.to_dict()),
        "campaign_registry.json": canonical_json_bytes(registry.to_dict()),
        "fdr_family.json": canonical_json_bytes(_family_payload(registry, cell_id, decisions)),
        "per_date_ic.csv": _per_date_ic_bytes(result.per_date),
        "quantile_diagnostics.csv": _quantile_bytes(result.per_date),
        "summary.json": canonical_json_bytes(summary),
    }
    if tuple(sorted(payloads)) != CONTENT_FILE_NAMES:
        raise RuntimeError("evidence file contract is incomplete")
    return EvidenceFiles(
        acceptance=acceptance,
        family_decisions=decisions,
        files=tuple(EvidenceFile(name, payloads[name]) for name in CONTENT_FILE_NAMES),
    )
