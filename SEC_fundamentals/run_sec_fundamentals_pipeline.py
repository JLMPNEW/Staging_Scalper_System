#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sec_fundamentals_config import (
    cfg_get,
    configure_pipeline_logging,
    load_sec_fundamentals_config,
    previous_or_same_business_day,
    validate_sql_identifier,
)

SQLITE_BUSY_TIMEOUT_MS = 30000
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run SEC fundamentals pipeline: "
            "init DB -> ingest -> build period features -> build enhanced snapshot -> export compatibility snapshot."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().with_name("config_sec_fundamentals.yaml"),
        help="Path to fundamentals YAML config.",
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "quarterly", "backfill"],
        default=None,
        help="Optional ingest mode override.",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Optional ingest start_date YYYY-MM-DD (passed to ingest step).",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Optional ingest end_date YYYY-MM-DD (passed to ingest step).",
    )
    parser.add_argument("--as-of-date", type=str, default=None, help="Optional feature as_of_date YYYY-MM-DD.")
    parser.add_argument(
        "--quality-gate-override",
        action="store_true",
        help="Pass --quality-gate-override to enhanced snapshot build.",
    )
    return parser.parse_args()


def run_step(cmd: list[str]) -> None:
    if len(cmd) >= 2 and cmd[0] == sys.executable:
        step_name = Path(cmd[1]).name
    else:
        step_name = Path(cmd[0]).name
    logger.info("Running [%s]: %s", step_name, " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            detail = f"exit code {exc.returncode}"
        elif isinstance(exc, subprocess.TimeoutExpired):
            detail = f"timeout after {exc.timeout}"
        else:
            detail = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(
            f"{step_name} failed ({detail}): {' '.join(cmd)}"
        ) from exc


def _resolve_path(config_path: Path, raw_value: str | None) -> Path | None:
    if not raw_value:
        return None
    path = Path(str(raw_value))
    if path.is_absolute():
        return path
    return (config_path.parent.parent / path).resolve()


def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def parse_iso_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _resolve_db_path(cfg: dict[str, Any], config_path: Path) -> Path:
    db_path_raw = cfg_get(cfg, "db_path", default=None)
    if not db_path_raw:
        raise ValueError("sec_fundamentals.db_path is required in config.")
    db_path = Path(str(db_path_raw)).expanduser()
    if not db_path.is_absolute():
        db_path = (config_path.parent / db_path).resolve()
    return db_path


def _infer_latest_as_of_date(db_path: Path, snapshot_table: str) -> str | None:
    table = validate_sql_identifier(snapshot_table, "resolver snapshot_table")
    if not db_path.exists():
        return None
    conn = _connect_sqlite(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        if not row:
            return None
        max_row = conn.execute(f"SELECT MAX(as_of_date) FROM {table}").fetchone()
        if max_row and max_row[0]:
            return str(max_row[0])
        return None
    finally:
        conn.close()


def _resolve_effective_snapshot_as_of_date(
    cfg: dict[str, Any],
    db_path: Path,
    requested_as_of_date: str | None,
) -> str:
    features_cfg = cfg_get(cfg, "features", default={})
    as_of_date = parse_iso_date(requested_as_of_date) or parse_iso_date(
        cfg_get(features_cfg, "as_of_date", default=None)
    )
    if as_of_date is None:
        inferred = _infer_latest_as_of_date(db_path=db_path, snapshot_table="sec_fundamental_period_t1")
        as_of_date = parse_iso_date(inferred) if inferred else None
    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).date()
    return previous_or_same_business_day(as_of_date).isoformat()


def require_enhanced_snapshot_rows(
    config_path: Path,
    cfg: dict[str, Any],
    requested_as_of_date: str | None,
) -> str:
    snap_cfg = cfg_get(cfg, "snapshot_enhanced", default={})
    if not isinstance(snap_cfg, dict):
        snap_cfg = {}
    snapshot_table = validate_sql_identifier(
        str(cfg_get(snap_cfg, "security_filled_table", default="sec_fundamental_snapshot_filled_security_t1")),
        "snapshot_enhanced.security_filled_table",
    )
    db_path = _resolve_db_path(cfg, config_path)
    effective_as_of_date = _resolve_effective_snapshot_as_of_date(
        cfg=cfg,
        db_path=db_path,
        requested_as_of_date=requested_as_of_date,
    )
    conn = _connect_sqlite(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (snapshot_table,),
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"Enhanced snapshot output table {snapshot_table} does not exist after build step."
            )
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM {snapshot_table} WHERE as_of_date = ?",
            (effective_as_of_date,),
        ).fetchone()
    finally:
        conn.close()
    row_count = int(count_row[0]) if count_row and count_row[0] is not None else 0
    if row_count <= 0:
        raise RuntimeError(
            f"Enhanced snapshot build produced no rows in {snapshot_table} for as_of_date={effective_as_of_date}. "
            "Aborting before resolver/export."
        )
    logger.info(
        "Verified enhanced snapshot rows: table=%s as_of_date=%s rows=%d",
        snapshot_table,
        effective_as_of_date,
        row_count,
    )
    return effective_as_of_date


