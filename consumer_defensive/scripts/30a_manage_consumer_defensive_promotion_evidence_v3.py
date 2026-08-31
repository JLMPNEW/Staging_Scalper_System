#!/usr/bin/env python3
"""Create immutable preregistration, anchor, and fresh-evidence artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.promotion_artifacts_v3 import (  # noqa: E402
    publish_immutable_json,
)
from consumer_defensive.core.promotion_engine_v3 import load_framework  # noqa: E402
from consumer_defensive.core.promotion_evidence_v3 import (  # noqa: E402
    build_fresh_evidence_manifest,
    build_registration_anchor,
    build_review_preregistration,
)


DEFAULT_FRAMEWORK = (
    ROOT / "consumer_defensive/data/consumer_defensive_promotion_framework_v3.yaml"
)


def _safe_file(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"{label} is missing or unsafe: {resolved}")
    return resolved


def _json(path: Path, *, label: str) -> Any:
    resolved = _safe_file(path, label=label)
    return json.loads(resolved.read_text(encoding="utf-8"))


def _object(path: Path, *, label: str) -> dict[str, Any]:
    payload = _json(path, label=label)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must contain a JSON object")
    return dict(payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _safe_file(path, label="methodology file").open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _methodology(values: list[str]) -> dict[str, str]:
    output: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError("--methodology-file must use canonical_name=path")
        name, path = raw.split("=", 1)
        if not name.strip() or name != name.strip() or name in output:
            raise ValueError("methodology names must be unique and canonical")
        output[name] = _sha256_file(Path(path))
    if not output:
        raise ValueError("at least one --methodology-file is required")
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="freeze a future review plan")
    plan.add_argument("--framework", type=Path, default=DEFAULT_FRAMEWORK)
    plan.add_argument("--review-id", required=True)
    plan.add_argument("--registered-at-utc", required=True)
    plan.add_argument("--fresh-start-exclusive", required=True)
    plan.add_argument("--scheduled-decision-asof", required=True)
    plan.add_argument("--eligible-return-dates-json", type=Path, required=True)
    plan.add_argument("--eligible-outer-oos-dates-json", type=Path, required=True)
    plan.add_argument("--minimum-new-paired-observations", type=int, default=1)
    plan.add_argument("--methodology-file", action="append", default=[])
    plan.add_argument("--previous-decision", type=Path, required=True)
    plan.add_argument("--previous-promotion-input", type=Path, required=True)
    plan.add_argument("--trusted-previous-decision-sha256", required=True)
    plan.add_argument("--output", type=Path, required=True)

    anchor = subparsers.add_parser("anchor", help="create the separately pinned anchor")
    anchor.add_argument("--framework", type=Path, default=DEFAULT_FRAMEWORK)
    anchor.add_argument("--preregistration", type=Path, required=True)
    anchor.add_argument("--anchor-created-at-utc", required=True)
    anchor.add_argument("--registration-authority", required=True)
    anchor.add_argument("--anchor-id", required=True)
    anchor.add_argument("--output", type=Path, required=True)

    manifest = subparsers.add_parser(
        "manifest", help="prove exact fresh path/outer-OOS extension"
    )
    manifest.add_argument("--framework", type=Path, default=DEFAULT_FRAMEWORK)
    manifest.add_argument("--preregistration", type=Path, required=True)
    manifest.add_argument("--registration-anchor", type=Path, required=True)
    manifest.add_argument("--trusted-registration-anchor-sha256", required=True)
    manifest.add_argument("--previous-decision", type=Path, required=True)
    manifest.add_argument("--previous-promotion-input", type=Path, required=True)
    manifest.add_argument("--current-promotion-input", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    framework = load_framework(args.framework)
    if args.command == "plan":
        eligible_returns = _json(
            args.eligible_return_dates_json, label="eligible return dates"
        )
        if not isinstance(eligible_returns, list):
            raise ValueError("eligible return dates JSON must be a list")
        eligible_outer = _object(
            args.eligible_outer_oos_dates_json, label="eligible outer-OOS dates"
        )
        minimum = {
            str(horizon): args.minimum_new_paired_observations
            for horizon in (21, 63, 126)
        }
        payload = build_review_preregistration(
            review_id=args.review_id,
            registered_at_utc=args.registered_at_utc,
            fresh_start_exclusive=args.fresh_start_exclusive,
            scheduled_decision_asof=args.scheduled_decision_asof,
            eligible_return_dates=eligible_returns,
            eligible_outer_oos_dates_by_cohort_horizon=eligible_outer,
            minimum_new_paired_observations_by_horizon=minimum,
            methodology_file_sha256s=_methodology(args.methodology_file),
            framework=framework,
            previous_decision=_object(
                args.previous_decision, label="previous decision"
            ),
            previous_promotion_input=_object(
                args.previous_promotion_input, label="previous promotion input"
            ),
            trusted_previous_decision_sha256=args.trusted_previous_decision_sha256,
        )
    elif args.command == "anchor":
        payload = build_registration_anchor(
            preregistration=_object(args.preregistration, label="preregistration"),
            framework=framework,
            anchor_created_at_utc=args.anchor_created_at_utc,
            registration_authority=args.registration_authority,
            anchor_id=args.anchor_id,
        )
    else:
        payload = build_fresh_evidence_manifest(
            preregistration=_object(args.preregistration, label="preregistration"),
            registration_anchor=_object(
                args.registration_anchor, label="registration anchor"
            ),
            trusted_anchor_sha256=args.trusted_registration_anchor_sha256,
            previous_decision=_object(
                args.previous_decision, label="previous decision"
            ),
            previous_promotion_input=_object(
                args.previous_promotion_input, label="previous promotion input"
            ),
            current_promotion_input=_object(
                args.current_promotion_input, label="current promotion input"
            ),
            framework=framework,
        )
    publish_immutable_json(args.output, payload)
    print(
        json.dumps(
            {
                "status": "PASS",
                "command": args.command,
                "output": str(args.output.expanduser().resolve()),
                "payload_sha256": payload["payload_sha256"],
                "calibration_write_performed": False,
                "portfolio_write_performed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
