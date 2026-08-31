from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from future_only_evidence.canonical_values import exact_utc
from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.prospective_contracts import PROSPECTIVE_ROLE
from future_only_evidence.transport_score_input_availability import (
    ACTIVATION_BASELINE_ROLE,
    FACT_INPUT_KIND,
    PANEL_INPUT_KIND,
    TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
    TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY,
    TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
    transport_fact_identity,
    transport_fact_input_value_sha256,
    transport_panel_input_identity,
    transport_panel_input_value_sha256,
    validate_transport_score_input_availability_snapshot,
)
from industrials.transportation.future_oos_activation_v6 import GROUP_TICKERS
from industrials.transportation.future_oos_score_lineage_v1 import (
    ACCEPTED_FACTS_SCHEMA,
    AUDIT_SCHEMA,
    BASELINE_EVIDENCE_ROLE,
    BASELINE_SCHEMA,
    BASELINE_STRUCTURE_AUDIT_SCHEMA,
    EVIDENCE_ROLE,
    GOVERNED_HORIZON_SESSIONS,
    PANEL_SCHEMA,
    REPLAY_INPUT_STRUCTURE_AUDIT_SCHEMA,
    SCORE_FIELDS,
    SCORE_FORMULA_ID,
    STRUCTURAL_BASELINE_SOURCE_ROLES,
    STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES,
    _eligibility_audit,
    _fact_identity,
    validate_transport_score_replay_baseline,
    validate_transport_score_replay_baseline_structure,
    validate_transport_replay_inputs_structure,
    validate_and_replay_transport_scores,
)
from industrials.transportation.subgroup_scoring import (
    build_v8_score_rows,
    load_subgroup_score_policy,
    ticker_location,
)


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = (
    ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_subgroup_score_policy_v8.yaml"
)
ASOF = "2026-08-26"
SIGNAL_CUTOFF = "2026-08-26T21:00:00Z"
BASELINE_CUTOFF = "2026-08-24T23:00:00Z"
POLICY_ID = "transportation_v8_subgroup_future_oos_v6"


class _Authority:
    def verify_snapshot(
        self,
        payload_bytes: bytes,
        digest: str,
        payload: dict[str, object],
    ) -> bool:
        assert hashlib.sha256(payload_bytes).hexdigest() == digest
        assert json.loads(payload_bytes.decode("utf-8")) == payload
        return True


