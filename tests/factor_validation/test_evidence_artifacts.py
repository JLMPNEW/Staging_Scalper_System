from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import factor_validation.artifacts as artifact_module
from factor_validation import (
    CampaignRegistry,
    FDRFamily,
    FactorObservation,
    FactorValidationConfig,
    FileSeal,
    ValidationCellRegistration,
    build_acceptance_record,
    evidence_package_path,
    register_campaign,
    sha256_bytes,
    transition_evidence_state,
    validate_factor,
    verify_evidence_package,
    write_evidence_package,
)


def _result():
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
    return validate_factor(
        observations,
        factor_id="quality",
        config=FactorValidationConfig(
            horizon_trading_days=21,
            target_name="sector_residual_forward_return",
            round_trip_cost=0.001,
        ),
    )


def _seal(name: str, content: bytes) -> FileSeal:
    return FileSeal(name, sha256_bytes(content), len(content))


def _registry(
    *,
    campaign_id: str = "campaign_2026q3",
    direction: str = "higher_is_better",
    config_sha256: str | None = None,
    alpha: float = 0.05,
    source_content: bytes = b"source-v1",
    code_content: bytes = b"code-v1",
) -> CampaignRegistry:
    family = FDRFamily("quality_family", ("quality_21d",), alpha)
    cell = ValidationCellRegistration(
        cell_id="quality_21d",
        sector_id="technology",
        factor_id="quality",
        target_name="sector_residual_forward_return",
        horizon_trading_days=21,
        entry_lag_trading_days=1,
        factor_direction=direction,  # type: ignore[arg-type]
        evaluation_step_trading_days=21,
        fdr_family_id=family.family_id,
        fdr_member_id="quality_21d",
        config_sha256=config_sha256 or sha256_bytes(b"config-v1"),
        source_files=(_seal("inputs/factor_panel.csv", source_content),),
        code_files=(_seal("factor_validation/core.py", code_content),),
    )
    return CampaignRegistry(campaign_id, (cell,), (family,))


def _family_p_values(registry: CampaignRegistry, result=None):
    result = result or _result()
    registry.family("quality_family")
    return {"quality_21d": result.primary_p_value}


def _observed(registry: CampaignRegistry):
    return registry.cell("quality_21d").registered_provenance


def test_registry_is_order_stable_and_requires_complete_family_membership() -> None:
    registry = _registry()
    rebuilt = CampaignRegistry.from_dict(registry.to_dict())
    assert rebuilt == registry
    assert rebuilt.registration_sha256 == registry.registration_sha256

    family = FDRFamily("incomplete", ("registered", "missing"), 0.05)
    cell = ValidationCellRegistration(
        cell_id="registered",
        sector_id="technology",
        factor_id="quality",
        target_name="sector_residual_forward_return",
        horizon_trading_days=21,
        entry_lag_trading_days=1,
        factor_direction="higher_is_better",
        evaluation_step_trading_days=21,
        fdr_family_id="incomplete",
        fdr_member_id="registered",
        config_sha256=sha256_bytes(b"config"),
        source_files=(_seal("source.csv", b"source"),),
        code_files=(_seal("code.py", b"code"),),
    )
    with pytest.raises(ValueError, match="registration mismatch"):
        CampaignRegistry("incomplete_campaign", (cell,), (family,))


def test_campaign_must_be_pre_registered_and_registration_is_immutable(tmp_path: Path) -> None:
    registry = _registry()
    result = _result()
    with pytest.raises(ValueError, match="registered before evidence"):
        write_evidence_package(
            tmp_path,
            registry,
            cell_id="quality_21d",
            result=result,
            family_p_values=_family_p_values(registry, result),
            observed_provenance=_observed(registry),
        )
    path = register_campaign(tmp_path, registry)
    original = path.read_bytes()
    assert register_campaign(tmp_path, registry) == path
    assert path.read_bytes() == original
    with pytest.raises(FileExistsError, match="different content"):
        register_campaign(
            tmp_path,
            _registry(config_sha256=sha256_bytes(b"changed-config")),
        )


