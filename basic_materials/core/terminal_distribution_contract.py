"""Governed F0.2 terminal-distribution policy, manifest, and review contract."""

from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
from datetime import date
import hashlib
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from basic_materials import MODEL_FAMILY, SECTOR


class TerminalDistributionContractError(RuntimeError):
    """Raised when the terminal-distribution evidence contract is invalid."""


@dataclass(frozen=True)
class TerminalDistributionPolicy:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    review_status: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ReviewCsvEntry:
    path: Path
    source_id: str
    sha256: str
    byte_size: int
    row_count: int
    unique_key: str


@dataclass(frozen=True)
class SourceDocument:
    document_key: str
    event_key: str
    document_role: str
    url: str
    document_date: str
    cache_relative_path: str
    sha256: str
    byte_size: int
    media_type: str


@dataclass(frozen=True)
class TerminalDistributionManifest:
    path: Path
    checksum: str
    artifact_id: str
    policy_version: str
    policy_sha256: str
    as_of_date: str
    base_market_manifest_sha256: str
    review_csv: ReviewCsvEntry
    source_documents: Mapping[str, SourceDocument]


@dataclass(frozen=True)
class TerminalDistributionReview:
    event_key: str
    ticker: str
    review_status: str
    bankruptcy_distribution_value: float | None
    distribution_currency: str | None
    source_id: str
    primary_document_key: str
    primary_evidence_locator: str
    primary_evidence_text: str
    supporting_document_key: str | None
    supporting_evidence_locator: str | None
    supporting_evidence_text: str | None
    reviewed_on: str
    notes: str


CSV_FIELDS = (
    "event_key",
    "ticker",
    "review_status",
    "bankruptcy_distribution_value",
    "distribution_currency",
    "source_id",
    "primary_document_key",
    "primary_evidence_locator",
    "primary_evidence_text",
    "supporting_document_key",
    "supporting_evidence_locator",
    "supporting_evidence_text",
    "reviewed_on",
    "notes",
)

_ALLOWED_REVIEW_STATUSES = {
    "zero_distribution_verified",
    "distribution_value_verified",
    "noncash_recovery_unresolved",
}
_SHA_PATTERN = re.compile(r"[0-9a-f]{64}")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TerminalDistributionContractError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        raise TerminalDistributionContractError(
            f"Invalid keys for {context}; missing={sorted(missing)}, unexpected={sorted(extra)}"
        )


def _iso_date(value: Any, context: str) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise TerminalDistributionContractError(f"{context} must be ISO YYYY-MM-DD") from exc


def _positive_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TerminalDistributionContractError(f"{context} must be a positive integer")
    return value


