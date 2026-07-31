#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.machinery.lifecycle_policy import (  # noqa: E402
    CANDIDATE_FIELDS,
    HARD_EVENT_FIELDS,
    POLICY_VERSION,
    REVENUE_POLICY_FIELDS,
    REVENUE_REVIEW_REQUIRED,
    REVIEW_REQUIRED,
    TRANSITION_FIELDS,
    file_sha256,
    generate_lifecycle_candidates,
    load_lifecycle_policy,
    parser_hard_event_candidates,
    validate_lifecycle_policy,
)
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate review-only machinery lifecycle transition candidates "
            "from point-in-time financial evidence."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _transition_review_row(
    candidate: dict[str, str],
    *,
    sequence: int,
) -> dict[str, str]:
    ticker = candidate["ticker"]
    return {
        "transition_id": (
            f"{POLICY_VERSION}_{ticker}_{candidate['asof_date']}_{sequence:03d}"
        ),
        "ticker": ticker,
        "from_class": candidate["current_lifecycle_class"],
        "to_class": candidate["suggested_lifecycle_class"],
        "valid_from": "",
        "evidence_asof": candidate["asof_date"],
        "evidence_artifact": candidate["evidence_artifact"],
        "evidence_sha256": candidate["evidence_sha256"],
        "decision_status": REVIEW_REQUIRED,
        "decision_reason": candidate["candidate_reasons"],
        "reviewer": "",
        "reviewed_at": "",
        "policy_version": POLICY_VERSION,
        "record_sha256": "",
    }


def _revenue_review_row(candidate: dict[str, str]) -> dict[str, str]:
    return {
        "ticker": candidate["ticker"],
        "revenue_classification": REVENUE_REVIEW_REQUIRED,
        "valid_from": "",
        "evidence_artifact": candidate["evidence_artifact"],
        "evidence_sha256": candidate["evidence_sha256"],
        "decision_status": REVIEW_REQUIRED,
        "decision_reason": "confirm_commercial_customer_revenue",
        "reviewer": "",
        "reviewed_at": "",
        "policy_version": POLICY_VERSION,
        "record_sha256": "",
    }


