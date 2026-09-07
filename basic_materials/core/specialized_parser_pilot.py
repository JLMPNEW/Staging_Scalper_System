"""F1A.1 real-document compiler pilot and golden-fixture workbench.

This module deliberately has no database argument.  It reads hash-sealed raw
evidence, writes an immutable content-addressed semantic cache, and emits only
review candidates and QA artifacts.  Candidate rows cannot become accepted
observations, score inputs, PIT rows, or calibration inputs.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.adapters.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    STRUCTURED_JSON_DECODER_VERSION,
    CompiledBlock,
    CompiledSemanticDocument,
    compile_source_document,
    find_table_family_candidates,
    semantic_blocks_sha256,
)
from basic_materials.core.atomic_io import atomic_write_bytes, atomic_write_csv, atomic_write_json
from basic_materials.core.specialized_contract import SpecializedMetricRegistry
from basic_materials.core.specialized_parser_contract import SpecializedParserPolicy
from dedicated_parser.contracts import DOCUMENT_PARSER_RELEASE


class SpecializedParserPilotError(ValueError):
    """Raised when the real-document pilot contract is malformed or unsafe."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TableFamilyRule:
    table_family: str
    match_terms: tuple[str, ...]
    prohibited_terms: tuple[str, ...]
    preferred_block_kinds: tuple[str, ...]
    minimum_numeric_tokens: int


@dataclass(frozen=True)
class SpecializedParserPilotPolicy:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    parser_policy_version: str
    decoder_version: str
    decoder_contract_sha256: str
    required_current_cohorts: tuple[str, ...]
    minimum_current_documents_per_cohort: int
    minimum_historical_documents: int
    required_media_types: tuple[str, ...]
    maximum_candidates_per_document_table_family: int
    derived_only_table_families: tuple[str, ...]
    minimum_reviewed_positive_per_physical_table_family: int
    minimum_reviewed_hard_negative_per_adapter: int
    production_target_positive_per_table_family: int
    production_target_hard_negative_per_table_family: int
    production_target_historical_per_table_family: int
    table_family_rules: tuple[TableFamilyRule, ...]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class PilotDocument:
    document_id: str
    ticker: str
    cohort_id: str
    history_role: str
    source_family: str
    source_id: str
    accession_number: str
    form_type: str
    document_name: str
    document_role: str
    period_end: str
    availability_timestamp: str
    source_url: str
    cache_relative_path: str
    media_type: str
    byte_size: int
    content_sha256: str


@dataclass(frozen=True)
class PilotDocumentManifest:
    path: Path
    checksum: str
    documents: tuple[PilotDocument, ...]


@dataclass(frozen=True)
class GoldenExpectation:
    expectation_id: str
    case_type: str
    document_id: str
    table_family: str
    block_key: str
    required_text: tuple[str, ...]
    expected_numeric_text: str
    expected_unit_text: str
    expected_period_text: str
    review_status: str
    reviewed_by: str
    reviewed_at_utc: str
    review_note: str


@dataclass(frozen=True)
class GoldenExpectationBundle:
    path: Path
    checksum: str
    expectations: tuple[GoldenExpectation, ...]


@dataclass(frozen=True)
class RealDocumentPilotReport:
    manifest_valid: bool
    document_gate_passed: bool
    compiler_gate_passed: bool
    golden_gate_passed: bool
    pilot_gate_passed: bool
    ready_for_golden_review: bool
    production_execution_allowed: bool
    database_mutated: bool
    policy_version: str
    policy_sha256: str
    decoder_contract_sha256: str
    document_manifest_sha256: str
    golden_expectations_sha256: str
    cache_only: bool
    counts: Mapping[str, Any]
    issues: tuple[Mapping[str, Any], ...]
    document_rows: tuple[Mapping[str, Any], ...]
    candidate_rows: tuple[Mapping[str, Any], ...]
    family_rows: tuple[Mapping[str, Any], ...]
    golden_rows: tuple[Mapping[str, Any], ...]

    def summary_dict(self) -> dict[str, Any]:
        return {
            "manifest_valid": self.manifest_valid,
            "document_gate_passed": self.document_gate_passed,
            "compiler_gate_passed": self.compiler_gate_passed,
            "golden_gate_passed": self.golden_gate_passed,
            "pilot_gate_passed": self.pilot_gate_passed,
            "ready_for_golden_review": self.ready_for_golden_review,
            "production_execution_allowed": self.production_execution_allowed,
            "database_mutated": self.database_mutated,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "decoder_contract_sha256": self.decoder_contract_sha256,
            "document_manifest_sha256": self.document_manifest_sha256,
            "golden_expectations_sha256": self.golden_expectations_sha256,
            "cache_only": self.cache_only,
            "counts": dict(self.counts),
            "issue_count": len(self.issues),
            "issues": [dict(row) for row in self.issues],
        }


DOCUMENT_FIELDS = (
    "document_id",
    "ticker",
    "cohort_id",
    "history_role",
    "source_family",
    "source_id",
    "accession_number",
    "form_type",
    "document_name",
    "document_role",
    "period_end",
    "availability_timestamp",
    "source_url",
    "cache_relative_path",
    "media_type",
    "byte_size",
    "content_sha256",
)

GOLDEN_FIELDS = (
    "expectation_id",
    "case_type",
    "document_id",
    "table_family",
    "block_key",
    "required_text_json",
    "expected_numeric_text",
    "expected_unit_text",
    "expected_period_text",
    "review_status",
    "reviewed_by",
    "reviewed_at_utc",
    "review_note",
)

DOCUMENT_REPORT_FIELDS = (
    *DOCUMENT_FIELDS,
    "raw_accessed",
    "raw_hash_verified",
    "semantic_cache_status",
    "semantic_cache_path",
    "semantic_sha256",
    "semantic_block_count",
    "table_row_count",
    "structured_fact_count",
    "candidate_count",
    "compiler",
    "compiler_warning",
)

CANDIDATE_FIELDS = (
    "candidate_id",
    "document_id",
    "ticker",
    "cohort_id",
    "history_role",
    "source_family",
    "source_id",
    "accession_number",
    "form_type",
    "period_end",
    "availability_timestamp",
    "source_url",
    "content_sha256",
    "semantic_sha256",
    "decoder_contract_sha256",
    "adapter_id",
    "table_family",
    "metric_ids_json",
    "block_key",
    "block_index",
    "block_kind",
    "table_id",
    "row_index",
    "matched_terms_json",
    "prohibited_terms_json",
    "numeric_tokens_json",
    "unit_tokens_json",
    "period_tokens_json",
    "candidate_status",
    "candidate_score",
    "evidence_text",
    "evidence_sha256",
    "accepted_observation_write_allowed",
)

