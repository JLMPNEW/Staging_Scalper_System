"""Basic Materials semantic compiler adapter for the real-document pilot.

The repository-level ``dedicated_parser`` package is a sector-neutral runtime.
This module is the only Basic Materials boundary that calls it.  It compiles
immutable source bytes into generic semantic blocks and creates review-only
table-family candidates; it never emits accepted observations.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from dedicated_parser.contracts import DOCUMENT_PARSER_RELEASE
from dedicated_parser.semantic import normalize_space, parse_semantic_document


ADAPTER_VERSION = "basic_materials_real_document_pilot_adapter_v1"
STRUCTURED_JSON_DECODER_VERSION = "sec_companyfacts_flatten_v1"

_CHARSET_PATTERN = re.compile(br"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE)
_NUMERIC_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[$\u20ac\u00a3]\s*)?\(?-?\d+(?:,\d{3})*(?:\.\d+)?\)?%?"
)
_UNIT_TOKEN_PATTERN = re.compile(
    r"\b(?:%|percent|basis points?|bps|tons?|tonnes?|metric tons?|short tons?|"
    r"ounces?|oz\.?|gold equivalent ounces?|geo|pounds?|lbs?\.?|kilograms?|kg|"
    r"cubic yards?|square meters?|barrels?|days?|years?|megawatts?|mwh|gj|"
    r"usd|cad|aud|eur|gbp|zar|brl|krw|cny|rmb)\b",
    re.IGNORECASE,
)
_PERIOD_TOKEN_PATTERN = re.compile(
    r"\b(?:19|20)\d{2}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?\b|"
    r"\b(?:three|six|nine|twelve) months? ended\b|\byear ended\b",
    re.IGNORECASE,
)


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CompiledBlock:
    block_key: str
    index: int
    kind: str
    text: str
    section_path: tuple[str, ...]
    table_id: int | None
    row_index: int | None
    cells: tuple[str, ...]
    header_cells: tuple[str, ...]
    preamble_text: str

    @property
    def search_text(self) -> str:
        parts = (
            *self.section_path,
            self.preamble_text,
            *self.header_cells,
            self.text,
        )
        unique: list[str] = []
        for part in parts:
            normalized = normalize_space(part)
            if normalized and normalized not in unique:
                unique.append(normalized)
        return " | ".join(unique)

    def as_dict(self) -> dict[str, Any]:
        return {
            "block_key": self.block_key,
            "index": self.index,
            "kind": self.kind,
            "text": self.text,
            "section_path": list(self.section_path),
            "table_id": self.table_id,
            "row_index": self.row_index,
            "cells": list(self.cells),
            "header_cells": list(self.header_cells),
            "preamble_text": self.preamble_text,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> CompiledBlock:
        return cls(
            block_key=str(row["block_key"]),
            index=int(row["index"]),
            kind=str(row["kind"]),
            text=str(row["text"]),
            section_path=tuple(str(item) for item in row.get("section_path", [])),
            table_id=None if row.get("table_id") is None else int(row["table_id"]),
            row_index=None if row.get("row_index") is None else int(row["row_index"]),
            cells=tuple(str(item) for item in row.get("cells", [])),
            header_cells=tuple(str(item) for item in row.get("header_cells", [])),
            preamble_text=str(row.get("preamble_text", "")),
        )


@dataclass(frozen=True)
class CompiledSemanticDocument:
    source_document: str
    media_type: str
    compiler: str
    parser_release: str
    warning: str
    blocks: tuple[CompiledBlock, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_document": self.source_document,
            "media_type": self.media_type,
            "compiler": self.compiler,
            "parser_release": self.parser_release,
            "warning": self.warning,
            "blocks": [block.as_dict() for block in self.blocks],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CompiledSemanticDocument:
        raw_blocks = payload.get("blocks")
        if not isinstance(raw_blocks, list):
            raise ValueError("semantic cache blocks must be a list")
        return cls(
            source_document=str(payload["source_document"]),
            media_type=str(payload["media_type"]),
            compiler=str(payload["compiler"]),
            parser_release=str(payload["parser_release"]),
            warning=str(payload.get("warning", "")),
            blocks=tuple(CompiledBlock.from_dict(row) for row in raw_blocks),
        )


@dataclass(frozen=True)
class CandidateSearchResult:
    """Capped review rows plus uncapped evidence-funnel diagnostics."""

    candidates: tuple[dict[str, Any], ...]
    lexical_hit_count: int
    numeric_eligible_hit_count: int
    review_candidate_hit_count: int
    prohibited_context_hit_count: int


def _block(
    *,
    source_document: str,
    index: int,
    kind: str,
    text: str,
    section_path: Sequence[str] = (),
    table_id: int | None = None,
    row_index: int | None = None,
    cells: Sequence[str] = (),
    header_cells: Sequence[str] = (),
    preamble_text: str = "",
) -> CompiledBlock:
    normalized_text = normalize_space(text)
    normalized_section = tuple(normalize_space(item) for item in section_path if normalize_space(item))
    normalized_cells = tuple(normalize_space(item) for item in cells)
    normalized_headers = tuple(normalize_space(item) for item in header_cells)
    normalized_preamble = normalize_space(preamble_text)
    identity = {
        "source_document": source_document,
        "index": index,
        "kind": kind,
        "text": normalized_text,
        "section_path": normalized_section,
        "table_id": table_id,
        "row_index": row_index,
        "cells": normalized_cells,
        "header_cells": normalized_headers,
        "preamble_text": normalized_preamble,
    }
    return CompiledBlock(
        block_key=_stable_hash(identity),
        index=index,
        kind=kind,
        text=normalized_text,
        section_path=normalized_section,
        table_id=table_id,
        row_index=row_index,
        cells=normalized_cells,
        header_cells=normalized_headers,
        preamble_text=normalized_preamble,
    )


def _decode_markup(payload: bytes) -> tuple[str, str]:
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", errors="replace"), "utf-16"
    declared = _CHARSET_PATTERN.search(payload[:8192])
    encodings = []
    if declared:
        encodings.append(declared.group(1).decode("ascii", errors="ignore").casefold())
    encodings.extend(("utf-8", "windows-1252"))
    seen: set[str] = set()
    for encoding in encodings:
        if not encoding or encoding in seen:
            continue
        seen.add(encoding)
        try:
            return payload.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace"), "utf-8-replace"


def _compile_markup(payload: bytes, *, source_document: str, media_type: str) -> CompiledSemanticDocument:
    text, encoding = _decode_markup(payload)
    semantic = parse_semantic_document(text, source_document=source_document)
    blocks = tuple(
        _block(
            source_document=source_document,
            index=item.index,
            kind=item.kind,
            text=item.text,
            section_path=item.section_path,
            table_id=item.table_id,
            row_index=item.row_index,
            cells=item.cells,
            header_cells=item.header_cells,
            preamble_text=item.preamble_text,
        )
        for item in semantic.blocks
    )
    warning_parts = [part for part in (semantic.warning, f"encoding={encoding}") if part]
    return CompiledSemanticDocument(
        source_document=source_document,
        media_type=media_type,
        compiler="dedicated_parser.semantic.parse_semantic_document",
        parser_release=DOCUMENT_PARSER_RELEASE,
        warning=";".join(warning_parts),
        blocks=blocks,
    )


def _observation_sort_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row.get(field, ""))
        for field in ("filed", "end", "start", "form", "accn", "fy", "fp", "frame", "val")
    )


def _compile_companyfacts(payload: bytes, *, source_document: str) -> CompiledSemanticDocument:
    root = json.loads(payload.decode("utf-8"))
    if not isinstance(root, Mapping) or not isinstance(root.get("facts"), Mapping):
        raise ValueError("SEC companyfacts payload must contain a facts mapping")
    entity_name = normalize_space(str(root.get("entityName", "")))
    blocks: list[CompiledBlock] = []
    headers = ("concept", "label", "value", "unit", "start", "end", "filed", "form", "frame", "accession")
    for taxonomy in sorted(root["facts"]):
        concepts = root["facts"][taxonomy]
        if not isinstance(concepts, Mapping):
            continue
        for concept in sorted(concepts):
            detail = concepts[concept]
            if not isinstance(detail, Mapping):
                continue
            label = normalize_space(str(detail.get("label", "")))
            description = normalize_space(str(detail.get("description", "")))
            units = detail.get("units")
            if not isinstance(units, Mapping):
                continue
            for unit in sorted(units):
                observations = units[unit]
                if not isinstance(observations, list):
                    continue
                valid_rows = [item for item in observations if isinstance(item, Mapping)]
                for observation in sorted(valid_rows, key=_observation_sort_key):
                    value = str(observation.get("val", ""))
                    cells = (
                        concept,
                        label,
                        value,
                        str(unit),
                        str(observation.get("start", "")),
                        str(observation.get("end", "")),
                        str(observation.get("filed", "")),
                        str(observation.get("form", "")),
                        str(observation.get("frame", "")),
                        str(observation.get("accn", "")),
                    )
                    text = " | ".join(item for item in cells if item)
                    blocks.append(
                        _block(
                            source_document=source_document,
                            index=len(blocks),
                            kind="structured_fact",
                            text=text,
                            section_path=(entity_name, str(taxonomy), concept),
                            row_index=len(blocks),
                            cells=cells,
                            header_cells=headers,
                            preamble_text=description,
                        )
                    )
    return CompiledSemanticDocument(
        source_document=source_document,
        media_type="application/json",
        compiler=STRUCTURED_JSON_DECODER_VERSION,
        parser_release=DOCUMENT_PARSER_RELEASE,
        warning="",
        blocks=tuple(blocks),
    )


def compile_source_document(
    payload: bytes,
    *,
    source_document: str,
    media_type: str,
) -> CompiledSemanticDocument:
    """Compile one immutable source payload without applying metric policy."""

    if media_type in {"text/html", "application/xhtml+xml", "application/xml", "text/xml"}:
        return _compile_markup(payload, source_document=source_document, media_type=media_type)
    if media_type == "application/json":
        return _compile_companyfacts(payload, source_document=source_document)
    raise ValueError(f"Unsupported pilot media type: {media_type}")


def semantic_blocks_sha256(document: CompiledSemanticDocument) -> str:
    return _stable_hash([block.as_dict() for block in document.blocks])


def find_table_family_candidates(
    document: CompiledSemanticDocument,
    *,
    match_terms: Sequence[str],
    prohibited_terms: Sequence[str],
    preferred_block_kinds: Sequence[str],
    minimum_numeric_tokens: int,
    limit: int,
) -> CandidateSearchResult:
    """Return high-recall review candidates; nothing here is accepted evidence."""

    normalized_terms = tuple(normalize_space(item).casefold() for item in match_terms)
    normalized_prohibited = tuple(normalize_space(item).casefold() for item in prohibited_terms)
    preferred = set(preferred_block_kinds)
    ranked: list[dict[str, Any]] = []
    lexical_hit_count = 0
    numeric_eligible_hit_count = 0
    review_candidate_hit_count = 0
    prohibited_context_hit_count = 0
    for block in document.blocks:
        search_text = block.search_text
        folded = search_text.casefold()
        matched = tuple(term for term in normalized_terms if term and term in folded)
        if not matched:
            continue
        lexical_hit_count += 1
        numeric_tokens = tuple(_NUMERIC_TOKEN_PATTERN.findall(search_text))
        if len(numeric_tokens) < minimum_numeric_tokens:
            continue
        numeric_eligible_hit_count += 1
        prohibited = tuple(term for term in normalized_prohibited if term and term in folded)
        if prohibited:
            prohibited_context_hit_count += 1
        else:
            review_candidate_hit_count += 1
        unit_tokens = tuple(dict.fromkeys(match.group(0) for match in _UNIT_TOKEN_PATTERN.finditer(search_text)))
        period_tokens = tuple(dict.fromkeys(match.group(0) for match in _PERIOD_TOKEN_PATTERN.finditer(search_text)))
        preferred_kind = block.kind in preferred
        score = len(matched) * 10 + min(len(numeric_tokens), 10) + (5 if preferred_kind else 0)
        if prohibited:
            score -= 50
        ranked.append(
            {
                "block_key": block.block_key,
                "block_index": block.index,
                "block_kind": block.kind,
                "table_id": "" if block.table_id is None else block.table_id,
                "row_index": "" if block.row_index is None else block.row_index,
                "matched_terms": matched,
                "prohibited_terms": prohibited,
                "numeric_tokens": numeric_tokens[:24],
                "unit_tokens": unit_tokens[:16],
                "period_tokens": period_tokens[:16],
                "candidate_status": "prohibited_context" if prohibited else "review_required",
                "candidate_score": score,
                "evidence_text": search_text[:4000],
                "evidence_sha256": hashlib.sha256(search_text.encode("utf-8")).hexdigest(),
            }
        )
    ranked.sort(
        key=lambda row: (
            row["candidate_status"] != "review_required",
            -int(row["candidate_score"]),
            int(row["block_index"]),
            str(row["block_key"]),
        )
    )
    return CandidateSearchResult(
        candidates=tuple(ranked[:limit]),
        lexical_hit_count=lexical_hit_count,
        numeric_eligible_hit_count=numeric_eligible_hit_count,
        review_candidate_hit_count=review_candidate_hit_count,
        prohibited_context_hit_count=prohibited_context_hit_count,
    )
