"""Sleeve taxonomy and MacroLayer fit selection for Stage 6."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from portfolio_layer.core.config import cfg_get
from portfolio_layer.macro.contract import finite_or_blank, staleness_days


@dataclass(frozen=True)
class MacroFit:
    macro_as_of_date: str
    macro_level: str
    macro_key: str
    macro_sector_name: str
    macro_fit_score: float | str
    coverage_flag: int | str
    fallback_used: int
    fallback_reason: str
    staleness_days: int | str


def norm_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("&", "and").split())


def sleeve_taxonomy(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    payload = cfg_get(config, "macro.sleeve_taxonomy", {}) or {}
    if not isinstance(payload, dict):
        return {}
    return {str(k).strip(): dict(v or {}) for k, v in payload.items() if str(k).strip()}


def score_pipelines(score_rows: Iterable[dict[str, str]]) -> list[str]:
    return sorted({str(r.get("source_pipeline", "")).strip() for r in score_rows if str(r.get("source_pipeline", "")).strip()})


def base_target_weights(score_rows: list[dict[str, str]], target_rows: list[dict[str, str]] | None = None) -> dict[str, float]:
    """Return neutral per-sleeve target weights for the macro contract.

    Prefer the sealed Stage 3 target book when present. If Stage 3 is unavailable, fall back
    to investable-eligible count share so Stage 6 can still be built after Stage 1.
    """
    pipe_by_ticker = {
        str(r.get("ticker", "")).strip().upper(): str(r.get("source_pipeline", "")).strip()
        for r in score_rows
        if str(r.get("ticker", "")).strip() and str(r.get("source_pipeline", "")).strip()
    }
    by_pipe: dict[str, float] = defaultdict(float)
    if target_rows:
        for row in target_rows:
            ticker = str(row.get("ticker", "")).strip().upper()
            pipe = pipe_by_ticker.get(ticker)
            if not pipe:
                continue
            try:
                weight = float(row.get("weight", 0.0))
            except (TypeError, ValueError):
                continue
            if weight > 0.0:
                by_pipe[pipe] += weight
    if by_pipe:
        return dict(by_pipe)

    for row in score_rows:
        if str(row.get("investable_eligible", "")).strip() != "1":
            continue
        pipe = str(row.get("source_pipeline", "")).strip()
        if pipe:
            by_pipe[pipe] += 1.0
    total = sum(by_pipe.values())
    if total <= 0:
        for pipe in score_pipelines(score_rows):
            by_pipe[pipe] = 1.0
        total = sum(by_pipe.values())
    return {pipe: value / total for pipe, value in by_pipe.items()} if total > 0 else {}


def _row_by_key(rows: list[sqlite3.Row], column: str) -> dict[str, sqlite3.Row]:
    return {norm_key(row[column]): row for row in rows if row[column] is not None}


def _best_named_row(
    rows_by_key: dict[str, sqlite3.Row],
    names: Iterable[Any],
) -> tuple[str, sqlite3.Row] | None:
    for name in names:
        key = norm_key(name)
        if key and key in rows_by_key:
            return str(name), rows_by_key[key]
    return None


def select_sleeve_macro_fit(
    *,
    run_as_of: str,
    source_pipeline: str,
    taxonomy: dict[str, Any],
    sector_as_of: str | None,
    sector_rows: list[sqlite3.Row],
    industry_as_of: str | None,
    industry_rows: list[sqlite3.Row],
    aggregate_as_of: str | None,
    aggregate_rows: list[sqlite3.Row],
) -> MacroFit:
    """Pick the most granular MacroLayer fit available for a portfolio sleeve."""
    industry_hit = _best_named_row(_row_by_key(industry_rows, "industry_name"), taxonomy.get("industries", []) or [])
    if industry_hit and industry_as_of:
        name, row = industry_hit
        stale = staleness_days(run_as_of, industry_as_of)
        return MacroFit(
            macro_as_of_date=industry_as_of,
            macro_level="industry",
            macro_key=name,
            macro_sector_name=str(row["sector_name"] or ""),
            macro_fit_score=finite_or_blank(row["final_score"]),
            coverage_flag=int(row["coverage_flag"]) if row["coverage_flag"] is not None else "",
            fallback_used=0,
            fallback_reason="exact_industry",
            staleness_days="" if stale is None else stale,
        )

    aggregate_hit = _best_named_row(
        _row_by_key(aggregate_rows, "industry_aggregate_name"),
        taxonomy.get("industry_aggregates", []) or [],
    )
    if aggregate_hit and aggregate_as_of:
        name, row = aggregate_hit
        stale = staleness_days(run_as_of, aggregate_as_of)
        return MacroFit(
            macro_as_of_date=aggregate_as_of,
            macro_level="industry_aggregate",
            macro_key=name,
            macro_sector_name=str(row["sector_name"] or ""),
            macro_fit_score=finite_or_blank(row["final_score"]),
            coverage_flag=int(row["coverage_flag"]) if row["coverage_flag"] is not None else "",
            fallback_used=1,
            fallback_reason="industry_missing_used_aggregate",
            staleness_days="" if stale is None else stale,
        )

    fallback_sector = taxonomy.get("macro_sector_fallback", "")
    sector_hit = _best_named_row(_row_by_key(sector_rows, "sector_name"), [fallback_sector])
    if sector_hit and sector_as_of:
        name, row = sector_hit
        stale = staleness_days(run_as_of, sector_as_of)
        return MacroFit(
            macro_as_of_date=sector_as_of,
            macro_level="sector",
            macro_key=name,
            macro_sector_name=name,
            macro_fit_score=finite_or_blank(row["final_score"]),
            coverage_flag=int(row["coverage_flag"]) if row["coverage_flag"] is not None else "",
            fallback_used=1,
            fallback_reason="granular_missing_used_sector",
            staleness_days="" if stale is None else stale,
        )

    return MacroFit(
        macro_as_of_date="",
        macro_level="missing",
        macro_key=source_pipeline,
        macro_sector_name=str(fallback_sector or ""),
        macro_fit_score="",
        coverage_flag="",
        fallback_used=1,
        fallback_reason="no_macro_fit",
        staleness_days="",
    )