FAMILY_FIELDS = (
    "adapter_id",
    "table_family",
    "family_role",
    "applicable_metric_count",
    "eligible_document_count",
    "lexical_hit_count",
    "numeric_eligible_hit_count",
    "uncapped_review_candidate_count",
    "uncapped_prohibited_context_count",
    "review_candidate_count",
    "prohibited_context_count",
    "approved_positive_count",
    "approved_hard_negative_count",
    "approved_historical_positive_count",
    "pilot_positive_gate_passed",
    "production_positive_target",
    "production_hard_negative_target",
    "production_historical_target",
    "coverage_state",
)

GOLDEN_RESULT_FIELDS = (
    "expectation_id",
    "case_type",
    "document_id",
    "table_family",
    "block_key",
    "review_status",
    "block_found",
    "candidate_found",
    "required_text_matched",
    "numeric_text_matched",
    "unit_text_matched",
    "period_text_matched",
    "passed",
    "failure_reason",
)

ISSUE_FIELDS = ("severity", "issue_code", "scope", "message")


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecializedParserPilotError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise SpecializedParserPilotError(
            f"{context} keys differ; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _strings(value: Any, context: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SpecializedParserPilotError(f"{context} must be a list")
    result = tuple(str(item).strip() for item in value)
    if (not allow_empty and not result) or any(not item for item in result):
        raise SpecializedParserPilotError(f"{context} contains blank or missing values")
    if len(result) != len(set(result)):
        raise SpecializedParserPilotError(f"{context} must contain unique values")
    return result


def _iso_date(value: Any, context: str, *, allow_empty: bool = False) -> str:
    raw = str(value).strip()
    if allow_empty and not raw:
        return ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise SpecializedParserPilotError(f"{context} must be an ISO date") from exc


def _iso_timestamp(value: Any, context: str, *, allow_empty: bool = False) -> str:
    raw = str(value).strip()
    if allow_empty and not raw:
        return ""
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SpecializedParserPilotError(f"{context} must be an ISO timestamp") from exc
    return raw


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_specialized_parser_pilot_policy(path: str | Path) -> SpecializedParserPilotPolicy:
    policy_path = Path(path).resolve()
    payload_bytes = policy_path.read_bytes()
    root = _mapping(yaml.safe_load(payload_bytes.decode("utf-8")), "pilot policy")
    _exact_keys(
        root,
        {
            "pilot_policy_version",
            "contract_as_of_date",
            "model_family",
            "sector",
            "parser_policy_version",
            "state",
            "decoder_contract",
            "pilot_controls",
            "golden_controls",
            "required_flags",
            "table_family_rules",
        },
        "pilot policy",
    )
    if root["pilot_policy_version"] != "basic_materials_specialized_parser_real_document_pilot_v1":
        raise SpecializedParserPilotError("Unsupported pilot policy version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise SpecializedParserPilotError("Pilot policy identity is invalid")
    if root["state"] != "f1a1_real_document_measurement_only":
        raise SpecializedParserPilotError("Pilot policy must remain measurement-only")

    decoder = _mapping(root["decoder_contract"], "decoder_contract")
    _exact_keys(
        decoder,
        {
            "decoder_version",
            "shared_parser_release",
            "html_xml_compiler",
            "structured_json_compiler",
            "content_address_algorithm",
            "immutable_cache",
            "compile_unique_content_once",
            "cache_only_replay",
        },
        "decoder_contract",
    )
    if (
        decoder["shared_parser_release"] != DOCUMENT_PARSER_RELEASE
        or decoder["html_xml_compiler"] != "dedicated_parser.semantic.parse_semantic_document"
        or decoder["structured_json_compiler"] != STRUCTURED_JSON_DECODER_VERSION
        or decoder["content_address_algorithm"] != "sha256"
        or decoder["immutable_cache"] is not True
        or decoder["compile_unique_content_once"] is not True
        or decoder["cache_only_replay"] is not True
    ):
        raise SpecializedParserPilotError("Decoder contract does not match the installed runtime")

    controls = _mapping(root["pilot_controls"], "pilot_controls")
    _exact_keys(
        controls,
        {
            "required_current_cohorts",
            "minimum_current_documents_per_cohort",
            "minimum_historical_documents",
            "required_media_types",
            "maximum_candidates_per_document_table_family",
            "verify_manifest_sha256_and_size",
            "require_https_source_url",
            "raw_download_allowed",
            "raw_cache_read_allowed",
            "semantic_compile_allowed",
            "cache_only_replay_opens_raw_documents",
        },
        "pilot_controls",
    )
    if (
        controls["verify_manifest_sha256_and_size"] is not True
        or controls["require_https_source_url"] is not True
        or controls["raw_download_allowed"] is not False
        or controls["raw_cache_read_allowed"] is not True
        or controls["semantic_compile_allowed"] is not True
        or controls["cache_only_replay_opens_raw_documents"] is not False
    ):
        raise SpecializedParserPilotError("Pilot source/compile controls are unsafe")
    positive_int_fields = (
        "minimum_current_documents_per_cohort",
        "minimum_historical_documents",
        "maximum_candidates_per_document_table_family",
    )
    if any(int(controls[field]) < 1 for field in positive_int_fields):
        raise SpecializedParserPilotError("Pilot minimums and candidate limit must be positive")

    golden = _mapping(root["golden_controls"], "golden_controls")
    _exact_keys(
        golden,
        {
            "derived_only_table_families",
            "minimum_reviewed_positive_per_physical_table_family",
            "minimum_reviewed_hard_negative_per_adapter",
            "production_target_positive_per_table_family",
            "production_target_hard_negative_per_table_family",
            "production_target_historical_per_table_family",
            "every_approved_expectation_must_pass",
            "empty_or_draft_expectations_pass",
        },
        "golden_controls",
    )
    if (
        golden["every_approved_expectation_must_pass"] is not True
        or golden["empty_or_draft_expectations_pass"] is not False
    ):
        raise SpecializedParserPilotError("Golden expectation controls are unsafe")
    golden_int_fields = (
        "minimum_reviewed_positive_per_physical_table_family",
        "minimum_reviewed_hard_negative_per_adapter",
        "production_target_positive_per_table_family",
        "production_target_hard_negative_per_table_family",
        "production_target_historical_per_table_family",
    )
    if any(int(golden[field]) < 1 for field in golden_int_fields):
        raise SpecializedParserPilotError("Golden corpus targets must be positive")

    flags = _mapping(root["required_flags"], "required_flags")
    if not flags or any(value is not False for value in flags.values()):
        raise SpecializedParserPilotError("Every production/mutation flag must remain false")

    raw_rules = root["table_family_rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise SpecializedParserPilotError("table_family_rules must be a non-empty list")
    rules: list[TableFamilyRule] = []
    seen_families: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        rule = _mapping(raw_rule, f"table_family_rules[{index}]")
        _exact_keys(
            rule,
            {
                "table_family",
                "match_terms",
                "prohibited_terms",
                "preferred_block_kinds",
                "minimum_numeric_tokens",
            },
            f"table_family_rules[{index}]",
        )
        family = str(rule["table_family"]).strip()
        if not family or family in seen_families:
            raise SpecializedParserPilotError(f"Invalid or duplicate table family: {family!r}")
        minimum_tokens = int(rule["minimum_numeric_tokens"])
        if minimum_tokens < 0:
            raise SpecializedParserPilotError(f"{family} minimum_numeric_tokens cannot be negative")
        seen_families.add(family)
        rules.append(
            TableFamilyRule(
                table_family=family,
                match_terms=_strings(rule["match_terms"], f"{family}.match_terms"),
                prohibited_terms=_strings(
                    rule["prohibited_terms"],
                    f"{family}.prohibited_terms",
                    allow_empty=True,
                ),
                preferred_block_kinds=_strings(
                    rule["preferred_block_kinds"], f"{family}.preferred_block_kinds"
                ),
                minimum_numeric_tokens=minimum_tokens,
            )
        )

    decoder_payload = {
        "adapter_version": ADAPTER_VERSION,
        **dict(decoder),
    }
    return SpecializedParserPilotPolicy(
        path=policy_path,
        checksum=hashlib.sha256(payload_bytes).hexdigest(),
        version=str(root["pilot_policy_version"]),
        as_of_date=_iso_date(root["contract_as_of_date"], "contract_as_of_date"),
        parser_policy_version=str(root["parser_policy_version"]),
        decoder_version=str(decoder["decoder_version"]),
        decoder_contract_sha256=_stable_hash(decoder_payload),
        required_current_cohorts=_strings(
            controls["required_current_cohorts"], "required_current_cohorts"
        ),
        minimum_current_documents_per_cohort=int(controls["minimum_current_documents_per_cohort"]),
        minimum_historical_documents=int(controls["minimum_historical_documents"]),
        required_media_types=_strings(controls["required_media_types"], "required_media_types"),
        maximum_candidates_per_document_table_family=int(
            controls["maximum_candidates_per_document_table_family"]
        ),
        derived_only_table_families=_strings(
            golden["derived_only_table_families"], "derived_only_table_families"
        ),
        minimum_reviewed_positive_per_physical_table_family=int(
            golden["minimum_reviewed_positive_per_physical_table_family"]
        ),
        minimum_reviewed_hard_negative_per_adapter=int(
            golden["minimum_reviewed_hard_negative_per_adapter"]
        ),
        production_target_positive_per_table_family=int(
            golden["production_target_positive_per_table_family"]
        ),
        production_target_hard_negative_per_table_family=int(
            golden["production_target_hard_negative_per_table_family"]
        ),
        production_target_historical_per_table_family=int(
            golden["production_target_historical_per_table_family"]
        ),
        table_family_rules=tuple(rules),
        payload=dict(root),
    )


def _read_csv(path: Path, expected_fields: tuple[str, ...], context: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != expected_fields:
            raise SpecializedParserPilotError(
                f"{context} columns differ; expected={list(expected_fields)}, "
                f"actual={list(reader.fieldnames or ())}"
            )
        return [dict(row) for row in reader]


def load_pilot_document_manifest(path: str | Path) -> PilotDocumentManifest:
    manifest_path = Path(path).resolve()
    rows = _read_csv(manifest_path, DOCUMENT_FIELDS, "pilot document manifest")
    if not rows:
        raise SpecializedParserPilotError("Pilot document manifest cannot be empty")
    documents: list[PilotDocument] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=2):
        document_id = row["document_id"].strip()
        if not document_id or document_id in seen_ids:
            raise SpecializedParserPilotError(f"Invalid or duplicate document_id at row {index}")
        required = [field for field in DOCUMENT_FIELDS if field not in {"period_end"}]
        if any(not row[field].strip() for field in required):
            raise SpecializedParserPilotError(f"Pilot document row {index} contains blank required fields")
        ticker = row["ticker"].strip()
        if ticker != ticker.upper():
            raise SpecializedParserPilotError(f"Pilot ticker must be uppercase at row {index}")
        history_role = row["history_role"].strip()
        if history_role not in {"current", "historical"}:
            raise SpecializedParserPilotError(f"Invalid history_role at row {index}")
        content_sha256 = row["content_sha256"].strip().lower()
        if not _SHA256.fullmatch(content_sha256):
            raise SpecializedParserPilotError(f"Invalid content_sha256 at row {index}")
        byte_size = int(row["byte_size"])
        if byte_size <= 0:
            raise SpecializedParserPilotError(f"byte_size must be positive at row {index}")
        source_url = row["source_url"].strip()
        if urlsplit(source_url).scheme.casefold() != "https":
            raise SpecializedParserPilotError(f"source_url must use HTTPS at row {index}")
        relative = Path(row["cache_relative_path"].strip())
        if relative.is_absolute() or ".." in relative.parts:
            raise SpecializedParserPilotError(f"Unsafe cache_relative_path at row {index}")
        seen_ids.add(document_id)
        documents.append(
            PilotDocument(
                document_id=document_id,
                ticker=ticker,
                cohort_id=row["cohort_id"].strip(),
                history_role=history_role,
                source_family=row["source_family"].strip(),
                source_id=row["source_id"].strip(),
                accession_number=row["accession_number"].strip(),
                form_type=row["form_type"].strip(),
                document_name=row["document_name"].strip(),
                document_role=row["document_role"].strip(),
                period_end=_iso_date(row["period_end"], f"row {index}.period_end", allow_empty=True),
                availability_timestamp=_iso_timestamp(
                    row["availability_timestamp"], f"row {index}.availability_timestamp"
                ),
                source_url=source_url,
                cache_relative_path=relative.as_posix(),
                media_type=row["media_type"].strip(),
                byte_size=byte_size,
                content_sha256=content_sha256,
            )
        )
    return PilotDocumentManifest(
        path=manifest_path,
        checksum=_file_hash(manifest_path),
        documents=tuple(documents),
    )


def load_golden_expectations(path: str | Path) -> GoldenExpectationBundle:
    expectation_path = Path(path).resolve()
    rows = _read_csv(expectation_path, GOLDEN_FIELDS, "golden expectations")
    expectations: list[GoldenExpectation] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=2):
        expectation_id = row["expectation_id"].strip()
        if not expectation_id or expectation_id in seen_ids:
            raise SpecializedParserPilotError(f"Invalid or duplicate expectation_id at row {index}")
        case_type = row["case_type"].strip()
        if case_type not in {"positive", "hard_negative"}:
            raise SpecializedParserPilotError(f"Invalid golden case_type at row {index}")
        block_key = row["block_key"].strip().lower()
        if not _SHA256.fullmatch(block_key):
            raise SpecializedParserPilotError(f"Invalid golden block_key at row {index}")
        try:
            required_text_raw = json.loads(row["required_text_json"])
        except json.JSONDecodeError as exc:
            raise SpecializedParserPilotError(
                f"required_text_json is invalid at row {index}"
            ) from exc
        required_text = _strings(required_text_raw, f"row {index}.required_text")
        review_status = row["review_status"].strip()
        if review_status not in {"draft", "approved"}:
            raise SpecializedParserPilotError(f"Invalid review_status at row {index}")
        reviewed_by = row["reviewed_by"].strip()
        reviewed_at = _iso_timestamp(
            row["reviewed_at_utc"],
            f"row {index}.reviewed_at_utc",
            allow_empty=review_status == "draft",
        )
        if review_status == "approved" and (not reviewed_by or not reviewed_at):
            raise SpecializedParserPilotError(f"Approved expectation lacks reviewer at row {index}")
        if case_type == "positive" and any(
            not row[field].strip()
            for field in ("expected_numeric_text", "expected_unit_text", "expected_period_text")
        ):
            raise SpecializedParserPilotError(
                f"Positive expectation lacks numeric/unit/period evidence at row {index}"
            )
        seen_ids.add(expectation_id)
        expectations.append(
            GoldenExpectation(
                expectation_id=expectation_id,
                case_type=case_type,
                document_id=row["document_id"].strip(),
                table_family=row["table_family"].strip(),
                block_key=block_key,
                required_text=required_text,
                expected_numeric_text=row["expected_numeric_text"].strip(),
                expected_unit_text=row["expected_unit_text"].strip(),
                expected_period_text=row["expected_period_text"].strip(),
                review_status=review_status,
                reviewed_by=reviewed_by,
                reviewed_at_utc=reviewed_at,
                review_note=row["review_note"].strip(),
            )
        )
    return GoldenExpectationBundle(
        path=expectation_path,
        checksum=_file_hash(expectation_path),
        expectations=tuple(expectations),
    )


def _semantic_cache_path(
    cache_root: Path,
    *,
    policy: SpecializedParserPilotPolicy,
    content_sha256: str,
) -> Path:
    root = (
        cache_root
        / "specialized_parser_pilot"
        / "semantic"
        # Keep the owned cache below the legacy Windows MAX_PATH boundary. The
        # complete decoder-contract digest remains embedded in and validated
        # against every cache payload, so this directory prefix is routing only.
        / policy.decoder_contract_sha256[:24]
    ).resolve(strict=False)
    target = (root / content_sha256[:2] / f"{content_sha256}.json.gz").resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SpecializedParserPilotError("Semantic cache path escaped owned cache root") from exc
    return target


def _semantic_cache_payload(
    *,
    policy: SpecializedParserPilotPolicy,
    content_sha256: str,
    document: CompiledSemanticDocument,
) -> dict[str, Any]:
    return {
        "cache_format_version": "basic_materials_semantic_cache_v1",
        "adapter_version": ADAPTER_VERSION,
        "decoder_version": policy.decoder_version,
        "decoder_contract_sha256": policy.decoder_contract_sha256,
        "content_sha256": content_sha256,
        "semantic_sha256": semantic_blocks_sha256(document),
        "document": document.as_dict(),
    }


def _load_semantic_cache(
    path: Path,
    *,
    policy: SpecializedParserPilotPolicy,
    content_sha256: str,
) -> tuple[CompiledSemanticDocument, str]:
    try:
        payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecializedParserPilotError(f"Invalid semantic cache {path}: {exc}") from exc
    root = _mapping(payload, f"semantic cache {path}")
    if (
        root.get("cache_format_version") != "basic_materials_semantic_cache_v1"
        or root.get("adapter_version") != ADAPTER_VERSION
        or root.get("decoder_version") != policy.decoder_version
        or root.get("decoder_contract_sha256") != policy.decoder_contract_sha256
        or root.get("content_sha256") != content_sha256
    ):
        raise SpecializedParserPilotError(f"Semantic cache contract mismatch: {path}")
    document = CompiledSemanticDocument.from_dict(
        _mapping(root.get("document"), f"semantic cache document {path}")
    )
    semantic_sha256 = semantic_blocks_sha256(document)
    if root.get("semantic_sha256") != semantic_sha256:
        raise SpecializedParserPilotError(f"Semantic cache payload hash mismatch: {path}")
    return document, semantic_sha256


def _write_semantic_cache(
    path: Path,
    *,
    policy: SpecializedParserPilotPolicy,
    content_sha256: str,
    document: CompiledSemanticDocument,
) -> str:
    payload = _semantic_cache_payload(
        policy=policy,
        content_sha256=content_sha256,
        document=document,
    )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    atomic_write_bytes(path, gzip.compress(encoded, compresslevel=9, mtime=0))
    return str(payload["semantic_sha256"])


def _document_path(cache_root: Path, relative_path: str) -> Path:
    root = cache_root.resolve(strict=False)
    target = (root / Path(relative_path)).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SpecializedParserPilotError("Pilot document escaped Basic Materials cache root") from exc
    return target


def _warning_is_error(warning: str) -> bool:
    return any(part and not part.startswith("encoding=") for part in warning.split(";"))


def _golden_result(
    expectation: GoldenExpectation,
    *,
    blocks: Mapping[str, CompiledBlock],
    candidate_lookup: set[tuple[str, str, str]],
) -> dict[str, Any]:
    block = blocks.get(expectation.block_key)
    evidence = block.search_text if block else ""
    folded = evidence.casefold()
    block_found = block is not None
    candidate_found = (
        expectation.document_id,
        expectation.table_family,
        expectation.block_key,
    ) in candidate_lookup
    required_text_matched = block_found and all(item.casefold() in folded for item in expectation.required_text)
    numeric_matched = not expectation.expected_numeric_text or expectation.expected_numeric_text.casefold() in folded
    unit_matched = not expectation.expected_unit_text or expectation.expected_unit_text.casefold() in folded
    period_matched = not expectation.expected_period_text or expectation.expected_period_text.casefold() in folded
    expected_candidate = expectation.case_type == "positive"
    passed = (
        expectation.review_status == "approved"
        and block_found
        and required_text_matched
        and numeric_matched
        and unit_matched
        and period_matched
        and candidate_found == expected_candidate
    )
    failures: list[str] = []
    if expectation.review_status != "approved":
        failures.append("EXPECTATION_NOT_APPROVED")
    if not block_found:
        failures.append("BLOCK_NOT_FOUND")
    if block_found and not required_text_matched:
        failures.append("REQUIRED_TEXT_MISMATCH")
    if block_found and not numeric_matched:
        failures.append("NUMERIC_TEXT_MISMATCH")
    if block_found and not unit_matched:
        failures.append("UNIT_TEXT_MISMATCH")
    if block_found and not period_matched:
        failures.append("PERIOD_TEXT_MISMATCH")
    if candidate_found != expected_candidate:
        failures.append("CANDIDATE_PRESENCE_MISMATCH")
    return {
        "expectation_id": expectation.expectation_id,
        "case_type": expectation.case_type,
        "document_id": expectation.document_id,
        "table_family": expectation.table_family,
        "block_key": expectation.block_key,
        "review_status": expectation.review_status,
        "block_found": int(block_found),
        "candidate_found": int(candidate_found),
        "required_text_matched": int(required_text_matched),
        "numeric_text_matched": int(numeric_matched),
        "unit_text_matched": int(unit_matched),
        "period_text_matched": int(period_matched),
        "passed": int(passed),
        "failure_reason": "|".join(failures),
    }


def run_real_document_parser_pilot(
    *,
    policy: SpecializedParserPilotPolicy,
    documents: PilotDocumentManifest,
    golden: GoldenExpectationBundle,
    registry: SpecializedMetricRegistry,
    parser_policy: SpecializedParserPolicy,
    cache_root: str | Path,
    registered_source_ids: set[str],
    cache_only: bool = False,
) -> RealDocumentPilotReport:
    """Compile or replay the exact pilot corpus without touching SQLite."""

    issues: list[dict[str, Any]] = []

    def add_issue(severity: str, code: str, scope: str, message: str) -> None:
        issues.append(
            {"severity": severity, "issue_code": code, "scope": scope, "message": message}
        )

    if policy.parser_policy_version != parser_policy.version:
        add_issue(
            "error",
            "PARSER_POLICY_VERSION_MISMATCH",
            "contract",
            "Real-document pilot does not target the active synthetic parser contract",
        )

    registry_cohorts = {metric.cohort_id for metric in registry.metrics}
    if set(policy.required_current_cohorts) != registry_cohorts:
        add_issue(
            "error",
            "REQUIRED_COHORT_SET_MISMATCH",
            "contract",
            "Pilot current-cohort set does not exactly match the metric registry",
        )

    expected_families = {
        family for metric in registry.metrics for family in metric.table_families
    }
    derived_families = set(policy.derived_only_table_families)
    physical_families = expected_families - derived_families
    rule_by_family = {rule.table_family: rule for rule in policy.table_family_rules}
    if set(rule_by_family) != physical_families:
        add_issue(
            "error",
            "PILOT_TABLE_FAMILY_RULE_COVERAGE_INVALID",
            "contract",
            f"missing={sorted(physical_families - set(rule_by_family))}; "
            f"extra={sorted(set(rule_by_family) - physical_families)}",
        )

    adapter_by_family = {
        family: adapter.adapter_id
        for adapter in parser_policy.adapters
        for family in adapter.table_families
    }
    metric_ids_by_cohort_family: dict[tuple[str, str], list[str]] = defaultdict(list)
    for metric in registry.metrics:
        for family in metric.table_families:
            if family in physical_families:
                metric_ids_by_cohort_family[(metric.cohort_id, family)].append(metric.metric_id)

    document_ids = {document.document_id for document in documents.documents}
    if len(document_ids) != len(documents.documents):
        add_issue("error", "DUPLICATE_DOCUMENT_ID", "manifest", "Document IDs are not unique")
    for expectation in golden.expectations:
        if expectation.document_id not in document_ids:
            add_issue(
                "error",
                "GOLDEN_DOCUMENT_NOT_IN_MANIFEST",
                expectation.expectation_id,
                expectation.document_id,
            )
        if expectation.table_family not in physical_families:
            add_issue(
                "error",
                "GOLDEN_TABLE_FAMILY_INVALID",
                expectation.expectation_id,
                expectation.table_family,
            )

    source_ids_missing = sorted(
        {document.source_id for document in documents.documents} - registered_source_ids
    )
    if source_ids_missing:
        add_issue(
            "error",
            "UNREGISTERED_SOURCE_ID",
            "manifest",
            ",".join(source_ids_missing),
        )

    cache = Path(cache_root).resolve(strict=False)
    compiled_by_hash: dict[str, tuple[CompiledSemanticDocument, str, Path, str]] = {}
    compiled_by_document: dict[str, CompiledSemanticDocument] = {}
    semantic_sha_by_document: dict[str, str] = {}
    document_rows: list[dict[str, Any]] = []
    successful_documents: set[str] = set()
    content_compile_counts: Counter[str] = Counter()

    for document in documents.documents:
        if document.cohort_id not in registry_cohorts:
            add_issue(
                "error",
                "DOCUMENT_COHORT_INVALID",
                document.document_id,
                document.cohort_id,
            )
        source_path = _document_path(cache, document.cache_relative_path)
        semantic_path = _semantic_cache_path(
            cache,
            policy=policy,
            content_sha256=document.content_sha256,
        )
        raw_accessed = 0
        raw_hash_verified = 0
        cache_status = "missing"
        semantic_sha256 = ""
        compiled: CompiledSemanticDocument | None = None

        try:
            if document.content_sha256 in compiled_by_hash:
                compiled, semantic_sha256, semantic_path, cache_status = compiled_by_hash[
                    document.content_sha256
                ]
                cache_status = "deduplicated_context"
            elif semantic_path.is_file():
                compiled, semantic_sha256 = _load_semantic_cache(
                    semantic_path,
                    policy=policy,
                    content_sha256=document.content_sha256,
                )
                cache_status = "cache_hit"
                compiled_by_hash[document.content_sha256] = (
                    compiled,
                    semantic_sha256,
                    semantic_path,
                    cache_status,
                )
            elif cache_only:
                raise SpecializedParserPilotError("semantic cache is absent in cache-only mode")
            else:
                if not source_path.is_file():
                    raise SpecializedParserPilotError("manifest source file is missing")
                raw_accessed = 1
                actual_size = source_path.stat().st_size
                if actual_size != document.byte_size:
                    raise SpecializedParserPilotError(
                        f"byte-size mismatch: expected {document.byte_size}, found {actual_size}"
                    )
                payload = source_path.read_bytes()
                actual_hash = hashlib.sha256(payload).hexdigest()
                if actual_hash != document.content_sha256:
                    raise SpecializedParserPilotError(
                        f"content hash mismatch: expected {document.content_sha256}, found {actual_hash}"
                    )
                raw_hash_verified = 1
                canonical_source = f"sha256:{document.content_sha256}"
                compiled = compile_source_document(
                    payload,
                    source_document=canonical_source,
                    media_type=document.media_type,
                )
                if not compiled.blocks:
                    raise SpecializedParserPilotError("semantic compiler emitted zero blocks")
                semantic_sha256 = _write_semantic_cache(
                    semantic_path,
                    policy=policy,
                    content_sha256=document.content_sha256,
                    document=compiled,
                )
                content_compile_counts[document.content_sha256] += 1
                cache_status = "compiled"
                compiled_by_hash[document.content_sha256] = (
                    compiled,
                    semantic_sha256,
                    semantic_path,
                    cache_status,
                )
            if compiled is None:
                raise SpecializedParserPilotError("semantic compiler returned no document")
            if compiled.media_type != document.media_type:
                raise SpecializedParserPilotError("semantic cache media type differs from manifest")
            if _warning_is_error(compiled.warning):
                raise SpecializedParserPilotError(f"semantic compiler warning: {compiled.warning}")
            compiled_by_document[document.document_id] = compiled
            semantic_sha_by_document[document.document_id] = semantic_sha256
            successful_documents.add(document.document_id)
        except (OSError, ValueError, SpecializedParserPilotError) as exc:
            add_issue(
                "error",
                "DOCUMENT_COMPILE_FAILED",
                document.document_id,
                f"{type(exc).__name__}: {exc}",
            )

        block_count = len(compiled.blocks) if compiled else 0
        document_rows.append(
            {
                **{
                    field: (
                        document.byte_size
                        if field == "byte_size"
                        else getattr(document, field)
                    )
                    for field in DOCUMENT_FIELDS
                },
                "raw_accessed": raw_accessed,
                "raw_hash_verified": raw_hash_verified,
                "semantic_cache_status": cache_status,
                "semantic_cache_path": str(semantic_path),
                "semantic_sha256": semantic_sha256,
                "semantic_block_count": block_count,
                "table_row_count": (
                    sum(block.kind == "table_row" for block in compiled.blocks) if compiled else 0
                ),
                "structured_fact_count": (
                    sum(block.kind == "structured_fact" for block in compiled.blocks) if compiled else 0
                ),
                "candidate_count": 0,
                "compiler": compiled.compiler if compiled else "",
                "compiler_warning": compiled.warning if compiled else "",
            }
        )

    current_counts = Counter(
        document.cohort_id
        for document in documents.documents
        if document.history_role == "current" and document.document_id in successful_documents
    )
    for cohort in policy.required_current_cohorts:
        if current_counts[cohort] < policy.minimum_current_documents_per_cohort:
            add_issue(
                "error",
                "CURRENT_COHORT_DOCUMENT_GAP",
                cohort,
                f"required={policy.minimum_current_documents_per_cohort}; found={current_counts[cohort]}",
            )
    historical_count = sum(
        document.history_role == "historical" and document.document_id in successful_documents
        for document in documents.documents
    )
    if historical_count < policy.minimum_historical_documents:
        add_issue(
            "error",
            "HISTORICAL_DOCUMENT_GAP",
            "manifest",
            f"required={policy.minimum_historical_documents}; found={historical_count}",
        )
    successful_media_types = {
        document.media_type
        for document in documents.documents
        if document.document_id in successful_documents
    }
    missing_media = sorted(set(policy.required_media_types) - successful_media_types)
    if missing_media:
        add_issue(
            "error",
            "REQUIRED_MEDIA_TYPE_GAP",
            "manifest",
            ",".join(missing_media),
        )
    duplicate_physical_compiles = {
        content_hash: count for content_hash, count in content_compile_counts.items() if count > 1
    }
    if duplicate_physical_compiles:
        add_issue(
            "error",
            "CONTENT_COMPILED_MORE_THAN_ONCE",
            "semantic_cache",
            json.dumps(duplicate_physical_compiles, sort_keys=True),
        )

    candidate_rows: list[dict[str, Any]] = []
    candidates_per_document: Counter[str] = Counter()
    lexical_family_counts: Counter[str] = Counter()
    numeric_eligible_family_counts: Counter[str] = Counter()
    uncapped_review_family_counts: Counter[str] = Counter()
    uncapped_prohibited_family_counts: Counter[str] = Counter()
    for document in documents.documents:
        compiled = compiled_by_document.get(document.document_id)
        if compiled is None:
            continue
        for family in sorted(physical_families):
            metric_ids = sorted(metric_ids_by_cohort_family.get((document.cohort_id, family), []))
            if not metric_ids:
                continue
            rule = rule_by_family.get(family)
            if rule is None:
                continue
            search = find_table_family_candidates(
                compiled,
                match_terms=rule.match_terms,
                prohibited_terms=rule.prohibited_terms,
                preferred_block_kinds=rule.preferred_block_kinds,
                minimum_numeric_tokens=rule.minimum_numeric_tokens,
                limit=policy.maximum_candidates_per_document_table_family,
            )
            lexical_family_counts[family] += search.lexical_hit_count
            numeric_eligible_family_counts[family] += search.numeric_eligible_hit_count
            uncapped_review_family_counts[family] += search.review_candidate_hit_count
            uncapped_prohibited_family_counts[family] += search.prohibited_context_hit_count
            for candidate in search.candidates:
                identity = {
                    "document_id": document.document_id,
                    "table_family": family,
                    "block_key": candidate["block_key"],
                    "decoder_contract_sha256": policy.decoder_contract_sha256,
                }
                candidate_rows.append(
                    {
                        "candidate_id": f"cand_{_stable_hash(identity)[:24]}",
                        "document_id": document.document_id,
                        "ticker": document.ticker,
                        "cohort_id": document.cohort_id,
                        "history_role": document.history_role,
                        "source_family": document.source_family,
                        "source_id": document.source_id,
                        "accession_number": document.accession_number,
                        "form_type": document.form_type,
                        "period_end": document.period_end,
                        "availability_timestamp": document.availability_timestamp,
                        "source_url": document.source_url,
                        "content_sha256": document.content_sha256,
                        "semantic_sha256": semantic_sha_by_document[document.document_id],
                        "decoder_contract_sha256": policy.decoder_contract_sha256,
                        "adapter_id": adapter_by_family.get(family, ""),
                        "table_family": family,
                        "metric_ids_json": json.dumps(metric_ids, separators=(",", ":")),
                        "block_key": candidate["block_key"],
                        "block_index": candidate["block_index"],
                        "block_kind": candidate["block_kind"],
                        "table_id": candidate["table_id"],
                        "row_index": candidate["row_index"],
                        "matched_terms_json": json.dumps(
                            candidate["matched_terms"], separators=(",", ":")
                        ),
                        "prohibited_terms_json": json.dumps(
                            candidate["prohibited_terms"], separators=(",", ":")
                        ),
                        "numeric_tokens_json": json.dumps(
                            candidate["numeric_tokens"], separators=(",", ":")
                        ),
                        "unit_tokens_json": json.dumps(
                            candidate["unit_tokens"], separators=(",", ":")
                        ),
                        "period_tokens_json": json.dumps(
                            candidate["period_tokens"], separators=(",", ":")
                        ),
                        "candidate_status": candidate["candidate_status"],
                        "candidate_score": candidate["candidate_score"],
                        "evidence_text": candidate["evidence_text"],
                        "evidence_sha256": candidate["evidence_sha256"],
                        "accepted_observation_write_allowed": 0,
                    }
                )
                candidates_per_document[document.document_id] += 1

    candidate_rows.sort(
        key=lambda row: (
            row["adapter_id"],
            row["table_family"],
            row["ticker"],
            -int(row["candidate_score"]),
            row["candidate_id"],
        )
    )
    for row in document_rows:
        row["candidate_count"] = candidates_per_document[str(row["document_id"])]

    blocks_by_document = {
        document_id: {block.block_key: block for block in compiled.blocks}
        for document_id, compiled in compiled_by_document.items()
    }
    accepted_candidate_lookup = {
        (str(row["document_id"]), str(row["table_family"]), str(row["block_key"]))
        for row in candidate_rows
        if row["candidate_status"] == "review_required"
    }
    golden_rows = tuple(
        _golden_result(
            expectation,
            blocks=blocks_by_document.get(expectation.document_id, {}),
            candidate_lookup=accepted_candidate_lookup,
        )
        for expectation in golden.expectations
    )
    failed_approved = [
        row for row in golden_rows if row["review_status"] == "approved" and not row["passed"]
    ]
    for row in failed_approved:
        add_issue(
            "error",
            "APPROVED_GOLDEN_EXPECTATION_FAILED",
            str(row["expectation_id"]),
            str(row["failure_reason"]),
        )

    approved = [
        expectation for expectation in golden.expectations if expectation.review_status == "approved"
    ]
    positive_counts = Counter(
        expectation.table_family for expectation in approved if expectation.case_type == "positive"
    )
    hard_negative_counts = Counter(
        expectation.table_family for expectation in approved if expectation.case_type == "hard_negative"
    )
    historical_document_ids = {
        document.document_id for document in documents.documents if document.history_role == "historical"
    }
    historical_positive_counts = Counter(
        expectation.table_family
        for expectation in approved
        if expectation.case_type == "positive" and expectation.document_id in historical_document_ids
    )
    adapter_negative_counts: Counter[str] = Counter()
    for family, count in hard_negative_counts.items():
        adapter_negative_counts[adapter_by_family.get(family, "")] += count

    candidate_family_counts = Counter(
        str(row["table_family"])
        for row in candidate_rows
        if row["candidate_status"] == "review_required"
    )
    prohibited_family_counts = Counter(
        str(row["table_family"])
        for row in candidate_rows
        if row["candidate_status"] == "prohibited_context"
    )
    eligible_document_counts: Counter[str] = Counter()
    for document in documents.documents:
        if document.document_id not in successful_documents:
            continue
        for family in physical_families:
            if metric_ids_by_cohort_family.get((document.cohort_id, family)):
                eligible_document_counts[family] += 1

    family_rows: list[dict[str, Any]] = []
    for family in sorted(expected_families):
        is_derived = family in derived_families
        positive_passed = is_derived or (
            positive_counts[family]
            >= policy.minimum_reviewed_positive_per_physical_table_family
        )
        if is_derived:
            coverage_state = "derived_only_operand_replay"
        elif positive_passed:
            coverage_state = "pilot_golden_positive_ready"
        elif candidate_family_counts[family]:
            coverage_state = "candidates_found_review_required"
        elif not lexical_family_counts[family]:
            coverage_state = "source_term_absent"
        elif not numeric_eligible_family_counts[family]:
            coverage_state = "numeric_evidence_absent"
        elif uncapped_prohibited_family_counts[family]:
            coverage_state = "prohibited_context_only"
        else:
            coverage_state = "no_candidate_found"
        family_rows.append(
            {
                "adapter_id": adapter_by_family.get(family, ""),
                "table_family": family,
                "family_role": "derived_only" if is_derived else "physical",
                "applicable_metric_count": sum(
                    family in metric.table_families for metric in registry.metrics
                ),
                "eligible_document_count": eligible_document_counts[family],
                "lexical_hit_count": lexical_family_counts[family],
                "numeric_eligible_hit_count": numeric_eligible_family_counts[family],
                "uncapped_review_candidate_count": uncapped_review_family_counts[family],
                "uncapped_prohibited_context_count": uncapped_prohibited_family_counts[family],
                "review_candidate_count": candidate_family_counts[family],
                "prohibited_context_count": prohibited_family_counts[family],
                "approved_positive_count": positive_counts[family],
                "approved_hard_negative_count": hard_negative_counts[family],
                "approved_historical_positive_count": historical_positive_counts[family],
                "pilot_positive_gate_passed": int(positive_passed),
                "production_positive_target": policy.production_target_positive_per_table_family,
                "production_hard_negative_target": policy.production_target_hard_negative_per_table_family,
                "production_historical_target": policy.production_target_historical_per_table_family,
                "coverage_state": coverage_state,
            }
        )
        if not is_derived and not candidate_family_counts[family]:
            add_issue(
                "blocker",
                "REAL_DOCUMENT_CANDIDATE_GAP",
                family,
                f"coverage_state={coverage_state}; no review candidate was found in the "
                "current pilot corpus",
            )
        if not is_derived and not positive_passed:
            add_issue(
                "blocker",
                "REVIEWED_POSITIVE_GOLDEN_GAP",
                family,
                f"required={policy.minimum_reviewed_positive_per_physical_table_family}; "
                f"approved={positive_counts[family]}",
            )

    for adapter in parser_policy.adapters:
        if adapter.adapter_id == "derived_metrics_v1":
            continue
        if adapter_negative_counts[adapter.adapter_id] < policy.minimum_reviewed_hard_negative_per_adapter:
            add_issue(
                "blocker",
                "REVIEWED_HARD_NEGATIVE_GAP",
                adapter.adapter_id,
                f"required={policy.minimum_reviewed_hard_negative_per_adapter}; "
                f"approved={adapter_negative_counts[adapter.adapter_id]}",
            )

    manifest_valid = not any(row["severity"] == "error" for row in issues)
    document_gate_passed = (
        len(successful_documents) == len(documents.documents)
        and all(
            current_counts[cohort] >= policy.minimum_current_documents_per_cohort
            for cohort in policy.required_current_cohorts
        )
        and historical_count >= policy.minimum_historical_documents
        and not missing_media
    )
    compiler_gate_passed = (
        document_gate_passed
        and not duplicate_physical_compiles
        and all(compiled_by_document[document_id].blocks for document_id in successful_documents)
    )
    golden_gate_passed = (
        compiler_gate_passed
        and not failed_approved
        and all(
            positive_counts[family]
            >= policy.minimum_reviewed_positive_per_physical_table_family
            for family in physical_families
        )
        and all(
            adapter.adapter_id == "derived_metrics_v1"
            or adapter_negative_counts[adapter.adapter_id]
            >= policy.minimum_reviewed_hard_negative_per_adapter
            for adapter in parser_policy.adapters
        )
    )
    pilot_gate_passed = manifest_valid and document_gate_passed and compiler_gate_passed and golden_gate_passed
    ready_for_golden_review = manifest_valid and document_gate_passed and compiler_gate_passed
    counts = {
        "manifest_documents": len(documents.documents),
        "successful_documents": len(successful_documents),
        "current_cohorts_required": len(policy.required_current_cohorts),
        "current_cohorts_covered": sum(
            current_counts[cohort] >= policy.minimum_current_documents_per_cohort
            for cohort in policy.required_current_cohorts
        ),
        "historical_documents": historical_count,
        "required_media_types": len(policy.required_media_types),
        "covered_media_types": len(set(policy.required_media_types) & successful_media_types),
        "unique_content_hashes": len({document.content_sha256 for document in documents.documents}),
        "physical_compiles_this_run": sum(content_compile_counts.values()),
        "semantic_cache_hits_or_deduplicated": sum(
            row["semantic_cache_status"] in {"cache_hit", "deduplicated_context"}
            for row in document_rows
        ),
        "semantic_blocks": sum(int(row["semantic_block_count"]) for row in document_rows),
        "table_rows": sum(int(row["table_row_count"]) for row in document_rows),
        "structured_facts": sum(int(row["structured_fact_count"]) for row in document_rows),
        "physical_table_families": len(physical_families),
        "derived_only_table_families": len(derived_families),
        "families_with_review_candidates": sum(candidate_family_counts[family] > 0 for family in physical_families),
        "families_with_lexical_hits": sum(
            lexical_family_counts[family] > 0 for family in physical_families
        ),
        "families_with_numeric_eligible_hits": sum(
            numeric_eligible_family_counts[family] > 0 for family in physical_families
        ),
        "source_term_absent_families": sum(
            lexical_family_counts[family] == 0 for family in physical_families
        ),
        "numeric_evidence_absent_families": sum(
            lexical_family_counts[family] > 0
            and numeric_eligible_family_counts[family] == 0
            for family in physical_families
        ),
        "review_candidates": len(candidate_rows),
        "approved_golden_expectations": len(approved),
        "approved_positive_expectations": sum(item.case_type == "positive" for item in approved),
        "approved_hard_negative_expectations": sum(
            item.case_type == "hard_negative" for item in approved
        ),
        "approved_expectation_failures": len(failed_approved),
        "accepted_observations_written": 0,
        "database_rows_written": 0,
        "scoring_rows_written": 0,
        "pit_rows_written": 0,
    }
    return RealDocumentPilotReport(
        manifest_valid=manifest_valid,
        document_gate_passed=document_gate_passed,
        compiler_gate_passed=compiler_gate_passed,
        golden_gate_passed=golden_gate_passed,
        pilot_gate_passed=pilot_gate_passed,
        ready_for_golden_review=ready_for_golden_review,
        production_execution_allowed=False,
        database_mutated=False,
        policy_version=policy.version,
        policy_sha256=policy.checksum,
        decoder_contract_sha256=policy.decoder_contract_sha256,
        document_manifest_sha256=documents.checksum,
        golden_expectations_sha256=golden.checksum,
        cache_only=cache_only,
        counts=counts,
        issues=tuple(issues),
        document_rows=tuple(document_rows),
        candidate_rows=tuple(candidate_rows),
        family_rows=tuple(family_rows),
        golden_rows=golden_rows,
    )


def write_real_document_parser_pilot_reports(
    report: RealDocumentPilotReport,
    *,
    report_dir: str | Path,
) -> dict[str, str]:
    output = Path(report_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": output / "real_document_parser_pilot_summary.json",
        "documents": output / "real_document_parser_pilot_documents.csv",
        "candidates": output / "real_document_parser_candidate_review.csv",
        "family_coverage": output / "real_document_parser_family_coverage.csv",
        "golden_results": output / "real_document_parser_golden_results.csv",
        "issues": output / "real_document_parser_pilot_issues.csv",
        "artifact_manifest": output / "artifact_manifest.json",
    }
    atomic_write_json(paths["summary"], report.summary_dict())
    atomic_write_csv(paths["documents"], report.document_rows, DOCUMENT_REPORT_FIELDS)
    atomic_write_csv(paths["candidates"], report.candidate_rows, CANDIDATE_FIELDS)
    atomic_write_csv(paths["family_coverage"], report.family_rows, FAMILY_FIELDS)
    atomic_write_csv(paths["golden_results"], report.golden_rows, GOLDEN_RESULT_FIELDS)
    atomic_write_csv(paths["issues"], report.issues, ISSUE_FIELDS)
    manifest = {
        "artifact_id": "basic_materials_real_document_parser_pilot",
        "policy_version": report.policy_version,
        "policy_sha256": report.policy_sha256,
        "decoder_contract_sha256": report.decoder_contract_sha256,
        "document_manifest_sha256": report.document_manifest_sha256,
        "golden_expectations_sha256": report.golden_expectations_sha256,
        "pilot_gate_passed": report.pilot_gate_passed,
        "production_execution_allowed": report.production_execution_allowed,
        "database_mutated": report.database_mutated,
        "artifacts": [
            {
                "name": name,
                "path": str(path),
                "sha256": _file_hash(path),
                "byte_size": path.stat().st_size,
            }
            for name, path in paths.items()
            if name != "artifact_manifest"
        ],
    }
    atomic_write_json(paths["artifact_manifest"], manifest)
    return {name: str(path) for name, path in paths.items()}
