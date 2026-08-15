from __future__ import annotations

import importlib.util
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_script(relative_path: str, module_name: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_event_anchor_census_retains_included_and_excluded_candidates() -> None:
    module = _load_script(
        "industrials/transportation/scripts/36g_audit_transportation_tanker_excluded_event_sources.py",
        "transportation_tanker_event_anchor_audit",
    )
    rows = [
        {
            "accession_number": "included",
            "candidate_type": "supplemental_event",
            "decision": "INCLUDE",
            "index_status": "CACHED",
        },
        {
            "accession_number": "excluded",
            "candidate_type": "supplemental_event",
            "decision": "EXCLUDE",
            "index_status": "CACHED",
        },
        {
            "accession_number": "missing",
            "candidate_type": "supplemental_event",
            "decision": "EXCLUDE_NO_METADATA_SIGNAL",
            "index_status": "MISSING",
        },
        {
            "accession_number": "periodic",
            "candidate_type": "base_periodic",
            "decision": "INCLUDE",
            "index_status": "CACHED",
        },
    ]

    selected = module._event_candidates(rows)

    assert [row["accession_number"] for row in selected] == ["included", "excluded"]


def test_event_hydrator_selects_primary_and_ex99_by_filename(tmp_path: Path) -> None:
    module = _load_script(
        "industrials/transportation/scripts/36i_hydrate_transportation_tanker_excluded_event_indexes.py",
        "transportation_tanker_event_hydrator",
    )
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "directory": {
                    "item": [
                        {"name": "form6-k.htm", "type": "text.gif"},
                        {"name": "issuer_ex99-1.htm", "type": "text.gif"},
                        {"name": "financial_data.xml", "type": "text.xml"},
                        {"name": "logo.jpg", "type": "image/jpeg"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    selected = module._selected_documents(index_path, form_type="6-K")

    assert selected == ("form6-k.htm", "issuer_ex99-1.htm")