def _bundle() -> SimpleNamespace:
    return SimpleNamespace(
        market_data_export=_Authority(),
        allowed_provider_ids=frozenset({"provider"}),
        allowed_dataset_ids=frozenset({"transport-score-inputs"}),
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_scores(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SCORE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _availability_row(
    row: dict[str, Any],
    *,
    kind: str,
    position: int,
) -> dict[str, Any]:
    if kind == PANEL_INPUT_KIND:
        identity = transport_panel_input_identity(row)
        value_digest = transport_panel_input_value_sha256(row)
        source_available = (
            "2026-08-26T20:59:00+00:00"
            if row["asof_date"] == ASOF
            else f"{row['asof_date']}T20:00:00+00:00"
        )
    else:
        identity = transport_fact_identity(row)
        value_digest = transport_fact_input_value_sha256(row)
        source_available = str(row["accepted_at"]).replace("Z", "+00:00")
    observation_id = f"{kind}|{identity}"
    return {
        "input_kind": kind,
        "ticker": row["ticker"],
        "input_identity_sha256": identity,
        "input_content_sha256": canonical_sha256(row),
        "input_value_sha256": value_digest,
        "availability_status": "available",
        "source_required_flag": 1,
        "source_id": "governed-export",
        "source_available_at_utc": source_available,
        "source_observation_id": observation_id,
        "source_locator": f"provider://{kind}/{position}/{identity}",
        "source_record_sha256": hashlib.sha256(
            observation_id.encode("utf-8")
        ).hexdigest(),
        "provider_id": "provider",
        "dataset_id": "transport-score-inputs",
    }


def _availability_mapping(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_kind": row["input_kind"],
        "ticker": row["ticker"],
        "input_identity_sha256": row["input_identity_sha256"],
        "input_content_sha256": row["input_content_sha256"],
        "input_value_sha256": row["input_value_sha256"],
        "source_observation_id": row["source_observation_id"],
        "source_record_sha256": row["source_record_sha256"],
        "source_available_at_utc": exact_utc(
            row["source_available_at_utc"],
            label="fixture source availability",
        ).isoformat(),
        "source_locator": row["source_locator"],
        "source_id": row["source_id"],
        "provider_id": row["provider_id"],
        "dataset_id": row["dataset_id"],
    }


def _write_availability_artifacts(
    *,
    snapshot_path: Path,
    attestation_path: Path,
    panel_rows: list[dict[str, Any]],
    fact_rows: list[dict[str, Any]],
    asof: str,
    cutoff: str,
    evidence_role: str,
) -> None:
    panel_availability = [
        _availability_row(row, kind=PANEL_INPUT_KIND, position=index)
        for index, row in enumerate(panel_rows)
    ]
    fact_availability = [
        _availability_row(row, kind=FACT_INPUT_KIND, position=index)
        for index, row in enumerate(fact_rows)
    ]
    generated = (
        "2026-08-24T23:00:30+00:00"
        if evidence_role == ACTIVATION_BASELINE_ROLE
        else (
            "2026-08-25T21:00:30+00:00"
            if asof == "2026-08-25"
            else "2026-08-26T21:00:30+00:00"
        )
    )
    exported = (
        "2026-08-24T23:01:00+00:00"
        if evidence_role == ACTIVATION_BASELINE_ROLE
        else (
            "2026-08-25T21:01:00+00:00"
            if asof == "2026-08-25"
            else "2026-08-26T21:01:00+00:00"
        )
    )
    snapshot = {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
        "evidence_role": evidence_role,
        "asof_date": asof,
        "snapshot_generated_at_utc": generated,
        "panel_input_rows": panel_availability,
        "panel_input_rows_sha256": canonical_sha256(panel_availability),
        "accepted_fact_input_rows": fact_availability,
        "accepted_fact_input_rows_sha256": canonical_sha256(fact_availability),
    }
    snapshot_bytes = json.dumps(snapshot).encode("utf-8")
    snapshot_path.write_bytes(snapshot_bytes)
    identity_census = {
        "panel": [transport_panel_input_identity(row) for row in panel_rows],
        "accepted_facts": [transport_fact_identity(row) for row in fact_rows],
    }
    content_census = {
        "panel": [canonical_sha256(row) for row in panel_rows],
        "accepted_facts": [canonical_sha256(row) for row in fact_rows],
    }
    value_census = {
        "panel": [transport_panel_input_value_sha256(row) for row in panel_rows],
        "accepted_facts": [
            transport_fact_input_value_sha256(row) for row in fact_rows
        ],
    }
    mappings = [
        _availability_mapping(row)
        for row in panel_availability + fact_availability
    ]
    max_available = max(
        exact_utc(
            row["source_available_at_utc"],
            label="fixture source availability",
        )
        for row in panel_availability + fact_availability
    ).isoformat()
    pairs = [
        {"provider_id": "provider", "dataset_id": "transport-score-inputs"}
    ]
    attestation = {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
        "authority_id": "market",
        "signature_base64": "signature",
        "signed_payload_sha256": "b" * 64,
        "family": "transportation",
        "policy_id": POLICY_ID,
        "evidence_role": evidence_role,
        "asof_date": asof,
        "availability_snapshot_sha256": hashlib.sha256(
            snapshot_bytes
        ).hexdigest(),
        "panel_input_rows_sha256": snapshot["panel_input_rows_sha256"],
        "accepted_fact_input_rows_sha256": snapshot[
            "accepted_fact_input_rows_sha256"
        ],
        "input_count": len(panel_rows) + len(fact_rows),
        "panel_input_count": len(panel_rows),
        "accepted_fact_input_count": len(fact_rows),
        "input_identity_census_sha256": canonical_sha256(identity_census),
        "input_content_census_sha256": canonical_sha256(content_census),
        "input_value_census_sha256": canonical_sha256(value_census),
        "source_observation_mapping_sha256": canonical_sha256(mappings),
        "provider_dataset_pair_count": 1,
        "provider_dataset_pairs_sha256": canonical_sha256(pairs),
        "source_max_information_at_utc": max_available,
        "status_effective_through_at_utc": cutoff.replace("Z", "+00:00"),
        "exported_at_utc": exported,
        "status_asof_policy": TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY,
        "query_sha256": "c" * 64,
    }
    attestation_path.write_bytes(json.dumps(attestation).encode("utf-8"))


def _source_metrics(policy: dict[str, Any]) -> list[str]:
    metrics: set[str] = set()
    for cohort in policy["cohorts"].values():
        for group in cohort["groups"].values():
            for feature in group.get("specialized_pack", {}).values():
                metrics.update(
                    str(metric)
                    for metric in (
                        feature.get("source_metrics")
                        or [feature.get("source_metric")]
                    )
                    if metric
                )
    return sorted(metrics)


def _fixture(tmp_path: Path) -> dict[str, Any]:
    policy = load_subgroup_score_policy(POLICY_PATH)
    dates = ("2026-07-31", ASOF)
    tickers = sorted(
        ticker for group_tickers in GROUP_TICKERS.values() for ticker in group_tickers
    )
    panel_rows: list[dict[str, object]] = []
    for score_date in dates:
        for index, ticker in enumerate(tickers):
            location = ticker_location(ticker, score_date, policy)
            assert location is not None
            cohort_id, _ = location
            panel_rows.append(
                {
                    "asof_date": score_date,
                    "ticker": ticker,
                    "horizon_sessions": GOVERNED_HORIZON_SESSIONS,
                    "calibration_cohort": policy["cohorts"][cohort_id][
                        "calibration_cohort"
                    ],
                    "metric_values_json": "{}",
                    "metric_status_json": "{}",
                    "positioning_score": float(20 + index),
                    "rank_ready_flag": 1,
                    "calibration_eligible_flag": 1,
                    "source_score_sha256": hashlib.sha256(
                        f"scoring-features|{score_date}".encode()
                    ).hexdigest(),
                }
            )
    census = {score_date: tickers for score_date in dates}
    panel = {
        "schema_version": PANEL_SCHEMA,
        "evidence_role": EVIDENCE_ROLE,
        "asof_date": ASOF,
        "governed_horizon_sessions": GOVERNED_HORIZON_SESSIONS,
        "rows": panel_rows,
        "rows_sha256": canonical_sha256(panel_rows),
        "date_ticker_census": census,
        "date_ticker_census_sha256": canonical_sha256(census),
    }
    staleness = {metric_id: 550 for metric_id in _source_metrics(policy)}
    facts = {
        "schema_version": ACCEPTED_FACTS_SCHEMA,
        "evidence_role": EVIDENCE_ROLE,
        "asof_date": ASOF,
        "rows": [],
        "rows_sha256": canonical_sha256([]),
        "staleness_days": staleness,
        "staleness_days_sha256": canonical_sha256(staleness),
    }
    baseline_rows = panel_rows[: len(tickers)]
    baseline_census = {dates[0]: tickers}
    baseline_source_hashes = {
        dates[0]: baseline_rows[0]["source_score_sha256"],
    }
    baseline = {
        "schema_version": BASELINE_SCHEMA,
        "evidence_role": BASELINE_EVIDENCE_ROLE,
        "baseline_cutoff_at_utc": BASELINE_CUTOFF,
        "panel_rows": baseline_rows,
        "panel_rows_sha256": canonical_sha256(baseline_rows),
        "date_ticker_census": baseline_census,
        "date_ticker_census_sha256": canonical_sha256(baseline_census),
        "source_score_file_sha256_by_date": baseline_source_hashes,
        "source_score_file_sha256_by_date_sha256": canonical_sha256(
            baseline_source_hashes
        ),
        "accepted_fact_rows": [],
        "accepted_fact_rows_sha256": canonical_sha256([]),
        "accepted_fact_identity_census_sha256": canonical_sha256([]),
        "staleness_days": staleness,
        "staleness_days_sha256": canonical_sha256(staleness),
    }
    score_rows, _, _ = build_v8_score_rows(
        panel_rows=panel_rows,
        accepted_rows=[],
        policy=policy,
        staleness_days=staleness,
    )
    paths = {
        "canonical_v8_score": tmp_path / "scores.csv",
        "scoring_panel": tmp_path / "panel.json",
        "accepted_facts": tmp_path / "facts.json",
        "score_replay_baseline": tmp_path / "baseline.json",
        "score_input_availability_baseline_snapshot": (
            tmp_path / "baseline-availability.json"
        ),
        "score_input_availability_baseline_attestation": (
            tmp_path / "baseline-availability-attestation.json"
        ),
        "score_input_availability_snapshot": tmp_path / "availability.json",
        "score_input_availability_attestation": (
            tmp_path / "availability-attestation.json"
        ),
        "v8_policy": POLICY_PATH,
    }
    _write_scores(paths["canonical_v8_score"], score_rows)
    _write_json(paths["scoring_panel"], panel)
    _write_json(paths["accepted_facts"], facts)
    _write_json(paths["score_replay_baseline"], baseline)
    _write_availability_artifacts(
        snapshot_path=paths["score_input_availability_baseline_snapshot"],
        attestation_path=paths[
            "score_input_availability_baseline_attestation"
        ],
        panel_rows=baseline["panel_rows"],
        fact_rows=baseline["accepted_fact_rows"],
        asof="2026-08-24",
        cutoff=BASELINE_CUTOFF,
        evidence_role=ACTIVATION_BASELINE_ROLE,
    )
    _write_availability_artifacts(
        snapshot_path=paths["score_input_availability_snapshot"],
        attestation_path=paths["score_input_availability_attestation"],
        panel_rows=panel["rows"],
        fact_rows=facts["rows"],
        asof=ASOF,
        cutoff=SIGNAL_CUTOFF,
        evidence_role=PROSPECTIVE_ROLE,
    )
    fixture = {
        "paths": paths,
        "expected": {role: _sha256(path) for role, path in paths.items()},
        "panel": panel,
        "facts": facts,
        "baseline": baseline,
    }
    _, _, baseline_availability_audit = (
        validate_transport_score_input_availability_snapshot(
            paths["score_input_availability_baseline_snapshot"],
            asof_date="2026-08-24",
            expected_panel_rows=baseline["panel_rows"],
            expected_accepted_fact_rows=baseline["accepted_fact_rows"],
            signal_cutoff_at_utc=BASELINE_CUTOFF,
            policy_id=POLICY_ID,
            attestation_path=paths[
                "score_input_availability_baseline_attestation"
            ],
            expected_attestation_sha256=fixture["expected"][
                "score_input_availability_baseline_attestation"
            ],
            bundle=_bundle(),
            expected_evidence_role=ACTIVATION_BASELINE_ROLE,
        )
    )
    fixture["baseline_availability_audit"] = baseline_availability_audit
    return fixture


def _validate(fixture: dict[str, Any]) -> dict[str, Any]:
    paths = fixture["paths"]
    return validate_and_replay_transport_scores(
        asof_date=ASOF,
        signal_cutoff_at_utc=SIGNAL_CUTOFF,
        scheduled_append_asof_dates=[ASOF],
        score_path=paths["canonical_v8_score"],
        scoring_panel_path=paths["scoring_panel"],
        accepted_facts_path=paths["accepted_facts"],
        score_replay_baseline_path=paths["score_replay_baseline"],
        score_input_availability_baseline_snapshot_path=paths[
            "score_input_availability_baseline_snapshot"
        ],
        score_input_availability_baseline_attestation_path=paths[
            "score_input_availability_baseline_attestation"
        ],
        score_input_availability_snapshot_path=paths[
            "score_input_availability_snapshot"
        ],
        score_input_availability_attestation_path=paths[
            "score_input_availability_attestation"
        ],
        v8_policy_path=paths["v8_policy"],
        policy_id=POLICY_ID,
        canonical_trust_bundle=_bundle(),
        expected_sha256=fixture["expected"],
        predecessor_score_input_availability_audit=fixture[
            "baseline_availability_audit"
        ],
    )


def _availability_replay_kwargs(
    fixture: dict[str, Any],
    *,
    predecessor_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = fixture["paths"]
    return {
        "score_input_availability_baseline_snapshot_path": paths[
            "score_input_availability_baseline_snapshot"
        ],
        "score_input_availability_baseline_attestation_path": paths[
            "score_input_availability_baseline_attestation"
        ],
        "score_input_availability_snapshot_path": paths[
            "score_input_availability_snapshot"
        ],
        "score_input_availability_attestation_path": paths[
            "score_input_availability_attestation"
        ],
        "policy_id": POLICY_ID,
        "canonical_trust_bundle": _bundle(),
        "predecessor_score_input_availability_audit": (
            predecessor_audit
            if predecessor_audit is not None
            else fixture["baseline_availability_audit"]
        ),
    }


def _prior_availability_audit(
    fixture: dict[str, Any],
    *,
    prior_asof: str,
    prior_cutoff: str,
    panel_rows: list[dict[str, Any]],
    fact_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    snapshot_path = fixture["paths"]["scoring_panel"].parent / (
        f"prior-availability-{prior_asof}.json"
    )
    attestation_path = fixture["paths"]["scoring_panel"].parent / (
        f"prior-availability-attestation-{prior_asof}.json"
    )
    _write_availability_artifacts(
        snapshot_path=snapshot_path,
        attestation_path=attestation_path,
        panel_rows=panel_rows,
        fact_rows=fact_rows,
        asof=prior_asof,
        cutoff=prior_cutoff,
        evidence_role=PROSPECTIVE_ROLE,
    )
    _, _, audit = validate_transport_score_input_availability_snapshot(
        snapshot_path,
        asof_date=prior_asof,
        expected_panel_rows=panel_rows,
        expected_accepted_fact_rows=fact_rows,
        signal_cutoff_at_utc=prior_cutoff,
        policy_id=POLICY_ID,
        attestation_path=attestation_path,
        expected_attestation_sha256=_sha256(attestation_path),
        bundle=_bundle(),
        predecessor_availability_audit=fixture[
            "baseline_availability_audit"
        ],
    )
    return audit


def _refresh_current_availability(fixture: dict[str, Any]) -> None:
    paths = fixture["paths"]
    _write_availability_artifacts(
        snapshot_path=paths["score_input_availability_snapshot"],
        attestation_path=paths["score_input_availability_attestation"],
        panel_rows=fixture["panel"]["rows"],
        fact_rows=fixture["facts"]["rows"],
        asof=ASOF,
        cutoff=SIGNAL_CUTOFF,
        evidence_role=PROSPECTIVE_ROLE,
    )
    for role in (
        "score_input_availability_snapshot",
        "score_input_availability_attestation",
    ):
        fixture["expected"][role] = _sha256(paths[role])


def _refresh_baseline_availability(fixture: dict[str, Any]) -> None:
    paths = fixture["paths"]
    baseline = fixture["baseline"]
    _write_availability_artifacts(
        snapshot_path=paths["score_input_availability_baseline_snapshot"],
        attestation_path=paths[
            "score_input_availability_baseline_attestation"
        ],
        panel_rows=baseline["panel_rows"],
        fact_rows=baseline["accepted_fact_rows"],
        asof="2026-08-24",
        cutoff=BASELINE_CUTOFF,
        evidence_role=ACTIVATION_BASELINE_ROLE,
    )
    for role in (
        "score_input_availability_baseline_snapshot",
        "score_input_availability_baseline_attestation",
    ):
        fixture["expected"][role] = _sha256(paths[role])
    _, _, audit = validate_transport_score_input_availability_snapshot(
        paths["score_input_availability_baseline_snapshot"],
        asof_date="2026-08-24",
        expected_panel_rows=baseline["panel_rows"],
        expected_accepted_fact_rows=baseline["accepted_fact_rows"],
        signal_cutoff_at_utc=BASELINE_CUTOFF,
        policy_id=POLICY_ID,
        attestation_path=paths[
            "score_input_availability_baseline_attestation"
        ],
        expected_attestation_sha256=fixture["expected"][
            "score_input_availability_baseline_attestation"
        ],
        bundle=_bundle(),
        expected_evidence_role=ACTIVATION_BASELINE_ROLE,
    )
    fixture["baseline_availability_audit"] = audit


def _resign_panel(fixture: dict[str, Any]) -> None:
    panel = fixture["panel"]
    census: dict[str, list[str]] = {}
    for row in panel["rows"]:
        census.setdefault(str(row["asof_date"]), []).append(str(row["ticker"]))
    panel["date_ticker_census"] = {
        score_date: sorted(tickers)
        for score_date, tickers in sorted(census.items())
    }
    panel["rows_sha256"] = canonical_sha256(panel["rows"])
    panel["date_ticker_census_sha256"] = canonical_sha256(
        panel["date_ticker_census"]
    )
    path = fixture["paths"]["scoring_panel"]
    _write_json(path, panel)
    fixture["expected"]["scoring_panel"] = _sha256(path)
    _refresh_current_availability(fixture)


def _resign_facts(fixture: dict[str, Any]) -> None:
    facts = fixture["facts"]
    facts["rows_sha256"] = canonical_sha256(facts["rows"])
    facts["staleness_days_sha256"] = canonical_sha256(facts["staleness_days"])
    path = fixture["paths"]["accepted_facts"]
    _write_json(path, facts)
    fixture["expected"]["accepted_facts"] = _sha256(path)
    _refresh_current_availability(fixture)


def _resign_baseline(fixture: dict[str, Any]) -> None:
    baseline = fixture["baseline"]
    census: dict[str, list[str]] = {}
    source_hashes: dict[str, str] = {}
    for row in baseline["panel_rows"]:
        score_date = str(row["asof_date"])
        census.setdefault(score_date, []).append(str(row["ticker"]))
        source_hashes.setdefault(score_date, str(row["source_score_sha256"]))
    baseline["date_ticker_census"] = {
        score_date: sorted(tickers)
        for score_date, tickers in sorted(census.items())
    }
    baseline["source_score_file_sha256_by_date"] = dict(
        sorted(source_hashes.items())
    )
    baseline["panel_rows_sha256"] = canonical_sha256(baseline["panel_rows"])
    baseline["date_ticker_census_sha256"] = canonical_sha256(
        baseline["date_ticker_census"]
    )
    baseline["source_score_file_sha256_by_date_sha256"] = canonical_sha256(
        baseline["source_score_file_sha256_by_date"]
    )
    baseline["accepted_fact_rows_sha256"] = canonical_sha256(
        baseline["accepted_fact_rows"]
    )
    baseline["accepted_fact_identity_census_sha256"] = canonical_sha256(
        [_fact_identity(row) for row in baseline["accepted_fact_rows"]]
    )
    baseline["staleness_days_sha256"] = canonical_sha256(
        baseline["staleness_days"]
    )
    path = fixture["paths"]["score_replay_baseline"]
    _write_json(path, baseline)
    fixture["expected"]["score_replay_baseline"] = _sha256(path)
    _refresh_baseline_availability(fixture)


def _validate_static_baseline(fixture: dict[str, Any]) -> dict[str, Any]:
    paths = fixture["paths"]
    return validate_transport_score_replay_baseline(
        baseline_path=paths["score_replay_baseline"],
        score_input_availability_baseline_snapshot_path=paths[
            "score_input_availability_baseline_snapshot"
        ],
        score_input_availability_baseline_attestation_path=paths[
            "score_input_availability_baseline_attestation"
        ],
        v8_policy_path=paths["v8_policy"],
        activation_registered_at_utc="2026-08-25T00:00:00Z",
        policy_id=POLICY_ID,
        canonical_trust_bundle=_bundle(),
        expected_sha256={
            role: fixture["expected"][role]
            for role in (
                "score_replay_baseline",
                "score_input_availability_baseline_snapshot",
                "score_input_availability_baseline_attestation",
                "v8_policy",
            )
        },
    )


def _validate_structural_baseline(fixture: dict[str, Any]) -> dict[str, Any]:
    paths = fixture["paths"]
    return validate_transport_score_replay_baseline_structure(
        baseline_path=paths["score_replay_baseline"],
        v8_policy_path=paths["v8_policy"],
        expected_baseline_cutoff_at_utc=BASELINE_CUTOFF,
        expected_sha256={
            role: fixture["expected"][role]
            for role in STRUCTURAL_BASELINE_SOURCE_ROLES
        },
    )


def _validate_structural_replay_inputs(
    fixture: dict[str, Any],
) -> dict[str, Any]:
    paths = fixture["paths"]
    return validate_transport_replay_inputs_structure(
        asof_date=ASOF,
        signal_cutoff_at_utc=SIGNAL_CUTOFF,
        scheduled_append_asof_dates=[ASOF],
        scoring_panel_path=paths["scoring_panel"],
        accepted_facts_path=paths["accepted_facts"],
        score_replay_baseline_path=paths["score_replay_baseline"],
        v8_policy_path=paths["v8_policy"],
        expected_baseline_cutoff_at_utc=BASELINE_CUTOFF,
        expected_sha256={
            role: fixture["expected"][role]
            for role in STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES
        },
    )


def _accepted_fact(
    *,
    candidate_key: str,
    accepted_at: str,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": "CNI",
        "metric_id": "tce_day_rate",
        "value": 1.0,
        "unit": "usd_per_day",
        "period_end": "2025-06-30",
        "filing_date": "2025-08-01",
        "accepted_at": accepted_at,
        "replay_status": "ACCEPTED",
        "candidate_key": candidate_key,
    }
    if reviewed_at is not None:
        row["reviewed_at"] = reviewed_at
    return row


def test_exact_point_in_time_replay_and_model_data_eligibility(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    audit = _validate(fixture)

    assert audit["exact_model_score_replay_pass"] is True
    assert audit["no_post_checkpoint_inputs_pass"] is True
    assert audit["ticker_count"] == 35
    assert audit["panel_date_count"] == 2
    assert audit["panel_date_max"] == ASOF
    eligibility = audit["model_data_eligibility_by_ticker"]
    assert all(
        eligibility[ticker]["model_data_eligible_flag"] == 0
        for ticker in GROUP_TICKERS["oil_tankers"]
    )
    assert all(
        eligibility[ticker]["model_data_exclusion_reason_codes"]
        == ["required_specialized_pack_not_ready"]
        for ticker in GROUP_TICKERS["oil_tankers"]
    )
    assert all(
        eligibility[ticker]["model_data_eligible_flag"] == 1
        for group, tickers in GROUP_TICKERS.items()
        if group != "oil_tankers"
        for ticker in tickers
    )


def test_static_baseline_is_validated_from_same_hash_bound_bytes(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    audit = _validate_static_baseline(fixture)

    assert audit["exact_activation_baseline_pass"] is True
    assert audit["baseline_panel_date_count"] == 1
    assert audit["baseline_panel_row_count"] == 35
    assert audit["full_semantic_policy_validation_pass"] is True
    assert audit["score_input_availability_audit"][
        "market_authority_attested_score_inputs_pass"
    ] is True


def test_unsigned_structural_baseline_is_valid_before_signing_request(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    audit = _validate_structural_baseline(fixture)

    assert audit["schema_version"] == BASELINE_STRUCTURE_AUDIT_SCHEMA
    assert audit["full_semantic_policy_validation_pass"] is True
    assert audit["availability_signing_request_ready"] is True
    assert audit["independent_source_availability_attestation_validated"] is False
    assert audit["capture_ready"] is False


def test_unsigned_structural_baseline_requires_exact_external_cutoff(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    paths = fixture["paths"]

    with pytest.raises(ValueError, match="cutoff differs from expected"):
        validate_transport_score_replay_baseline_structure(
            baseline_path=paths["score_replay_baseline"],
            v8_policy_path=paths["v8_policy"],
            expected_baseline_cutoff_at_utc="2026-08-24T22:59:59Z",
            expected_sha256={
                role: fixture["expected"][role]
                for role in STRUCTURAL_BASELINE_SOURCE_ROLES
            },
        )


def test_unsigned_structural_baseline_rejects_noncanonical_expected_digest(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    paths = fixture["paths"]
    expected = {
        role: fixture["expected"][role]
        for role in STRUCTURAL_BASELINE_SOURCE_ROLES
    }
    expected["v8_policy"] = expected["v8_policy"].upper()

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        validate_transport_score_replay_baseline_structure(
            baseline_path=paths["score_replay_baseline"],
            v8_policy_path=paths["v8_policy"],
            expected_baseline_cutoff_at_utc=BASELINE_CUTOFF,
            expected_sha256=expected,
        )


def test_unsigned_structural_replay_inputs_are_ready_only_for_signing(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    audit = _validate_structural_replay_inputs(fixture)

    assert audit["schema_version"] == REPLAY_INPUT_STRUCTURE_AUDIT_SCHEMA
    assert audit["exact_baseline_prefix_pass"] is True
    assert audit["exact_scheduled_append_pass"] is True
    assert audit["append_only_accepted_facts_pass"] is True
    assert audit["availability_signing_request_ready"] is True
    assert audit["independent_source_availability_attestation_validated"] is False
    assert audit["canonical_score_replay_validated"] is False
    assert audit["capture_ready"] is False


def test_unsigned_structural_replay_inputs_reject_malformed_panel_value(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["panel"]["rows"][-1]["positioning_score"] = "20.0"
    _resign_panel(fixture)

    with pytest.raises(ValueError, match="must be numeric"):
        _validate_structural_replay_inputs(fixture)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("unknown_ticker", "outside frozen policy"),
        ("numeric_string", "must be numeric"),
        ("unknown_metric", "metric is outside frozen recipes"),
    ],
)
def test_static_baseline_rejects_semantically_invalid_rows(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    fixture = _fixture(tmp_path)
    baseline = fixture["baseline"]
    if mutation == "unknown_ticker":
        baseline["panel_rows"][0]["ticker"] = "ZZZZ"
    elif mutation == "numeric_string":
        baseline["panel_rows"][0]["positioning_score"] = "20.0"
    else:
        baseline["accepted_fact_rows"] = [
            {
                "ticker": "CNI",
                "metric_id": "invented_metric",
                "value": 1.0,
                "unit": "ratio",
                "period_end": "2025-06-30",
                "filing_date": "2025-08-01",
                "accepted_at": "2025-08-01T20:00:00Z",
                "replay_status": "ACCEPTED",
                "candidate_key": "invalid-baseline-metric",
            }
        ]
    _resign_baseline(fixture)

    with pytest.raises(ValueError, match=match):
        _validate_static_baseline(fixture)


def test_static_baseline_rejects_noncanonical_historical_policy_ticker(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    policy["historical_calibration_only"]["ksu"] = policy[
        "historical_calibration_only"
    ].pop("KSU")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    fixture["paths"]["v8_policy"] = policy_path
    fixture["expected"]["v8_policy"] = _sha256(policy_path)

    with pytest.raises(ValueError, match="historical ticker identity"):
        _validate_static_baseline(fixture)


def test_multi_failure_model_data_reason_codes_are_canonical() -> None:
    audit = _eligibility_audit(
        {
            "source_rank_ready_flag": 1,
            "source_calibration_eligible_flag": 0,
            "group_cross_section_ready_flag": 1,
            "group_specialized_ready_flag": 0,
            "v8_calibration_eligible_flag": 0,
        }
    )

    assert audit == {
        "model_data_eligible_flag": 0,
        "model_data_exclusion_reason_codes": [
            "required_specialized_pack_not_ready",
            "source_calibration_ineligible",
        ],
    }


def test_coherently_resigned_published_score_mutation_fails_replay(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    score_path = fixture["paths"]["canonical_v8_score"]
    with score_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    target = next(row for row in rows if row["asof_date"] == ASOF)
    target["v8_final_score"] = str(float(target["v8_final_score"]) + 1.0)
    _write_scores(score_path, rows)
    fixture["expected"]["canonical_v8_score"] = _sha256(score_path)

    with pytest.raises(ValueError, match="replay changed v8_final_score"):
        _validate(fixture)


def test_duplicate_governed_horizon_row_fails_even_when_resigned(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    panel_path = fixture["paths"]["scoring_panel"]
    panel = fixture["panel"]
    panel["rows"].append(dict(panel["rows"][-1]))
    panel["rows_sha256"] = canonical_sha256(panel["rows"])
    _write_json(panel_path, panel)
    fixture["expected"]["scoring_panel"] = _sha256(panel_path)

    with pytest.raises(ValueError, match="ambiguous duplicate 63-session"):
        _validate(fixture)


def test_mixed_source_file_hashes_within_one_date_fail(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    panel_path = fixture["paths"]["scoring_panel"]
    panel = fixture["panel"]
    target = next(row for row in panel["rows"] if row["asof_date"] == ASOF)
    target["source_score_sha256"] = "f" * 64
    panel["rows_sha256"] = canonical_sha256(panel["rows"])
    _write_json(panel_path, panel)
    fixture["expected"]["scoring_panel"] = _sha256(panel_path)

    with pytest.raises(ValueError, match="mixes source-score identities"):
        _validate(fixture)


def test_one_source_file_hash_cannot_be_reused_across_dates(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    panel_path = fixture["paths"]["scoring_panel"]
    panel = fixture["panel"]
    capture_hash = next(
        row["source_score_sha256"]
        for row in panel["rows"]
        if row["asof_date"] == ASOF
    )
    for row in panel["rows"]:
        if row["asof_date"] != ASOF:
            row["source_score_sha256"] = capture_hash
    panel["rows_sha256"] = canonical_sha256(panel["rows"])
    _write_json(panel_path, panel)
    fixture["expected"]["scoring_panel"] = _sha256(panel_path)

    with pytest.raises(ValueError, match="reuses one source-score identity across dates"):
        _validate(fixture)


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_panel_json_flags_reject_noncanonical_types(
    tmp_path: Path,
    value: object,
) -> None:
    fixture = _fixture(tmp_path)
    panel_path = fixture["paths"]["scoring_panel"]
    panel = fixture["panel"]
    panel["rows"][0]["rank_ready_flag"] = value
    panel["rows_sha256"] = canonical_sha256(panel["rows"])
    _write_json(panel_path, panel)
    fixture["expected"]["scoring_panel"] = _sha256(panel_path)

    with pytest.raises(ValueError, match="canonical integer"):
        _validate(fixture)


def test_policy_integer_controls_reject_numeric_strings(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    policy["cohorts"]["surface_freight_core"]["groups"][
        "rail_networks"
    ]["minimum_cross_section"] = "4"
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    fixture["paths"]["v8_policy"] = policy_path
    fixture["expected"]["v8_policy"] = _sha256(policy_path)

    with pytest.raises(ValueError, match="canonical integer"):
        _validate(fixture)


def test_fact_staleness_rejects_numeric_strings(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    facts_path = fixture["paths"]["accepted_facts"]
    facts = fixture["facts"]
    metric_id = sorted(facts["staleness_days"])[0]
    facts["staleness_days"][metric_id] = "550"
    facts["staleness_days_sha256"] = canonical_sha256(facts["staleness_days"])
    _write_json(facts_path, facts)
    fixture["expected"]["accepted_facts"] = _sha256(facts_path)

    with pytest.raises(ValueError, match="canonical integer"):
        _validate(fixture)


def test_post_checkpoint_panel_row_fails_before_score_use(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    panel_path = fixture["paths"]["scoring_panel"]
    panel = fixture["panel"]
    panel["rows"][0]["asof_date"] = "2026-08-27"
    panel["rows_sha256"] = canonical_sha256(panel["rows"])
    _write_json(panel_path, panel)
    fixture["expected"]["scoring_panel"] = _sha256(panel_path)

    with pytest.raises(ValueError, match="post-checkpoint"):
        _validate(fixture)


def test_post_checkpoint_accepted_fact_fails_before_replay(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    facts_path = fixture["paths"]["accepted_facts"]
    facts = fixture["facts"]
    metric_id = sorted(facts["staleness_days"])[0]
    facts["rows"] = [
        {
            "ticker": "CNI",
            "metric_id": metric_id,
            "value": 1.0,
            "unit": "ratio",
            "period_end": "2026-08-27",
            "filing_date": "2026-08-27",
            "accepted_at": "2026-08-27T00:00:00Z",
            "replay_status": "ACCEPTED",
            "candidate_key": "future-fact",
        }
    ]
    facts["rows_sha256"] = canonical_sha256(facts["rows"])
    _write_json(facts_path, facts)
    fixture["expected"]["accepted_facts"] = _sha256(facts_path)

    with pytest.raises(ValueError, match="post-checkpoint"):
        _validate(fixture)


def test_historical_rfc3339_fact_and_review_timestamps_pass_replay(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    facts = fixture["facts"]
    facts["rows"] = [
        {
            "ticker": "CNI",
            "metric_id": "tce_day_rate",
            "value": 1.0,
            "unit": "usd_per_day",
            "period_end": "2025-06-30",
            "filing_date": "2025-08-01",
            "accepted_at": "2025-08-01T20:00:00.000Z",
            "reviewed_at": "2025-08-02T00:00:00+00:00",
            "replay_status": "ACCEPTED",
            "candidate_key": "historical-irrelevant-group-fact",
        }
    ]
    baseline = fixture["baseline"]
    baseline["accepted_fact_rows"] = list(facts["rows"])
    _resign_baseline(fixture)
    _resign_facts(fixture)

    audit = _validate(fixture)

    assert audit["exact_model_score_replay_pass"] is True


def test_post_checkpoint_review_timestamp_fails_before_replay(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    facts_path = fixture["paths"]["accepted_facts"]
    facts = fixture["facts"]
    facts["rows"] = [
        {
            "ticker": "CNI",
            "metric_id": "tce_day_rate",
            "value": 1.0,
            "unit": "usd_per_day",
            "period_end": "2025-06-30",
            "filing_date": "2025-08-01",
            "accepted_at": "2025-08-01T20:00:00Z",
            "reviewed_at": "2026-08-27T00:00:00Z",
            "replay_status": "ACCEPTED",
            "candidate_key": "post-checkpoint-review",
        }
    ]
    facts["rows_sha256"] = canonical_sha256(facts["rows"])
    _write_json(facts_path, facts)
    fixture["expected"]["accepted_facts"] = _sha256(facts_path)

    with pytest.raises(ValueError, match="post-signal information"):
        _validate(fixture)


def test_semantic_parser_uses_the_exact_hash_bound_bytes(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    panel_path = fixture["paths"]["scoring_panel"]
    panel = fixture["panel"]
    panel["rows"][0]["positioning_score"] = 99.0
    panel["rows_sha256"] = canonical_sha256(panel["rows"])
    _write_json(panel_path, panel)

    with pytest.raises(ValueError, match="archived bytes differ"):
        _validate(fixture)


@pytest.mark.parametrize("mutation", ["omit", "change", "reorder"])
def test_frozen_baseline_panel_prefix_cannot_be_coherently_rewritten(
    tmp_path: Path,
    mutation: str,
) -> None:
    fixture = _fixture(tmp_path)
    rows = fixture["panel"]["rows"]
    if mutation == "omit":
        del rows[0]
    elif mutation == "change":
        rows[0]["positioning_score"] = 99.0
    else:
        rows[0], rows[1] = rows[1], rows[0]
    _resign_panel(fixture)

    with pytest.raises(ValueError, match="availability predecessor prefix changed"):
        _validate(fixture)


def test_frozen_baseline_source_score_hash_cannot_change(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    for row in fixture["panel"]["rows"][:35]:
        row["source_score_sha256"] = "f" * 64
    _resign_panel(fixture)

    with pytest.raises(ValueError, match="availability predecessor prefix changed"):
        _validate(fixture)


def test_off_schedule_panel_date_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    rows = fixture["panel"]["rows"]
    extra_rows = []
    for row in rows[:35]:
        extra = dict(row)
        extra["asof_date"] = "2026-08-25"
        extra["source_score_sha256"] = hashlib.sha256(
            b"scoring-features|2026-08-25"
        ).hexdigest()
        extra_rows.append(extra)
    fixture["panel"]["rows"] = rows[:35] + extra_rows + rows[35:]
    _resign_panel(fixture)

    with pytest.raises(ValueError, match="omitted or added a scheduled score date"):
        _validate(fixture)


def test_scheduled_panel_row_reordering_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    rows = fixture["panel"]["rows"]
    rows[35], rows[36] = rows[36], rows[35]
    _resign_panel(fixture)

    with pytest.raises(ValueError, match="missing, extra, or reordered"):
        _validate(fixture)


def test_baseline_accepted_fact_mutation_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    baseline_fact = _accepted_fact(
        candidate_key="baseline-fact",
        accepted_at="2025-08-01T20:00:00Z",
        reviewed_at="2026-08-21T23:00:00Z",
    )
    baseline = fixture["baseline"]
    baseline["accepted_fact_rows"] = [baseline_fact]
    baseline["accepted_fact_rows_sha256"] = canonical_sha256([baseline_fact])
    baseline["accepted_fact_identity_census_sha256"] = canonical_sha256(
        [_fact_identity(baseline_fact)]
    )
    _resign_baseline(fixture)
    fixture["facts"]["rows"] = [dict(baseline_fact)]
    fixture["facts"]["rows"][0]["value"] = 2.0
    _resign_facts(fixture)

    with pytest.raises(ValueError, match="availability predecessor prefix changed"):
        _validate(fixture)


def test_same_day_post_signal_fact_timestamp_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["facts"]["rows"] = [
        _accepted_fact(
            candidate_key="same-day-post-close",
            accepted_at="2026-08-26T21:00:00.001Z",
        )
    ]
    _resign_facts(fixture)

    with pytest.raises(ValueError, match="post-signal information"):
        _validate(fixture)


def test_newly_appended_fact_cannot_be_backdated_before_baseline(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["facts"]["rows"] = [
        _accepted_fact(
            candidate_key="omitted-before-baseline",
            accepted_at="2025-08-01T20:00:00Z",
            reviewed_at="2026-08-21T23:00:00Z",
        )
    ]
    _resign_facts(fixture)

    with pytest.raises(ValueError, match=r"backdated at/before .*cutoff"):
        _validate(fixture)


def test_valid_integer_staleness_drift_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    metric_id = sorted(fixture["facts"]["staleness_days"])[0]
    fixture["facts"]["staleness_days"][metric_id] = 549
    _resign_facts(fixture)

    with pytest.raises(ValueError, match="staleness changed after activation"):
        _validate(fixture)


def test_later_capture_rejects_fact_backdated_before_predecessor_cutoff(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    prior_asof = "2026-08-25"
    rows = fixture["panel"]["rows"]
    prior_rows: list[dict[str, Any]] = []
    for row in rows[35:]:
        prior = dict(row)
        prior["asof_date"] = prior_asof
        prior["source_score_sha256"] = hashlib.sha256(
            b"scoring-features|2026-08-25"
        ).hexdigest()
        prior_rows.append(prior)
    fixture["panel"]["rows"] = rows[:35] + prior_rows + rows[35:]
    _resign_panel(fixture)
    prior_panel_rows = fixture["panel"]["rows"][:70]
    prior_cutoff = "2026-08-25T21:00:00Z"
    prior_availability = _prior_availability_audit(
        fixture,
        prior_asof=prior_asof,
        prior_cutoff=prior_cutoff,
        panel_rows=prior_panel_rows,
        fact_rows=[],
    )
    predecessor = {
        "schema_version": AUDIT_SCHEMA,
        "score_formula_id": SCORE_FORMULA_ID,
        "asof_date": prior_asof,
        "signal_cutoff_at_utc": prior_cutoff,
        "scheduled_append_asof_dates": [prior_asof],
        "scheduled_append_asof_dates_sha256": canonical_sha256([prior_asof]),
        "panel_row_count": len(prior_panel_rows),
        "full_panel_rows_sha256": canonical_sha256(prior_panel_rows),
        "accepted_fact_row_count": 0,
        "full_accepted_fact_rows_sha256": canonical_sha256([]),
        "score_input_availability_audit": prior_availability,
    }
    fixture["facts"]["rows"] = [
        _accepted_fact(
            candidate_key="late-ledger-insertion",
            accepted_at="2025-08-01T20:00:00Z",
            reviewed_at="2026-08-25T20:00:00Z",
        )
    ]
    _resign_facts(fixture)
    paths = fixture["paths"]

    with pytest.raises(ValueError, match=r"backdated at/before .*cutoff"):
        validate_and_replay_transport_scores(
            asof_date=ASOF,
            signal_cutoff_at_utc=SIGNAL_CUTOFF,
            scheduled_append_asof_dates=[prior_asof, ASOF],
            score_path=paths["canonical_v8_score"],
            scoring_panel_path=paths["scoring_panel"],
            accepted_facts_path=paths["accepted_facts"],
            score_replay_baseline_path=paths["score_replay_baseline"],
            v8_policy_path=paths["v8_policy"],
            expected_sha256=fixture["expected"],
            predecessor_replay_audit=predecessor,
            **_availability_replay_kwargs(
                fixture,
                predecessor_audit=prior_availability,
            ),
        )


def test_later_capture_rejects_changed_prior_scheduled_panel_prefix(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    prior_asof = "2026-08-25"
    rows = fixture["panel"]["rows"]
    prior_rows: list[dict[str, Any]] = []
    for row in rows[35:]:
        prior = dict(row)
        prior["asof_date"] = prior_asof
        prior["source_score_sha256"] = hashlib.sha256(
            b"scoring-features|2026-08-25"
        ).hexdigest()
        prior_rows.append(prior)
    fixture["panel"]["rows"] = rows[:35] + prior_rows + rows[35:]
    _resign_panel(fixture)
    prior_panel_rows = fixture["panel"]["rows"][:70]
    prior_cutoff = "2026-08-25T21:00:00Z"
    prior_availability = _prior_availability_audit(
        fixture,
        prior_asof=prior_asof,
        prior_cutoff=prior_cutoff,
        panel_rows=prior_panel_rows,
        fact_rows=[],
    )
    predecessor = {
        "schema_version": AUDIT_SCHEMA,
        "score_formula_id": SCORE_FORMULA_ID,
        "asof_date": prior_asof,
        "signal_cutoff_at_utc": prior_cutoff,
        "scheduled_append_asof_dates": [prior_asof],
        "scheduled_append_asof_dates_sha256": canonical_sha256([prior_asof]),
        "panel_row_count": len(prior_panel_rows),
        "full_panel_rows_sha256": canonical_sha256(prior_panel_rows),
        "accepted_fact_row_count": 0,
        "full_accepted_fact_rows_sha256": canonical_sha256([]),
        "score_input_availability_audit": prior_availability,
    }
    fixture["panel"]["rows"][35]["positioning_score"] = 99.0
    _resign_panel(fixture)
    paths = fixture["paths"]

    with pytest.raises(ValueError, match="availability predecessor prefix changed"):
        validate_and_replay_transport_scores(
            asof_date=ASOF,
            signal_cutoff_at_utc=SIGNAL_CUTOFF,
            scheduled_append_asof_dates=[prior_asof, ASOF],
            score_path=paths["canonical_v8_score"],
            scoring_panel_path=paths["scoring_panel"],
            accepted_facts_path=paths["accepted_facts"],
            score_replay_baseline_path=paths["score_replay_baseline"],
            v8_policy_path=paths["v8_policy"],
            expected_sha256=fixture["expected"],
            predecessor_replay_audit=predecessor,
            **_availability_replay_kwargs(
                fixture,
                predecessor_audit=prior_availability,
            ),
        )


@pytest.mark.parametrize(
    "cutoff",
    [
        True,
        "2026-08-26",
        "2026-08-26 21:00:00Z",
        "2026-08-26T21:00:00.1234567Z",
        "2026-08-26T21:00:00-05:00",
    ],
)
def test_signal_cutoff_requires_exact_utc_timestamp(
    tmp_path: Path,
    cutoff: object,
) -> None:
    fixture = _fixture(tmp_path)
    paths = fixture["paths"]

    with pytest.raises(ValueError, match="exact RFC3339 UTC"):
        validate_and_replay_transport_scores(
            asof_date=ASOF,
            signal_cutoff_at_utc=cutoff,  # type: ignore[arg-type]
            scheduled_append_asof_dates=[ASOF],
            score_path=paths["canonical_v8_score"],
            scoring_panel_path=paths["scoring_panel"],
            accepted_facts_path=paths["accepted_facts"],
            score_replay_baseline_path=paths["score_replay_baseline"],
            v8_policy_path=paths["v8_policy"],
            expected_sha256=fixture["expected"],
            **_availability_replay_kwargs(fixture),
        )