def test_evidence_package_is_deterministic_atomic_and_immutable(tmp_path: Path) -> None:
    registry = _registry()
    result = _result()
    family_p_values = _family_p_values(registry, result)
    register_campaign(tmp_path / "first", registry)
    register_campaign(tmp_path / "second", registry)
    first = write_evidence_package(
        tmp_path / "first", registry, cell_id="quality_21d", result=result,
        family_p_values=family_p_values, observed_provenance=_observed(registry),
    )
    second = write_evidence_package(
        tmp_path / "second", registry, cell_id="quality_21d", result=result,
        family_p_values=family_p_values, observed_provenance=_observed(registry),
    )

    assert first.state == "accepted"
    assert first.manifest_sha256 == second.manifest_sha256
    assert {item.name: item.read_bytes() for item in first.path.iterdir()} == {
        item.name: item.read_bytes() for item in second.path.iterdir()
    }
    report = verify_evidence_package(
        first.path,
        expected_registry=registry,
        expected_cell_id="quality_21d",
    )
    assert report.ok is True and report.errors == ()
    for name in ("summary.json", "acceptance.json", "campaign_registry.json", "fdr_family.json", "manifest.json"):
        json.loads((first.path / name).read_text(encoding="utf-8"), parse_constant=lambda value: pytest.fail(value))

    with pytest.raises(FileExistsError, match="immutable"):
        write_evidence_package(
            tmp_path / "first", registry, cell_id="quality_21d", result=result,
            family_p_values=family_p_values, observed_provenance=_observed(registry),
        )


def test_tampering_partial_packages_and_registry_drift_fail_closed(tmp_path: Path) -> None:
    registry = _registry()
    result = _result()
    register_campaign(tmp_path / "published", registry)
    package = write_evidence_package(
        tmp_path / "published",
        registry,
        cell_id="quality_21d",
        result=result,
        family_p_values=_family_p_values(registry, result),
        observed_provenance=_observed(registry),
    )
    summary = package.path / "summary.json"
    summary.write_bytes(summary.read_bytes() + b" ")
    report = verify_evidence_package(package.path, expected_registry=registry)
    assert report.ok is False
    assert any("summary.json" in error for error in report.errors)

    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "summary.json").write_text("{}\n", encoding="utf-8")
    partial_report = verify_evidence_package(partial, expected_registry=registry)
    assert partial_report.ok is False
    assert any("package_file_set_mismatch" in error for error in partial_report.errors)

    register_campaign(tmp_path / "clean", registry)
    clean = write_evidence_package(
        tmp_path / "clean",
        registry,
        cell_id="quality_21d",
        result=result,
        family_p_values=_family_p_values(registry, result),
        observed_provenance=_observed(registry),
    )
    drifted = _registry(config_sha256=sha256_bytes(b"config-v2"))
    drift_report = verify_evidence_package(clean.path, expected_registry=drifted)
    assert drift_report.ok is False
    assert "registry_sha256_mismatch" in drift_report.errors


@pytest.mark.parametrize(
    "drifted_registry",
    [
        _registry(config_sha256=sha256_bytes(b"config-v2")),
        _registry(alpha=0.10),
        _registry(source_content=b"source-v2"),
        _registry(code_content=b"code-v2"),
    ],
    ids=("config", "alpha", "source", "code"),
)
def test_registered_provenance_or_family_drift_is_rejected(
    tmp_path: Path,
    drifted_registry: CampaignRegistry,
) -> None:
    registry = _registry()
    result = _result()
    register_campaign(tmp_path, registry)
    package = write_evidence_package(
        tmp_path,
        registry,
        cell_id="quality_21d",
        result=result,
        family_p_values=_family_p_values(registry, result),
        observed_provenance=_observed(registry),
    )
    report = verify_evidence_package(package.path, expected_registry=drifted_registry)
    assert report.ok is False
    assert "registry_sha256_mismatch" in report.errors


@pytest.mark.parametrize(
    "observed_registry",
    [
        _registry(config_sha256=sha256_bytes(b"runtime-config-v2")),
        _registry(source_content=b"runtime-source-v2"),
        _registry(code_content=b"runtime-code-v2"),
    ],
    ids=("config", "source", "code"),
)
def test_runtime_provenance_drift_blocks_publication(
    tmp_path: Path,
    observed_registry: CampaignRegistry,
) -> None:
    registry = _registry()
    result = _result()
    register_campaign(tmp_path, registry)
    with pytest.raises(ValueError, match="runtime source, config, or code provenance drifted"):
        write_evidence_package(
            tmp_path,
            registry,
            cell_id="quality_21d",
            result=result,
            family_p_values=_family_p_values(registry, result),
            observed_provenance=_observed(observed_registry),
        )
    assert not evidence_package_path(tmp_path, registry, cell_id="quality_21d").exists()


