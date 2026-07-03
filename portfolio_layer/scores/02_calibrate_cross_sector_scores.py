#!/usr/bin/env python3
"""Stage 1 - calibrate native sector scores onto the common expected-alpha contract.

Reads runs/<as_of>/collected_scores.csv, maps each sector's native composite to expected alpha,
assigns within-sector percentile + rating, resolves cross-sector duplicate tickers, and writes the
canonical runs/<as_of>/stocks_scores.csv (also upserted to the layer DB).
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from statistics import median
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    CONTRACT_FIELDS, DEFAULT_RATING_BANDS, contract_version, expected_alpha, fail_if_exists,
    percentiles_within, rating_for_percentile, read_csv, upsert_stocks_scores, validate_rating_bands, write_csv,
)
from portfolio_layer.core.db import add_issue, connect, finish_run, start_run  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402


LOGGER = logging.getLogger("calibrate_cross_sector_scores")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate collected scores into the stocks_scores contract.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=iso_date_arg, default=None, help="Run as-of date (default: latest run folder).")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="Overwrite artifacts for an existing as-of run.")
    return parser.parse_args()


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def latest_run(runs_root: Path) -> str | None:
    if not runs_root.exists():
        return None
    dates = sorted(p.name for p in runs_root.iterdir() if p.is_dir() and (p / "collected_scores.csv").exists())
    return dates[-1] if dates else None


def staleness_days(run_as_of: str, source_asof: str) -> int | None:
    try:
        return (date.fromisoformat(run_as_of) - date.fromisoformat(source_asof)).days
    except (ValueError, TypeError):
        return None


def parse_finite(value: object, label: str) -> float:
    try:
        raw = str(value).strip() if value is not None else ""
        parsed = float(raw) if raw else float("nan")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric, got {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return parsed


def row_flag(row: dict, field: str) -> int:
    try:
        return 1 if int(float(str(row.get(field, "0")).strip() or "0")) != 0 else 0
    except (TypeError, ValueError):
        return 0


def resolve_calibration_anchor(value: object, label: str, rows: list[dict[str, str]]) -> float:
    """Resolve numeric calibration anchors; `median` centers each sector on its current scored population."""
    raw = str(value).strip().lower() if value is not None else ""
    if raw == "median":
        values = []
        for r in rows:
            parsed = parse_finite(r.get("native_score"), f"{label}:native_score")
            if not row_flag(r, "missing_score_flag"):
                values.append(parsed)
        if not values:
            raise ValueError(f"{label}=median requires at least one finite non-missing native_score")
        return float(median(values))
    return parse_finite(value, label)


def assign_percentiles_and_ratings(rows: list[dict], bands: dict[str, float]) -> None:
    """Assign within-sector percentiles after duplicate resolution."""
    by_pipeline: dict[str, list[dict]] = {}
    for row in rows:
        by_pipeline.setdefault(str(row["source_pipeline"]), []).append(row)
    for sector_rows in by_pipeline.values():
        ranked_rows = [row for row in sector_rows if not row_flag(row, "missing_score_flag")]
        natives = [float(row["native_score"]) for row in ranked_rows]
        for row in sector_rows:
            if row_flag(row, "missing_score_flag"):
                row["within_sector_percentile"] = 0.0
                row["rating"] = "avoid"
        for row, pct in zip(ranked_rows, percentiles_within(natives)):
            row["within_sector_percentile"] = round(pct, 4)
            row["rating"] = rating_for_percentile(pct, bands)


def invalidate_downstream_artifacts(run_dir: Path) -> None:
    """Calibration changes invalidate the sealed Stage 1 manifest and validation outputs."""
    for path in (run_dir / "manifest.json",):
        if path.exists():
            path.unlink()
    validation_dir = run_dir / "validation"
    if validation_dir.exists():
        for path in validation_dir.iterdir():
            if path.is_file():
                path.unlink()


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    score_version = contract_version(config)
    paths = resolve_runtime_paths(config, config_path)
    try:
        db_path = resolve_database_path(paths, args.db)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    runs_root = paths.output_dir / "runs"

    run_as_of = args.as_of or latest_run(runs_root)
    if not run_as_of:
        LOGGER.error("No run folder with collected_scores.csv found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    collected_path = run_dir / "collected_scores.csv"
    if not collected_path.exists():
        LOGGER.error("collected_scores.csv not found for %s: %s", run_as_of, collected_path)
        return 1
    collected = read_csv(collected_path)
    if not collected:
        LOGGER.error("collected_scores.csv is empty for %s", run_as_of)
        return 1

    calib_by_family = {
        str(s["model_family"]): dict(s.get("calibration", {}))
        for s in cfg_get(config, "score_contract.sectors", [])
    }
    global_native_range = dict(cfg_get(config, "score_contract.native_score_range", {}) or {})
    native_range_by_family = {
        str(s["model_family"]): {**global_native_range, **dict(s.get("native_score_range", {}) or {})}
        for s in cfg_get(config, "score_contract.sectors", [])
    }
    bands = {**DEFAULT_RATING_BANDS, **cfg_get(config, "score_contract.rating_bands", {})}
    band_errors = validate_rating_bands(bands)
    if band_errors:
        LOGGER.error("Invalid score_contract.rating_bands: %s", band_errors)
        return 1
    try:
        max_abs_expected_alpha = parse_finite(
            cfg_get(config, "score_contract.max_abs_expected_alpha", 1.0),
            "score_contract.max_abs_expected_alpha",
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    # Optional canonical-pipeline overrides (ticker -> source_pipeline) to settle cross-sector duplicates.
    overrides: dict[str, str] = {}
    ov_rel = cfg_get(config, "score_contract.canonical_pipeline_overrides_csv", None)
    if ov_rel is None:
        ov_rel = cfg_get(config, "score_contract.canonical_sector_overrides_csv", None)
    if ov_rel:
        ov_path = resolve_path(ov_rel, base_dir=config_path.parent)
        if ov_path.exists():
            for r in read_csv(ov_path):
                t = str(r.get("ticker", "")).strip().upper()
                pipeline = str(r.get("canonical_pipeline", "") or r.get("canonical_sector", "")).strip()
                if t and pipeline:
                    overrides[t] = pipeline
        else:
            LOGGER.warning("canonical pipeline overrides CSV not found: %s", ov_path)
    invalid_overrides = sorted({pipeline for pipeline in overrides.values() if pipeline not in calib_by_family})
    if invalid_overrides:
        LOGGER.error("canonical pipeline overrides contain unknown pipelines: %s", invalid_overrides)
        return 1

    # Calibrate native scores. Percentile/rating must wait until after duplicate resolution so each
    # name is ranked against the final sleeve population, not the pre-dedup collected population.
    by_pipeline: dict[str, list[dict]] = {}
    for row in collected:
        by_pipeline.setdefault(row["source_pipeline"], []).append(row)
    unknown_pipelines = sorted(set(by_pipeline) - set(calib_by_family))
    if unknown_pipelines:
        LOGGER.error("Unknown source_pipeline values in collected_scores.csv: %s", unknown_pipelines)
        return 1

    contract_rows: list[dict] = []
    for pipeline, rows in by_pipeline.items():
        calib = calib_by_family.get(pipeline, {})
        try:
            neutral = resolve_calibration_anchor(calib.get("neutral", 50.0), f"{pipeline}:calibration.neutral", rows)
            scale = parse_finite(calib.get("scale", 50.0), f"{pipeline}:calibration.scale")
            alpha_full = parse_finite(
                calib.get("expected_alpha_at_full", 0.15),
                f"{pipeline}:calibration.expected_alpha_at_full",
            )
            native_min = parse_finite(
                native_range_by_family.get(pipeline, {}).get("min", 0.0),
                f"{pipeline}:native_score_range.min",
            )
            native_max = parse_finite(
                native_range_by_family.get(pipeline, {}).get("max", 100.0),
                f"{pipeline}:native_score_range.max",
            )
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 1
        if native_min > native_max:
            LOGGER.error("%s native_score_range min %.6f exceeds max %.6f", pipeline, native_min, native_max)
            return 1
        for r in rows:
            ticker = str(r.get("ticker", "<missing>"))
            try:
                native = parse_finite(r.get("native_score"), f"{pipeline}:{ticker}:native_score")
                score_confidence = parse_finite(
                    r.get("score_confidence"),
                    f"{pipeline}:{ticker}:score_confidence",
                )
                final_score = expected_alpha(
                    native, neutral=neutral, scale=scale, expected_alpha_at_full=alpha_full,
                )
            except ValueError as exc:
                LOGGER.error("%s", exc)
                return 1
            if native < native_min or native > native_max:
                LOGGER.error(
                    "%s:%s native_score %.6f outside configured range [%.6f, %.6f]",
                    pipeline,
                    ticker,
                    native,
                    native_min,
                    native_max,
                )
                return 1
            if abs(final_score) > max_abs_expected_alpha:
                LOGGER.error(
                    "%s:%s final_score %.6f exceeds max_abs_expected_alpha %.6f",
                    pipeline,
                    ticker,
                    final_score,
                    max_abs_expected_alpha,
                )
                return 1
            missing_score_flag = row_flag(r, "missing_score_flag")
            contract_rows.append({
                "as_of_date": run_as_of, "ticker": r["ticker"], "source_pipeline": pipeline,
                "sector": r["sector"], "industry": r["industry"], "industry_aggregate": r["industry_aggregate"],
                "final_score": round(0.0 if missing_score_flag else final_score, 6),
                "rating": "", "within_sector_percentile": "",
                "score_confidence": round(score_confidence, 4),
                "investable_eligible": int(r["investable_eligible"]),
                "eligibility_reason": r["eligibility_reason"], "native_score": native,
                "calibration_research_eligible": int(r.get("calibration_research_eligible") or 0),
                "calibration_research_reason": str(r.get("calibration_research_reason") or "").strip()
                or ("ok" if int(r.get("calibration_research_eligible") or 0) else "not_calibration_research_eligible"),
                "calibration_sample_role": str(r.get("calibration_sample_role") or "excluded").strip() or "excluded",
                "stage1_sample_role": str(
                    r.get("stage1_sample_role") or r.get("calibration_sample_role") or "excluded"
                ).strip() or "excluded",
                "oos_score_valid_flag": int(r.get("oos_score_valid_flag") or 0),
                "missing_score_flag": missing_score_flag,
                "survivorship_corrected_panel_flag": row_flag(r, "survivorship_corrected_panel_flag"),
                "source_asof_date": r["source_asof_date"],
                "staleness_days": staleness_days(run_as_of, r["source_asof_date"]),
                "score_version": score_version,
            })

    # Resolve duplicate tickers deterministically: canonical override for cross-sector duplicates, then
    # highest confidence, then config order. Same-pipeline duplicates are tracked separately.
    order = {str(s["model_family"]): i for i, s in enumerate(cfg_get(config, "score_contract.sectors", []))}
    best: dict[str, dict] = {}
    cross_duplicates = 0
    intra_duplicates = 0
    overrides_applied = 0
    duplicate_rows: list[dict] = []
    for row in contract_rows:
        key = row["ticker"]
        cur = best.get(key)
        if cur is None:
            best[key] = row
            continue
        same_pipeline = row["source_pipeline"] == cur["source_pipeline"]
        if same_pipeline:
            intra_duplicates += 1
        else:
            cross_duplicates += 1
        canon = overrides.get(key)
        candidates = sorted({str(cur["source_pipeline"]), str(row["source_pipeline"])})
        selected_before = str(cur["source_pipeline"])
        duplicate_type = "intra_sector" if same_pipeline else "cross_sector"
        method = "intra_sector_confidence" if same_pipeline else "confidence_then_config_order"
        if not same_pipeline and canon and (row["source_pipeline"] == canon) != (cur["source_pipeline"] == canon):
            # exactly one candidate matches the canonical pipeline -> that one wins, ignore confidence/order
            if row["source_pipeline"] == canon:
                best[key] = row
            overrides_applied += 1
            method = "canonical_override"
            duplicate_rows.append({
                "ticker": key,
                "duplicate_type": duplicate_type,
                "candidates": "|".join(candidates),
                "selected_pipeline": best[key]["source_pipeline"],
                "previous_pipeline": selected_before,
                "method": method,
                "canonical_pipeline": canon,
            })
            continue
        challenger_key = (float(row["score_confidence"]), -order.get(row["source_pipeline"], 999))
        incumbent_key = (float(cur["score_confidence"]), -order.get(cur["source_pipeline"], 999))
        if challenger_key > incumbent_key:
            best[key] = row
        duplicate_rows.append({
            "ticker": key,
            "duplicate_type": duplicate_type,
            "candidates": "|".join(candidates),
            "selected_pipeline": best[key]["source_pipeline"],
            "previous_pipeline": selected_before,
            "method": method,
            "canonical_pipeline": canon or "",
        })

    final_rows = list(best.values())
    final_by_pipeline: dict[str, list[dict]] = {}
    for row in final_rows:
        final_by_pipeline.setdefault(str(row["source_pipeline"]), []).append(row)
    for pipeline, rows in final_by_pipeline.items():
        calib = calib_by_family.get(pipeline, {})
        try:
            neutral = resolve_calibration_anchor(calib.get("neutral", 50.0), f"{pipeline}:calibration.neutral", rows)
            scale = parse_finite(calib.get("scale", 50.0), f"{pipeline}:calibration.scale")
            alpha_full = parse_finite(
                calib.get("expected_alpha_at_full", 0.15),
                f"{pipeline}:calibration.expected_alpha_at_full",
            )
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 1
        for row in rows:
            if row_flag(row, "missing_score_flag"):
                row["final_score"] = 0.0
                continue
            final_score = expected_alpha(
                float(row["native_score"]),
                neutral=neutral,
                scale=scale,
                expected_alpha_at_full=alpha_full,
            )
            if abs(final_score) > max_abs_expected_alpha:
                LOGGER.error(
                    "%s:%s final_score %.6f exceeds max_abs_expected_alpha %.6f after dedup calibration",
                    pipeline,
                    row["ticker"],
                    final_score,
                    max_abs_expected_alpha,
                )
                return 1
            row["final_score"] = round(final_score, 6)
    assign_percentiles_and_ratings(final_rows, bands)
    final_rows = sorted(final_rows, key=lambda r: (r["source_pipeline"], -float(r["final_score"])))
    out_path = run_dir / "stocks_scores.csv"
    duplicate_path = run_dir / "validation" / "duplicate_resolution.csv"
    if args.force:
        invalidate_downstream_artifacts(run_dir)
    try:
        fail_if_exists([out_path, duplicate_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    n = write_csv(out_path, CONTRACT_FIELDS, final_rows)
    write_csv(
        duplicate_path,
        [
            "ticker", "duplicate_type", "candidates", "selected_pipeline", "previous_pipeline",
            "method", "canonical_pipeline",
        ],
        sorted(duplicate_rows, key=lambda r: str(r["ticker"])),
    )

    with connect(db_path) as conn:
        run_id = start_run(conn, run_type="calibrate_cross_sector_scores", input_path=collected_path)
        upsert_stocks_scores(conn, run_as_of, final_rows)
        with conn:
            conn.execute(
                """
                DELETE FROM data_quality_issues
                WHERE stage = ?
                  AND issue_type IN (?, ?)
                  AND detail LIKE ?
                """,
                (
                    "stage1_calibrate",
                    "cross_sector_duplicate_ticker",
                    "intra_sector_duplicate_ticker",
                    f"%as_of={run_as_of}",
                ),
            )
        if cross_duplicates:
            detail = f"resolved {cross_duplicates} cross-sector duplicate ticker rows for as_of={run_as_of}"
            add_issue(
                conn, stage="stage1_calibrate", issue_type="cross_sector_duplicate_ticker",
                detail=detail, severity="warning",
            )
        if intra_duplicates:
            detail = f"resolved {intra_duplicates} intra-sector duplicate ticker rows for as_of={run_as_of}"
            add_issue(
                conn, stage="stage1_calibrate", issue_type="intra_sector_duplicate_ticker",
                detail=detail, severity="warning",
            )
        finish_run(conn, run_id=run_id, status="success", row_count=n,
                   message=(
                       f"as_of={run_as_of} rows={n} cross_duplicates={cross_duplicates} "
                       f"intra_duplicates={intra_duplicates} "
                       f"overrides_applied={overrides_applied}"
                   ))

    eligible = sum(int(r["investable_eligible"]) for r in final_rows)
    LOGGER.info(
        "Calibrated %d names (%d eligible, %d cross duplicates, %d intra duplicates, "
        "%d via canonical override) -> %s",
        n, eligible, cross_duplicates, intra_duplicates, overrides_applied, out_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
