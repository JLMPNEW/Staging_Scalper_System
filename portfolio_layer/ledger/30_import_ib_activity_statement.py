#!/usr/bin/env python3
"""Stage 8.5 - import and normalize an IB activity-statement CSV.

The raw IB CSV is the sealed broker source artifact. This script does not contact IB; it parses a
one-day or date-range CSV, writes normalized run-local CSVs, and records the raw source hash.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.ledger.ledger_common import (  # noqa: E402
    CASH_REPORT_FIELDS,
    CASH_TRANSACTION_FIELDS,
    DIVIDEND_FIELDS,
    FEE_FIELDS,
    INSTRUMENT_FIELDS,
    NET_STOCK_POSITION_FIELDS,
    OPEN_POSITION_FIELDS,
    SECURITIES_LENDING_FIELDS,
    STATEMENT_META_FIELDS,
    TRADE_FIELDS,
    latest_ib_report,
    parse_ib_activity_statement,
)


LOGGER = logging.getLogger("import_ib_activity_statement")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import a sealed IB activity-statement CSV into normalized artifacts.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--ib-csv", type=Path, default=None, help="IB activity statement CSV. Defaults to newest configured report.")
    p.add_argument("--as-of", type=iso_date_arg, default=None, help="Run as-of date. Defaults to the statement period end.")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _resolve_source(config: dict[str, Any], config_path: Path, raw: Path | None) -> Path:
    if raw is not None:
        return ensure_not_prod_path(raw.expanduser().resolve(), label="IB CSV")
    source_dir = resolve_path(cfg_get(config, "holdings_ledger.source_reports_dir", "../IB_reports"), base_dir=config_path.parent)
    return ensure_not_prod_path(latest_ib_report(source_dir), label="IB CSV")


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    ib_csv = _resolve_source(config, config_path, args.ib_csv)
    if not ib_csv.exists():
        LOGGER.error("IB CSV not found: %s", ib_csv)
        return 1

    try:
        parsed = parse_ib_activity_statement(ib_csv)
    except Exception as exc:  # noqa: BLE001 - report parser failures as stage failures
        LOGGER.exception("Failed to parse IB CSV %s: %s", ib_csv, exc)
        return 1

    run_as_of = args.as_of or parsed.meta["period_end"]
    if run_as_of != parsed.meta["period_end"]:
        LOGGER.warning("Run as-of %s differs from IB statement end %s", run_as_of, parsed.meta["period_end"])

    ledger_dir = paths.output_dir / "runs" / run_as_of / "ledger"
    artifacts = {
        "ib_statement_meta.json": ledger_dir / "ib_statement_meta.json",
        "broker_statement_sources.csv": ledger_dir / "broker_statement_sources.csv",
        "broker_open_positions.csv": ledger_dir / "broker_open_positions.csv",
        "broker_net_stock_positions.csv": ledger_dir / "broker_net_stock_positions.csv",
        "broker_trades.csv": ledger_dir / "broker_trades.csv",
        "broker_instruments.csv": ledger_dir / "broker_instruments.csv",
        "broker_cash_report.csv": ledger_dir / "broker_cash_report.csv",
        "broker_dividends.csv": ledger_dir / "broker_dividends.csv",
        "broker_cash_transactions.csv": ledger_dir / "broker_cash_transactions.csv",
        "broker_fees.csv": ledger_dir / "broker_fees.csv",
        "broker_securities_lending.csv": ledger_dir / "broker_securities_lending.csv",
    }
    if args.force:
        for path in artifacts.values():
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(artifacts.values(), force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    write_csv(artifacts["broker_statement_sources.csv"], STATEMENT_META_FIELDS, [parsed.meta])
    write_csv(artifacts["broker_open_positions.csv"], OPEN_POSITION_FIELDS, parsed.open_positions)
    write_csv(artifacts["broker_net_stock_positions.csv"], NET_STOCK_POSITION_FIELDS, parsed.net_stock_positions)
    write_csv(artifacts["broker_trades.csv"], TRADE_FIELDS, parsed.trades)
    write_csv(artifacts["broker_instruments.csv"], INSTRUMENT_FIELDS, parsed.instruments)
    write_csv(artifacts["broker_cash_report.csv"], CASH_REPORT_FIELDS, parsed.cash_report)
    write_csv(artifacts["broker_dividends.csv"], DIVIDEND_FIELDS, parsed.dividends)
    write_csv(artifacts["broker_cash_transactions.csv"], CASH_TRANSACTION_FIELDS, parsed.cash_transactions)
    write_csv(artifacts["broker_fees.csv"], FEE_FIELDS, parsed.fees)
    write_csv(artifacts["broker_securities_lending.csv"], SECURITIES_LENDING_FIELDS, parsed.securities_lending)

    meta = {
        "stage": "stage8_5_import_ib_activity_statement",
        "run_as_of": run_as_of,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS",
        "raw_source": {
            "path": str(ib_csv),
            "sha256": parsed.meta["source_sha256"],
            "period": parsed.meta["period"],
            "period_start": parsed.meta["period_start"],
            "period_end": parsed.meta["period_end"],
            "when_generated": parsed.meta["when_generated"],
            "account_id": parsed.meta["account_id"],
        },
        "row_counts": {
            "open_positions": len(parsed.open_positions),
            "net_stock_positions": len(parsed.net_stock_positions),
            "trades": len(parsed.trades),
            "instruments": len(parsed.instruments),
            "cash_report": len(parsed.cash_report),
            "dividends": len(parsed.dividends),
            "cash_transactions": len(parsed.cash_transactions),
            "fees": len(parsed.fees),
            "securities_lending": len(parsed.securities_lending),
        },
        "outputs_sha256": {name: sha256_file(path) for name, path in artifacts.items() if name != "ib_statement_meta.json"},
        "source_sha256": {"30_import_ib_activity_statement.py": sha256_file(Path(__file__).resolve())},
    }
    write_manifest(artifacts["ib_statement_meta.json"], meta)
    LOGGER.info(
        "Imported IB CSV %s -> %s (as_of=%s, trades=%d, positions=%d, instruments=%d)",
        ib_csv,
        ledger_dir,
        run_as_of,
        len(parsed.trades),
        len(parsed.open_positions),
        len(parsed.instruments),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