def maybe_compile_metric_mapping(py: str, config_path: Path) -> None:
    cfg_path, cfg = load_sec_fundamentals_config(config_path)
    snap_cfg = cfg_get(cfg, "snapshot_enhanced", default={})
    if not isinstance(snap_cfg, dict):
        snap_cfg = {}

    required = bool(cfg_get(snap_cfg, "metric_mapping_required", default=True))
    mapping_csv = _resolve_path(cfg_path, cfg_get(snap_cfg, "metric_mapping_csv", default=None))
    overlay_yaml = _resolve_path(
        cfg_path,
        cfg_get(
            snap_cfg,
            "mapping_overlay_yaml",
            default=str(Path("fundamental_data") / "sec_metric_mapping_overlay.yaml"),
        ),
    )
    unmatched_csv = _resolve_path(
        cfg_path,
        cfg_get(
            snap_cfg,
            "mapping_unmatched_csv",
            default=str(Path("fundamental_data") / "sec_metric_mapping_overlay_unmatched.csv"),
        ),
    )
    rule_diag_csv = _resolve_path(
        cfg_path,
        cfg_get(
            snap_cfg,
            "mapping_rule_diagnostics_csv",
            default=str(Path("fundamental_data") / "sec_metric_mapping_rule_diagnostics.csv"),
        ),
    )

    if mapping_csv is None:
        if required:
            raise ValueError(
                "snapshot_enhanced.metric_mapping_csv is required but missing in config."
            )
        return
    mapping_csv.parent.mkdir(parents=True, exist_ok=True)

    if overlay_yaml is None or not overlay_yaml.exists():
        if required:
            raise FileNotFoundError(
                f"Mapping overlay YAML not found: {overlay_yaml}. "
                "Set snapshot_enhanced.mapping_overlay_yaml or create the default file."
            )
        return

    db_path_raw = cfg_get(cfg, "db_path", default=None)
    if not db_path_raw:
        raise ValueError("sec_fundamentals.db_path is required in config.")
    db_path = Path(str(db_path_raw)).expanduser()
    db_url = f"sqlite:///{db_path.as_posix()}"

    builder = Path(__file__).resolve().with_name("sec_metric_mapping_overlay_builder.py")
    cmd = [
        py,
        str(builder),
        "--overlay-yaml",
        str(overlay_yaml),
        "--db-url",
        db_url,
        "--metric-source-table",
        "sec_xbrl_facts_raw",
        "--output-csv",
        str(mapping_csv),
    ]
    if unmatched_csv is not None:
        unmatched_csv.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--unmatched-rules-csv", str(unmatched_csv)])
    if rule_diag_csv is not None:
        rule_diag_csv.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--rule-diagnostics-csv", str(rule_diag_csv)])
    run_step(cmd)


