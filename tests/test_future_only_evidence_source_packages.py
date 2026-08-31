from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import pytest

from future_only_evidence.transport_score_input_availability import (
    ACTIVATION_BASELINE_ROLE,
    AVAILABILITY_ROW_FIELDS as TRANSPORT_AVAILABILITY_ROW_FIELDS,
    validate_transport_score_input_availability_snapshot,
)
from future_only_evidence.source_packages import (
    build_transport_replay_inputs,
    build_transport_score_replay_baseline,
)
from tests.industrials.test_transportation_future_oos_score_lineage_v1 import (
    ASOF as TRANSPORT_ASOF,
    BASELINE_CUTOFF as TRANSPORT_BASELINE_CUTOFF,
    POLICY_ID as TRANSPORT_POLICY_ID,
    POLICY_PATH as TRANSPORT_POLICY_PATH,
    SIGNAL_CUTOFF as TRANSPORT_SIGNAL_CUTOFF,
    _bundle as _transport_bundle,
    _fixture as _transport_fixture,
)

_TRANSPORT_PANEL_FIELDS = [
    "asof_date",
    "ticker",
    "horizon_sessions",
    "calibration_cohort",
    "metric_values_json",
    "metric_status_json",
    "positioning_score",
    "rank_ready_flag",
    "calibration_eligible_flag",
    "source_score_sha256",
]
_TRANSPORT_FACT_FIELDS = [
    "ticker",
    "metric_id",
    "value",
    "unit",
    "period_end",
    "filing_date",
    "accepted_at",
    "replay_status",
]