def _event_available_date(event: dict[str, Any]) -> str:
    raw = str(event.get("accepted_at") or event.get("filing_date") or "")
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    digits = "".join(character for character in raw if character.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    filing_date = str(event.get("filing_date") or "")
    if len(filing_date) >= 10:
        return filing_date[:10]
    raise ValueError(
        f"{event.get('ticker')}: parser hard-event evidence has no valid date"
    )


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=base_dir,
        )
    )
    output_root = resolve_path(
        cfg_get(
            config,
            "machinery_lifecycle.output_root",
            "../../output/industrials/machinery/lifecycle",
        ),
        base_dir=base_dir,
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else output_root / asof
    )
    policy = load_lifecycle_policy(config, config_path=config_path)
    validation = validate_lifecycle_policy(policy)
    if validation["acceptance"] != "PASS":
        raise ValueError(
            "Machinery lifecycle ledgers are invalid: "
            + ";".join(validation["issues"])
        )
    with connect(
        db_path,
        timeout_sec=float(
            cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)
        ),
    ) as conn:
        candidates = generate_lifecycle_candidates(
            conn,
            asof=asof,
            policy=policy,
        )
        parser_hard_events = parser_hard_event_candidates(
            conn,
            asof=asof,
        )

    generated_at = datetime.now(timezone.utc).isoformat()
    evidence_dir = output_dir / "evidence"
    for candidate in candidates:
        evidence_path = (
            evidence_dir
            / f"{candidate['ticker']}_{asof}_lifecycle_evidence.json"
        )
        evidence: dict[str, Any] = {
            "artifact_family": "machinery_lifecycle_candidate_evidence",
            "policy_version": policy.policy_version,
            "generated_at": generated_at,
            "asof_date": asof,
            "ticker": candidate["ticker"],
            "candidate": {
                key: value
                for key, value in candidate.items()
                if key not in {"evidence_artifact", "evidence_sha256"}
            },
            "policy_source_sha256": validation["source_sha256"],
            "point_in_time_contract": {
                "financial_rows": (
                    "latest row available by distinct fiscal_period_end "
                    "with asof_date <= candidate asof"
                ),
                "transition_application": (
                    "accepted transitions with valid_from <= candidate asof"
                ),
                "human_ratification_required": True,
                "calibration_cohort_changed": False,
            },
        }
        write_text_atomic(
            evidence_path,
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        )
        candidate["evidence_artifact"] = str(evidence_path.resolve())
        candidate["evidence_sha256"] = file_sha256(evidence_path)

    hard_event_review_rows: list[dict[str, str]] = []
    for event in parser_hard_events:
        ticker = str(event.get("ticker") or "").upper()
        event_date = _event_available_date(event)
        event_evidence_path = (
            evidence_dir
            / f"{ticker}_{event_date}_going_concern_evidence.json"
        )
        event_payload = {
            "artifact_family": "machinery_lifecycle_hard_event_evidence",
            "policy_version": policy.policy_version,
            "generated_at": generated_at,
            "asof_date": asof,
            "event_type": "going_concern",
            "human_ratification_required": True,
            "parser_evidence": event,
        }
        write_text_atomic(
            event_evidence_path,
            json.dumps(event_payload, indent=2, sort_keys=True) + "\n",
        )
        hard_event_review_rows.append(
            {
                "event_id": (
                    f"{POLICY_VERSION}_{ticker}_going_concern_{event_date}"
                ),
                "ticker": ticker,
                "event_type": "going_concern",
                "valid_from": "",
                "valid_to": "",
                "evidence_artifact": str(event_evidence_path.resolve()),
                "evidence_sha256": file_sha256(event_evidence_path),
                "decision_status": REVIEW_REQUIRED,
                "decision_reason": str(event.get("status_reason") or ""),
                "reviewer": "",
                "reviewed_at": "",
                "policy_version": POLICY_VERSION,
                "record_sha256": "",
            }
        )

    candidate_path = output_dir / "machinery_lifecycle_candidates.csv"
    review_path = output_dir / "machinery_lifecycle_transition_review.csv"
    revenue_review_path = (
        output_dir / "machinery_lifecycle_revenue_review.csv"
    )
    hard_event_review_path = (
        output_dir / "machinery_lifecycle_hard_event_review.csv"
    )
    manifest_path = output_dir / "machinery_lifecycle_candidates_manifest.json"
    write_csv_atomic(candidate_path, CANDIDATE_FIELDS, candidates)
    review_rows = [
        _transition_review_row(candidate, sequence=index)
        for index, candidate in enumerate(candidates, start=1)
        if candidate["suggested_lifecycle_class"]
        != candidate["current_lifecycle_class"]
    ]
    write_csv_atomic(review_path, TRANSITION_FIELDS, review_rows)
    revenue_review_rows = [
        _revenue_review_row(candidate)
        for candidate in candidates
        if candidate["suggested_lifecycle_class"]
        != candidate["current_lifecycle_class"]
        and candidate["revenue_classification"] == REVENUE_REVIEW_REQUIRED
    ]
    write_csv_atomic(
        revenue_review_path,
        REVENUE_POLICY_FIELDS,
        revenue_review_rows,
    )
    write_csv_atomic(
        hard_event_review_path,
        HARD_EVENT_FIELDS,
        hard_event_review_rows,
    )
    status_counts: dict[str, int] = {}
    for candidate in candidates:
        status = candidate["candidate_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    precommercial_after_candidates = sum(
        candidate["suggested_lifecycle_class"] == "pre_commercial"
        and candidate["calibration_cohort"]
        == "development_stage_emerging_machinery"
        for candidate in candidates
    )
    calibration_floor = (
        policy.thresholds.minimum_precommercial_calibration_members
    )
    cohort_pressure_warning = (
        "lifecycle_does_not_change_calibration_cohort;future_recuration_"
        f"would_leave_{precommercial_after_candidates}_precommercial_members_"
        f"below_floor_{calibration_floor}"
        if precommercial_after_candidates < calibration_floor
        else ""
    )
    summary = {
        "acceptance": "PASS",
        "artifact_family": "machinery_lifecycle_candidates",
        "policy_version": policy.policy_version,
        "generated_at": generated_at,
        "asof_date": asof,
        "ticker_count": len(candidates),
        "transition_review_count": len(review_rows),
        "status_counts": status_counts,
        "candidate_csv": str(candidate_path.resolve()),
        "candidate_csv_sha256": file_sha256(candidate_path),
        "transition_review_csv": str(review_path.resolve()),
        "transition_review_csv_sha256": file_sha256(review_path),
        "revenue_review_count": len(revenue_review_rows),
        "revenue_review_csv": str(revenue_review_path.resolve()),
        "revenue_review_csv_sha256": file_sha256(revenue_review_path),
        "hard_event_review_count": len(hard_event_review_rows),
        "hard_event_review_csv": str(hard_event_review_path.resolve()),
        "hard_event_review_csv_sha256": file_sha256(
            hard_event_review_path
        ),
        "precommercial_dev_cohort_after_candidate_count": (
            precommercial_after_candidates
        ),
        "calibration_cohort_floor": calibration_floor,
        "cohort_pressure_warning": cohort_pressure_warning,
        "policy_validation": validation,
        "production_policy_changed": False,
    }
    write_text_atomic(
        manifest_path,
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
