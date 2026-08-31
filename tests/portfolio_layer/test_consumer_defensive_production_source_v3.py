from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from consumer_defensive.core.calibration_scope import calibration_scope_contract
from consumer_defensive.core.config import load_config
from consumer_defensive.core.production_scores_v3 import (
    build_production_rank_rows,
    file_sha256,
    load_bound_artifacts,
    publish_production_scores,
    publisher_bindings,
)
from industrials.core.config import load_yaml
from portfolio_layer.scores.adapters import run_adapter


ROOT = Path(__file__).resolve().parents[2]
CONSUMER_CONFIG = ROOT / "consumer_defensive" / "config.yaml"
PORTFOLIO_CONFIG = ROOT / "portfolio_layer" / "config.yaml"
COMPLETED_STAGE6A_DB = publisher_bindings(load_config(CONSUMER_CONFIG))[
    "source_database_path"
]


def _write_terminal_manifest(
    tmp_path: Path,
    score_config: dict,
    score_manifest: dict,
    *,
    payload: dict | None = None,
) -> tuple[Path, dict]:
    rank_path = Path(score_manifest["rank_csv_path"]).resolve()
    publisher_path = rank_path.with_name(
        score_config["production_score_manifest_filename"]
    ).resolve()
    terminal_path = (
        tmp_path
        / score_config["production_terminal_manifest_file_path"].replace(
            "{yyyy-mm-dd}", "2026-08-28"
        )
    ).resolve()
    terminal = payload or {
        "schema_version": "consumer_defensive_production_refresh_manifest_v3",
        "status": "PASS",
        "asof_date": "2026-08-28",
        "signal_asof_date": "2026-08-27",
        "failure": None,
        "steps": [
            {"sequence": 1, "status": "PASS", "return_code": 0},
            {"sequence": 2, "status": "PASS", "return_code": 0},
        ],
        "artifacts": {
            "rank_table": {
                "path": str(rank_path),
                "sha256": file_sha256(rank_path),
                "rows": int(score_manifest["rank_row_count"]),
                "rank_ready_rows": int(score_manifest["rank_ready_count"]),
                "oos_valid_rows": int(score_manifest["oos_valid_count"]),
                "calibration_scope_sha256": score_manifest[
                    "calibration_scope_sha256"
                ],
            },
            "publisher_manifest": {
                "path": str(publisher_path),
                "sha256": file_sha256(publisher_path),
                "status": "PASS",
            },
        },
    }
    terminal_path.parent.mkdir(parents=True, exist_ok=True)
    terminal_path.write_text(
        json.dumps(terminal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return terminal_path, terminal


def _prepare_consumer_production_source(
    tmp_path: Path,
):
    bundle = load_config(CONSUMER_CONFIG)
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
    connection = sqlite3.connect(
        f"{COMPLETED_STAGE6A_DB.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        connection.execute("BEGIN")
        data_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
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

    wal_path = Path(f"{COMPLETED_STAGE6A_DB.resolve()}-wal")
    source.update(
        {
            "source_database_file_sha256": file_sha256(COMPLETED_STAGE6A_DB),
            "source_database_wal_file_sha256": (
                file_sha256(wal_path) if wal_path.is_file() else ""
            ),
            "source_database_data_version": data_version,
        }
    )
    score_manifest = publish_production_scores(
        output_root=tmp_path,
        allocation_asof_date="2026-08-28",
        rows=rows,
        source=source,
        artifact_identities=identities,
        source_database_path=COMPLETED_STAGE6A_DB,
    )

    portfolio = load_yaml(PORTFOLIO_CONFIG)
    score_config = next(
        item
        for item in portfolio["score_contract"]["sectors"]
        if item["model_family"] == "consumer_defensive"
    )
    registry_target = tmp_path / score_config[
        "production_activation_registry_file_path"
    ]
    registry_target.parent.mkdir(parents=True, exist_ok=True)
    registry_target.write_bytes(bindings["activation_registry_path"].read_bytes())

    terminal_path, terminal = _write_terminal_manifest(
        tmp_path,
        score_config,
        score_manifest,
    )
    return {
        "activation": activation,
        "rows": rows,
        "scope": calibration_scope_contract(bundle),
        "score_config": score_config,
        "registry_target": registry_target,
        "terminal_path": terminal_path,
        "terminal": terminal,
    }


def test_real_consumer_publisher_output_is_accepted_by_portfolio_adapter(
    tmp_path: Path,
) -> None:
    fixture = _prepare_consumer_production_source(tmp_path)
    score_config = fixture["score_config"]
    adapted = run_adapter(score_config, tmp_path, "2026-08-28")
    eligible = [row for row in adapted.rows if row.investable_eligible == 1]
    scope = fixture["scope"]
    assert len(adapted.rows) == len(fixture["rows"]) == 79
    assert not set(scope["excluded_tickers"]).intersection(
        row.ticker for row in adapted.rows
    )
    assert {
        cohort: sum(row.model_scope_id == cohort for row in adapted.rows)
        for cohort in scope["expected_remaining_current_by_cohort"]
    } == scope["expected_remaining_current_by_cohort"]
    assert eligible
    assert {row.model_scope_id for row in eligible} == set(
        fixture["activation"]["cohorts"]
    )
    assert fixture["registry_target"].resolve() in adapted.source_files
    assert (
        tmp_path
        / "consumer_defensive"
        / "dashboard"
        / "2026-08-28"
        / "consumer_defensive_production_score_manifest_v3.json"
    ).resolve() in adapted.source_files
    assert fixture["terminal_path"].resolve() in adapted.source_files


def test_consumer_production_authority_rejects_missing_terminal_manifest(
    tmp_path: Path,
) -> None:
    fixture = _prepare_consumer_production_source(tmp_path)
    fixture["terminal_path"].unlink()

    with pytest.raises(ValueError, match="terminal manifest is missing"):
        run_adapter(fixture["score_config"], tmp_path, "2026-08-28")


def test_consumer_terminal_must_bind_same_date_artifact_bytes_and_pass_steps(
    tmp_path: Path,
) -> None:
    fixture = _prepare_consumer_production_source(tmp_path)
    baseline = fixture["terminal"]
    mutations = (
        (
            "terminal status",
            lambda value: value.update(status="FAIL"),
            "not a v3 PASS artifact",
        ),
        (
            "allocation date",
            lambda value: value.update(asof_date="2026-08-27"),
            "date mismatch",
        ),
        (
            "rank path",
            lambda value: value["artifacts"]["rank_table"].update(
                path=str(tmp_path / "wrong-rank.csv")
            ),
            "terminal/rank byte binding failed",
        ),
        (
            "rank hash",
            lambda value: value["artifacts"]["rank_table"].update(
                sha256="0" * 64
            ),
            "terminal/rank byte binding failed",
        ),
        (
            "publisher path",
            lambda value: value["artifacts"]["publisher_manifest"].update(
                path=str(tmp_path / "wrong-publisher.json")
            ),
            "terminal/publisher byte binding failed",
        ),
        (
            "publisher hash",
            lambda value: value["artifacts"]["publisher_manifest"].update(
                sha256="f" * 64
            ),
            "terminal/publisher byte binding failed",
        ),
        (
            "step status",
            lambda value: value["steps"][1].update(
                status="FAIL", return_code=1
            ),
            "non-PASS step",
        ),
    )
    for _label, mutate, error in mutations:
        forged = copy.deepcopy(baseline)
        mutate(forged)
        fixture["terminal_path"].write_text(
            json.dumps(forged, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=error):
            run_adapter(
                fixture["score_config"],
                tmp_path,
                "2026-08-28",
            )
