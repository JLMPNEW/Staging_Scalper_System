#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.promotion import promote_run  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT.parent / "config.yaml"
ADAPTER = (
    "industrials.defense.dedicated_parser_adapter:"
    "extract_metric_evidence"
)
DEFENSE_GOLDEN_CORPUS = (
    PROJECT_ROOT / "dedicated_parser" / "golden_corpus" / "defense_v1.json"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Promote reviewed defense parser evidence into its isolated "
            "supplemental SEC source."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args(argv)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _latest_run_id(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
    adapter_version: str,
) -> int:
    row = conn.execute(
        """
        SELECT run_id
        FROM sec_parser_run
        WHERE model_family = 'defense'
          AND asof_date = ?
          AND adapter_version = ?
          AND status = 'COMPLETED'
          AND failed_work_count = 0
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (asof_date, adapter_version),
    ).fetchone()
    if row is None:
        raise ValueError(
            "No fully completed defense parser run matches "
            f"asof={asof_date} adapter={adapter_version}"
        )
    return int(row["run_id"])


def _validate_production_readiness(registry: object) -> None:
    payload = json.loads(DEFENSE_GOLDEN_CORPUS.read_text(encoding="utf-8"))
    expectations = payload.get("expectations")
    if not isinstance(expectations, list) or not expectations:
        raise ValueError(
            "Defense production promotion requires a nonempty adjudicated "
            f"golden corpus: {DEFENSE_GOLDEN_CORPUS}"
        )

    review_policy_path = Path(
        str(getattr(registry, "review_policy_path", ""))
    ).expanduser()
    if not review_policy_path.is_file():
        raise ValueError(
            "Defense production promotion requires a review policy: "
            f"{review_policy_path}"
        )
    with review_policy_path.open(encoding="utf-8-sig", newline="") as handle:
        reviewed_rows = [
            row
            for row in csv.DictReader(handle)
            if str(row.get("enabled", "")).strip().lower()
            in {"1", "true", "yes", "y"}
            and str(row.get("model_family", "")).strip().lower()
            == "defense"
            and str(row.get("reviewed_by", "")).strip()
            and str(row.get("reviewed_at", "")).strip()
        ]
    if not reviewed_rows:
        raise ValueError(
            "Defense production promotion requires at least one enabled, "
            "attributed, timestamped defense review-policy decision."
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    if not bool(cfg_get(config, "dedicated_parser.production_enabled", False)):
        raise ValueError(
            "Defense dedicated-parser production is disabled. Complete the "
            "full-universe adjudication and PIT/OOS recalibration before "
            "setting dedicated_parser.production_enabled=true."
        )
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    registry = load_registry(ADAPTER)
    source_id = str(
        cfg_get(
            config,
            "dedicated_parser.production_source_id",
            "dedicated_parser_defense_production",
        )
    ).strip()
    if source_id != "dedicated_parser_defense_production":
        raise ValueError(
            "Defense parser source must remain isolated as "
            "dedicated_parser_defense_production"
        )
    _validate_production_readiness(registry)
    with connect_database(db_path) as conn:
        run_id = args.run_id or _latest_run_id(
            conn,
            asof_date=args.asof,
            adapter_version=registry.adapter_version,
        )
        summary = promote_run(
            conn,
            run_id=run_id,
            registry=registry,
            source_id=source_id,
            min_confidence=float(
                cfg_get(
                    config,
                    "dedicated_parser.production_min_confidence",
                    0.90,
                )
            ),
        )
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json is not None
        else resolve_path(
            cfg_get(
                config,
                "dedicated_parser.production_manifest_json",
            ),
            base_dir=config_path.parent,
        )
    )
    _write_json(output_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
