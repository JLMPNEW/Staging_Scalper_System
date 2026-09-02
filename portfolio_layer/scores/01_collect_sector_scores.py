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

from portfolio_layer.core.config import (  # noqa: E402
    active_score_sectors,
    cfg_get,
    load_yaml,
    resolve_path,
)
from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    COLLECTED_FIELDS,
    FINANCIAL_LINEAGE_FIELDS,
    fail_if_exists,
    manifest_accepts,
    read_csv,
    read_manifest,
    sha256_file,
    write_csv,
)
from portfolio_layer.core.db import add_issue, connect, finish_run, start_run  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.scores.adapters import (  # noqa: E402
    dated_candidates,
    run_adapter,
    validate_consumer_v3_optimizer_cap_binding,
    validate_consumer_v3_runtime_authority,
)


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
    parser.add_argument(
        "--reuse-sealed-run-raw",
        action="store_true",
        help="On a forced historical rebuild, reuse a missing sector source only when the same-date run "
        "manifest seals the archived raw bytes.",
    )
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
    invalidate_dependents(run_dir, "scores")


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


def sealed_run_raw_fallback(paths, *, run_as_of: str, pipeline: str) -> Path | None:
    """Return a same-date archived source only when the prior Stage 1 manifest proves its bytes."""
    run_dir = paths.output_dir / "runs" / run_as_of
    manifest_path = run_dir / "manifest.json"
    raw_name = f"{pipeline}_scores.csv"
    raw_path = run_dir / "raw" / raw_name
    if not manifest_path.exists() or not raw_path.exists():
        return None
    try:
        manifest = read_manifest(manifest_path)
    except ValueError:
        return None
    manifest_as_of = str(manifest.get("run_as_of", manifest.get("run_as_of_date", ""))).strip()
    if not manifest_accepts(manifest) or manifest_as_of != run_as_of:
        return None
    raw_entry = (manifest.get("raw") or {}).get(raw_name)
    expected = str(raw_entry.get("sha256", "")).strip() if isinstance(raw_entry, dict) else ""
    if not expected or sha256_file(raw_path) != expected:
        return None
    return raw_path


def _oos_valid_rows(path: Path) -> bool:
    return any(
        str(row.get("oos_score_valid_flag", "")).strip().casefold()
        in {"1", "1.0", "true", "yes", "y", "t"}
        for row in read_csv(path)
    )


