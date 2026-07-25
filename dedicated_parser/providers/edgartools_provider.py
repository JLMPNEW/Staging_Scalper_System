from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def inspect_full_submission(
    path: Path,
    *,
    state_dir: Path,
) -> dict[str, Any]:
    """Read local SGML metadata through EdgarTools without network acquisition."""

    worker_state_dir = state_dir / f"worker-{os.getpid()}"
    worker_state_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("EDGAR_LOCAL_DATA_DIR", str(worker_state_dir))
    os.environ.setdefault("EDGAR_USE_LOCAL_DATA", "1")
    try:
        from edgar.sgml import FilingSGML
    except ImportError:
        return {
            "provider": "edgartools",
            "available": False,
            "status": "dependency_missing",
        }

    try:
        filing = FilingSGML.from_source(path)
        attachments = [
            {
                "document_name": str(item.document or ""),
                "document_type": str(item.document_type or ""),
                "description": str(item.description or ""),
                "sequence_number": str(item.sequence_number or ""),
            }
            for item in filing.attachments
        ]
        return {
            "provider": "edgartools",
            "available": True,
            "status": "parsed",
            "accession_number": str(filing.accession_number or ""),
            "cik": str(filing.cik or ""),
            "form_type": str(filing.form or ""),
            "attachment_count": len(attachments),
            "attachments": attachments,
        }
    except Exception as exc:
        return {
            "provider": "edgartools",
            "available": True,
            "status": "parse_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
