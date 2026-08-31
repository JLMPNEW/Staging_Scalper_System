from __future__ import annotations

import csv
import copy
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from consumer_defensive.core.calibration_scope import calibration_scope_contract
from consumer_defensive.core.config import load_config
from consumer_defensive.core.production_scores_v3 import (
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA,
    PORTFOLIO_REQUIRED_COLUMNS,
    RANK_COLUMNS,
    RANK_FILENAME,
    _strict_json_object,
    build_production_rank_rows,
    load_bound_artifacts,
    publish_immutable_text,
    publish_production_scores,
    publisher_bindings,
    rank_row_sha256,
)
from consumer_defensive.core.trading_calendar_v1 import (
    assert_one_session_lag,
    prior_xnys_session,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "consumer_defensive" / "config.yaml"
COMPLETED_STAGE6A_DB = publisher_bindings(load_config(CONFIG))["source_database_path"]


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _artifacts(bundle):
    bindings = publisher_bindings(bundle)
    activation, candidates, identities = load_bound_artifacts(
        activation_registry_path=bindings["activation_registry_path"],
        trusted_activation_registry_file_sha256=bindings[
            "activation_registry_file_sha256"
        ],
        trusted_activation_registry_payload_sha256=bindings[
            "activation_registry_payload_sha256"
        ],
        candidate_registry_path=bindings["candidate_registry_path"],
        trusted_candidate_registry_file_sha256=bindings[
            "candidate_registry_file_sha256"
        ],
        trusted_candidate_registry_payload_sha256=bindings[
            "candidate_registry_payload_sha256"
        ],
    )
    return bindings, activation, candidates, identities


def test_xnys_entry_lag_is_exact_across_weekdays_and_weekends() -> None:
    assert prior_xnys_session("2026-08-28") == "2026-08-27"
    assert prior_xnys_session("2026-08-31") == "2026-08-28"
    assert_one_session_lag(
        signal_asof_date="2026-08-27", allocation_asof_date="2026-08-28"
    )
    with pytest.raises(ValueError, match="exactly one XNYS session"):
        assert_one_session_lag(
            signal_asof_date="2026-08-26", allocation_asof_date="2026-08-28"
        )
    with pytest.raises(ValueError, match="not an XNYS trading session"):
        prior_xnys_session("2026-08-30")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_strict_json_rejects_nonfinite_constants(tmp_path: Path, constant: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"value":' + constant + "}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        _strict_json_object(path, label="test artifact")


def test_strict_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"value":1,"value":2}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        _strict_json_object(path, label="test artifact")


def test_publisher_config_and_authority_pins_tie_exactly() -> None:
    bundle = load_config(CONFIG)
    bindings, activation, candidates, identities = _artifacts(bundle)
    assert bindings["entry_lag_trading_sessions"] == 1
    assert activation["payload_sha256"] == bindings[
        "activation_registry_payload_sha256"
    ]
    assert candidates["payload_sha256"] == bindings[
        "candidate_registry_payload_sha256"
    ]
    assert identities["activation_registry_file_sha256"] == bindings[
        "activation_registry_file_sha256"
    ]
    assert {lock["deployment_state"] for lock in activation["cohorts"].values()} == {
        "active_full"
    }
    assert {lock["optimizer_cap"] for lock in activation["cohorts"].values()} == {
        0.03125
    }


def test_artifact_file_pin_rejects_tampering(tmp_path: Path) -> None:
    bundle = load_config(CONFIG)
    bindings = publisher_bindings(bundle)
    copied = tmp_path / "activation.json"
    copied.write_bytes(bindings["activation_registry_path"].read_bytes() + b" ")
    with pytest.raises(ValueError, match="file SHA-256"):
        load_bound_artifacts(
            activation_registry_path=copied,
            trusted_activation_registry_file_sha256=bindings[
                "activation_registry_file_sha256"
            ],
            trusted_activation_registry_payload_sha256=bindings[
                "activation_registry_payload_sha256"
            ],
            candidate_registry_path=bindings["candidate_registry_path"],
            trusted_candidate_registry_file_sha256=bindings[
                "candidate_registry_file_sha256"
            ],
            trusted_candidate_registry_payload_sha256=bindings[
                "candidate_registry_payload_sha256"
            ],
        )


def test_real_completed_feature_snapshot_builds_exact_production_contract() -> None:
    bundle = load_config(CONFIG)
    bindings, activation, candidates, _ = _artifacts(bundle)
    connection = _open_read_only(COMPLETED_STAGE6A_DB)
    try:
        data_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
        connection.execute("BEGIN")
        rows, source = build_production_rank_rows(
            connection,
            bundle,
            signal_asof_date="2026-08-27",
            allocation_asof_date="2026-08-28",
            activation_registry=activation,
            candidate_registry=candidates,
            bindings=bindings,
        )
        assert int(connection.execute("PRAGMA data_version").fetchone()[0]) == data_version
        connection.rollback()
    finally:
        connection.close()

    scope = calibration_scope_contract(bundle)
    assert len(rows) == scope["expected_remaining_current_ticker_count"] == 79
    assert not set(scope["excluded_tickers"]).intersection(
        row["ticker"] for row in rows
    )
    assert source["calibration_scope_contract"] == scope
    assert source["calibration_scope_sha256"] == scope["payload_sha256"]
    assert source["source_live_ticker_count"] == 110
    assert source["observed_excluded_ticker_count"] == 31
    assert source["published_tickers_by_cohort"] == {
        "beverages": 12,
        "consumer_staples_distribution_retail": 23,
        "household_personal_tobacco": 20,
        "packaged_foods_agricultural_products": 24,
    }
    assert source["stage6b_overlay_required"] is False
    assert source["specialized_weighted_cohorts"] == []
    assert set(source["rank_ready_by_cohort"]) == set(activation["cohorts"])
    assert all(count > 0 for count in source["rank_ready_by_cohort"].values())
    assert all(set(row) == set(RANK_COLUMNS) for row in rows)
    assert all(set(PORTFOLIO_REQUIRED_COLUMNS).issubset(row) for row in rows)
    assert all(row["asof_date"] == "2026-08-28" for row in rows)
    assert all(row["signal_asof_date"] == "2026-08-27" for row in rows)
    assert any(int(row["oos_score_valid_flag"]) == 1 for row in rows)
    for row in rows:
        lock = activation["cohorts"][row["calibration_cohort"]]
        assert row["score_model_version"] == lock["score_model_version"]
        assert row["scoring_contract_version"] == lock["scoring_contract_version"]
        assert row["consumer_defensive_model_contract_sha256"] == lock[
            "model_contract_sha256"
        ]
        assert row["consumer_defensive_production_lock_sha256"] == lock[
            "payload_sha256"
        ]
        assert row["consumer_defensive_calibration_scope_sha256"] == scope[
            "payload_sha256"
        ]


def test_immutable_output_is_idempotent_and_divergence_fails(tmp_path: Path) -> None:
    path = tmp_path / "artifact.txt"
    publish_immutable_text(path, "same\n")
    publish_immutable_text(path, "same\n")
    with pytest.raises(FileExistsError, match="different bytes"):
        publish_immutable_text(path, "different\n")



def test_immutable_output_uses_a_bounded_temporary_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "consumer_defensive_production_score_manifest_v3.json"
    linked_sources: list[Path] = []
    real_link = os.link

    def capture_link(source: str | Path, destination: str | Path) -> None:
        linked_sources.append(Path(source))
        real_link(source, destination)

    monkeypatch.setattr(os, "link", capture_link)
    publish_immutable_text(target, "same\n")

    assert len(linked_sources) == 1
    assert len(linked_sources[0].name) <= 32
    assert target.name not in linked_sources[0].name

def test_publish_manifest_is_self_hashed_and_csv_schema_exact(tmp_path: Path) -> None:
    bundle = load_config(CONFIG)
    bindings, activation, candidates, identities = _artifacts(bundle)
    connection = _open_read_only(COMPLETED_STAGE6A_DB)
    try:
        connection.execute("BEGIN")
        rows, source = build_production_rank_rows(
            connection,
            bundle,
            signal_asof_date="2026-08-27",
            allocation_asof_date="2026-08-28",
            activation_registry=activation,
            candidate_registry=candidates,
            bindings=bindings,
        )
        connection.rollback()
    finally:
        connection.close()
    source.update(
        {
            "source_database_file_sha256": "1" * 64,
            "source_database_wal_file_sha256": "",
            "source_database_data_version": 7,
        }
    )
    manifest = publish_production_scores(
        output_root=tmp_path,
        allocation_asof_date="2026-08-28",
        rows=rows,
        source=source,
        artifact_identities=identities,
        source_database_path=COMPLETED_STAGE6A_DB,
    )
    assert manifest["schema_version"] == MANIFEST_SCHEMA
    assert manifest["status"] == "PASS"
    assert manifest["source_database_file_sha256"] == "1" * 64
    assert manifest["source_database_data_version"] == 7
    assert manifest["stage6b_overlay_required"] is False
    assert manifest["specialized_weighted_cohorts"] == []
    assert manifest["calibration_scope_contract"] == calibration_scope_contract(bundle)
    assert manifest["published_ticker_count"] == 79
    assert manifest["observed_excluded_ticker_count"] == 31
    assert manifest["portfolio_write_performed"] is False
    dated = tmp_path / "consumer_defensive" / "dashboard" / "2026-08-28"
    stored = json.loads((dated / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert stored == manifest
    with (dated / RANK_FILENAME).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == RANK_COLUMNS
        assert len(list(reader)) == len(rows)


def test_publish_rejects_excluded_ticker_even_when_other_fields_are_ineligible(
    tmp_path: Path,
) -> None:
    bundle = load_config(CONFIG)
    bindings, activation, candidates, identities = _artifacts(bundle)
    connection = _open_read_only(COMPLETED_STAGE6A_DB)
    try:
        connection.execute("BEGIN")
        rows, source = build_production_rank_rows(
            connection,
            bundle,
            signal_asof_date="2026-08-27",
            allocation_asof_date="2026-08-28",
            activation_registry=activation,
            candidate_registry=candidates,
            bindings=bindings,
        )
        connection.rollback()
    finally:
        connection.close()
    forged = copy.deepcopy(rows)
    forged[0]["ticker"] = "MKC"
    forged[0]["rank_ready_flag"] = 0
    forged[0]["portfolio_candidate_gate"] = 0
    source.update(
        {
            "source_database_file_sha256": "1" * 64,
            "source_database_wal_file_sha256": "",
            "source_database_data_version": 1,
            "published_tickers_sha256": hashlib.sha256(
                json.dumps(
                    sorted(str(row["ticker"]) for row in forged),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest(),
        }
    )
    with pytest.raises(ValueError, match="reviewed exclusions"):
        publish_production_scores(
            output_root=tmp_path,
            allocation_asof_date="2026-08-28",
            rows=forged,
            source=source,
            artifact_identities=identities,
            source_database_path=COMPLETED_STAGE6A_DB,
        )


def test_publish_rejects_same_size_wrong_cohort_substitution(
    tmp_path: Path,
) -> None:
    bundle = load_config(CONFIG)
    bindings, activation, candidates, identities = _artifacts(bundle)
    connection = _open_read_only(COMPLETED_STAGE6A_DB)
    try:
        connection.execute("BEGIN")
        rows, source = build_production_rank_rows(
            connection,
            bundle,
            signal_asof_date="2026-08-27",
            allocation_asof_date="2026-08-28",
            activation_registry=activation,
            candidate_registry=candidates,
            bindings=bindings,
        )
        connection.rollback()
    finally:
        connection.close()
    forged = copy.deepcopy(rows)
    target = next(
        row for row in forged if row["calibration_cohort"] == "beverages"
    )
    target["calibration_cohort"] = "consumer_staples_distribution_retail"
    target["row_sha256"] = rank_row_sha256(target)
    source["published_tickers_by_cohort"] = {
        cohort: sum(row["calibration_cohort"] == cohort for row in forged)
        for cohort in source["published_tickers_by_cohort"]
    }
    source.update(
        {
            "source_database_file_sha256": "1" * 64,
            "source_database_wal_file_sha256": "",
            "source_database_data_version": 1,
        }
    )
    with pytest.raises(ValueError, match="cohort census"):
        publish_production_scores(
            output_root=tmp_path,
            allocation_asof_date="2026-08-28",
            rows=forged,
            source=source,
            artifact_identities=identities,
            source_database_path=COMPLETED_STAGE6A_DB,
        )


def test_publish_rejects_stale_row_self_hash(tmp_path: Path) -> None:
    bundle = load_config(CONFIG)
    bindings, activation, candidates, identities = _artifacts(bundle)
    connection = _open_read_only(COMPLETED_STAGE6A_DB)
    try:
        connection.execute("BEGIN")
        rows, source = build_production_rank_rows(
            connection,
            bundle,
            signal_asof_date="2026-08-27",
            allocation_asof_date="2026-08-28",
            activation_registry=activation,
            candidate_registry=candidates,
            bindings=bindings,
        )
        connection.rollback()
    finally:
        connection.close()
    forged = copy.deepcopy(rows)
    forged[0]["final_score"] = float(forged[0]["final_score"]) + 1.0
    source.update(
        {
            "source_database_file_sha256": "1" * 64,
            "source_database_wal_file_sha256": "",
            "source_database_data_version": 1,
        }
    )
    with pytest.raises(ValueError, match="self-hash"):
        publish_production_scores(
            output_root=tmp_path,
            allocation_asof_date="2026-08-28",
            rows=forged,
            source=source,
            artifact_identities=identities,
            source_database_path=COMPLETED_STAGE6A_DB,
        )
