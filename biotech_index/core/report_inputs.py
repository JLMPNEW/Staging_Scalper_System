from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


def compact_asof(asof_date: str) -> str:
    text = str(asof_date or "").strip()
    if len(text) == 8 and text.isdigit():
        return text
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        return text.replace("-", "")


def dated_output_dir(base_output_dir: Path, asof_date: str) -> Path:
    compact = compact_asof(asof_date)
    return base_output_dir if base_output_dir.name == compact else base_output_dir / compact


def _folder_date_key(path: Path) -> str:
    name = path.name
    return name if len(name) == 8 and name.isdigit() else ""


def resolve_dated_report_input_csv(
    configured_path: Path,
    *,
    base_output_dir: Path,
    asof_date: str,
    logger: logging.Logger | None = None,
) -> Path:
    """Resolve a report input, preferring a point-in-time dated copy.

    Historical recomputation must not use the current root universe when a
    dated universe exists.  Fallbacks are limited to dated folders not after the
    requested as-of date to avoid accidental look-ahead.
    """
    cutoff = compact_asof(asof_date)
    dated_candidate = dated_output_dir(base_output_dir, asof_date) / configured_path.name
    if dated_candidate.exists():
        return dated_candidate

    candidates = [
        path
        for path in base_output_dir.glob(f"*/{configured_path.name}")
        if (key := _folder_date_key(path.parent)) and key <= cutoff
    ]
    candidates.sort(key=lambda path: _folder_date_key(path.parent), reverse=True)
    if candidates:
        if logger is not None:
            logger.warning(
                "Using latest non-lookahead dated report input %s for asof=%s instead of root/current input %s",
                candidates[0],
                asof_date,
                configured_path,
            )
        return candidates[0]
    if configured_path.exists():
        return configured_path
    return configured_path


def resolve_market_snapshot_universe_csv(
    configured_path: Path,
    *,
    base_output_dir: Path,
    requested_asof_date: str,
    effective_market_asof_date: str,
    logger: logging.Logger | None = None,
) -> Path:
    """Resolve universe membership at report date, not the last market date.

    Weekend and holiday market refreshes roll price collection back to the
    latest trading day. Universe membership and adjudications remain effective
    on the requested report date and must not roll back with the prices.
    """
    requested_key = compact_asof(requested_asof_date)
    effective_key = compact_asof(effective_market_asof_date)
    for label, key in (
        ("requested_asof_date", requested_key),
        ("effective_market_asof_date", effective_key),
    ):
        if len(key) != 8 or not key.isdigit():
            raise ValueError(f"Invalid {label}: {key!r}")
        try:
            datetime.strptime(key, "%Y%m%d")
        except ValueError as exc:
            raise ValueError(f"Invalid {label}: {key!r}") from exc
    if effective_key > requested_key:
        raise ValueError(
            "effective_market_asof_date cannot be after requested_asof_date: "
            f"{effective_market_asof_date!r} > {requested_asof_date!r}"
        )

    return resolve_dated_report_input_csv(
        configured_path,
        base_output_dir=base_output_dir,
        asof_date=requested_asof_date,
        logger=logger,
    )
