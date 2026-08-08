#!/usr/bin/env python3
"""Stage 12b - enrich sealed target weights with advisory and current-holdings context."""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    manifest_acceptance_value,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_manifest,
    write_text_atomic,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.ledger.ledger_common import parse_number  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    monitor_output_subdir,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("enrich_final_target_book")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
NON_SECURITY_TICKERS = {"CASH", "PAYOUT_RESERVED"}
MACRO_FIELDS = [
    "active_current_regime",
    "active_next_regime",
    "current_confidence",
    "next_confidence",
    "macro_as_of_date",
]
IB_PERFORMANCE_FIELDS = [
    "ib_mark_to_market_mtd_profit",
    "ib_mark_to_market_ytd_profit",
    "ib_profit_as_of_date",
]
IB_REALIZED_ROWS = {
    "mtd": [
        "ib_realized_profit_loss_mtd",
        "ib_realized_short_term_mtd",
        "ib_realized_long_term_mtd",
        "ib_dividends_mtd",
        "ib_net_broker_interest_mtd",
    ],
    "ytd": [
        "ib_realized_profit_loss_ytd",
        "ib_realized_short_term_ytd",
        "ib_realized_long_term_ytd",
        "ib_dividends_ytd",
        "ib_net_broker_interest_ytd",
    ],
}
BOOK_FIELDS = [
    "ticker",
    "ib_symbol",
    "weight",
    "IB_Holding",
    "IB_quantity",
    "ib_holding_as_of",
    "is_scored",
    "is_monitored",
    "next_earnings_date",
    "sector",
    "rating",
    "final_score",
    "score_confidence",
    "internal_state",
    "action_state",
    "benchmark_ticker",
    "rel_ret_5d",
    "rel_ret_20d",
    "current_price",
    "price_source",
    "ma50",
    "ma200",
    "below_ma50",
    "below_ma200",
    "price_band_status",
    "price_band_basis",
    "starter_band_low",
    "starter_band_high",
    "add_band_low",
    "add_band_high",
    "trim_band_low",
    "trim_band_high",
]


