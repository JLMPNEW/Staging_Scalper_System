#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_text_atomic  # noqa: E402

SCRIPTS = PROJECT_ROOT / "industrials" / "transportation" / "scripts"
DEFAULT_SURFACE_ROOT = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v3" / "surface_delta"
)
DEFAULT_TANKER_ROOT = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v3" / "tanker_delta"
)
DEFAULT_GATE_ROOT = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "specialized_contemporaneous_coverage"
)
DEFAULT_SCORE_HISTORY = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5"
    / "pit_scores_v6" / "transportation_v5_pit_score_history_build.csv"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the transportation specialized-metric completion sequence once: "
            "census, bounded hydration, one corpus parse, semantic replay, static "
            "coverage, and the final PIT breadth gate. Historical rebuilding and "
            "calibration are intentionally outside this command."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--surface-output-dir", type=Path, default=None)
    parser.add_argument("--tanker-output-dir", type=Path, default=None)
    parser.add_argument("--gate-output-dir", type=Path, default=None)
    parser.add_argument("--score-history", type=Path, default=DEFAULT_SCORE_HISTORY)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--reviewed-at", default=None)
    parser.add_argument("--offline-only", action="store_true")
    parser.add_argument(
        "--resume-after-event-sources",
        action="store_true",
        help="Reuse a completed, audited event-source pass and resume at exact-gap hydration.",
    )
    parser.add_argument(
        "--resume-after-parser-runs",
        action="store_true",
        help="Reuse completed source and parser checkpoints and resume at local derivation/review.",
    )
    return parser.parse_args()


def _run(
    stage: str,
    command: list[str],
    receipts: list[dict[str, object]],
    *,
    allowed_return_codes: frozenset[int] = frozenset({0}),
    skip: bool = False,
) -> None:
    if skip:
        receipt = {
            "stage": stage,
            "command": command,
            "return_code": 0,
            "elapsed_seconds": 0.0,
            "status": "PASS",
            "reused_completed_checkpoint": True,
        }
        receipts.append(receipt)
        print(f"SKIP {stage} reason=completed_checkpoint", flush=True)
        return
    print(f"START {stage}", flush=True)
    started = time.monotonic()
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    elapsed = round(time.monotonic() - started, 3)
    receipt = {
        "stage": stage,
        "command": command,
        "return_code": completed.returncode,
        "elapsed_seconds": elapsed,
        "status": "PASS" if completed.returncode in allowed_return_codes else "FAIL",
        "allowed_return_codes": sorted(allowed_return_codes),
    }
    receipts.append(receipt)
    print(f"END {stage} status={receipt['status']} elapsed={elapsed}", flush=True)
    if completed.returncode not in allowed_return_codes:
        raise RuntimeError(f"{stage} failed with return code {completed.returncode}")


def _python(script: str, *args: object) -> list[str]:
    return [sys.executable, str(SCRIPTS / script), *(str(value) for value in args)]


def _run_id(path: Path) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    run_id = int(payload.get("run_id") or (payload.get("extraction_funnel") or {}).get("run", {}).get("run_id") or 0)
    if run_id <= 0:
        raise ValueError(f"parser run id missing: {path}")
    return run_id


