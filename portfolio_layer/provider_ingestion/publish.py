#!/usr/bin/env python3
"""Publish a sealed point-in-time provider estimate snapshot for one portfolio date."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.provider_ingestion.store import (  # noqa: E402
    artifact_dependency_errors,
    connect_store,
    digest,
    record_artifact_dependencies,
    verify_store,
    writer_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDS = (
    "snapshot_id",
    "snapshot_run_id",
    "provider",
    "endpoint_id",
    "ticker",
    "instrument_id",
    "provider_symbol",
    "fiscal_period_end",
    "fiscal_period",
    "estimate_type",
    "estimate_average",
    "estimate_high",
    "estimate_low",
    "analyst_count",
    "estimate_average_7_days_ago",
    "estimate_average_30_days_ago",
    "estimate_average_60_days_ago",
    "estimate_average_90_days_ago",
    "revision_up_7_days",
    "revision_down_7_days",
    "revision_up_30_days",
    "revision_down_30_days",
    "currency",
    "available_at_utc",
    "effective_trading_date",
    "effective_from_utc",
    "same_session_eligible",
    "normalized_sha256",
    "semantic_basis_hash",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--cutoff-utc", type=datetime.fromisoformat)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _default_cutoff(as_of: date, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    return datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=zone).astimezone(
        timezone.utc
    )


def _as_of_rows(conn: Any, *, as_of: date, cutoff_utc: datetime) -> list[dict[str, Any]]:
    cutoff = cutoff_utc.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    rows = conn.execute(
        "WITH ranked AS ("
        " SELECT s.*,ROW_NUMBER() OVER("
        "  PARTITION BY s.provider,s.instrument_id,s.estimate_type,s.fiscal_period_end "
        "  ORDER BY s.available_at_utc DESC,s.snapshot_id DESC"
        " ) AS row_rank FROM provider_estimate_snapshots s"
        " WHERE s.available_at_utc<? AND s.effective_trading_date<=?"
        ") SELECT * FROM ranked WHERE row_rank=1 "
        "ORDER BY provider,ticker,estimate_type,fiscal_period_end",
        (cutoff, as_of.isoformat()),
    ).fetchall()
    return [{field: row[field] for field in FIELDS} for row in rows]


def run_selftest() -> None:
    cutoff = _default_cutoff(date(2026, 8, 3), "America/New_York")
    assert cutoff.isoformat() == "2026-08-04T04:00:00+00:00"
    print("provider as-of publication selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    ingestion = cfg_get(config, "provider_ingestion", {})
    if not isinstance(ingestion, dict):
        raise ValueError("provider_ingestion config must be a mapping")
    store_path = ensure_not_prod_path(
        resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    timeout = float(ingestion.get("writer_lock_timeout_sec", 30.0))
    cutoff = args.cutoff_utc or _default_cutoff(
        args.as_of, str(ingestion.get("timezone", "America/New_York"))
    )
    if cutoff.tzinfo is None:
        raise ValueError("--cutoff-utc must include a timezone")
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir
        / str(ingestion.get("output_subdir", "provider_ingestion"))
        / "asof"
        / args.as_of.isoformat()
    )
    output_path = output_dir / "provider_estimates_asof.csv"
    manifest_path = output_dir / "provider_estimates_asof_manifest.json"
    if manifest_path.exists() and not args.force:
        raise FileExistsError(f"As-of provider publication already exists: {manifest_path}")
    lock_path = store_path.with_suffix(store_path.suffix + ".writer.lock")
    with writer_lock(lock_path, timeout_sec=timeout):
        conn = connect_store(store_path, timeout_sec=timeout)
        try:
            errors = verify_store(conn)
            if errors:
                raise RuntimeError(f"Provider observation store verification failed: {errors}")
            rows = _as_of_rows(conn, as_of=args.as_of, cutoff_utc=cutoff)
            write_csv(output_path, FIELDS, rows)
            artifact_hash = sha256_file(output_path)
            observation_ids = [str(row["snapshot_id"]) for row in rows]
            if observation_ids:
                record_artifact_dependencies(
                    conn,
                    artifact_path=str(output_path.resolve()),
                    artifact_sha256=artifact_hash,
                    observation_ids=observation_ids,
                )
                dependency_errors = artifact_dependency_errors(
                    conn,
                    artifact_path=str(output_path.resolve()),
                    artifact_sha256=artifact_hash,
                )
                if dependency_errors:
                    raise RuntimeError(f"Provider publication lineage failed: {dependency_errors}")
            run_digests = [
                str(row[0])
                for row in conn.execute(
                    "SELECT run_digest FROM capture_runs WHERE completed_at_utc<? ORDER BY completed_at_utc",
                    (cutoff.astimezone(timezone.utc).replace(microsecond=0).isoformat(),),
                ).fetchall()
            ]
        finally:
            conn.close()
    write_manifest(
        manifest_path,
        {
            "schema_version": "provider_asof_manifest_v1",
            "acceptance": "PASS" if rows else "PASS_NO_COVERAGE",
            "as_of_date": args.as_of.isoformat(),
            "cutoff_utc": cutoff.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "row_count": len(rows),
            "observation_digest": digest(observation_ids),
            "source_run_digest": digest(run_digests),
            "store_path": str(store_path),
            "inputs_sha256": {
                str(config_path): sha256_file(config_path),
                str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
                str(Path(__file__).with_name("store.py")): sha256_file(Path(__file__).with_name("store.py")),
            },
            "outputs_sha256": {output_path.name: sha256_file(output_path)},
        },
    )
    print(f"PROVIDER AS-OF PUBLICATION: {'PASS' if rows else 'PASS_NO_COVERAGE'}; rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

