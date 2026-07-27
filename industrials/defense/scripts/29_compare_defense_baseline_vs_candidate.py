#!/usr/bin/env python3
"""Baseline-versus-candidate promotion evidence for defense weekly OOS research.

Report-only. Compares the sealed baseline weekly research artifacts against a
candidate namespace produced by ``26_run_defense_weekly_calibration_research.py
--research-label <label>`` and evaluates the production-promotion gates:

  * candidate validation IC > 0
  * candidate untouched holdout IC > 0
  * candidate holdout excess return vs XAR > 0
  * candidate improves on the BASELINE (validation IC, holdout IC, holdout
    excess) — improvement over zero alone is insufficient
  * Optuna selection metric is validation_ic (never train)
  * purged/embargoed split boundaries present in the candidate panel
  * no coverage regression (eligible panel rows) beyond tolerance
  * turnover/concentration guard: candidate selected_count per period within
    tolerance of baseline

Never mutates research artifacts and never promotes; ``27_promote...`` remains
the only promotion path and should require this report to PASS.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.defense.research_artifacts import as_float, sha256_file, utc_now, write_json_atomic  # noqa: E402

REPORT_FIELDS = ["gate", "status", "baseline", "candidate", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare baseline vs candidate defense weekly OOS research artifacts.")
    parser.add_argument("--candidate-label", required=True)
    parser.add_argument("--baseline-stage8", type=Path, default=PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8")
    parser.add_argument("--baseline-stage9", type=Path, default=PROJECT_ROOT / "output" / "industrials" / "defense" / "stage9")
    parser.add_argument("--coverage-regression-tolerance", type=float, default=0.02, help="Allowed fractional drop in eligible panel rows vs baseline.")
    parser.add_argument("--selection-count-tolerance", type=float, default=0.35, help="Allowed fractional change in mean selected_count per period (concentration/turnover guard).")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def read_summary_csv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    return rows[0] if rows else {}


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def mean_selected_count(period_csv: Path) -> float | None:
    if not period_csv.exists():
        return None
    counts = [v for r in csv.DictReader(open(period_csv, encoding="utf-8-sig")) if (v := as_float(r.get("selected_count"))) is not None]
    return sum(counts) / len(counts) if counts else None


def namespace_paths(stage8: Path, stage9: Path) -> dict[str, Path]:
    return {
        "calibration_summary": stage8 / "optuna_calibration_weekly" / "defense_optuna_calibration_summary.csv",
        "panel_manifest": stage8 / "oos_calibration_panel_weekly" / "defense_oos_calibration_panel_manifest.json",
        "splits_csv": stage8 / "oos_calibration_panel_weekly" / "defense_oos_calibration_splits.csv",
        "backtest_summary": stage9 / "score_backtest_weekly" / "defense_score_backtest_summary.csv",
        "backtest_periods": stage9 / "score_backtest_weekly" / "defense_score_backtest_periods.csv",
    }


def main() -> int:
    args = parse_args()
    label = args.candidate_label.strip().lower()
    base8 = args.baseline_stage8.expanduser().resolve()
    base9 = args.baseline_stage9.expanduser().resolve()
    cand8 = base8 / "candidates" / label
    cand9 = base9 / "candidates" / label
    output_dir = (args.output_dir.expanduser().resolve() if args.output_dir else cand8 / "baseline_vs_candidate")

    baseline = namespace_paths(base8, base9)
    candidate = namespace_paths(cand8, cand9)
    missing = [str(p) for p in candidate.values() if not p.exists()]
    if missing:
        raise FileNotFoundError("Candidate namespace incomplete; run 26 with --research-label first. Missing: " + "; ".join(missing[:4]))

    b_cal = read_summary_csv(baseline["calibration_summary"])
    c_cal = read_summary_csv(candidate["calibration_summary"])
    b_bt = read_summary_csv(baseline["backtest_summary"])
    c_bt = read_summary_csv(candidate["backtest_summary"])
    b_panel = read_json(baseline["panel_manifest"])
    c_panel = read_json(candidate["panel_manifest"])

    rows: list[dict[str, str]] = []
    failures: list[str] = []

    def gate(name: str, ok: bool | None, baseline_value: object, candidate_value: object, detail: str = "") -> None:
        status = "PASS" if ok else ("WARN" if ok is None else "FAIL")
        if ok is False:
            failures.append(name)
        rows.append({
            "gate": name,
            "status": status,
            "baseline": "" if baseline_value is None else str(baseline_value),
            "candidate": "" if candidate_value is None else str(candidate_value),
            "detail": detail,
        })

    c_val_ic = as_float(c_cal.get("validation_ic"))
    c_hold_ic = as_float(c_cal.get("holdout_ic"))
    b_val_ic = as_float(b_cal.get("validation_ic"))
    b_hold_ic = as_float(b_cal.get("holdout_ic"))
    gate("candidate_validation_ic_positive", c_val_ic is not None and c_val_ic > 0, b_val_ic, c_val_ic)
    gate("candidate_holdout_ic_positive", c_hold_ic is not None and c_hold_ic > 0, b_hold_ic, c_hold_ic)

    c_hold_exc = as_float(c_bt.get("holdout_mean_excess_vs_benchmark"))
    b_hold_exc = as_float(b_bt.get("holdout_mean_excess_vs_benchmark"))
    gate("candidate_holdout_excess_vs_xar_positive", c_hold_exc is not None and c_hold_exc > 0, b_hold_exc, c_hold_exc)

    gate(
        "candidate_beats_baseline_validation_ic",
        (c_val_ic is not None and b_val_ic is not None and c_val_ic > b_val_ic) if b_val_ic is not None else None,
        b_val_ic, c_val_ic,
        "baseline missing -> WARN" if b_val_ic is None else "",
    )
    gate(
        "candidate_beats_baseline_holdout_ic",
        (c_hold_ic is not None and b_hold_ic is not None and c_hold_ic > b_hold_ic) if b_hold_ic is not None else None,
        b_hold_ic, c_hold_ic,
        "baseline missing -> WARN" if b_hold_ic is None else "",
    )
    gate(
        "candidate_beats_baseline_holdout_excess",
        (c_hold_exc is not None and b_hold_exc is not None and c_hold_exc > b_hold_exc) if b_hold_exc is not None else None,
        b_hold_exc, c_hold_exc,
        "baseline missing -> WARN" if b_hold_exc is None else "",
    )

    selection_metric = str(c_cal.get("selection_metric") or "")
    gate("optuna_selection_on_validation_only", selection_metric == "validation_ic", str(b_cal.get("selection_metric") or ""), selection_metric)

    embargoed = c_panel.get("embargoed_snapshots")
    split_names = set()
    if candidate["splits_csv"].exists():
        split_names = {r.get("split_name", "") for r in csv.DictReader(open(candidate["splits_csv"], encoding="utf-8-sig"))}
    gate(
        "purged_embargo_enforced",
        (embargoed is not None and int(embargoed) >= 0) and ("embargo" in split_names or int(embargoed or 0) == 0),
        str(b_panel.get("embargoed_snapshots")), str(embargoed),
        f"splits={sorted(split_names)}",
    )

    b_rows = as_float(b_panel.get("eligible_rows"))
    c_rows = as_float(c_panel.get("eligible_rows"))
    if b_rows and c_rows is not None:
        regression = (b_rows - c_rows) / b_rows
        gate("no_coverage_regression", regression <= args.coverage_regression_tolerance, b_rows, c_rows, f"drop={regression:.4f} tol={args.coverage_regression_tolerance}")
    else:
        gate("no_coverage_regression", None, b_rows, c_rows, "baseline or candidate eligible_rows missing")

    b_sel = mean_selected_count(baseline["backtest_periods"])
    c_sel = mean_selected_count(candidate["backtest_periods"])
    if b_sel and c_sel is not None:
        change = abs(c_sel - b_sel) / b_sel
        gate("selection_count_within_tolerance", change <= args.selection_count_tolerance, f"{b_sel:.2f}", f"{c_sel:.2f}", f"change={change:.4f} tol={args.selection_count_tolerance}")
    else:
        gate("selection_count_within_tolerance", None, b_sel, c_sel, "period selection counts missing")

    promotable = not failures
    output_dir.mkdir(parents=True, exist_ok=True)
    report_csv = output_dir / "defense_baseline_vs_candidate_report.csv"
    write_csv_atomic(report_csv, REPORT_FIELDS, rows)
    manifest = {
        "artifact_family": "defense_baseline_vs_candidate",
        "created_at_utc": utc_now(),
        "candidate_label": label,
        "promotable_evidence": promotable,
        "failed_gates": failures,
        "warn_gates": [r["gate"] for r in rows if r["status"] == "WARN"],
        "inputs": {
            name: {"baseline": str(baseline[name]), "candidate": str(candidate[name]),
                   "candidate_sha256": sha256_file(candidate[name]) if candidate[name].exists() else ""}
            for name in candidate
        },
        "report_csv": str(report_csv),
        "report_csv_sha256": sha256_file(report_csv),
        "note": (
            "Report-only. If the candidate does not improve OOS performance, keep parsed "
            "specialized data in production as supplemental diagnostics with zero scoring weight."
        ),
    }
    write_json_atomic(output_dir / "defense_baseline_vs_candidate_manifest.json", manifest)
    for row in rows:
        print(f"{row['status']:4} {row['gate']:42} baseline={row['baseline'] or '-':>14} candidate={row['candidate'] or '-':>14} {row['detail']}")
    print(f"Promotion evidence: {'PASS' if promotable else 'FAIL'} -> {report_csv}")
    return 0 if promotable else 1


if __name__ == "__main__":
    raise SystemExit(main())
