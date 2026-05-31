from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from biotech_index.core.config import cfg_get


DEFAULT_SCORING_MARKET_SOURCES = ["yahoo_adjusted", "interactive_brokers"]
DEFAULT_CALIBRATION_MARKET_SOURCES = ["yahoo_adjusted", "interactive_brokers"]


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
    fallback_sources = normalize_source_list(fallback, ["interactive_brokers"]) if fallback is not None else ["interactive_brokers"]
    return normalize_source_list([primary, *fallback_sources], DEFAULT_SCORING_MARKET_SOURCES)


def calibration_market_sources(config: dict[str, Any]) -> list[str]:
    return normalize_source_list(
        cfg_get(config, "market_data_policy.calibration_sources", None),
        DEFAULT_CALIBRATION_MARKET_SOURCES,
    )


def live_validation_primary_source(config: dict[str, Any]) -> str:
    sources = normalize_source_list(
        cfg_get(config, "market_data_policy.live_validation_primary_source", "interactive_brokers"),
        ["interactive_brokers"],
    )
    return sources[0]


def source_priority_index(source_priority: list[str]) -> dict[str, int]:
    return {source: idx for idx, source in enumerate(normalize_source_list(source_priority, []))}


def row_date(raw: object) -> date | None:
    raw_text = str(raw or "").strip()
    if len(raw_text) > 10 and raw_text[10] not in {"T", " "}:
        return None
    text = raw_text[:10]
    if not text:
        return None
    try:
        from datetime import datetime

        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def select_latest_rows_by_source_priority(
    rows: Iterable[Any],
    *,
    asof_date: date,
    source_priority: list[str],
    max_staleness_days: int,
) -> dict[int, dict[str, Any]]:
    priority = source_priority_index(source_priority)
    candidates: list[tuple[tuple[int, int, int, int], int, dict[str, Any]]] = []
    max_age = max(0, int(max_staleness_days))
    for row in rows:
        row_dict = dict(row)
        company_id = int(row_dict["company_id"])
        row_asof = row_date(row_dict.get("asof_date"))
        if row_asof is None or row_asof > asof_date:
            continue
        age_days = (asof_date - row_asof).days if row_asof is not None else 999_999
        stale_rank = 1 if age_days > max_age else 0
        source = str(row_dict.get("source") or "").strip().lower()
        priority_rank = priority.get(source, 999)
        # Negative ordinal makes a newer row sort before an older row once source/staleness are equal.
        recency_rank = -(row_asof.toordinal() if row_asof is not None else 0)
        candidates.append(((company_id, stale_rank, priority_rank, recency_rank), company_id, row_dict))
    candidates.sort(key=lambda item: item[0])
    selected: dict[int, dict[str, Any]] = {}
    for _, company_id, row_dict in candidates:
        if company_id not in selected:
            selected[company_id] = row_dict
    return selected
