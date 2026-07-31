#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.machinery.recoverable_coverage import (  # noqa: E402
    LEDGER_FIELDS,
    ISSUER_IR_REQUEST_FIELDS,
    build_issuer_ir_recovery_requests,
    build_recovery_evidence,
    recovery_summary,
    replace_recovery_evidence,
)
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
QUEUE_FIELDS = ["priority_rank", *LEDGER_FIELDS]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify every missing machinery metric cell by evidence and recovery lane."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"),
        base_dir=config_path.parent,
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        "../../output/industrials/machinery/stage4",
        base_dir=config_path.parent,
    )
    with connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)),
    ) as conn:
        rows = build_recovery_evidence(conn, asof=asof)
        replace_recovery_evidence(conn, asof=asof, rows=rows)
    summary = recovery_summary(rows, asof=asof)
    issuer_ir_requests = build_issuer_ir_recovery_requests(rows)
    summary["issuer_ir_request_count"] = len(issuer_ir_requests)
    queue: list[dict[str, Any]] = []
    for rank, row in enumerate(
        (item for item in rows if item["recoverability"] in {"HIGH", "MEDIUM"}),
        start=1,
    ):
        queue.append({"priority_rank": rank, **row})
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(output_dir / "machinery_metric_recovery_evidence.csv", LEDGER_FIELDS, rows)
    write_csv_atomic(output_dir / "machinery_metric_recovery_queue.csv", QUEUE_FIELDS, queue)
    write_csv_atomic(
        output_dir / "machinery_issuer_ir_recovery_requests.csv",
        ISSUER_IR_REQUEST_FIELDS,
        issuer_ir_requests,
    )
    write_text_atomic(
        output_dir / "machinery_metric_recovery_summary.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(
        "PASS: classified "
        f"{len(rows)} missing machinery metric cells; "
        f"high={summary['recoverability_counts'].get('HIGH', 0)} "
        f"medium={summary['recoverability_counts'].get('MEDIUM', 0)} "
        f"low={summary['recoverability_counts'].get('LOW', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
