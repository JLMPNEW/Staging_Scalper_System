from __future__ import annotations

import bisect
import csv
import gzip
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from io import TextIOWrapper
from pathlib import Path
from typing import Iterable, Mapping, Sequence, TextIO


MODEL_FAMILY = "transportation"
OUTCOME_PANEL_VERSION = "transportation_walk_forward_outcome_panel_v1"
DEFAULT_FORWARD_TRADING_DAYS = 63
DEFAULT_MAX_PRICE_STALENESS_DAYS = 10
ACTIVE_PRICE_SOURCE = "yahoo_finance_adjusted"
DELISTED_PRICE_SOURCE = "norgate_us_equities_total_return"
TERMINAL_TYPES = {"acquisition", "distressed_nonzero", "wipeout"}


@dataclass(frozen=True)
class PricePoint:
    bar_date: date
    value: float
    source_id: str
    price_basis: str
    price_adjustment: str = ""


@dataclass(frozen=True)
class MembershipEvent:
    ticker: str
    start_date: date
    end_date: date | None
    membership_status: str
    terminal_type: str = ""
    exit_type: str = ""


@dataclass(frozen=True)
class ContinuityPolicy:
    ticker: str
    current_security_start_date: date
    continuity_policy: str
    structural_break_date: date | None
    history_treatment: str


@dataclass(frozen=True)
class AliasPolicy:
    contract_ticker: str
    active_ticker: str
    predecessor_ticker: str
    effective_date: date


@dataclass(frozen=True)
class OutcomeWindow:
    anchor: PricePoint | None
    forward: PricePoint | None
    outcome_method: str
    unavailable_reason: str
    session_count: int | None
    terminal_type: str = ""

    @property
    def forward_return(self) -> float | None:
        if self.anchor is None or self.forward is None:
            return None
        if self.anchor.value <= 0 or self.forward.value < 0:
            return None
        value = self.forward.value / self.anchor.value - 1.0
        return value if math.isfinite(value) else None


def parse_date(value: object, *, field: str = "date") -> date:
    raw = str(value or "").strip()[:10]
    if not raw:
        raise ValueError(f"{field} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} is not ISO date: {value!r}") from exc


def optional_date(value: object, *, field: str = "date") -> date | None:
    raw = str(value or "").strip()
    return parse_date(raw, field=field) if raw else None


