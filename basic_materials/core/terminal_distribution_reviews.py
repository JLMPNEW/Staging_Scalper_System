"""Evidence hydration, atomic application, and validation for F0.2 reviews."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping

import requests

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import (
    atomic_write_bytes,
    atomic_write_csv,
    atomic_write_json,
)
from basic_materials.core.config import BasicMaterialsConfig
from basic_materials.core.db import (
    assert_database_identity,
    database_counts,
    utc_now,
)
from basic_materials.core.terminal_distribution_contract import (
    SourceDocument,
    TerminalDistributionContractError,
    TerminalDistributionManifest,
    TerminalDistributionPolicy,
    TerminalDistributionReview,
    sha256_bytes,
)


@dataclass(frozen=True)
class EvidencePayload:
    document_key: str
    path: Path
    sha256: str
    byte_size: int
    cache_status: str
    payload: bytes

    def report_dict(self, document: SourceDocument) -> dict[str, Any]:
        return {
            "document_key": self.document_key,
            "event_key": document.event_key,
            "document_role": document.document_role,
            "url": document.url,
            "document_date": document.document_date,
            "cache_path": str(self.path),
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "cache_status": self.cache_status,
        }


@dataclass(frozen=True)
class TerminalDistributionLoadStats:
    policy_version: str
    policy_sha256: str
    manifest_sha256: str
    review_rows: int
    source_documents: int
    raw_payload_rows: int
    zero_distribution_rows: int
    value_distribution_rows: int
    unresolved_noncash_rows: int
    updated_terminal_rules: int
    calibration_activated: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)


def _normalized_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _document_text(payload: bytes) -> str:
    parser = _HtmlTextExtractor()
    parser.feed(payload.decode("utf-8", errors="replace"))
    return _normalized_text(" ".join(parser.parts))


def _request_bytes(
    url: str,
    *,
    user_agent: str,
    timeout_seconds: float,
    max_retries: int,
    interval_seconds: float,
) -> bytes:
    last_error = "no response"
    for attempt in range(max_retries):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=timeout_seconds,
            )
            if response.status_code == 200 and response.content:
                time.sleep(interval_seconds)
                return response.content
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < max_retries:
            time.sleep(max(interval_seconds, 0.25) * (attempt + 1))
    raise TerminalDistributionContractError(
        f"Unable to fetch terminal-distribution evidence {url}: {last_error}"
    )


def hydrate_terminal_distribution_evidence(
    *,
    config: BasicMaterialsConfig,
    policy: TerminalDistributionPolicy,
    manifest: TerminalDistributionManifest,
    reviews: tuple[TerminalDistributionReview, ...],
    cache_only: bool = False,
) -> tuple[EvidencePayload, ...]:
    """Fetch or replay exact source bytes and verify each evidence phrase."""

    required_keys = {
        review.primary_document_key
        for review in reviews
    } | {
        str(review.supporting_document_key)
        for review in reviews
        if review.supporting_document_key
    }
    if required_keys != set(manifest.source_documents):
        raise TerminalDistributionContractError(
            "Review rows must use every governed source document exactly within scope"
        )
    phrases: dict[str, list[str]] = {key: [] for key in required_keys}
    for review in reviews:
        phrases[review.primary_document_key].append(review.primary_evidence_text)
        if review.supporting_document_key and review.supporting_evidence_text:
            phrases[review.supporting_document_key].append(review.supporting_evidence_text)

    cache_root = config.paths.cache_root.resolve()
    output: list[EvidencePayload] = []
    for document_key in sorted(required_keys):
        document = manifest.source_documents[document_key]
        path = (cache_root / document.cache_relative_path).resolve()
        try:
            path.relative_to(cache_root)
        except ValueError as exc:
            raise TerminalDistributionContractError(
                f"{document_key}: cache path escapes configured cache root"
            ) from exc
        status = "cache_hit"
        if path.is_file():
            payload = path.read_bytes()
        else:
            if cache_only:
                raise TerminalDistributionContractError(
                    f"Cache-only evidence is missing for {document_key}: {path}"
                )
            payload = _request_bytes(
                document.url,
                user_agent=config.sec_fundamentals.user_agent,
                timeout_seconds=config.sec_fundamentals.timeout_seconds,
                max_retries=config.sec_fundamentals.max_retries,
                interval_seconds=config.sec_fundamentals.request_interval_seconds,
            )
            if sha256_bytes(payload) != document.sha256 or len(payload) != document.byte_size:
                raise TerminalDistributionContractError(
                    f"{document_key}: downloaded bytes differ from the governed source seal"
                )
            atomic_write_bytes(path, payload)
            status = "downloaded"
        actual_sha = sha256_bytes(payload)
        if actual_sha != document.sha256 or len(payload) != document.byte_size:
            raise TerminalDistributionContractError(
                f"{document_key}: cached bytes differ from the governed source seal"
            )
        normalized = _document_text(payload)
        for evidence_phrase in phrases[document_key]:
            phrase = _normalized_text(evidence_phrase)
            if not phrase or phrase not in normalized:
                raise TerminalDistributionContractError(
                    f"{document_key}: governed evidence phrase is absent from source bytes"
                )
        output.append(
            EvidencePayload(
                document_key=document_key,
                path=path,
                sha256=actual_sha,
                byte_size=len(payload),
                cache_status=status,
                payload=payload,
            )
        )
    return tuple(output)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_bytes(payload.encode("utf-8"))


def _review_projection(
    review: TerminalDistributionReview,
    *,
    policy: TerminalDistributionPolicy,
    manifest: TerminalDistributionManifest,
) -> dict[str, Any]:
    base = policy.payload["base_contract"]
    primary = manifest.source_documents[review.primary_document_key]
    supporting = (
        manifest.source_documents[review.supporting_document_key]
        if review.supporting_document_key
        else None
    )
    projection: dict[str, Any] = {
        "event_key": review.event_key,
        "ticker": review.ticker,
        "review_status": review.review_status,
        "bankruptcy_distribution_value": review.bankruptcy_distribution_value,
        "distribution_currency": review.distribution_currency,
        "source_id": review.source_id,
        "primary_document_key": primary.document_key,
        "primary_source_url": primary.url,
        "primary_source_document_date": primary.document_date,
        "primary_source_sha256": primary.sha256,
        "primary_cache_relative_path": primary.cache_relative_path,
        "primary_evidence_locator": review.primary_evidence_locator,
        "primary_evidence_text": review.primary_evidence_text,
        "supporting_document_key": supporting.document_key if supporting else None,
        "supporting_source_url": supporting.url if supporting else None,
        "supporting_source_document_date": supporting.document_date if supporting else None,
        "supporting_source_sha256": supporting.sha256 if supporting else None,
        "supporting_cache_relative_path": supporting.cache_relative_path if supporting else None,
        "supporting_evidence_locator": review.supporting_evidence_locator,
        "supporting_evidence_text": review.supporting_evidence_text,
        "reviewed_on": review.reviewed_on,
        "notes": review.notes,
        "base_terminal_rules_sha256": str(base["terminal_return_rules_sha256"]),
        "overlay_policy_version": policy.version,
        "overlay_policy_sha256": policy.checksum,
        "overlay_manifest_sha256": manifest.checksum,
    }
    projection["review_row_sha256"] = _canonical_hash(projection)
    return projection


def _assert_closed_gates(conn: sqlite3.Connection) -> None:
    control = conn.execute(
        """
        SELECT promotion_state, portfolio_candidate_gate, oos_score_valid_flag,
               current_universe_is_survivorship_corrected,
               current_universe_calibration_eligible
        FROM model_control_state WHERE identity_id = 1
        """
    ).fetchone()
    if control is None or (
        str(control["promotion_state"]) != "shadow_monitor"
        or any(
            int(control[field]) != 0
            for field in (
                "portfolio_candidate_gate",
                "oos_score_valid_flag",
                "current_universe_is_survivorship_corrected",
                "current_universe_calibration_eligible",
            )
        )
    ):
        raise TerminalDistributionContractError("F0.2 requires all model-control gates closed")
    open_memberships = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM dim_universe_membership
            WHERE calibration_eligible <> 0
               OR (
                    membership_status = 'current'
                    AND survivorship_corrected <> 0
               )
            """
        ).fetchone()[0]
    )
    if open_memberships:
        raise TerminalDistributionContractError(
            "F0.2 requires calibration-ineligible memberships and an uncorrected current universe"
        )


