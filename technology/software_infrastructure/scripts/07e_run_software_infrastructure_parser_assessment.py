#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)


HYDRATION_SCRIPT = (
    PACKAGE_ROOT
    / "software_infrastructure"
    / "scripts"
    / "07c_hydrate_software_infrastructure_parser_documents.py"
)
SHADOW_SCRIPT = (
    PACKAGE_ROOT
    / "software_infrastructure"
    / "scripts"
    / "07d_run_software_infrastructure_parser_shadow.py"
)
DEFAULT_CACHE = PROJECT_ROOT / "output" / "technology_cache" / "dedicated_parser"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "dedicated_parser_assessment"
)


@dataclass(frozen=True)
class AssessmentStratum:
    name: str
    forms: tuple[str, ...]


ASSESSMENT_STRATA = (
    AssessmentStratum(
        "latest_annual",
        ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"),
    ),
    AssessmentStratum("latest_quarterly", ("10-Q", "10-Q/A")),
    AssessmentStratum("latest_8k", ("8-K", "8-K/A")),
    AssessmentStratum("latest_6k", ("6-K", "6-K/A")),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sequentially hydrate and shadow-parse the latest representative "
            "software filing strata across the survivorship-corrected universe."
        )
    )
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--start-date", default="")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument(
        "--strata",
        default=",".join(stratum.name for stratum in ASSESSMENT_STRATA),
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--request-spacing-sec", type=float, default=0.25)
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform SEC hydration and shadow parsing. Default is plan-only.",
    )
    return parser.parse_args()


def _selected_strata(raw: str) -> tuple[AssessmentStratum, ...]:
    requested = {value.strip() for value in raw.split(",") if value.strip()}
    known = {stratum.name: stratum for stratum in ASSESSMENT_STRATA}
    unknown = sorted(requested - set(known))
    if unknown:
        raise ValueError(f"Unknown assessment strata: {unknown}")
    selected = tuple(stratum for stratum in ASSESSMENT_STRATA if stratum.name in requested)
    if not selected:
        raise ValueError("At least one assessment stratum must be selected")
    return selected


def _run_command(command: list[str], *, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="") as log:
        log.write(f"\n[{datetime.now(timezone.utc).isoformat()}] {' '.join(command)}\n")
        log.flush()
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}; see {log_path}"
        )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Expected a JSON object: {path}")
    return payload


def _integer(payload: dict[str, Any], key: str) -> int:
    return int(payload.get(key) or 0)


def _write_progress(
    *,
    output_dir: Path,
    asof_date: str,
    rows: list[dict[str, Any]],
    execution_mode: str,
) -> dict[str, Any]:
    report_dir = output_dir / asof_date
    selected = sum(int(row["selected_filing_count"]) for row in rows)
    complete = sum(int(row["complete_filing_count"]) for row in rows)
    parser_planned = sum(int(row["parser_planned_work_count"]) for row in rows)
    parser_completed = sum(int(row["parser_completed_work_count"]) for row in rows)
    parser_failed = sum(int(row["parser_failed_work_count"]) for row in rows)
    accepted = sum(int(row["accepted_evidence_count"]) for row in rows)
    review_required = sum(int(row["review_required_evidence_count"]) for row in rows)
    hydration_rate = complete / selected if selected else 0.0
    parser_denominator = parser_completed + parser_failed
    parser_rate = parser_completed / parser_denominator if parser_denominator else 0.0
    manifest = {
        "manifest_version": "software_parser_assessment_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asof_date": asof_date,
        "model_family": "software_infrastructure",
        "execution_mode": execution_mode,
        "completed_strata": [str(row["stratum"]) for row in rows],
        "selected_filing_count": selected,
        "complete_filing_count": complete,
        "incomplete_filing_count": selected - complete,
        "hydration_success_rate": round(hydration_rate, 8),
        "parser_planned_work_count": parser_planned,
        "parser_completed_work_count": parser_completed,
        "parser_failed_work_count": parser_failed,
        "parser_success_rate": round(parser_rate, 8),
        "accepted_evidence_count": accepted,
        "review_required_evidence_count": review_required,
        "hydration_gate_pass": int(selected > 0 and hydration_rate >= 0.98),
        "parser_gate_pass": int(
            execution_mode == "execute"
            and parser_denominator > 0
            and parser_rate >= 0.98
        ),
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
        "rows": rows,
    }
    if rows:
        atomic_csv(report_dir / "software_parser_assessment_by_stratum.csv", rows)
    atomic_json(report_dir / "software_parser_assessment_manifest.json", manifest)
    return manifest


def _hydration_command(
    *,
    args: argparse.Namespace,
    stratum: AssessmentStratum,
    stratum_output: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(HYDRATION_SCRIPT),
        "--asof",
        str(args.asof),
        "--forms",
        ",".join(stratum.forms),
        "--max-filings-per-ticker",
        "1",
        "--max-documents-per-filing",
        "0",
        "--cache-dir",
        str(args.cache_dir.expanduser().resolve()),
        "--output-dir",
        str(stratum_output),
        "--request-spacing-sec",
        str(max(0.1, args.request_spacing_sec)),
        "--timeout-sec",
        str(max(1.0, args.timeout_sec)),
        "--max-retries",
        str(max(1, args.max_retries)),
    ]
    if args.start_date:
        command.extend(("--start-date", str(args.start_date)))
    if args.tickers:
        command.extend(("--tickers", str(args.tickers)))
    if args.max_tickers > 0:
        command.extend(("--max-tickers", str(args.max_tickers)))
    if args.execute:
        command.append("--execute")
    return command


