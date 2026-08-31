from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]


def load_script() -> ModuleType:
    path = ROOT / "med_devices/scripts/66_sync_med_device_sec_form4_edgar.py"
    spec = importlib.util.spec_from_file_location("med_device_form4_edgar", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_xsl_primary_document_path_resolves_to_raw_xml() -> None:
    module = load_script()
    assert module.raw_primary_document_name("xslF345X05/ownership.xml") == "ownership.xml"
    assert module.raw_primary_document_name(r"xslF345X06\form4.xml") == "form4.xml"
    assert module.raw_primary_document_name("ownership.xml") == "ownership.xml"


def test_cache_path_does_not_reuse_transformed_xsl_response(tmp_path: Path) -> None:
    module = load_script()
    company = module.Company(company_id=1, ticker="LNTH", cik="0001521036", company_name="Lantheus")
    filing = module.Filing(
        company=company,
        accession_number="0000950170-24-070777",
        primary_document="xslF345X05/ownership.xml",
        filing_date="2024-08-01",
        form="4",
    )
    assert module.cache_path(tmp_path, filing) == tmp_path / "LNTH" / "000095017024070777_ownership.xml"
