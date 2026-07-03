from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Iterable

from med_devices.core.config import cfg_get


DEFAULT_SCORING_MARKET_SOURCES = ["yahoo_finance_backup", "ib_market_data"]
DEFAULT_CALIBRATION_MARKET_SOURCES = ["yahoo_finance_backup", "ib_market_data"]
DEFAULT_LIVE_VALIDATION_SOURCE = "ib_market_data"


def normalize_source_list(raw: object, default: Iterable[str]) -> list[str]:
    if raw is None:
        candidates = list(default)
    elif isinstance(raw, str):
        candidates = raw.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        candidates = [str(item) for item in raw]
    else:
        candidates = [str(raw)]

    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        source = str(candidate or "").strip().lower()
        if not source or source in seen:
            continue
        out.append(source)
        seen.add(source)
    return out or list(default)


def scoring_market_sources(config: dict[str, Any]) -> list[str]:
    raw_sources = cfg_get(config, "market_data_policy.scoring_sources", None)
    if raw_sources is not None:
        return normalize_source_list(raw_sources, DEFAULT_SCORING_MARKET_SOURCES)
    primary = str(cfg_get(config, "market_data_policy.scoring_primary_source", "") or "").strip()
    fallback = cfg_get(config, "market_data_policy.scoring_fallback_sources", None)
    if not primary and fallback is None:
        return list(DEFAULT_SCORING_MARKET_SOURCES)
    fallback_sources = normalize_source_list(fallback, ["ib_market_data"]) if fallback is not None else ["ib_market_data"]
    return normalize_source_list([primary, *fallback_sources], DEFAULT_SCORING_MARKET_SOURCES)


def calibration_market_sources(config: dict[str, Any]) -> list[str]:
    return normalize_source_list(
        cfg_get(config, "market_data_policy.calibration_sources", None),
        DEFAULT_CALIBRATION_MARKET_SOURCES,
    )


def live_validation_primary_source(config: dict[str, Any]) -> str:
    return str(
        cfg_get(config, "market_data_policy.live_validation_primary_source", DEFAULT_LIVE_VALIDATION_SOURCE)
        or DEFAULT_LIVE_VALIDATION_SOURCE
    ).strip().lower()


def source_priority_index(source_priority: list[str]) -> dict[str, int]:
    return {source: idx for idx, source in enumerate(source_priority)}


def row_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def is_adjusted_price_row(row: dict[str, Any]) -> bool:
    adjusted_raw = row.get("is_adjusted")
    flag = "" if adjusted_raw is None else str(adjusted_raw).strip().lower()
    if flag in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if flag:
        # Explicit non-truthy flag (e.g. "0", "false") means unadjusted; skip the heuristic.
        return False
    try:
        adj_close = float(str(row.get("adj_close")).strip())
    except (TypeError, ValueError):
        return False
    return math.isfinite(adj_close) and adj_close > 0.0


def price_adjustment_label(row: dict[str, Any]) -> str:
    return "adjusted" if is_adjusted_price_row(row) else "raw"


def select_latest_rows_by_source_priority(
    rows: Iterable[Any],
    *,
    asof_date: date,
    source_priority: list[str],
    max_staleness_days: int,
    entity_key: str = "ticker",
    source_key: str = "source_id",
    date_key: str = "bar_date",
    drop_stale: bool = False,
    allow_unlisted_sources: bool = True,
    selection_meta: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    priority = source_priority_index(source_priority)
    candidates: list[tuple[tuple[str, int, int, int], str, dict[str, Any], dict[str, Any]]] = []
    max_age = max(0, int(max_staleness_days))
    for row in rows:
        row_dict = dict(row)
        entity = str(row_dict.get(entity_key) or "").strip().upper()
        if not entity:
            continue
        item_date = row_date(row_dict.get(date_key))
        age_days = (asof_date - item_date).days if item_date is not None else 999_999
        stale_rank = 1 if age_days > max_age else 0
        if drop_stale and stale_rank:
            continue
        source = str(row_dict.get(source_key) or "").strip().lower()
        if not allow_unlisted_sources and source not in priority:
            continue
        priority_rank = priority.get(source, 999)
        # Negative ordinal makes a newer row sort before an older row once source/staleness are equal.
        recency_rank = -(item_date.toordinal() if item_date is not None else 0)
        policy_flags = []
        if stale_rank:
            policy_flags.append("stale")
        if source not in priority:
            policy_flags.append("unlisted_source")
        meta = {
            "source": source,
            "bar_date": str(row_dict.get(date_key) or ""),
            "bar_age_days": age_days,
            "policy_status": ";".join(policy_flags) if policy_flags else "ok",
        }
        candidates.append(((entity, stale_rank, priority_rank, recency_rank), entity, row_dict, meta))

    candidates.sort(key=lambda item: item[0])
    selected: dict[str, dict[str, Any]] = {}
    for _, entity, row_dict, meta in candidates:
        if entity not in selected:
            selected[entity] = row_dict
            if selection_meta is not None:
                selection_meta[entity] = meta
    return selected
