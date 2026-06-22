#!/usr/bin/env python3
"""Stage 1 - validate the stocks_scores contract and seal the run manifest.

Hard gates (fail the build): schema, non-null keys, single as-of, ticker uniqueness, monotonic
final_score vs native within sector, eligibility populated. Staleness is a warning. Cross-sector
calibration parity and per-sector realized-return IC are DEFERRED to Stage 2 (they need the return panel).
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import date, timezone, datetime
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    CONTRACT_FIELDS, DEFAULT_RATING_BANDS, contract_version, fail_if_exists, percentiles_within,
    rating_for_percentile, read_csv, sha256_file, write_csv, write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402


LOGGER = logging.getLogger("validate_score_contract")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
KEY_FIELDS = ("ticker", "as_of_date", "source_pipeline", "sector")
FLOAT_FIELDS = ("final_score", "within_sector_percentile", "score_confidence", "native_score")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the stocks_scores contract for a run.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=iso_date_arg, default=None, help="Run as-of date (default: latest run folder).")
    parser.add_argument("--db", type=Path, default=None, help="Optional DB path to apply the same PROD guard.")
    parser.add_argument("--force", action="store_true", help="Overwrite validation and manifest artifacts.")
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
    dates = sorted(p.name for p in runs_root.iterdir() if p.is_dir() and (p / "stocks_scores.csv").exists())
    return dates[-1] if dates else None


def parse_float(value: object) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        parsed = float(str(value).strip())
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def parse_iso_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def main() -> int:  # noqa: C901 - linear sequence of acceptance checks
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    score_version = contract_version(config)
    paths = resolve_runtime_paths(config, config_path)
    try:
        resolve_database_path(paths, args.db)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run(runs_root)
    if not run_as_of:
        LOGGER.error("No run folder with stocks_scores.csv found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    scores_path = run_dir / "stocks_scores.csv"
    if not scores_path.exists():
        LOGGER.error("stocks_scores.csv not found for %s: %s", run_as_of, scores_path)
        return 1
    validation_path = run_dir / "validation" / "score_contract_validation.csv"
    manifest_path = run_dir / "manifest.json"
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    rows = read_csv(scores_path)
    tolerance = int(cfg_get(config, "score_contract.staleness_tolerance_days", 10))
    bands = {**DEFAULT_RATING_BANDS, **cfg_get(config, "score_contract.rating_bands", {})}

    checks: list[dict] = []

    def record(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # 1. schema
    header = set(rows[0].keys()) if rows else set()
    missing = [f for f in CONTRACT_FIELDS if f not in header]
    record("schema_complete", "PASS" if not missing and rows else "FAIL",
           "all contract fields present" if not missing and rows else f"missing/empty: {missing or 'no rows'}")

    # 2. non-null keys
    bad_keys = [r["ticker"] for r in rows if any(not str(r.get(k, "")).strip() for k in KEY_FIELDS)]
    record("non_null_keys", "PASS" if not bad_keys else "FAIL",
           "all key fields populated" if not bad_keys else f"{len(bad_keys)} rows with empty keys")

    # 3. single as-of
    asofs = {r["as_of_date"] for r in rows}
    record("single_as_of", "PASS" if asofs == {run_as_of} else "FAIL", f"as_of values={sorted(asofs)}")

    # 4. configured contract version
    versions = {str(r.get("score_version", "")).strip() for r in rows}
    record("score_version_matches_config", "PASS" if versions == {score_version} else "FAIL",
           f"expected={score_version}; found={sorted(versions)}")

    # 5. ticker uniqueness
    seen: dict[str, int] = {}
    for r in rows:
        seen[r["ticker"]] = seen.get(r["ticker"], 0) + 1
    dups = sorted(t for t, c in seen.items() if c > 1)
    record("ticker_uniqueness", "PASS" if not dups else "FAIL",
           "one row per ticker" if not dups else f"duplicates: {dups[:10]}")

    # 6. numeric contract fields and ranges
    bad_numeric: list[str] = []
    for r in rows:
        ticker = r.get("ticker", "<missing>")
        for field in FLOAT_FIELDS:
            value = parse_float(r.get(field))
            if value is None:
                bad_numeric.append(f"{ticker}:{field}=missing_or_non_numeric")
                continue
            if field == "score_confidence" and not 0.0 <= value <= 1.0:
                bad_numeric.append(f"{ticker}:score_confidence={value}")
            if field == "within_sector_percentile" and not 0.0 <= value <= 100.0:
                bad_numeric.append(f"{ticker}:within_sector_percentile={value}")
        if str(r.get("investable_eligible", "")).strip() not in ("0", "1"):
            bad_numeric.append(f"{ticker}:investable_eligible={r.get('investable_eligible')}")
        if parse_float(r.get("staleness_days")) is None:
            bad_numeric.append(f"{ticker}:staleness_days=missing_or_non_numeric")
    record("numeric_fields_valid", "PASS" if not bad_numeric else "FAIL",
           "numeric fields parse and ranges hold" if not bad_numeric else (
               f"{len(bad_numeric)} bad values; first={bad_numeric[:10]}"
           ))

    # 7. monotonic final_score vs native within sector
    by_pipe_rows: dict[str, list[dict]] = {}
    for r in rows:
        by_pipe_rows.setdefault(r["source_pipeline"], []).append(r)
    non_monotone = []
    for pipe, srows in by_pipe_rows.items():
        pairs = [
            (native, final)
            for native, final in (
                (parse_float(r.get("native_score")), parse_float(r.get("final_score")))
                for r in srows
            )
            if native is not None and final is not None
        ]
        ordered = sorted(pairs, key=lambda p: p[0])
        if any(ordered[i][1] < ordered[i - 1][1] - 1e-9 for i in range(1, len(ordered))):
            non_monotone.append(pipe)
    record("monotonic_calibration", "PASS" if not non_monotone else "FAIL",
           "final_score monotonic in native per sector" if not non_monotone else f"violations: {non_monotone}")

    # 8. percentile/rating computed against final deduplicated sector population
    bad_pct_rating: list[str] = []
    for pipe, srows in by_pipe_rows.items():
        native_values: list[float] = []
        valid_rows: list[dict] = []
        for r in srows:
            native = parse_float(r.get("native_score"))
            if native is None:
                bad_pct_rating.append(f"{r.get('ticker', '<missing>')}:{pipe}:native_score_unparseable")
                continue
            native_values.append(native)
            valid_rows.append(r)
        if not valid_rows:
            continue
        for r, pct in zip(valid_rows, percentiles_within(native_values)):
            expected_pct = round(pct, 4)
            actual_pct = parse_float(r.get("within_sector_percentile"))
            if actual_pct is None or abs(actual_pct - expected_pct) > 0.0001:
                bad_pct_rating.append(
                    f"{r['ticker']}:{pipe}:pct expected={expected_pct} actual={actual_pct}"
                )
            expected_rating = rating_for_percentile(pct, bands)
            if str(r.get("rating", "")).strip() != expected_rating:
                bad_pct_rating.append(
                    f"{r['ticker']}:{pipe}:rating expected={expected_rating} actual={r.get('rating')}"
                )
    record("percentile_rating_final_population", "PASS" if not bad_pct_rating else "FAIL",
           "percentile/rating recompute from final sector populations" if not bad_pct_rating else (
               f"{len(bad_pct_rating)} mismatches; first={bad_pct_rating[:10]}"
           ))

    # 9. eligibility populated
    bad_elig = [
        r["ticker"]
        for r in rows
        if str(r.get("investable_eligible", "")).strip() not in ("0", "1")
        or not str(r.get("eligibility_reason", "")).strip()
    ]
    record("eligibility_populated", "PASS" if not bad_elig else "FAIL",
           "investable_eligible in {0,1} with reason" if not bad_elig else f"{len(bad_elig)} rows bad")

    # 10. point-in-time source dates and staleness
    run_date = parse_iso_date(run_as_of)
    stale = []
    future = []
    bad_dates = []
    for r in rows:
        source_date = parse_iso_date(r.get("source_asof_date"))
        if run_date is None or source_date is None:
            bad_dates.append(r.get("ticker", "<missing>"))
            continue
        d_float = parse_float(r.get("staleness_days"))
        if d_float is None:
            bad_dates.append(r.get("ticker", "<missing>"))
            continue
        d = int(d_float)
        if source_date > run_date or d < 0:
            future.append((r["source_pipeline"], r["ticker"], r.get("source_asof_date", ""), d))
        if d > tolerance:
            stale.append((r["source_pipeline"], d))
    record("source_dates_valid", "PASS" if not bad_dates else "FAIL",
           "run/source dates parse" if not bad_dates else f"{len(bad_dates)} rows have invalid dates")
    record("no_future_sources", "PASS" if not future else "FAIL",
           "no source_asof_date is after run as_of" if not future else f"future rows: {future[:10]}")
    worst = {p: max(d for pp, d in stale if pp == p) for p, _ in stale}
    record("staleness_within_tolerance", "PASS" if not stale else "WARN",
           f"tolerance={tolerance}d; over-tolerance sectors={worst}" if stale else f"all within {tolerance}d")

    # 11/12. deferred to Stage 2 (need realized-return panel)
    record("cross_sector_parity", "DEFERRED", "equal-final_score -> equal realized alpha needs Stage 2 return panel")
    record("per_sector_realized_ic", "DEFERRED", "inherited from each sector's own calibration; revalidated in Stage 2")

    write_csv(validation_path, ["check", "status", "detail"], checks)

    # Manifest with integrity hashes.
    raw_dir = run_dir / "raw"
    raw_files = (
        {
            p.name: {"sha256": sha256_file(p), "rows": max(0, len(read_csv(p)))}
            for p in sorted(raw_dir.glob("*.csv"))
        }
        if raw_dir.exists()
        else {}
    )
    collected_path = run_dir / "collected_scores.csv"
    duplicate_path = run_dir / "validation" / "duplicate_resolution.csv"
    duplicate_rows = read_csv(duplicate_path) if duplicate_path.exists() else []
    per_sector = {}
    for pipe, srows in sorted(by_pipe_rows.items()):
        finals = [v for v in (parse_float(r.get("final_score")) for r in srows) if v is not None]
        per_sector[pipe] = {
            "rows": len(srows),
            "eligible": sum(1 for r in srows if str(r.get("investable_eligible", "")).strip() == "1"),
            "source_asof": next((r["source_asof_date"] for r in srows), ""),
            "final_score_min": round(min(finals), 6) if finals else None,
            "final_score_max": round(max(finals), 6) if finals else None,
        }
    hard = [c for c in checks if c["status"] not in ("WARN", "DEFERRED")]
    passed = all(c["status"] == "PASS" for c in hard)
    has_deferred = any(c["status"] == "DEFERRED" for c in checks)
    acceptance = "PASS_WITH_DEFERRED" if passed and has_deferred else "PASS" if passed else "FAIL"
    files = {
        "stocks_scores.csv": {"sha256": sha256_file(scores_path), "rows": len(rows)},
        "validation/score_contract_validation.csv": {"sha256": sha256_file(validation_path), "rows": len(checks)},
    }
    if collected_path.exists():
        files["collected_scores.csv"] = {"sha256": sha256_file(collected_path), "rows": len(read_csv(collected_path))}
    if duplicate_path.exists():
        files["validation/duplicate_resolution.csv"] = {
            "sha256": sha256_file(duplicate_path),
            "rows": len(duplicate_rows),
        }
    provenance: dict[str, dict[str, object]] = {
        "config_yaml": {"path": str(config_path), "sha256": sha256_file(config_path)},
    }
    ov_rel = cfg_get(config, "score_contract.canonical_pipeline_overrides_csv", None)
    if ov_rel is None:
        ov_rel = cfg_get(config, "score_contract.canonical_sector_overrides_csv", None)
    if ov_rel:
        ov_path = resolve_path(ov_rel, base_dir=config_path.parent)
        provenance["canonical_overrides_csv"] = {
            "path": str(ov_path),
            "exists": ov_path.exists(),
            "sha256": sha256_file(ov_path) if ov_path.exists() else "",
        }
    manifest = {
        "run_as_of": run_as_of,
        "contract_version": score_version,
        "field_naming": "lower_snake_case",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": acceptance,
        "hard_gate_acceptance": "PASS" if passed else "FAIL",
        "deferred_checks": [c["check"] for c in checks if c["status"] == "DEFERRED"],
        "files": files,
        "provenance": provenance,
        "duplicate_resolution": {
            "duplicate_rows": len(duplicate_rows),
            "cross_sector_duplicate_rows": sum(
                1 for r in duplicate_rows if r.get("duplicate_type", "cross_sector") == "cross_sector"
            ),
            "intra_sector_duplicate_rows": sum(
                1 for r in duplicate_rows if r.get("duplicate_type") == "intra_sector"
            ),
            "canonical_overrides_applied": sum(1 for r in duplicate_rows if r.get("method") == "canonical_override"),
        },
        "raw": raw_files,
        "per_sector": per_sector,
        "checks": checks,
    }
    write_manifest(manifest_path, manifest)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if passed:
        LOGGER.info(
            "STAGE 1 HARD GATES: PASS%s (as_of=%s, %d names) -> %s",
            "; empirical gates deferred" if has_deferred else "",
            run_as_of,
            len(rows),
            manifest_path,
        )
        return 0
    LOGGER.error("STAGE 1 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
