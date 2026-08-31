"""Operational Consumer Defensive publisher with cohort-bounded activation.

Stage 10 remains an immutable research/shadow artifact.  This module creates a
separate Portfolio Layer-facing snapshot.  In shadow mode it preserves all
zero gates.  In production mode it activates only cohorts covered by an
effective, independently signed lock and stamps every promoted row with the
lock lineage Portfolio Layer verifies again.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from consumer_defensive.core.atomic_io import atomic_text_writer
from consumer_defensive.core.stage10_publishing import (
    FINAL_RANK_FILE,
    FINAL_RANK_REQUIRED_FIELDS,
    MANIFEST_FILE as STAGE10_MANIFEST_FILE,
    VALIDATION_FILE as STAGE10_VALIDATION_FILE,
)
from future_only_evidence.production_activation import (
    effective_scope_locks,
    validate_activation_registry,
)


OPERATIONAL_MANIFEST_FILE = "consumer_defensive_operational_manifest.json"
OPERATIONAL_SCHEMA = "consumer_defensive_operational_snapshot_v1"
LOCK_FIELDS = (
    "consumer_defensive_production_lock_id",
    "consumer_defensive_production_lock_sha256",
    "consumer_defensive_model_contract_sha256",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("payload_sha256", None)
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        return fields, [dict(row) for row in reader]


def _csv_bytes(fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(
        handle,
        fieldnames=list(fields),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue().encode("utf-8")


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _finite(value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite score: {value!r}")
    return parsed


def validate_stage10_source(stage10_dir: Path, *, asof_date: str) -> Path:
    """Verify the independent Stage 10 PASS and its exact rank-file binding."""

    date.fromisoformat(asof_date)
    root = stage10_dir.expanduser().resolve()
    rank_path = root / FINAL_RANK_FILE
    validation = _read_json(root / STAGE10_VALIDATION_FILE)
    manifest = _read_json(root / STAGE10_MANIFEST_FILE)
    if validation.get("status") != "PASS":
        raise ValueError("Stage 10 independent validation is not PASS")
    if validation.get("asof_date") != asof_date:
        raise ValueError("Stage 10 validation as-of does not match")
    check_count = int(validation.get("check_count") or 0)
    if check_count <= 0 or int(validation.get("passed_check_count") or 0) != check_count:
        raise ValueError("Stage 10 validation is incomplete")
    if manifest.get("asof_date") != asof_date:
        raise ValueError("Stage 10 manifest as-of does not match")
    expected = dict(manifest.get("file_sha256s") or {}).get(FINAL_RANK_FILE)
    if not expected or _sha256_bytes(rank_path.read_bytes()) != expected:
        raise ValueError("Stage 10 rank table does not match its validated manifest")
    return rank_path


def load_activation_registry(
    registry_path: Path,
    *,
    expected_sha256: str,
    public_key_path: Path,
) -> dict[str, Any]:
    """Load a byte-pinned registry and revalidate every embedded signature."""

    raw = registry_path.expanduser().resolve().read_bytes()
    supplied = str(expected_sha256 or "").strip().lower()
    if len(supplied) != 64 or _sha256_bytes(raw) != supplied:
        raise ValueError("production activation registry hash mismatch")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("production activation registry must be an object")
    return validate_activation_registry(
        payload,
        family="consumer_defensive",
        change_control_public_key_path=public_key_path.expanduser().resolve(),
    )


def build_operational_rows(
    source_rows: Sequence[Mapping[str, Any]],
    *,
    asof_date: str,
    activation_registry: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build a shadow or selectively promoted row set without changing Stage 10."""

    date.fromisoformat(asof_date)
    locks = (
        effective_scope_locks(activation_registry, asof_date=asof_date)
        if activation_registry is not None
        else {}
    )
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in source_rows:
        row = dict(raw)
        missing = set(FINAL_RANK_REQUIRED_FIELDS) - set(row)
        if missing:
            raise ValueError(f"Stage 10 row is missing required fields: {sorted(missing)}")
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            raise ValueError(f"blank or duplicate ticker in Stage 10 rows: {ticker!r}")
        seen.add(ticker)
        if str(row.get("asof_date") or "") != asof_date:
            raise ValueError(f"{ticker}: Stage 10 row as-of mismatch")
        if str(row.get("promotion_state") or "") != "shadow_monitor":
            raise ValueError(f"{ticker}: Stage 10 source is not immutable shadow output")
        if _truthy(row.get("portfolio_candidate_gate")) or _truthy(
            row.get("oos_score_valid_flag")
        ):
            raise ValueError(f"{ticker}: Stage 10 shadow source asserts a production gate")
        _finite(row.get("final_score"))
        scope = str(row.get("calibration_cohort") or "").strip()
        if not scope:
            raise ValueError(f"{ticker}: blank calibration cohort")
        lock = locks.get(scope)
        if lock is None:
            row.update(
                {
                    "promotion_state": "shadow_monitor",
                    "portfolio_candidate_gate": 0,
                    "portfolio_candidate_status": "shadow_only",
                    "portfolio_candidate_reason": "cohort_not_effectively_promoted",
                    "research_calibration_input_eligible_flag": 0,
                    "research_calibration_reason": "cohort_not_effectively_promoted",
                    "calibration_sample_role": "excluded",
                    "stage11_calibration_input_eligible_flag": 0,
                    "stage11_calibration_input_reason": "cohort_not_effectively_promoted",
                    "oos_score_valid_flag": 0,
                    "oos_score_asof_date": "",
                    "oos_invalid_reason": "cohort_not_effectively_promoted",
                    "calibration_lock_date": "",
                    **{field: "" for field in LOCK_FIELDS},
                }
            )
        else:
            if str(row.get("score_model_version") or "") != str(
                lock.get("score_model_version") or ""
            ):
                raise ValueError(f"{ticker}: score model version is outside the active lock")
            if str(row.get("scoring_contract_version") or "") != str(
                lock.get("scoring_contract_version") or ""
            ):
                raise ValueError(f"{ticker}: scoring contract version is outside the active lock")
            score_valid = str(row.get("model_status") or "").strip().lower() == "complete"
            research_ok = score_valid and _truthy(row.get("calibration_eligible_flag"))
            candidate = research_ok and _truthy(row.get("rank_ready_flag"))
            row.update(
                {
                    "promotion_state": "promoted",
                    "portfolio_candidate_gate": int(candidate),
                    "portfolio_candidate_score": row.get("final_score", ""),
                    "portfolio_candidate_status": "eligible" if candidate else "ineligible",
                    "portfolio_candidate_reason": "ok" if candidate else "not_rank_ready_or_calibration_eligible",
                    "research_calibration_input_eligible_flag": int(research_ok),
                    "research_calibration_reason": "ok" if research_ok else "model_or_calibration_ineligible",
                    "calibration_sample_role": "strict_oos" if score_valid else "excluded",
                    "stage11_calibration_input_eligible_flag": int(research_ok),
                    "stage11_calibration_input_reason": "ok" if research_ok else "model_or_calibration_ineligible",
                    "oos_score_valid_flag": int(score_valid),
                    "oos_score_asof_date": asof_date if score_valid else "",
                    "oos_invalid_reason": "" if score_valid else "model_incomplete",
                    "calibration_lock_date": str(lock["effective_from"]),
                    LOCK_FIELDS[0]: str(lock["lock_id"]),
                    LOCK_FIELDS[1]: str(lock["payload_sha256"]),
                    LOCK_FIELDS[2]: str(lock["model_contract_sha256"]),
                }
            )
        row.pop("row_sha256", None)
        row["row_sha256"] = _canonical_sha256(row)
        output.append(row)
    output.sort(key=lambda row: (str(row["calibration_cohort"]), int(float(row["final_rank"]))))
    return output, locks