def _sha256(value: Any, context: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA_PATTERN.fullmatch(text):
        raise TerminalDistributionContractError(f"{context} must be a lowercase SHA-256")
    return text


def _safe_package_path(base: Path, value: Any, package_root: Path, context: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise TerminalDistributionContractError(f"{context} must be a non-empty path")
    path = (base / text).resolve()
    try:
        path.relative_to(package_root.resolve())
    except ValueError as exc:
        raise TerminalDistributionContractError(f"{context} escapes the Basic Materials package") from exc
    return path


def load_terminal_distribution_policy(path: str | Path) -> TerminalDistributionPolicy:
    policy_path = Path(path).resolve()
    payload = policy_path.read_bytes()
    root = _mapping(yaml.safe_load(payload.decode("utf-8")), "terminal-distribution policy")
    expected = {
        "policy_version",
        "contract_as_of_date",
        "model_family",
        "sector",
        "review_status",
        "calibration_eligible",
        "base_contract",
        "review_contract",
        "evidence_contract",
        "application_contract",
        "gates",
    }
    _exact_keys(root, expected, "terminal-distribution policy")
    if root["policy_version"] != "basic_materials_terminal_distribution_policy_v1":
        raise TerminalDistributionContractError("Unsupported terminal-distribution policy version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise TerminalDistributionContractError("Terminal-distribution policy identity mismatch")
    if root["review_status"] != "approved_f0_2_terminal_distribution_review":
        raise TerminalDistributionContractError("Terminal-distribution review status is not approved")
    if root["calibration_eligible"] is not False:
        raise TerminalDistributionContractError("Terminal-distribution work cannot activate calibration")
    as_of = _iso_date(root["contract_as_of_date"], "contract_as_of_date")

    base = _mapping(root["base_contract"], "base_contract")
    _exact_keys(
        base,
        {
            "market_policy_version",
            "market_manifest_sha256",
            "terminal_return_rules_sha256",
            "expected_pending_events",
        },
        "base_contract",
    )
    if base["market_policy_version"] != "basic_materials_market_data_policy_v2":
        raise TerminalDistributionContractError("F0.2 must remain bound to Stage 3 policy v2")
    _sha256(base["market_manifest_sha256"], "base_contract.market_manifest_sha256")
    _sha256(base["terminal_return_rules_sha256"], "base_contract.terminal_return_rules_sha256")
    events = _mapping(base["expected_pending_events"], "base_contract.expected_pending_events")
    if events != {
        "terminal_ANV_20150310": "ANV",
        "terminal_MCP_20150625": "MCP",
        "terminal_GMO_20201118": "GMO",
        "terminal_BIOA_20181022": "BIOA",
    }:
        raise TerminalDistributionContractError("The F0.2 pending-event scope must remain exact")

    review = _mapping(root["review_contract"], "review_contract")
    _exact_keys(
        review,
        {
            "path",
            "source_id",
            "expected_rows",
            "unique_key",
            "required_outcome_class",
            "allowed_review_statuses",
            "expected_status_counts",
        },
        "review_contract",
    )
    if (
        review["path"] != "system_csvs/basic_materials_terminal_distribution_reviews.csv"
        or review["source_id"] != "basic_materials_terminal_distribution_review"
        or review["expected_rows"] != 4
        or review["unique_key"] != "event_key"
        or review["required_outcome_class"] != "bankruptcy_distribution"
        or set(review["allowed_review_statuses"] or ()) != _ALLOWED_REVIEW_STATUSES
        or _mapping(review["expected_status_counts"], "review_contract.expected_status_counts")
        != {"zero_distribution_verified": 4}
    ):
        raise TerminalDistributionContractError("Terminal-distribution review contract changed")

    evidence = _mapping(root["evidence_contract"], "evidence_contract")
    _exact_keys(
        evidence,
        {
            "provider",
            "allowed_hosts",
            "expected_document_keys",
            "expected_document_count",
            "cache_root_relative_path",
            "require_exact_payload_sha256",
            "require_exact_byte_size",
            "require_evidence_text_match",
            "allow_cache_only_replay",
        },
        "evidence_contract",
    )
    expected_documents = {
        "anv_confirmed_plan",
        "mcp_effective_plan",
        "bioa_monitor_update",
        "gmo_confirmation_release",
        "gmo_restructuring_support",
    }
    if (
        evidence["provider"] != "sec_edgar"
        or evidence["allowed_hosts"] != ["www.sec.gov"]
        or set(evidence["expected_document_keys"] or ()) != expected_documents
        or evidence["expected_document_count"] != 5
        or evidence["cache_root_relative_path"] != "terminal_distribution_reviews/2026-09-07"
        or any(
            evidence[key] is not True
            for key in (
                "require_exact_payload_sha256",
                "require_exact_byte_size",
                "require_evidence_text_match",
                "allow_cache_only_replay",
            )
        )
    ):
        raise TerminalDistributionContractError("Terminal-distribution evidence contract changed")

    application = _mapping(root["application_contract"], "application_contract")
    if application != {
        "preserve_stage3_base_contract": True,
        "update_only_pending_bankruptcy_rules": True,
        "verified_review_rule_status": "ready_for_calculation",
        "unresolved_review_rule_status": "pending_distribution_evidence",
        "terminal_reconciliation_required": True,
        "isolated_database_required_by_default": True,
    }:
        raise TerminalDistributionContractError("Terminal-distribution application contract changed")
    gates = _mapping(root["gates"], "gates")
    if not gates or any(value is not False for value in gates.values()):
        raise TerminalDistributionContractError("All F0.2 downstream gates must remain closed")
    return TerminalDistributionPolicy(
        path=policy_path,
        checksum=sha256_bytes(payload),
        version=str(root["policy_version"]),
        as_of_date=as_of,
        review_status=str(root["review_status"]),
        payload=root,
    )


def validate_terminal_distribution_manifest(
    path: str | Path,
    *,
    policy: TerminalDistributionPolicy,
    package_root: str | Path,
) -> TerminalDistributionManifest:
    manifest_path = Path(path).resolve()
    payload = manifest_path.read_bytes()
    root = _mapping(yaml.safe_load(payload.decode("utf-8")), "terminal-distribution manifest")
    _exact_keys(
        root,
        {
            "manifest_version",
            "artifact_id",
            "policy_version",
            "policy_sha256",
            "contract_as_of_date",
            "base_market_manifest_sha256",
            "state",
            "calibration_eligible",
            "review_csv",
            "source_documents",
        },
        "terminal-distribution manifest",
    )
    base = _mapping(policy.payload["base_contract"], "base_contract")
    if (
        root["manifest_version"] != 1
        or root["artifact_id"] != "basic_materials_terminal_distribution_review_v1"
        or root["policy_version"] != policy.version
        or _sha256(root["policy_sha256"], "manifest.policy_sha256") != policy.checksum
        or _iso_date(root["contract_as_of_date"], "manifest.contract_as_of_date")
        != policy.as_of_date
        or _sha256(
            root["base_market_manifest_sha256"],
            "manifest.base_market_manifest_sha256",
        )
        != str(base["market_manifest_sha256"])
        or root["state"] != policy.review_status
        or root["calibration_eligible"] is not False
    ):
        raise TerminalDistributionContractError("Terminal-distribution manifest header mismatch")

    package = Path(package_root).resolve()
    csv_raw = _mapping(root["review_csv"], "manifest.review_csv")
    _exact_keys(
        csv_raw,
        {"path", "source_id", "sha256", "byte_size", "row_count", "unique_key"},
        "manifest.review_csv",
    )
    csv_path = _safe_package_path(
        manifest_path.parent,
        csv_raw["path"],
        package,
        "manifest.review_csv.path",
    )
    csv_sha = _sha256(csv_raw["sha256"], "manifest.review_csv.sha256")
    csv_size = _positive_integer(csv_raw["byte_size"], "manifest.review_csv.byte_size")
    if not csv_path.is_file():
        raise TerminalDistributionContractError(f"Review CSV not found: {csv_path}")
    csv_payload = csv_path.read_bytes()
    if sha256_bytes(csv_payload) != csv_sha or len(csv_payload) != csv_size:
        raise TerminalDistributionContractError("Review CSV differs from its manifest seal")
    review_contract = _mapping(policy.payload["review_contract"], "review_contract")
    if (
        csv_raw["source_id"] != review_contract["source_id"]
        or csv_raw["row_count"] != review_contract["expected_rows"]
        or csv_raw["unique_key"] != review_contract["unique_key"]
    ):
        raise TerminalDistributionContractError("Review CSV manifest metadata differs from policy")
    csv_entry = ReviewCsvEntry(
        path=csv_path,
        source_id=str(csv_raw["source_id"]),
        sha256=csv_sha,
        byte_size=csv_size,
        row_count=int(csv_raw["row_count"]),
        unique_key=str(csv_raw["unique_key"]),
    )

    evidence = _mapping(policy.payload["evidence_contract"], "evidence_contract")
    docs_raw = _mapping(root["source_documents"], "manifest.source_documents")
    expected_doc_keys = set(str(value) for value in evidence["expected_document_keys"])
    if set(docs_raw) != expected_doc_keys or len(docs_raw) != int(evidence["expected_document_count"]):
        raise TerminalDistributionContractError("Source-document keys differ from policy")
    allowed_hosts = set(str(value) for value in evidence["allowed_hosts"])
    cache_prefix = str(evidence["cache_root_relative_path"]).rstrip("/") + "/"
    documents: dict[str, SourceDocument] = {}
    primary_counts: Counter[str] = Counter()
    expected_events = set(str(value) for value in base["expected_pending_events"])
    for document_key, value in docs_raw.items():
        document = _mapping(value, f"manifest.source_documents.{document_key}")
        _exact_keys(
            document,
            {
                "event_key",
                "document_role",
                "url",
                "document_date",
                "cache_relative_path",
                "sha256",
                "byte_size",
                "media_type",
            },
            f"manifest.source_documents.{document_key}",
        )
        event_key = str(document["event_key"])
        role = str(document["document_role"])
        parsed = urlparse(str(document["url"]))
        relative = str(document["cache_relative_path"]).replace("\\", "/")
        if (
            event_key not in expected_events
            or role not in {"primary", "supporting"}
            or parsed.scheme != "https"
            or parsed.netloc not in allowed_hosts
            or not relative.startswith(cache_prefix)
            or ".." in Path(relative).parts
            or Path(relative).is_absolute()
            or document["media_type"] != "text/html"
        ):
            raise TerminalDistributionContractError(
                f"Invalid source-document metadata for {document_key}"
            )
        document_date = _iso_date(
            document["document_date"],
            f"manifest.source_documents.{document_key}.document_date",
        )
        if document_date > policy.as_of_date:
            raise TerminalDistributionContractError(f"{document_key}: source date exceeds cutoff")
        if role == "primary":
            primary_counts[event_key] += 1
        documents[str(document_key)] = SourceDocument(
            document_key=str(document_key),
            event_key=event_key,
            document_role=role,
            url=str(document["url"]),
            document_date=document_date,
            cache_relative_path=relative,
            sha256=_sha256(document["sha256"], f"{document_key}.sha256"),
            byte_size=_positive_integer(document["byte_size"], f"{document_key}.byte_size"),
            media_type=str(document["media_type"]),
        )
    if primary_counts != Counter({event_key: 1 for event_key in expected_events}):
        raise TerminalDistributionContractError("Every reviewed event requires exactly one primary document")
    return TerminalDistributionManifest(
        path=manifest_path,
        checksum=sha256_bytes(payload),
        artifact_id=str(root["artifact_id"]),
        policy_version=str(root["policy_version"]),
        policy_sha256=str(root["policy_sha256"]),
        as_of_date=policy.as_of_date,
        base_market_manifest_sha256=str(root["base_market_manifest_sha256"]),
        review_csv=csv_entry,
        source_documents=documents,
    )


def read_terminal_distribution_reviews(
    path: str | Path,
    *,
    policy: TerminalDistributionPolicy,
    manifest: TerminalDistributionManifest,
) -> tuple[TerminalDistributionReview, ...]:
    csv_path = Path(path).resolve()
    if csv_path != manifest.review_csv.path:
        raise TerminalDistributionContractError("Configured review CSV differs from the manifest path")
    payload = csv_path.read_bytes()
    if (
        sha256_bytes(payload) != manifest.review_csv.sha256
        or len(payload) != manifest.review_csv.byte_size
    ):
        raise TerminalDistributionContractError("Review CSV changed after manifest validation")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise TerminalDistributionContractError("Review CSV columns differ from contract")
        rows = [
            {key: str(value or "").strip() for key, value in row.items()}
            for row in reader
        ]
    if len(rows) != manifest.review_csv.row_count:
        raise TerminalDistributionContractError("Review CSV row count differs from manifest")

    base = _mapping(policy.payload["base_contract"], "base_contract")
    expected_events = {
        str(event_key): str(ticker)
        for event_key, ticker in _mapping(
            base["expected_pending_events"],
            "base_contract.expected_pending_events",
        ).items()
    }
    by_event = {row["event_key"]: row for row in rows}
    if len(by_event) != len(rows) or set(by_event) != set(expected_events):
        raise TerminalDistributionContractError("Review event keys must be exact and unique")
    review_contract = _mapping(policy.payload["review_contract"], "review_contract")
    allowed = set(str(value) for value in review_contract["allowed_review_statuses"])
    status_counts = Counter(row["review_status"] for row in rows)
    expected_counts = Counter(
        {
            str(key): int(value)
            for key, value in _mapping(
                review_contract["expected_status_counts"],
                "review_contract.expected_status_counts",
            ).items()
        }
    )
    if status_counts != expected_counts:
        raise TerminalDistributionContractError("Review-status counts differ from policy")

    output: list[TerminalDistributionReview] = []
    for index, row in enumerate(rows, start=2):
        event_key = row["event_key"]
        if row["ticker"].upper() != expected_events[event_key]:
            raise TerminalDistributionContractError(f"row {index}: ticker differs from event scope")
        if row["review_status"] not in allowed:
            raise TerminalDistributionContractError(f"row {index}: invalid review_status")
        value_text = row["bankruptcy_distribution_value"]
        try:
            value = float(value_text) if value_text else None
        except ValueError as exc:
            raise TerminalDistributionContractError(f"row {index}: distribution value is invalid") from exc
        status = row["review_status"]
        currency = row["distribution_currency"].upper() or None
        if (
            (status == "zero_distribution_verified" and (value != 0 or currency != "USD"))
            or (status == "distribution_value_verified" and (value is None or value <= 0 or not currency))
            or (status == "noncash_recovery_unresolved" and (value is not None or currency is not None))
        ):
            raise TerminalDistributionContractError(f"row {index}: value/currency conflicts with status")
        if row["source_id"] != review_contract["source_id"]:
            raise TerminalDistributionContractError(f"row {index}: source_id differs from policy")
        primary = manifest.source_documents.get(row["primary_document_key"])
        if primary is None or primary.event_key != event_key or primary.document_role != "primary":
            raise TerminalDistributionContractError(f"row {index}: primary source linkage is invalid")
        if not row["primary_evidence_locator"] or not row["primary_evidence_text"]:
            raise TerminalDistributionContractError(f"row {index}: primary evidence is incomplete")
        support_values = (
            row["supporting_document_key"],
            row["supporting_evidence_locator"],
            row["supporting_evidence_text"],
        )
        if any(support_values) and not all(support_values):
            raise TerminalDistributionContractError(f"row {index}: supporting evidence is partial")
        support_key = row["supporting_document_key"] or None
        if support_key:
            supporting = manifest.source_documents.get(support_key)
            if (
                supporting is None
                or supporting.event_key != event_key
                or supporting.document_role != "supporting"
            ):
                raise TerminalDistributionContractError(f"row {index}: supporting source linkage is invalid")
        if _iso_date(row["reviewed_on"], f"row {index}.reviewed_on") != policy.as_of_date:
            raise TerminalDistributionContractError(f"row {index}: reviewed_on differs from cutoff")
        if not row["notes"]:
            raise TerminalDistributionContractError(f"row {index}: notes are required")
        output.append(
            TerminalDistributionReview(
                event_key=event_key,
                ticker=row["ticker"].upper(),
                review_status=status,
                bankruptcy_distribution_value=value,
                distribution_currency=currency,
                source_id=row["source_id"],
                primary_document_key=row["primary_document_key"],
                primary_evidence_locator=row["primary_evidence_locator"],
                primary_evidence_text=row["primary_evidence_text"],
                supporting_document_key=support_key,
                supporting_evidence_locator=row["supporting_evidence_locator"] or None,
                supporting_evidence_text=row["supporting_evidence_text"] or None,
                reviewed_on=row["reviewed_on"],
                notes=row["notes"],
            )
        )
    return tuple(sorted(output, key=lambda item: item.event_key))
