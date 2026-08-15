#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.source_incorporation import (  # noqa: E402
    SOURCE_INCORPORATION_FIELDS,
    build_med_device_source_incorporation,
)
from orchestration_contracts.financial_lineage import (  # noqa: E402
    evaluate_financial_lineage_rows,
    evaluation_manifest,
    policy_for_model_family,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "med_devices"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and enforce Med Devices SEC/FDA source-incorporation lineage."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--score-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--policy-context", default="research")
    return parser.parse_args()


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def _fields(
    base_fields: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
) -> list[str]:
    output = list(base_fields)
    for field in SOURCE_INCORPORATION_FIELDS:
        if field not in output:
            output.append(field)
    for row in rows:
        for field in row:
            if field not in output:
                output.append(field)
    return output


def _write_csv_atomic(
    path: Path,
    fields: list[str],
    rows: Iterable[Mapping[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def main() -> int:
    args = parse_args()
    try:
        datetime.strptime(str(args.asof), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("--asof must be YYYY-MM-DD") from exc

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    review_root = resolve_path(
        cfg_get(
            config,
            "scoring.review_pack_dir",
            "../output/med_devices_reports/score_review_pack",
        ),
        base_dir=base_dir,
    )
    score_csv = (
        args.score_csv.expanduser().resolve()
        if args.score_csv
        else review_root / str(args.asof) / "med_device_daily_composite_scores.csv"
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else PROJECT_ROOT
        / "output"
        / "med_devices_reports"
        / "financial_lineage"
        / str(args.asof)
    )
    if not score_csv.is_file():
        raise FileNotFoundError(f"Published Med Devices score file missing: {score_csv}")

    base_fields, score_rows = _read_csv(score_csv)
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        gated_rows, evidence_rows = build_med_device_source_incorporation(
            conn,
            asof=str(args.asof),
            score_rows=score_rows,
            fda_source_id=str(
                cfg_get(config, "fda_core_ingestion.source_id", "openfda_device")
                or "openfda_device"
            ),
        )

    fields = _fields(base_fields, gated_rows)
    _write_csv_atomic(score_csv, fields, gated_rows)
    sidecar_path = output_dir / "med_device_financial_lineage.csv"
    _write_csv_atomic(sidecar_path, fields, evidence_rows)

    policy = policy_for_model_family(MODEL_FAMILY)
    policy_mode = policy.mode_for_asof(args.policy_context, str(args.asof))
    evaluation = evaluate_financial_lineage_rows(
        evidence_rows,
        policy_mode=policy_mode,
        expected_asof=str(args.asof),
        min_core_metric_count=policy.min_core_metric_count,
    )
    manifest = {
        "model_family": MODEL_FAMILY,
        "asof_date": str(args.asof),
        "score_csv": str(score_csv),
        "score_csv_sha256": hashlib.sha256(score_csv.read_bytes()).hexdigest(),
        "sidecar_csv": str(sidecar_path),
        "sidecar_csv_sha256": hashlib.sha256(sidecar_path.read_bytes()).hexdigest(),
        "row_count": len(evidence_rows),
        "source_incorporated_count": sum(
            row.get("financial_lineage_gate") == "1" for row in evidence_rows
        ),
        **evaluation_manifest(
            evaluation,
            policy=policy,
            context=str(args.policy_context),
        ),
    }
    manifest_path = output_dir / "med_device_financial_lineage.json"
    _write_json_atomic(manifest_path, manifest)
    print(
        f"med_devices source incorporation {manifest['acceptance']}: "
        f"{manifest['source_incorporated_count']}/{manifest['row_count']} "
        f"sidecar={sidecar_path}"
    )
    if evaluation.blocking_issues:
        for issue in evaluation.blocking_issues:
            print(f"BLOCKING: {issue.render()}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