def publish_operational_snapshot(
    *,
    stage10_dir: Path,
    output_root: Path,
    asof_date: str,
    activation_registry: Mapping[str, Any] | None = None,
    activation_registry_sha256: str = "",
) -> dict[str, Any]:
    """Publish an immutable dated operational snapshot and a lineage manifest."""

    rank_path = validate_stage10_source(stage10_dir, asof_date=asof_date)
    source_fields, source_rows = _read_csv(rank_path)
    rows, locks = build_operational_rows(
        source_rows,
        asof_date=asof_date,
        activation_registry=activation_registry,
    )
    fields = [field for field in source_fields if field != "row_sha256"]
    for field in (*LOCK_FIELDS, "row_sha256"):
        if field not in fields:
            fields.append(field)
    csv_payload = _csv_bytes(fields, rows)
    output_dir = output_root.expanduser().resolve() / asof_date
    output_path = output_dir / FINAL_RANK_FILE
    promoted_scopes = sorted(locks)
    manifest: dict[str, Any] = {
        "schema_version": OPERATIONAL_SCHEMA,
        "acceptance": "PASS",
        "asof_date": asof_date,
        "mode": "bounded_production" if promoted_scopes else "shadow",
        "source_stage10_rank_sha256": _sha256_bytes(rank_path.read_bytes()),
        "activation_registry_sha256": activation_registry_sha256 if promoted_scopes else "",
        "effective_lock_sha256s": {
            scope: str(lock["payload_sha256"]) for scope, lock in sorted(locks.items())
        },
        "promoted_scopes": promoted_scopes,
        "ticker_count": len(rows),
        "promoted_ticker_count": sum(row["promotion_state"] == "promoted" for row in rows),
        "portfolio_candidate_count": sum(int(row["portfolio_candidate_gate"]) for row in rows),
        "output_file": FINAL_RANK_FILE,
        "output_sha256": _sha256_bytes(csv_payload),
    }
    manifest["payload_sha256"] = _canonical_sha256(manifest)
    manifest_payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    for path, payload in (
        (output_path, csv_payload),
        (output_dir / OPERATIONAL_MANIFEST_FILE, manifest_payload),
    ):
        if path.exists():
            if path.read_bytes() != payload:
                raise FileExistsError(f"immutable operational artifact differs: {path}")
        else:
            with atomic_text_writer(path, newline="") as handle:
                handle.write(payload.decode("utf-8"))
    return manifest


