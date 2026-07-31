#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_adapter import (  # noqa: E402
    SUPPORTED_FORMS,
)
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    open_read_only_database,
    parse_iso_date,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    hydrate_filings,
    select_filings,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CACHE = PROJECT_ROOT / "output" / "technology_cache" / "dedicated_parser"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "dedicated_parser"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resume-safe technology-owned hydration of complete software SEC accessions."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--start-date", default="2010-01-01")
    parser.add_argument("--tickers", default="")
    parser.add_argument(
        "--accession-file",
        type=Path,
        default=None,
        help=(
            "Optional CSV containing an accession_number column. When set, "
            "only those exact accessions are selected."
        ),
    )
    parser.add_argument("--forms", default=",".join(SUPPORTED_FORMS))
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--max-filings-per-ticker", type=int, default=0)
    parser.add_argument("--max-documents-per-filing", type=int, default=0)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--request-spacing-sec", type=float, default=0.2)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.execute and args.max_documents_per_filing != 0:
        raise ValueError(
            "Executed hydration must use --max-documents-per-filing 0 so an "
            "incomplete accession cannot be certified as parser-ready"
        )
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    asof_date = parse_iso_date(args.asof, field_name="asof")
    start_date = parse_iso_date(args.start_date, field_name="start_date")
    tickers = tuple(
        sorted({value.strip().upper() for value in args.tickers.split(",") if value.strip()})
    )
    forms = tuple(
        dict.fromkeys(value.strip().upper() for value in args.forms.split(",") if value.strip())
    )
    unsupported = sorted(set(forms) - set(SUPPORTED_FORMS))
    if unsupported:
        raise ValueError(f"Unsupported software parser forms: {unsupported}")
    accessions: tuple[str, ...] = ()
    if args.accession_file is not None:
        accession_path = args.accession_file.expanduser().resolve()
        if not accession_path.is_file():
            raise FileNotFoundError(accession_path)
        with accession_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle)
            if "accession_number" not in set(reader.fieldnames or ()):
                raise ValueError(
                    f"{accession_path}: missing accession_number column"
                )
            accessions = tuple(
                dict.fromkeys(
                    str(row.get("accession_number") or "").strip()
                    for row in reader
                    if str(row.get("accession_number") or "").strip()
                )
            )
        if not accessions:
            raise ValueError(
                f"{accession_path}: no accession numbers were supplied"
            )
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with open_read_only_database(db_path, timeout_sec=timeout) as conn:
        filings = select_filings(
            conn,
            forms=forms,
            asof_date=asof_date,
            start_date=start_date,
            tickers=tickers,
            accessions=accessions,
            max_filings_per_ticker=args.max_filings_per_ticker,
            max_tickers=args.max_tickers,
        )
    user_agent = expand_env_vars(
        cfg_get(
            config,
            "sec_fundamentals.user_agent",
            "Independent technology research contact@example.com",
        )
    )
    if "@" not in user_agent:
        raise ValueError("SEC User-Agent must contain a contact email address")
    manifest = hydrate_filings(
        filings,
        cache_dir=args.cache_dir.expanduser().resolve(),
        output_dir=args.output_dir.expanduser().resolve() / asof_date / "hydration",
        user_agent=user_agent,
        timeout_sec=args.timeout_sec,
        max_retries=args.max_retries,
        request_spacing_sec=max(0.1, args.request_spacing_sec),
        execute=args.execute,
        max_documents_per_filing=args.max_documents_per_filing,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
