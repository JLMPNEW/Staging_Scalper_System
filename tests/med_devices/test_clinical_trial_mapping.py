from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

from med_devices.core.db import init_db


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "med_devices" / "scripts" / "82_sync_med_device_clinical_trials.py"
REVIEW_PATH = REPO_ROOT / "med_devices" / "data" / "clinical_trial_mapping_reviews.csv"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("med_device_clinical_trial_sync_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_trial_reviews_block_si_bone_and_mobi_c_ticker_collisions() -> None:
    module = load_module()
    reviews = module.load_reviews(REVIEW_PATH)
    included = {(row.ticker, row.nct_id) for row in reviews if row.decision == "include"}
    excluded = {(row.ticker, row.nct_id) for row in reviews if row.decision == "exclude"}

    assert included == {
        ("SI", "NCT06754150"),
        ("MOBI", "NCT05301140"),
        ("MOBI", "NCT05691023"),
    }
    assert {
        ("SI", "NCT04194138"),
        ("SI", "NCT05276024"),
        ("SI", "NCT07565545"),
        ("SI", "NCT01681004"),
        ("SI", "NCT01640080"),
        ("SI", "NCT01640353"),
        ("MOBI", "NCT06485206"),
        ("MOBI", "NCT04012996"),
        ("MOBI", "NCT00389597"),
    }.issubset(excluded)


def test_trial_payload_validation_requires_exact_sponsor_and_product_terms() -> None:
    module = load_module()
    review = next(
        row
        for row in module.load_reviews(REVIEW_PATH)
        if row.ticker == "MOBI" and row.nct_id == "NCT05301140"
    )
    payload = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT05301140", "briefTitle": "Vivistim Registry"},
            "statusModule": {
                "overallStatus": "RECRUITING",
                "studyFirstPostDateStruct": {"date": "2024-01-02"},
                "lastUpdatePostDateStruct": {"date": "2026-08-22"},
            },
            "designModule": {"studyType": "OBSERVATIONAL", "enrollmentInfo": {"count": 1000}},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "MicroTransponder Inc."}},
            "armsInterventionsModule": {
                "interventions": [{"type": "DEVICE", "name": "Vivistim System"}]
            },
        }
    }
    parsed = module.parse_study(payload)
    assert parsed.last_update_post_date == "2026-08-22"
    assert module.validate_review(review, payload, parsed) == []

    payload["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"]["name"] = "Highridge Medical"
    parsed = module.parse_study(payload)
    assert any(issue.startswith("sponsor_mismatch:") for issue in module.validate_review(review, payload, parsed))


def test_clinical_trial_schema_stores_mapping_provenance() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(fact_clinical_trial_status)")}
    assert {
        "lead_sponsor",
        "relationship_type",
        "mapping_confidence",
        "mapping_method",
        "valid_from",
        "valid_to",
        "reviewed_at",
        "source_snapshot_asof_date",
    }.issubset(columns)


def test_refresh_pipeline_registers_governed_clinical_trial_sync() -> None:
    spec = importlib.util.spec_from_file_location(
        "med_device_refresh_clinical_trial_stage_test",
        REPO_ROOT / "med_devices" / "scripts" / "71_run_med_device_refresh_pipeline.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    steps = module.build_steps(
        asof="2026-08-22",
        force_refresh=False,
        skip_ibkr_borrow=True,
        skip_form4_runner=True,
        import_positioning_sources="",
    )
    step = next(item for item in steps if item.step_id == "82_sync_clinical_trials")
    assert step.network is True
    assert step.optional is True
    assert step.args == ["--asof", "2026-08-22", "--allow-partial"]