def maybe_run_snapshot_resolver(py: str, config_path: Path, as_of_date: str | None = None) -> None:
    cfg_path, cfg = load_sec_fundamentals_config(config_path)
    resolver_cfg = cfg_get(cfg, "snapshot_resolver", default={})
    if not isinstance(resolver_cfg, dict):
        resolver_cfg = {}
    if not bool(cfg_get(resolver_cfg, "enabled", default=True)):
        return

    snap_cfg = cfg_get(cfg, "snapshot_enhanced", default={})
    if not isinstance(snap_cfg, dict):
        snap_cfg = {}

    db_path_raw = cfg_get(cfg, "db_path", default=None)
    if not db_path_raw:
        raise ValueError("sec_fundamentals.db_path is required in config.")
    db_path = Path(str(db_path_raw)).expanduser()
    db_url = f"sqlite:///{db_path.as_posix()}"

    resolver_script = Path(__file__).resolve().with_name("sec_snapshot_gap_resolver.py")
    snapshot_table = str(
        cfg_get(
            resolver_cfg,
            "snapshot_table",
            default=str(cfg_get(snap_cfg, "security_filled_table", default="sec_fundamental_snapshot_filled_security_t1")),
        )
    )
    snapshot_table = validate_sql_identifier(snapshot_table, "resolver snapshot_table")
    facts_table = validate_sql_identifier(
        str(cfg_get(resolver_cfg, "facts_table", default="sec_xbrl_facts_raw")),
        "resolver facts_table",
    )
    output_table = validate_sql_identifier(
        cfg_get(
            resolver_cfg,
            "output_table",
            default=f"{snapshot_table}_resolved",
        ),
        "resolver output_table",
    )
    candidate_table = cfg_get(resolver_cfg, "candidate_table", default=None)
    if candidate_table:
        candidate_table = validate_sql_identifier(str(candidate_table), "resolver candidate_table")
    extension_yaml = _resolve_path(
        cfg_path,
        cfg_get(
            resolver_cfg,
            "extension_rule_yaml",
            default=str(Path("fundamental_data") / "sec_extension_pattern_library.yaml"),
        ),
    )
    applicability_yaml = _resolve_path(
        cfg_path,
        cfg_get(
            resolver_cfg,
            "applicability_yaml",
            default=str(Path("fundamental_data") / "sec_metric_applicability_policy.yaml"),
        ),
    )
    issuer_override_csv = _resolve_path(
        cfg_path,
        cfg_get(
            resolver_cfg,
            "issuer_override_csv",
            default=str(Path("fundamental_data") / "sec_issuer_metric_override_seed.csv"),
        ),
    )
    candidate_csv = _resolve_path(
        cfg_path,
        cfg_get(resolver_cfg, "candidate_csv", default=str(Path("output") / "sec_gap_candidates_latest.csv")),
    )
    missing_tickers_csv = _resolve_path(
        cfg_path,
        cfg_get(
            resolver_cfg,
            "missing_tickers_csv",
            default=str(Path("output") / "sec_missing_metrics_tickers.csv"),
        ),
    )
    prior_enabled = bool(cfg_get(resolver_cfg, "prior_filing_fallback_enabled", default=True))
    prior_max_days = int(cfg_get(resolver_cfg, "prior_filing_max_staleness_days", default=550))
    effective_as_of_date = as_of_date or _infer_latest_as_of_date(db_path=db_path, snapshot_table=snapshot_table)

    cmd = [
        py,
        str(resolver_script),
        "--db-url",
        db_url,
        "--snapshot-table",
        snapshot_table,
        "--facts-table",
        facts_table,
        "--output-table",
        output_table,
        "--persist",
        "--prior-filing-fallback-enabled",
        "true" if prior_enabled else "false",
        "--prior-filing-max-staleness-days",
        str(prior_max_days),
    ]
    if effective_as_of_date:
        cmd.extend(["--as-of-date", effective_as_of_date])
    if candidate_table:
        cmd.extend(["--candidate-table", str(candidate_table)])
    if extension_yaml is not None:
        if not extension_yaml.exists():
            raise FileNotFoundError(f"Resolver extension_rule_yaml not found: {extension_yaml}")
        cmd.extend(["--extension-rule-yaml", str(extension_yaml)])
    if applicability_yaml is not None:
        if not applicability_yaml.exists():
            raise FileNotFoundError(f"Resolver applicability_yaml not found: {applicability_yaml}")
        cmd.extend(["--applicability-yaml", str(applicability_yaml)])
    if issuer_override_csv is not None:
        if not issuer_override_csv.exists():
            raise FileNotFoundError(f"Resolver issuer_override_csv not found: {issuer_override_csv}")
        cmd.extend(["--issuer-override-csv", str(issuer_override_csv)])
    if candidate_csv is not None:
        candidate_csv.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--candidate-csv", str(candidate_csv)])
    if missing_tickers_csv is not None:
        missing_tickers_csv.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--missing-tickers-csv", str(missing_tickers_csv)])
    run_step(cmd)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    py = sys.executable
    cfg = str(args.config)
    _, pipeline_cfg = load_sec_fundamentals_config(args.config)

    run_step([py, str(Path(__file__).resolve().with_name("init_sec_fundamentals_db.py")), "--config", cfg])

    ingest_cmd = [py, str(Path(__file__).resolve().with_name("ingest_sec_fundamentals_tier1.py")), "--config", cfg]
    if args.mode:
        ingest_cmd.extend(["--mode", args.mode])
    if args.start_date:
        ingest_cmd.extend(["--start-date", args.start_date])
    if args.end_date:
        ingest_cmd.extend(["--end-date", args.end_date])
    run_step(ingest_cmd)

    build_cmd = [py, str(Path(__file__).resolve().with_name("build_sec_fundamental_features_tier1.py")), "--config", cfg]
    if args.as_of_date:
        build_cmd.extend(["--as-of-date", args.as_of_date])
    run_step(build_cmd)

    maybe_compile_metric_mapping(py=py, config_path=args.config)

    enhanced_cmd = [py, str(Path(__file__).resolve().with_name("build_sec_tier1_snapshot_enhanced.py")), "--config", cfg]
    if args.as_of_date:
        enhanced_cmd.extend(["--as-of-date", args.as_of_date])
    if args.quality_gate_override:
        enhanced_cmd.append("--quality-gate-override")
    run_step(enhanced_cmd)
    effective_snapshot_as_of_date = require_enhanced_snapshot_rows(
        config_path=args.config,
        cfg=pipeline_cfg,
        requested_as_of_date=args.as_of_date,
    )

    maybe_run_snapshot_resolver(py=py, config_path=args.config, as_of_date=effective_snapshot_as_of_date)

    export_cmd = [py, str(Path(__file__).resolve().with_name("export_sec_fundamentals_for_pipeline.py")), "--config", cfg]
    export_cmd.extend(["--as-of-date", effective_snapshot_as_of_date])
    run_step(export_cmd)
    logger.info("SEC fundamentals pipeline completed.")


if __name__ == "__main__":
    main()