def finite_float(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def fmt(value: float | None, digits: int = 12) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def price_source_order(universe_role: str) -> tuple[str, str]:
    if str(universe_role or "").strip() == "delisted_usable":
        return DELISTED_PRICE_SOURCE, ACTIVE_PRICE_SOURCE
    return ACTIVE_PRICE_SOURCE, DELISTED_PRICE_SOURCE


def resolve_price_ticker(
    ticker: str,
    asof: date,
    aliases: Mapping[str, Sequence[AliasPolicy]],
) -> tuple[str, str]:
    policies = sorted(
        aliases.get(ticker.upper(), ()),
        key=lambda item: item.effective_date,
    )
    if not policies:
        return ticker.upper(), ""
    selected = policies[-1]
    for policy in policies:
        if asof < policy.effective_date:
            selected = policy
            return (
                policy.predecessor_ticker or ticker.upper(),
                "verified_predecessor",
            )
        selected = policy
    return selected.active_ticker or ticker.upper(), "verified_active_alias"


def _anchor_index(
    series: Sequence[PricePoint],
    *,
    asof: date,
    max_staleness_days: int,
) -> int:
    dates = [point.bar_date for point in series]
    index = bisect.bisect_right(dates, asof) - 1
    if index < 0:
        return -1
    point = series[index]
    if point.value <= 0 or (asof - point.bar_date).days > max_staleness_days:
        return -1
    return index


def outcome_window(
    series_by_source: Mapping[str, Sequence[PricePoint]],
    *,
    asof: str,
    forward_trading_days: int,
    source_order: Sequence[str],
    membership: MembershipEvent | None = None,
    horizon_end: date | None = None,
    continuity: ContinuityPolicy | None = None,
    max_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS,
) -> OutcomeWindow:
    if forward_trading_days <= 0:
        raise ValueError("forward_trading_days must be positive")
    asof_date = parse_date(asof, field="asof_date")
    terminal_expected = bool(
        membership is not None
        and membership.end_date is not None
        and horizon_end is not None
        and asof_date < membership.end_date <= horizon_end
    )
    partial_anchor: PricePoint | None = None
    structural_rejection = False
    terminal_rejection = False
    for source_id in source_order:
        series = list(series_by_source.get(source_id, ()))
        if not series:
            continue
        anchor_index = _anchor_index(
            series,
            asof=asof_date,
            max_staleness_days=max_staleness_days,
        )
        if anchor_index < 0:
            continue
        anchor = series[anchor_index]
        partial_anchor = partial_anchor or anchor
        if (
            continuity is not None
            and anchor.bar_date < continuity.current_security_start_date
        ):
            structural_rejection = True
            continue
        if terminal_expected and membership is not None:
            terminal_points = [
                point
                for point in series[anchor_index + 1 :]
                if membership.end_date is not None
                and point.bar_date <= membership.end_date
            ]
            if not terminal_points:
                terminal_rejection = True
                continue
            terminal = terminal_points[-1]
            if (
                membership.end_date is None
                or (membership.end_date - terminal.bar_date).days
                > max_staleness_days
            ):
                terminal_rejection = True
                continue
            if membership.terminal_type == "wipeout":
                terminal = PricePoint(
                    bar_date=membership.end_date,
                    value=0.0,
                    source_id=source_id,
                    price_basis="reviewed_terminal_zero",
                    price_adjustment=(
                        "terminal_type=wipeout;"
                        f"last_verified_bar={terminal.bar_date.isoformat()}"
                    ),
                )
            elif membership.terminal_type not in TERMINAL_TYPES:
                terminal_rejection = True
                continue
            if (
                continuity is not None
                and continuity.structural_break_date is not None
                and anchor.bar_date
                < continuity.structural_break_date
                <= terminal.bar_date
            ):
                structural_rejection = True
                continue
            return OutcomeWindow(
                anchor=anchor,
                forward=terminal,
                outcome_method="terminal_membership_exit",
                unavailable_reason="",
                session_count=len(terminal_points),
                terminal_type=membership.terminal_type,
            )
        forward_index = anchor_index + forward_trading_days
        if forward_index >= len(series):
            continue
        forward = series[forward_index]
        if (
            continuity is not None
            and continuity.structural_break_date is not None
            and anchor.bar_date
            < continuity.structural_break_date
            <= forward.bar_date
        ):
            structural_rejection = True
            continue
        return OutcomeWindow(
            anchor=anchor,
            forward=forward,
            outcome_method="standard_forward_sessions",
            unavailable_reason="",
            session_count=forward_trading_days,
        )
    if structural_rejection:
        reason = "security_continuity_boundary_violation"
    elif terminal_expected and terminal_rejection:
        reason = "missing_verified_terminal_outcome"
    elif partial_anchor is not None:
        reason = "right_censored_missing_forward_price"
    else:
        reason = "missing_or_stale_asof_price"
    return OutcomeWindow(
        anchor=partial_anchor,
        forward=None,
        outcome_method="",
        unavailable_reason=reason,
        session_count=None,
    )


def rank_usable_period_count(
    rows: Iterable[Mapping[str, str]],
    *,
    minimum_tickers: int = 3,
) -> int:
    by_date: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        if str(row.get("panel_row_eligible_flag") or "") != "1":
            continue
        value = finite_float(row.get("direction_adjusted_metric_value"))
        ticker = str(row.get("ticker") or "")
        asof = str(row.get("asof_date") or "")
        if value is None or not ticker or not asof:
            continue
        by_date.setdefault(asof, []).append((ticker, value))
    usable = 0
    for observations in by_date.values():
        tickers = {ticker for ticker, _ in observations}
        values = {value for _, value in observations}
        if len(tickers) >= minimum_tickers and len(values) >= 2:
            usable += 1
    return usable


def write_gzip_csv_atomic(
    path: Path,
    fields: Sequence[str],
    rows: Iterable[Mapping[str, object]],
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".tmp-",
        suffix=".csv.gz",
        dir=str(path.parent),
    )
    raw = os.fdopen(descriptor, "wb")
    compressed = gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=raw,
        mtime=0,
    )
    text: TextIO = TextIOWrapper(
        compressed,
        encoding="utf-8",
        newline="",
    )
    count = 0
    try:
        writer = csv.DictWriter(text, fieldnames=list(fields))
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
            count += 1
        text.flush()
        text.close()
        raw.close()
        os.replace(temp_name, path)
    except BaseException:
        try:
            text.close()
        finally:
            raw.close()
            try:
                Path(temp_name).unlink(missing_ok=True)
            except PermissionError:
                pass
        raise
    return count