def test_interrupted_write_leaves_no_visible_package_or_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    result = _result()
    register_campaign(tmp_path, registry)
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
        write_evidence_package(
            tmp_path,
            registry,
            cell_id="quality_21d",
            result=result,
            family_p_values=_family_p_values(registry, result),
            observed_provenance=_observed(registry),
        )
    assert not target.exists()
    assert list(target.parent.glob(".*.draft-*")) == []
    assert list(target.parent.glob("*.publication.lock")) == []


def test_existing_publication_lock_is_preserved(tmp_path: Path) -> None:
    registry = _registry()
    result = _result()
    register_campaign(tmp_path, registry)
    target = evidence_package_path(tmp_path, registry, cell_id="quality_21d")
    target.parent.mkdir(parents=True)
    lock = target.parent / f".{target.name}.publication.lock"
    lock.write_text("other publisher\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_evidence_package(
            tmp_path,
            registry,
            cell_id="quality_21d",
            result=result,
            family_p_values=_family_p_values(registry, result),
            observed_provenance=_observed(registry),
        )
    assert lock.read_text(encoding="utf-8") == "other publisher\n"


def test_acceptance_rejects_wrong_direction_and_state_machine_is_append_only() -> None:
    registry = _registry(direction="lower_is_better")
    result = _result()
    record = build_acceptance_record(
        registry,
        cell_id="quality_21d",
        result=result,
        family_p_values=_family_p_values(registry, result),
    )
    assert record.state == "rejected"
    assert record.state_history == ("draft", "validated", "rejected")
    assert next(item for item in record.gates if item.name == "factor_direction_consistent").passed is False
    assert transition_evidence_state("accepted", "superseded") == "superseded"
    with pytest.raises(ValueError, match="invalid evidence state transition"):
        transition_evidence_state("accepted", "draft")
    with pytest.raises(ValueError, match="membership mismatch"):
        build_acceptance_record(
            registry,
            cell_id="quality_21d",
            result=result,
            family_p_values={},
        )


def test_rejected_evidence_is_valid_but_cannot_masquerade_as_accepted(tmp_path: Path) -> None:
    registry = _registry(direction="lower_is_better")
    result = _result()
    register_campaign(tmp_path, registry)
    package = write_evidence_package(
        tmp_path,
        registry,
        cell_id="quality_21d",
        result=result,
        family_p_values=_family_p_values(registry, result),
        observed_provenance=_observed(registry),
    )
    assert package.state == "rejected"
    report = verify_evidence_package(package.path, expected_registry=registry)
    assert report.ok is True
    acceptance = json.loads((package.path / "acceptance.json").read_text(encoding="utf-8"))
    assert acceptance["state"] == "rejected"
    assert any(gate["passed"] is False for gate in acceptance["gates"])


def test_supersession_is_append_only_and_preserves_prior_package(tmp_path: Path) -> None:
    old_registry = _registry(campaign_id="campaign_2026q3_v1")
    result = _result()
    register_campaign(tmp_path, old_registry)
    old = write_evidence_package(
        tmp_path,
        old_registry,
        cell_id="quality_21d",
        result=result,
        family_p_values=_family_p_values(old_registry, result),
        observed_provenance=_observed(old_registry),
    )
    old_bytes = {item.name: item.read_bytes() for item in old.path.iterdir()}
    new_registry = _registry(campaign_id="campaign_2026q3_v2")
    register_campaign(tmp_path, new_registry)
    new = write_evidence_package(
        tmp_path,
        new_registry,
        cell_id="quality_21d",
        result=result,
        family_p_values=_family_p_values(new_registry, result),
        observed_provenance=_observed(new_registry),
        supersedes_manifest_sha256=old.manifest_sha256,
    )
    assert new.path != old.path
    assert {item.name: item.read_bytes() for item in old.path.iterdir()} == old_bytes
    manifest = json.loads((new.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["supersedes_manifest_sha256"] == old.manifest_sha256
    assert verify_evidence_package(new.path, expected_registry=new_registry).ok is True


def test_atomic_publish_retries_transient_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    result = _result()
    register_campaign(tmp_path, registry)
    real_rename = artifact_module.os.rename
    attempts = 0

    def flaky_rename(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("OneDrive transient lock")
        real_rename(source, target)

    monkeypatch.setattr(artifact_module.os, "rename", flaky_rename)
    package = write_evidence_package(
        tmp_path,
        registry,
        cell_id="quality_21d",
        result=result,
        family_p_values=_family_p_values(registry, result),
        observed_provenance=_observed(registry),
    )
    assert attempts == 2
    assert verify_evidence_package(package.path, expected_registry=registry).ok is True
