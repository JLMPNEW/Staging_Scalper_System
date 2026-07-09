from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "ticker_mapping" / "build_cusip_from_edgar.py"
SPEC = importlib.util.spec_from_file_location("build_cusip_from_edgar", SCRIPT_PATH)
assert SPEC is not None
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_normalize_ticker_and_cik_are_nan_and_decimal_safe() -> None:
    assert module.normalize_ticker(math.nan) == ""
    assert module.normalize_ticker("brk.b") == "BRK-B"
    assert module.normalize_cik("320193.0") == "0000320193"
    assert module.normalize_cik("0000320193") == "0000320193"
    assert module.normalize_cik("320193 trailing") == ""


def test_cusip_extraction_uses_standalone_tokens_not_sliding_windows() -> None:
    assert module._extract_cusip_from_fragment("CUSIP 037833100") == "037833100"
    assert module._extract_cusip_from_fragment("FIGI BBG000B9XRY4") is None
    assert module._extract_cusip_from_fragment("12,345,678 90 shares") is None
    assert module.extract_cusip_from_document("CUSIP Number for the Common Stock of the Company is 037833100")[0] == "037833100"


def test_generic_document_scan_does_not_find_far_away_numeric_false_positive() -> None:
    text = "The word CUSIP appears on this cover page. " + ("x" * 500) + " 037833100"
    assert module.extract_cusip_from_document(text) == (None, "none")


def test_complete_submission_url_uses_dashed_accession_filename() -> None:
    filing = module.FilingRef(
        cik10="0000320193",
        accession="0000320193-24-000123",
        accession_nodash="000032019324000123",
        filing_date="2024-05-01",
        form="10-Q",
        primary_document="",
    )
    urls = module._build_submission_urls(filing)
    assert urls == [
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123.txt"
    ]


def test_default_forms_exclude_debt_prospectus_and_event_report_forms() -> None:
    excluded = {"424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "8-K", "6-K"}
    assert excluded.isdisjoint(set(module.DEFAULT_FORMS))
