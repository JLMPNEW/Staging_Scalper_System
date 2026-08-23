#!/usr/bin/env python3
"""Freeze the accepted specialized set and gate any second historical rebuild."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.specialized_metric_freeze import (  # noqa: E402
    AcceptedDomain,
    accepted_summary_rows,
    compare_replay_with_panel,
)


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5"
DATA = PROJECT_ROOT / "industrials" / "transportation" / "data"
DEFAULT_COMPLETION = (
    ROOT / "specialized_contemporaneous_coverage" / "2026-08-21"
    / "transportation_specialized_metric_completion.json"
)
DEFAULT_COVERAGE = (
    ROOT / "specialized_contemporaneous_coverage" / "2026-08-21"
    / "transportation_specialized_contemporaneous_coverage.json"
)
DEFAULT_SURFACE_REPLAY = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v3"
    / "surface_delta" / "2026-08-21"
    / "transportation_surface_semantic_replay_accepted.csv"
)
DEFAULT_TANKER_REPLAY = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v3"
    / "tanker_delta" / "2026-08-21"
    / "transportation_tanker_semantic_replay_accepted.csv"
)
DEFAULT_PANEL = (
    ROOT / "outcome_panel_v6" / "2026-08-16"
    / "transportation_v5_outcome_panel.csv"
)
DEFAULT_FORENSICS = (
    ROOT / "model_forensic_audit_v7" / "2026-08-21"
    / "transportation_v5_specialized_metric_signal_audit.csv"
)
DEFAULT_DECISION = (
    ROOT / "research_decision_v7" / "2026-08-21"
    / "transportation_v7_research_decision.json"
)
DEFAULT_REGISTRY = DATA / "transportation_specialized_metric_discovery_registry.csv"
DEFAULT_SURFACE_DOMAINS = DATA / "transportation_surface_metric_comparison_domains_v2.csv"
DEFAULT_TANKER_DOMAINS = DATA / "transportation_tanker_metric_comparison_domains_v1.csv"
DEFAULT_OUTPUT = ROOT / "specialized_metric_freeze_v8" / "2026-08-21"

ACCEPTED_FIELDS = (
    "policy_version", "cohort", "metric_id", "comparison_domain_id", "score_date_count",
    "passing_score_date_count", "passing_score_date_fraction",
    "minimum_date_pass_fraction", "latest_score_date", "latest_date_gate",
    "calibration_eligibility", "calibration_gate", "applicable_tickers",
    "minimum_accepted_breadth", "max_staleness_days",
    "prior_forensic_observed_rows", "prior_forensic_coverage",
    "prior_forensic_disposition",
)
DELTA_FIELDS = (
    "cohort", "metric_id", "comparison_domain_id", "asof_date", "ticker",
    "prior_panel_value", "new_replay_value", "value_disposition", "absolute_delta",
)
DELTA_SUMMARY_FIELDS = (
    "cohort", "metric_id", "comparison_domain_id", "ticker_count",
    "score_date_count", "minimum_breadth", "prior_panel_passing_date_count",
    "new_replay_passing_date_count", "prior_panel_passing_date_fraction",
    "new_replay_passing_date_fraction", "unchanged_cell_count",
    "new_fill_cell_count", "changed_value_cell_count",
    "new_information_cell_count", "not_in_new_compatible_set_count",
    "input_delta_gate",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default="2026-08-21")
    parser.add_argument("--completion", type=Path, default=DEFAULT_COMPLETION)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--surface-replay", type=Path, default=DEFAULT_SURFACE_REPLAY)
    parser.add_argument("--tanker-replay", type=Path, default=DEFAULT_TANKER_REPLAY)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--forensics", type=Path, default=DEFAULT_FORENSICS)
    parser.add_argument("--research-decision", type=Path, default=DEFAULT_DECISION)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--surface-domains", type=Path, default=DEFAULT_SURFACE_DOMAINS)
    parser.add_argument("--tanker-domains", type=Path, default=DEFAULT_TANKER_DOMAINS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def domain_rows(paths: list[Path]) -> dict[tuple[str, str, str], dict[str, str]]:
    rows: dict[tuple[str, str, str], dict[str, str]] = {}
    for path in paths:
        for row in read_csv(path):
            cohort = str(row.get("cohort") or "surface")
            key = (
                cohort,
                str(row["metric_id"]),
                str(row["comparison_domain_id"]),
            )
            if key in rows:
                raise ValueError(f"duplicate specialized domain rule={key}")
            rows[key] = row
    return rows


def markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Transportation accepted specialized-metric freeze",
        "",
        f"**Decision:** {payload['decision']}",
        "",
        "The exhaustive parser and semantic-review batch is complete. This gate "
        "compares the accepted point-in-time facts with the immutable v6 panel "
        "before permitting any second historical build.",
        "",
        "## Frozen accepted set",
        "",
        "| Cohort | Metric | Domain | PIT dates | Latest | Prior disposition |",
        "|---|---|---|---:|---|---|",
    ]
    for row in payload["accepted_metric_domains"]:
        lines.append(
            f"| {row['cohort']} | {row['metric_id']} | "
            f"{row['comparison_domain_id']} | {row['passing_score_date_count']}/"
            f"{row['score_date_count']} | {row['latest_date_gate']} | "
            f"{row['prior_forensic_disposition']} |"
        )
    lines.extend(
        [
            "",
            "## Rebuild gate",
            "",
            f"- New or changed point-in-time cells: {payload['new_information_cell_count']}",
            f"- Full historical rebuild authorized: {payload['full_historical_feature_rebuild_authorized']}",
            f"- Historical recalibration authorized: {payload['historical_recalibration_authorized']}",
            f"- Production activation authorized: {payload['production_activation_authorized']}",
            f"- Next gate: {payload['next_gate']}",
            "",
            "Revealed v6 outcomes remain diagnostic only. Any incremental proof can "
            "allocate research effort but cannot support production promotion.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    paths = {
        "completion": args.completion.resolve(),
        "coverage": args.coverage.resolve(),
        "surface_replay": args.surface_replay.resolve(),
        "tanker_replay": args.tanker_replay.resolve(),
        "panel": args.panel.resolve(),
        "forensics": args.forensics.resolve(),
        "research_decision": args.research_decision.resolve(),
        "registry": args.registry.resolve(),
        "surface_domains": args.surface_domains.resolve(),
        "tanker_domains": args.tanker_domains.resolve(),
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing specialized freeze inputs={missing}")

    completion = read_json(paths["completion"])
    coverage = read_json(paths["coverage"])
    decision = read_json(paths["research_decision"])
    if completion.get("acceptance") != "PASS":
        raise ValueError("specialized metric completion is not PASS")
    reparse_count = completion.get("document_reparse_after_semantic_review")
    if reparse_count is None or int(reparse_count) != 0:
        raise ValueError("semantic-review contract was violated by a reparse")
    if coverage.get("acceptance") != "PASS":
        raise ValueError("contemporaneous coverage audit is not PASS")
    if bool(coverage.get("production_promotion_authorized")):
        raise ValueError("coverage audit cannot authorize production")
    if (
        decision.get("decision")
        != "APPROVE_RESEARCH_SPEC_AND_ACCEPTED_FACT_REPLAY_ONLY"
        or bool(decision.get("historical_recalibration_authorized"))
        or bool(decision.get("production_activation_authorized"))
    ):
        raise ValueError("v7 research decision is not fail-closed")

    summary_path = Path(str(coverage["summary_csv"])).resolve()
    if (
        summary_path != paths["coverage"].parent
        / "transportation_specialized_contemporaneous_coverage_summary.csv"
        or file_sha256(summary_path) != str(coverage["summary_csv_sha256"])
    ):
        raise ValueError("coverage summary path/hash mismatch")
    if str(coverage["input_hashes"]["registry"]) != file_sha256(paths["registry"]):
        raise ValueError("metric registry changed after coverage audit")
    if str(coverage["input_hashes"]["surface_domains"]) != file_sha256(
        paths["surface_domains"]
    ):
        raise ValueError("surface domain policy changed after coverage audit")
    if str(coverage["input_hashes"]["tanker_domains"]) != file_sha256(
        paths["tanker_domains"]
    ):
        raise ValueError("tanker domain policy changed after coverage audit")
    if str(coverage["input_hashes"]["surface_replay"]) != file_sha256(
        paths["surface_replay"]
    ):
        raise ValueError("surface semantic replay changed after coverage audit")
    if str(coverage["input_hashes"]["tanker_replay"]) != file_sha256(
        paths["tanker_replay"]
    ):
        raise ValueError("tanker semantic replay changed after coverage audit")

    accepted = accepted_summary_rows(read_csv(summary_path))
    if len(accepted) != int(coverage["calibration_accepted_domain_count"]):
        raise ValueError("accepted domain count does not match coverage manifest")

    rules = domain_rows([paths["surface_domains"], paths["tanker_domains"]])
    staleness = {
        row["metric_id"]: int(row["max_staleness_days"])
        for row in read_csv(paths["registry"])
    }
    forensic = {
        (
            "surface"
            if str(row.get("cohort_id") or "").startswith("north_american_surface")
            else "tanker",
            str(row.get("metric_id") or ""),
            str(row.get("comparison_domain") or ""),
        ): row
        for row in read_csv(paths["forensics"])
    }

    domains: list[AcceptedDomain] = []
    frozen_rows: list[dict[str, object]] = []
    for row in accepted:
        key = (
            str(row["cohort"]),
            str(row["metric_id"]),
            str(row["comparison_domain_id"]),
        )
        rule = rules.get(key)
        if rule is None:
            raise ValueError(f"accepted metric-domain lacks frozen rule={key}")
        tickers = tuple(
            ticker.strip().upper()
            for ticker in str(rule["applicable_tickers"]).split("|")
            if ticker.strip()
        )
        max_age = int(staleness[str(row["metric_id"])])
        domains.append(
            AcceptedDomain(
                cohort=key[0],
                metric_id=key[1],
                domain_id=key[2],
                tickers=tickers,
                minimum_breadth=int(rule["minimum_accepted_breadth"]),
                max_staleness_days=max_age,
            )
        )
        prior = forensic.get(key, {})
        frozen_rows.append(
            {
                **row,
                "applicable_tickers": "|".join(tickers),
                "minimum_accepted_breadth": int(rule["minimum_accepted_breadth"]),
                "max_staleness_days": max_age,
                "prior_forensic_observed_rows": prior.get("observed_rows", ""),
                "prior_forensic_coverage": prior.get("coverage", ""),
                "prior_forensic_disposition": prior.get(
                    "research_disposition", "NOT_PREVIOUSLY_TESTED"
                ),
            }
        )

    replay_rows = read_csv(paths["surface_replay"]) + read_csv(paths["tanker_replay"])
    delta, delta_summary = compare_replay_with_panel(
        panel_rows=read_csv(paths["panel"]),
        replay_rows=replay_rows,
        domains=domains,
    )
    new_information = sum(
        int(row["new_information_cell_count"]) for row in delta_summary
    )
    prior_tested = all(
        row["prior_forensic_disposition"] != "NOT_PREVIOUSLY_TESTED"
        for row in frozen_rows
    )
    proof_authorized = new_information > 0
    next_gate = (
        "RUN_ACCEPTED_FACT_INCREMENTAL_PROOF_WITHOUT_REBUILD"
        if proof_authorized
        else "CAPTURE_V7_FUTURE_ONLY_SHADOW_SIGNALS_FROM_2026_08_24"
    )
    result: dict[str, Any] = {
        "acceptance": "PASS",
        "asof_date": str(args.asof)[:10],
        "contract_version": "transportation_specialized_metric_freeze_v8_v1",
        "decision": (
            "FREEZE_ACCEPTED_SET_AND_REQUIRE_NO_REBUILD_INCREMENTAL_PROOF"
            if proof_authorized
            else "FREEZE_ACCEPTED_SET_DENY_DUPLICATE_HISTORICAL_REBUILD"
        ),
        "accepted_metric_domain_count": len(frozen_rows),
        "accepted_metric_count": len({row["metric_id"] for row in frozen_rows}),
        "accepted_metric_domains": frozen_rows,
        "all_accepted_domains_previously_tested": prior_tested,
        "new_information_cell_count": new_information,
        "accepted_fact_incremental_proof_authorized": proof_authorized,
        "canonical_candidate_materialization_authorized": False,
        "full_historical_feature_rebuild_authorized": False,
        "historical_recalibration_authorized": False,
        "production_activation_authorized": False,
        "first_eligible_future_signal_date": decision["research_specification"][
            "first_future_signal_date"
        ],
        "next_gate": next_gate,
        "network_requests": 0,
        "parser_invocations": 0,
        "lineage": {
            label: {"path": str(path), "sha256": file_sha256(path)}
            for label, path in paths.items()
        },
    }

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    accepted_path = output_dir / "transportation_accepted_specialized_metric_set.csv"
    detail_path = output_dir / "transportation_specialized_input_delta.csv"
    summary_out = output_dir / "transportation_specialized_input_delta_summary.csv"
    manifest_path = output_dir / "transportation_specialized_metric_freeze.json"
    markdown_path = output_dir / "TRANSPORTATION_SPECIALIZED_METRIC_FREEZE.md"
    write_csv_atomic(accepted_path, ACCEPTED_FIELDS, frozen_rows)
    write_csv_atomic(detail_path, DELTA_FIELDS, delta)
    write_csv_atomic(summary_out, DELTA_SUMMARY_FIELDS, delta_summary)
    result["artifacts"] = {
        "accepted_metric_set": {
            "path": str(accepted_path),
            "sha256": file_sha256(accepted_path),
        },
        "input_delta": {"path": str(detail_path), "sha256": file_sha256(detail_path)},
        "input_delta_summary": {
            "path": str(summary_out),
            "sha256": file_sha256(summary_out),
        },
    }
    write_text_atomic(
        manifest_path, json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    write_text_atomic(markdown_path, markdown(result))
    print(
        json.dumps(
            {
                "acceptance": result["acceptance"],
                "decision": result["decision"],
                "accepted_metric_domains": [
                    f"{row['metric_id']}::{row['comparison_domain_id']}"
                    for row in frozen_rows
                ],
                "new_information_cell_count": new_information,
                "next_gate": next_gate,
                "full_historical_feature_rebuild_authorized": False,
                "production_activation_authorized": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
