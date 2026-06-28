#!/usr/bin/env python3
"""Stage 8.5 - import and normalize an IB activity-statement CSV.

The raw IB CSV is the sealed broker source artifact. This script does not contact IB; it parses a
one-day or date-range CSV, writes normalized run-local CSVs, and records the raw source hash.
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

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
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
    peek_statement_period_end,
)


LOGGER = logging.getLogger("import_ib_activity_statement")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

STATEMENT_GLOB_DEFAULT = "U*.csv"

SUBSTANCE_TABLES = [
    ("broker_statement_sources.csv", "meta", STATEMENT_META_FIELDS),
    ("broker_open_positions.csv", "open_positions", OPEN_POSITION_FIELDS),
    ("broker_net_stock_positions.csv", "net_stock_positions", NET_STOCK_POSITION_FIELDS),
    ("broker_trades.csv", "trades", TRADE_FIELDS),
    ("broker_instruments.csv", "instruments", INSTRUMENT_FIELDS),
    ("broker_cash_report.csv", "cash_report", CASH_REPORT_FIELDS),
    ("broker_dividends.csv", "dividends", DIVIDEND_FIELDS),
    ("broker_cash_transactions.csv", "cash_transactions", CASH_TRANSACTION_FIELDS),
    ("broker_fees.csv", "fees", FEE_FIELDS),
    ("broker_securities_lending.csv", "securities_lending", SECURITIES_LENDING_FIELDS),
]
SUBSTANCE_IGNORE_FIELDS = {"source_sha256", "source_file", "source_row", "trade_key", "when_generated"}


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
    p.add_argument(
        "--backfill",
        action="store_true",
        help="Import every statement in the source dir whose dated ledger run is missing (gap fill).",
    )
    p.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Override holdings_ledger.source_reports_dir for --backfill.",
    )
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _resolve_source(config: dict[str, Any], config_path: Path, raw: Path | None) -> Path:
    if raw is not None:
        return ensure_not_prod_path(raw.expanduser().resolve(), label="IB CSV")
    source_dir = resolve_path(cfg_get(config, "holdings_ledger.source_reports_dir", "../IB_reports"), base_dir=config_path.parent)
    source_dir = ensure_not_prod_path(source_dir.expanduser().resolve(), label="IB source dir")
    statement_glob = str(cfg_get(config, "holdings_ledger.statement_glob", STATEMENT_GLOB_DEFAULT) or STATEMENT_GLOB_DEFAULT)
    return ensure_not_prod_path(latest_ib_report(source_dir, statement_glob), label="IB CSV")


def _import_statement(paths: Any, ib_csv: Path, as_of: str | None, *, force: bool) -> int:
    """Parse one sealed IB statement into normalized run-local artifacts. Returns 0 on success."""
    if not ib_csv.exists():
        LOGGER.error("IB CSV not found: %s", ib_csv)
        return 1

    try:
        parsed = parse_ib_activity_statement(ib_csv)
    except Exception as exc:  # noqa: BLE001 - report parser failures as stage failures
        LOGGER.exception("Failed to parse IB CSV %s: %s", ib_csv, exc)
        return 1

    run_as_of = as_of or parsed.meta["period_end"]
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
    if force:
        for path in artifacts.values():
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(artifacts.values(), force=force)
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


def _statement_rows(parsed: Any, attr: str) -> list[dict[str, str]]:
    if attr == "meta":
        return [parsed.meta]
    return list(getattr(parsed, attr))


def _normalize_substance(rows: list[dict[str, Any]], fields: list[str]) -> list[dict[str, str]]:
    comparable_fields = [field for field in fields if field not in SUBSTANCE_IGNORE_FIELDS]
    normalized = [
        {field: str(row.get(field, "") or "").strip() for field in comparable_fields}
        for row in rows
    ]
    return sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))


def _substance_matches_sealed(parsed: Any, ledger_dir: Path) -> tuple[bool, list[str]]:
    mismatches: list[str] = []
    for filename, attr, fields in SUBSTANCE_TABLES:
        path = ledger_dir / filename
        if not path.exists():
            mismatches.append(f"{filename}:missing_sealed_artifact")
            continue
        sealed = _normalize_substance(read_csv(path), fields)
        candidate = _normalize_substance(_statement_rows(parsed, attr), fields)
        if sealed != candidate:
            mismatches.append(f"{filename}:substance_diff")
    return not mismatches, mismatches


def _statement_span_key(path: Path) -> tuple[str, int, float]:
    try:
        parsed = parse_ib_activity_statement(path)
        start = parsed.meta.get("period_start", "")
    except Exception:  # noqa: BLE001 - tie-break fallback only
        start = ""
    width_key = "9999-99-99" if not start else start
    try:
        stat = path.stat()
        size = int(stat.st_size)
        mtime = float(stat.st_mtime)
    except OSError:
        size = 0
        mtime = 0.0
    # Wider coverage has an earlier period_start. Then prefer larger files, then newest mtime.
    return (width_key, -size, -mtime)


def _backfill(paths: Any, *, source_dir: Path, statement_glob: str, force: bool) -> int:
    """Import every statement whose dated ledger run is missing, newest gaps last.

    Efficiency: each file is dated by a header-only peek (no full parse); a full parse + write happens
    only for dates that are actually missing. Dates that already have a sealed run are skipped after a
    single hash compare, which also surfaces any statement that was silently restated on disk.
    """
    source_dir = ensure_not_prod_path(source_dir.expanduser().resolve(), label="IB source dir")
    runs_root = paths.output_dir / "runs"
    files = sorted(source_dir.glob(statement_glob))
    if not files:
        LOGGER.error("No IB CSV reports found under %s with glob %r", source_dir, statement_glob)
        return 1

    by_date: dict[str, Path] = {}
    undated: list[str] = []
    for f in files:
        ib_csv = ensure_not_prod_path(f.resolve(), label="IB CSV")
        end = peek_statement_period_end(ib_csv)
        if not end:
            try:
                parsed = parse_ib_activity_statement(ib_csv)
                end = parsed.meta.get("period_end", "")
            except Exception as exc:  # noqa: BLE001 - report parse failures as backfill failures
                undated.append(f.name)
                LOGGER.error("Could not determine statement period end for %s via header or full parse: %s", f.name, exc)
                continue
            if not end:
                undated.append(f.name)
                LOGGER.error("Could not determine statement period end for %s via header or full parse", f.name)
                continue
            LOGGER.warning("Header period peek failed for %s; full parse dated statement as %s", f.name, end)
        prev = by_date.get(end)
        if prev is not None:
            keep = min([prev, ib_csv], key=_statement_span_key)
            LOGGER.warning(
                "Two statements end %s (%s, %s); using widest/largest %s",
                end, prev.name, ib_csv.name, keep.name,
            )
            by_date[end] = keep
        else:
            by_date[end] = ib_csv

    imported: list[str] = []
    current: list[str] = []
    restated: list[str] = []
    failed: list[str] = []
    for end in sorted(by_date):
        ib_csv = ensure_not_prod_path(by_date[end].resolve(), label="IB CSV")
        meta_path = runs_root / end / "ledger" / "ib_statement_meta.json"
        if meta_path.exists() and not force:
            try:
                sealed_sha = str((json.loads(meta_path.read_text(encoding="utf-8")).get("raw_source") or {}).get("sha256", ""))
            except (json.JSONDecodeError, OSError):
                sealed_sha = ""
            if sealed_sha and sealed_sha == sha256_file(ib_csv):
                current.append(end)
                continue
            try:
                parsed_candidate = parse_ib_activity_statement(ib_csv)
            except Exception as exc:  # noqa: BLE001 - parse failure means the candidate cannot prove equivalence
                restated.append(end)
                LOGGER.error(
                    "Statement for %s changed since sealed import and candidate could not be parsed (%s): %s",
                    end, ib_csv.name, exc,
                )
                continue
            same_substance, substance_mismatches = _substance_matches_sealed(parsed_candidate, meta_path.parent)
            if same_substance:
                current.append(end)
                LOGGER.info(
                    "Statement for %s has a different raw hash but normalized broker substance matches sealed import "
                    "(likely benign re-download; candidate=%s)",
                    end,
                    ib_csv.name,
                )
                continue
            restated.append(end)
            LOGGER.warning(
                "Statement for %s changed since sealed import (%s); substance_diff=%s; re-run with --force to overwrite",
                end, ib_csv.name, substance_mismatches[:8],
            )
            continue
        rc = _import_statement(paths, ib_csv, end, force=force)
        (imported if rc == 0 else failed).append(end)

    LOGGER.info(
        "Backfill complete: imported=%s already_current=%s restated_skipped=%s failed=%s undated=%s",
        imported or "none", current or "none", restated or "none", failed or "none", undated or "none",
    )
    return 1 if failed or restated or undated else 0


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    if args.backfill:
        if args.source_dir is not None:
            source_dir = ensure_not_prod_path(args.source_dir.expanduser().resolve(), label="IB source dir")
        else:
            source_dir = resolve_path(
                cfg_get(config, "holdings_ledger.source_reports_dir", "../IB_reports"), base_dir=config_path.parent
            )
            source_dir = ensure_not_prod_path(source_dir.expanduser().resolve(), label="IB source dir")
        statement_glob = str(cfg_get(config, "holdings_ledger.statement_glob", STATEMENT_GLOB_DEFAULT) or STATEMENT_GLOB_DEFAULT)
        return _backfill(paths, source_dir=source_dir, statement_glob=statement_glob, force=args.force)
    ib_csv = _resolve_source(config, config_path, args.ib_csv)
    return _import_statement(paths, ib_csv, args.as_of, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
