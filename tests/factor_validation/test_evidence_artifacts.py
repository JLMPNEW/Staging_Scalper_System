from __future__ import annotations

import json
import os
import shutil
import socket
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import factor_validation.artifacts as artifact_module
import factor_validation.ledger as ledger_module
from factor_validation import (
    CampaignRegistry,
    FDRFamily,
    FactorObservation,
    FactorValidationConfig,
    FileSeal,
    ProvenanceFileSet,
    ValidationCellRegistration,
    abandon_incomplete_family,
    anchor_campaign_report,
    build_acceptance_record,
    campaign_registry_path,
    campaign_ledger_head_path,
    campaign_ledger_path,
    canonical_json_bytes,
    evidence_package_path,
    read_campaign_ledger,
    repair_campaign_ledger_head,
    register_campaign,
    sha256_bytes,
    transition_evidence_state,
    validate_factor,
    verify_campaign_ledger,
    verify_evidence_package,
    write_evidence_family,
    write_evidence_package,
)


CONFIG_BYTES = b"validation-config-v1\\n"
SOURCE_BYTES = b"point-in-time-factor-panel-v1\\n"
CODE_BYTES = b"shared-factor-kernel-v1\\n"


def _rewrite_ledger_entries(root: Path, mutate: Any) -> None:
    path = campaign_ledger_path(root)
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    mutate(entries)
    prior = ledger_module.LEDGER_GENESIS_SHA256
    for sequence, entry in enumerate(entries, start=1):
        entry["sequence"] = sequence
        entry["prior_entry_sha256"] = prior
        entry["entry_sha256"] = ledger_module._entry_digest(entry)
        prior = entry["entry_sha256"]
    path.write_bytes(b"".join(canonical_json_bytes(entry) for entry in entries))
    ledger_module._write_head(root, sequence=len(entries), head_sha256=prior)


def _config(**changes: object) -> FactorValidationConfig:
    values: dict[str, object] = {
        "horizon_trading_days": 21,
        "target_name": "sector_residual_forward_return",
        "round_trip_cost": 0.001,
        "transition_cadence_trading_days": 21,
    }
    values.update(changes)
    return FactorValidationConfig(**values)  # type: ignore[arg-type]


