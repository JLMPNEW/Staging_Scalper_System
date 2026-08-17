#!/usr/bin/env python3
"""Materialize reviewed ticker-scoped XBRL mappings without fetching filings."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402
from industrials.transportation.ticker_scoped_xbrl_backfill import (  # noqa: E402
    load_ticker_scoped_concept_rules,
    materialize_ticker_scoped_xbrl_facts,
)


DEFAULT_RULES = PROJECT_ROOT / "industrials" / "transportation" / "system_csvs" / "transportation_ticker_xbrl_concept_aliases.csv"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "ticker_scoped_xbrl"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = date.fromisoformat(str(args.asof)[:10])
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    rules_path = args.rules.expanduser().resolve()
    rules = load_ticker_scoped_concept_rules(rules_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        counts = materialize_ticker_scoped_xbrl_facts(
            connection,
            rules=rules,
            asof=asof,
            execute=bool(args.execute),
        )
    finally:
        connection.close()
    payload = {
        "acceptance": "PASS" if counts["eligible_raw_fact_count"] > 0 else "FAIL",
        "contract_version": "transportation_v5_ticker_scoped_xbrl_materialization_v1",
        "asof_date": asof.isoformat(),
        "mode": "execute" if args.execute else "plan_only",
        **counts,
        "rules_path": str(rules_path),
        "rules_sha256": file_sha256(rules_path),
        "network_requests": 0,
        "parser_invocations": 0,
    }
    output_dir = args.output_root.expanduser().resolve() / asof.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "transportation_v5_ticker_scoped_xbrl_materialization.json"
    write_text_atomic(output_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({**counts, "acceptance": payload["acceptance"], "mode": payload["mode"], "output": str(output_path)}, indent=2))
    return 0 if payload["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