def _display_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        return text
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def write_final_report(
    path: Path,
    *,
    ib_performance: dict[str, Any],
    macro: dict[str, str],
    rows: list[dict[str, Any]],
) -> None:
    """Write IB performance, five macro rows, and one book table."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    performance_row: list[Any] = []
    for field in IB_PERFORMANCE_FIELDS:
        value = ib_performance.get(field, "")
        performance_row.extend(
            [field, _display_date(value) if field == "ib_profit_as_of_date" else value]
        )
    writer.writerow(performance_row)
    for fields in IB_REALIZED_ROWS.values():
        realized_row: list[Any] = []
        for field in fields:
            realized_row.extend([field, ib_performance.get(field, "")])
        writer.writerow(realized_row)
    for field in MACRO_FIELDS:
        value = macro.get(field, "")
        writer.writerow([field, _display_date(value) if field == "macro_as_of_date" else value])
    writer.writerow([])
    writer.writerow(BOOK_FIELDS)
    for row in rows:
        writer.writerow([row.get(field, "") for field in BOOK_FIELDS])
    write_text_atomic(path, buffer.getvalue())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sealed_csv(
    artifact: Path,
    manifest_path: Path,
    *,
    keys: tuple[str, ...],
    run_as_of: str,
    accepted: set[str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    manifest = read_manifest(manifest_path)
    acceptance = manifest_acceptance_value(manifest)
    if acceptance not in accepted:
        raise ValueError(
            f"Upstream manifest did not pass: {manifest_path} acceptance={acceptance}"
        )
    manifest_date = str(
        manifest.get("run_as_of", manifest.get("as_of_date", ""))
    ).strip()
    if manifest_date != run_as_of:
        raise ValueError(
            f"Upstream manifest date mismatch: {manifest_path} "
            f"actual={manifest_date or 'MISSING'} expected={run_as_of}"
        )
    errors = sealed_artifact_errors(
        manifest,
        artifact,
        *keys,
        allow_deferred=True,
    )
    if errors:
        raise ValueError(f"Unsealed/stale input {artifact}: {errors}")
    return read_csv(artifact), manifest


def _latest_ledger_run(
    runs_root: Path, run_as_of: str, *, max_staleness_days: int
) -> tuple[Path, int, list[dict[str, str]]]:
    """Newest PASS ledger run on or before run_as_of, bounded by max_staleness_days.

    Returns (run_dir, ledger_age_days, skipped_newer) where skipped_newer records every
    newer candidate that was passed over because its manifest is FAIL/corrupt, so the
    consumer manifest can surface them instead of silently walking past.
    """
    candidates = sorted(
        (
            path
            for path in runs_root.iterdir()
            if path.is_dir()
            and path.name <= run_as_of
            and (path / "ledger" / "ledger_manifest.json").is_file()
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No accepted broker ledger exists on or before {run_as_of}")
    run_date = date.fromisoformat(run_as_of)
    skipped: list[dict[str, str]] = []
    for candidate in candidates:
        manifest_path = candidate / "ledger" / "ledger_manifest.json"
        try:
            manifest = read_manifest(manifest_path)
        except ValueError as exc:
            skipped.append({"run": candidate.name, "reason": f"corrupt_manifest: {exc}"})
            continue
        acceptance = manifest_acceptance_value(manifest)
        if acceptance != "PASS":
            skipped.append(
                {"run": candidate.name, "reason": f"acceptance={acceptance or 'MISSING'}"}
            )
            continue
        try:
            ledger_date = date.fromisoformat(candidate.name)
        except ValueError as exc:
            raise ValueError(
                f"Ledger run directory name is not an ISO date: {candidate.name}"
            ) from exc
        age_days = (run_date - ledger_date).days
        if age_days > max_staleness_days:
            raise ValueError(
                f"Newest PASS broker ledger {candidate.name} is {age_days} days old for run "
                f"{run_as_of}, beyond holdings_ledger.max_staleness_days={max_staleness_days}; "
                f"newer non-PASS ledgers skipped: {skipped or 'none'}"
            )
        return candidate, age_days, skipped
    raise ValueError(
        f"No PASS broker ledger exists on or before {run_as_of}; "
        f"candidates skipped: {skipped}"
    )


def _current_price(level: dict[str, str] | None) -> float | None:
    if level is None:
        return None
    try:
        market = json.loads(str(level.get("market_structure_json", "{}")))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid levels market_structure_json for {level.get('ticker', '')!r}"
        ) from exc
    if not isinstance(market, dict):
        raise ValueError(
            f"Levels market_structure_json is not an object for {level.get('ticker', '')!r}"
        )
    return _finite(market.get("latest_price"))


def _market_context(signal: dict[str, str] | None) -> dict[str, Any]:
    if signal is None:
        return {
            "benchmark_ticker": "",
            "rel_ret_5d": "",
            "rel_ret_20d": "",
            "ma50": "",
            "ma200": "",
            "below_ma50": "",
            "below_ma200": "",
            "market_latest_price": None,
        }
    try:
        inputs = json.loads(str(signal.get("inputs_json", "{}")))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid market inputs_json for {signal.get('ticker', '')}"
        ) from exc
    if not isinstance(inputs, dict):
        raise ValueError(
            f"Market inputs_json is not an object for {signal.get('ticker', '')}"
        )
    return {
        "benchmark_ticker": str(signal.get("benchmark_ticker", "")).strip(),
        "rel_ret_5d": signal.get("rel_ret_5d", ""),
        "rel_ret_20d": signal.get("rel_ret_20d", ""),
        "ma50": inputs.get("ma50", ""),
        "ma200": inputs.get("ma200", ""),
        "below_ma50": signal.get("below_ma50", ""),
        "below_ma200": signal.get("below_ma200", ""),
        "market_latest_price": _finite(inputs.get("latest_adj_close")),
    }


def _consistent_pair(
    values: list[tuple[float, float]], *, label: str
) -> tuple[float, float]:
    if not values:
        raise ValueError(f"IB statement has no {label}")
    expected_mtd, expected_ytd = values[0]
    if any(
        not math.isclose(mtd, expected_mtd, abs_tol=1e-8)
        or not math.isclose(ytd, expected_ytd, abs_tol=1e-8)
        for mtd, ytd in values[1:]
    ):
        raise ValueError(f"IB duplicate {label} MTD/YTD values disagree")
    return expected_mtd, expected_ytd


def _ib_performance(path: Path, *, as_of: str) -> dict[str, Any]:
    performance_section = "Month & Year to Date Performance Summary"
    cash_section = "Cash Report"
    headers: dict[str, list[str]] = {}
    totals: list[tuple[float, float, float, float, float, float]] = []
    cash_values: dict[str, list[tuple[float, float]]] = {
        "Dividends": [],
        "Broker Interest Paid and Received": [],
    }
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 2 or row[0] not in {performance_section, cash_section}:
                continue
            section = row[0]
            if row[1] == "Header":
                headers[section] = row[2:]
                continue
            header = headers.get(section)
            if row[1] != "Data" or header is None:
                continue
            mapped = {
                field.strip(): (
                    row[index + 2].strip() if index + 2 < len(row) else ""
                )
                for index, field in enumerate(header)
            }
            if section == performance_section:
                if mapped.get("Asset Category") != "Total (All Assets)":
                    continue
                # IB statement cells use ledger importer semantics: "--" sentinel,
                # parenthesized negatives, thousands commas (ledger_common.parse_number).
                parsed_values: list[float] = []
                missing_fields: list[str] = []
                for field in (
                    "Mark-to-Market MTD",
                    "Mark-to-Market YTD",
                    "Realized S/T MTD",
                    "Realized S/T YTD",
                    "Realized L/T MTD",
                    "Realized L/T YTD",
                ):
                    parsed = parse_number(mapped.get(field))
                    if parsed is None:
                        missing_fields.append(f"{field}={mapped.get(field)!r}")
                    else:
                        parsed_values.append(parsed)
                if missing_fields:
                    raise ValueError(
                        "IB Total (All Assets) performance values are "
                        f"missing/non-numeric: {', '.join(missing_fields)}"
                    )
                totals.append(
                    (
                        parsed_values[0],
                        parsed_values[1],
                        parsed_values[2],
                        parsed_values[3],
                        parsed_values[4],
                        parsed_values[5],
                    )
                )
                continue
            component = mapped.get("Currency Summary", "")
            if (
                component not in cash_values
                or mapped.get("Currency") != "Base Currency Summary"
            ):
                continue
            mtd = parse_number(mapped.get("Month to Date"))
            ytd = parse_number(mapped.get("Year to Date"))
            if mtd is None or ytd is None:
                raise ValueError(
                    f"IB Cash Report {component} MTD/YTD values are missing/non-numeric: "
                    f"mtd={mapped.get('Month to Date')!r} ytd={mapped.get('Year to Date')!r}"
                )
            cash_values[component].append((mtd, ytd))
    if not totals:
        raise ValueError(
            f"IB statement has no {performance_section!r} Total (All Assets) row"
        )
    if cash_section not in headers:
        raise ValueError("IB statement has no Cash Report section")
    expected = totals[0]
    if any(
        any(
            not math.isclose(actual, target, abs_tol=1e-8)
            for actual, target in zip(values, expected, strict=True)
        )
        for values in totals[1:]
    ):
        raise ValueError("IB duplicate Total (All Assets) performance values disagree")
    (
        mark_to_market_mtd,
        mark_to_market_ytd,
        realized_short_term_mtd,
        realized_short_term_ytd,
        realized_long_term_mtd,
        realized_long_term_ytd,
    ) = expected
    dividends_mtd, dividends_ytd = (
        _consistent_pair(cash_values["Dividends"], label="Cash Report Dividends")
        if cash_values["Dividends"]
        else (0.0, 0.0)
    )
    interest_mtd, interest_ytd = (
        _consistent_pair(
            cash_values["Broker Interest Paid and Received"],
            label="Cash Report Broker Interest Paid and Received",
        )
        if cash_values["Broker Interest Paid and Received"]
        else (0.0, 0.0)
    )
    realized_profit_loss_mtd = round(
        realized_short_term_mtd
        + realized_long_term_mtd
        + dividends_mtd
        + interest_mtd,
        8,
    )
    realized_profit_loss_ytd = round(
        realized_short_term_ytd
        + realized_long_term_ytd
        + dividends_ytd
        + interest_ytd,
        8,
    )
    return {
        "ib_mark_to_market_mtd_profit": mark_to_market_mtd,
        "ib_mark_to_market_ytd_profit": mark_to_market_ytd,
        "ib_realized_profit_loss_mtd": realized_profit_loss_mtd,
        "ib_realized_profit_loss_ytd": realized_profit_loss_ytd,
        "ib_realized_short_term_mtd": realized_short_term_mtd,
        "ib_realized_short_term_ytd": realized_short_term_ytd,
        "ib_realized_long_term_mtd": realized_long_term_mtd,
        "ib_realized_long_term_ytd": realized_long_term_ytd,
        "ib_dividends_mtd": dividends_mtd,
        "ib_dividends_ytd": dividends_ytd,
        "ib_net_broker_interest_mtd": interest_mtd,
        "ib_net_broker_interest_ytd": interest_ytd,
        "ib_profit_as_of_date": as_of,
    }


def _keyed(
    rows: list[dict[str, str]], field: str = "ticker"
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        ticker = str(row.get(field, "")).strip().upper()
        if not ticker or ticker in result:
            raise ValueError(
                f"Blank or duplicate {field} in enriched-book input: {ticker!r}"
            )
        result[ticker] = row
    return result


def _weights(rows: list[dict[str, str]]) -> dict[str, float]:
    keyed = _keyed(rows)
    result: dict[str, float] = {}
    for ticker, row in keyed.items():
        weight = _finite(row.get("weight"))
        if weight is None or weight < 0.0:
            raise ValueError(f"Invalid target weight for {ticker}: {row.get('weight')!r}")
        result[ticker] = weight
    if not math.isclose(sum(result.values()), 1.0, abs_tol=1e-6):
        raise ValueError(f"Target weights do not sum to one: {sum(result.values()):.10f}")
    return result


def _normalize_ib_symbol(value: object) -> str:
    """Map raw IB class-share symbols ("BRK B") onto the score/monitor form ("BRK.B")."""
    return ".".join(str(value if value is not None else "").strip().upper().split())


def _stock_holdings(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """Key IB stock positions by normalized symbol, dropping zero-share stubs."""
    holdings: dict[str, dict[str, str]] = {}
    for row in rows:
        raw_symbol = str(row.get("symbol", "")).strip()
        ticker = _normalize_ib_symbol(raw_symbol)
        if not ticker:
            raise ValueError(f"Blank IB holding symbol in ledger row: {row!r}")
        shares = _finite(row.get("net_shares"))
        if shares is None:
            raise ValueError(
                f"Non-numeric net_shares for IB holding {raw_symbol!r}: "
                f"{row.get('net_shares')!r}"
            )
        # net_shares nets out securities lending: a position fully lent under
        # IB's Stock Yield Enhancement Program reports shares_at_ib=100,
        # shares_lent=-100, net_shares=0 while the owner keeps full economic
        # exposure. Add lent shares back so a lent-out holding is never
        # mistaken for a closed one.
        lent_raw = str(row.get("shares_lent", "")).strip()
        lent = _finite(lent_raw) if lent_raw else 0.0
        if lent is None:
            raise ValueError(
                f"Non-numeric shares_lent for IB holding {raw_symbol!r}: "
                f"{row.get('shares_lent')!r}"
            )
        economic = shares - lent
        if abs(economic) <= 1e-12:
            continue
        row["net_shares"] = f"{economic:g}"
        if ticker in holdings:
            raise ValueError(
                f"Duplicate IB holding symbol after normalization: {ticker!r}"
            )
        holdings[ticker] = row
    return holdings


def _stock_holding_prices(rows: list[dict[str, str]]) -> dict[str, float]:
    """Ledger close price per normalized stock symbol; duplicates fail closed."""
    prices: dict[str, float] = {}
    for row in rows:
        if str(row.get("asset_category", "")) != "Stocks":
            continue
        ticker = _normalize_ib_symbol(row.get("symbol", ""))
        if not ticker:
            continue
        value = _finite(row.get("close_price"))
        if value is None:
            continue
        if ticker in prices:
            raise ValueError(
                f"Duplicate holding_state symbol after normalization: {ticker!r}"
            )
        prices[ticker] = value
    return prices


def _published_row_count(
    output_root: Path, run_as_of: str, *, monitor_subdir: str
) -> int:
    """Rows already published for run_as_of, regardless of the publication flag."""
    ledger_path = (
        output_root / monitor_subdir / "outcomes" / "state_publication_ledger.csv"
    )
    if not ledger_path.is_file():
        return 0
    return sum(
        str(row.get("published_as_of", "")) == run_as_of
        for row in read_csv(ledger_path)
    )


def _published_states(
    output_root: Path, run_as_of: str, *, monitor_subdir: str
) -> tuple[dict[str, dict[str, str]], list[Path]]:
    outcome_dir = output_root / monitor_subdir / "outcomes"
    ledger_path = outcome_dir / "state_publication_ledger.csv"
    manifest_path = outcome_dir / "state_outcome_ledger_manifest.json"
    if not ledger_path.is_file() or not manifest_path.is_file():
        raise ValueError(
            "state_publication_enabled but the monitor state publication ledger "
            f"is missing or partial under {outcome_dir}"
        )
    manifest = read_manifest(manifest_path)
    if manifest_acceptance_value(manifest) not in {"PASS", "PASS_WITH_DEFERRED"}:
        raise ValueError("Monitor state publication ledger did not pass")
    ledger_as_of = str(manifest.get("as_of_date", "")).strip()
    if ledger_as_of != run_as_of:
        raise ValueError(
            "Monitor state publication ledger is stale: manifest as_of_date="
            f"{ledger_as_of or 'MISSING'} expected {run_as_of}"
        )
    if manifest.get("publication_chain_errors") or manifest.get(
        "resolution_chain_errors"
    ):
        raise ValueError("Monitor state outcome ledger reports chain errors")
    expected_hash = str(
        dict(manifest.get("outputs_sha256", {})).get(ledger_path.name, "")
    )
    if not expected_hash or expected_hash != sha256_file(ledger_path):
        raise ValueError("Monitor state publication ledger hash mismatch")
    rows: dict[str, dict[str, str]] = {}
    for row in read_csv(ledger_path):
        if str(row.get("published_as_of", "")) != run_as_of:
            continue
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker or ticker in rows:
            raise ValueError(
                f"Blank or duplicate published monitor state for {ticker!r}"
            )
        rows[ticker] = row
    return rows, [ledger_path, manifest_path]


def compose_rows(
    *,
    weights: dict[str, float],
    scores: dict[str, dict[str, str]],
    states: dict[str, dict[str, str]],
    market_signals: dict[str, dict[str, str]],
    levels: dict[str, dict[str, str]],
    earnings: dict[str, dict[str, str]],
    holdings: dict[str, dict[str, str]],
    holding_prices: dict[str, float],
    holding_as_of: str,
) -> list[dict[str, Any]]:
    tickers = set(weights) | set(holdings)
    rows: list[dict[str, Any]] = []
    for ticker in sorted(tickers, key=lambda value: (-weights.get(value, 0.0), value)):
        score = scores.get(ticker)
        state = states.get(ticker)
        market = _market_context(market_signals.get(ticker))
        level = levels.get(ticker)
        earnings_row = earnings.get(ticker)
        holding = holdings.get(ticker)
        price = _current_price(level)
        price_source = "levels_market_structure" if price is not None else ""
        if price is None:
            fallback = holding_prices.get(ticker)
            if fallback is not None:
                price = fallback
                price_source = "ledger_close"
        row = {
            "ticker": ticker,
            "ib_symbol": str(holding.get("symbol", "")).strip() if holding else "",
            "weight": round(weights.get(ticker, 0.0), 10),
            "IB_Holding": int(holding is not None),
            "IB_quantity": holding.get("net_shares", "") if holding else "",
            "ib_holding_as_of": _display_date(holding_as_of),
            "is_scored": int(score is not None),
            "is_monitored": int(state is not None),
            "next_earnings_date": (
                _display_date(earnings_row.get("next_earnings_date", ""))
                if earnings_row
                else ""
            ),
            "sector": (
                score.get("sector", "") if score else state.get("sector", "") if state else ""
            ),
            "rating": score.get("rating", "") if score else "",
            "final_score": score.get("final_score", "") if score else "",
            "score_confidence": score.get("score_confidence", "") if score else "",
            "internal_state": state.get("internal_state", "") if state else "",
            "action_state": state.get("action_state", "") if state else "",
            "benchmark_ticker": market["benchmark_ticker"],
            "rel_ret_5d": market["rel_ret_5d"],
            "rel_ret_20d": market["rel_ret_20d"],
            "current_price": price,
            "price_source": price_source,
            "ma50": market["ma50"],
            "ma200": market["ma200"],
            "below_ma50": market["below_ma50"],
            "below_ma200": market["below_ma200"],
            "_market_latest_price": market["market_latest_price"],
            "price_band_status": (
                level.get("band_reference_status", "") if level else ""
            ),
            "price_band_basis": level.get("band_basis", "") if level else "",
            "starter_band_low": level.get("starter_band_low", "") if level else "",
            "starter_band_high": level.get("starter_band_high", "") if level else "",
            "add_band_low": level.get("add_band_low", "") if level else "",
            "add_band_high": level.get("add_band_high", "") if level else "",
            "trim_band_low": level.get("trim_band_low", "") if level else "",
            "trim_band_high": level.get("trim_band_high", "") if level else "",
        }
        rows.append(row)
    return rows


def _missing_market_context(row: dict[str, Any]) -> bool:
    """True when a scored row lacks a benchmark or a real (typed) current price."""
    if not int(row["is_scored"]):
        return False
    price = row["current_price"]
    price_missing = price is None or str(price).strip() in {"", "None"}
    return price_missing or not str(row["benchmark_ticker"]).strip()


def run_selftest() -> None:
    # IB class-share symbol normalization (M7): "BRK B" joins as "BRK.B".
    assert _normalize_ib_symbol(" brk b ") == "BRK.B"
    assert _normalize_ib_symbol("AAPL") == "AAPL"
    holdings = _stock_holdings(
        [
            {"symbol": "BRK B", "net_shares": "25"},
            {"symbol": "ZERO", "net_shares": "0"},
            {"symbol": "SMR", "net_shares": "0", "shares_lent": "-100"},
        ]
    )
    # Zero-share stubs drop; a fully-lent SYEP holding (net 0, lent -100) is
    # still economically held and must survive with the lent shares added back.
    assert set(holdings) == {"BRK.B", "SMR"}
    assert holdings["SMR"]["net_shares"] == "100"
    try:
        _stock_holdings(
            [
                {"symbol": "BRK B", "net_shares": "1"},
                {"symbol": "BRK.B", "net_shares": "2"},
            ]
        )
        raise AssertionError("duplicate normalized holding symbol must raise")
    except ValueError:
        pass
    try:
        _stock_holding_prices(
            [
                {"asset_category": "Stocks", "symbol": "BRK B", "close_price": "7.5"},
                {"asset_category": "Stocks", "symbol": "BRK.B", "close_price": "7.6"},
            ]
        )
        raise AssertionError("duplicate holding_state symbol must raise")
    except ValueError:
        pass
    assert _stock_holding_prices(
        [{"asset_category": "Stocks", "symbol": "BRK B", "close_price": "7.5"}]
    ) == {"BRK.B": 7.5}
    # Corrupt levels JSON fails closed (M6) instead of degrading to the ledger close.
    try:
        _current_price({"ticker": "AAA", "market_structure_json": "{corrupt"})
        raise AssertionError("corrupt market_structure_json must raise")
    except ValueError:
        pass

    rows = compose_rows(
        weights={"AAA": 0.8, "CASH": 0.2},
        scores={"AAA": {"sector": "Tech", "rating": "buy", "final_score": "0.1", "score_confidence": "0.8"}},
        states={"AAA": {"internal_state": "green", "action_state": "hold"}},
        market_signals={
            "AAA": {
                "benchmark_ticker": "XLK",
                "rel_ret_5d": "0.02",
                "rel_ret_20d": "0.03",
                "below_ma50": "0",
                "below_ma200": "0",
                "inputs_json": (
                    '{"latest_adj_close":12.5,"ma50":11.5,"ma200":10.5}'
                ),
            }
        },
        levels={
            "AAA": {
                "market_structure_json": '{"latest_price":12.5}',
                "starter_band_low": "10",
                "starter_band_high": "11",
                "add_band_low": "8",
                "add_band_high": "9",
                "trim_band_low": "14",
                "trim_band_high": "15",
            }
        },
        earnings={"AAA": {"next_earnings_date": "2026-08-05"}},
        holdings={"BRK.B": {"net_shares": "25", "symbol": "BRK B"}},
        holding_prices={"BRK.B": 7.5},
        holding_as_of="2026-07-31",
    )
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["AAA"]["current_price"] == 12.5
    assert by_ticker["AAA"]["price_source"] == "levels_market_structure"
    assert by_ticker["AAA"]["benchmark_ticker"] == "XLK"
    assert by_ticker["AAA"]["ma50"] == 11.5
    assert by_ticker["BRK.B"]["weight"] == 0.0
    assert by_ticker["BRK.B"]["IB_Holding"] == 1
    assert by_ticker["BRK.B"]["is_scored"] == 0
    assert by_ticker["BRK.B"]["ib_symbol"] == "BRK B"  # raw IB symbol preserved
    assert by_ticker["BRK.B"]["current_price"] == 7.5
    assert by_ticker["BRK.B"]["price_source"] == "ledger_close"
    assert by_ticker["CASH"]["price_source"] == ""
    # H3: a scored row with a typed-None price is missing market context; the old
    # str() form turned None into the truthy "None" and could never fail.
    assert not _missing_market_context(by_ticker["AAA"])
    assert not _missing_market_context(by_ticker["BRK.B"])  # unscored rows exempt
    assert _missing_market_context(
        {"is_scored": 1, "current_price": None, "benchmark_ticker": "XLK"}
    )
    assert _missing_market_context(
        {"is_scored": 1, "current_price": "None", "benchmark_ticker": "XLK"}
    )
    assert _missing_market_context(
        {"is_scored": 1, "current_price": 12.5, "benchmark_ticker": " "}
    )
    assert "layer_source" not in BOOK_FIELDS
    assert not set(MACRO_FIELDS) & set(BOOK_FIELDS)
    # IB statement cells reuse ledger parse_number semantics (sentinel/parens/commas).
    assert parse_number("--") is None
    assert parse_number("(1,234.50)") == -1234.5
    print("final target book enrichment selftest: PASS")


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_subdir = monitor_output_subdir(config)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "final/final_target_weights.csv")
    if not run_as_of:
        raise ValueError("No target-weight run exists")
    try:
        date.fromisoformat(run_as_of)
    except ValueError as exc:
        raise ValueError(
            f"Invalid --as-of value {run_as_of!r}: expected an ISO date (YYYY-MM-DD)"
        ) from exc
    run_dir = runs_root / run_as_of
    out_dir = run_dir / "final"
    output_path = out_dir / "final_target_book.csv"
    manifest_path = out_dir / "final_manifest.json"
    fail_if_exists([output_path, manifest_path], force=args.force)

    input_paths: list[Path] = [config_path, Path(__file__).resolve()]
    weight_rows, weights_manifest = _sealed_csv(
        out_dir / "final_target_weights.csv",
        out_dir / "final_weights_manifest.json",
        keys=("final_target_weights.csv",),
        run_as_of=run_as_of,
        accepted={"PASS"},
    )
    input_paths.extend(
        [out_dir / "final_target_weights.csv", out_dir / "final_weights_manifest.json"]
    )
    weights = _weights(weight_rows)

    score_rows, _ = _sealed_csv(
        run_dir / "stocks_scores.csv",
        run_dir / "manifest.json",
        keys=("stocks_scores.csv",),
        run_as_of=run_as_of,
        accepted={"PASS", "PASS_WITH_DEFERRED"},
    )
    input_paths.extend([run_dir / "stocks_scores.csv", run_dir / "manifest.json"])

    macro_rows, _ = _sealed_csv(
        run_dir / "macro" / "macro_regime.csv",
        run_dir / "macro" / "macro_manifest.json",
        keys=("macro_regime.csv",),
        run_as_of=run_as_of,
        accepted={"PASS"},
    )
    if len(macro_rows) != 1:
        raise ValueError(f"Expected one macro regime row, got {len(macro_rows)}")
    macro = macro_rows[0]
    if str(macro.get("coverage_flag", "")) not in {"1", "1.0"}:
        raise ValueError("Current macro regime is not covered")
    input_paths.extend(
        [run_dir / "macro" / "macro_regime.csv", run_dir / "macro" / "macro_manifest.json"]
    )

    earnings_rows, _ = _sealed_csv(
        run_dir / "earnings_dates" / "earnings_calendar.csv",
        run_dir / "earnings_dates" / "earnings_manifest.json",
        keys=("earnings_calendar.csv",),
        run_as_of=run_as_of,
        accepted={"PASS", "PASS_WITH_WARNINGS"},
    )
    input_paths.extend(
        [
            run_dir / "earnings_dates" / "earnings_calendar.csv",
            run_dir / "earnings_dates" / "earnings_manifest.json",
        ]
    )

    state_rows, state_manifest = _sealed_csv(
        run_dir / monitor_subdir / "expectations_state.csv",
        run_dir / monitor_subdir / "expectations_state_manifest.json",
        keys=("expectations_state.csv",),
        run_as_of=run_as_of,
        accepted={"PASS"},
    )
    state_validation = read_manifest(
        run_dir
        / monitor_subdir
        / "validation"
        / "expectations_state_validation_manifest.json"
    )
    if (
        manifest_acceptance_value(state_validation) != "PASS"
        or str(state_validation.get("as_of_date", "")) != run_as_of
    ):
        raise ValueError("Same-date validated expectations state is required")
    input_paths.extend(
        [
            run_dir / monitor_subdir / "expectations_state.csv",
            run_dir / monitor_subdir / "expectations_state_manifest.json",
            run_dir
            / monitor_subdir
            / "validation"
            / "expectations_state_validation_manifest.json",
        ]
    )
    states = _keyed(state_rows)
    # Pre-overlay snapshot: the recomputed monitor-state source of record, used to
    # derive (not assert) the state-source policy check after rows are composed.
    recomputed_states = {
        ticker: {
            field: str(row.get(field, ""))
            for field in ("internal_state", "action_state")
        }
        for ticker, row in states.items()
    }
    market_signal_rows, _ = _sealed_csv(
        run_dir / monitor_subdir / "signals" / "market_signals.csv",
        run_dir / monitor_subdir / "signals" / "market_signals_manifest.json",
        keys=("market_signals.csv",),
        run_as_of=run_as_of,
        accepted={"PASS"},
    )
    input_paths.extend(
        [
            run_dir / monitor_subdir / "signals" / "market_signals.csv",
            run_dir / monitor_subdir / "signals" / "market_signals_manifest.json",
        ]
    )
    market_signals = _keyed(market_signal_rows)
    publication_enabled = bool(
        dict(config.get("expectations_monitor", {})).get(
            "state_publication_enabled", False
        )
    )
    published_states, published_state_paths = (
        _published_states(
            paths.output_dir, run_as_of, monitor_subdir=monitor_subdir
        )
        if publication_enabled
        else ({}, [])
    )
    input_paths.extend(published_state_paths)
    # The ledger row count for run_as_of is read regardless of the flag so that
    # flipping state_publication_enabled after publication cannot silently revert
    # the book to recomputed states without a manifest FAIL.
    published_rows_for_run = (
        len(published_states)
        if publication_enabled
        else _published_row_count(
            paths.output_dir, run_as_of, monitor_subdir=monitor_subdir
        )
    )
    first_write_overlays = 0
    first_write_differences = 0
    for ticker, published in published_states.items():
        state = states.get(ticker)
        if state is None:
            continue
        replacement = dict(state)
        for field in ("internal_state", "action_state"):
            if replacement.get(field, "") != published.get(field, ""):
                first_write_differences += 1
            replacement[field] = published.get(field, "")
        states[ticker] = replacement
        first_write_overlays += 1

    level_rows, levels_manifest = _sealed_csv(
        run_dir / "levels" / "levels.csv",
        run_dir / "levels" / "levels_manifest.json",
        keys=("levels.csv",),
        run_as_of=run_as_of,
        accepted={"PASS", "PASS_WITH_DEFERRED"},
    )
    input_paths.extend(
        [run_dir / "levels" / "levels.csv", run_dir / "levels" / "levels_manifest.json"]
    )

    raw_staleness = cfg_get(config, "holdings_ledger.max_staleness_days", 7)
    try:
        max_staleness_days = int(str(raw_staleness))
    except ValueError as exc:
        raise ValueError(
            f"holdings_ledger.max_staleness_days must be an integer, got {raw_staleness!r}"
        ) from exc
    if max_staleness_days < 0:
        raise ValueError(
            f"holdings_ledger.max_staleness_days must be >= 0, got {max_staleness_days}"
        )
    ledger_run, ledger_age_days, ledger_runs_skipped = _latest_ledger_run(
        runs_root, run_as_of, max_staleness_days=max_staleness_days
    )
    ledger_as_of = ledger_run.name
    ledger_dir = ledger_run / "ledger"
    statement_source_rows, _ = _sealed_csv(
        ledger_dir / "broker_statement_sources.csv",
        ledger_dir / "ledger_manifest.json",
        keys=("broker_statement_sources", "broker_statement_sources.csv"),
        run_as_of=ledger_as_of,
        accepted={"PASS"},
    )
    if len(statement_source_rows) != 1:
        raise ValueError(
            f"Expected one sealed IB statement source, got {len(statement_source_rows)}"
        )
    statement_source = statement_source_rows[0]
    if str(statement_source.get("period_end", "")).strip() != ledger_as_of:
        raise ValueError("IB statement source period does not match ledger as-of")
    raw_statement_path = Path(str(statement_source.get("source_file", ""))).resolve()
    expected_statement_hash = str(
        statement_source.get("source_sha256", "")
    ).strip()
    if (
        not raw_statement_path.is_file()
        or not expected_statement_hash
        or sha256_file(raw_statement_path) != expected_statement_hash
    ):
        raise ValueError("Raw IB statement is missing or differs from its ledger seal")
    ib_performance = _ib_performance(raw_statement_path, as_of=ledger_as_of)
    holding_rows, _ = _sealed_csv(
        ledger_dir / "broker_net_stock_positions.csv",
        ledger_dir / "ledger_manifest.json",
        keys=("broker_net_stock_positions", "broker_net_stock_positions.csv"),
        run_as_of=ledger_as_of,
        accepted={"PASS"},
    )
    holding_state_rows, _ = _sealed_csv(
        ledger_dir / "holding_state.csv",
        ledger_dir / "ledger_manifest.json",
        keys=("holding_state", "holding_state.csv"),
        run_as_of=ledger_as_of,
        accepted={"PASS"},
    )
    input_paths.extend(
        [
            ledger_dir / "broker_net_stock_positions.csv",
            ledger_dir / "holding_state.csv",
            ledger_dir / "broker_statement_sources.csv",
            ledger_dir / "ledger_manifest.json",
            raw_statement_path,
        ]
    )

    holdings = _stock_holdings(holding_rows)
    holding_prices = _stock_holding_prices(holding_state_rows)
    rows = compose_rows(
        weights=weights,
        scores=_keyed(score_rows),
        states=states,
        market_signals=market_signals,
        levels=_keyed(level_rows),
        earnings=_keyed(earnings_rows),
        holdings=holdings,
        holding_prices=holding_prices,
        holding_as_of=ledger_as_of,
    )

    by_ticker = {str(row["ticker"]): row for row in rows}
    expected_union = set(weights) | set(holdings)
    row_tickers = [str(row["ticker"]) for row in rows]
    union_missing = sorted(expected_union - set(row_tickers))
    union_unexpected = sorted(set(row_tickers) - expected_union)
    union_duplicates = sorted(
        {ticker for ticker in row_tickers if row_tickers.count(ticker) > 1}
    )
    holding_row_errors: list[str] = []
    for ticker, holding in holdings.items():
        row = by_ticker.get(ticker)
        if (
            row is None
            or int(row.get("IB_Holding", 0)) != 1
            or str(row.get("IB_quantity", "")) != str(holding.get("net_shares", ""))
            or str(row.get("ib_symbol", "")) != str(holding.get("symbol", "")).strip()
        ):
            holding_row_errors.append(ticker)
    policy_mismatches: list[str] = []
    for ticker, row in by_ticker.items():
        recomputed = recomputed_states.get(ticker)
        if recomputed is None:
            continue
        published = published_states.get(ticker) if publication_enabled else None
        expected_source = published if published is not None else recomputed
        if any(
            str(row.get(field, "")) != str(expected_source.get(field, ""))
            for field in ("internal_state", "action_state")
        ):
            policy_mismatches.append(ticker)
    publication_flag_mismatch = (
        publication_enabled and published_rows_for_run == 0
    ) or (not publication_enabled and published_rows_for_run > 0)
    checks = [
        {
            "check": "complete_unique_union",
            "status": (
                "PASS"
                if not union_missing and not union_unexpected and not union_duplicates
                else "FAIL"
            ),
            "detail": (
                f"rows={len(rows)} target={len(weights)} holdings={len(holdings)}; "
                f"missing={union_missing}; unexpected={union_unexpected}; "
                f"duplicates={union_duplicates}"
            ),
        },
        {
            "check": "target_weights_conserved",
            "status": "PASS" if math.isclose(sum(float(row["weight"]) for row in rows), 1.0, abs_tol=1e-6) else "FAIL",
            "detail": f"weight_sum={sum(float(row['weight']) for row in rows):.10f}",
        },
        {
            "check": "all_ib_stock_holdings_included",
            "status": "PASS" if not holding_row_errors else "FAIL",
            "detail": (
                f"ledger_as_of={ledger_as_of}; ledger_age_days={ledger_age_days}; "
                f"stock_holdings={len(holdings)}; "
                f"rows_missing_or_mismatched={holding_row_errors}"
            ),
        },
        {
            "check": "macro_context_complete",
            "status": "PASS" if all(str(macro.get(field, "")).strip() for field in ("active_current_regime", "active_next_regime", "current_confidence", "next_confidence")) else "FAIL",
            "detail": f"macro_as_of={macro.get('macro_as_of_date', '')}",
        },
        {
            "check": "ib_mark_to_market_profit_sealed",
            "status": "PASS",
            "detail": (
                f"as_of={ledger_as_of}; "
                f"mtd={ib_performance['ib_mark_to_market_mtd_profit']}; "
                f"ytd={ib_performance['ib_mark_to_market_ytd_profit']}"
            ),
        },
        {
            "check": "ib_realized_profit_loss_reconciled",
            "status": (
                "PASS"
                if all(
                    math.isclose(
                        float(ib_performance[f"ib_realized_profit_loss_{period}"]),
                        sum(
                            float(ib_performance[f"ib_{component}_{period}"])
                            for component in (
                                "realized_short_term",
                                "realized_long_term",
                                "dividends",
                                "net_broker_interest",
                            )
                        ),
                        abs_tol=1e-8,
                    )
                    for period in ("mtd", "ytd")
                )
                else "FAIL"
            ),
            "detail": (
                "realized short-term + realized long-term + dividends + "
                "net broker interest"
            ),
        },
        {
            "check": "scored_names_have_market_context",
            "status": (
                "PASS"
                if not any(_missing_market_context(row) for row in rows)
                else "FAIL"
            ),
            "detail": (
                f"scored={sum(int(row['is_scored']) for row in rows)}; "
                f"missing={sum(_missing_market_context(row) for row in rows)}"
            ),
        },
        {
            "check": "market_and_level_prices_consistent",
            "status": (
                "PASS"
                if all(
                    row["_market_latest_price"] is None
                    or row["current_price"] is None
                    or math.isclose(
                        float(row["_market_latest_price"]),
                        float(row["current_price"]),
                        rel_tol=1e-10,
                        abs_tol=1e-8,
                    )
                    for row in rows
                )
                else "FAIL"
            ),
            "detail": "same-date market-signal and levels prices agree when both exist",
        },
        {
            "check": "unscored_unmonitored_visible",
            "status": "PASS",
            "detail": (
                f"unscored={sum(int(row['is_scored']) == 0 for row in rows)}; "
                f"unmonitored={sum(int(row['is_monitored']) == 0 for row in rows)}"
            ),
        },
        {
            "check": "monitor_state_source_policy_honored",
            "status": "PASS" if not policy_mismatches else "FAIL",
            "detail": (
                f"publication_enabled={publication_enabled}; "
                f"published_as_of={run_as_of}; overlays={first_write_overlays}; "
                f"field_differences_restored={first_write_differences}; "
                f"recomputed_vs_published_mismatches={policy_mismatches[:10]}"
                f" (count={len(policy_mismatches)})"
            ),
        },
        {
            "check": "state_publication_flag_consistent",
            "status": "FAIL" if publication_flag_mismatch else "PASS",
            "detail": (
                f"state_publication_enabled={publication_enabled}; "
                f"ledger_rows_for_{run_as_of}={published_rows_for_run}; "
                "flag and publication ledger must agree at book-build time"
            ),
        },
        {
            "check": "earnings_coverage_diagnostic",
            "status": "PASS" if all(
                ticker in NON_SECURITY_TICKERS or str(by_ticker[ticker]["next_earnings_date"]).strip()
                for ticker in by_ticker
            ) else "WARN",
            "detail": "missing dates remain blank and visible; they are never fabricated",
        },
    ]
    failures = [check for check in checks if check["status"] == "FAIL"]
    acceptance = "FAIL" if failures else "PASS"
    if manifest_path.exists():
        # --force republish: retract the previous seal BEFORE publishing the new CSV
        # so a crash between the two writes leaves a missing manifest (fail closed
        # downstream) instead of a stale PASS manifest fronting a different book.
        manifest_path.unlink()
    write_final_report(
        output_path,
        ib_performance=ib_performance,
        macro=macro,
        rows=rows,
    )
    manifest = {
        "stage": "stage12b_enriched_final_target_book",
        "schema_version": "final_target_book_report_v6_price_source_ib_symbol",
        "generated_at": utc_now(),
        "run_as_of": run_as_of,
        "acceptance": acceptance,
        "base_layer": weights_manifest.get("base_layer", ""),
        "gross_exposure": round(sum(float(row["weight"]) for row in rows), 10),
        "target_row_count": len(weights),
        "ib_stock_holding_count": len(holdings),
        "holding_only_count": len(set(holdings) - set(weights)),
        "ledger_as_of": ledger_as_of,
        "ledger_age_days": ledger_age_days,
        "ledger_max_staleness_days": max_staleness_days,
        "ledger_runs_skipped": ledger_runs_skipped,
        "first_write_monitor_state_count": first_write_overlays,
        "first_write_monitor_state_differences_restored": first_write_differences,
        "state_publication_enabled": publication_enabled,
        "published_state_rows_for_run": published_rows_for_run,
        "advisory_column_groups": {
            "price_bands": {
                "columns": [
                    "price_band_status",
                    "price_band_basis",
                    "starter_band_low",
                    "starter_band_high",
                    "add_band_low",
                    "add_band_high",
                    "trim_band_low",
                    "trim_band_high",
                ],
                "advisory": True,
                "shadow_only": bool(levels_manifest.get("shadow_only", False)),
                "source_manifest": "levels/levels_manifest.json",
            },
            "monitor_states": {
                "columns": ["internal_state", "action_state", "is_monitored"],
                "advisory": True,
                "shadow_only": bool(state_manifest.get("shadow_only", False)),
                "source_manifest": f"{monitor_subdir}/expectations_state_manifest.json",
            },
        },
        "report_layout": {
            "ib_performance_first_row": IB_PERFORMANCE_FIELDS,
            "ib_realized_performance_rows": IB_REALIZED_ROWS,
            "macro_preamble_rows": MACRO_FIELDS,
            "blank_separator_rows": 1,
            "book_fields": BOOK_FIELDS,
        },
        "ib_performance": {
            **ib_performance,
            "basis": "IB Month & Year to Date Performance Summary / Total (All Assets) / Mark-to-Market",
            "realized_profit_loss_formula": (
                "realized_short_term + realized_long_term + dividends + "
                "net_broker_interest"
            ),
            "cash_component_basis": (
                "IB Cash Report / Base Currency Summary / Month to Date and "
                "Year to Date"
            ),
            "currency": str(statement_source.get("base_currency", "")),
            "source_sha256": expected_statement_hash,
        },
        "macro_context": {
            field: macro.get(field, "")
            for field in (
                "macro_as_of_date",
                "active_current_regime",
                "active_next_regime",
                "current_confidence",
                "next_confidence",
            )
        },
        "checks": checks,
        "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
        "source_sha256": {
            "21_enrich_final_target_book.py": sha256_file(Path(__file__).resolve())
        },
        "files": {
            "final_target_book.csv": {
                "sha256": sha256_file(output_path),
                "rows": len(rows),
            }
        },
    }
    write_manifest(manifest_path, manifest)
    for check in checks:
        LOGGER.info("[%s] %s -- %s", check["status"], check["check"], check["detail"])
    LOGGER.info(
        "ENRICHED FINAL TARGET BOOK (%s): rows=%d target=%d ib_holdings=%d holding_only=%d -> %s",
        acceptance,
        len(rows),
        len(weights),
        len(holdings),
        len(set(holdings) - set(weights)),
        output_path,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
