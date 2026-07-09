#!/usr/bin/env python3
"""Stage 6 - validate and seal the portfolio-native macro contract."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.macro.contract import (  # noqa: E402
    MACRO_COUNTRY_FIELDS,
    MACRO_FOREIGN_BUDGET_FIELDS,
    MACRO_FOREIGN_CANDIDATE_FIELDS,
    MACRO_REGIME_FIELDS,
    MACRO_SECTOR_FIELDS,
    MACRO_STOCK_FIELDS,
    macro_serving_content_sha256,
)
from portfolio_layer.macro.taxonomy import score_pipelines, sleeve_taxonomy  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("validate_macro_contract")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = [
    "contract.py",
    "taxonomy.py",
    "20_run_macro_serving.py",
    "21_build_macro_contract.py",
    "22_validate_macro_contract.py",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate Stage 6 macro contract artifacts.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _future_date_violation(run_as_of: str, value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
        run_date = date.fromisoformat(run_as_of)
    except ValueError:
        return None
    return text if parsed > run_date else None


def _stale_ok(row: dict[str, str], tolerance: int) -> bool:
    value = _safe_float(row.get("staleness_days"))
    return value is not None and 0 <= value <= tolerance


def _csv_header(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            return next(reader, [])
    except OSError:
        return []


def _field_check(
    rows: list[dict[str, str]],
    expected: list[str],
    *,
    path: Path | None = None,
    allow_header_only: bool = False,
) -> list[str]:
    if not rows:
        if allow_header_only and path is not None:
            actual_header = _csv_header(path)
            return [] if actual_header == expected else [f"columns={actual_header} expected={expected}"]
        return ["no_rows"]
    actual = list(rows[0].keys())
    return [] if actual == expected else [f"columns={actual} expected={expected}"]


def _table_freshness(
    rows: list[dict[str, str]],
    *,
    tolerance: int,
    label: str,
    required: bool = True,
) -> list[str]:
    if not rows:
        return [f"{label}:missing"] if required else []
    bad = [r for r in rows if not _stale_ok(r, tolerance)]
    if bad:
        examples = [f"{r.get('ticker') or r.get('source_pipeline') or label}:{r.get('staleness_days')}" for r in bad[:5]]
        return [f"{label}:stale_or_invalid tolerance={tolerance} examples={examples}"]
    return []


def _sector_freshness(rows: list[dict[str, str]], tolerances: dict[str, Any]) -> list[str]:
    if not rows:
        return ["sector_fit:missing"]
    label_by_level = {
        "industry": "industry_fit",
        "industry_aggregate": "industry_aggregate_fit",
        "sector": "sector_fit",
    }
    bad = []
    for row in rows:
        level = str(row.get("macro_level", "")).strip()
        label = label_by_level.get(level, "sector_fit")
        tolerance = int(tolerances.get(label, tolerances.get("sector_fit", 30)))
        if not _stale_ok(row, tolerance):
            bad.append(
                f"{row.get('source_pipeline') or 'sector_fit'}:{level or 'missing_level'}:"
                f"{row.get('staleness_days')} tolerance={tolerance}"
            )
    if bad:
        return [f"sector_fit:stale_or_invalid examples={bad[:5]}"]
    return []


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No Stage 1 run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    macro_dir = run_dir / "macro"
    validation_path = macro_dir / "validation" / "macro_contract_validation.csv"
    manifest_path = macro_dir / "macro_manifest.json"
    if args.force:
        for path in (validation_path, manifest_path):
            if path.exists():
                path.unlink()
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    file_paths = {
        "macro_regime.csv": macro_dir / "macro_regime.csv",
        "macro_sector_fit.csv": macro_dir / "macro_sector_fit.csv",
        "macro_stock_overlay.csv": macro_dir / "macro_stock_overlay.csv",
        "macro_country_fit.csv": macro_dir / "macro_country_fit.csv",
        "macro_foreign_budget.csv": macro_dir / "macro_foreign_budget.csv",
        "macro_foreign_candidates.csv": macro_dir / "macro_foreign_candidates.csv",
        "macro_contract_meta.json": macro_dir / "macro_contract_meta.json",
    }
    missing = [name for name, path in file_paths.items() if not path.exists()]
    if missing:
        LOGGER.error("Run 21 first; missing macro artifacts: %s", missing)
        return 1

    meta = json.loads(file_paths["macro_contract_meta.json"].read_text(encoding="utf-8"))
    serving_db_meta = str(meta.get("serving_db_path") or "").strip()
    serving_db_path = ensure_not_prod_path(
        Path(serving_db_meta) if serving_db_meta else paths.macro_serving_db_path,
        label="macro serving db",
    )
    scores_path = run_dir / "stocks_scores.csv"
    stage1_manifest_path = run_dir / "manifest.json"
    scores = read_csv(scores_path)
    stage1_manifest = json.loads(stage1_manifest_path.read_text(encoding="utf-8"))
    regime = read_csv(file_paths["macro_regime.csv"])
    sector = read_csv(file_paths["macro_sector_fit.csv"])
    stock = read_csv(file_paths["macro_stock_overlay.csv"])
    country = read_csv(file_paths["macro_country_fit.csv"])
    foreign_budget = read_csv(file_paths["macro_foreign_budget.csv"])
    foreign_candidates = read_csv(file_paths["macro_foreign_candidates.csv"])

    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # 1. Independence / shadow only.
    prod_token = "PROD_" + "Scalper_System"
    prod_hits = []
    for name in SOURCE_FILES:
        path = PACKAGE_ROOT / "macro" / name
        if path.exists() and prod_token in path.read_text(encoding="utf-8"):
            prod_hits.append(name)
    enabled = bool(cfg_get(config, "macro.enabled_in_production", False)) or bool(meta.get("enabled_in_production"))
    rec("independence_shadow_only", "PASS" if not prod_hits and not enabled else "FAIL",
        "macro wrapper has no PROD path token and production is disabled"
        if not prod_hits and not enabled else f"prod_hits={prod_hits} enabled={enabled}")

    # 2. Required schema.
    schema_bad = []
    for label, rows, expected in [
        ("macro_regime", regime, MACRO_REGIME_FIELDS),
        ("macro_sector_fit", sector, MACRO_SECTOR_FIELDS),
        ("macro_stock_overlay", stock, MACRO_STOCK_FIELDS),
        ("macro_country_fit", country, MACRO_COUNTRY_FIELDS),
        ("macro_foreign_budget", foreign_budget, MACRO_FOREIGN_BUDGET_FIELDS),
        ("macro_foreign_candidates", foreign_candidates, MACRO_FOREIGN_CANDIDATE_FIELDS),
    ]:
        path = file_paths[f"{label}.csv"]
        schema_bad.extend(
            f"{label}:{msg}"
            for msg in _field_check(
                rows,
                expected,
                path=path,
                allow_header_only=(label == "macro_foreign_candidates"),
            )
        )
    rec("macro_contract_schema", "PASS" if not schema_bad else "FAIL",
        "all macro contract files expose exact schemas" if not schema_bad else f"{schema_bad[:8]}")

    # 3. Stage 1 contract is sealed and untouched.
    stage1_bad = []
    recorded = ((stage1_manifest.get("files") or {}).get("stocks_scores.csv") or {}).get("sha256")
    actual = sha256_file(scores_path)
    if not recorded or recorded != actual:
        stage1_bad.append(f"stocks_scores hash mismatch manifest={str(recorded)[:12]} actual={actual[:12]}")
    meta_hash = (meta.get("inputs_sha256") or {}).get("stocks_scores.csv")
    if meta_hash != actual:
        stage1_bad.append("macro meta does not pin current stocks_scores.csv")
    rec("stage1_contract_unchanged", "PASS" if not stage1_bad else "FAIL",
        "stocks_scores hash matches Stage 1 manifest and macro meta" if not stage1_bad else f"{stage1_bad}")

    # 4. PIT/no-lookahead.
    pit_bad = []
    for label, rows in [
        ("regime", regime),
        ("sector", sector),
        ("stock", stock),
        ("country", country),
        ("foreign_budget", foreign_budget),
        ("foreign_candidates", foreign_candidates),
    ]:
        for row in rows:
            future_date = _future_date_violation(run_as_of, row.get("macro_as_of_date"))
            if future_date:
                pit_bad.append(f"{label}:{row.get('ticker') or row.get('source_pipeline') or future_date}={future_date}")
                break
    for table, as_of in (meta.get("source_dates") or {}).items():
        future_date = _future_date_violation(run_as_of, as_of)
        if future_date:
            pit_bad.append(f"{table}:source_date={future_date}>{run_as_of}")
    rec("pit_no_future_macro_dates", "PASS" if not pit_bad else "FAIL",
        f"all macro dates <= {run_as_of}" if not pit_bad else f"{pit_bad[:8]}")

    # 5. Freshness against configured tolerances.
    tol = cfg_get(config, "macro.freshness_tolerance_days", {}) or {}
    freshness_bad = []
    freshness_bad.extend(_table_freshness(regime, tolerance=int(tol.get("regime", 5)), label="regime"))
    freshness_bad.extend(_table_freshness(country, tolerance=int(tol.get("country_fit", 30)), label="country_fit"))
    freshness_bad.extend(_sector_freshness(sector, tol))
    freshness_bad.extend(_table_freshness(stock, tolerance=int(tol.get("stock_fit", 30)), label="stock_fit"))
    freshness_bad.extend(_table_freshness(
        foreign_budget,
        tolerance=int(tol.get("foreign_budget", 30)),
        label="foreign_budget",
    ))
    freshness_bad.extend(_table_freshness(
        foreign_candidates,
        tolerance=int(tol.get("foreign_candidates", 30)),
        label="foreign_candidates",
        required=False,
    ))
    rec("macro_freshness_within_tolerance", "PASS" if not freshness_bad else "FAIL",
        "all macro contract rows within configured tolerances" if not freshness_bad else f"{freshness_bad[:8]}")

    # 6. Sleeve taxonomy: macro_sector_fit must be keyed exactly to Stage 1 source_pipeline values.
    score_pipe_set = set(score_pipelines(scores))
    sector_pipe_set = {str(r.get("source_pipeline", "")).strip() for r in sector if str(r.get("source_pipeline", "")).strip()}
    taxonomy_keys = set(sleeve_taxonomy(config))
    sector_pipe_values = [str(r.get("source_pipeline", "")).strip() for r in sector]
    dup_sectors = sorted({pipe for pipe in sector_pipe_values if pipe and sector_pipe_values.count(pipe) > 1})
    taxonomy_bad = []
    if sector_pipe_set != score_pipe_set:
        taxonomy_bad.append(f"sector_fit_vs_scores diff={sorted(sector_pipe_set ^ score_pipe_set)}")
    if bool(cfg_get(config, "macro.require_all_sleeves", True)) and not score_pipe_set.issubset(taxonomy_keys):
        taxonomy_bad.append(f"missing_taxonomy={sorted(score_pipe_set - taxonomy_keys)}")
    if dup_sectors:
        taxonomy_bad.append(f"duplicate_sleeves={dup_sectors}")
    rec("sleeve_taxonomy_matches_scores", "PASS" if not taxonomy_bad else "FAIL",
        f"{len(score_pipe_set)} sleeves keyed by source_pipeline" if not taxonomy_bad else f"{taxonomy_bad}")

    # 7. Stock overlay covers every Stage 1 ticker exactly once, with bounded fallback.
    score_tickers = [str(r.get("ticker", "")).strip().upper() for r in scores if str(r.get("ticker", "")).strip()]
    stock_tickers = [str(r.get("ticker", "")).strip().upper() for r in stock if str(r.get("ticker", "")).strip()]
    missing_tickers = sorted(set(score_tickers) - set(stock_tickers))
    extra_tickers = sorted(set(stock_tickers) - set(score_tickers))
    dup_tickers = sorted({t for t in stock_tickers if stock_tickers.count(t) > 1})
    eligible = {str(r.get("ticker", "")).strip().upper() for r in scores if str(r.get("investable_eligible")) == "1"}
    fallback = {str(r.get("ticker", "")).strip().upper() for r in stock if str(r.get("fallback_used")) == "1"}
    fallback_frac = len(fallback) / len(score_tickers) if score_tickers else 1.0
    eligible_fallback_frac = len(fallback & eligible) / len(eligible) if eligible else 0.0
    max_fallback = float(cfg_get(config, "macro.max_stock_fallback_fraction", 0.50))
    max_eligible_fallback = float(cfg_get(config, "macro.max_eligible_stock_fallback_fraction", 0.40))
    stock_bad = []
    if missing_tickers:
        stock_bad.append(f"missing={missing_tickers[:8]}")
    if extra_tickers:
        stock_bad.append(f"extra={extra_tickers[:8]}")
    if dup_tickers:
        stock_bad.append(f"duplicates={dup_tickers[:8]}")
    if fallback_frac > max_fallback + 1e-12:
        stock_bad.append(f"fallback_frac={fallback_frac:.3f}>{max_fallback:.3f}")
    if eligible_fallback_frac > max_eligible_fallback + 1e-12:
        stock_bad.append(f"eligible_fallback_frac={eligible_fallback_frac:.3f}>{max_eligible_fallback:.3f}")
    rec("stock_overlay_coverage_and_fallback", "PASS" if not stock_bad else "FAIL",
        f"rows={len(stock_tickers)} fallback={fallback_frac:.3f} eligible_fallback={eligible_fallback_frac:.3f}"
        if not stock_bad else f"{stock_bad}")

    # 8. Stage 7 contract surface: sector targets and foreign budget are present and numeric.
    contract_bad = []
    weights = []
    for row in sector:
        w = _safe_float(row.get("target_weight"))
        fit = _safe_float(row.get("macro_fit_score"))
        if w is None or w < -1e-12:
            contract_bad.append(f"{row.get('source_pipeline')}:bad_target_weight={row.get('target_weight')}")
        else:
            weights.append(w)
        if fit is None:
            contract_bad.append(f"{row.get('source_pipeline')}:bad_macro_fit_score={row.get('macro_fit_score')}")
    if weights and abs(sum(weights) - 1.0) > 1e-6:
        contract_bad.append(f"target_weight_sum={sum(weights):.12f}")
    if foreign_budget:
        for col in ("foreign_budget", "min_budget", "max_budget"):
            if _safe_float(foreign_budget[0].get(col)) is None:
                contract_bad.append(f"foreign_budget:{col}=non_numeric")
    rec("stage7_contract_surface", "PASS" if not contract_bad else "FAIL",
        "sector target weights + macro fits + foreign budget are numeric" if not contract_bad else f"{contract_bad[:8]}")

    # 9. Build meta still matches input/source files and artifact hashes.
    meta_bad = []
    inputs = meta.get("inputs_sha256") or {}
    configured_stock_fallback_policy = str(cfg_get(config, "macro.stock_fallback_policy", ""))
    if str(meta.get("stock_fallback_policy", "")) != configured_stock_fallback_policy:
        meta_bad.append("stock_fallback_policy:meta_config_mismatch")
    expected_inputs = {
        "config.yaml": config_path,
        "stocks_scores.csv": scores_path,
        "manifest.json": stage1_manifest_path,
    }
    optimizer_inputs = {
        "optimizer/target_weights.csv": run_dir / "optimizer" / "target_weights.csv",
        "optimizer/optimizer_manifest.json": run_dir / "optimizer" / "optimizer_manifest.json",
    }
    for name, path in optimizer_inputs.items():
        if name in inputs or path.exists():
            expected_inputs[name] = path
    for name, path in expected_inputs.items():
        if not path.exists():
            meta_bad.append(f"{name}:missing_input")
            continue
        if inputs.get(name) != sha256_file(path):
            meta_bad.append(f"{name}:input_hash_mismatch")
    serving_content_key = "macro_serving.sqlite:content"
    actual_serving_content_hash = macro_serving_content_sha256(serving_db_path, run_as_of)
    if inputs.get(serving_content_key) != actual_serving_content_hash:
        meta_bad.append(f"{serving_content_key}:input_hash_mismatch")
    for name in SOURCE_FILES:
        path = PACKAGE_ROOT / "macro" / name
        recorded_source = inputs.get(f"source/{name}")
        if path.exists() and recorded_source and recorded_source != sha256_file(path):
            meta_bad.append(f"source/{name}:hash_mismatch")
    for name, info in (meta.get("files") or {}).items():
        path = macro_dir / name
        if not path.exists() or sha256_file(path) != info.get("sha256"):
            meta_bad.append(f"{name}:artifact_hash_mismatch")
    rec("macro_meta_reproducible", "PASS" if not meta_bad else "FAIL",
        "meta pins current inputs, sources, and CSV artifacts" if not meta_bad else f"{meta_bad[:8]}")

    # 10. MacroLayer legacy optimizer outputs are not part of this contract.
    forbidden = [
        run_dir / "stocks_scores_macro_adjusted.csv",
        run_dir / "macro_optimizer_inputs.csv",
        run_dir / "optimizer" / "macro_optimizer_inputs.csv",
    ]
    present_forbidden = [str(p) for p in forbidden if p.exists()]
    rec("no_legacy_macro_optimizer_outputs", "PASS" if not present_forbidden else "FAIL",
        "Stage 6 writes only macro contract artifacts" if not present_forbidden else f"{present_forbidden}")

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)
    hard_pass = all(c["status"] != "FAIL" for c in checks)

    provenance = {
        **file_paths,
        "validation/macro_contract_validation.csv": validation_path,
        "stocks_scores.csv": scores_path,
        "manifest.json": stage1_manifest_path,
        "config.yaml": config_path,
    }
    for name in SOURCE_FILES:
        path = PACKAGE_ROOT / "macro" / name
        if path.exists():
            provenance[f"source/{name}"] = path
    manifest = {
        "run_as_of": run_as_of,
        "stage": "stage6_macro_contract",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if hard_pass else "FAIL",
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "macro.enabled_in_production", False)),
        "macro_contract_meta_sha256": sha256_file(file_paths["macro_contract_meta.json"]),
        "macro_serving_content_sha256": inputs.get("macro_serving.sqlite:content", ""),
        "counts": {
            "sector_rows": len(sector),
            "stock_rows": len(stock),
            "country_rows": len(country),
            "foreign_candidate_rows": len(foreign_candidates),
            "stock_fallback_fraction": round(fallback_frac, 6),
            "eligible_stock_fallback_fraction": round(eligible_fallback_frac, 6),
        },
        "provenance_sha256": {name: sha256_file(path) for name, path in provenance.items() if path.exists()},
        "checks": checks,
    }
    write_manifest(manifest_path, manifest)

    for check in checks:
        LOGGER.info("[%s] %s -- %s", check["status"], check["check"], check["detail"])
    if hard_pass:
        LOGGER.info("STAGE 6 ACCEPTANCE: PASS (as_of=%s) -> %s", run_as_of, manifest_path)
        return 0
    LOGGER.error("STAGE 6 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