def _observations() -> list[FactorObservation]:
    observations: list[FactorObservation] = []
    for month in range(30):
        as_of = date(2023 + month // 12, month % 12 + 1, 15)
        for entity in range(12):
            factor = float((entity * 7 + month * 3) % 12)
            noise = float((entity * 13 + month * 5) % 7 - 3) * 0.006
            observations.append(
                FactorObservation(
                    as_of,
                    f"E{entity:02d}",
                    factor,
                    factor * 0.01 + noise,
                    regime="expansion" if month % 2 == 0 else "contraction",
                )
            )
    return observations


def _result(
    *,
    factor_id: str = "quality",
    config: FactorValidationConfig | None = None,
):
    validation_config = config or _config()
    return validate_factor(
        _observations(),
        factor_id=factor_id,
        config=validation_config,
    )


def _seal(name: str, content: bytes) -> FileSeal:
    return FileSeal(name, sha256_bytes(content), len(content))


def _registry(
    *,
    campaign_id: str = "campaign_2026q3",
    direction: str = "higher_is_better",
    config: FactorValidationConfig | None = None,
    config_content: bytes = CONFIG_BYTES,
    source_content: bytes = SOURCE_BYTES,
    code_content: bytes = CODE_BYTES,
    alpha: float = 0.05,
) -> CampaignRegistry:
    validation_config = config or _config()
    family = FDRFamily("quality_family", ("quality_21d",), alpha)
    cell = ValidationCellRegistration(
        cell_id="quality_21d",
        sector_id="technology",
        factor_id="quality",
        target_name=validation_config.target_name,
        horizon_trading_days=validation_config.horizon_trading_days,
        entry_lag_trading_days=validation_config.entry_lag_trading_days,
        factor_direction=direction,  # type: ignore[arg-type]
        evaluation_step_trading_days=21,
        fdr_family_id=family.family_id,
        fdr_member_id="quality_21d",
        config_sha256=sha256_bytes(config_content),
        source_files=(_seal("inputs/factor_panel.csv", source_content),),
        code_files=(_seal("factor_validation/core.py", code_content),),
        validation_config=validation_config,
    )
    return CampaignRegistry(campaign_id, (cell,), (family,))


def _family_registry(
    *,
    campaign_id: str = "family_campaign_2026q3",
) -> CampaignRegistry:
    family = FDRFamily("registered_family", ("member_a", "member_b", "member_c"), 0.05)
    cells = tuple(
        ValidationCellRegistration(
            cell_id=f"cell_{suffix}",
            sector_id="technology",
            factor_id=f"factor_{suffix}",
            target_name=_config().target_name,
            horizon_trading_days=21,
            entry_lag_trading_days=1,
            factor_direction="higher_is_better",
            evaluation_step_trading_days=21,
            fdr_family_id=family.family_id,
            fdr_member_id=f"member_{suffix}",
            config_sha256=sha256_bytes(CONFIG_BYTES),
            source_files=(_seal("inputs/factor_panel.csv", SOURCE_BYTES),),
            code_files=(_seal("factor_validation/core.py", CODE_BYTES),),
            validation_config=_config(),
        )
        for suffix in ("a", "b", "c")
    )
    return CampaignRegistry(campaign_id, cells, (family,))


def _files(
    base: Path,
    *,
    config_content: bytes = CONFIG_BYTES,
    source_content: bytes = SOURCE_BYTES,
    code_content: bytes = CODE_BYTES,
) -> ProvenanceFileSet:
    directory = base / "runtime_files"
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "factor_validation.json"
    source_path = directory / "factor_panel.csv"
    code_path = directory / "core.py"
    config_path.write_bytes(config_content)
    source_path.write_bytes(source_content)
    code_path.write_bytes(code_content)
    return ProvenanceFileSet(
        config_path=config_path,
        source_paths={"inputs/factor_panel.csv": source_path},
        code_paths={"factor_validation/core.py": code_path},
    )


def _register(
    root: Path,
    registry: CampaignRegistry,
    files: ProvenanceFileSet | None = None,
) -> Path:
    concrete = files or _files(root)
    return register_campaign(
        root,
        registry,
        provenance_files={cell.cell_id: concrete for cell in registry.cells},
    )


def _publish(
    root: Path,
    registry: CampaignRegistry,
    *,
    observations: list[FactorObservation] | None = None,
    files: ProvenanceFileSet | None = None,
    supersedes: str | None = None,
):
    cell = registry.cells[0]
    return write_evidence_package(
        root,
        registry,
        cell_id=cell.cell_id,
        observations=observations or _observations(),
        config=cell.validation_config,
        provenance_files=files or _files(root),
        supersedes_manifest_sha256=supersedes,
    )


def test_registry_round_trip_seals_complete_config() -> None:
    registry = _registry()
    rebuilt = CampaignRegistry.from_dict(registry.to_dict())
    assert rebuilt == registry
    assert rebuilt.registration_sha256 == registry.registration_sha256
    cell = rebuilt.cell("quality_21d")
    assert cell.validation_config.to_dict() == _config().to_dict()
    assert cell.validation_config_sha256 == sha256_bytes(
        canonical_json_bytes(_config().to_dict())
    )


def test_registry_rejects_ntfs_casefold_collisions() -> None:
    registry = _registry()
    first = registry.cells[0]
    second = replace(first, cell_id="QUALITY_21D")
    family = FDRFamily("quality_family", ("quality_21d", "QUALITY_21D"), 0.05)
    second = replace(
        second,
        fdr_member_id="QUALITY_21D",
        fdr_family_id=family.family_id,
    )
    with pytest.raises(ValueError, match="case-insensitively"):
        CampaignRegistry("casefold_campaign", (first, second), (family,))

    with pytest.raises(ValueError, match="case-insensitively"):
        ProvenanceFileSet(
            config_path="config.json",
            source_paths={"Panel.csv": "a", "panel.csv": "b"},
            code_paths={"core.py": "c"},
        )


@pytest.mark.parametrize("unsafe_id", ["CON", "prn.txt", "campaign."])
def test_registry_rejects_nonportable_windows_ids(unsafe_id: str) -> None:
    registry = _registry()
    with pytest.raises(ValueError, match="reserved Windows|end with a dot"):
        CampaignRegistry(unsafe_id, registry.cells, registry.fdr_families)


def test_registration_hashes_real_files_and_rejects_fabricated_seals(
    tmp_path: Path,
) -> None:
    fabricated = _registry(source_content=b"NOT-THE-REAL-SOURCE")
    with pytest.raises(ValueError, match="real source, config, or code bytes"):
        _register(tmp_path, fabricated, _files(tmp_path))
    assert not campaign_ledger_path(tmp_path).exists()


def test_campaign_registration_is_immutable_and_ledger_anchored(
    tmp_path: Path,
) -> None:
    registry = _registry()
    path = _register(tmp_path, registry)
    original = path.read_bytes()
    assert _register(tmp_path, registry) == path
    assert path.read_bytes() == original
    entries = read_campaign_ledger(tmp_path)
    assert len(entries) == 1
    assert entries[0]["event_type"] == "campaign_registered"
    assert verify_campaign_ledger(tmp_path).ok is True

    changed = _registry(config_content=b"changed-config")
    with pytest.raises((FileExistsError, ValueError)):
        _register(
            tmp_path,
            changed,
            _files(tmp_path / "changed", config_content=b"changed-config"),
        )


def test_evidence_is_deterministic_atomic_and_ledger_verified(
    tmp_path: Path,
) -> None:
    registry = _registry()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _register(first_root, registry)
    _register(second_root, registry)
    first = _publish(first_root, registry)
    second = _publish(second_root, registry)
    assert first.state == "accepted"
    assert first.manifest_sha256 == second.manifest_sha256
    assert {item.name: item.read_bytes() for item in first.path.iterdir()} == {
        item.name: item.read_bytes() for item in second.path.iterdir()
    }
    assert verify_evidence_package(first.path, expected_registry=registry).ok is True
    assert verify_campaign_ledger(first_root).ok is True


def test_compact_package_path_handles_real_long_technology_identity(
    tmp_path: Path,
) -> None:
    base = _registry(campaign_id="tech_shadow_0f5ad3e90aef")
    cell_id = "fv_5dbc523f29_21d"
    family = FDRFamily("quality_family", (cell_id,), 0.05)
    cell = replace(
        base.cells[0],
        cell_id=cell_id,
        sector_id="software_infrastructure",
        factor_id="deferred_revenue_yoy_growth",
        fdr_member_id=cell_id,
    )
    registry = CampaignRegistry(base.campaign_id, (cell,), (family,))

    target = evidence_package_path(tmp_path, registry, cell_id=cell_id)
    assert target == (tmp_path / registry.campaign_id / "packages" / cell_id).resolve()
    if os.name == "nt":
        assert max(
            len(str(target / name)) for name in artifact_module.PACKAGE_FILE_NAMES
        ) <= 240


def test_existing_legacy_package_path_remains_addressable(tmp_path: Path) -> None:
    registry = _registry()
    cell = registry.cells[0]
    legacy = (
        tmp_path
        / registry.campaign_id
        / cell.sector_id
        / cell.factor_id
        / f"{cell.horizon_trading_days}d"
        / cell.cell_id
    ).resolve()
    legacy.mkdir(parents=True)

    assert evidence_package_path(tmp_path, registry, cell_id=cell.cell_id) == legacy


def test_coordinated_package_regeneration_fails_the_external_anchor(
    tmp_path: Path,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    package = _publish(tmp_path, registry)
    summary_path = package.path / "summary.json"
    manifest_path = package.path / "manifest.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["hit_rate"] = 0.314159
    summary_bytes = canonical_json_bytes(summary)
    summary_path.write_bytes(summary_bytes)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_sha256"]["summary.json"] = sha256_bytes(summary_bytes)
    manifest["file_size_bytes"]["summary.json"] = len(summary_bytes)
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    assert verify_evidence_package(
        package.path,
        expected_registry=registry,
        require_ledger=False,
    ).ok is True
    anchored = verify_evidence_package(package.path, expected_registry=registry)
    assert anchored.ok is False
    assert "manifest_not_anchored_in_campaign_ledger" in anchored.errors
    ledger = verify_campaign_ledger(tmp_path)
    assert ledger.ok is False
    assert any("ledger_manifest_hash_mismatch" in error for error in ledger.errors)


def test_coordinated_accepted_with_failed_gate_mutation_is_rejected(
    tmp_path: Path,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    package = _publish(tmp_path, registry)
    acceptance_path = package.path / "acceptance.json"
    manifest_path = package.path / "manifest.json"
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    acceptance["gates"][0]["passed"] = False
    acceptance_bytes = canonical_json_bytes(acceptance)
    acceptance_path.write_bytes(acceptance_bytes)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["acceptance_record_sha256"] = sha256_bytes(acceptance_bytes)
    manifest["file_sha256"]["acceptance.json"] = sha256_bytes(acceptance_bytes)
    manifest["file_size_bytes"]["acceptance.json"] = len(acceptance_bytes)
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    report = verify_evidence_package(
        package.path,
        expected_registry=registry,
        require_ledger=False,
    )
    assert report.ok is False
    assert "acceptance_gate_contract_mismatch" in report.errors


def test_unknown_manifest_keys_are_rejected_even_without_ledger(
    tmp_path: Path,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    package = _publish(tmp_path, registry)
    manifest_path = package.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["promotion_approved"] = True
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    report = verify_evidence_package(
        package.path,
        expected_registry=registry,
        require_ledger=False,
    )
    assert report.ok is False
    assert any("manifest_schema_keys_mismatch" in error for error in report.errors)


def test_deleted_package_leaves_ledger_scar_and_cannot_be_resubmitted(
    tmp_path: Path,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    package = _publish(tmp_path, registry)
    shutil.rmtree(package.path)
    ledger = verify_campaign_ledger(tmp_path)
    assert ledger.ok is False
    assert any("ledger_package_missing" in error for error in ledger.errors)
    with pytest.raises(FileExistsError, match="already exists in the ledger"):
        _publish(
            tmp_path,
            registry,
            supersedes=package.manifest_sha256,
        )


def test_multi_member_family_rejects_single_and_derives_complete_p_vector(
    tmp_path: Path,
) -> None:
    registry = _family_registry()
    concrete = _files(tmp_path)
    _register(tmp_path, registry, concrete)
    first = registry.cells[0]
    with pytest.raises(ValueError, match="write_evidence_family"):
        write_evidence_package(
            tmp_path,
            registry,
            cell_id=first.cell_id,
            observations=_observations(),
            config=first.validation_config,
            provenance_files=concrete,
        )

    results = {
        cell.cell_id: _result(
            factor_id=cell.factor_id,
            config=cell.validation_config,
        )
        for cell in registry.cells
    }
    packages = write_evidence_family(
        tmp_path,
        registry,
        family_id="registered_family",
        observations={cell.cell_id: _observations() for cell in registry.cells},
        configs={cell.cell_id: cell.validation_config for cell in registry.cells},
        provenance_files={cell.cell_id: concrete for cell in registry.cells},
    )
    assert len(packages) == 3
    expected = {
        cell.fdr_member_id: results[cell.cell_id].primary_p_value
        for cell in registry.cells
    }
    for package in packages:
        fdr = json.loads((package.path / "fdr_family.json").read_text(encoding="utf-8"))
        actual = {
            item["member_id"]: item["p_value"]
            for item in fdr["family_decisions"]
        }
        assert actual == expected
        assert verify_evidence_package(package.path, expected_registry=registry).ok
    assert verify_campaign_ledger(tmp_path).ok is True


def test_interrupted_multi_member_family_resumes_only_exact_anchored_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _family_registry()
    concrete = _files(tmp_path)
    _register(tmp_path, registry, concrete)
    kwargs = {
        "family_id": "registered_family",
        "observations": {cell.cell_id: _observations() for cell in registry.cells},
        "configs": {cell.cell_id: cell.validation_config for cell in registry.cells},
        "provenance_files": {cell.cell_id: concrete for cell in registry.cells},
    }
    real_publish = artifact_module._write_evidence_package
    calls = 0

    def stop_after_first(*args: Any, **call_kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated process interruption")
        return real_publish(*args, **call_kwargs)

    monkeypatch.setattr(artifact_module, "_write_evidence_package", stop_after_first)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        write_evidence_family(tmp_path, registry, **kwargs)
    anchored_before = [
        entry
        for entry in read_campaign_ledger(tmp_path)
        if entry["event_type"] == "publication_succeeded"
    ]
    assert len(anchored_before) == 1
    interrupted = verify_campaign_ledger(tmp_path)
    assert interrupted.ok is False
    assert any("ledger_incomplete_published_family" in item for item in interrupted.errors)

    monkeypatch.setattr(artifact_module, "_write_evidence_package", real_publish)
    packages = write_evidence_family(tmp_path, registry, **kwargs)
    assert len(packages) == 3
    assert all(
        verify_evidence_package(package.path, expected_registry=registry).ok
        for package in packages
    )
    assert verify_campaign_ledger(tmp_path).ok is True
    ledger_before = campaign_ledger_path(tmp_path).read_bytes()
    rerun = write_evidence_family(tmp_path, registry, **kwargs)
    assert len(rerun) == 3
    assert campaign_ledger_path(tmp_path).read_bytes() == ledger_before


def test_incomplete_family_abandonment_is_auditable_and_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _family_registry()
    concrete = _files(tmp_path)
    _register(tmp_path, registry, concrete)
    kwargs = {
        "family_id": "registered_family",
        "observations": {cell.cell_id: _observations() for cell in registry.cells},
        "configs": {cell.cell_id: cell.validation_config for cell in registry.cells},
        "provenance_files": {cell.cell_id: concrete for cell in registry.cells},
    }
    real_publish = artifact_module._write_evidence_package
    calls = 0

    def stop_after_first(*args: Any, **call_kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated process interruption")
        return real_publish(*args, **call_kwargs)

    monkeypatch.setattr(artifact_module, "_write_evidence_package", stop_after_first)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        write_evidence_family(tmp_path, registry, **kwargs)
    monkeypatch.setattr(artifact_module, "_write_evidence_package", real_publish)

    event = abandon_incomplete_family(
        tmp_path,
        registry,
        family_id="registered_family",
        reason_code="code_provenance_changed_after_interruption",
    )
    assert event["event_type"] == "family_abandoned"
    assert verify_campaign_ledger(tmp_path).ok is True
    with pytest.raises(ValueError, match="abandoned evidence family"):
        write_evidence_family(tmp_path, registry, **kwargs)


def test_public_writer_cannot_accept_a_fabricated_result_object(tmp_path: Path) -> None:
    registry = _registry()
    concrete = _files(tmp_path)
    _register(tmp_path, registry, concrete)
    fabricated = replace(
        _result(),
        mean_ic=0.99,
        primary_p_value=1e-12,
        evidence_eligible=True,
    )

    with pytest.raises(TypeError, match="unexpected keyword argument 'result'"):
        write_evidence_package(
            tmp_path,
            registry,
            cell_id="quality_21d",
            result=fabricated,  # type: ignore[call-arg]
            config=_config(),
            provenance_files=concrete,
        )

    assert [entry["event_type"] for entry in read_campaign_ledger(tmp_path)] == [
        "campaign_registered"
    ]


def test_ineligible_evidence_publishes_as_explicit_rejection(tmp_path: Path) -> None:
    config = _config(min_dates=40)
    registry = _registry(config=config)
    _register(tmp_path, registry)

    package = _publish(tmp_path, registry)

    assert package.state == "rejected"
    summary = json.loads((package.path / "summary.json").read_text(encoding="utf-8"))
    acceptance = json.loads(
        (package.path / "acceptance.json").read_text(encoding="utf-8")
    )
    gate_map = {item["name"]: item["passed"] for item in acceptance["gates"]}
    assert summary["evidence_eligible"] is False
    assert summary["primary_p_value"] is None
    assert gate_map["evidence_eligible"] is False
    assert gate_map["primary_p_value_available"] is False
    assert verify_evidence_package(package.path, expected_registry=registry).ok is True


def test_acceptance_derives_family_p_values_and_rejects_wrong_direction() -> None:
    registry = _registry(direction="lower_is_better")
    result = _result()
    record = build_acceptance_record(
        registry,
        cell_id="quality_21d",
        result=result,
        family_results={"quality_21d": result},
    )
    assert record.state == "rejected"
    assert record.state_history == ("draft", "validated", "rejected")
    assert (
        next(
            item
            for item in record.gates
            if item.name == "factor_direction_consistent"
        ).passed
        is False
    )
    with pytest.raises(ValueError, match="family result membership mismatch"):
        build_acceptance_record(
            registry,
            cell_id="quality_21d",
            result=result,
            family_results={},
        )
    assert transition_evidence_state("accepted", "superseded") == "superseded"
    with pytest.raises(ValueError, match="invalid evidence state transition"):
        transition_evidence_state("accepted", "draft")


def test_rejected_evidence_is_valid_but_cannot_masquerade_as_accepted(
    tmp_path: Path,
) -> None:
    registry = _registry(direction="lower_is_better")
    _register(tmp_path, registry)
    package = _publish(tmp_path, registry)
    assert package.state == "rejected"
    report = verify_evidence_package(package.path, expected_registry=registry)
    assert report.ok is True
    acceptance = json.loads(
        (package.path / "acceptance.json").read_text(encoding="utf-8")
    )
    assert acceptance["state"] == "rejected"
    assert any(gate["passed"] is False for gate in acceptance["gates"])


def test_runtime_config_knob_swap_is_rejected_and_recorded(
    tmp_path: Path,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    with pytest.raises(ValueError, match="FactorValidationConfig drifted"):
        write_evidence_package(
            tmp_path,
            registry,
            cell_id="quality_21d",
            observations=_observations(),
            config=_config(min_dates=99),
            provenance_files=_files(tmp_path),
        )
    entries = read_campaign_ledger(tmp_path)
    assert entries[-2]["event_type"] == "publication_attempted"
    assert entries[-1]["event_type"] == "publication_failed"
    assert entries[-1]["error_code"] == "ValueError"
    assert verify_campaign_ledger(tmp_path).ok is True


def test_runtime_file_drift_is_rejected_and_recorded(tmp_path: Path) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    drifted = _files(tmp_path / "drifted", code_content=b"changed-code")
    with pytest.raises(ValueError, match="bytes drifted"):
        write_evidence_package(
            tmp_path,
            registry,
            cell_id="quality_21d",
            observations=_observations(),
            config=_config(),
            provenance_files=drifted,
        )
    assert read_campaign_ledger(tmp_path)[-1]["event_type"] == "publication_failed"


def test_manifest_records_environment_and_full_config(tmp_path: Path) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    package = _publish(tmp_path, registry)
    manifest = json.loads((package.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["validation_config"] == _config().to_dict()
    assert set(manifest["environment"]) == {
        "numpy_version",
        "platform",
        "python_implementation",
        "python_version",
        "scipy_version",
    }
    assert manifest["environment_sha256"] == sha256_bytes(
        canonical_json_bytes(manifest["environment"])
    )


def test_supersession_requires_reachable_same_cell_ledger_transition(
    tmp_path: Path,
) -> None:
    old_registry = _registry(campaign_id="campaign_2026q3_v1")
    _register(tmp_path, old_registry)
    old = _publish(tmp_path, old_registry)
    new_registry = _registry(campaign_id="campaign_2026q3_v2")
    _register(tmp_path, new_registry)
    with pytest.raises(ValueError, match="expected="):
        _publish(tmp_path, new_registry, supersedes="f" * 64)
    new = _publish(tmp_path, new_registry, supersedes=old.manifest_sha256)
    assert old.path.is_dir()
    assert new.path != old.path
    ledger = verify_campaign_ledger(tmp_path)
    assert ledger.ok is True
    assert ledger.superseded_manifest_sha256 == (old.manifest_sha256,)


def test_supersession_is_rechecked_atomically_at_ledger_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_registry = _registry(campaign_id="atomic_supersession_v1")
    _register(tmp_path, old_registry)
    _publish(tmp_path, old_registry)
    new_registry = _registry(campaign_id="atomic_supersession_v2")
    _register(tmp_path, new_registry)
    target = evidence_package_path(tmp_path, new_registry, cell_id="quality_21d")

    monkeypatch.setattr(artifact_module, "_active_prior_manifest", lambda *_args, **_kwargs: None)
    with pytest.raises(ValueError, match="supersession changed before ledger commit"):
        _publish(tmp_path, new_registry, supersedes=None)

    assert not target.exists()
    entries = read_campaign_ledger(tmp_path)
    assert entries[-1]["event_type"] == "publication_failed"
    assert verify_campaign_ledger(tmp_path).ok is True


def test_ledger_head_tampering_fails_closed(tmp_path: Path) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    _publish(tmp_path, registry)
    head_path = campaign_ledger_head_path(tmp_path)
    head = json.loads(head_path.read_text(encoding="utf-8"))
    head["head_sha256"] = "0" * 64
    head_path.write_bytes(canonical_json_bytes(head))
    report = verify_campaign_ledger(tmp_path)
    assert report.ok is False
    assert "ledger_head_mismatch" in report.errors
    repaired = repair_campaign_ledger_head(tmp_path)
    assert repaired["sequence"] == len(read_campaign_ledger(tmp_path))
    assert verify_campaign_ledger(tmp_path).ok is True


def test_ledger_head_replace_retries_transient_onedrive_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    real_replace = ledger_module.os.replace
    attempts = 0

    def flaky_replace(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError("OneDrive transient head lock")
        real_replace(source, target)

    monkeypatch.setattr(ledger_module.os, "replace", flaky_replace)
    monkeypatch.setattr(ledger_module.time, "sleep", lambda _seconds: None)
    package = _publish(tmp_path, registry)
    assert attempts >= 4  # two retries plus attempted/succeeded head commits
    assert verify_evidence_package(package.path, expected_registry=registry).ok


def test_ledger_head_persistent_lock_rolls_back_uncommitted_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    ledger_before = campaign_ledger_path(tmp_path).read_bytes()
    head_before = campaign_ledger_head_path(tmp_path).read_bytes()
    target = evidence_package_path(tmp_path, registry, cell_id="quality_21d")

    def blocked_replace(_source: Path, _target: Path) -> None:
        raise PermissionError("persistent OneDrive head lock")

    monkeypatch.setattr(ledger_module.os, "replace", blocked_replace)
    monkeypatch.setattr(ledger_module.time, "sleep", lambda _seconds: None)
    with pytest.raises(PermissionError, match="persistent OneDrive head lock"):
        _publish(tmp_path, registry)

    assert campaign_ledger_path(tmp_path).read_bytes() == ledger_before
    assert campaign_ledger_head_path(tmp_path).read_bytes() == head_before
    assert read_campaign_ledger(tmp_path)
    assert not target.exists()


def test_interrupted_write_has_failed_ledger_terminal_and_no_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    target = evidence_package_path(tmp_path, registry, cell_id="quality_21d")
    real_write = artifact_module._write_exclusive
    calls = 0

    def fail_after_first(path: Path, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        real_write(path, data)

    monkeypatch.setattr(artifact_module, "_write_exclusive", fail_after_first)
    with pytest.raises(OSError, match="simulated interruption"):
        _publish(tmp_path, registry)
    assert not target.exists()
    assert list(target.parent.glob(".*.draft-*")) == []
    assert read_campaign_ledger(tmp_path)[-1]["event_type"] == "publication_failed"
    assert verify_campaign_ledger(tmp_path).ok is True


def test_evidence_draft_prefix_does_not_repeat_cell_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    real_mkdtemp = artifact_module.tempfile.mkdtemp
    observed_prefixes: list[str] = []

    def capture_mkdtemp(*, prefix: str, dir: Path) -> str:
        observed_prefixes.append(prefix)
        return real_mkdtemp(prefix=prefix, dir=dir)

    monkeypatch.setattr(artifact_module.tempfile, "mkdtemp", capture_mkdtemp)
    _publish(tmp_path, registry)

    assert observed_prefixes == [".draft-"]
    assert registry.cells[0].cell_id not in observed_prefixes[0]


@pytest.mark.skipif(os.name != "nt", reason="Windows path-limit regression")
def test_evidence_path_preflight_accounts_for_longest_package_filename(
    tmp_path: Path,
) -> None:
    registry = _registry()
    short_target = evidence_package_path(tmp_path, registry, cell_id="quality_21d")
    padding = 239 - len(str(short_target))
    assert padding > 0
    long_root = tmp_path / ("x" * padding)

    with pytest.raises(ValueError, match="package file path exceeds"):
        evidence_package_path(long_root, registry, cell_id="quality_21d")


def test_atomic_publish_retries_transient_onedrive_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    real_rename = artifact_module.os.rename
    attempts = 0

    def flaky_rename(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("OneDrive transient lock")
        real_rename(source, target)

    monkeypatch.setattr(artifact_module.os, "rename", flaky_rename)
    package = _publish(tmp_path, registry)
    assert attempts == 2
    assert verify_evidence_package(package.path, expected_registry=registry).ok


def test_partial_package_fails_closed(tmp_path: Path) -> None:
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "summary.json").write_text("{}\\n", encoding="utf-8")
    report = verify_evidence_package(partial)
    assert report.ok is False
    assert any("package_file_set_mismatch" in error for error in report.errors)


def test_stale_pid_lock_is_recovered(tmp_path: Path) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    target = evidence_package_path(tmp_path, registry, cell_id="quality_21d")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / f".{target.name}.publication.lock"
    lock.write_bytes(
        canonical_json_bytes(
            {
                "hostname": socket.gethostname(),
                "pid": max(os.getpid() + 10_000_000, 99_999_999),
                "schema_version": "factor_validation_lock_v1",
            }
        )
    )
    package = _publish(tmp_path, registry)
    assert package.path.is_dir()
    assert not lock.exists()


def test_live_publication_lock_is_preserved(tmp_path: Path) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    target = evidence_package_path(tmp_path, registry, cell_id="quality_21d")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / f".{target.name}.publication.lock"
    content = canonical_json_bytes(
        {
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "schema_version": "factor_validation_lock_v1",
        }
    )
    lock.write_bytes(content)
    with pytest.raises(FileExistsError, match="active factor-validation"):
        _publish(tmp_path, registry)
    assert lock.read_bytes() == content
    assert read_campaign_ledger(tmp_path)[-1]["event_type"] == "publication_failed"


def test_released_lock_is_recovered_even_while_pid_is_alive(tmp_path: Path) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    target = evidence_package_path(tmp_path, registry, cell_id="quality_21d")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / f".{target.name}.publication.lock"
    lock.write_bytes(
        canonical_json_bytes(
            {
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "schema_version": "factor_validation_lock_v1",
                "state": "released",
            }
        )
    )
    package = _publish(tmp_path, registry)
    assert package.path.is_dir()
    assert not lock.exists()


def test_lock_cleanup_failure_does_not_destroy_successful_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    real_release = artifact_module.release_advisory_lock

    def cleanup_reports_failure(path: Path, descriptor: int | None) -> bool:
        real_release(path, descriptor)
        return False

    monkeypatch.setattr(
        artifact_module,
        "release_advisory_lock",
        cleanup_reports_failure,
    )
    package = _publish(tmp_path, registry)
    assert package.path.is_dir()
    assert verify_evidence_package(package.path, expected_registry=registry).ok is True


def test_registry_and_manifest_tampering_exercise_hash_branches(
    tmp_path: Path,
) -> None:
    registry = _registry()
    registry_path = _register(tmp_path, registry)
    package = _publish(tmp_path, registry)
    registry_value = json.loads(registry_path.read_text(encoding="utf-8"))
    registry_value["campaign_id"] = "tampered_campaign"
    registry_path.write_bytes(canonical_json_bytes(registry_value))
    ledger = verify_campaign_ledger(tmp_path)
    assert ledger.ok is False
    assert any("registry_hash_mismatch" in error for error in ledger.errors)
    package_report = verify_evidence_package(package.path, expected_registry=registry)
    assert package_report.ok is False
    assert "ledger_registry_file_mismatch" in package_report.errors


def test_registration_rechecks_ledger_after_target_lock_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(campaign_id="registration_toctou")
    concrete = _files(tmp_path)
    target = campaign_registry_path(tmp_path, registry)
    real_acquire = artifact_module.acquire_advisory_lock
    injected = False

    def acquire_with_competing_registration(path: Path, **kwargs: Any) -> int:
        nonlocal injected
        if path.name == ".campaign_registry.publication.lock" and not injected:
            injected = True
            ledger_module.append_campaign_ledger_event(
                tmp_path,
                event_type="campaign_registered",
                attempt_id=f"register:{registry.campaign_id}:{registry.registration_sha256}",
                campaign_id=registry.campaign_id,
                registry_sha256=registry.registration_sha256,
                state="registered",
                package_relative_path=target.relative_to(tmp_path).as_posix(),
            )
        return real_acquire(path, **kwargs)

    monkeypatch.setattr(
        artifact_module,
        "acquire_advisory_lock",
        acquire_with_competing_registration,
    )
    registered = register_campaign(
        tmp_path,
        registry,
        provenance_files={registry.cells[0].cell_id: concrete},
    )
    assert registered == target
    registrations = [
        item
        for item in read_campaign_ledger(tmp_path)
        if item["event_type"] == "campaign_registered"
    ]
    assert len(registrations) == 1
    assert verify_campaign_ledger(tmp_path).ok is True


def test_missing_manifest_state_is_reported_as_none_not_string_none(
    tmp_path: Path,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    package = _publish(tmp_path, registry)
    manifest_path = package.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("state")
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    report = verify_evidence_package(
        package.path,
        expected_registry=registry,
        require_ledger=False,
    )
    assert report.ok is False
    assert report.state is None
    assert any("manifest_schema_keys_mismatch" in error for error in report.errors)


def test_runtime_provenance_is_reobserved_and_subclasses_are_rejected(
    tmp_path: Path,
) -> None:
    registry = _registry()
    concrete = _files(tmp_path)
    _register(tmp_path, registry, concrete)
    package = _publish(tmp_path, registry, files=concrete)
    code_path = dict(concrete.code_paths)["factor_validation/core.py"]
    code_path.write_bytes(b"runtime-drift\n")

    report = verify_evidence_package(
        package.path,
        expected_registry=registry,
        provenance_files=concrete,
    )
    assert report.ok is False
    assert "runtime_provenance_drift" in report.errors

    class ForgedProvenanceFileSet(ProvenanceFileSet):
        pass

    forged = ForgedProvenanceFileSet(
        config_path=dict(concrete.source_paths).get("missing", concrete.config_path),
        source_paths=dict(concrete.source_paths),
        code_paths=dict(concrete.code_paths),
    )
    other_root = tmp_path / "forged"
    with pytest.raises(TypeError, match="ProvenanceFileSet"):
        register_campaign(
            other_root,
            registry,
            provenance_files={cell.cell_id: forged for cell in registry.cells},
        )


def test_ledger_rejects_unknown_events_and_malformed_appends(
    tmp_path: Path,
) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    ledger_before = campaign_ledger_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="invalid ledger event fields"):
        ledger_module.append_campaign_ledger_event(
            tmp_path,
            event_type="publication_failed",
            attempt_id="bad",
            campaign_id=registry.campaign_id,
            registry_sha256=registry.registration_sha256,
            state="not_failed",
        )
    assert campaign_ledger_path(tmp_path).read_bytes() == ledger_before

    def mutate(entries: list[dict[str, Any]]) -> None:
        entries[-1]["event_type"] = "unknown_event"

    _rewrite_ledger_entries(tmp_path, mutate)
    report = verify_campaign_ledger(tmp_path)
    assert report.ok is False
    assert any("event_type_unknown" in error for error in report.errors)


def test_ledger_detects_multiple_active_publications_for_one_logical_cell(
    tmp_path: Path,
) -> None:
    old_registry = _registry(campaign_id="active_v1")
    _register(tmp_path, old_registry)
    old = _publish(tmp_path, old_registry)
    new_registry = _registry(campaign_id="active_v2")
    _register(tmp_path, new_registry)
    _publish(tmp_path, new_registry, supersedes=old.manifest_sha256)

    def remove_transition(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            if entry.get("campaign_id") == "active_v2" and entry.get("event_type") in {
                "publication_attempted",
                "publication_succeeded",
            }:
                entry["supersedes_manifest_sha256"] = None

    _rewrite_ledger_entries(tmp_path, remove_transition)
    report = verify_campaign_ledger(tmp_path)
    assert report.ok is False
    assert any("ledger_multiple_active_publications" in error for error in report.errors)


def test_campaign_report_is_ledger_anchored_and_idempotent(tmp_path: Path) -> None:
    registry = _registry()
    _register(tmp_path, registry)
    report_path = tmp_path / registry.campaign_id / "reconciliation.json"
    report_path.write_bytes(canonical_json_bytes({"campaign_id": registry.campaign_id}))
    event = anchor_campaign_report(
        tmp_path,
        registry,
        family_id=registry.fdr_families[0].family_id,
        report_path=report_path,
    )
    assert event["event_type"] == "campaign_report_published"
    assert verify_campaign_ledger(tmp_path).ok is True
    ledger_before = campaign_ledger_path(tmp_path).read_bytes()
    anchor_campaign_report(
        tmp_path,
        registry,
        family_id=registry.fdr_families[0].family_id,
        report_path=report_path,
    )
    assert campaign_ledger_path(tmp_path).read_bytes() == ledger_before

    report_path.write_bytes(canonical_json_bytes({"campaign_id": "tampered"}))
    ledger = verify_campaign_ledger(tmp_path)
    assert ledger.ok is False
    assert any("ledger_report_hash_mismatch" in error for error in ledger.errors)


def test_corrupt_stale_lock_uses_age_fallback(tmp_path: Path) -> None:
    lock = tmp_path / ledger_module.LEDGER_LOCK_FILE_NAME
    lock.write_text("{", encoding="utf-8")
    descriptor = ledger_module.acquire_advisory_lock(lock, stale_after_seconds=0.0)
    assert descriptor >= 0
    assert ledger_module.release_advisory_lock(lock, descriptor) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows path-limit regression")
def test_campaign_registry_path_has_windows_preflight(tmp_path: Path) -> None:
    registry = _registry()
    short = campaign_registry_path(tmp_path, registry)
    padding = 241 - len(str(short))
    assert 0 < padding < 240
    long_root = tmp_path / ("r" * padding)
    with pytest.raises(ValueError, match="campaign registry path exceeds"):
        campaign_registry_path(long_root, registry)
