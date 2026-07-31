from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = (
    Path(
        r"C:\Users\josel\OneDrive\Desktop\Investment Files\Staging_Scalper_System"
    )
    / "helper_scripts"
    / "update_sec_form4_daily.py"
)
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("update_sec_form4_daily_resume_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeConnection:
    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    def commit(self) -> None:
        return None


def test_loaded_prefix_does_not_consume_processing_cap(monkeypatch) -> None:
    fetched: list[str] = []
    monkeypatch.setattr(MODULE, "accession_from_filename", lambda value: value)
    monkeypatch.setattr(MODULE, "already_loaded", lambda _conn, accession: accession == "loaded")
    monkeypatch.setattr(
        MODULE,
        "fetch_text",
        lambda _session, url, **_kwargs: fetched.append(url) or "submission",
    )
    monkeypatch.setattr(MODULE, "extract_acceptance_ts_utc", lambda _text: "")
    monkeypatch.setattr(MODULE, "xml_block_from_submission_txt", lambda _text: "<ownershipDocument/>")
    monkeypatch.setattr(
        MODULE,
        "parse_form4_xml",
        lambda **kwargs: (
            {
                "accession_number": kwargs["accession_number"],
                "document_type": "4",
            },
            [],
            [],
        ),
    )
    monkeypatch.setattr(MODULE, "upsert_daily_manifest_row", lambda **_kwargs: None)
    monkeypatch.setattr(MODULE, "upsert_submission", lambda *_args: None)
    monkeypatch.setattr(MODULE, "mark_log", lambda **_kwargs: None)
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)
    rows = [
        {
            "form_type": "4",
            "filename": "loaded",
            "source_dataset_id": "2026-07-27",
            "filing_date_iso": "2026-07-27",
            "filing_date": "27-JUL-2026",
            "company_name": "Loaded",
            "cik": "1",
        },
        {
            "form_type": "4",
            "filename": "pending",
            "source_dataset_id": "2026-07-27",
            "filing_date_iso": "2026-07-27",
            "filing_date": "27-JUL-2026",
            "company_name": "Pending",
            "cik": "2",
        },
        {
            "form_type": "4",
            "filename": "overflow",
            "source_dataset_id": "2026-07-27",
            "filing_date_iso": "2026-07-27",
            "filing_date": "27-JUL-2026",
            "company_name": "Overflow",
            "cik": "3",
        },
    ]

    seen, loaded, limit_hit = MODULE.process_rows(
        conn=FakeConnection(),
        session=object(),
        rows=rows,
        form_types={"4"},
        archives_base_url="https://example.test",
        sleep_seconds=0.0,
        filing_timeout_seconds=1,
        filing_max_retries=0,
        filing_missing_statuses={404},
        retry_backoff_base_seconds=0.0,
        retry_backoff_cap_seconds=0.0,
        force_reprocess=False,
        max_filings=1,
        progress_every_filings=0,
        progress_interval_sec=0.0,
    )

    assert (seen, loaded, limit_hit) == (1, 1, True)
    assert fetched == ["https://example.test/pending"]
