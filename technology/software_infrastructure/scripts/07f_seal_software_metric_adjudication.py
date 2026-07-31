#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    open_read_only_database,
)
from technology.software_infrastructure.software_metric_governance import (  # noqa: E402
    build_expansion_queue,
    build_golden_corpus,
    build_policy_payload,
    load_source_rows,
    policy_csv_rows,
    release_manifest,
    validate_release,
    adjudicate_rows,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
GOLDEN_DIR = PROJECT_ROOT / "dedicated_parser" / "golden_corpus"
SOFTWARE_ROOT = PACKAGE_ROOT / "software_infrastructure"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "dedicated_parser_governance"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Seal and validate the reviewed 77-observation software metric "
            "corpus without modifying production facts or scores."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the versioned corpus, policy, and expansion artifacts.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    corpus_path = GOLDEN_DIR / "software_metrics_v1.json"
    policy_path = GOLDEN_DIR / "software_metrics_policy_v1.json"
    policy_csv_path = (
        SOFTWARE_ROOT
        / "review_policies"
        / "software_metrics_v1_policy.csv"
    )
    expansion_queue_path = (
        SOFTWARE_ROOT
        / "data"
        / "software_metrics_v1_expansion_queue.csv"
    )
    expansion_summary_path = (
        SOFTWARE_ROOT
        / "data"
        / "software_metrics_v1_expansion_summary.csv"
    )
    registry_path = (
        SOFTWARE_ROOT
        / "data"
        / "software_infrastructure_specialized_metric_registry.yaml"
    )
    adapter_path = SOFTWARE_ROOT / "dedicated_parser_adapter.py"

    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        source_rows = load_source_rows(conn)
        decisions = adjudicate_rows(source_rows)
        corpus = build_golden_corpus(source_rows)
        policy = build_policy_payload(
            decisions=decisions,
            registry_path=registry_path,
            adapter_path=adapter_path,
        )
        expansion_queue, expansion_summary = build_expansion_queue(
            conn,
            source_rows=source_rows,
        )
        if args.write:
            atomic_json(corpus_path, corpus)
            atomic_json(policy_path, policy)
            atomic_csv(policy_csv_path, policy_csv_rows(decisions))
            if expansion_queue:
                atomic_csv(expansion_queue_path, expansion_queue)
            atomic_csv(expansion_summary_path, expansion_summary)
        required = (
            corpus_path,
            policy_path,
            policy_csv_path,
            expansion_queue_path,
            expansion_summary_path,
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Governance artifacts are missing; rerun with --write: "
                + ", ".join(missing)
            )
        errors = validate_release(
            conn,
            corpus_path=corpus_path,
            policy_path=policy_path,
            registry_path=registry_path,
            adapter_path=adapter_path,
        )
    manifest = release_manifest(
        corpus_path=corpus_path,
        policy_path=policy_path,
        policy_csv_path=policy_csv_path,
        expansion_queue_path=expansion_queue_path,
        expansion_summary_path=expansion_summary_path,
        policy_payload=policy,
    )
    manifest["validation_status"] = "FAIL" if errors else "PASS"
    manifest["validation_errors"] = errors
    output_path = (
        args.output_dir.expanduser().resolve()
        / "software_metrics_v1_release_manifest.json"
    )
    atomic_json(output_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
