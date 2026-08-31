"""Restartable PIT score-sidecar and operational-history backfill.

The sidecar is calibration-only.  It asserts survivorship correction only when
the Stage 7 ticker census exactly matches the database's point-in-time eligible
membership census for that date.  It never asserts OOS or portfolio gates.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.atomic_io import atomic_write_text  # noqa: E402
from consumer_defensive.core.config import (  # noqa: E402
    cfg_get,
    load_config,
    resolve_path,
)
from consumer_defensive.core.stage3_runtime import database_path  # noqa: E402

DEFAULT_CONFIG = ROOT / "consumer_defensive" / "config.yaml"
SIDECAR_NAME = "consumer_defensive_stage11_survivorship_calibration_panel.csv"
CHECKPOINT_NAME = "consumer_defensive_stage11_backfill_checkpoint.json"


def iso_date(raw: str) -> str:
    date.fromisoformat(raw)
    return raw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--from", dest="date_from", type=iso_date, required=True)
    parser.add_argument("--to", dest="date_to", type=iso_date, required=True)
    parser.add_argument("--dashboard-root", type=Path)
    parser.add_argument("--chunk-size", type=int, default=20)
    parser.add_argument("--no-operational-publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    encoded = quote(resolved.as_posix(), safe="/:")
    conn = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_text(path, handle.getvalue())


def _available_dates(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    source_id: str,
) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT asof_date
               FROM feature_scoring_model_output
               WHERE model_family='consumer_defensive'
                 AND source_id=?
                 AND asof_date BETWEEN ? AND ?
               ORDER BY asof_date""",
            (source_id, start, end),
        )
    ]


def _expected_tickers(conn: sqlite3.Connection, asof: str) -> set[str]:
    return {
        str(row[0]).strip().upper()
        for row in conn.execute(
            """SELECT DISTINCT ticker
               FROM dim_universe_membership
               WHERE model_family='consumer_defensive'
                 AND point_in_time_flag=1
                 AND historical_calibration_eligible_flag=1
                 AND start_date<=?
                 AND (end_date IS NULL OR end_date>=?)""",
            (asof, asof),
        )
    }


