#!/usr/bin/env python3
"""Build the sealed expectations-monitor eligibility overlay consumed by Stage 3."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    manifest_acceptance_value,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    monitor_output_subdir,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_monitor_eligibility_overlay")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OVERLAY_FIELDS = [
    "ticker",
    "run_as_of",
    "source_pipeline",
    "stage1_investable_eligible",
    "monitor_state_present",
    "monitor_investable_eligible",
    "internal_state",
    "action_state",
    "market_data_status",
    "blocking_flags_json",
    "optimizer_entry_eligible",
    "optimizer_retention_eligible",
    "policy_reason",
    "state_input_digest",
]
VALIDATION_FIELDS = ["check", "status", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _flag(value: object) -> int:
    return int(str(value or "").strip().lower() in {"1", "1.0", "true"})


def _keyed(
    rows: list[dict[str, str]], *, label: str
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker or ticker in result:
            raise ValueError(f"{label} has blank or duplicate ticker: {ticker!r}")
        result[ticker] = row
    return result


def _policy(config: dict[str, Any]) -> dict[str, Any]:
    raw = cfg_get(config, "optimizer.monitor_entry_policy", {})
    if not isinstance(raw, dict):
        raise ValueError("optimizer.monitor_entry_policy must be a mapping")
    if raw.get("policy_version") != "monitor_optimizer_entry_v1":
        raise ValueError("optimizer.monitor_entry_policy policy_version is not frozen")
    if raw.get("enabled_in_production") is not True:
        raise ValueError("optimizer.monitor_entry_policy must be enabled_in_production")
    entry_states = [str(value).strip() for value in raw.get("entry_states", [])]
    retention_states = [
        str(value).strip() for value in raw.get("retention_states", [])
    ]
    if set(entry_states) != {"green", "stable"}:
        raise ValueError("optimizer entry_states must be exactly green and stable")
    if set(retention_states) != {"green", "stable", "watch"}:
        raise ValueError(
            "optimizer retention_states must be exactly green, stable, and watch"
        )
    raw["entry_states"] = entry_states
    raw["retention_states"] = retention_states
    raw["blocking_escalation_flags"] = sorted(
        {str(value).strip() for value in raw.get("blocking_escalation_flags", [])}
        - {""}
    )
    coverage_floor = float(raw.get("minimum_investable_state_coverage_fraction", 1.0))
    if not 0.0 <= coverage_floor <= 1.0:
        raise ValueError("minimum_investable_state_coverage_fraction must be in [0,1]")
    raw["minimum_investable_state_coverage_fraction"] = coverage_floor
    return raw


def build_overlay_rows(
    score_rows: list[dict[str, str]],
    state_rows: list[dict[str, str]],
    *,
    run_as_of: str,
    policy: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    scores = _keyed(score_rows, label="stocks_scores")
    states = _keyed(state_rows, label="expectations_state")
    entry_states = set(policy["entry_states"])
    retention_states = set(policy["retention_states"])
    blocking_flags = set(policy["blocking_escalation_flags"])
    rows: list[dict[str, Any]] = []
    invalid_state_dates: list[str] = []
    investability_mismatches: list[str] = []
    malformed_states: list[str] = []

    for ticker, score in sorted(scores.items()):
        stage1_investable = _flag(score.get("investable_eligible"))
        state = states.get(ticker)
        state_present = int(state is not None)
        monitor_investable = _flag(state.get("investable_eligible")) if state else 0
        internal_state = str(state.get("internal_state", "")).strip() if state else ""
        action_state = str(state.get("action_state", "")).strip() if state else ""
        market_status = str(state.get("market_data_status", "")).strip() if state else ""
        flags: list[str] = []
        if state:
            if str(state.get("run_as_of", "")).strip() != run_as_of:
                invalid_state_dates.append(ticker)
            if monitor_investable != stage1_investable:
                investability_mismatches.append(ticker)
            try:
                decoded = json.loads(str(state.get("escalation_flags_json", "[]")))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{ticker} escalation_flags_json is invalid") from exc
            if not isinstance(decoded, list) or any(
                not isinstance(value, str) for value in decoded
            ):
                raise ValueError(f"{ticker} escalation_flags_json must be a string list")
            flags = sorted(set(decoded))
            if internal_state not in {"green", "stable", "watch", "deteriorating", "broken"}:
                malformed_states.append(ticker)

        active_blockers = sorted(set(flags) & blocking_flags)
        if not stage1_investable:
            entry_eligible = 0
            reason = "stage1_ineligible"
        elif state is None:
            entry_eligible = 0
            reason = "missing_monitor_state"
        elif monitor_investable != stage1_investable:
            entry_eligible = 0
            reason = "investability_mismatch"
        elif market_status != "current":
            entry_eligible = 0
            reason = f"market_data_not_current:{market_status or 'missing'}"
        elif internal_state not in entry_states:
            entry_eligible = 0
            reason = f"internal_state_blocked:{internal_state or 'missing'}"
        elif active_blockers:
            entry_eligible = 0
            reason = f"blocking_flags:{','.join(active_blockers)}"
        else:
            entry_eligible = 1
            reason = "ok"

        # Retention is diagnostic for a future holdings-aware optimizer. Missing state
        # never forces a sale; deteriorating/broken states route to the exit engine.
        retention_eligible = int(
            state is None or internal_state in retention_states
        )
        rows.append(
            {
                "ticker": ticker,
                "run_as_of": run_as_of,
                "source_pipeline": score.get("source_pipeline", ""),
                "stage1_investable_eligible": stage1_investable,
                "monitor_state_present": state_present,
                "monitor_investable_eligible": monitor_investable,
                "internal_state": internal_state,
                "action_state": action_state,
                "market_data_status": market_status,
                "blocking_flags_json": json.dumps(
                    active_blockers, separators=(",", ":")
                ),
                "optimizer_entry_eligible": entry_eligible,
                "optimizer_retention_eligible": retention_eligible,
                "policy_reason": reason,
                "state_input_digest": state.get("input_digest", "") if state else "",
            }
        )

    investable = [row for row in rows if row["stage1_investable_eligible"] == 1]
    covered = [row for row in investable if row["monitor_state_present"] == 1]
    coverage = len(covered) / len(investable) if investable else 1.0
    checks = [
        {
            "check": "investable_state_coverage",
            "status": (
                "PASS"
                if coverage
                >= float(policy["minimum_investable_state_coverage_fraction"])
                else "FAIL"
            ),
            "detail": (
                f"covered={len(covered)}/{len(investable)} fraction={coverage:.6f} "
                f"floor={policy['minimum_investable_state_coverage_fraction']}"
            ),
        },
        {
            "check": "same_date_states",
            "status": "PASS" if not invalid_state_dates else "FAIL",
            "detail": f"invalid={invalid_state_dates[:20]}",
        },
        {
            "check": "stage1_monitor_investability_matches",
            "status": "PASS" if not investability_mismatches else "FAIL",
            "detail": f"mismatches={investability_mismatches[:20]}",
        },
        {
            "check": "closed_internal_state_contract",
            "status": "PASS" if not malformed_states else "FAIL",
            "detail": f"malformed={malformed_states[:20]}",
        },
        {
            "check": "entry_policy_is_green_stable_only",
            "status": (
                "PASS"
                if all(
                    not row["optimizer_entry_eligible"]
                    or (
                        row["internal_state"] in entry_states
                        and row["market_data_status"] == "current"
                        and row["blocking_flags_json"] == "[]"
                    )
                    for row in rows
                )
                else "FAIL"
            ),
            "detail": "entry requires green/stable, current market data, and no blocker",
        },
        {
            "check": "retention_policy_preserves_watch_or_missing",
            "status": (
                "PASS"
                if all(
                    not row["optimizer_retention_eligible"]
                    or not row["internal_state"]
                    or row["internal_state"] in retention_states
                    for row in rows
                )
                else "FAIL"
            ),
            "detail": "green/stable/watch retain; missing state never forces liquidation",
        },
    ]
    return rows, checks


def _row_digest(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_selftest() -> None:
    policy = {
        "entry_states": ["green", "stable"],
        "retention_states": ["green", "stable", "watch"],
        "blocking_escalation_flags": ["R6", "PROVIDER_CONFLICT"],
        "minimum_investable_state_coverage_fraction": 0.5,
    }
    scores = [
        {"ticker": ticker, "investable_eligible": "1", "source_pipeline": "x"}
        for ticker in ("GREEN", "STABLE", "WATCH", "BADFLAG", "MISSING")
    ]
    states = [
        {
            "ticker": ticker,
            "run_as_of": "2026-07-31",
            "investable_eligible": "1",
            "internal_state": state,
            "action_state": "hold",
            "market_data_status": "current",
            "escalation_flags_json": flags,
            "input_digest": ticker,
        }
        for ticker, state, flags in (
            ("GREEN", "green", "[]"),
            ("STABLE", "stable", "[]"),
            ("WATCH", "watch", "[]"),
            ("BADFLAG", "stable", '["R6"]'),
        )
    ]
    rows, checks = build_overlay_rows(
        scores, states, run_as_of="2026-07-31", policy=policy
    )
    keyed = {row["ticker"]: row for row in rows}
    assert keyed["GREEN"]["optimizer_entry_eligible"] == 1
    assert keyed["STABLE"]["optimizer_entry_eligible"] == 1
    assert keyed["WATCH"]["optimizer_entry_eligible"] == 0
    assert keyed["WATCH"]["optimizer_retention_eligible"] == 1
    assert keyed["BADFLAG"]["optimizer_entry_eligible"] == 0
    assert keyed["MISSING"]["optimizer_entry_eligible"] == 0
    assert keyed["MISSING"]["optimizer_retention_eligible"] == 1
    assert all(check["status"] == "PASS" for check in checks)
    print("monitor optimizer eligibility overlay selftest: PASS")


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    policy = _policy(config)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    monitor_subdir = monitor_output_subdir(config)
    run_as_of = args.as_of or latest_run_with(
        runs_root, f"{monitor_subdir}/expectations_state.csv"
    )
    if not run_as_of:
        raise ValueError("No expectations-state run exists")
    date.fromisoformat(run_as_of)
    run_dir = runs_root / run_as_of
    monitor_dir = run_dir / monitor_subdir
    optimizer_dir = run_dir / "optimizer"
    overlay_path = optimizer_dir / "monitor_eligibility_overlay.csv"
    validation_path = optimizer_dir / "monitor_eligibility_validation.csv"
    manifest_path = optimizer_dir / "monitor_eligibility_manifest.json"
    fail_if_exists(
        [overlay_path, validation_path, manifest_path], force=args.force
    )

    scores_path = run_dir / "stocks_scores.csv"
    stage1_manifest_path = run_dir / "manifest.json"
    state_path = monitor_dir / "expectations_state.csv"
    state_manifest_path = monitor_dir / "expectations_state_manifest.json"
    validation_manifest_path = (
        monitor_dir
        / "validation"
        / "expectations_state_validation_manifest.json"
    )
    required = [
        scores_path,
        stage1_manifest_path,
        state_path,
        state_manifest_path,
        validation_manifest_path,
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Monitor eligibility inputs missing: {missing}")

    stage1_manifest = read_manifest(stage1_manifest_path)
    if manifest_acceptance_value(stage1_manifest) not in {"PASS", "PASS_WITH_DEFERRED"}:
        raise ValueError("Stage 1 manifest did not pass")
    stage1_files = stage1_manifest.get("files", {})
    score_seal = (
        stage1_files.get("stocks_scores.csv", {})
        if isinstance(stage1_files, dict)
        else {}
    )
    expected_scores_hash = (
        str(score_seal.get("sha256", ""))
        if isinstance(score_seal, dict)
        else ""
    )
    if expected_scores_hash != sha256_file(scores_path):
        raise ValueError("stocks_scores.csv differs from the Stage 1 seal")

    state_manifest = read_manifest(state_manifest_path)
    state_errors = sealed_artifact_errors(
        state_manifest,
        state_path,
        "expectations_state.csv",
        run_as_of=run_as_of,
    )
    if state_errors:
        raise ValueError(f"Expectations state is not sealed/current: {state_errors}")
    state_validation_manifest = read_manifest(validation_manifest_path)
    if (
        manifest_acceptance_value(state_validation_manifest) != "PASS"
        or str(state_validation_manifest.get("as_of_date", "")) != run_as_of
    ):
        raise ValueError("Same-date PASS expectations-state validation is required")
    validation_inputs = dict(state_validation_manifest.get("inputs_sha256", {}))
    if not any(
        Path(str(path)).name == state_manifest_path.name
        and str(value) == sha256_file(state_manifest_path)
        for path, value in validation_inputs.items()
    ):
        raise ValueError("Expectations-state validation does not seal the consumed state manifest")

    rows, checks = build_overlay_rows(
        read_csv(scores_path),
        read_csv(state_path),
        run_as_of=run_as_of,
        policy=policy,
    )
    acceptance = "PASS" if all(row["status"] == "PASS" for row in checks) else "FAIL"
    write_csv(overlay_path, OVERLAY_FIELDS, rows)
    write_csv(validation_path, VALIDATION_FIELDS, checks)
    manifest = {
        "stage": "stage3_monitor_optimizer_eligibility",
        "schema_version": "monitor_optimizer_eligibility_v1",
        "generated_at": utc_now(),
        "run_as_of": run_as_of,
        "acceptance": acceptance,
        "production_entry_gate": True,
        "policy": policy,
        "row_count": len(rows),
        "entry_eligible_count": sum(
            int(row["optimizer_entry_eligible"]) for row in rows
        ),
        "retention_eligible_count": sum(
            int(row["optimizer_retention_eligible"]) for row in rows
        ),
        "row_digest": _row_digest(rows),
        "checks": checks,
        "inputs_sha256": {
            str(path): sha256_file(path)
            for path in required + [config_path, Path(__file__).resolve()]
        },
        "outputs_sha256": {
            overlay_path.name: sha256_file(overlay_path),
            validation_path.name: sha256_file(validation_path),
        },
    }
    write_manifest(manifest_path, manifest)
    for check in checks:
        LOGGER.info(
            "[%s] %s -- %s", check["status"], check["check"], check["detail"]
        )
    LOGGER.info(
        "MONITOR OPTIMIZER ELIGIBILITY (%s): entry=%d/%d -> %s",
        acceptance,
        manifest["entry_eligible_count"],
        len(rows),
        overlay_path,
    )
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
