from __future__ import annotations

from dataclasses import dataclass
import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


TRUE_VALUES = {"1", "true", "t", "yes", "y"}


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in TRUE_VALUES


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def split_ticker_filter(raw: object) -> set[str]:
    return {ticker for part in str(raw or "").split(",") if (ticker := normalize_ticker(part))}


def read_final_scoring_tickers(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"Final scoring universe CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        out = {
            ticker
            for row in reader
            if (ticker := normalize_ticker(row.get("ticker")))
            and as_bool(row.get("scoring_include"))
        }
    if not out:
        raise ValueError(f"Final scoring universe CSV contains no scoring tickers: {path}")
    return out


@dataclass(frozen=True)
class UniverseCoverage:
    expected_count: int
    observed_count: int
    missing_tickers: tuple[str, ...]
    extra_tickers: tuple[str, ...]


def universe_coverage(expected_tickers: Iterable[str], observed_tickers: Iterable[str]) -> UniverseCoverage:
    expected = {normalize_ticker(ticker) for ticker in expected_tickers if normalize_ticker(ticker)}
    observed = {normalize_ticker(ticker) for ticker in observed_tickers if normalize_ticker(ticker)}
    return UniverseCoverage(
        expected_count=len(expected),
        observed_count=len(observed),
        missing_tickers=tuple(sorted(expected - observed)),
        extra_tickers=tuple(sorted(observed - expected)),
    )


def format_ticker_sample(tickers: Sequence[str], *, limit: int = 25) -> str:
    sample = list(tickers[:limit])
    suffix = "" if len(tickers) <= limit else f"...(+{len(tickers) - limit})"
    return ",".join(sample) + suffix


def parse_asof_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_layer_freshness(
    *,
    base_rows: Iterable[Mapping[str, Any]],
    layer_rows_by_company: Mapping[int, Mapping[str, Any]],
    asof_date: date | str,
    context: str,
    max_staleness_days: int = 0,
) -> None:
    target_date = parse_asof_date(asof_date)
    if target_date is None:
        raise ValueError(f"Invalid asof_date for {context}: {asof_date}")
    missing: list[str] = []
    stale: list[str] = []
    future: list[str] = []
    invalid_date: list[str] = []
    for base_row in base_rows:
        company_id = int(base_row["company_id"])
        ticker = normalize_ticker(base_row.get("ticker")) or str(company_id)
        layer_row = layer_rows_by_company.get(company_id)
        if not layer_row:
            missing.append(ticker)
            continue
        layer_date = parse_asof_date(layer_row.get("asof_date"))
        if layer_date is None:
            invalid_date.append(ticker)
            continue
        age_days = (target_date - layer_date).days
        if age_days < 0:
            future.append(f"{ticker}:{layer_date.isoformat()}")
        elif age_days > max_staleness_days:
            stale.append(f"{ticker}:{layer_date.isoformat()}")
    failures: list[str] = []
    if missing:
        failures.append(f"missing {len(missing)} ticker(s): {format_ticker_sample(sorted(missing))}")
    if stale:
        failures.append(
            f"stale {len(stale)} ticker(s) older than {max_staleness_days} day(s): "
            f"{format_ticker_sample(sorted(stale))}"
        )
    if future:
        failures.append(f"future-dated {len(future)} ticker(s): {format_ticker_sample(sorted(future))}")
    if invalid_date:
        failures.append(f"invalid asof_date {len(invalid_date)} ticker(s): {format_ticker_sample(sorted(invalid_date))}")
    if failures:
        raise RuntimeError(f"{context} freshness validation failed for asof={target_date.isoformat()}: " + " | ".join(failures))


def validate_requested_tickers(*, requested_tickers: set[str], loaded_tickers: Iterable[str], context: str) -> None:
    if not requested_tickers:
        return
    loaded = {normalize_ticker(ticker) for ticker in loaded_tickers if normalize_ticker(ticker)}
    missing = tuple(sorted(requested_tickers - loaded))
    if missing:
        raise RuntimeError(f"{context} did not load requested ticker(s): {format_ticker_sample(missing)}")


def validate_nonempty_selection(*, count: int, context: str, subset_mode: bool = False) -> None:
    if count <= 0:
        suffix = " for requested subset" if subset_mode else ""
        raise RuntimeError(f"{context} selected zero companies{suffix}; refusing to continue")


def validate_full_universe_coverage(
    *,
    expected_tickers: set[str],
    observed_tickers: Iterable[str],
    context: str,
    subset_mode: bool,
) -> UniverseCoverage:
    coverage = universe_coverage(expected_tickers, observed_tickers)
    if subset_mode:
        return coverage
    if coverage.missing_tickers:
        raise RuntimeError(
            f"{context} missing {len(coverage.missing_tickers)} final-universe ticker(s) "
            f"before write: {format_ticker_sample(coverage.missing_tickers)}"
        )
    return coverage


def validate_output_coverage(
    *,
    expected_tickers: set[str],
    output_tickers: Iterable[str],
    context: str,
    subset_mode: bool,
) -> UniverseCoverage:
    coverage = universe_coverage(expected_tickers, output_tickers)
    if subset_mode:
        return coverage
    if coverage.missing_tickers:
        raise RuntimeError(
            f"{context} output missing {len(coverage.missing_tickers)} final-universe ticker(s): "
            f"{format_ticker_sample(coverage.missing_tickers)}"
        )
    return coverage


def subset_mode_enabled(*, ticker_filter: set[str], max_count: int = 0) -> bool:
    return bool(ticker_filter) or int(max_count or 0) > 0


def subset_output_path(path: Path, *, subset_mode: bool) -> Path:
    if not subset_mode:
        return path
    return path.with_name(f"{path.stem}_subset{path.suffix}")