def main() -> int:
    args = parse_args()
    surface = (
        args.surface_output_dir.expanduser().resolve()
        if args.surface_output_dir
        else DEFAULT_SURFACE_ROOT / args.asof
    )
    tanker = (
        args.tanker_output_dir.expanduser().resolve()
        if args.tanker_output_dir
        else DEFAULT_TANKER_ROOT / args.asof
    )
    gate = (
        args.gate_output_dir.expanduser().resolve()
        if args.gate_output_dir
        else DEFAULT_GATE_ROOT / args.asof
    )
    surface.mkdir(parents=True, exist_ok=True)
    tanker.mkdir(parents=True, exist_ok=True)
    gate.mkdir(parents=True, exist_ok=True)
    reviewed_at = args.reviewed_at or f"{args.asof}T23:00:00Z"
    receipts: list[dict[str, object]] = []
    overall_path = gate / "transportation_specialized_metric_completion.json"
    resume_source = args.resume_after_event_sources or args.resume_after_parser_runs

    try:
        if resume_source:
            required_source_artifacts = (
                surface / "transportation_surface_delta_source_census.csv",
                surface / "transportation_surface_delta_source_decisions.csv",
                surface / "transportation_surface_delta_cache_gaps.csv",
                surface / "transportation_surface_excluded_event_anchor_audit.csv",
                tanker / "transportation_tanker_delta_source_census.csv",
                tanker / "transportation_tanker_delta_source_decisions.csv",
                tanker / "transportation_tanker_delta_cache_gaps.csv",
                tanker / "transportation_tanker_excluded_event_anchor_audit.csv",
            )
            missing = [str(path) for path in required_source_artifacts if not path.is_file()]
            if missing:
                raise FileNotFoundError(
                    "cannot resume without the completed source checkpoint: "
                    + ", ".join(missing)
                )
        if args.resume_after_parser_runs:
            required_parser_artifacts = (
                surface / "transportation_surface_delta_parser_run.json",
                tanker / "transportation_tanker_delta_parser_run.json",
            )
            missing = [str(path) for path in required_parser_artifacts if not path.is_file()]
            if missing:
                raise FileNotFoundError(
                    "cannot resume without completed parser checkpoints: "
                    + ", ".join(missing)
                )

        # Bootstrap both decision sets before event-document completion.
        _run(
            "surface_initial_census",
            _python("36j_build_transportation_surface_delta_census.py", "--asof", args.asof, "--output-dir", surface),
            receipts,
            allowed_return_codes=frozenset({0, 2}),
            skip=resume_source,
        )
        _run(
            "tanker_initial_census",
            _python("36c_build_transportation_tanker_delta_census.py", "--asof", args.asof, "--output-dir", tanker),
            receipts,
            allowed_return_codes=frozenset({0, 2}),
            skip=resume_source,
        )
        event_command = _python(
            "39a_complete_transportation_specialized_event_sources.py",
            "--asof", args.asof,
            "--cohort", "both",
            "--surface-output-dir", surface,
            "--tanker-output-dir", tanker,
        )
        if not args.offline_only:
            event_command.append("--hydrate")
        _run(
            "all_metric_event_source_completion",
            event_command,
            receipts,
            skip=resume_source,
        )

        # Rebuild the censuses once with the offline event-anchor accession set.
        _run(
            "surface_event_anchored_census",
            _python("36j_build_transportation_surface_delta_census.py", "--asof", args.asof, "--output-dir", surface),
            receipts,
            allowed_return_codes=frozenset({0, 2}),
            skip=resume_source,
        )
        _run(
            "tanker_event_anchored_census",
            _python("36c_build_transportation_tanker_delta_census.py", "--asof", args.asof, "--output-dir", tanker),
            receipts,
            allowed_return_codes=frozenset({0, 2}),
            skip=resume_source,
        )

        # Hydrate only remaining exact census gaps, then seal zero-gap censuses.
        _run(
            "surface_exact_document_hydration",
            _python("36l_hydrate_transportation_surface_delta_documents.py", "--asof", args.asof, "--output-dir", surface),
            receipts,
            skip=args.resume_after_parser_runs,
        )
        _run(
            "tanker_exact_document_hydration",
            _python("36e_hydrate_transportation_tanker_delta_documents.py", "--asof", args.asof, "--output-dir", tanker),
            receipts,
            skip=args.resume_after_parser_runs,
        )
        _run(
            "surface_final_zero_gap_census",
            _python("36j_build_transportation_surface_delta_census.py", "--asof", args.asof, "--output-dir", surface),
            receipts,
            skip=args.resume_after_parser_runs,
        )
        _run(
            "tanker_final_zero_gap_census",
            _python("36c_build_transportation_tanker_delta_census.py", "--asof", args.asof, "--output-dir", tanker),
            receipts,
            skip=args.resume_after_parser_runs,
        )

        # Each sealed corpus is parsed once for all its metric families.
        _run(
            "surface_parser_plan",
            _python("36k_run_transportation_surface_delta_parser.py", "--asof", args.asof, "--workers", args.workers, "--output-dir", surface, "--plan-only"),
            receipts,
            skip=args.resume_after_parser_runs,
        )
        _run(
            "surface_parser_execute",
            _python("36k_run_transportation_surface_delta_parser.py", "--asof", args.asof, "--workers", args.workers, "--output-dir", surface, "--execute"),
            receipts,
            skip=args.resume_after_parser_runs,
        )
        _run(
            "tanker_parser_plan",
            _python("36d_run_transportation_tanker_delta_parser.py", "--asof", args.asof, "--workers", args.workers, "--output-dir", tanker, "--plan-only"),
            receipts,
            skip=args.resume_after_parser_runs,
        )
        _run(
            "tanker_parser_execute",
            _python("36d_run_transportation_tanker_delta_parser.py", "--asof", args.asof, "--workers", args.workers, "--output-dir", tanker, "--execute"),
            receipts,
            skip=args.resume_after_parser_runs,
        )
        surface_run = _run_id(surface / "transportation_surface_delta_parser_run.json")
        tanker_run = _run_id(tanker / "transportation_tanker_delta_parser_run.json")

        # Reuse persisted facts and parser evidence; no second document parse.
        _run(
            "surface_xbrl_derivation_replay",
            _python("36n_replay_transportation_surface_xbrl_derivations.py", "--asof", args.asof, "--source-run-id", surface_run, "--output-dir", surface),
            receipts,
        )
        _run(
            "surface_fact_store_ratios",
            _python("36o_build_transportation_surface_ratios_from_fact_store.py", "--asof", args.asof, "--parser-run-id", surface_run, "--output-dir", surface),
            receipts,
        )
        _run(
            "surface_static_parser_coverage",
            _python("36m_audit_transportation_surface_parser_coverage.py", "--asof", args.asof, "--run-id", surface_run, "--output-dir", surface),
            receipts,
        )
        _run(
            "tanker_static_parser_coverage",
            _python("36f_audit_transportation_tanker_parser_coverage.py", "--asof", args.asof, "--run-id", tanker_run, "--output-dir", tanker),
            receipts,
        )

        # Review all priority tiers once after discovery is exhausted.
        _run(
            "surface_semantic_queue",
            _python("36p_build_transportation_surface_semantic_review_queue.py", "--asof", args.asof, "--run-id", surface_run, "--output-dir", surface),
            receipts,
        )
        _run(
            "surface_semantic_review",
            _python("36q_review_transportation_surface_semantic_definitions.py", "--asof", args.asof, "--run-id", surface_run, "--reviewed-at", reviewed_at, "--priorities", "HIGH,MEDIUM,LOW", "--output-dir", surface),
            receipts,
        )
        _run(
            "surface_semantic_replay",
            _python("36r_replay_transportation_surface_semantic_approvals.py", "--asof", args.asof, "--run-id", surface_run, "--output-dir", surface),
            receipts,
        )
        _run(
            "surface_post_review_coverage",
            _python("36s_audit_transportation_surface_post_review_coverage.py", "--asof", args.asof, "--run-id", surface_run, "--output-dir", surface),
            receipts,
        )
        _run(
            "tanker_semantic_queue",
            _python("36t_build_transportation_tanker_semantic_review_queue.py", "--asof", args.asof, "--run-id", tanker_run, "--output-dir", tanker),
            receipts,
        )
        _run(
            "tanker_semantic_review",
            _python("36u_review_transportation_tanker_semantic_definitions.py", "--asof", args.asof, "--run-id", tanker_run, "--reviewed-at", reviewed_at, "--priorities", "HIGH,MEDIUM,LOW", "--output-dir", tanker),
            receipts,
        )
        _run(
            "tanker_semantic_replay",
            _python("36v_replay_transportation_tanker_semantic_approvals.py", "--asof", args.asof, "--run-id", tanker_run, "--output-dir", tanker),
            receipts,
        )
        _run(
            "tanker_post_review_coverage",
            _python("36w_audit_transportation_tanker_post_review_coverage.py", "--asof", args.asof, "--run-id", tanker_run, "--output-dir", tanker),
            receipts,
        )
        _run(
            "contemporaneous_specialized_coverage",
            _python(
                "39b_audit_transportation_contemporaneous_specialized_coverage.py",
                "--asof", args.asof,
                "--score-history", args.score_history.expanduser().resolve(),
                "--surface-replay", surface / "transportation_surface_semantic_replay_accepted.csv",
                "--tanker-replay", tanker / "transportation_tanker_semantic_replay_accepted.csv",
                "--output-dir", gate,
            ),
            receipts,
        )
        coverage = json.loads(
            (gate / "transportation_specialized_contemporaneous_coverage.json").read_text(encoding="utf-8")
        )
        final = {
            "acceptance": "PASS",
            "asof_date": args.asof,
            "surface_parser_run_id": surface_run,
            "tanker_parser_run_id": tanker_run,
            "stage_count": len(receipts),
            "stages": receipts,
            "source_document_parse_batches": 2,
            "document_reparse_after_semantic_review": 0,
            "calibration_accepted_metric_count": coverage["calibration_accepted_metric_count"],
            "historical_reconstruction_authorized": bool(coverage["calibration_authorized"]),
            "calibration_authorized": False,
            "production_promotion_authorized": False,
            "next_gate": coverage["next_gate"],
        }
    except Exception as exc:
        final = {
            "acceptance": "FAIL",
            "asof_date": args.asof,
            "stage_count": len(receipts),
            "stages": receipts,
            "error": f"{type(exc).__name__}: {exc}",
            "historical_reconstruction_authorized": False,
            "calibration_authorized": False,
            "production_promotion_authorized": False,
        }
        write_text_atomic(
            overall_path,
            json.dumps(final, indent=2, sort_keys=True) + "\n",
        )
        print(json.dumps(final, indent=2, sort_keys=True))
        return 2

    write_text_atomic(
        overall_path,
        json.dumps(final, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(final, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

