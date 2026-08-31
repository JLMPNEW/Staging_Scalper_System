from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import consumer_defensive.core.production_refresh_v3 as production_refresh

from consumer_defensive.core.calibration_scope import calibration_scope_contract
from consumer_defensive.core.config import load_config
from consumer_defensive.core.production_refresh_v3 import (
    ProductionRefreshError,
    _verify_existing_terminal,
    build_refresh_plan,
    run_refresh,
    verify_publisher_outputs,
)
from consumer_defensive.core.production_scores_v3 import (
    RANK_COLUMNS,
    load_bound_artifacts,
    publisher_bindings,
    rank_row_sha256,
)
from consumer_defensive.core.promotion_engine_v3 import (
    REQUIRED_COHORTS,
    canonical_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "consumer_defensive" / "config.yaml"


def _value_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan(tmp_path: Path, **kwargs):
    return build_refresh_plan(
        allocation_asof_date="2026-08-28",
        signal_asof_date="2026-08-27",
        config_path=CONFIG,
        output_root=tmp_path / "output",
        **kwargs,
    )


def _write_publisher_outputs(
    plan,
    *,
    leaked_ticker: str | None = None,
    row_authority_override: tuple[str, Any] | None = None,
    manifest_identity_override: tuple[str, Any] | None = None,
) -> None:
    plan.rank_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = load_config(plan.config_path)
    scope = calibration_scope_contract(bundle)
    bindings = publisher_bindings(bundle)
    activation, _candidates, identities = load_bound_artifacts(
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
    rows = []
    with (ROOT / "ticker_mapping" / "consumer_defensive.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        source_tickers = [row["ticker"] for row in csv.DictReader(handle)]
    with (
        ROOT
        / "consumer_defensive"
        / "data"
        / "consumer_defensive_metric_applicability.csv"
    ).open(encoding="utf-8-sig", newline="") as handle:
        cohort_by_ticker = {
            row["ticker"]: row["calibration_cohort_id"]
            for row in csv.DictReader(handle)
        }
    excluded = set(scope["excluded_tickers"])
    reviewed_tickers = sorted(
        ticker for ticker in source_tickers if ticker not in excluded
    )
    assert _value_sha256(reviewed_tickers) == scope[
        "expected_remaining_current_tickers_sha256"
    ]
    for ticker in reviewed_tickers:
        cohort = cohort_by_ticker[ticker]
        lock = activation["cohorts"][cohort]
        eligible = bool(lock["investable"])
        row = {column: "" for column in RANK_COLUMNS}
        row.update(
            {
                    "ticker": ticker,
                    "asof_date": plan.allocation_asof_date,
                    "signal_asof_date": plan.signal_asof_date,
                    "allocation_asof_date": plan.allocation_asof_date,
                    "entry_lag_trading_sessions": "1",
                    "calibration_cohort": cohort,
                    "rank_ready_flag": "1",
                    "oos_score_valid_flag": "1",
                    "portfolio_candidate_gate": str(int(eligible)),
                    "portfolio_candidate_status": (
                        "eligible" if eligible else "not_eligible"
                    ),
                    "portfolio_candidate_reason": (
                        "ok" if eligible else "promotion_or_rank_gate"
                    ),
                    "score_model_version": lock["score_model_version"],
                    "model_version": lock["score_model_version"],
                    "scoring_contract_version": lock[
                        "scoring_contract_version"
                    ],
                    "calibration_lock_date": activation["asof_date"],
                    "promotion_state": lock["promotion_state"],
                    "consumer_defensive_production_lock_id": lock["lock_id"],
                    "consumer_defensive_production_lock_sha256": lock[
                        "payload_sha256"
                    ],
                    "consumer_defensive_model_contract_sha256": lock[
                        "model_contract_sha256"
                    ],
                    "consumer_defensive_decision_sha256": lock[
                        "decision_sha256"
                    ],
                    "consumer_defensive_selected_candidate_id": lock[
                        "selected_candidate_id"
                    ],
                    "consumer_defensive_deployment_state": lock[
                        "deployment_state"
                    ],
                    "consumer_defensive_optimizer_cap": lock["optimizer_cap"],
                    "consumer_defensive_confidence_multiplier": lock[
                        "confidence_multiplier"
                    ],
                    "consumer_defensive_calibration_scope_sha256": scope[
                        "payload_sha256"
                    ],
            }
        )
        rows.append(row)
    if row_authority_override is not None:
        field, value = row_authority_override
        rows[0][field] = value
    if leaked_ticker is not None:
        rows[0]["ticker"] = leaked_ticker
        rows[0]["rank_ready_flag"] = "0"
        rows[0]["oos_score_valid_flag"] = "0"
        rows[0]["portfolio_candidate_gate"] = "0"
    for row in rows:
        row["row_sha256"] = rank_row_sha256(row)
    with plan.rank_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=RANK_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(cast(Any, rows))
    rank_sha = hashlib.sha256(plan.rank_path.read_bytes()).hexdigest()
    ticker_sha = hashlib.sha256(
        json.dumps(
            sorted(str(row["ticker"]) for row in rows),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema_version": "consumer_defensive_production_score_manifest_v3",
        "status": "PASS",
        "allocation_asof_date": plan.allocation_asof_date,
        "signal_asof_date": plan.signal_asof_date,
        "entry_lag_trading_sessions": 1,
        "rank_csv_path": str(plan.rank_path.resolve()),
        "rank_csv_file_sha256": rank_sha,
        "rank_row_count": len(rows),
        "rank_ready_count": sum(row["rank_ready_flag"] == "1" for row in rows),
        "oos_valid_count": sum(
            row["oos_score_valid_flag"] == "1" for row in rows
        ),
        "rank_ready_by_cohort": {
            cohort: sum(
                row["calibration_cohort"] == cohort
                and row["rank_ready_flag"] == "1"
                for row in rows
            )
            for cohort in sorted(REQUIRED_COHORTS)
        },
        "calibration_scope_contract": scope,
        "calibration_scope_sha256": scope["payload_sha256"],
        "source_live_ticker_count": 110,
        "observed_excluded_tickers": scope["excluded_tickers"],
        "observed_excluded_ticker_count": scope["excluded_ticker_count"],
        "published_ticker_count": len(rows),
        "published_tickers_sha256": ticker_sha,
        "published_tickers_by_cohort": scope[
            "expected_remaining_current_by_cohort"
        ],
        "model_contract_sha256_by_cohort": {
            cohort: activation["cohorts"][cohort]["model_contract_sha256"]
            for cohort in sorted(REQUIRED_COHORTS)
        },
        **identities,
        "source_database_path": str(plan.database_path.resolve()),
        "source_database_file_sha256": "a" * 64,
        "source_database_wal_file_sha256": "",
        "source_database_data_version": 1,
        "database_access_mode": "read_only",
        "database_write_count": 0,
        "portfolio_write_performed": False,
    }
    if manifest_identity_override is not None:
        field, value = manifest_identity_override
        manifest[field] = value
    manifest["payload_sha256"] = canonical_sha256(manifest)
    plan.publisher_manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _terminal_step(plan, position: int, *, return_code: int = 0) -> dict[str, Any]:
    step = plan.steps[position - 1]
    log_dir = plan.terminal_manifest_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict[str, Any]] = {}
    for stream in ("stdout", "stderr"):
        path = log_dir / f"{position:02d}_{step.name}.{stream}.log"
        path.write_text(f"{stream} {position}\n", encoding="utf-8")
        records[stream] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
    return {
        "sequence": position,
        "name": step.name,
        "script": str(step.script),
        "script_sha256": hashlib.sha256(step.script.read_bytes()).hexdigest(),
        "network": step.network,
        "argv": list(step.argv(plan.python_executable)),
        "started_at_utc": "2026-08-29T01:00:00Z",
        "completed_at_utc": "2026-08-29T01:00:01Z",
        "return_code": return_code,
        "status": "PASS" if return_code == 0 else "FAIL",
        "stdout_log": records["stdout"],
        "stderr_log": records["stderr"],
    }


def test_refresh_plan_uses_repo_output_root_and_explicit_refresh_modes(
    tmp_path: Path,
) -> None:
    normal = _plan(tmp_path)
    assert normal.output_root == (tmp_path / "output").resolve()
    assert normal.consumer_output_dir == (tmp_path / "output" / "consumer_defensive").resolve()
    assert normal.rank_path == (
        normal.consumer_output_dir
        / "dashboard"
        / "2026-08-28"
        / "consumer_defensive_final_rank_table.csv"
    )
    publisher = normal.steps[-1]
    assert normal.steps[0].name == "stage2_readiness_sync"
    assert normal.steps[0].network is False
    assert normal.steps[1].name == "market_price_sync"
    assert normal.steps[1].network is True
    assert normal.steps[2].name == "market_norgate_price_sync"
    assert normal.steps[2].network is False
    assert normal.steps[3].name == "market_policy_audit"
    assert normal.steps[2].script.name == "03b_import_consumer_defensive_norgate_prices.py"
    assert any(step.name == "positioning_cache_rematch" for step in normal.steps)
    output_index = publisher.arguments.index("--output-root") + 1
    assert Path(publisher.arguments[output_index]) == normal.output_root
    assert all(
        "--force-refresh" not in step.arguments and "--cache-only" not in step.arguments
        for step in normal.steps
        if step.network
    )

    forced = _plan(tmp_path, force_refresh=True)
    cached = _plan(tmp_path, cache_only=True)
    assert all("--force-refresh" in step.arguments for step in forced.steps if step.network)
    assert all("--cache-only" in step.arguments for step in cached.steps if step.network)
    assert len({normal.payload_sha256(), forced.payload_sha256(), cached.payload_sha256()}) == 3
    assert normal.contract_dict()["implementation"]["sha256"]
    assert all(step["script_sha256"] for step in normal.contract_dict()["steps"])
    with pytest.raises(ProductionRefreshError, match="mutually exclusive"):
        _plan(tmp_path, cache_only=True, force_refresh=True)


def test_publisher_verifier_ties_manifest_to_rows_and_chronology(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    _write_publisher_outputs(plan)
    verified = verify_publisher_outputs(plan)
    assert verified["rank_table"]["rows"] == 79
    assert verified["rank_table"]["oos_valid_rows"] == 79

    plan.rank_path.write_text(plan.rank_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ProductionRefreshError, match="hash"):
        verify_publisher_outputs(plan)


def test_publisher_verifier_rejects_excluded_ticker_even_when_not_gated(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    _write_publisher_outputs(plan, leaked_ticker="MKC")
    with pytest.raises(ProductionRefreshError, match="reviewed excluded tickers"):
        verify_publisher_outputs(plan)


def test_publisher_verifier_rejects_noncanonical_lowercase_ticker(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    _write_publisher_outputs(plan, leaked_ticker="mkc")
    with pytest.raises(ProductionRefreshError, match="noncanonical"):
        verify_publisher_outputs(plan)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("consumer_defensive_production_lock_id", "wrong_lock"),
        ("consumer_defensive_model_contract_sha256", "d" * 64),
        ("consumer_defensive_deployment_state", "rollback"),
        ("consumer_defensive_optimizer_cap", "0.0"),
    ],
)
def test_publisher_verifier_rejects_row_promotion_authority_drift(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    plan = _plan(tmp_path)
    _write_publisher_outputs(
        plan,
        row_authority_override=(field, value),
    )
    with pytest.raises(ProductionRefreshError, match="promotion authority mismatch"):
        verify_publisher_outputs(plan)


def test_publisher_verifier_rejects_manifest_promotion_identity_drift(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    _write_publisher_outputs(
        plan,
        manifest_identity_override=(
            "activation_registry_payload_sha256",
            "d" * 64,
        ),
    )
    with pytest.raises(
        ProductionRefreshError,
        match="promotion-artifact identities drifted",
    ):
        verify_publisher_outputs(plan)


def test_publisher_verifier_rejects_stale_row_self_hash(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    _write_publisher_outputs(plan)
    with plan.rank_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["final_score"] = "99.0"
    with plan.rank_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=RANK_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
    manifest = json.loads(plan.publisher_manifest_path.read_text(encoding="utf-8"))
    manifest["rank_csv_file_sha256"] = hashlib.sha256(
        plan.rank_path.read_bytes()
    ).hexdigest()
    manifest.pop("payload_sha256", None)
    manifest["payload_sha256"] = canonical_sha256(manifest)
    plan.publisher_manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ProductionRefreshError, match="self-hash"):
        verify_publisher_outputs(plan)


def test_existing_pass_is_bound_to_exact_refresh_plan(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    _write_publisher_outputs(plan)
    artifacts = verify_publisher_outputs(plan)
    terminal = {
        "schema_version": "consumer_defensive_production_refresh_manifest_v3",
        "status": "PASS",
        "plan_sha256": plan.payload_sha256(),
        "asof_date": plan.allocation_asof_date,
        "signal_asof_date": plan.signal_asof_date,
        "database_path": str(plan.database_path),
        "output_root": str(plan.output_root),
        "consumer_output_dir": str(plan.consumer_output_dir),
        "cache_only": plan.cache_only,
        "force_refresh": plan.force_refresh,
        "config": {
            "path": str(plan.config_path),
            "sha256": hashlib.sha256(plan.config_path.read_bytes()).hexdigest(),
        },
        "artifacts": artifacts,
        "steps": [
            _terminal_step(plan, position)
            for position, _step in enumerate(plan.steps, start=1)
        ],
    }
    _verify_existing_terminal(plan, terminal)
    with pytest.raises(ProductionRefreshError, match="refresh-plan drift"):
        _verify_existing_terminal(replace(plan, force_refresh=True), terminal)


def test_failed_refresh_resumes_only_verified_pass_prefix(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    _write_publisher_outputs(plan)
    first_calls: list[list[str]] = []

    def fail_second(argv, **_kwargs):
        first_calls.append(list(argv))
        return_code = 7 if len(first_calls) == 2 else 0
        return __import__("subprocess").CompletedProcess(
            argv,
            return_code,
            stdout="first\n",
            stderr="failed\n" if return_code else "",
        )

    failed = run_refresh(plan, executor=fail_second)
    assert failed["status"] == "FAIL"
    assert len(first_calls) == 2

    resumed_calls: list[list[str]] = []

    def pass_all(argv, **_kwargs):
        resumed_calls.append(list(argv))
        return __import__("subprocess").CompletedProcess(
            argv,
            0,
            stdout="resumed\n",
            stderr="",
        )

    passed = run_refresh(plan, resume=True, executor=pass_all)
    assert passed["status"] == "PASS"
    assert passed["resume"]["resumed"] is True
    assert passed["resume"]["start_sequence"] == 2
    assert len(resumed_calls) == len(plan.steps) - 1
    assert Path(passed["resume"]["source"]["path"]).is_file()


def test_resume_rejects_tampered_completed_step_log(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    calls = 0

    def fail_second(argv, **_kwargs):
        nonlocal calls
        calls += 1
        return __import__("subprocess").CompletedProcess(
            argv,
            9 if calls == 2 else 0,
            stdout="logged\n",
            stderr="",
        )

    assert run_refresh(plan, executor=fail_second)["status"] == "FAIL"
    first_log = plan.terminal_manifest_path.parent / "logs" / (
        f"01_{plan.steps[0].name}.stdout.log"
    )
    first_log.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ProductionRefreshError, match="hash or size drift"):
        run_refresh(plan, resume=True)


def test_refresh_plan_inventories_consumer_code_and_data_contracts(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    inventory = plan.contract_dict()["implementation"]["dependency_inventory"]
    files = inventory["files"]
    paths = [record["path"] for record in files]

    assert paths == sorted(paths)
    assert len(paths) == len(set(paths)) == inventory["file_count"]
    assert inventory["file_count"] == sum(inventory["group_counts"].values())
    assert {
        "core/financial_pipeline.py",
        "adapters/dedicated_parser_adapter.py",
        "data/consumer_defensive_financial_concept_map.yaml",
    }.issubset(paths)
    assert all(len(record["sha256"]) == 64 for record in files)
    assert inventory["payload_sha256"] == canonical_sha256(inventory)


@pytest.mark.parametrize(
    "dependency_relative_path",
    [
        "core/financial_pipeline.py",
        "data/consumer_defensive_financial_concept_map.yaml",
    ],
)
def test_existing_pass_is_invalidated_by_consumer_dependency_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dependency_relative_path: str,
) -> None:
    plan = _plan(tmp_path)
    original_plan_sha256 = plan.payload_sha256()
    dependency_path = (
        ROOT / "consumer_defensive" / dependency_relative_path
    ).resolve()
    original_sha256 = production_refresh._sha256

    def drifted_sha256(path: Path) -> str:
        if Path(path).resolve() == dependency_path:
            return "0" * 64
        return original_sha256(path)

    monkeypatch.setattr(production_refresh, "_sha256", drifted_sha256)
    assert plan.payload_sha256() != original_plan_sha256

    old_terminal = {
        "schema_version": "consumer_defensive_production_refresh_manifest_v3",
        "status": "PASS",
        "plan_sha256": original_plan_sha256,
        "asof_date": plan.allocation_asof_date,
        "signal_asof_date": plan.signal_asof_date,
        "database_path": str(plan.database_path),
        "output_root": str(plan.output_root),
        "consumer_output_dir": str(plan.consumer_output_dir),
        "cache_only": plan.cache_only,
        "force_refresh": plan.force_refresh,
    }
    with pytest.raises(ProductionRefreshError, match="refresh-plan drift"):
        _verify_existing_terminal(plan, old_terminal)