def _score_rows(
    conn: sqlite3.Connection, asof: str, *, source_id: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT o.*,t.portfolio_sector,t.calibration_cohort_id,
                      t.calibration_cohort,c.company_name
               FROM feature_scoring_model_output o
               JOIN dim_consumer_defensive_taxonomy t
                 ON t.ticker=o.ticker AND t.model_family=o.model_family
               LEFT JOIN dim_company c ON c.company_id=t.company_id
               WHERE o.model_family='consumer_defensive'
                 AND o.source_id=? AND o.asof_date=?
               ORDER BY o.ticker""",
            (source_id, asof),
        )
    )


def _sidecar_rows(
    conn: sqlite3.Connection, asof: str, *, source_id: str
) -> list[dict[str, object]]:
    scores = _score_rows(conn, asof, source_id=source_id)
    expected = _expected_tickers(conn, asof)
    actual = {str(row["ticker"]).strip().upper() for row in scores}
    if not expected:
        raise ValueError(f"{asof}: PIT membership census is empty")
    if actual != expected:
        raise ValueError(
            f"{asof}: Stage 7/PIT census mismatch; "
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )
    rows: list[dict[str, object]] = []
    for source in scores:
        row = dict(source)
        ticker = str(row["ticker"]).strip().upper()
        cohort = str(row.get("calibration_cohort_id") or "").strip()
        model_version = str(row.get("model_version") or "").strip()
        contract_sha = str(row.get("model_contract_sha256") or "").strip()
        eligible = int(row.get("calibration_eligible_flag") or 0)
        model_complete = str(row.get("model_status") or "").strip().lower() == "complete"
        research_ok = bool(eligible and model_complete)
        result: dict[str, object] = {
            "asof_date": asof,
            "ticker": ticker,
            "company_name": str(row.get("company_name") or ticker),
            "sector": str(row.get("portfolio_sector") or "Consumer Staples"),
            "industry": str(row.get("calibration_cohort") or cohort),
            "industry_aggregate": "Consumer Staples",
            "calibration_cohort": cohort,
            "final_score": row.get("final_score"),
            "final_rank": row.get("final_rank"),
            "rank_ready_flag": int(row.get("rank_ready_flag") or 0),
            "model_status": str(row.get("model_status") or ""),
            "promotion_state": "shadow_monitor",
            "score_confidence": row.get("data_quality_confidence") or 0,
            "score_model_version": model_version,
            "model_version": model_version,
            "scoring_contract_version": contract_sha,
            "portfolio_candidate_gate": 0,
            "portfolio_candidate_score": row.get("final_score"),
            "portfolio_candidate_status": "calibration_only",
            "portfolio_candidate_reason": "historical_survivorship_panel_only",
            "calibration_eligible_flag": eligible,
            "research_calibration_input_eligible_flag": int(research_ok),
            "research_calibration_reason": "ok" if research_ok else "model_or_calibration_ineligible",
            "calibration_sample_role": "pre_lock_research" if research_ok else "excluded",
            "stage11_calibration_panel_source": "consumer_defensive_pit_census_v1",
            "stage11_calibration_input_eligible_flag": int(research_ok),
            "stage11_calibration_input_reason": "ok" if research_ok else "model_or_calibration_ineligible",
            "survivorship_corrected_panel_flag": 1,
            "oos_score_valid_flag": 0,
            "oos_score_asof_date": "",
            "oos_invalid_reason": "historical_reconstruction_not_strict_oos",
            "calibration_lock_date": "",
            "score_observation_id": str(row.get("score_observation_id") or ""),
            "model_contract_sha256": contract_sha,
        }
        result["row_sha256"] = _canonical_hash(result)
        rows.append(result)
    return rows


def _merge_sidecar(path: Path, additions: list[dict[str, object]]) -> None:
    _fields, existing = _read_csv(path)
    by_key: dict[tuple[str, str], dict[str, object]] = {
        (str(row["asof_date"]), str(row["ticker"])): dict(row) for row in existing
    }
    for row in additions:
        key = (str(row["asof_date"]), str(row["ticker"]))
        previous = by_key.get(key)
        if previous is not None and str(previous.get("row_sha256")) != str(row["row_sha256"]):
            raise FileExistsError(f"immutable Stage 11 sidecar row changed: {key}")
        by_key[key] = row
    ordered = [by_key[key] for key in sorted(by_key)]
    _write_csv(path, ordered)


def main() -> int:
    args = parse_args()
    if args.date_from > args.date_to:
        raise ValueError("--from must not follow --to")
    if args.chunk_size <= 0:
        raise ValueError("--chunk-size must be positive")
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db).expanduser().resolve()
    base_output = resolve_path(
        cfg_get(bundle.payload, "paths.output_dir"), base_dir=bundle.base_dir
    )
    dashboard = (args.dashboard_root or base_output / "dashboard").resolve()
    checkpoint_path = dashboard / "stage11_combined" / CHECKPOINT_NAME
    checkpoint = (
        json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint_path.exists()
        else {"completed_dates": [], "failed_dates": {}}
    )
    supplied_checkpoint_hash = str(checkpoint.get("payload_sha256") or "")
    if supplied_checkpoint_hash and supplied_checkpoint_hash != _canonical_hash(
        {key: value for key, value in checkpoint.items() if key != "payload_sha256"}
    ):
        raise ValueError("Stage 11 backfill checkpoint self-hash mismatch")
    completed = set(checkpoint.get("completed_dates") or [])
    source_id = str(cfg_get(bundle.payload, "stage7_scoring.source_id"))
    with _connect_read_only(db_path) as conn:
        available = _available_dates(
            conn, args.date_from, args.date_to, source_id=source_id
        )
        pending = [
            value
            for value in available
            if value not in completed
        ][: args.chunk_size]
        if args.dry_run:
            print(json.dumps({"status": "DRY_RUN", "pending_dates": pending}, indent=2))
            return 0
        for asof in pending:
            try:
                additions = _sidecar_rows(conn, asof, source_id=source_id)
                _merge_sidecar(dashboard / SIDECAR_NAME, additions)
                if not args.no_operational_publish:
                    command = [
                        sys.executable,
                        str(ROOT / "consumer_defensive/scripts/28_run_consumer_defensive_stage12_pipeline.py"),
                        "--config", str(args.config.resolve()),
                        "--db", str(db_path),
                        "--asof", asof,
                        "--skip-local-score-build",
                    ]
                    result = subprocess.run(command, cwd=ROOT, check=False)
                    if result.returncode:
                        raise RuntimeError(f"Stage 12 historical publish failed with {result.returncode}")
                completed.add(asof)
                checkpoint.setdefault("failed_dates", {}).pop(asof, None)
            except Exception as exc:
                checkpoint.setdefault("failed_dates", {})[asof] = f"{type(exc).__name__}: {exc}"
                checkpoint["completed_dates"] = sorted(completed)
                checkpoint["payload_sha256"] = _canonical_hash(
                    {key: value for key, value in checkpoint.items() if key != "payload_sha256"}
                )
                atomic_write_text(checkpoint_path, json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
                raise
            checkpoint["completed_dates"] = sorted(completed)
            checkpoint["payload_sha256"] = _canonical_hash(
                {key: value for key, value in checkpoint.items() if key != "payload_sha256"}
            )
            atomic_write_text(checkpoint_path, json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "status": "PASS",
                "processed_dates": pending,
                "completed_date_count": len(completed),
                "remaining_available_date_count": sum(
                    value not in completed for value in available
                ),
                "sidecar": str(dashboard / SIDECAR_NAME),
                "checkpoint": str(checkpoint_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
