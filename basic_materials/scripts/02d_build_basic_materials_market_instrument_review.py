"""Build the reviewed Stage 3 Norgate instrument-role contract.

This is a deliberate contract-build utility, not part of the daily refresh.  It
resolves provider symbols to immutable Norgate asset IDs, records the provider
snapshot, and writes only the package-owned review CSV.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
import json
from pathlib import Path
import sys
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.atomic_io import atomic_write_csv  # noqa: E402
from basic_materials.core.config import load_config, resolve_cli_path  # noqa: E402
from basic_materials.core.market_data_contract import MARKET_INSTRUMENT_COLUMNS  # noqa: E402
from basic_materials.core.norgate_runtime import (  # noqa: E402
    NORGATE_EQUITY_DATABASES,
    norgate_database_fingerprint,
    require_norgate_snapshot,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Basic Materials config path")
    parser.add_argument("--output", type=Path, help="Explicit package-owned review CSV path")
    parser.add_argument(
        "--replace-reviewed-contract",
        action="store_true",
        help="Permit replacement of an existing governed review CSV",
    )
    return parser.parse_args()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _iso_provider_date(value: Any, context: str, *, required: bool) -> str:
    if value is None or str(value).strip() in {"", "None"}:
        if required:
            raise RuntimeError(f"Provider did not return {context}")
        return ""
    if isinstance(value, datetime):
        result = value.date().isoformat()
    elif isinstance(value, date):
        result = value.isoformat()
    else:
        result = str(value).strip()[:10]
    date.fromisoformat(result)
    return result


def _maximum_date(left: str, right: str) -> str:
    date.fromisoformat(left)
    date.fromisoformat(right)
    return max(left, right)


def _provider_catalog(provider: Any) -> dict[tuple[str, str], str]:
    catalog: dict[tuple[str, str], str] = {}
    for database_name in NORGATE_EQUITY_DATABASES:
        for item in provider.database(database_name):
            symbol = str(item["symbol"]).strip().upper()
            asset_id = str(item["assetid"]).strip()
            key = (symbol, asset_id)
            if key in catalog and catalog[key] != database_name:
                raise RuntimeError(f"Provider identity {key} appears in multiple databases")
            catalog[key] = database_name
    return catalog


def _identity(
    provider: Any,
    catalog: Mapping[tuple[str, str], str],
    symbol: str,
    *,
    expected_asset_id: str = "",
) -> dict[str, str]:
    normalized_symbol = symbol.strip().upper()
    asset_id = str(provider.assetid(normalized_symbol) or "").strip()
    if not asset_id.isdigit():
        raise RuntimeError(f"Provider asset ID is missing for {normalized_symbol}")
    if expected_asset_id and asset_id != expected_asset_id:
        raise RuntimeError(
            f"Provider asset changed for {normalized_symbol}: expected {expected_asset_id}, found {asset_id}"
        )
    database_name = catalog.get((normalized_symbol, asset_id))
    if database_name is None:
        raise RuntimeError(
            f"Provider identity {(normalized_symbol, asset_id)} is absent from governed equity databases"
        )
    first = _iso_provider_date(
        provider.first_quoted_date(normalized_symbol),
        f"first quoted date for {normalized_symbol}",
        required=True,
    )
    last = _iso_provider_date(
        provider.last_quoted_date(normalized_symbol),
        f"last quoted date for {normalized_symbol}",
        required=False,
    )
    return {
        "instrument_key": f"norgate_us_equities_total_return:{asset_id}",
        "provider_source_id": "norgate_us_equities_total_return",
        "provider_database": database_name,
        "provider_symbol": normalized_symbol,
        "provider_asset_id": asset_id,
        "provider_first_quoted_date": first,
        "provider_last_quoted_date": last,
    }


def _role(
    identity: Mapping[str, str],
    *,
    role_key: str,
    role_type: str,
    model_ticker: str,
    security_scope: str,
    event_key: str,
    expected_start_date: str,
    expected_end_date: str,
    trading_currency: str,
    current_gate: bool,
    reviewed_on: str,
    notes: str,
) -> dict[str, str]:
    return {
        "role_key": role_key,
        **identity,
        "instrument_role": role_type,
        "model_ticker": model_ticker,
        "security_scope": security_scope,
        "event_key": event_key,
        "expected_start_date": expected_start_date,
        "expected_end_date": expected_end_date,
        "trading_currency": trading_currency,
        "required_for_stage3": "1",
        "required_for_current_gate": "1" if current_gate else "0",
        "evidence_label": "provider_snapshot_reviewed",
        "review_status": "approved_stage3_market_instrument",
        "reviewed_on": reviewed_on,
        "notes": notes,
    }


def build_rows(config: Any, provider: Any) -> tuple[list[dict[str, str]], dict[str, str]]:
    policy = __import__("yaml").safe_load(
        config.paths.market_data_policy.read_text(encoding="utf-8")
    )
    history_start = str(policy["history"]["history_start"])
    reviewed_on = str(policy["contract_as_of_date"])
    overrides = policy["provider_symbol_overrides"]
    current = _read_csv(config.paths.universe_csv)
    historical = _read_csv(config.paths.historical_membership_csv)
    terminal_events = _read_csv(config.paths.terminal_events_csv)
    terminal_rules = {
        row["event_key"]: row for row in _read_csv(config.paths.terminal_return_rules_csv)
    }

    fingerprint = norgate_database_fingerprint(provider, NORGATE_EQUITY_DATABASES)
    catalog = _provider_catalog(provider)
    rows: list[dict[str, str]] = []

    for item in current:
        ticker = item["ticker"].upper()
        identity = _identity(provider, catalog, ticker)
        rows.append(
            _role(
                identity,
                role_key=f"current:{ticker}",
                role_type="current_universe",
                model_ticker=ticker,
                security_scope="current_primary_listing",
                event_key="",
                expected_start_date=_maximum_date(history_start, identity["provider_first_quoted_date"]),
                expected_end_date="",
                trading_currency=item["currency"],
                current_gate=True,
                reviewed_on=reviewed_on,
                notes="Current reviewed Basic Materials security",
            )
        )

    for item in historical:
        ticker = item["historical_ticker"].upper()
        identity = _identity(
            provider,
            catalog,
            item["provider_symbol"],
            expected_asset_id=item["provider_asset_id"],
        )
        if identity["provider_first_quoted_date"] != item["membership_start_date"]:
            raise RuntimeError(f"Historical first quote changed for {ticker}")
        if identity["provider_last_quoted_date"] != item["membership_end_date"]:
            raise RuntimeError(f"Historical last quote changed for {ticker}")
        rows.append(
            _role(
                identity,
                role_key=f"historical:{ticker}",
                role_type="historical_pilot",
                model_ticker=ticker,
                security_scope="historical_primary_listing",
                event_key="",
                expected_start_date=_maximum_date(history_start, item["membership_start_date"]),
                expected_end_date=item["membership_end_date"],
                trading_currency=item["trading_currency"],
                current_gate=False,
                reviewed_on=reviewed_on,
                notes="Stage 2B historical calibration-pilot security",
            )
        )

    for label, role_type in (("sector", "sector_benchmark"), ("broad", "broad_benchmark")):
        ticker = str(policy["benchmarks"][label]["ticker"]).upper()
        identity = _identity(provider, catalog, ticker)
        rows.append(
            _role(
                identity,
                role_key=f"benchmark:{label}:{ticker}",
                role_type=role_type,
                model_ticker=ticker,
                security_scope=f"{label}_benchmark",
                event_key="",
                expected_start_date=_maximum_date(history_start, identity["provider_first_quoted_date"]),
                expected_end_date="",
                trading_currency="USD",
                current_gate=True,
                reviewed_on=reviewed_on,
                notes=f"Stage 3 {label} benchmark and calendar input",
            )
        )

    for event in terminal_events:
        event_key = event["event_key"]
        rule = terminal_rules[event_key]
        if rule["outcome_class"] != "stock_conversion":
            continue
        model_ticker = event["successor_ticker"].upper()
        override = overrides.get(event_key, {})
        provider_symbol = str(override.get("provider_symbol") or event["successor_provider_symbol"] or model_ticker)
        expected_asset_id = str(override.get("provider_asset_id") or "")
        identity = _identity(provider, catalog, provider_symbol, expected_asset_id=expected_asset_id)
        rows.append(
            _role(
                identity,
                role_key=f"terminal_successor:{event_key}",
                role_type="terminal_successor",
                model_ticker=model_ticker,
                security_scope="terminal_successor_security",
                event_key=event_key,
                expected_start_date=event["successor_reference_date"],
                expected_end_date="",
                trading_currency=event["cash_currency"] or "USD",
                current_gate=False,
                reviewed_on=reviewed_on,
                notes=(
                    f"Economic successor {model_ticker}; provider symbol {provider_symbol}"
                    if provider_symbol != model_ticker
                    else f"Successor security for {event_key}"
                ),
            )
        )

    require_norgate_snapshot(provider, fingerprint, context="before market-instrument contract publication")
    expected_counts = {str(key): int(value) for key, value in policy["expected_role_counts"].items()}
    actual_counts = {
        role: sum(row["instrument_role"] == role for row in rows) for role in expected_counts
    }
    if actual_counts != expected_counts:
        raise RuntimeError(f"Role counts differ from policy: expected={expected_counts}, actual={actual_counts}")
    unique_instruments = len({row["instrument_key"] for row in rows})
    if unique_instruments != int(policy["expected_unique_instruments"]):
        raise RuntimeError(
            f"Expected {policy['expected_unique_instruments']} unique instruments, found {unique_instruments}"
        )
    return rows, fingerprint


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        output = resolve_cli_path(args.output, config.paths.market_instruments_csv)
        if output != config.paths.market_instruments_csv:
            try:
                output.relative_to(config.package_root)
            except ValueError as exc:
                raise ValueError("Instrument-review output must remain inside basic_materials") from exc
        if output.exists() and not args.replace_reviewed_contract:
            raise FileExistsError(
                f"Governed contract already exists: {output}; pass --replace-reviewed-contract intentionally"
            )
        import norgatedata as provider

        if provider.status() is not True:
            raise RuntimeError("Local Norgate Data Updater is unavailable")
        rows, fingerprint = build_rows(config, provider)
        atomic_write_csv(output, rows, MARKET_INSTRUMENT_COLUMNS)
        print(
            json.dumps(
                {
                    "succeeded": True,
                    "output": str(output),
                    "role_rows": len(rows),
                    "unique_instruments": len({row["instrument_key"] for row in rows}),
                    "provider_database_fingerprint": fingerprint,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps({"succeeded": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
