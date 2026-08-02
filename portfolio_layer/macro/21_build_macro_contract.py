#!/usr/bin/env python3
"""Stage 6 - build portfolio-native macro contract artifacts from the vendored MacroLayer DB.

This script is a boundary adapter. It reads MacroLayer's serving SQLite DB read-only, filters every
table to as_of_date <= the portfolio run as-of, maps MacroLayer taxonomy onto the five
source_pipeline sleeves, and writes only under runs/<as_of>/macro/.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.artifacts import invalidate_macro_outputs_after_contract_change  # noqa: E402
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
    finite_or_blank,
    int_or_blank,
    macro_serving_content_sha256,
    open_macro_serving_db,
    regime_table_for_source,
    rows_at_latest,
    single_latest_regime_row,
    staleness_days,
    h1_promotion_status,
    v2_promotion_status,
    verify_v2_promotion_manifest,
)
from portfolio_layer.macro.taxonomy import (  # noqa: E402
    base_target_weights,
    score_pipelines,
    select_sleeve_macro_fit,
    sleeve_taxonomy,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_macro_contract")
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
    p = argparse.ArgumentParser(description="Build Stage 6 macro contract artifacts.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--serving-db", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _macro_serving_path(default_path: Path, override: Path | None) -> Path:
    if override is not None:
        return ensure_not_prod_path(override, label="macro serving db")
    return ensure_not_prod_path(default_path, label="macro serving db")


def _source_hashes(
    config_path: Path,
    serving_db_path: Path,
    run_dir: Path,
    run_as_of: str,
    *,
    regime_table: str,
    regime_model_version: str | None,
    promotion_manifest_path: Path | None,
) -> dict[str, str]:
    paths = {
        "config.yaml": config_path,
        "MacroLayer/config_macro_raw.yaml": PACKAGE_ROOT / "MacroLayer" / "config_macro_raw.yaml",
        "MacroLayer/macro_metric_policy.csv": PACKAGE_ROOT / "MacroLayer" / "macro_metric_policy.csv",
        "MacroLayer/macro_feature_policy.csv": PACKAGE_ROOT / "MacroLayer" / "macro_feature_policy.csv",
        "MacroLayer/macro_composite_policy.csv": PACKAGE_ROOT / "MacroLayer" / "macro_composite_policy.csv",
        "MacroLayer/macro_serving_common.py": PACKAGE_ROOT / "MacroLayer" / "macro_serving_common.py",
        "MacroLayer/build_macro_observation_daily_pit.py": (
            PACKAGE_ROOT / "MacroLayer" / "build_macro_observation_daily_pit.py"
        ),
        "stocks_scores.csv": run_dir / "stocks_scores.csv",
        "manifest.json": run_dir / "manifest.json",
        "optimizer/target_weights.csv": run_dir / "optimizer" / "target_weights.csv",
        "optimizer/optimizer_manifest.json": run_dir / "optimizer" / "optimizer_manifest.json",
    }
    if promotion_manifest_path is not None:
        paths["MacroLayer/regime_v2_promotion_manifest.json"] = promotion_manifest_path
    for name in SOURCE_FILES:
        path = PACKAGE_ROOT / "macro" / name
        if path.exists():
            paths[f"source/{name}"] = path
    hashes = {name: sha256_file(path) for name, path in paths.items() if path.exists()}
    hashes["macro_serving.sqlite:content"] = macro_serving_content_sha256(
        serving_db_path,
        run_as_of,
        regime_table=regime_table,
        regime_model_version=regime_model_version,
    )
    return hashes


def _verify_v2_promotion_manifest(
    *,
    promotion_row: Any | None,
    model_version: str,
    macro_config_path: Path,
) -> tuple[Path | None, list[str]]:
    if promotion_row is None:
        return None, ["missing_v2_promotion_summary"]
    errors: list[str] = []
    if str(promotion_row["acceptance"] or "") != "PROMOTABLE":
        errors.append(f"acceptance={promotion_row['acceptance']}")
    raw_path = str(promotion_row["artifact_manifest_path"] or "").strip()
    if not raw_path:
        return None, [*errors, "missing_artifact_manifest_path"]
    path = ensure_not_prod_path(Path(raw_path), label="v2 promotion manifest").resolve()
    builder_path = PACKAGE_ROOT / "MacroLayer" / "validate_macro_regime_v2_promotion.py"
    errors.extend(
        verify_v2_promotion_manifest(
            path,
            model_version=model_version,
            macro_config_path=macro_config_path,
            builder_path=builder_path,
            allowed_root=PACKAGE_ROOT / "MacroLayer" / "out" / "regime_v2",
        )
    )
    return path, errors


def _load_sealed_optimizer_targets(run_dir: Path) -> tuple[list[dict[str, str]] | None, list[str]]:
    """Load Stage 3 target weights only when their optimizer manifest still seals them."""
    target_path = run_dir / "optimizer" / "target_weights.csv"
    if not target_path.exists():
        return None, []

    manifest_path = run_dir / "optimizer" / "optimizer_manifest.json"
    errors: list[str] = []
    if not manifest_path.exists():
        return None, ["optimizer_manifest.json missing for existing target_weights.csv"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"optimizer_manifest.json unreadable:{type(exc).__name__}"]

    if manifest.get("acceptance") != "PASS":
        errors.append(f"optimizer_manifest acceptance={manifest.get('acceptance')}")
    sealed = (manifest.get("provenance_sha256") or {}).get("target_weights.csv")
    actual = sha256_file(target_path)
    if sealed != actual:
        errors.append(f"target_weights.csv hash mismatch manifest={str(sealed)[:12]} actual={actual[:12]}")
    if errors:
        return None, errors
    return read_csv(target_path), []


def _coverage_flag(value: Any) -> int | str:
    parsed = int_or_blank(value)
    return parsed


def _regime_row(run_as_of: str, row: Any | None, *, source_table: str) -> dict[str, Any]:
    if row is None:
        return {
            "run_as_of": run_as_of,
            "macro_as_of_date": "",
            "active_current_regime": "",
            "active_next_regime": "",
            "current_confidence": "",
            "next_confidence": "",
            "coverage_flag": "",
            "regime_override_reason": f"missing_{source_table}",
            "staleness_days": "",
        }
    stale = staleness_days(run_as_of, str(row["as_of_date"]))
    override_reason = str(row["regime_override_reason"] or "")
    if stale is not None and stale > 0:
        carry_reason = f"COVERED_CARRY_FORWARD:{source_table}:{stale}d"
        override_reason = f"{carry_reason}|{override_reason}" if override_reason else carry_reason
    return {
        "run_as_of": run_as_of,
        "macro_as_of_date": row["as_of_date"],
        "active_current_regime": row["active_current_regime"] or "",
        "active_next_regime": row["active_next_regime"] or "",
        "current_confidence": finite_or_blank(row["current_confidence"]),
        "next_confidence": finite_or_blank(row["next_confidence"]),
        "coverage_flag": _coverage_flag(row["coverage_flag"]),
        "regime_override_reason": override_reason,
        "staleness_days": "" if stale is None else stale,
    }


def _sector_rows(
    *,
    run_as_of: str,
    score_rows: list[dict[str, str]],
    target_rows: list[dict[str, str]] | None,
    taxonomy: dict[str, dict[str, Any]],
    sector_as_of: str | None,
    sector_macro_rows: list[Any],
    industry_as_of: str | None,
    industry_rows: list[Any],
    aggregate_as_of: str | None,
    aggregate_rows: list[Any],
) -> list[dict[str, Any]]:
    target_by_pipe = base_target_weights(score_rows, target_rows)
    rows = []
    for pipe in score_pipelines(score_rows):
        fit = select_sleeve_macro_fit(
            run_as_of=run_as_of,
            source_pipeline=pipe,
            taxonomy=taxonomy.get(pipe, {}),
            sector_as_of=sector_as_of,
            sector_rows=sector_macro_rows,
            industry_as_of=industry_as_of,
            industry_rows=industry_rows,
            aggregate_as_of=aggregate_as_of,
            aggregate_rows=aggregate_rows,
        )
        rows.append({
            "run_as_of": run_as_of,
            "source_pipeline": pipe,
            "macro_as_of_date": fit.macro_as_of_date,
            "macro_level": fit.macro_level,
            "macro_key": fit.macro_key,
            "macro_sector_name": fit.macro_sector_name,
            "target_weight": round(float(target_by_pipe.get(pipe, 0.0)), 12),
            "macro_fit_score": fit.macro_fit_score,
            "coverage_flag": fit.coverage_flag,
            "fallback_used": fit.fallback_used,
            "fallback_reason": fit.fallback_reason,
            "staleness_days": fit.staleness_days,
        })
    return rows


def _stock_rows(
    *,
    run_as_of: str,
    score_rows: list[dict[str, str]],
    stock_as_of: str | None,
    stock_fit_rows: list[Any],
    sector_fit_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stock_by_ticker = {str(r["ticker"]).strip().upper(): r for r in stock_fit_rows if r["ticker"] is not None}
    sector_by_pipe = {str(r["source_pipeline"]): r for r in sector_fit_rows}
    stale_stock = None if not stock_as_of else staleness_days(run_as_of, stock_as_of)
    rows = []
    for score in score_rows:
        ticker = str(score.get("ticker", "")).strip().upper()
        pipe = str(score.get("source_pipeline", "")).strip()
        hit = stock_by_ticker.get(ticker)
        if hit is not None and stock_as_of:
            rows.append({
                "run_as_of": run_as_of,
                "ticker": ticker,
                "source_pipeline": pipe,
                "macro_as_of_date": stock_as_of,
                "macro_stock_fit_z": finite_or_blank(hit["macro_stock_fit_z"]),
                "industry_macro_fit": finite_or_blank(hit["industry_macro_fit"]),
                "industry_aggregate_macro_fit": finite_or_blank(hit["industry_aggregate_macro_fit"]),
                "sector_macro_fit": finite_or_blank(hit["sector_macro_fit"]),
                "coverage_flag": _coverage_flag(hit["coverage_flag"]),
                "fallback_used": 0,
                "fallback_reason": "exact_ticker",
                "staleness_days": "" if stale_stock is None else stale_stock,
            })
            continue
        sleeve = sector_by_pipe.get(pipe, {})
        rows.append({
            "run_as_of": run_as_of,
            "ticker": ticker,
            "source_pipeline": pipe,
            "macro_as_of_date": sleeve.get("macro_as_of_date", ""),
            "macro_stock_fit_z": "",
            "industry_macro_fit": "",
            "industry_aggregate_macro_fit": "",
            "sector_macro_fit": sleeve.get("macro_fit_score", ""),
            "coverage_flag": sleeve.get("coverage_flag", ""),
            "fallback_used": 1,
            "fallback_reason": "missing_ticker_used_sleeve_fit",
            "staleness_days": sleeve.get("staleness_days", ""),
        })
    return rows


def _country_rows(run_as_of: str, country_as_of: str | None, rows: list[Any]) -> list[dict[str, Any]]:
    stale = None if not country_as_of else staleness_days(run_as_of, country_as_of)
    return [
        {
            "run_as_of": run_as_of,
            "ticker": str(r["ticker"] or "").strip().upper(),
            "macro_as_of_date": country_as_of or "",
            "ref_area": r["ref_area"] or "",
            "country_name": r["country_name"] or "",
            "region": r["region"] or "",
            "market_class": r["market_class"] or "",
            "country_macro_fit": finite_or_blank(r["country_macro_fit"]),
            "confidence_adjusted_fit": finite_or_blank(r["confidence_adjusted_fit"]),
            "coverage_flag": _coverage_flag(r["coverage_flag"]),
            "staleness_days": "" if stale is None else stale,
        }
        for r in rows
    ]


def _foreign_budget_rows(run_as_of: str, as_of: str | None, rows: list[Any]) -> list[dict[str, Any]]:
    if not rows:
        return [{
            "run_as_of": run_as_of,
            "macro_as_of_date": "",
            "active_flag": 0,
            "foreign_budget": 0.0,
            "min_budget": 0.0,
            "max_budget": 0.0,
            "eligible_candidate_count": 0,
            "selected_candidate_count": 0,
            "activation_reason": "missing_foreign_sleeve_budget_daily",
            "coverage_flag": "",
            "staleness_days": "",
        }]
    stale = None if not as_of else staleness_days(run_as_of, as_of)
    row = rows[0]
    return [{
        "run_as_of": run_as_of,
        "macro_as_of_date": as_of or "",
        "active_flag": int_or_blank(row["active_flag"]),
        "foreign_budget": finite_or_blank(row["foreign_budget"]),
        "min_budget": finite_or_blank(row["min_budget"]),
        "max_budget": finite_or_blank(row["max_budget"]),
        "eligible_candidate_count": int_or_blank(row["eligible_candidate_count"]),
        "selected_candidate_count": int_or_blank(row["selected_candidate_count"]),
        "activation_reason": row["activation_reason"] or "",
        "coverage_flag": _coverage_flag(row["coverage_flag"]),
        "staleness_days": "" if stale is None else stale,
    }]


def _foreign_candidate_rows(run_as_of: str, as_of: str | None, rows: list[Any]) -> list[dict[str, Any]]:
    stale = None if not as_of else staleness_days(run_as_of, as_of)
    return [
        {
            "run_as_of": run_as_of,
            "ticker": str(r["ticker"] or "").strip().upper(),
            "macro_as_of_date": as_of or "",
            "market_name": r["market_name"] or "",
            "region": r["region"] or "",
            "candidate_score": finite_or_blank(r["candidate_score"]),
            "sleeve_weight": finite_or_blank(r["sleeve_weight"]),
            "portfolio_weight_at_budget": finite_or_blank(r["portfolio_weight_at_budget"]),
            "eligible_flag": int_or_blank(r["eligible_flag"]),
            "selected_flag": int_or_blank(r["selected_flag"]),
            "active_flag": int_or_blank(r["active_flag"]),
            "coverage_flag": _coverage_flag(r["coverage_flag"]),
            "staleness_days": "" if stale is None else stale,
        }
        for r in rows
    ]


def main() -> int:
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
    scores_path = run_dir / "stocks_scores.csv"
    stage1_manifest_path = run_dir / "manifest.json"
    if not scores_path.exists() or not stage1_manifest_path.exists():
        LOGGER.error("Stage 1 contract is required first: %s / %s", scores_path, stage1_manifest_path)
        return 1

    serving_db_path = _macro_serving_path(paths.macro_serving_db_path, args.serving_db)
    regime_source = str(cfg_get(config, "macro.regime_source", "v1") or "").strip().lower()
    try:
        regime_table = regime_table_for_source(regime_source)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    if regime_source == "v2":
        regime_model_version = str(cfg_get(config, "macro.regime_v2_model_version", "") or "").strip()
        if not regime_model_version:
            LOGGER.error("macro.regime_v2_model_version is required when macro.regime_source=v2")
            return 1
    elif regime_source == "h1":
        regime_model_version = str(
            cfg_get(config, "macro.regime_h1_model_version", "macro_regime_h1_hybrid_v1") or ""
        ).strip()
        if not regime_model_version:
            LOGGER.error("macro.regime_h1_model_version is required when macro.regime_source=h1")
            return 1
    else:
        regime_model_version = None
    macro_dir = run_dir / "macro"
    paths_out = {
        "macro_regime.csv": macro_dir / "macro_regime.csv",
        "macro_sector_fit.csv": macro_dir / "macro_sector_fit.csv",
        "macro_stock_overlay.csv": macro_dir / "macro_stock_overlay.csv",
        "macro_country_fit.csv": macro_dir / "macro_country_fit.csv",
        "macro_foreign_budget.csv": macro_dir / "macro_foreign_budget.csv",
        "macro_foreign_candidates.csv": macro_dir / "macro_foreign_candidates.csv",
        "macro_contract_meta.json": macro_dir / "macro_contract_meta.json",
    }
    if args.force:
        invalidate_macro_outputs_after_contract_change(macro_dir)
        for path in paths_out.values():
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(paths_out.values(), force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    score_rows = read_csv(scores_path)
    target_rows, optimizer_errors = _load_sealed_optimizer_targets(run_dir)
    if optimizer_errors:
        LOGGER.error("Stage 3 optimizer target contract is not sealed/current: %s", optimizer_errors[:8])
        return 1

    taxonomy = sleeve_taxonomy(config)
    conn = open_macro_serving_db(serving_db_path)
    promotion_manifest_path: Path | None = None
    promotion: Any | None = None
    try:
        if regime_source == "v2":
            promotion = v2_promotion_status(
                conn,
                model_version=str(regime_model_version),
                run_as_of=run_as_of,
            )
            promotion_manifest_path, promotion_errors = _verify_v2_promotion_manifest(
                promotion_row=promotion,
                model_version=str(regime_model_version),
                macro_config_path=PACKAGE_ROOT / "MacroLayer" / "config_macro_raw.yaml",
            )
            if promotion_errors:
                LOGGER.error("V2 regime source is not promotable/current: %s", promotion_errors[:8])
                return 1
        elif regime_source == "h1":
            promotion_manifest_path, h1_errors = h1_promotion_status(
                output_root=PACKAGE_ROOT / "MacroLayer" / "out" / "regime_h1",
                run_as_of=run_as_of,
                model_version=str(regime_model_version),
            )
            if h1_errors:
                LOGGER.error("H1 regime source is not promotable/current: %s", h1_errors[:8])
                return 1
        regime = _regime_row(
            run_as_of,
            single_latest_regime_row(
                conn,
                source=regime_source,
                run_as_of=run_as_of,
                model_version=regime_model_version,
                covered_only=True,
            ),
            source_table=regime_table,
        )
        if regime_source == "v2" and promotion is not None:
            promotion_date = str(promotion["evidence_as_of_date"] or "")
            regime_date = str(regime.get("macro_as_of_date") or "")
            if promotion_date != regime_date:
                LOGGER.error(
                    "V2 promotion evidence is stale relative to the selected decision: evidence=%s regime=%s",
                    promotion_date,
                    regime_date,
                )
                return 1
        # AMENDMENT 2 (A2.3): mirror the V2 evidence-date parity check for H1 - the H1 evidence's
        # evidence_as_of_date must equal the selected macro regime row's macro_as_of_date.
        if regime_source == "h1" and promotion_manifest_path is not None:
            try:
                h1_evidence = json.loads(Path(promotion_manifest_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                LOGGER.error("Unable to read H1 promotion evidence for date parity: %s", exc)
                return 1
            evidence_date = str(h1_evidence.get("evidence_as_of_date") or "")
            regime_date = str(regime.get("macro_as_of_date") or "")
            if evidence_date != regime_date:
                LOGGER.error(
                    "H1 promotion evidence is stale relative to the selected decision: evidence=%s regime=%s",
                    evidence_date,
                    regime_date,
                )
                return 1
        sector_as_of, sector_macro_rows = rows_at_latest(conn, "sector_macro_fit_daily", run_as_of)
        industry_as_of, industry_rows = rows_at_latest(conn, "industry_macro_fit_daily", run_as_of)
        aggregate_as_of, aggregate_rows = rows_at_latest(conn, "industry_aggregate_macro_fit_daily", run_as_of)
        stock_as_of, stock_fit_rows = rows_at_latest(conn, "stock_macro_fit_daily", run_as_of)
        country_as_of, country_fit_rows = rows_at_latest(conn, "country_macro_fit_daily", run_as_of)
        foreign_budget_as_of, foreign_budget = rows_at_latest(conn, "foreign_sleeve_budget_daily", run_as_of)
        foreign_candidate_as_of, foreign_candidates = rows_at_latest(conn, "foreign_sleeve_candidate_daily", run_as_of)
    finally:
        conn.close()

    sector_fit = _sector_rows(
        run_as_of=run_as_of,
        score_rows=score_rows,
        target_rows=target_rows,
        taxonomy=taxonomy,
        sector_as_of=sector_as_of,
        sector_macro_rows=sector_macro_rows,
        industry_as_of=industry_as_of,
        industry_rows=industry_rows,
        aggregate_as_of=aggregate_as_of,
        aggregate_rows=aggregate_rows,
    )
    stock_overlay = _stock_rows(
        run_as_of=run_as_of,
        score_rows=score_rows,
        stock_as_of=stock_as_of,
        stock_fit_rows=stock_fit_rows,
        sector_fit_rows=sector_fit,
    )
    country_fit = _country_rows(run_as_of, country_as_of, country_fit_rows)
    foreign_budget_rows = _foreign_budget_rows(run_as_of, foreign_budget_as_of, foreign_budget)
    foreign_candidate_rows = _foreign_candidate_rows(run_as_of, foreign_candidate_as_of, foreign_candidates)

    macro_dir.mkdir(parents=True, exist_ok=True)
    write_csv(paths_out["macro_regime.csv"], MACRO_REGIME_FIELDS, [regime])
    write_csv(paths_out["macro_sector_fit.csv"], MACRO_SECTOR_FIELDS, sector_fit)
    write_csv(paths_out["macro_stock_overlay.csv"], MACRO_STOCK_FIELDS, stock_overlay)
    write_csv(paths_out["macro_country_fit.csv"], MACRO_COUNTRY_FIELDS, country_fit)
    write_csv(paths_out["macro_foreign_budget.csv"], MACRO_FOREIGN_BUDGET_FIELDS, foreign_budget_rows)
    write_csv(paths_out["macro_foreign_candidates.csv"], MACRO_FOREIGN_CANDIDATE_FIELDS, foreign_candidate_rows)

    source_dates = {
        regime_table: regime["macro_as_of_date"],
        "sector_macro_fit_daily": sector_as_of,
        "industry_macro_fit_daily": industry_as_of,
        "industry_aggregate_macro_fit_daily": aggregate_as_of,
        "stock_macro_fit_daily": stock_as_of,
        "country_macro_fit_daily": country_as_of,
        "foreign_sleeve_budget_daily": foreign_budget_as_of,
        "foreign_sleeve_candidate_daily": foreign_candidate_as_of,
    }
    eligible_tickers = {
        str(row.get("ticker", "")).strip().upper()
        for row in score_rows
        if str(row.get("ticker", "")).strip() and str(row.get("investable_eligible", "")).strip() == "1"
    }
    fallback_tickers = {
        str(row.get("ticker", "")).strip().upper()
        for row in stock_overlay
        if str(row.get("ticker", "")).strip() and str(row.get("fallback_used")) == "1"
    }
    meta = {
        "run_as_of": run_as_of,
        "stage": "stage6_macro_contract",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "macro.enabled_in_production", False)),
        "regime_source": regime_source,
        "regime_source_table": regime_table,
        "regime_model_version": regime_model_version,
        "regime_promotion_manifest_path": str(promotion_manifest_path) if promotion_manifest_path else "",
        "serving_db_path": str(serving_db_path),
        "source_dates": source_dates,
        "sleeve_taxonomy": taxonomy,
        "stock_fallback_policy": str(cfg_get(config, "macro.stock_fallback_policy", "")),
        "counts": {
            "score_rows": len(score_rows),
            "sector_rows": len(sector_fit),
            "stock_rows": len(stock_overlay),
            "country_rows": len(country_fit),
            "foreign_budget_rows": len(foreign_budget_rows),
            "foreign_candidate_rows": len(foreign_candidate_rows),
            "stock_fallback_rows": len(fallback_tickers),
            "eligible_stock_fallback_rows": len(fallback_tickers & eligible_tickers),
        },
        "inputs_sha256": _source_hashes(
            config_path,
            serving_db_path,
            run_dir,
            run_as_of,
            regime_table=regime_table,
            regime_model_version=regime_model_version,
            promotion_manifest_path=promotion_manifest_path,
        ),
        "files": {
            name: {"sha256": sha256_file(path), "rows": sum(1 for _ in path.open("r", encoding="utf-8")) - 1}
            for name, path in paths_out.items()
            if name.endswith(".csv")
        },
    }
    write_manifest(paths_out["macro_contract_meta.json"], meta)
    LOGGER.info(
        "Built Stage 6 macro contract for %s: %d sleeves, %d stock rows, source_dates=%s",
        run_as_of,
        len(sector_fit),
        len(stock_overlay),
        json.dumps(source_dates, sort_keys=True),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
