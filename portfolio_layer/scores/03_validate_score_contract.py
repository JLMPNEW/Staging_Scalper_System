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
from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    CONTRACT_FIELDS, DEFAULT_RATING_BANDS, contract_version, fail_if_exists, percentiles_within,
    rating_for_percentile, read_csv, sha256_file, validate_rating_bands, write_csv, write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.scores.adapters import _truthy as adapter_truthy  # noqa: E402


LOGGER = logging.getLogger("validate_score_contract")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
KEY_FIELDS = ("ticker", "as_of_date", "source_pipeline", "sector")
FLOAT_FIELDS = ("final_score", "within_sector_percentile", "score_confidence", "native_score")
SAMPLE_ROLES = ("strict_oos", "pre_lock_research", "excluded")


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


def row_flag(row: dict[str, str], field: str) -> bool:
    try:
        return int(float(str(row.get(field, "0")).strip() or "0")) != 0
    except (TypeError, ValueError):
        return False


# Contract eligibility_reason prefixes that legitimately demote an upstream gate=1 row: the adapter
# fails investability CLOSED when the score is not a valid frozen OOS model (pre-lock historical
# files), when the candidate status forbids it, or when the score itself is a missing-value sentinel.
MED_HANDOFF_DEMOTION_PREFIXES = ("not_oos_score_valid", "missing_score", "failed_portfolio_candidate_gate")