def apply_terminal_distribution_reviews(
    conn: sqlite3.Connection,
    *,
    policy: TerminalDistributionPolicy,
    manifest: TerminalDistributionManifest,
    reviews: tuple[TerminalDistributionReview, ...],
    evidence: tuple[EvidencePayload, ...],
) -> TerminalDistributionLoadStats:
    """Atomically record reviewed evidence and overlay the four Stage 3 rules."""

    assert_database_identity(conn)
    if conn.in_transaction:
        raise RuntimeError("apply_terminal_distribution_reviews requires a clean connection")
    _assert_closed_gates(conn)
    evidence_by_key = {item.document_key: item for item in evidence}
    if set(evidence_by_key) != set(manifest.source_documents):
        raise TerminalDistributionContractError("Evidence payload set differs from the manifest")
    for document_key, document in manifest.source_documents.items():
        item = evidence_by_key[document_key]
        if (
            item.sha256 != document.sha256
            or item.byte_size != document.byte_size
            or sha256_bytes(item.payload) != document.sha256
            or len(item.payload) != document.byte_size
        ):
            raise TerminalDistributionContractError(
                f"{document_key}: evidence payload differs from the manifest seal"
            )
    source_id = str(policy.payload["review_contract"]["source_id"])
    source = conn.execute(
        "SELECT active FROM source_registry WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    if source is None or int(source["active"]) != 1:
        raise TerminalDistributionContractError(
            "Active terminal-distribution source-registry row is required"
        )

    projections = {
        review.event_key: _review_projection(review, policy=policy, manifest=manifest)
        for review in reviews
    }
    placeholders = ",".join("?" for _ in reviews)
    rules = {
        str(row["event_key"]): dict(row)
        for row in conn.execute(
            f"""
            SELECT r.*, s.ticker AS event_ticker
            FROM dim_terminal_return_rule AS r
            JOIN fact_terminal_event_reconciliation AS t ON t.event_key = r.event_key
            JOIN dim_security AS s ON s.security_id = t.security_id
            WHERE r.event_key IN ({placeholders})
            """,
            tuple(review.event_key for review in reviews),
        ).fetchall()
    }
    if set(rules) != set(projections):
        raise TerminalDistributionContractError(
            "All reviewed events must exist in the loaded Stage 3 rule contract"
        )
    existing_reviews = {
        str(row["event_key"]): dict(row)
        for row in conn.execute(
            f"SELECT * FROM fact_terminal_distribution_review WHERE event_key IN ({placeholders})",
            tuple(review.event_key for review in reviews),
        ).fetchall()
    }
    base_sha = str(policy.payload["base_contract"]["terminal_return_rules_sha256"])
    prepared_rules: dict[str, dict[str, Any]] = {}
    for review in reviews:
        rule = rules[review.event_key]
        projection = projections[review.event_key]
        if (
            str(rule["event_ticker"]).upper() != review.ticker
            or str(rule["outcome_class"]) != "bankruptcy_distribution"
            or float(rule["cash_weight"]) != 0
            or float(rule["stock_weight"]) != 0
        ):
            raise TerminalDistributionContractError(
                f"{review.event_key}: loaded terminal rule differs from F0.2 scope"
            )
        prior = existing_reviews.get(review.event_key)
        base_pending = (
            str(rule["rule_status"]) == "pending_distribution_evidence"
            and rule["bankruptcy_distribution_value"] is None
            and str(rule["contract_sha256"]) == base_sha
            and str(rule["source_id"]) == "basic_materials_terminal_return_rule_review"
        )
        same_overlay = (
            prior is not None
            and str(prior["review_row_sha256"]) == projection["review_row_sha256"]
            and str(rule["contract_sha256"]) == projection["review_row_sha256"]
            and str(rule["source_id"]) == source_id
        )
        if not base_pending and not same_overlay:
            raise TerminalDistributionContractError(
                f"{review.event_key}: refusing to overwrite a conflicting terminal-rule state"
            )
        current_evidence = json.loads(str(rule["evidence_json"]))
        base_evidence = (
            current_evidence.get("base_stage3_rule")
            if same_overlay
            else current_evidence
        )
        if not isinstance(base_evidence, Mapping):
            raise TerminalDistributionContractError(
                f"{review.event_key}: base Stage 3 evidence is not recoverable"
            )
        verified = review.review_status in {
            "zero_distribution_verified",
            "distribution_value_verified",
        }
        prepared_rules[review.event_key] = {
            "bankruptcy_distribution_value": review.bankruptcy_distribution_value,
            "distribution_currency": review.distribution_currency,
            "rule_status": (
                "ready_for_calculation" if verified else "pending_distribution_evidence"
            ),
            "evidence_json": json.dumps(
                {
                    "base_stage3_rule": base_evidence,
                    "f0_2_terminal_distribution_review": projection,
                },
                sort_keys=True,
            ),
            "contract_sha256": projection["review_row_sha256"],
        }

    now = utc_now()
    columns = tuple(next(iter(projections.values())))
    insert_columns = (*columns, "created_at_utc", "updated_at_utc")
    update_columns = tuple(
        column for column in columns if column not in {"event_key"}
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        for document_key, document in manifest.source_documents.items():
            item = evidence_by_key[document_key]
            conn.execute(
                """
                INSERT INTO raw_source_payloads (
                    snapshot_id, source_id, source_snapshot_date, source_path, sha256,
                    byte_size, row_count, media_type, payload, manifest_version,
                    ingested_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO NOTHING
                """,
                (
                    f"{source_id}:{document.sha256}",
                    source_id,
                    document.document_date,
                    document.url,
                    document.sha256,
                    document.byte_size,
                    document.media_type,
                    item.payload,
                    policy.version,
                    now,
                ),
            )
        placeholders_sql = ",".join("?" for _ in insert_columns)
        updates_sql = ",".join(
            f"{column}=excluded.{column}" for column in (*update_columns, "updated_at_utc")
        )
        conn.executemany(
            f"""
            INSERT INTO fact_terminal_distribution_review ({','.join(insert_columns)})
            VALUES ({placeholders_sql})
            ON CONFLICT(event_key) DO UPDATE SET {updates_sql}
            """,
            [
                tuple(projection[column] for column in columns) + (now, now)
                for projection in projections.values()
            ],
        )
        for event_key, values in prepared_rules.items():
            conn.execute(
                """
                UPDATE dim_terminal_return_rule
                SET bankruptcy_distribution_value = ?,
                    distribution_currency = ?,
                    rule_status = ?,
                    source_id = ?,
                    evidence_json = ?,
                    contract_version = ?,
                    contract_sha256 = ?,
                    updated_at_utc = ?
                WHERE event_key = ?
                """,
                (
                    values["bankruptcy_distribution_value"],
                    values["distribution_currency"],
                    values["rule_status"],
                    source_id,
                    values["evidence_json"],
                    policy.version,
                    values["contract_sha256"],
                    now,
                    event_key,
                ),
            )
        _assert_closed_gates(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    raw_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM raw_source_payloads WHERE source_id = ?",
            (source_id,),
        ).fetchone()[0]
    )
    return TerminalDistributionLoadStats(
        policy_version=policy.version,
        policy_sha256=policy.checksum,
        manifest_sha256=manifest.checksum,
        review_rows=len(reviews),
        source_documents=len(evidence),
        raw_payload_rows=raw_count,
        zero_distribution_rows=sum(
            review.review_status == "zero_distribution_verified" for review in reviews
        ),
        value_distribution_rows=sum(
            review.review_status == "distribution_value_verified" for review in reviews
        ),
        unresolved_noncash_rows=sum(
            review.review_status == "noncash_recovery_unresolved" for review in reviews
        ),
        updated_terminal_rules=len(prepared_rules),
        calibration_activated=False,
    )


def validate_terminal_distribution_database(
    conn: sqlite3.Connection,
    *,
    policy: TerminalDistributionPolicy,
    manifest: TerminalDistributionManifest,
    reviews: tuple[TerminalDistributionReview, ...],
    calculation_asof_date: str | None = None,
    require_reconciled: bool = False,
) -> dict[str, Any]:
    """Fail closed unless database rows match the sealed overlay exactly."""

    assert_database_identity(conn)
    _assert_closed_gates(conn)
    projections = {
        review.event_key: _review_projection(review, policy=policy, manifest=manifest)
        for review in reviews
    }
    rows = {
        str(row["event_key"]): dict(row)
        for row in conn.execute(
            """
            SELECT d.*, r.rule_status, r.source_id AS rule_source_id,
                   r.bankruptcy_distribution_value AS rule_distribution_value,
                   r.distribution_currency AS rule_distribution_currency,
                   r.contract_version AS rule_contract_version,
                   r.contract_sha256 AS rule_contract_sha256
            FROM fact_terminal_distribution_review AS d
            JOIN dim_terminal_return_rule AS r ON r.event_key = d.event_key
            """
        ).fetchall()
    }
    if set(rows) != set(projections):
        raise TerminalDistributionContractError("Database review-event set differs from contract")
    source_id = str(policy.payload["review_contract"]["source_id"])
    for event_key, expected in projections.items():
        row = rows[event_key]
        expected_status = (
            "ready_for_calculation"
            if expected["review_status"]
            in {"zero_distribution_verified", "distribution_value_verified"}
            else "pending_distribution_evidence"
        )
        if (
            str(row["review_row_sha256"]) != expected["review_row_sha256"]
            or str(row["overlay_policy_sha256"]) != policy.checksum
            or str(row["overlay_manifest_sha256"]) != manifest.checksum
            or str(row["rule_status"]) != expected_status
            or str(row["rule_source_id"]) != source_id
            or str(row["rule_contract_version"]) != policy.version
            or str(row["rule_contract_sha256"]) != expected["review_row_sha256"]
            or row["rule_distribution_value"] != expected["bankruptcy_distribution_value"]
            or row["rule_distribution_currency"] != expected["distribution_currency"]
        ):
            raise TerminalDistributionContractError(
                f"{event_key}: database overlay differs from the governed review"
            )
    raw_hashes = {
        str(row["sha256"])
        for row in conn.execute(
            "SELECT sha256 FROM raw_source_payloads WHERE source_id = ?",
            (source_id,),
        ).fetchall()
    }
    expected_hashes = {document.sha256 for document in manifest.source_documents.values()}
    if raw_hashes != expected_hashes:
        raise TerminalDistributionContractError("Database source payload hashes differ from manifest")

    terminal_calculation_rows = 0
    resolved_review_rows = 0
    total_unresolved = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_terminal_event_reconciliation WHERE resolved = 0"
        ).fetchone()[0]
    )
    if require_reconciled:
        if not calculation_asof_date:
            raise TerminalDistributionContractError("A calculation as-of date is required")
        placeholders = ",".join("?" for _ in projections)
        calculations = conn.execute(
            f"""
            SELECT event_key, calculation_status, resolved, distribution_component,
                   terminal_value, no_future_price_used
            FROM fact_terminal_return_calculation
            WHERE calculation_asof_date = ? AND event_key IN ({placeholders})
            ORDER BY event_key
            """,
            (calculation_asof_date, *sorted(projections)),
        ).fetchall()
        terminal_calculation_rows = len(calculations)
        resolved_review_rows = sum(int(row["resolved"]) for row in calculations)
        if (
            len(calculations) != len(projections)
            or any(
                int(row["resolved"]) != 1
                or str(row["calculation_status"]) != "resolved_bankruptcy_distribution"
                or float(row["distribution_component"]) != 0
                or float(row["terminal_value"]) != 0
                or int(row["no_future_price_used"]) != 1
                for row in calculations
            )
            or total_unresolved != 0
        ):
            raise TerminalDistributionContractError(
                "Terminal reconciliation does not reflect four zero recoveries"
            )
    return {
        "passed": True,
        "policy_version": policy.version,
        "policy_sha256": policy.checksum,
        "manifest_sha256": manifest.checksum,
        "database_review_rows": len(rows),
        "database_source_payloads": len(raw_hashes),
        "terminal_calculation_rows": terminal_calculation_rows,
        "resolved_review_rows": resolved_review_rows,
        "total_unresolved_terminal_events": total_unresolved,
        "calibration_activated": False,
    }


