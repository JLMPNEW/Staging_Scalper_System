"""Fail-closed path handling for locally cached SEC filing documents.

SEC document names arrive from remote JSON/XML metadata and therefore must be
treated as untrusted basenames.  Keep the policy here so the ingestion and
parser layers cannot drift into subtly different path-validation rules.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable
from urllib.parse import quote

from dedicated_parser.path_io import absolute_path, is_file, resolve_path


SEC_DOCUMENT_SUFFIXES = frozenset(
    {".htm", ".html", ".xhtml", ".xml", ".txt", ".pdf"}
)
SEC_SUBMISSIONS_ARCHIVE_SUFFIXES = frozenset({".json"})
SEC_PRIMARY_DOCUMENT_SUFFIXES = SEC_DOCUMENT_SUFFIXES | frozenset({".paper"})
SEC_ARCHIVE_ENTRY_SUFFIXES = SEC_DOCUMENT_SUFFIXES | frozenset(
    {
        ".xsd", ".css", ".js", ".json", ".jpg", ".jpeg", ".gif", ".png",
        ".svg", ".zip", ".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx",
    }
)

_WINDOWS_DEVICE_STEM = re.compile(
    r"(?:con|prn|aux|nul|com(?:[1-9]|[\u00b9\u00b2\u00b3])|"
    r"lpt(?:[1-9]|[\u00b9\u00b2\u00b3]))",
    re.IGNORECASE,
)


def validate_sec_document_basename(
    value: object,
    *,
    allowed_suffixes: Iterable[str] = SEC_DOCUMENT_SUFFIXES,
    context: str = "SEC document name",
) -> str:
    """Return a validated, unchanged SEC basename or raise ``ValueError``.

    The function deliberately does not silently trim or Unicode-normalize a
    remote name.  Mutating it could make the audited metadata identify bytes
    different from those eventually opened.  Valid names are NFC-normalized,
    have no leading/trailing whitespace, and use only an explicitly permitted
    suffix.  Internal ordinary spaces are supported and are URL-quoted by
    :func:`quote_sec_document_basename`.
    """

    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a nonempty string")
    name = value
    if unicodedata.normalize("NFC", name) != name:
        raise ValueError(f"{context} must use NFC Unicode normalization: {name!r}")
    if name != name.strip():
        raise ValueError(f"{context} has leading or trailing whitespace: {name!r}")
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise ValueError(f"{context} contains a control/format character: {name!r}")
    if any(char.isspace() and char != " " for char in name):
        raise ValueError(f"{context} contains non-normalized whitespace: {name!r}")
    if "?" in name or "#" in name:
        raise ValueError(f"{context} contains a URL query or fragment marker: {name!r}")
    if any(char in name for char in '<>"|*'):
        raise ValueError(f"{context} contains a Windows-invalid character: {name!r}")
    if "/" in name or "\\" in name:
        raise ValueError(f"{context} must be a basename without separators: {name!r}")

    windows_path = PureWindowsPath(name)
    if windows_path.is_absolute() or windows_path.drive or windows_path.root or ":" in name:
        raise ValueError(f"{context} must not be absolute or drive-qualified: {name!r}")
    if name in {".", ".."}:
        raise ValueError(f"{context} must not be a dot path: {name!r}")
    if name.endswith((".", " ")):
        raise ValueError(f"{context} must not end in a dot or space: {name!r}")

    # Windows reserves the first path stem even when one or more extensions
    # follow it (for example CON.htm and COM1.report.xml).
    device_stem = name.split(".", 1)[0].rstrip(" .")
    if _WINDOWS_DEVICE_STEM.fullmatch(device_stem):
        raise ValueError(f"{context} uses a Windows-reserved device stem: {name!r}")

    permitted = frozenset(str(suffix).casefold() for suffix in allowed_suffixes)
    suffix = Path(name).suffix.casefold()
    if not suffix or suffix not in permitted:
        raise ValueError(
            f"{context} has unsupported suffix {suffix or '<none>'!r}: {name!r}"
        )
    if not name[: -len(suffix)]:
        raise ValueError(f"{context} has an empty filename stem: {name!r}")
    return name


def resolve_sec_document_path(
    accession_dir: Path,
    value: object,
    *,
    allowed_suffixes: Iterable[str] = SEC_DOCUMENT_SUFFIXES,
    containment_root: Path | None = None,
    require_file: bool = False,
    context: str = "SEC document name",
) -> Path:
    """Validate and resolve a document while enforcing directory containment.

    Resolution follows existing symlinks before the containment check, so a
    cached symlink that points outside its accession directory is rejected
    before any caller reads or hashes the target.
    """

    name = validate_sec_document_basename(
        value, allowed_suffixes=allowed_suffixes, context=context
    )
    directory = resolve_path(accession_dir, strict=False)
    if containment_root is not None:
        root = resolve_path(containment_root, strict=False)
        try:
            directory.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"SEC accession directory escapes its cache root: {directory}"
            ) from exc
    candidate = directory / name
    try:
        resolved = resolve_path(candidate, strict=require_file)
    except OSError as exc:
        raise ValueError(f"Unable to resolve {context}: {candidate}") from exc
    try:
        resolved.relative_to(directory)
    except ValueError as exc:
        raise ValueError(
            f"{context} resolves outside the accession directory: {name!r}"
        ) from exc
    if require_file and not is_file(resolved):
        raise ValueError(f"{context} is not a regular file: {name!r}")
    return resolved


def quote_sec_document_basename(
    value: object,
    *,
    allowed_suffixes: Iterable[str] = SEC_DOCUMENT_SUFFIXES,
    context: str = "SEC document name",
) -> str:
    """Validate a filing document basename and quote it as one URL segment."""

    name = validate_sec_document_basename(
        value, allowed_suffixes=allowed_suffixes, context=context
    )
    return quote(name, safe="")


def validate_sec_relative_document_path(
    value: object,
    *,
    allowed_suffixes: Iterable[str] | None = None,
    allow_blank: bool = False,
    context: str = "SEC relative document path",
) -> str:
    """Validate a slash-delimited SEC archive-relative document path."""

    if allow_blank and value == "":
        return ""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a nonempty string")
    relative = value
    if unicodedata.normalize("NFC", relative) != relative:
        raise ValueError(f"{context} must use NFC Unicode normalization")
    if relative != relative.strip() or relative.endswith((".", " ")):
        raise ValueError(f"{context} has unsafe leading/trailing text: {relative!r}")
    if "\\" in relative or "?" in relative or "#" in relative:
        raise ValueError(f"{context} contains an unsafe separator/query marker")
    if any(unicodedata.category(char).startswith("C") for char in relative):
        raise ValueError(f"{context} contains a control/format character")
    if any(char in relative for char in '<>"|*'):
        raise ValueError(f"{context} contains a Windows-invalid character")
    posix = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise ValueError(f"{context} must be relative")
    if not posix.parts or any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError(f"{context} contains an empty/dot path segment")
    if posix.as_posix() != relative:
        raise ValueError(f"{context} is not canonically slash-delimited")
    for position, part in enumerate(posix.parts):
        if part != part.strip() or part.endswith((".", " ")):
            raise ValueError(f"{context} has an unsafe path segment: {part!r}")
        suffixes = allowed_suffixes if position == len(posix.parts) - 1 else None
        # Directory segments have no suffix contract, but share all basename
        # safety rules. Add a private sentinel suffix only for validation.
        candidate = part if suffixes is not None else part + ".dir"
        validate_sec_document_basename(
            candidate,
            allowed_suffixes=suffixes or frozenset({".dir"}),
            context=f"{context} segment",
        )
    return relative


def resolve_sec_relative_document_path(
    accession_dir: Path,
    value: object,
    *,
    allowed_suffixes: Iterable[str] | None = None,
    containment_root: Path | None = None,
    require_file: bool = False,
    context: str = "SEC relative document path",
) -> Path:
    relative = validate_sec_relative_document_path(
        value, allowed_suffixes=allowed_suffixes, context=context
    )
    directory = resolve_path(accession_dir, strict=False)
    root = (
        resolve_path(containment_root, strict=False)
        if containment_root is not None
        else directory
    )
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise ValueError("SEC accession directory escapes its cache root") from exc
    if containment_root is not None:
        lexical_root = absolute_path(containment_root)
        lexical_directory = absolute_path(accession_dir)
        try:
            relative_directory = lexical_directory.relative_to(lexical_root)
        except ValueError as exc:
            raise ValueError("SEC accession directory is not lexically contained") from exc
        expected_directory = root / relative_directory
        if directory != expected_directory:
            raise ValueError("SEC accession directory has a symlinked identity")
    try:
        resolved = resolve_path(
            directory / PurePosixPath(relative), strict=require_file
        )
    except OSError as exc:
        raise ValueError(f"Unable to resolve {context}") from exc
    try:
        resolved.relative_to(directory)
    except ValueError as exc:
        raise ValueError(f"{context} resolves outside the accession directory") from exc
    if require_file and not is_file(resolved):
        raise ValueError(f"{context} is not a regular file")
    return resolved


def quote_sec_relative_document_path(
    value: object,
    *,
    allowed_suffixes: Iterable[str] | None = None,
    context: str = "SEC relative document path",
) -> str:
    relative = validate_sec_relative_document_path(
        value, allowed_suffixes=allowed_suffixes, context=context,
    )
    return "/".join(quote(part, safe="") for part in PurePosixPath(relative).parts)


def resolve_sec_seal_root(
    cache_root: Path,
    relative_path: object,
    *,
    expected_asof: str | None = None,
) -> Path:
    """Resolve canonical ``sealed/YYYY-MM-DD`` beneath a configured cache."""

    if not isinstance(relative_path, str):
        raise ValueError("SEC seal_relative_path must be a string")
    relative = relative_path
    match = re.fullmatch(r"sealed/(\d{4}-\d{2}-\d{2})", relative)
    if match is None:
        raise ValueError(
            f"SEC seal_relative_path must be canonical sealed/YYYY-MM-DD: {relative!r}"
        )
    if expected_asof is not None and match.group(1) != str(expected_asof)[:10]:
        raise ValueError(
            "SEC seal_relative_path date does not match the requested as-of date"
        )
    root = resolve_path(cache_root, strict=False)
    seal = resolve_path(root / "sealed" / match.group(1), strict=False)
    try:
        seal.relative_to(root)
    except ValueError as exc:
        raise ValueError("SEC seal root escapes the configured cache root") from exc
    return seal
