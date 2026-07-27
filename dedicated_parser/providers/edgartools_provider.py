from __future__ import annotations

import contextlib
import io
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
    # Unconditional assignment: setdefault is a no-op when the host already
    # exported these, which would leave edgartools network-capable and point
    # every worker at one shared external data dir (concurrent-write races).
    os.environ["EDGAR_LOCAL_DATA_DIR"] = str(worker_state_dir)
    os.environ["EDGAR_USE_LOCAL_DATA"] = "1"
    captured_stderr = io.StringIO()
    with contextlib.redirect_stderr(captured_stderr):
        try:
            from edgar.sgml import FilingSGML
        except ImportError:
            payload: dict[str, Any] = {
                "provider": "edgartools",
                "available": False,
                "status": "dependency_missing",
            }
        else:
            worker_state_dir.mkdir(parents=True, exist_ok=True)
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
                payload = {
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
                payload = {
                    "provider": "edgartools",
                    "available": True,
                    "status": "parse_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
    stderr_lines = [
        line.strip()
        for line in captured_stderr.getvalue().splitlines()
        if line.strip()
    ]
    payload["stderr_warning_count"] = len(stderr_lines)
    payload["stderr_messages"] = list(dict.fromkeys(stderr_lines))
    return payload