def validate_med_devices_handoff(
    *,
    run_dir: Path,
    score_rows: list[dict[str, str]],
) -> tuple[list[str], list[str]]:
    """Validate that med-devices optimizer eligibility follows its published gate, demotion-aware.

    Stage 1 intentionally reads the full med-device daily composite file so the non-investable
    population remains available for score context and diagnostics. The portfolio optimizer must
    see (a) NO med-device name the sector did not gate, and (b) every gated name, unless the
    contract records a recognized fail-closed demotion (e.g. pre-lock rows are gate=1 but
    oos_score_valid=0 and the adapter demotes them by design) or cross-sector duplicate
    resolution assigned the ticker to another pipeline.

    Returns (errors, warnings). Warnings do not fail the hard gate: an entirely demoted gate set
    is legitimate on pre-lock historical dates but must stay visible in the validation artifact.
    """
    raw_path = run_dir / "raw" / "med_devices_scores.csv"
    if not raw_path.exists():
        return [f"missing_raw_source:{raw_path}"], []
    raw_rows = read_csv(raw_path)
    if not raw_rows:
        return ["raw_source_empty"], []
    required = {"ticker", "portfolio_candidate_gate", "portfolio_candidate_score", "analyst_review_decision"}
    missing = sorted(required - set(raw_rows[0].keys()))
    if missing:
        return [f"raw_source_missing_columns:{missing}"], []
    # the raw gate must parse with the exact adapter semantics (adapters._truthy), or values the
    # adapter accepts (e.g. "true", "2") make this comparison false-diverge from Stage 1 output
    gate_tickers = {
        str(row.get("ticker", "")).strip().upper()
        for row in raw_rows
        if str(row.get("ticker", "")).strip() and adapter_truthy(row.get("portfolio_candidate_gate"))
    }
    med_rows = {
        str(row.get("ticker", "")).strip().upper(): row
        for row in score_rows
        if str(row.get("source_pipeline", "")).strip() == "med_devices"
    }
    any_pipeline = {str(row.get("ticker", "")).strip().upper() for row in score_rows}
    stage1_tickers = {
        t for t, row in med_rows.items() if str(row.get("investable_eligible", "")).strip() == "1"
    }
    errors: list[str] = []
    extra_in_stage1 = sorted(stage1_tickers - gate_tickers)
    if extra_in_stage1:
        errors.append(f"stage1_investable_beyond_gate:{extra_in_stage1[:20]}")
    unexplained: list[str] = []
    for ticker in sorted(gate_tickers - stage1_tickers):
        row = med_rows.get(ticker)
        if row is not None:
            reason = str(row.get("eligibility_reason", "")).strip()
            if not reason.startswith(MED_HANDOFF_DEMOTION_PREFIXES):
                unexplained.append(f"{ticker}:reason={reason[:60] or '<empty>'}")
        elif ticker in any_pipeline:
            continue  # cross-sector duplicate resolved to another pipeline; still optimizer-visible
        else:
            unexplained.append(f"{ticker}:dropped_from_contract")
    if unexplained:
        errors.append(f"gate_tickers_demoted_without_recognized_reason:{unexplained[:20]}")
    negative_gate_tickers = sorted(
        str(row.get("ticker", "")).strip().upper()
        for row in raw_rows
        if adapter_truthy(row.get("portfolio_candidate_gate"))
        and str(row.get("analyst_review_decision", "")).strip().lower() in {"reject", "data_fix_needed"}
    )
    if negative_gate_tickers:
        errors.append(f"negative_analyst_decisions_in_gate:{negative_gate_tickers[:20]}")

    def _rank_1_to_10(row: dict[str, str]) -> bool:
        try:
            return 1 <= int(float(str(row.get("rank", "")).strip())) <= 10
        except (TypeError, ValueError):
            return False

    top_non_candidate_tickers = sorted(
        str(row.get("ticker", "")).strip().upper()
        for row in raw_rows
        if not adapter_truthy(row.get("portfolio_candidate_gate")) and _rank_1_to_10(row)
    )
    if top_non_candidate_tickers:
        LOGGER.info(
            "Med-devices top-score rows excluded from optimizer by portfolio_candidate_gate: %s",
            top_non_candidate_tickers,
        )
    warnings: list[str] = []
    if gate_tickers and not stage1_tickers:
        warnings.append(f"all_gate_tickers_demoted_or_reassigned:{sorted(gate_tickers)[:20]}")
    return errors, warnings


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
    duplicate_path = run_dir / "validation" / "duplicate_resolution.csv"
    if args.force:
        invalidate_dependents(run_dir, "scores")
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    rows = read_csv(scores_path)
    duplicate_rows = read_csv(duplicate_path) if duplicate_path.exists() else []
    enabled_sectors = [s for s in cfg_get(config, "score_contract.sectors", []) if bool(s.get("enabled", True))]
    expected_pipelines = {str(s.get("model_family", "")) for s in enabled_sectors if str(s.get("model_family", ""))}
    required_pipelines = {
        str(s.get("model_family", ""))
        for s in enabled_sectors
        if bool(s.get("required", True)) and str(s.get("model_family", ""))
    }
    global_native_range = dict(cfg_get(config, "score_contract.native_score_range", {}) or {})
    native_range_by_pipeline = {
        str(s.get("model_family", "")): {**global_native_range, **dict(s.get("native_score_range", {}) or {})}
        for s in enabled_sectors
        if str(s.get("model_family", ""))
    }
    tolerance = int(cfg_get(config, "score_contract.staleness_tolerance_days", 10))
    tolerance_by_pipeline = {
        str(s.get("model_family", "")): int(s.get("staleness_tolerance_days", tolerance))
        for s in enabled_sectors
        if str(s.get("model_family", ""))
    }
    parsed_max_abs_expected_alpha = parse_float(cfg_get(config, "score_contract.max_abs_expected_alpha", 1.0))
    max_abs_expected_alpha = 1.0 if parsed_max_abs_expected_alpha is None else parsed_max_abs_expected_alpha
    bands = {**DEFAULT_RATING_BANDS, **cfg_get(config, "score_contract.rating_bands", {})}

    checks: list[dict] = []

    def record(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    band_errors = validate_rating_bands(bands)
    record(
        "rating_bands_valid",
        "PASS" if not band_errors else "FAIL",
        "rating thresholds numeric, in range, and descending"
        if not band_errors else f"invalid rating bands: {band_errors}",
    )

    # 1. schema
    header = set(rows[0].keys()) if rows else set()
    missing = [f for f in CONTRACT_FIELDS if f not in header]
    record("schema_complete", "PASS" if not missing and rows else "FAIL",
           "all contract fields present" if not missing and rows else f"missing/empty: {missing or 'no rows'}")

    # 2. non-null keys
    bad_keys = [r.get("ticker", "<missing>") for r in rows if any(not str(r.get(k, "")).strip() for k in KEY_FIELDS)]
    record("non_null_keys", "PASS" if not bad_keys else "FAIL",
           "all key fields populated" if not bad_keys else f"{len(bad_keys)} rows with empty keys")

    # 3. single as-of
    asofs = {str(r.get("as_of_date", "")).strip() for r in rows}
    record("single_as_of", "PASS" if asofs == {run_as_of} else "FAIL", f"as_of values={sorted(asofs)}")

    # 3b. required sector/pipeline presence
    present_pipelines = {str(r.get("source_pipeline", "")).strip() for r in rows if str(r.get("source_pipeline", "")).strip()}
    missing_required = sorted(required_pipelines - present_pipelines)
    unexpected_pipelines = sorted(present_pipelines - expected_pipelines)
    pipeline_bad = []
    if missing_required:
        pipeline_bad.append(f"missing_required={missing_required}")
    if unexpected_pipelines:
        pipeline_bad.append(f"unexpected={unexpected_pipelines}")
    record(
        "required_pipelines_present",
        "PASS" if not pipeline_bad else "FAIL",
        f"present={sorted(present_pipelines)}"
        if not pipeline_bad else "; ".join(pipeline_bad),
    )

    # 4. configured contract version
    versions = {str(r.get("score_version", "")).strip() for r in rows}
    record("score_version_matches_config", "PASS" if versions == {score_version} else "FAIL",
           f"expected={score_version}; found={sorted(versions)}")

    # 5. ticker uniqueness
    seen: dict[str, int] = {}
    for r in rows:
        ticker = str(r.get("ticker", "")).strip()
        if ticker:
            seen[ticker] = seen.get(ticker, 0) + 1
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
        if str(r.get("calibration_research_eligible", "")).strip() not in ("0", "1"):
            bad_numeric.append(f"{ticker}:calibration_research_eligible={r.get('calibration_research_eligible')}")
        if str(r.get("calibration_sample_role", "")).strip() not in SAMPLE_ROLES:
            bad_numeric.append(f"{ticker}:calibration_sample_role={r.get('calibration_sample_role')}")
        if str(r.get("stage1_sample_role", "")).strip() not in SAMPLE_ROLES:
            bad_numeric.append(f"{ticker}:stage1_sample_role={r.get('stage1_sample_role')}")
        if str(r.get("oos_score_valid_flag", "")).strip() not in ("0", "1"):
            bad_numeric.append(f"{ticker}:oos_score_valid_flag={r.get('oos_score_valid_flag')}")
        if str(r.get("missing_score_flag", "")).strip() not in ("0", "1"):
            bad_numeric.append(f"{ticker}:missing_score_flag={r.get('missing_score_flag')}")
        if str(r.get("survivorship_corrected_panel_flag", "")).strip() not in ("0", "1"):
            bad_numeric.append(
                f"{ticker}:survivorship_corrected_panel_flag={r.get('survivorship_corrected_panel_flag')}"
            )
        if parse_float(r.get("staleness_days")) is None:
            bad_numeric.append(f"{ticker}:staleness_days=missing_or_non_numeric")
    record("numeric_fields_valid", "PASS" if not bad_numeric else "FAIL",
           "numeric fields parse and ranges hold" if not bad_numeric else (
               f"{len(bad_numeric)} bad values; first={bad_numeric[:10]}"
           ))

    # 6b. expected-alpha magnitude sanity
    bad_alpha = []
    for r in rows:
        final_score = parse_float(r.get("final_score"))
        if final_score is not None and abs(final_score) > max_abs_expected_alpha:
            bad_alpha.append(f"{r.get('ticker', '<missing>')}:{final_score}")
    record("final_score_magnitude_sane", "PASS" if not bad_alpha else "FAIL",
           f"|final_score| <= {max_abs_expected_alpha}" if not bad_alpha else (
               f"{len(bad_alpha)} rows exceed {max_abs_expected_alpha}; first={bad_alpha[:10]}"
           ))

    # 6c. native score scale sanity (missing-score sentinel rows are neutralized upstream; their
    # sentinel native value is exempt from the range gate, mirroring 02)
    bad_native_range = []
    for r in rows:
        if str(r.get("missing_score_flag", "")).strip() in ("1", "1.0"):
            continue
        pipeline = str(r.get("source_pipeline", "")).strip()
        native = parse_float(r.get("native_score"))
        range_cfg = native_range_by_pipeline.get(pipeline, global_native_range)
        native_min = parse_float(range_cfg.get("min", 0.0))
        native_max = parse_float(range_cfg.get("max", 100.0))
        if native is None or native_min is None or native_max is None:
            bad_native_range.append(f"{r.get('ticker', '<missing>')}:{pipeline}:range_or_native_unparseable")
            continue
        if native_min > native_max:
            bad_native_range.append(f"{pipeline}:range_min_gt_max:{native_min}>{native_max}")
            continue
        if native < native_min or native > native_max:
            bad_native_range.append(
                f"{r.get('ticker', '<missing>')}:{pipeline}:native={native} outside [{native_min},{native_max}]"
            )
    record("native_score_range_valid", "PASS" if not bad_native_range else "FAIL",
           "all native scores within configured ranges" if not bad_native_range else (
               f"{len(bad_native_range)} range errors; first={bad_native_range[:10]}"
           ))

    # 7. monotonic final_score vs native within sector
    by_pipe_rows: dict[str, list[dict]] = {}
    for r in rows:
        pipe = str(r.get("source_pipeline", "")).strip()
        if pipe:
            by_pipe_rows.setdefault(pipe, []).append(r)
    non_monotone = []
    for pipe, srows in by_pipe_rows.items():
        pairs = [
            (native, final)
            for native, final in (
                (parse_float(r.get("native_score")), parse_float(r.get("final_score")))
                for r in srows
                if not row_flag(r, "missing_score_flag")
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
            if row_flag(r, "missing_score_flag"):
                actual_pct = parse_float(r.get("within_sector_percentile"))
                actual_rating = str(r.get("rating", "")).strip()
                final_score = parse_float(r.get("final_score"))
                if actual_pct != 0.0 or actual_rating != "avoid" or final_score is None or abs(final_score) > 1e-12:
                    bad_pct_rating.append(f"{r.get('ticker', '<missing>')}:{pipe}:missing_score_not_neutral_avoid")
                continue
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
                    f"{r.get('ticker', '<missing>')}:{pipe}:pct expected={expected_pct} actual={actual_pct}"
                )
            expected_rating = rating_for_percentile(pct, bands)
            if str(r.get("rating", "")).strip() != expected_rating:
                bad_pct_rating.append(
                    f"{r.get('ticker', '<missing>')}:{pipe}:rating expected={expected_rating} actual={r.get('rating')}"
                )
    record("percentile_rating_final_population", "PASS" if not bad_pct_rating else "FAIL",
           "percentile/rating recompute from final sector populations" if not bad_pct_rating else (
               f"{len(bad_pct_rating)} mismatches; first={bad_pct_rating[:10]}"
           ))

    # 9. eligibility populated
    bad_elig = [
        r.get("ticker", "<missing>")
        for r in rows
        if str(r.get("investable_eligible", "")).strip() not in ("0", "1")
        or not str(r.get("eligibility_reason", "")).strip()
        or str(r.get("calibration_research_eligible", "")).strip() not in ("0", "1")
        or not str(r.get("calibration_research_reason", "")).strip()
        or str(r.get("calibration_sample_role", "")).strip() not in SAMPLE_ROLES
        or str(r.get("stage1_sample_role", "")).strip() not in SAMPLE_ROLES
        or str(r.get("oos_score_valid_flag", "")).strip() not in ("0", "1")
        or str(r.get("missing_score_flag", "")).strip() not in ("0", "1")
        or str(r.get("survivorship_corrected_panel_flag", "")).strip() not in ("0", "1")
    ]
    record("eligibility_populated", "PASS" if not bad_elig else "FAIL",
           "allocation and calibration eligibility flags in {0,1} with reasons" if not bad_elig else f"{len(bad_elig)} rows bad")

    # 9b. cross-field eligibility invariants.
    invariant_errors: list[str] = []
    for r in rows:
        ticker = r.get("ticker", "<missing>")
        investable = str(r.get("investable_eligible", "")).strip() == "1"
        research = str(r.get("calibration_research_eligible", "")).strip() == "1"
        source_role = str(r.get("calibration_sample_role", "")).strip()
        stage1_role = str(r.get("stage1_sample_role", "")).strip()
        oos = str(r.get("oos_score_valid_flag", "")).strip() == "1"
        missing_score = str(r.get("missing_score_flag", "")).strip() == "1"
        survivorship_corrected = str(r.get("survivorship_corrected_panel_flag", "")).strip() == "1"
        eligibility_reason = str(r.get("eligibility_reason", "")).strip().lower()
        research_reason = str(r.get("calibration_research_reason", "")).strip().lower()
        final_score = parse_float(r.get("final_score"))
        percentile = parse_float(r.get("within_sector_percentile"))
        rating = str(r.get("rating", "")).strip()
        if investable and stage1_role != "strict_oos":
            invariant_errors.append(f"{ticker}:investable_requires_stage1_strict_oos:{stage1_role}")
        if stage1_role == "strict_oos" and not oos:
            invariant_errors.append(f"{ticker}:strict_oos_requires_oos_score_valid")
        if investable and not research:
            invariant_errors.append(f"{ticker}:investable_requires_calibration_research_eligible")
        if investable and eligibility_reason != "ok":
            invariant_errors.append(f"{ticker}:investable_requires_ok_reason:{eligibility_reason}")
        if not investable and eligibility_reason == "ok":
            invariant_errors.append(f"{ticker}:ineligible_reason_must_not_be_ok")
        if research and research_reason != "ok":
            invariant_errors.append(f"{ticker}:research_eligible_requires_ok_reason:{research_reason}")
        if stage1_role == "strict_oos" and source_role != "strict_oos":
            invariant_errors.append(f"{ticker}:stage1_strict_oos_requires_source_strict_oos:{source_role}")
        if research and stage1_role == "pre_lock_research" and not survivorship_corrected:
            invariant_errors.append(f"{ticker}:pre_lock_research_requires_survivorship_corrected_panel")
        if missing_score:
            if investable or research:
                invariant_errors.append(f"{ticker}:missing_score_must_not_be_investable_or_research_eligible")
            if final_score is None or abs(final_score) > 1e-12 or percentile != 0.0 or rating != "avoid":
                invariant_errors.append(f"{ticker}:missing_score_requires_neutral_avoid_contract")
    record(
        "eligibility_invariants_hold",
        "PASS" if not invariant_errors else "FAIL",
        "Stage 1 investable rows are strict OOS, strict OOS rows have valid OOS scores, and source roles are not upgraded"
        if not invariant_errors
        else f"{len(invariant_errors)} invariant errors; first={invariant_errors[:10]}",
    )

    med_handoff_errors, med_handoff_warnings = validate_med_devices_handoff(run_dir=run_dir, score_rows=rows)
    record(
        "med_devices_portfolio_handoff_gate",
        "PASS" if not med_handoff_errors else "FAIL",
        "med_devices investable_eligible tickers exactly match upstream portfolio_candidate_gate=1; "
        "active reject/data_fix_needed rows are excluded"
        if not med_handoff_errors
        else f"{len(med_handoff_errors)} errors; first={med_handoff_errors[:5]}",
    )
    record(
        "med_devices_gate_demotion_profile",
        "PASS" if not med_handoff_warnings else "WARN",
        "at least one upstream gate ticker reaches investable_eligible=1"
        if not med_handoff_warnings
        else f"optimizer receives zero investable med-device names; {med_handoff_warnings[0]}",
    )

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
            future.append((r.get("source_pipeline", ""), r.get("ticker", ""), r.get("source_asof_date", ""), d))
        row_tolerance = tolerance_by_pipeline.get(str(r.get("source_pipeline", "")).strip(), tolerance)
        if d > row_tolerance:
            stale.append((r.get("source_pipeline", ""), d, row_tolerance))
    record("source_dates_valid", "PASS" if not bad_dates else "FAIL",
           "run/source dates parse" if not bad_dates else f"{len(bad_dates)} rows have invalid dates")
    record("no_future_sources", "PASS" if not future else "FAIL",
           "no source_asof_date is after run as_of" if not future else f"future rows: {future[:10]}")
    worst = {
        p: {
            "max_staleness_days": max(d for pp, d, _tol in stale if pp == p),
            "tolerance_days": min(_tol for pp, _d, _tol in stale if pp == p),
        }
        for p, _d, _tol in stale
    }
    record("staleness_within_tolerance", "PASS" if not stale else "WARN",
           f"default_tolerance={tolerance}d; over-tolerance sectors={worst}" if stale else (
               f"all within configured per-sector tolerances (default={tolerance}d)"
           ))

    # 10b. cross-sector duplicates should be curated by canonical override, not silent confidence parity.
    if duplicate_path.exists():
        uncurated_cross_duplicates = [
            r for r in duplicate_rows
            if r.get("duplicate_type") == "cross_sector" and r.get("method") == "confidence_then_config_order"
        ]
        record(
            "cross_sector_duplicates_curated",
            "PASS" if not uncurated_cross_duplicates else "WARN",
            "all cross-sector duplicate tickers resolved by canonical override"
            if not uncurated_cross_duplicates
            else (
                f"{len(uncurated_cross_duplicates)} cross-sector duplicates resolved by confidence/order; "
                f"tickers={[r.get('ticker') for r in uncurated_cross_duplicates[:10]]}"
            ),
        )
    else:
        record("cross_sector_duplicates_curated", "WARN", "duplicate_resolution.csv missing; curation not verified")

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
    per_sector = {}
    for pipe, srows in sorted(by_pipe_rows.items()):
        finals = [v for v in (parse_float(r.get("final_score")) for r in srows) if v is not None]
        per_sector[pipe] = {
            "rows": len(srows),
            "eligible": sum(1 for r in srows if str(r.get("investable_eligible", "")).strip() == "1"),
            "calibration_research_eligible": sum(
                1 for r in srows if str(r.get("calibration_research_eligible", "")).strip() == "1"
            ),
            "source_asof": next((r.get("source_asof_date", "") for r in srows), ""),
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