def _shadow_command(
    *,
    args: argparse.Namespace,
    source_manifest: Path,
    stratum_output: Path,
) -> list[str]:
    return [
        sys.executable,
        str(SHADOW_SCRIPT),
        "--asof",
        str(args.asof),
        "--source-manifest",
        str(source_manifest),
        "--cache-dir",
        str(args.cache_dir.expanduser().resolve()),
        "--output-dir",
        str(stratum_output / "parser"),
        "--workers",
        str(max(1, args.workers)),
    ]


def main() -> int:
    args = parse_args()
    strata = _selected_strata(args.strata)
    output_dir = args.output_dir.expanduser().resolve()
    rows: list[dict[str, Any]] = []
    metric_status_totals: Counter[str] = Counter()
    for stratum in strata:
        print(f"[{stratum.name}] hydrating forms={','.join(stratum.forms)}", flush=True)
        stratum_output = output_dir / stratum.name
        log_path = output_dir / str(args.asof) / "logs" / f"{stratum.name}.log"
        _run_command(
            _hydration_command(args=args, stratum=stratum, stratum_output=stratum_output),
            log_path=log_path,
        )
        hydration_path = (
            stratum_output
            / str(args.asof)
            / "hydration"
            / "software_parser_hydration_manifest.json"
        )
        hydration = _read_json(hydration_path)
        selected = _integer(hydration, "selected_filing_count")
        complete = _integer(hydration, "complete_filing_count")
        row: dict[str, Any] = {
            "stratum": stratum.name,
            "forms": ",".join(stratum.forms),
            "selected_filing_count": selected,
            "complete_filing_count": complete,
            "incomplete_filing_count": _integer(hydration, "incomplete_filing_count"),
            "sealed_document_count": _integer(hydration, "sealed_document_count"),
            "parser_run_id": 0,
            "parser_status": "not_run",
            "parser_planned_work_count": 0,
            "parser_completed_work_count": 0,
            "parser_failed_work_count": 0,
            "accepted_evidence_count": 0,
            "review_required_evidence_count": 0,
            "metric_status_counts_json": "{}",
            "recovery_class_counts_json": "{}",
            "hydration_manifest": str(hydration_path),
            "shadow_manifest": "",
        }
        if not args.execute:
            rows.append(row)
            _write_progress(
                output_dir=output_dir,
                asof_date=str(args.asof),
                rows=rows,
                execution_mode="plan_only",
            )
            continue
        if selected <= 0 or complete != selected:
            rows.append(row)
            _write_progress(
                output_dir=output_dir,
                asof_date=str(args.asof),
                rows=rows,
                execution_mode="execute",
            )
            raise RuntimeError(
                f"{stratum.name} hydration incomplete: complete={complete} selected={selected}"
            )
        source_manifest = Path(str(hydration["sealed_source_manifest_path"]))
        print(f"[{stratum.name}] shadow parsing {selected} accessions", flush=True)
        _run_command(
            _shadow_command(
                args=args,
                source_manifest=source_manifest,
                stratum_output=stratum_output,
            ),
            log_path=log_path,
        )
        shadow_path = (
            stratum_output
            / "parser"
            / str(args.asof)
            / "shadow"
            / "software_parser_shadow_run.json"
        )
        shadow = _read_json(shadow_path)
        funnel = dict(shadow.get("extraction_funnel") or {})
        run = dict(funnel.get("run") or {})
        status_counts = dict(funnel.get("evidence_status_counts") or {})
        metric_counts = dict(funnel.get("metric_status_counts") or {})
        recovery = dict(shadow.get("recovery_assessment") or {})
        recovery_counts = dict(recovery.get("recovery_class_counts") or {})
        for metric_name, counts in metric_counts.items():
            for status, count in dict(counts or {}).items():
                metric_status_totals[f"{metric_name}:{status}"] += int(count or 0)
        row.update(
            {
                "parser_run_id": int(shadow.get("run_id") or 0),
                "parser_status": str(run.get("status") or ""),
                "parser_planned_work_count": int(run.get("planned_work_count") or 0),
                "parser_completed_work_count": int(shadow.get("completed_work_count") or 0),
                "parser_failed_work_count": int(shadow.get("failed_work_count") or 0),
                "accepted_evidence_count": int(status_counts.get("ACCEPTED") or 0),
                "review_required_evidence_count": int(
                    status_counts.get("REVIEW_REQUIRED") or 0
                ),
                "metric_status_counts_json": json.dumps(metric_counts, sort_keys=True),
                "recovery_class_counts_json": json.dumps(
                    recovery_counts, sort_keys=True
                ),
                "shadow_manifest": str(shadow_path),
            }
        )
        rows.append(row)
        manifest = _write_progress(
            output_dir=output_dir,
            asof_date=str(args.asof),
            rows=rows,
            execution_mode="execute",
        )
        print(
            f"[{stratum.name}] complete={complete}/{selected} "
            f"parser_failed={row['parser_failed_work_count']}",
            flush=True,
        )
    manifest = _write_progress(
        output_dir=output_dir,
        asof_date=str(args.asof),
        rows=rows,
        execution_mode="execute" if args.execute else "plan_only",
    )
    manifest["metric_status_totals"] = dict(sorted(metric_status_totals.items()))
    atomic_json(
        output_dir / str(args.asof) / "software_parser_assessment_manifest.json",
        manifest,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    if args.execute and not (
        int(manifest["hydration_gate_pass"]) and int(manifest["parser_gate_pass"])
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
