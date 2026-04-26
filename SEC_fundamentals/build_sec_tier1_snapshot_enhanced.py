#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from sec_fundamentals_config import (
    cfg_get,
    configure_pipeline_logging,
    load_sec_fundamentals_config,
    normalize_cik_10d,
    parse_iso_date,
    previous_or_same_business_day,
    sql_normalized_cik_expr,
    validate_sql_identifier,
)
from sec_tier1_snapshot_enhanced import (
    SnapshotConfig,
    Tier1FundamentalSnapshotBuilder,
    default_metric_mapping_df,
    make_as_of_timestamp,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config_sec_fundamentals.yaml")
DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\PROD\DB\sec_fundamentals.sqlite")
SQLITE_BUSY_TIMEOUT_MS = 30000
logger = logging.getLogger(__name__)


def _resolve_db_path(
    *,
    cfg_path: Path,
    cfg: dict[str, Any],
    db_path_override: Path | None,
) -> Path:
    raw_value = db_path_override if db_path_override is not None else cfg_get(cfg, "db_path", default=str(DEFAULT_DB_PATH))
    path = Path(raw_value).expanduser()
    if path.is_absolute():
        return path
    if db_path_override is not None:
        return (Path.cwd() / path).resolve()
    return (cfg_path.parent.parent / path).resolve()

def _normalize_ticker(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.upper()


def _normalize_cik(series: pd.Series) -> pd.Series:
    digits = (
        series.fillna("")
        .astype(str)
        .str.replace(r"\D", "", regex=True)
    )
    digits = digits.where(digits != "", pd.NA)
    return digits.map(lambda v: v.zfill(10) if isinstance(v, str) else pd.NA)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _to_sqlite_timestamp(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _prepare_sqlite_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            out[col] = pd.to_datetime(s, errors="coerce").map(
                lambda v: None if pd.isna(v) else v.isoformat()
            )
            continue
        if s.dtype == object and s.map(lambda v: isinstance(v, (pd.Timestamp, datetime))).any():
            out[col] = s.map(_to_sqlite_timestamp)
    return out


def _upsert_asof_df(
    conn: sqlite3.Connection,
    table_name: str,
    as_of_date: str,
    df: pd.DataFrame,
) -> None:
    table_name = validate_sql_identifier(table_name, "table_name", allow_dotted=True)
    write_df = _prepare_sqlite_frame(df)
    dedupe_candidates = [
        ["as_of_date", "ticker", "cik", "accession_number", "report_period_end"],
        ["as_of_date", "ticker", "cik", "accession_number"],
        ["as_of_date", "ticker", "cik"],
        ["as_of_date", "cik", "accession_number"],
        ["as_of_date"],
    ]
    for subset in dedupe_candidates:
        if all(col in write_df.columns for col in subset):
            write_df = write_df.drop_duplicates(subset=subset, keep="last")
            break
    write_df = write_df.drop_duplicates(keep="last")
    if _table_exists(conn, table_name):
        conn.execute(f"DELETE FROM {table_name} WHERE as_of_date = ?", (as_of_date,))
        if not write_df.empty:
            write_df.to_sql(table_name, conn, if_exists="append", index=False)
        return
    if write_df.empty:
        return
    write_df.to_sql(table_name, conn, if_exists="replace", index=False)


def _load_universe_df(universe_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(universe_csv)
    cols = {c.strip().lower(): c for c in df.columns}
    ticker_col = cols.get("ticker") or cols.get("matchedticker") or cols.get("symbol")
    cik_col = cols.get("cik")
    if ticker_col is None:
        raise ValueError(f"Universe CSV missing ticker column: {universe_csv}")

    out = pd.DataFrame({"ticker": _normalize_ticker(df[ticker_col])})
    if cik_col is not None:
        out["cik"] = _normalize_cik(df[cik_col])
    else:
        out["cik"] = pd.NA
    out = out[out["ticker"] != ""].drop_duplicates(subset=["ticker"], keep="first")
    return out.reset_index(drop=True)


def _load_issuer_profile_df(profile_csv: Path | None) -> pd.DataFrame | None:
    if profile_csv is None or not profile_csv.exists():
        return None
    df = pd.read_csv(profile_csv)
    cols = {c.strip().lower(): c for c in df.columns}
    ticker_col = cols.get("ticker") or cols.get("symbol")
    sector_col = cols.get("sector")
    industry_col = cols.get("industry")
    indagg_col = cols.get("industry_aggregate")
    if ticker_col is None or sector_col is None or industry_col is None:
        return None
    keep = [ticker_col, sector_col, industry_col]
    if indagg_col is not None:
        keep.append(indagg_col)
    out = df[keep].copy()
    rename = {
        ticker_col: "ticker",
        sector_col: "sector",
        industry_col: "industry",
    }
    if indagg_col is not None:
        rename[indagg_col] = "industry_aggregate"
    out = out.rename(columns=rename)
    if "industry_aggregate" not in out.columns:
        out["industry_aggregate"] = out["industry"]
    out["ticker"] = _normalize_ticker(out["ticker"])
    for col in ("sector", "industry", "industry_aggregate"):
        cleaned = out[col].astype("string").str.strip()
        out[col] = cleaned.where(cleaned.notna() & cleaned.ne(""), None).astype("object")
    out = out[out["ticker"] != ""].drop_duplicates(subset=["ticker"], keep="first")
    return out.reset_index(drop=True)


def _load_metric_mapping_df(metric_mapping_csv: Path | None, required: bool) -> pd.DataFrame:
    if metric_mapping_csv is not None:
        if not metric_mapping_csv.exists():
            raise FileNotFoundError(f"Metric mapping CSV not found: {metric_mapping_csv}")
        df = pd.read_csv(metric_mapping_csv)
        if df.empty:
            raise ValueError(f"Metric mapping CSV is empty: {metric_mapping_csv}")
        required_cols = {"metric_name", "source_kind", "taxonomy", "concept_name", "priority"}
        missing = sorted(required_cols - set(df.columns))
        if missing:
            raise ValueError(
                f"Metric mapping CSV missing required columns {missing}: {metric_mapping_csv}"
            )
        return df
    if required:
        raise ValueError(
            "snapshot_enhanced.metric_mapping_csv is required but not configured. "
            "Compile and configure a full mapping CSV first."
        )
    return default_metric_mapping_df()


def _load_source_df(
    conn: sqlite3.Connection,
    as_of_date: str,
    cik_filter: pd.Series | None,
    core_metrics: list[str],
    lookback_days: int | None = None,
) -> pd.DataFrame:
    cik_expr = sql_normalized_cik_expr("p.cik")
    select_cols = ", ".join(
        [
            f"{cik_expr} AS cik",
            "p.ticker",
            "p.accession_number",
            "p.form_type",
            "p.filing_date",
            "p.acceptance_datetime",
            "p.report_period_end",
            *[f"p.{col}" for col in core_metrics],
        ]
    )
    lookback_sql = ""
    params: list[Any] = [as_of_date]
    if lookback_days is not None and int(lookback_days) > 0:
        lookback_sql = (
            " AND date(COALESCE(p.report_period_end, p.filing_date)) >= date(?, '-' || ? || ' day')"
        )
        params.extend([as_of_date, int(lookback_days)])

    if cik_filter is None or cik_filter.empty:
        return pd.read_sql_query(
            f"SELECT {select_cols} FROM sec_fundamental_period_t1 p WHERE p.as_of_date = ? AND {cik_expr} IS NOT NULL{lookback_sql}",
            conn,
            params=params,
        )

    conn.execute("DROP TABLE IF EXISTS _tmp_snapshot_universe_cik")
    try:
        conn.execute("CREATE TEMP TABLE _tmp_snapshot_universe_cik (cik TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT OR IGNORE INTO _tmp_snapshot_universe_cik(cik) VALUES (?)",
            [
                (norm_cik,)
                for v in cik_filter.dropna().astype(str).tolist()
                if (norm_cik := normalize_cik_10d(v)) is not None
            ],
        )
        return pd.read_sql_query(
            f"""
            SELECT {select_cols}
            FROM sec_fundamental_period_t1 p
            JOIN _tmp_snapshot_universe_cik u
              ON {cik_expr} = u.cik
            WHERE p.as_of_date = ? AND {cik_expr} IS NOT NULL{lookback_sql}
            """,
            conn,
            params=params,
        )
    finally:
        conn.execute("DROP TABLE IF EXISTS _tmp_snapshot_universe_cik")


def _load_metric_facts_df(
    conn: sqlite3.Connection,
    source_df: pd.DataFrame,
    metric_mapping_df: pd.DataFrame,
    repair_only_missing_accessions: bool,
    core_metrics: list[str],
) -> pd.DataFrame:
    if source_df.empty:
        return pd.DataFrame()

    if repair_only_missing_accessions:
        missing_mask = source_df[core_metrics].isna().any(axis=1)
        accession_series = source_df.loc[missing_mask, "accession_number"]
    else:
        accession_series = source_df["accession_number"]
    accessions = accession_series.dropna().astype(str).drop_duplicates().tolist()
    if not accessions:
        return pd.DataFrame()

    req_cols = {"taxonomy", "concept_name"}
    if not req_cols.issubset(metric_mapping_df.columns):
        return pd.DataFrame()

    pairs = sorted(
        {
            (str(tax).lower(), str(tag))
            for tax, tag in metric_mapping_df[["taxonomy", "concept_name"]].itertuples(index=False)
        }
    )
    if not pairs:
        return pd.DataFrame()

    conn.execute("DROP TABLE IF EXISTS _tmp_metric_pairs")
    conn.execute("CREATE TEMP TABLE _tmp_metric_pairs (taxonomy TEXT, concept TEXT)")
    conn.executemany(
        "INSERT INTO _tmp_metric_pairs(taxonomy, concept) VALUES (?, ?)",
        pairs,
    )

    conn.execute("DROP TABLE IF EXISTS _tmp_metric_accessions")
    conn.execute("CREATE TEMP TABLE _tmp_metric_accessions (accession TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO _tmp_metric_accessions(accession) VALUES (?)",
        [(a,) for a in accessions],
    )

    facts_cik_expr = sql_normalized_cik_expr("f.cik")
    filing_cik_expr = sql_normalized_cik_expr("fi.cik")
    facts = pd.read_sql_query(
        """
        SELECT
            {facts_cik_expr} AS cik,
            f.accession_number AS accession_number,
            COALESCE(fi.report_period_end, f.report_period_end) AS report_period_end,
            lower(f.taxonomy) AS taxonomy,
            f.tag AS concept_name,
            f.value_num AS fact_value,
            NULL AS context_id,
            f.unit AS unit,
            NULL AS period_type,
            0 AS dimension_count,
            NULL AS statement_type
        FROM sec_xbrl_facts_raw f
        LEFT JOIN sec_filing_index fi
          ON f.accession_number = fi.accession_number
         AND {facts_cik_expr} = {filing_cik_expr}
        JOIN _tmp_metric_accessions a
          ON f.accession_number = a.accession
        JOIN _tmp_metric_pairs p
          ON lower(f.taxonomy) = p.taxonomy
         AND f.tag = p.concept
        WHERE {facts_cik_expr} IS NOT NULL
        """.format(facts_cik_expr=facts_cik_expr, filing_cik_expr=filing_cik_expr),
        conn,
    )
    if not facts.empty and "cik" in facts.columns:
        missing_cik = facts["cik"].isna().sum()
        if int(missing_cik) > 0:
            logger.warning(
                "_load_metric_facts_df dropped %d fact rows with missing cik (cannot map to 10-digit CIK).",
                int(missing_cik),
            )
            facts = facts[facts["cik"].notna()].copy()
    return facts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build enhanced SEC tier-1 snapshots (entity + security) from config.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to config_sec_fundamentals.yaml")
    parser.add_argument("--db-path", type=Path, default=None, help="Override SQLite DB path")
    parser.add_argument("--as-of-date", type=str, default=None, help="As-of date YYYY-MM-DD")
    parser.add_argument("--no-persist", action="store_true", help="Run and print diagnostics without writing tables")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        help="Optional output directory for per-date snapshot artifacts.",
    )
    parser.add_argument(
        "--quality-gate-override",
        action="store_true",
        help="Temporarily disable snapshot_enhanced quality-gate enforcement for this run.",
    )
    return parser.parse_args()


def _build_run_row(
    *,
    as_of_date: str,
    as_of_timestamp: pd.Timestamp,
    source_df: pd.DataFrame,
    result: Any,
    entity_strict_df: pd.DataFrame,
    entity_filled_df: pd.DataFrame,
    security_strict_df: pd.DataFrame,
    security_filled_df: pd.DataFrame,
    coverage_df: pd.DataFrame,
    audit_df: pd.DataFrame,
    snapshot_cfg: SnapshotConfig,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "as_of_date": as_of_date,
                "as_of_timestamp": as_of_timestamp,
                "source_rows": int(len(source_df)),
                "prepared_rows": int(result.stats.get("prepared_rows", 0)),
                "accession_bundles": int(result.stats.get("accession_bundles", 0)),
                "entity_strict_rows": int(len(entity_strict_df)),
                "entity_filled_rows": int(len(entity_filled_df)),
                "security_strict_rows": int(len(security_strict_df)),
                "security_filled_rows": int(len(security_filled_df)),
                "entity_filled_non_null_by_metric_json": json.dumps(
                    result.stats.get("entity_filled_non_null_by_metric", {}),
                    sort_keys=True,
                ),
                "security_filled_non_null_by_metric_json": json.dumps(
                    result.stats.get("security_filled_non_null_by_metric", {}),
                    sort_keys=True,
                ),
                "coverage_report_json": coverage_df.to_json(orient="records", date_format="iso"),
                "audit_report_json": audit_df.to_json(orient="records", date_format="iso"),
                "config_json": json.dumps(snapshot_cfg.__dict__, sort_keys=True, default=str),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
    )


def _write_artifact_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def _write_artifacts(
    *,
    artifact_dir: Path,
    entity_strict_df: pd.DataFrame,
    entity_filled_df: pd.DataFrame,
    security_strict_df: pd.DataFrame,
    security_filled_df: pd.DataFrame,
    coverage_df: pd.DataFrame,
    audit_df: pd.DataFrame,
    run_row_df: pd.DataFrame,
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    _write_artifact_csv(entity_strict_df, artifact_dir / "entity_strict.csv")
    _write_artifact_csv(entity_filled_df, artifact_dir / "entity_filled.csv")
    _write_artifact_csv(security_strict_df, artifact_dir / "security_strict.csv")
    _write_artifact_csv(security_filled_df, artifact_dir / "security_filled.csv")
    _write_artifact_csv(coverage_df, artifact_dir / "coverage_report.csv")
    _write_artifact_csv(audit_df, artifact_dir / "audit_report.csv")
    _write_artifact_csv(run_row_df, artifact_dir / "run_row.csv")
    logger.info("Wrote snapshot artifacts to %s", artifact_dir)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    cfg_path, cfg = load_sec_fundamentals_config(args.config)
    snap_cfg = cfg_get(cfg, "snapshot_enhanced", default={})
    if not isinstance(snap_cfg, dict):
        snap_cfg = {}

    db_path = _resolve_db_path(cfg_path=cfg_path, cfg=cfg, db_path_override=args.db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Fundamentals DB not found: {db_path}")

    cli_as_of = parse_iso_date(args.as_of_date)
    if args.as_of_date and cli_as_of is None:
        raise ValueError(f"Invalid --as-of-date: {args.as_of_date!r}")
    cfg_as_of_raw = cfg_get(cfg_get(cfg, "features", default={}), "as_of_date", default=None)
    cfg_as_of = parse_iso_date(cfg_as_of_raw)
    if cfg_as_of_raw is not None and str(cfg_as_of_raw).strip() and cfg_as_of is None:
        raise ValueError(f"Invalid features.as_of_date: {cfg_as_of_raw!r}")
    as_of_date_dt = cli_as_of or cfg_as_of
    if as_of_date_dt is None:
        conn = _connect_sqlite(db_path)
        try:
            row = conn.execute("SELECT MAX(as_of_date) FROM sec_fundamental_period_t1").fetchone()
        finally:
            conn.close()
        as_of_date_dt = parse_iso_date(str(row[0]) if row and row[0] else None) or datetime.now(timezone.utc).date()
    normalized_as_of_date = previous_or_same_business_day(as_of_date_dt)
    if normalized_as_of_date != as_of_date_dt:
        logger.info("Adjusted non-business as_of_date %s to %s.", as_of_date_dt.isoformat(), normalized_as_of_date.isoformat())
    as_of_date = normalized_as_of_date.isoformat()

    cutoff_time = str(cfg_get(snap_cfg, "cutoff_time", default="16:15:00"))
    cutoff_tz = str(cfg_get(snap_cfg, "cutoff_timezone", default="America/New_York"))
    as_of_timestamp = make_as_of_timestamp(as_of_date=as_of_date, cutoff_time=cutoff_time, timezone=cutoff_tz)

    universe_raw = cfg_get(snap_cfg, "universe_csv", default=None) or cfg_get(cfg, "universe_csv", default=None)
    if not universe_raw:
        raise ValueError("Missing universe CSV path. Set sec_fundamentals.universe_csv or snapshot_enhanced.universe_csv.")
    universe_csv = Path(str(universe_raw))
    if not universe_csv.is_absolute():
        universe_csv = (cfg_path.parent.parent / universe_csv).resolve()
    if not universe_csv.exists():
        raise FileNotFoundError(f"Universe CSV not found: {universe_csv}")
    universe_df = _load_universe_df(universe_csv)

    issuer_profile_raw = cfg_get(snap_cfg, "issuer_profile_csv", default=None)
    issuer_profile_csv: Path | None = None
    if issuer_profile_raw:
        issuer_profile_csv = Path(str(issuer_profile_raw))
        if not issuer_profile_csv.is_absolute():
            issuer_profile_csv = (cfg_path.parent.parent / issuer_profile_csv).resolve()
    issuer_profile_df = _load_issuer_profile_df(issuer_profile_csv)

    metric_mapping_raw = cfg_get(snap_cfg, "metric_mapping_csv", default=None)
    metric_mapping_required = bool(cfg_get(snap_cfg, "metric_mapping_required", default=True))
    metric_mapping_csv: Path | None = None
    if metric_mapping_raw:
        metric_mapping_csv = Path(str(metric_mapping_raw))
        if not metric_mapping_csv.is_absolute():
            metric_mapping_csv = (cfg_path.parent.parent / metric_mapping_csv).resolve()
    metric_mapping_df = _load_metric_mapping_df(metric_mapping_csv, required=metric_mapping_required)

    core_metrics = ["revenue", "net_income", "operating_cash_flow", "total_assets", "total_equity"]

    conn = _connect_sqlite(db_path)
    try:
        cik_filter = universe_df["cik"] if "cik" in universe_df.columns else None
        lookback_days = int(cfg_get(snap_cfg, "lookback_days", default=900))
        source_df = _load_source_df(
            conn,
            as_of_date=as_of_date,
            cik_filter=cik_filter,
            core_metrics=core_metrics,
            lookback_days=lookback_days,
        )
        if source_df.empty:
            raise RuntimeError(
                f"No sec_fundamental_period_t1 rows found for as_of_date={as_of_date} "
                "after applying the current universe filter. Build tier-1 period features first."
            )
        repair_only_missing = bool(cfg_get(snap_cfg, "repair_only_for_missing_accessions", default=True))
        metric_facts_df = _load_metric_facts_df(
            conn,
            source_df=source_df,
            metric_mapping_df=metric_mapping_df,
            repair_only_missing_accessions=repair_only_missing,
            core_metrics=core_metrics,
        )
    finally:
        conn.close()

    enforce_quality_gates = bool(cfg_get(snap_cfg, "enforce_quality_gates", default=False))
    if args.quality_gate_override:
        enforce_quality_gates = False

    snapshot_cfg = SnapshotConfig(
        same_filing_repair_enabled=bool(cfg_get(snap_cfg, "same_filing_repair_enabled", default=True)),
        output_security_snapshots=bool(cfg_get(snap_cfg, "output_security_snapshots", default=True)),
        fanout_aliases=bool(cfg_get(snap_cfg, "fanout_aliases", default=True)),
        include_missing_universe_rows=True,
        lookback_days=lookback_days,
        publication_lag_minutes=int(cfg_get(snap_cfg, "publication_lag_minutes", default=0)),
        enforce_quality_gates=enforce_quality_gates,
        max_all5_missing_entity=int(cfg_get(snap_cfg, "max_all5_missing_entity", default=0)),
        strict_table=str(cfg_get(snap_cfg, "strict_table", default="sec_fundamental_snapshot_strict_t1")),
        filled_table=str(cfg_get(snap_cfg, "filled_table", default="sec_fundamental_snapshot_filled_t1")),
        security_strict_table=str(cfg_get(snap_cfg, "security_strict_table", default="sec_fundamental_snapshot_strict_security_t1")),
        security_filled_table=str(cfg_get(snap_cfg, "security_filled_table", default="sec_fundamental_snapshot_filled_security_t1")),
        run_table=str(cfg_get(snap_cfg, "run_table", default="sec_fundamental_snapshot_run_t1")),
    )
    if args.quality_gate_override:
        logger.info("Quality-gate override enabled for this run (enforce_quality_gates=false).")
    builder = Tier1FundamentalSnapshotBuilder(engine=None, config=snapshot_cfg)
    result = builder.run_from_dataframes(
        as_of_timestamp=as_of_timestamp,
        source_df=source_df,
        universe_df=universe_df,
        alias_df=None,
        issuer_profile_df=issuer_profile_df,
        metric_facts_df=metric_facts_df,
        metric_mapping_df=metric_mapping_df,
        persist=False,
    )

    entity_strict_df = result.entity_strict_df.copy()
    entity_filled_df = result.entity_filled_df.copy()
    security_strict_df = result.security_strict_df.copy()
    security_filled_df = result.security_filled_df.copy()
    coverage_df = result.coverage_report_df.copy()
    audit_df = result.audit_report_df.copy()
    run_row = _build_run_row(
        as_of_date=as_of_date,
        as_of_timestamp=as_of_timestamp,
        source_df=source_df,
        result=result,
        entity_strict_df=entity_strict_df,
        entity_filled_df=entity_filled_df,
        security_strict_df=security_strict_df,
        security_filled_df=security_filled_df,
        coverage_df=coverage_df,
        audit_df=audit_df,
        snapshot_cfg=snapshot_cfg,
    )

    if args.artifact_dir is not None:
        _write_artifacts(
            artifact_dir=args.artifact_dir,
            entity_strict_df=entity_strict_df,
            entity_filled_df=entity_filled_df,
            security_strict_df=security_strict_df,
            security_filled_df=security_filled_df,
            coverage_df=coverage_df,
            audit_df=audit_df,
            run_row_df=run_row,
        )

    if not args.no_persist:
        conn = _connect_sqlite(db_path)
        try:
            _upsert_asof_df(conn, snapshot_cfg.strict_table, as_of_date, entity_strict_df)
            _upsert_asof_df(conn, snapshot_cfg.filled_table, as_of_date, entity_filled_df)
            _upsert_asof_df(conn, snapshot_cfg.security_strict_table, as_of_date, security_strict_df)
            _upsert_asof_df(conn, snapshot_cfg.security_filled_table, as_of_date, security_filled_df)
            _upsert_asof_df(conn, snapshot_cfg.run_table, as_of_date, run_row)
            conn.commit()
        finally:
            conn.close()

    core = snapshot_cfg.metric_names()
    all5_missing = int(security_filled_df[core].isna().all(axis=1).sum()) if not security_filled_df.empty else 0
    any_missing = int(security_filled_df[core].isna().any(axis=1).sum()) if not security_filled_df.empty else 0

    logger.info("Built enhanced SEC snapshot for as_of_date=%s", as_of_date)
    logger.info("Universe rows: %s", f"{len(universe_df):,}")
    logger.info("Source rows: %s", f"{len(source_df):,}")
    logger.info("Metric fact rows used for same-filing repair: %s", f"{len(metric_facts_df):,}")
    logger.info("Security snapshot rows: %s", f"{len(security_filled_df):,}")
    logger.info("Security all-5-core-missing: %s", f"{all5_missing:,}")
    logger.info("Security any-core-missing: %s", f"{any_missing:,}")
    if not coverage_df.empty:
        logger.info("Coverage report:\n%s", coverage_df.to_string(index=False))
    if not audit_df.empty:
        logger.info("Audit report:\n%s", audit_df.to_string(index=False))


if __name__ == "__main__":
    main()
