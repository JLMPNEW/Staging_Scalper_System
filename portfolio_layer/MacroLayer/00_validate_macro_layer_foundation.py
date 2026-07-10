#!/usr/bin/env python3
"""Foundation checks for the Staging-owned MacroLayer copy.

This script validates the copied macro seed before Stage 6 adaptation:
  1. no filesystem/import coupling back to PROD;
  2. no literal API keys in config;
  3. configured paths stay under portfolio_layer;
  4. copied raw/serving SQLite DBs are readable;
  5. serving DB exposes the tables/columns Stage 7 will consume.

It does not run live macro refreshes. Live connectors remain controlled by
environment variables and later Stage 6 orchestration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


MACRO_ROOT = Path(__file__).resolve().parent
PORTFOLIO_ROOT = MACRO_ROOT.parent
PROJECT_ROOT = PORTFOLIO_ROOT.parent
DEFAULT_CONFIG = MACRO_ROOT / "config_macro_raw.yaml"
DEFAULT_MANIFEST = MACRO_ROOT / "macro_seed_manifest.json"

PROD_COUPLING_RE = re.compile(
    r"(?i)(?:[A-Za-z]:)?[\\/][^\r\n\"']*[\\/]PROD[\\/]"
    r"|[\\/]PROD_Scalper_System|PROD_Scalper_System[\\/]"
    r"|^\s*(?:from|import)\s+(?:PROD_Scalper_System|PROD)\b"
)
HARD_LEGACY_IMPORT_RE = re.compile(
    r"^(?:from|import)\s+"
    r"(?:BackTest|tier1_common|tier1_portfolio_optimizer|technology|biotech_index|med_devices|SEC_FORM4_Runner|ticker_mapping)\b"
)
TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".md"}
PATH_CONFIG_KEYS = {
    "db_path",
    "serving_db_path",
    "registry_csv",
    "metric_policy_csv",
    "feature_policy_csv",
    "composite_policy_csv",
    "country_metadata_csv",
    "release_calendar_csv",
    "cache_dir",
    "output_dir",
    "sec_db_path",
}
REQUIRED_RAW_TABLES = {
    "macro_ingest_run",
    "macro_metric_registry",
    "macro_observation_raw",
    "macro_release_calendar",
}
REQUIRED_SERVING_TABLES: dict[str, set[str]] = {
    "macro_regime_decision_daily": {
        "as_of_date",
        "active_current_regime",
        "active_next_regime",
        "current_confidence",
        "next_confidence",
    },
    "macro_regime_v2_decision_daily": {
        "model_version",
        "as_of_date",
        "active_current_regime",
        "active_next_regime",
        "current_confidence",
        "next_confidence",
        "coverage_flag",
    },
    "macro_regime_v2_promotion_summary": {
        "model_version",
        "evidence_as_of_date",
        "acceptance",
        "validated_cell_count",
        "required_cell_count",
    },
    "stock_sector_target_daily": {
        "as_of_date",
        "sector_name",
        "target_weight",
        "min_weight",
        "max_weight",
    },
    "country_macro_fit_daily": {
        "as_of_date",
        "ticker",
        "ref_area",
        "country_name",
        "country_macro_fit",
        "confidence_adjusted_fit",
    },
    "foreign_sleeve_budget_daily": {
        "as_of_date",
        "foreign_budget",
        "min_budget",
        "max_budget",
        "eligible_candidate_count",
    },
    "foreign_sleeve_candidate_daily": {
        "as_of_date",
        "ticker",
        "candidate_score",
        "sleeve_weight",
    },
    "stock_macro_fit_daily": {
        "as_of_date",
        "ticker",
        "sector_name",
        "macro_stock_fit_z",
        "sector_macro_fit",
        "shock_fit",
        "base_optimizer_eligible",
    },
    "portfolio_inputs_daily": {
        "as_of_date",
        "ticker",
        "sleeve",
        "sector_name",
        "final_score",
        "state",
        "macro_overlay_enabled",
    },
}
OPTIONAL_EMPTY_SERVING_TABLES = {"foreign_sleeve_candidate_daily"}

LOGGER = logging.getLogger("validate_macro_layer_foundation")


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


def _load_config(config_path: Path) -> dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("macro_raw", data)
    return raw if isinstance(raw, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_config_path(raw_value: Any) -> Path | None:
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (PORTFOLIO_ROOT / path).resolve()


def _iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in MACRO_ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


def _check_no_prod_references() -> CheckResult:
    offenders: list[str] = []
    for path in _iter_text_files():
        if path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if PROD_COUPLING_RE.search(line):
                offenders.append(f"{path.relative_to(PORTFOLIO_ROOT)}:{lineno}")
    if offenders:
        return CheckResult("no_prod_path_or_import_coupling", False, f"found {offenders}")
    return CheckResult("no_prod_path_or_import_coupling", True, "no PROD filesystem/import coupling")


def _check_source_api_credentials(cfg: dict[str, Any]) -> CheckResult:
    problems: list[str] = []
    resolved: list[str] = []
    sources = cfg.get("sources", {})
    if isinstance(sources, dict):
        for source_name, source_cfg in sources.items():
            if not isinstance(source_cfg, dict):
                continue
            env_name = str(source_cfg.get("api_key_env", "") or "").strip()
            if env_name and not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
                problems.append(f"sources.{source_name}.api_key_env")
                continue
            literal_key = str(source_cfg.get("api_key", "") or "").strip()
            if literal_key and literal_key.lower() not in {"null", "none"}:
                if literal_key.lower() in {"changeme", "placeholder", "todo", "required"}:
                    problems.append(f"sources.{source_name}.api_key_placeholder")
                else:
                    resolved.append(f"{source_name}:config")
                continue
            if env_name:
                if os.getenv(env_name):
                    resolved.append(f"{source_name}:env")
                else:
                    problems.append(f"sources.{source_name}.missing_env:{env_name}")
    if problems:
        return CheckResult("source_api_credentials_configured", False, f"credential problems: {problems}")
    detail = "no keyed sources configured" if not resolved else f"credentials available via {resolved}; values redacted"
    return CheckResult("source_api_credentials_configured", True, detail)


def _check_no_hard_legacy_imports() -> CheckResult:
    offenders: list[str] = []
    for path in MACRO_ROOT.rglob("*.py"):
        if path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if HARD_LEGACY_IMPORT_RE.search(line):
                offenders.append(f"{path.relative_to(PORTFOLIO_ROOT)}:{lineno}")
    if offenders:
        return CheckResult("no_hard_legacy_imports", False, f"found {offenders}")
    return CheckResult("no_hard_legacy_imports", True, "no module-load dependency on legacy BackTest/tier1/sector trees")


def _walk_config_paths(value: Any, path: tuple[str, ...] = ()) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            next_path = (*path, str(key))
            if key in PATH_CONFIG_KEYS:
                out.append((".".join(next_path), child))
            out.extend(_walk_config_paths(child, next_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            out.extend(_walk_config_paths(child, (*path, str(index))))
    return out


def _check_config_paths_staging_rooted(cfg: dict[str, Any]) -> CheckResult:
    escaped: dict[str, str] = {}
    for key_path, raw_value in _walk_config_paths(cfg):
        resolved = _resolve_config_path(raw_value)
        if resolved is None:
            continue
        if not _is_within(resolved, PORTFOLIO_ROOT):
            escaped[key_path] = str(resolved)
    if escaped:
        return CheckResult("config_paths_under_portfolio_layer", False, f"escaped paths: {escaped}")
    return CheckResult("config_paths_under_portfolio_layer", True, "configured file paths resolve under portfolio_layer")


def _sqlite_connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30.0)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _table_count(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _max_as_of(conn: sqlite3.Connection, table_name: str) -> str | None:
    columns = _table_columns(conn, table_name)
    if "as_of_date" not in columns:
        return None
    row = conn.execute(f"SELECT MAX(as_of_date) FROM {table_name}").fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _check_db_health(cfg: dict[str, Any]) -> tuple[CheckResult, dict[str, Any]]:
    db_info: dict[str, Any] = {}
    raw_db = _resolve_config_path(cfg.get("db_path"))
    serving_db = _resolve_config_path(cfg.get("serving_db_path"))
    required = {"raw": raw_db, "serving": serving_db}
    for label, path in required.items():
        if path is None:
            return CheckResult("sqlite_db_health", False, f"{label} DB path is not configured"), db_info
        if not path.exists():
            return CheckResult("sqlite_db_health", False, f"{label} DB missing: {path}"), db_info
        with _sqlite_connect_ro(path) as conn:
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            db_info[label] = {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "quick_check": quick_check,
                "table_count": len(tables),
            }
            if quick_check.lower() != "ok":
                return CheckResult("sqlite_db_health", False, f"{label} quick_check={quick_check}"), db_info
            missing = (REQUIRED_RAW_TABLES if label == "raw" else set(REQUIRED_SERVING_TABLES)) - tables
            if missing:
                return CheckResult("sqlite_db_health", False, f"{label} missing tables: {sorted(missing)}"), db_info
    return CheckResult("sqlite_db_health", True, "raw and serving DBs open read-only and pass quick_check"), db_info


def _check_stage7_contract(cfg: dict[str, Any]) -> tuple[CheckResult, dict[str, Any]]:
    serving_db = _resolve_config_path(cfg.get("serving_db_path"))
    table_info: dict[str, Any] = {}
    if serving_db is None or not serving_db.exists():
        return CheckResult("stage7_contract_tables", False, "serving DB is missing"), table_info
    with _sqlite_connect_ro(serving_db) as conn:
        for table_name, required_columns in REQUIRED_SERVING_TABLES.items():
            columns = _table_columns(conn, table_name)
            missing_columns = required_columns - columns
            row_count = _table_count(conn, table_name)
            table_info[table_name] = {
                "row_count": row_count,
                "max_as_of_date": _max_as_of(conn, table_name),
                "required_columns_present": not missing_columns,
            }
            if missing_columns:
                return (
                    CheckResult(
                        "stage7_contract_tables",
                        False,
                        f"{table_name} missing columns: {sorted(missing_columns)}",
                    ),
                    table_info,
                )
            if row_count <= 0 and table_name not in OPTIONAL_EMPTY_SERVING_TABLES:
                return CheckResult("stage7_contract_tables", False, f"{table_name} has no rows"), table_info
    return CheckResult("stage7_contract_tables", True, "serving DB exposes Stage 7 contract tables"), table_info


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_manifest(
    *,
    manifest_path: Path,
    checks: list[CheckResult],
    db_info: dict[str, Any],
    table_info: dict[str, Any],
    hash_db: bool,
    cfg: dict[str, Any],
) -> None:
    db_hashes: dict[str, str] = {}
    if hash_db:
        for label, raw_path in {"raw": cfg.get("db_path"), "serving": cfg.get("serving_db_path")}.items():
            path = _resolve_config_path(raw_path)
            if path is not None and path.exists():
                db_hashes[label] = _sha256_file(path)
    manifest = {
        "generated_at_utc": _utc_now(),
        "macro_root": str(MACRO_ROOT),
        "acceptance": "PASS" if all(c.ok for c in checks) else "FAIL",
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
        "db_info": db_info,
        "stage7_contract_tables": table_info,
        "config_sha256": _sha256_file(DEFAULT_CONFIG) if DEFAULT_CONFIG.exists() else None,
        "db_sha256": db_hashes,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--hash-db", action="store_true", help="Hash large copied DB files; slower.")
    args = parser.parse_args(argv)

    _configure_logging()
    cfg = _load_config(args.config)
    checks: list[CheckResult] = [
        _check_no_prod_references(),
        _check_source_api_credentials(cfg),
        _check_no_hard_legacy_imports(),
        _check_config_paths_staging_rooted(cfg),
    ]
    db_check, db_info = _check_db_health(cfg)
    checks.append(db_check)
    contract_check, table_info = _check_stage7_contract(cfg)
    checks.append(contract_check)

    for check in checks:
        LOGGER.info("[%s] %s -- %s", "PASS" if check.ok else "FAIL", check.name, check.detail)

    _write_manifest(
        manifest_path=args.manifest,
        checks=checks,
        db_info=db_info,
        table_info=table_info,
        hash_db=args.hash_db,
        cfg=cfg,
    )
    LOGGER.info("wrote manifest: %s", args.manifest)
    if all(check.ok for check in checks):
        LOGGER.info("MACRO LAYER FOUNDATION: PASS")
        return 0
    LOGGER.error("MACRO LAYER FOUNDATION: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
