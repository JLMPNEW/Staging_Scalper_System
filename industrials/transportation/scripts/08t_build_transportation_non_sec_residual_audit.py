#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    metric_search_aliases,
)
from industrials.transportation.non_sec_residual import (  # noqa: E402
    NON_SEC_RESIDUAL_FIELDS,
    NON_SEC_POST_REPAIR_VERSION,
    NON_SEC_RESIDUAL_VERSION,
    build_non_sec_residual_rows,
    summarize_residual_rows,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory every unresolved transportation ticker/metric pair "
            "against non-SEC primary-source lanes. This command performs no "
            "network retrieval, parsing, feature build, or calibration."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--coverage-prefix",
        choices=(
            "transportation_sec_union",
            "transportation_repaired_sec_union",
        ),
        default="transportation_sec_union",
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Non-SEC residual audit requires parser execution disabled"
        )
    base_dir = config_path.parent
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / str(parser_cfg["source_census_asof_date"])
    )
    coverage_path = output_dir / (
        f"{args.coverage_prefix}_ticker_metric_coverage.csv"
    )
    coverage_manifest_path = output_dir / (
        f"{args.coverage_prefix}_coverage_manifest.json"
    )
    filing_inventory_path = (
        output_dir
        / "transportation_source_exhaustion_filing_inventory.csv"
    )
    coverage_manifest = _read_json(coverage_manifest_path)
    if (
        coverage_manifest.get("acceptance") != "PASS"
        or file_sha256(coverage_path)
        != str(
            (
                coverage_manifest.get("artifacts") or {}
            ).get("ticker_metric_coverage", {}).get("sha256")
            or ""
        )
    ):
        raise ValueError("SEC union coverage is not sealed and passing")
    filing_rows = read_csv(filing_inventory_path)
    foreign_tickers = {
        str(row["ticker"]).upper()
        for row in filing_rows
        if str(row.get("form_type") or "").upper()
        in {"6-K", "6-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
    }
    residual_version = (
        NON_SEC_POST_REPAIR_VERSION
        if args.coverage_prefix
        == "transportation_repaired_sec_union"
        else NON_SEC_RESIDUAL_VERSION
    )
    rows = build_non_sec_residual_rows(
        coverage_rows=read_csv(coverage_path),
        metric_aliases=metric_search_aliases(),
        foreign_tickers=foreign_tickers,
        residual_version=residual_version,
    )
    # The DP6G (pre-repair) and DP6J (post-repair) variants must never share
    # filenames: the post-repair rerun used to overwrite the sealed DP6G
    # artifacts that 08u had hashed as inputs. Post-repair keeps the legacy
    # names (08z/09b/09c read them); pre-repair now writes its own names.
    post_repair = (
        args.coverage_prefix == "transportation_repaired_sec_union"
    )
    residual_basename = (
        "transportation_non_sec_residual_source"
        if post_repair
        else "transportation_pre_repair_non_sec_residual_source"
    )
    csv_path = output_dir / f"{residual_basename}_audit.csv"
    manifest_path = output_dir / f"{residual_basename}_manifest.json"
    write_csv_atomic(csv_path, NON_SEC_RESIDUAL_FIELDS, rows)
    summary = summarize_residual_rows(rows)
    payload = {
        "acceptance": "PASS" if rows else "FAIL",
        "gate": (
            "DP6J_POST_REPAIR_NON_SEC_RESIDUAL_SOURCE_AUDIT"
            if args.coverage_prefix
            == "transportation_repaired_sec_union"
            else "DP6G_NON_SEC_RESIDUAL_SOURCE_AUDIT"
        ),
        "residual_version": residual_version,
        "model_family": MODEL_FAMILY,
        "asof_date": str(parser_cfg["source_census_asof_date"]),
        "coverage_prefix": args.coverage_prefix,
        "source_universe_scope": "NON_SEC_PRIMARY_DISCLOSURES",
        "sec_union_coverage_path": str(coverage_path.resolve()),
        "sec_union_coverage_sha256": file_sha256(coverage_path),
        "foreign_private_issuer_ticker_count": len(foreign_tickers),
        **summary,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "retrieval_authorized": False,
        "parser_execution_authorized": False,
        "production_promotion_authorized": False,
        "non_sec_primary_source_audit_complete": True,
        "global_source_exhaustion_complete": False,
        "artifact": {
            "path": str(csv_path.resolve()),
            "row_count": len(rows),
            "sha256": file_sha256(csv_path),
        },
        "next_gate": "SEAL_NON_SEC_ENDPOINT_MANIFEST_ONCE",
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