def _write_rows_csv(
    path: Path,
    *,
    rows: list[dict[str, object]],
    fields: list[str],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _transport_builder_inputs(tmp_path: Path) -> dict[str, object]:
    fixture_root = tmp_path / "lineage-fixture"
    fixture_root.mkdir()
    fixture = _transport_fixture(fixture_root)
    baseline = fixture["baseline"]
    panel = fixture["panel"]
    facts = fixture["facts"]
    paths = fixture["paths"]
    assert isinstance(baseline, dict)
    assert isinstance(panel, dict)
    assert isinstance(facts, dict)
    assert isinstance(paths, dict)

    baseline_panel_csv = tmp_path / "baseline-panel.csv"
    full_panel_csv = tmp_path / "full-panel.csv"
    facts_csv = tmp_path / "facts.csv"
    staleness_path = tmp_path / "staleness.json"
    baseline_availability_csv = tmp_path / "baseline-availability.csv"
    dynamic_availability_csv = tmp_path / "dynamic-availability.csv"
    _write_rows_csv(
        baseline_panel_csv,
        rows=list(baseline["panel_rows"]),
        fields=_TRANSPORT_PANEL_FIELDS,
    )
    _write_rows_csv(
        full_panel_csv,
        rows=list(panel["rows"]),
        fields=_TRANSPORT_PANEL_FIELDS,
    )
    _write_rows_csv(facts_csv, rows=[], fields=_TRANSPORT_FACT_FIELDS)
    staleness_path.write_text(
        json.dumps(facts["staleness_days"]), encoding="utf-8"
    )
    for source_path, target_path in (
        (
            paths["score_input_availability_baseline_snapshot"],
            baseline_availability_csv,
        ),
        (paths["score_input_availability_snapshot"], dynamic_availability_csv),
    ):
        availability = json.loads(Path(source_path).read_text(encoding="utf-8"))
        rows = [
            *availability["panel_input_rows"],
            *availability["accepted_fact_input_rows"],
        ]
        _write_rows_csv(
            target_path,
            rows=rows,
            fields=sorted(TRANSPORT_AVAILABILITY_ROW_FIELDS),
        )
    return {
        "fixture": fixture,
        "baseline_panel_csv": baseline_panel_csv,
        "full_panel_csv": full_panel_csv,
        "facts_csv": facts_csv,
        "staleness_path": staleness_path,
        "baseline_availability_csv": baseline_availability_csv,
        "dynamic_availability_csv": dynamic_availability_csv,
    }


def _build_transport_baseline_request(
    tmp_path: Path,
    inputs: dict[str, object],
) -> dict[str, object]:
    return build_transport_score_replay_baseline(
        baseline_cutoff_at_utc=TRANSPORT_BASELINE_CUTOFF,
        activation_registered_at_utc="2026-08-25T12:00:00Z",
        raw_panel_path=Path(inputs["baseline_panel_csv"]),
        raw_accepted_facts_path=Path(inputs["facts_csv"]),
        staleness_path=Path(inputs["staleness_path"]),
        v8_policy_path=TRANSPORT_POLICY_PATH,
        raw_source_availability_csv_path=Path(
            inputs["baseline_availability_csv"]
        ),
        snapshot_generated_at_utc="2026-08-24T23:00:30Z",
        policy_id=TRANSPORT_POLICY_ID,
        query_sha256="c" * 64,
        output_path=tmp_path / "built-baseline.json",
        availability_output_path=tmp_path / "built-baseline-availability.json",
        availability_signing_request_output_path=(
            tmp_path / "built-baseline-availability-request.json"
        ),
    )


def test_transport_baseline_builder_emits_structural_unsigned_request(
    tmp_path: Path,
) -> None:
    inputs = _transport_builder_inputs(tmp_path)
    audit = _build_transport_baseline_request(tmp_path, inputs)
    request_path = tmp_path / "built-baseline-availability-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))

    assert audit["baseline_structure_audit"][
        "full_semantic_policy_validation_pass"
    ] is True
    assert audit["signed_baseline_validation_pending"] is True
    assert audit["external_attestation_required"] is True
    assert audit["capture_ready"] is False
    assert audit["production_activation_authorized"] is False
    assert request["request_is_not_a_trusted_attestation"] is True
    assert request["capture_ready"] is False
    assert (
        request["unsigned_attestation_claims"]["evidence_role"]
        == ACTIVATION_BASELINE_ROLE
    )


def test_transport_builder_request_validates_after_independent_signature(
    tmp_path: Path,
) -> None:
    inputs = _transport_builder_inputs(tmp_path)
    _build_transport_baseline_request(tmp_path, inputs)
    baseline = json.loads(
        (tmp_path / "built-baseline.json").read_text(encoding="utf-8")
    )
    availability_path = tmp_path / "built-baseline-availability.json"
    request = json.loads(
        (tmp_path / "built-baseline-availability-request.json").read_text(
            encoding="utf-8"
        )
    )
    attestation = dict(request["unsigned_attestation_claims"])
    attestation.update(
        {
            "authority_id": "market",
            "exported_at_utc": "2026-08-24T23:01:00+00:00",
            "signature_base64": "signature",
            "signed_payload_sha256": "b" * 64,
        }
    )
    attestation_path = tmp_path / "signed-baseline-availability.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    _, _, availability_audit = (
        validate_transport_score_input_availability_snapshot(
            availability_path,
            asof_date="2026-08-24",
            expected_panel_rows=baseline["panel_rows"],
            expected_accepted_fact_rows=baseline["accepted_fact_rows"],
            signal_cutoff_at_utc=TRANSPORT_BASELINE_CUTOFF,
            policy_id=TRANSPORT_POLICY_ID,
            attestation_path=attestation_path,
            expected_attestation_sha256=hashlib.sha256(
                attestation_path.read_bytes()
            ).hexdigest(),
            bundle=_transport_bundle(),
            expected_evidence_role=ACTIVATION_BASELINE_ROLE,
        )
    )

    assert availability_audit["exact_input_content_and_value_pass"] is True
    assert availability_audit["evidence_role"] == ACTIVATION_BASELINE_ROLE


def test_transport_replay_builder_stops_before_signed_availability(
    tmp_path: Path,
) -> None:
    inputs = _transport_builder_inputs(tmp_path)
    _build_transport_baseline_request(tmp_path, inputs)
    fixture = inputs["fixture"]
    assert isinstance(fixture, dict)
    paths = fixture["paths"]
    assert isinstance(paths, dict)

    audit = build_transport_replay_inputs(
        asof_date=TRANSPORT_ASOF,
        raw_panel_path=Path(inputs["full_panel_csv"]),
        raw_accepted_facts_path=Path(inputs["facts_csv"]),
        staleness_path=Path(inputs["staleness_path"]),
        canonical_score_path=Path(paths["canonical_v8_score"]),
        score_replay_baseline_path=tmp_path / "built-baseline.json",
        v8_policy_path=TRANSPORT_POLICY_PATH,
        signal_cutoff_at_utc=TRANSPORT_SIGNAL_CUTOFF,
        scheduled_append_asof_dates=[TRANSPORT_ASOF],
        raw_source_availability_csv_path=Path(
            inputs["dynamic_availability_csv"]
        ),
        snapshot_generated_at_utc="2026-08-26T21:00:30Z",
        policy_id=TRANSPORT_POLICY_ID,
        query_sha256="c" * 64,
        panel_output_path=tmp_path / "built-panel.json",
        accepted_facts_output_path=tmp_path / "built-facts.json",
        availability_output_path=tmp_path / "built-availability.json",
        availability_signing_request_output_path=(
            tmp_path / "built-availability-request.json"
        ),
    )

    assert audit["replay_input_structure_audit"][
        "exact_scheduled_append_pass"
    ] is True
    assert audit["canonical_score_replay_validated"] is False
    assert audit["score_replay_pending_signed_availability"] is True
    assert audit["external_attestation_required"] is True
    assert audit["capture_ready"] is False
    assert audit["production_activation_authorized"] is False