def operational_oos_fallback_config(
    cfg: dict, sector_root: Path, *, run_as_of: str
) -> dict:
    """Select a prior production artifact during a bounded model-transition gap."""
    if str(cfg.get("file_mode", "flat")) != "dated":
        raise ValueError(
            f"Operational OOS fallback requires dated mode for {cfg.get('model_family')}"
        )
    candidates = dated_candidates(cfg, sector_root)
    target = run_as_of.replace("-", "")
    eligible = [candidate for candidate in candidates if candidate[0] <= target]
    if not eligible:
        raise FileNotFoundError(
            f"No dated score file for {cfg.get('model_family')} at or before {run_as_of}"
        )
    chosen = max(eligible, key=lambda candidate: candidate[0])
    if _oos_valid_rows(chosen[1]):
        return dict(cfg)
    tolerance = int(cfg.get("staleness_tolerance_days", 0))
    if tolerance < 0:
        raise ValueError("staleness_tolerance_days must be non-negative")
    target_date = parse_iso_date(run_as_of, label="run as-of")
    for candidate_date, candidate_path in sorted(eligible, reverse=True):
        candidate_iso = (
            f"{candidate_date[:4]}-{candidate_date[4:6]}-{candidate_date[6:]}"
        )
        age_days = (
            target_date - parse_iso_date(candidate_iso, label="candidate date")
        ).days
        if age_days < 0 or age_days > tolerance:
            continue
        if _oos_valid_rows(candidate_path):
            LOGGER.warning(
                "Sector %s artifact %s has no OOS-valid rows; using prior "
                "OOS-valid artifact %s within %d-day tolerance",
                cfg.get("model_family"),
                chosen[1],
                candidate_path,
                tolerance,
            )
            fallback_cfg = dict(cfg)
            fallback_cfg["file_mode"] = "flat"
            fallback_cfg["file_path"] = str(candidate_path)
            return fallback_cfg
    raise ValueError(
        f"No OOS-valid dated score file for {cfg.get('model_family')} at or before "
        f"{run_as_of} within {tolerance} calendar days"
    )


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

    try:
        all_enabled_sectors = active_score_sectors(config, None)
        sectors = active_score_sectors(config, args.as_of)
    except ValueError as exc:
        LOGGER.error("Invalid score-sector activation contract: %s", exc)
        return 1
    if not sectors:
        LOGGER.error("No score sectors are active for as_of=%s", args.as_of or "latest")
        return 1
    configured_min_successful = int(
        cfg_get(config, "score_contract.min_successful_sectors", len(all_enabled_sectors))
    )
    if not 1 <= configured_min_successful <= len(all_enabled_sectors):
        LOGGER.error(
            "score_contract.min_successful_sectors=%d is outside [1,%d]",
            configured_min_successful,
            len(all_enabled_sectors),
        )
        return 1
    min_successful = min(configured_min_successful, len(sectors))
    fallback_pipelines = {
        str(value).strip()
        for value in cfg_get(
            config, "score_contract.operational_oos_fallback_pipelines", []
        )
        if str(value).strip()
    }
    configured_pipelines = {
        str(sector.get("model_family", "unknown"))
        for sector in all_enabled_sectors
    }
    unknown_fallbacks = sorted(fallback_pipelines - configured_pipelines)
    if unknown_fallbacks:
        LOGGER.error(
            "Unknown operational_oos_fallback_pipelines entries: %s",
            unknown_fallbacks,
        )
        return 1
    required_pipelines = {
        str(s.get("model_family", "unknown"))
        for s in sectors
        if bool(s.get("required", True))
    }

    consumer_cfg = next(
        (
            sector
            for sector in sectors
            if str(sector.get("model_family") or "").strip()
            == "consumer_defensive"
        ),
        None,
    )
    if consumer_cfg is not None:
        try:
            validate_consumer_v3_optimizer_cap_binding(
                consumer_cfg, cfg_get(config, "optimizer", {})
            )
        except ValueError as exc:
            LOGGER.error("Consumer Defensive capital-control binding failed: %s", exc)
            return 1

    results = []
    raw_results = []
    failures: list[dict[str, str]] = []
    for sector_cfg in sectors:
        cfg = dict(sector_cfg)
        pipeline = str(cfg.get("model_family", "unknown"))
        if pipeline in fallback_pipelines:
            if not args.as_of:
                LOGGER.error(
                    "Operational OOS fallback requires an explicit --as-of for %s",
                    pipeline,
                )
                return 1
            cfg = operational_oos_fallback_config(
                cfg, sector_root, run_as_of=args.as_of
            )
        try:
            result = run_adapter(cfg, sector_root, args.as_of)
        except FileNotFoundError as exc:
            fallback = (
                sealed_run_raw_fallback(paths, run_as_of=args.as_of, pipeline=pipeline)
                if args.force and args.reuse_sealed_run_raw and args.as_of
                else None
            )
            if fallback is not None:
                fallback_cfg = dict(cfg)
                fallback_cfg["file_mode"] = "flat"
                fallback_cfg["file_path"] = str(fallback)
                result = run_adapter(fallback_cfg, sector_root, args.as_of)
                LOGGER.warning(
                    "Sector %s live source missing; reusing same-date manifest-sealed raw archive %s",
                    pipeline,
                    fallback,
                )
                failures.append({
                    "source_pipeline": pipeline,
                    "issue_type": "sealed_raw_replay",
                    "detail": f"{pipeline} rebuilt from manifest-sealed same-date raw archive {fallback}",
                })
            else:
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
    consumer_result = next(
        (
            result
            for result in results
            if result.source_pipeline == "consumer_defensive"
        ),
        None,
    )
    if consumer_cfg is not None and consumer_result is not None:
        try:
            validate_consumer_v3_runtime_authority(
                dict(consumer_cfg),
                sector_root,
                source_asof=consumer_result.source_asof_date,
                portfolio_asof=run_as_of,
            )
        except ValueError as exc:
            LOGGER.error("Consumer Defensive runtime authority failed: %s", exc)
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
    planned_raw: list[Path] = []
    for result in raw_results:
        sources = result.source_files or (result.source_file,)
        for index, source in enumerate(sources):
            name = (
                f"{result.source_pipeline}_scores.csv"
                if index == 0
                else f"{result.source_pipeline}_source_{index + 1:02d}_{source.name}"
            )
            planned_raw.append(raw_dir / name)
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
        preserved_sources = {
            source.resolve()
            for result in raw_results
            for source in (result.source_files or (result.source_file,))
            if source.parent.resolve() == raw_dir.resolve()
        }
        for path in raw_dir.glob("*.csv"):
            if path.resolve() not in preserved_sources:
                path.unlink()

    collected: list[dict] = []
    for result in raw_results:
        sources = result.source_files or (result.source_file,)
        for index, source in enumerate(sources):
            name = (
                f"{result.source_pipeline}_scores.csv"
                if index == 0
                else f"{result.source_pipeline}_source_{index + 1:02d}_{source.name}"
            )
            destination = raw_dir / name
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
    for result in results:
        for row in result.rows:
            collected.append({
                "as_of_date": run_as_of, "ticker": row.ticker, "source_pipeline": row.source_pipeline,
                "sector": row.sector, "industry": row.industry, "industry_aggregate": row.industry_aggregate,
                "model_scope_id": row.model_scope_id,
                "production_policy_id": row.production_policy_id,
                "production_policy_sha256": row.production_policy_sha256,
                "selection_reliability_class": row.selection_reliability_class,
                "active_sleeve_weight": row.active_sleeve_weight,
                "active_name_weight_cap": row.active_name_weight_cap,
                "active_selected_name_count": row.active_selected_name_count,
                "benchmark_residual_weight": row.benchmark_residual_weight,
                "benchmark_residual_ticker": row.benchmark_residual_ticker,
                "native_score": row.native_score, "investable_eligible": row.investable_eligible,
                "eligibility_reason": row.eligibility_reason, "score_confidence": row.score_confidence,
                "calibration_research_eligible": row.calibration_research_eligible,
                "calibration_research_reason": row.calibration_research_reason,
                "calibration_sample_role": row.calibration_sample_role,
                "stage1_sample_role": row.stage1_sample_role,
                "oos_score_valid_flag": row.oos_score_valid_flag,
                "missing_score_flag": row.missing_score_flag,
                "survivorship_corrected_panel_flag": row.survivorship_corrected_panel_flag,
                "source_asof_date": row.source_asof_date,
                **{
                    field: getattr(row, field) for field in FINANCIAL_LINEAGE_FIELDS
                },
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
