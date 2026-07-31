from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from dedicated_parser.contracts import DocumentRef
from industrials.machinery.disclosure_documents import (
    DocumentText,
    extract_document_text,
    extract_pdf_text_pymupdf,
)


CONTENT_TEXT_CACHE_VERSION = "transportation_content_text_cache_v1"


@dataclass(frozen=True)
class ExtractionOptions:
    enable_pdf_ocr: bool
    max_pdf_pages: int
    max_pdf_bytes: int
    pdf_extraction_timeout_seconds: float

    @property
    def content_sha256(self) -> str:
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def cache_root_from_document(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    for parent in resolved.parents:
        if parent.name == "non_sec_primary_documents":
            return parent
    raise ValueError(f"Direct transportation document is outside its cache root: {resolved}")


def cache_path(cache_root: Path, content_sha256: str) -> Path:
    return cache_root / "extracted_text_sha256" / content_sha256[:2] / f"{content_sha256}.json.gz"


def legacy_word_docx_path(
    cache_root: Path,
    content_sha256: str,
) -> Path:
    return cache_root / "legacy_word_docx_sha256" / content_sha256[:2] / f"{content_sha256}.docx"


def _payload_to_document_text(payload: Mapping[str, Any]) -> DocumentText:
    return DocumentText(
        text=str(payload.get("text") or ""),
        extraction_method=str(payload.get("extraction_method") or ""),
        page_count=int(payload.get("page_count") or 0),
        ocr_used=bool(payload.get("ocr_used")),
        warning=str(payload.get("warning") or ""),
    )


def load_cached_text(
    *,
    cache_root: Path,
    content_sha256: str,
    options: ExtractionOptions,
) -> DocumentText | None:
    path = cache_path(cache_root, content_sha256)
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("cache_version") != CONTENT_TEXT_CACHE_VERSION
        or payload.get("content_sha256") != content_sha256
        or payload.get("extraction_options_sha256") != options.content_sha256
    ):
        return None
    return _payload_to_document_text(payload)


def _write_cached_text(
    *,
    cache_root: Path,
    content_sha256: str,
    document_name: str,
    options: ExtractionOptions,
    extracted: DocumentText,
) -> Path:
    path = cache_path(cache_root, content_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the atomic sibling name short. The production OneDrive root plus a
    # 64-character content key is already close to legacy Windows MAX_PATH;
    # repeating the target filename in the temporary name crosses that limit.
    # The suffix must NOT match *.json.gz: a hard-killed process would leave
    # the temp file inflating every cache-inventory count and hash.
    temporary = path.with_name(f".tmp.{uuid.uuid4().hex}.part")
    payload = {
        "cache_version": CONTENT_TEXT_CACHE_VERSION,
        "content_sha256": content_sha256,
        "document_name": document_name,
        "extraction_options": asdict(options),
        "extraction_options_sha256": options.content_sha256,
        "text": extracted.text,
        "extraction_method": extracted.extraction_method,
        "page_count": extracted.page_count,
        "ocr_used": extracted.ocr_used,
        "warning": extracted.warning,
    }
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _extract(
    document: DocumentRef,
    *,
    cache_root: Path,
    content_type: str,
    options: ExtractionOptions,
) -> DocumentText:
    source_path = Path(document.path)
    payload = source_path.read_bytes()
    if document.name.lower().endswith(".doc") and payload.startswith(b"\xd0\xcf\x11\xe0"):
        converted = legacy_word_docx_path(
            cache_root,
            document.content_sha256,
        )
        if converted.is_file() and converted.stat().st_size > 0:
            converted_text = extract_document_text(
                converted.read_bytes(),
                document_name=converted.name,
            )
            return DocumentText(
                converted_text.text,
                "legacy_word_wordconv_docx_xml",
                warning=converted_text.warning,
            )
    return extract_document_text(
        payload,
        document_name=document.name,
        content_type=content_type,
        enable_pdf_ocr=options.enable_pdf_ocr,
        max_pdf_pages=options.max_pdf_pages,
        max_pdf_bytes=options.max_pdf_bytes,
        pdf_extraction_timeout_sec=(options.pdf_extraction_timeout_seconds),
    )


def extract_document_once(
    document: DocumentRef,
    *,
    content_type: str = "",
    options: ExtractionOptions,
    lock_timeout_seconds: float = 900.0,
) -> tuple[DocumentText, str]:
    cache_root = cache_root_from_document(Path(document.path))
    cached = load_cached_text(
        cache_root=cache_root,
        content_sha256=document.content_sha256,
        options=options,
    )
    if cached is not None:
        return cached, "CACHE_HIT"
    path = cache_path(cache_root, document.content_sha256)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(1.0, lock_timeout_seconds)
    acquired = False
    while not acquired:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.close(descriptor)
            acquired = True
        except FileExistsError:
            cached = load_cached_text(
                cache_root=cache_root,
                content_sha256=document.content_sha256,
                options=options,
            )
            if cached is not None:
                return cached, "CACHE_WAIT_HIT"
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for content cache lock: {lock_path}")
            try:
                if time.time() - lock_path.stat().st_mtime > (lock_timeout_seconds):
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.2)
    try:
        cached = load_cached_text(
            cache_root=cache_root,
            content_sha256=document.content_sha256,
            options=options,
        )
        if cached is not None:
            return cached, "CACHE_POST_LOCK_HIT"
        extracted = _extract(
            document,
            cache_root=cache_root,
            content_type=content_type,
            options=options,
        )
        _write_cached_text(
            cache_root=cache_root,
            content_sha256=document.content_sha256,
            document_name=document.name,
            options=options,
            extracted=extracted,
        )
        return extracted, "EXTRACTED_AND_CACHED"
    finally:
        lock_path.unlink(missing_ok=True)


def repair_pdf_cache_if_better(
    document: DocumentRef,
    *,
    options: ExtractionOptions,
) -> tuple[DocumentText, str]:
    """Try one bounded local fallback without discarding usable cached text."""
    cache_root = cache_root_from_document(Path(document.path))
    existing = load_cached_text(
        cache_root=cache_root,
        content_sha256=document.content_sha256,
        options=options,
    )
    if existing is None:
        raise FileNotFoundError(f"Cannot repair a missing content-text cache: {document.content_sha256}")
    if not document.name.lower().endswith(".pdf"):
        return existing, "CACHE_NOT_PDF"
    candidate = extract_pdf_text_pymupdf(
        Path(document.path).read_bytes(),
        max_pages=options.max_pdf_pages,
        extraction_timeout_sec=options.pdf_extraction_timeout_seconds,
    )
    existing_length = len(existing.text.strip())
    candidate_length = len(candidate.text.strip())
    validated_empty = candidate_length == 0 and existing_length == 0 and not candidate.warning
    improved = candidate_length > existing_length or (
        candidate_length == existing_length
        and candidate_length > 0
        and bool(existing.warning)
        and not candidate.warning
    )
    if not (improved or validated_empty):
        return existing, "CACHE_LIMITATION_RETAINED"
    _write_cached_text(
        cache_root=cache_root,
        content_sha256=document.content_sha256,
        document_name=document.name,
        options=options,
        extracted=candidate,
    )
    return candidate, ("CACHE_VALIDATED_EMPTY_PYMUPDF" if validated_empty else "CACHE_IMPROVED_PYMUPDF")
