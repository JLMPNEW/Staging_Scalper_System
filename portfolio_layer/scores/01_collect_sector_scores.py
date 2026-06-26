#!/usr/bin/env python3
"""Stage 1 - collect each sector's published scores into one immutable per-as-of run folder.

Reads each enabled sector's native CSV via its adapter, copies the raw file into runs/<as_of>/raw/
for provenance, and writes runs/<as_of>/collected_scores.csv (native, pre-calibration).
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import COLLECTED_FIELDS, fail_if_exists, write_csv  # noqa: E402
from portfolio_layer.core.db import add_issue, connect, finish_run, start_run  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


LOGGER = logging.getLogger("collect_sector_scores")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect sector scores into a dated run folder.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--as-of",
        type=iso_date_arg,
        default=None,
        help="Run as-of date YYYY-MM-DD (default: max source date).",
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="Overwrite artifacts for an existing as-of run.")
    return parser.parse_args()


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_iso_date(raw: str, *, label: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD, got {raw!r}") from exc


def invalidate_downstream_artifacts(run_dir: Path) -> None:
    """Remove artifacts derived from collected_scores.csv before force-recollecting."""
    for rel in ("stocks_scores.csv", "manifest.json"):
        path = run_dir / rel
        if path.exists():
            path.unlink()
    validation_dir = run_dir / "validation"
    if validation_dir.exists():
        for path in validation_dir.iterdir():
            if path.is_file():
                path.unlink()


def refresh_collect_issues(conn, run_as_of: str, failures: list[dict[str, str]]) -> None:
    with conn:
        conn.execute(
            "DELETE FROM data_quality_issues WHERE stage = ? AND detail LIKE ?",
            ("stage1_collect", f"%as_of={run_as_of}%"),
        )
    for failure in failures:
        add_issue(
            conn,
            stage="stage1_collect",
            source_id=failure["source_pipeline"],
            issue_type=failure["issue_type"],
            detail=f"as_of={run_as_of}; {failure['detail']}",
            severity="warning",
        )


def clear_contract_rows(conn, run_as_of: str) -> None:
    """Remove downstream DB contract rows when force-recollecting a run."""
    try:
        with conn:
            conn.execute("DELETE FROM stocks_scores WHERE run_as_of_date = ?", (run_as_of,))
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    sector_root = resolve_path(
        cfg_get(config, "score_contract.sector_output_root", "../output"),
        base_dir=config_path.parent,
    )
    try:
        db_path = resolve_database_path(paths, args.db)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    sectors = [s for s in cfg_get(config, "score_contract.sectors", []) if bool(s.get("enabled", True))]
    if not sectors:
        LOGGER.error("No enabled sectors in score_contract.sectors")
        return 1
    min_successful = int(cfg_get(config, "score_contract.min_successful_sectors", len(sectors)))
    required_pipelines = {
        str(s.get("model_family", "unknown"))
        for s in sectors
        if bool(s.get("required", True))
    }

    results = []
    raw_results = []
    failures: list[dict[str, str]] = []
    for cfg in sectors:
        pipeline = str(cfg.get("model_family", "unknown"))
        try:
            result = run_adapter(cfg, sector_root, args.as_of)
        except FileNotFoundError as exc:
            LOGGER.warning("Sector %s skipped: %s", pipeline, exc)
            failures.append({
                "source_pipeline": pipeline,
                "issue_type": "sector_collect_failed",
                "detail": f"{pipeline} skipped during collect: {exc}",
            })
            continue
        raw_results.append(result)
        if not result.rows:
            LOGGER.warning("Sector %s skipped: adapter produced zero contract rows", result.source_pipeline)
            failures.append({
                "source_pipeline": result.source_pipeline,
                "issue_type": "empty_sector_scores",
                "detail": f"{result.source_pipeline} produced zero contract rows",
            })
            continue
        results.append(result)
        LOGGER.info("Adapter %-22s rows=%-4d eligible=%-4d source=%s", result.source_pipeline,
                    len(result.rows), sum(r.investable_eligible for r in result.rows), result.source_asof_date)

    run_as_of = args.as_of or max((r.source_asof_date for r in results if r.source_asof_date), default="")
    if len(results) < min_successful:
        issue_date = run_as_of or "unknown"
        LOGGER.error(
            "Only %d/%d sectors collected successfully; minimum required is %d",
            len(results),
            len(sectors),
            min_successful,
        )
        with connect(db_path) as conn:
            run_id = start_run(conn, run_type="collect_sector_scores", input_path=config_path)
            refresh_collect_issues(conn, issue_date, failures)
            finish_run(
                conn,
                run_id=run_id,
                status="failed",
                row_count=0,
                message=f"as_of={issue_date} successful_sectors={len(results)} min_required={min_successful}",
            )
        return 1

    successful_pipelines = {r.source_pipeline for r in results}
    missing_required = sorted(required_pipelines - successful_pipelines)
    if missing_required:
        issue_date = run_as_of or "unknown"
        LOGGER.error("Required sectors missing or empty for %s: %s", issue_date, missing_required)
        audit_failures = list(failures)
        for pipeline in missing_required:
            audit_failures.append({
                "source_pipeline": pipeline,
                "issue_type": "required_sector_missing",
                "detail": f"required sector {pipeline} missing or empty for as_of={issue_date}",
            })
        with connect(db_path) as conn:
            run_id = start_run(conn, run_type="collect_sector_scores", input_path=config_path)
            refresh_collect_issues(conn, issue_date, audit_failures)
            finish_run(
                conn,
                run_id=run_id,
                status="failed",
                row_count=0,
                message=f"as_of={issue_date} missing_required={','.join(missing_required)}",
            )
        return 1

    if not run_as_of:
        LOGGER.error("Could not determine run as-of date (no source dates and no --as-of)")
        return 1
    try:
        run_date = parse_iso_date(run_as_of, label="run as-of")
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    future_sources: set[str] = set()
    for result in results:
        for row in result.rows:
            if not row.source_asof_date:
                future_sources.add(f"{result.source_pipeline}:{row.ticker}:missing_source_asof")
                continue
            try:
                source_date = parse_iso_date(row.source_asof_date, label=f"{result.source_pipeline} source as-of")
            except ValueError as exc:
                future_sources.add(f"{result.source_pipeline}:{row.ticker}:invalid_source_asof:{exc}")
                continue
            if source_date > run_date:
                future_sources.add(f"{result.source_pipeline}:{row.ticker}:{row.source_asof_date}")
    if future_sources:
        LOGGER.error("Refusing to collect future/missing source dates for run %s: %s", run_as_of, sorted(future_sources))
        audit_failures = list(failures)
        for source in sorted(future_sources):
            pipeline = source.split(":", 1)[0]
            audit_failures.append({
                "source_pipeline": pipeline,
                "issue_type": "future_or_invalid_source_date",
                "detail": f"{pipeline} has invalid source date for as_of={run_as_of}: {source}",
            })
        with connect(db_path) as conn:
            run_id = start_run(conn, run_type="collect_sector_scores", input_path=config_path)
            refresh_collect_issues(conn, run_as_of, audit_failures)
            finish_run(
                conn,
                run_id=run_id,
                status="failed",
                row_count=0,
                message=f"as_of={run_as_of} future_or_invalid_sources={len(future_sources)}",
            )
        return 1

    run_dir = paths.output_dir / "runs" / run_as_of
    raw_dir = run_dir / "raw"
    planned_raw = [raw_dir / f"{result.source_pipeline}_scores.csv" for result in raw_results]
    planned_artifacts = [run_dir / "collected_scores.csv", *planned_raw]
    try:
        fail_if_exists(planned_artifacts, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    if args.force:
        invalidate_downstream_artifacts(run_dir)
    (run_dir / "validation").mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    if args.force:
        for path in raw_dir.glob("*.csv"):
            path.unlink()

    collected: list[dict] = []
    for result in raw_results:
        shutil.copy2(result.source_file, raw_dir / f"{result.source_pipeline}_scores.csv")
    for result in results:
        for row in result.rows:
            collected.append({
                "as_of_date": run_as_of, "ticker": row.ticker, "source_pipeline": row.source_pipeline,
                "sector": row.sector, "industry": row.industry, "industry_aggregate": row.industry_aggregate,
                "native_score": row.native_score, "investable_eligible": row.investable_eligible,
                "eligibility_reason": row.eligibility_reason, "score_confidence": row.score_confidence,
                "source_asof_date": row.source_asof_date,
            })

    out_path = run_dir / "collected_scores.csv"
    n = write_csv(out_path, COLLECTED_FIELDS, collected)

    with connect(db_path) as conn:
        if args.force:
            clear_contract_rows(conn, run_as_of)
        run_id = start_run(conn, run_type="collect_sector_scores", input_path=config_path)
        refresh_collect_issues(conn, run_as_of, failures)
        finish_run(
            conn,
            run_id=run_id,
            status="success",
            row_count=n,
            message=f"as_of={run_as_of} sectors={len(results)} skipped={len(failures)}",
        )

    LOGGER.info("Collected %d rows from %d sectors (%d skipped) -> %s", n, len(results), len(failures), out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
