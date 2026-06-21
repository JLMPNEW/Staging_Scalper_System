#!/usr/bin/env python3
"""Stage 1 - calibrate native sector scores onto the common expected-alpha contract.

Reads runs/<as_of>/collected_scores.csv, maps each sector's native composite to expected alpha,
assigns within-sector percentile + rating, resolves cross-sector duplicate tickers, and writes the
canonical runs/<as_of>/stocks_scores.csv (also upserted to the layer DB).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    CONTRACT_FIELDS, DEFAULT_RATING_BANDS, contract_version, expected_alpha, fail_if_exists,
    percentiles_within, rating_for_percentile, read_csv, upsert_stocks_scores, write_csv,
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


def assign_percentiles_and_ratings(rows: list[dict], bands: dict[str, float]) -> None:
    """Assign within-sector percentiles after duplicate resolution."""
    by_pipeline: dict[str, list[dict]] = {}
    for row in rows:
        by_pipeline.setdefault(str(row["source_pipeline"]), []).append(row)
    for sector_rows in by_pipeline.values():
        natives = [float(row["native_score"]) for row in sector_rows]
        for row, pct in zip(sector_rows, percentiles_within(natives)):
            row["within_sector_percentile"] = round(pct, 4)
            row["rating"] = rating_for_percentile(pct, bands)


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
    bands = {**DEFAULT_RATING_BANDS, **cfg_get(config, "score_contract.rating_bands", {})}

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

    # Calibrate native scores. Percentile/rating must wait until after duplicate resolution so each
    # name is ranked against the final sleeve population, not the pre-dedup collected population.
    by_pipeline: dict[str, list[dict]] = {}
    for row in collected:
        by_pipeline.setdefault(row["source_pipeline"], []).append(row)

    contract_rows: list[dict] = []
    for pipeline, rows in by_pipeline.items():
        calib = calib_by_family.get(pipeline, {})
        neutral = float(calib.get("neutral", 50.0))
        scale = float(calib.get("scale", 50.0))
        alpha_full = float(calib.get("expected_alpha_at_full", 0.15))
        for r in rows:
            native = float(r["native_score"])
            contract_rows.append({
                "as_of_date": run_as_of, "ticker": r["ticker"], "source_pipeline": pipeline,
                "sector": r["sector"], "industry": r["industry"], "industry_aggregate": r["industry_aggregate"],
                "final_score": round(
                    expected_alpha(native, neutral=neutral, scale=scale, expected_alpha_at_full=alpha_full),
                    6,
                ),
                "rating": "", "within_sector_percentile": "",
                "score_confidence": round(float(r["score_confidence"]), 4),
                "investable_eligible": int(r["investable_eligible"]),
                "eligibility_reason": r["eligibility_reason"], "native_score": native,
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
    assign_percentiles_and_ratings(final_rows, bands)
    final_rows = sorted(final_rows, key=lambda r: (r["source_pipeline"], -float(r["final_score"])))
    out_path = run_dir / "stocks_scores.csv"
    duplicate_path = run_dir / "validation" / "duplicate_resolution.csv"
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