def _file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_terminal_distribution_reports(
    conn: sqlite3.Connection,
    *,
    policy: TerminalDistributionPolicy,
    manifest: TerminalDistributionManifest,
    evidence: tuple[EvidencePayload, ...],
    load_stats: Mapping[str, Any],
    terminal_stats: Mapping[str, Any],
    validation: Mapping[str, Any],
    calculation_asof_date: str,
    report_dir: str | Path,
) -> dict[str, str]:
    """Publish the F0.2 decision, source, and reconciliation evidence pack."""

    target = Path(report_dir).resolve(strict=False)
    target.mkdir(parents=True, exist_ok=True)
    summary = {
        "passed": True,
        "model_family": MODEL_FAMILY,
        "sector": SECTOR,
        "stage": "f0_2_terminal_distribution_closure",
        "contract_as_of_date": policy.as_of_date,
        "calculation_asof_date": calculation_asof_date,
        "policy_version": policy.version,
        "policy_sha256": policy.checksum,
        "manifest_sha256": manifest.checksum,
        "load": dict(load_stats),
        "terminal_reconciliation": dict(terminal_stats),
        "validation": dict(validation),
        "generated_at_utc": utc_now(),
    }
    reviews = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM fact_terminal_distribution_review
            ORDER BY event_key
            """
        ).fetchall()
    ]
    source_rows = [
        item.report_dict(manifest.source_documents[item.document_key])
        for item in evidence
    ]
    calculations = [
        dict(row)
        for row in conn.execute(
            """
            SELECT c.*, r.review_status, r.primary_source_url,
                   r.primary_source_sha256, r.supporting_source_url,
                   r.supporting_source_sha256
            FROM fact_terminal_return_calculation AS c
            JOIN fact_terminal_distribution_review AS r ON r.event_key = c.event_key
            WHERE c.calculation_asof_date = ?
            ORDER BY c.event_key
            """,
            (calculation_asof_date,),
        ).fetchall()
    ]
    written: dict[str, Path] = {}
    written["summary"] = atomic_write_json(
        target / "terminal_distribution_review_summary.json",
        summary,
    )
    written["reviews"] = atomic_write_csv(
        target / "terminal_distribution_reviews.csv",
        reviews,
        tuple(reviews[0]),
    )
    written["sources"] = atomic_write_csv(
        target / "terminal_distribution_sources.csv",
        source_rows,
        tuple(source_rows[0]),
    )
    written["calculations"] = atomic_write_csv(
        target / "terminal_distribution_calculations.csv",
        calculations,
        tuple(calculations[0]),
    )
    artifacts = {
        name: {
            "path": str(path),
            "sha256": _file_sha256(path),
            "byte_size": path.stat().st_size,
        }
        for name, path in written.items()
    }
    written["artifact_manifest"] = atomic_write_json(
        target / "artifact_manifest.json",
        {
            "artifact_id": "basic_materials_f0_2_terminal_distribution_evidence_v1",
            "model_family": MODEL_FAMILY,
            "sector": SECTOR,
            "policy_version": policy.version,
            "policy_sha256": policy.checksum,
            "manifest_sha256": manifest.checksum,
            "calculation_asof_date": calculation_asof_date,
            "database_counts": database_counts(conn),
            "artifacts": artifacts,
        },
    )
    return {name: str(path) for name, path in written.items()}
