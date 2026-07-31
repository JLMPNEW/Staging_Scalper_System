from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

from dedicated_parser.contracts import file_sha256


OCR_RECOVERY_VERSION = "transportation_dp7b_ocr_recovery_v1"
OCR_TIMEOUT_SECONDS = 600.0

OCR_RESULT_FIELDS = (
    "recovery_version",
    "content_sha256",
    "document_name",
    "ticker_contexts",
    "page_count",
    "content_bytes",
    "isolated_local_path",
    "isolation_method",
    "cache_path",
    "cache_status",
    "extraction_method",
    "ocr_used",
    "text_character_count",
    "warning",
    "elapsed_seconds",
    "error",
)


def tesseract_candidates(
    *,
    python_executable: Path | None = None,
    home_dir: Path | None = None,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    discovered = shutil.which("tesseract")
    if discovered:
        candidates.append(Path(discovered))
    if python_executable is not None:
        environment = python_executable.expanduser().resolve().parent
        candidates.append(
            environment / "Library" / "bin" / "tesseract.exe"
        )
    home = (
        home_dir.expanduser().resolve()
        if home_dir is not None
        else Path.home().resolve()
    )
    candidates.extend(
        (
            home
            / "AppData"
            / "Local"
            / "Programs"
            / "Tesseract-OCR"
            / "tesseract.exe",
            home
            / "AppData"
            / "Local"
            / "Programs"
            / "Tesseract-OCR-Portable"
            / "tesseract.exe",
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        )
    )
    output: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            output.append(path)
            seen.add(key)
    return tuple(output)


def verify_tesseract(
    path: Path,
) -> tuple[str, str]:
    executable = path.expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    completed = subprocess.run(
        [str(executable), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Tesseract verification failed "
            f"code={completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    version = (completed.stdout or completed.stderr).splitlines()[0]
    return version.strip(), file_sha256(executable)


def configure_tesseract_environment(path: Path) -> None:
    executable = path.expanduser().resolve()
    binary_dir = str(executable.parent)
    current = os.environ.get("PATH", "")
    if binary_dir.lower() not in {
        item.strip().lower()
        for item in current.split(os.pathsep)
        if item.strip()
    }:
        os.environ["PATH"] = binary_dir + os.pathsep + current
    tessdata = executable.parent / "tessdata"
    if tessdata.is_dir():
        os.environ["TESSDATA_PREFIX"] = str(tessdata)


def inventory_sha256(root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    if not root.is_dir():
        return count, digest.hexdigest()
    for path in sorted(root.rglob("*.json.gz")):
        stat = path.stat()
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        count += 1
    return count, digest.hexdigest()


def isolate_document(
    *,
    source_path: Path,
    target_path: Path,
    expected_sha256: str,
) -> str:
    source = source_path.expanduser().resolve()
    target = target_path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if file_sha256(source) != expected_sha256:
        raise ValueError(f"Source hash changed: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if file_sha256(target) != expected_sha256:
            raise ValueError(f"Existing isolated file hash changed: {target}")
        return "EXISTING_HASH_VERIFIED"
    try:
        os.link(source, target)
        method = "NTFS_HARDLINK"
    except OSError:
        shutil.copy2(source, target)
        method = "FILE_COPY"
    if file_sha256(target) != expected_sha256:
        raise ValueError(f"Isolated document hash mismatch: {target}")
    return method


def build_recovered_source_rows(
    *,
    base_rows: Sequence[Mapping[str, str]],
    recovered_paths: Mapping[str, Path],
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for source in base_rows:
        content_hash = str(source["content_sha256"]).lower()
        isolated = recovered_paths.get(content_hash)
        if isolated is None:
            continue
        row = dict(source)
        row["local_path"] = str(isolated.resolve())
        row["cache_status"] = "CACHED_HASHED"
        row["source_kind"] = (
            "transportation_non_sec_primary_document"
        )
        output.append(row)
    output.sort(
        key=lambda row: (
            row["ticker"],
            row["accession_number"],
            row["document_name"],
        )
    )
    return output


def summarize_ocr_results(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    recovered = [
        row
        for row in rows
        if str(row.get("cache_status")) == "RECOVERED_OCR"
    ]
    return {
        "ocr_document_count": len(rows),
        "ocr_recovered_document_count": len(recovered),
        "ocr_unrecovered_document_count": len(rows) - len(recovered),
        "ocr_recovered_page_count": sum(
            int(str(row.get("page_count") or 0)) for row in recovered
        ),
        "ocr_recovered_text_character_count": sum(
            int(str(row.get("text_character_count") or 0))
            for row in recovered
        ),
        "ocr_status_counts": dict(
            sorted(
                Counter(
                    str(row.get("cache_status") or "") for row in rows
                ).items()
            )
        ),
        "isolation_method_counts": dict(
            sorted(
                Counter(
                    str(row.get("isolation_method") or "")
                    for row in rows
                ).items()
            )
        ),
    }


def json_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