def validate_operational_snapshot(output_dir: Path) -> dict[str, Any]:
    """Independently validate the published CSV/manifest byte binding and gates."""

    root = output_dir.expanduser().resolve()
    manifest = _read_json(root / OPERATIONAL_MANIFEST_FILE)
    if manifest.get("schema_version") != OPERATIONAL_SCHEMA:
        raise ValueError("unsupported Consumer Defensive operational manifest")
    if manifest.get("payload_sha256") != _canonical_sha256(manifest):
        raise ValueError("operational manifest self-hash mismatch")
    rank_path = root / str(manifest.get("output_file") or "")
    if _sha256_bytes(rank_path.read_bytes()) != manifest.get("output_sha256"):
        raise ValueError("operational rank-table hash mismatch")
    _fields, rows = _read_csv(rank_path)
    if len(rows) != int(manifest.get("ticker_count", -1)):
        raise ValueError("operational ticker census mismatch")
    promoted = [row for row in rows if row.get("promotion_state") == "promoted"]
    if len(promoted) != int(manifest.get("promoted_ticker_count", -1)):
        raise ValueError("operational promoted ticker census mismatch")
    for row in rows:
        if row.get("promotion_state") != "promoted" and (
            _truthy(row.get("portfolio_candidate_gate"))
            or _truthy(row.get("oos_score_valid_flag"))
            or any(str(row.get(field) or "") for field in LOCK_FIELDS)
        ):
            raise ValueError(f"{row.get('ticker')}: non-promoted row asserts production authority")
        if row.get("promotion_state") == "promoted" and not all(
            str(row.get(field) or "") for field in LOCK_FIELDS
        ):
            raise ValueError(f"{row.get('ticker')}: promoted row lacks lock lineage")
    return manifest


__all__ = [
    "LOCK_FIELDS",
    "OPERATIONAL_MANIFEST_FILE",
    "build_operational_rows",
    "load_activation_registry",
    "publish_operational_snapshot",
    "validate_operational_snapshot",
    "validate_stage10_source",
]