def test_transport_replay_builder_preflights_every_target(
    tmp_path: Path,
) -> None:
    inputs = _transport_builder_inputs(tmp_path)
    _build_transport_baseline_request(tmp_path, inputs)
    fixture = inputs["fixture"]
    assert isinstance(fixture, dict)
    paths = fixture["paths"]
    assert isinstance(paths, dict)
    existing = tmp_path / "existing-request.json"
    existing.write_text("already exists", encoding="utf-8")

    with pytest.raises(FileExistsError, match="create-only"):
        build_transport_replay_inputs(
            asof_date=TRANSPORT_ASOF,
            raw_panel_path=Path(inputs["full_panel_csv"]),
            raw_accepted_facts_path=Path(inputs["facts_csv"]),
            staleness_path=Path(inputs["staleness_path"]),
            canonical_score_path=Path(paths["canonical_v8_score"]),
            score_replay_baseline_path=tmp_path / "built-baseline.json",
            v8_policy_path=TRANSPORT_POLICY_PATH,
            signal_cutoff_at_utc=TRANSPORT_SIGNAL_CUTOFF,
            scheduled_append_asof_dates=[TRANSPORT_ASOF],
            raw_source_availability_csv_path=Path(
                inputs["dynamic_availability_csv"]
            ),
            snapshot_generated_at_utc="2026-08-26T21:00:30Z",
            policy_id=TRANSPORT_POLICY_ID,
            query_sha256="c" * 64,
            panel_output_path=tmp_path / "should-not-exist-panel.json",
            accepted_facts_output_path=tmp_path / "should-not-exist-facts.json",
            availability_output_path=tmp_path / "should-not-exist-availability.json",
            availability_signing_request_output_path=existing,
        )
    assert not (tmp_path / "should-not-exist-panel.json").exists()
    assert not (tmp_path / "should-not-exist-facts.json").exists()
    assert not (tmp_path / "should-not-exist-availability.json").exists()


def test_transport_cli_exposes_signed_availability_request_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from future_only_evidence import source_package_cli

    monkeypatch.setattr(
        sys,
        "argv",
        ["source-package-cli", "transport-replay-inputs", "--help"],
    )
    with pytest.raises(SystemExit) as exc:
        source_package_cli.main()
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--raw-source-availability-csv" in help_text
    assert "--availability-output" in help_text
    assert "--availability-signing-request-output" in help_text


def test_transport_cli_dispatches_all_availability_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from future_only_evidence import source_package_cli

    received: dict[str, object] = {}

    def _fake_builder(**kwargs: object) -> dict[str, object]:
        received.update(kwargs)
        return {"capture_ready": False}

    monkeypatch.setattr(
        source_package_cli,
        "build_transport_replay_inputs",
        _fake_builder,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-package-cli",
            "transport-replay-inputs",
            "--asof",
            TRANSPORT_ASOF,
            "--signal-cutoff-at-utc",
            TRANSPORT_SIGNAL_CUTOFF,
            "--scheduled-asof",
            TRANSPORT_ASOF,
            "--raw-panel",
            "panel.csv",
            "--raw-accepted-facts",
            "facts.csv",
            "--staleness-policy",
            "staleness.json",
            "--canonical-score",
            "score.csv",
            "--score-replay-baseline",
            "baseline.json",
            "--v8-policy",
            "policy.yaml",
            "--raw-source-availability-csv",
            "availability.csv",
            "--snapshot-generated-at-utc",
            "2026-08-26T21:00:30Z",
            "--policy-id",
            TRANSPORT_POLICY_ID,
            "--query-sha256",
            "c" * 64,
            "--panel-output",
            "panel.json",
            "--accepted-facts-output",
            "facts.json",
            "--availability-output",
            "availability.json",
            "--availability-signing-request-output",
            "request.json",
        ],
    )

    assert source_package_cli.main() == 0
    assert received["raw_source_availability_csv_path"] == Path(
        "availability.csv"
    )
    assert received["availability_output_path"] == Path("availability.json")
    assert received["availability_signing_request_output_path"] == Path(
        "request.json"
    )
    assert received["policy_id"] == TRANSPORT_POLICY_ID
    assert json.loads(capsys.readouterr().out)["capture_ready"] is False
