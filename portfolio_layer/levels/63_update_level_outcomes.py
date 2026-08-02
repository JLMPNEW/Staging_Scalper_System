#!/usr/bin/env python3
"""Append all active and inactive level publications to the immutable evidence ledger."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import read_csv, read_manifest, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.market_data_common import (  # noqa: E402
    SELECTED_OHLCV_FILENAME,
    read_gzip_csv,
)
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    monitor_output_subdir,
)
from portfolio_layer.levels.levels_common import (  # noqa: E402
    LEVELS_MODEL_VERSION,
    append_level_publications,
    append_level_resolutions,
    connect_levels_db,
    numeric_series,
    optional_float,
    utc_now,
    verify_level_chain,
    verify_level_resolution_chain,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LEDGER_FIELDS = [
    "row_sequence", "previous_row_sha256", "row_sha256", "level_id", "published_as_of",
    "published_at_utc", "ticker", "band_type", "band_low", "band_high", "level_status",
    "inactive_reason", "market_price_at_publish", "model_version", "config_sha256",
    "input_manifest_sha256", "code_sha256",
]
SOURCE_ALIAS_FIELDS = [
    "publication_row_sha256", "config_sha256", "input_manifest_sha256",
    "code_sha256", "recorded_at_utc",
]
RESOLUTION_FIELDS = [
    "row_sequence", "previous_row_sha256", "row_sha256", "publication_row_sha256",
    "level_id", "ticker", "published_as_of", "band_type", "resolved_through",
    "first_touch_date", "trading_days_to_touch", "touched_flag",
    "maximum_favorable_excursion", "maximum_adverse_excursion",
    "resolution_available_at_utc",
]


def _publication_basis_window(
    frame: pd.DataFrame, *, horizon: int
) -> pd.DataFrame:
    """Convert future raw OHLC to the share basis in force at publication."""
    ordered = frame.sort_values("date").copy()
    for field in ("open", "high", "low", "close", "split_factor"):
        ordered[field] = numeric_series(ordered[field])
    ordered = ordered.dropna(subset=["open", "high", "low", "close", "split_factor"])
    ordered = ordered.loc[
        (ordered[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (ordered["split_factor"] > 0)
    ]
    if len(ordered) < horizon:
        return ordered.iloc[0:0].copy()
    window = ordered.iloc[:horizon].copy().reset_index(drop=True)
    cumulative_split = window["split_factor"].cumprod()
    for field in ("open", "high", "low", "close"):
        window[f"publication_basis_{field}"] = window[field] * cumulative_split
    return window


def _touch_fill_price(
    *, band_type: str, band_low: float, band_high: float, opening_price: float
) -> float:
    if band_type in {"starter", "add"}:
        return min(band_high, opening_price) if opening_price <= band_high else band_high
    return max(band_low, opening_price) if opening_price >= band_low else band_low


def preserve_first_write_levels(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Normalize same-date reruns to immutable published level fields."""
    normalized: list[dict[str, Any]] = []
    drift_count = 0
    immutable_fields = (
        "band_low",
        "band_high",
        "level_status",
        "inactive_reason",
        "market_price_at_publish",
        "model_version",
    )
    for raw in rows:
        row = dict(raw)
        existing = conn.execute(
            """
            SELECT band_low,band_high,level_status,inactive_reason,
                   market_price_at_publish,model_version
            FROM level_publication_ledger
            WHERE ticker=? AND published_as_of=? AND band_type=?
            """,
            (row["ticker"], row["published_as_of"], row["band_type"]),
        ).fetchone()
        if existing is not None:
            changed = any(row.get(field) != existing[field] for field in immutable_fields)
            drift_count += int(changed)
            for field in immutable_fields:
                row[field] = existing[field]
        normalized.append(row)
    return normalized, drift_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--market-data-dir", type=Path)
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def run_selftest() -> None:
    with TemporaryDirectory() as temporary:
        conn = connect_levels_db(Path(temporary) / "levels.sqlite", timeout_sec=5.0)
        publication = {
            "published_as_of": "2026-07-01", "published_at_utc": "2026-07-01T22:00:00+00:00",
            "ticker": "TEST", "band_type": "starter", "band_low": 90.0, "band_high": 95.0,
            "level_status": "active", "inactive_reason": "", "market_price_at_publish": 100.0,
            "model_version": LEVELS_MODEL_VERSION, "config_sha256": "a" * 64,
            "input_manifest_sha256": "b" * 64, "code_sha256": "c" * 64,
        }
        assert append_level_publications(conn, [publication]) == (1, 0)
        assert append_level_publications(conn, [publication]) == (0, 1)
        resealed = {**publication, "input_manifest_sha256": "d" * 64}
        assert append_level_publications(conn, [resealed]) == (0, 1)
        alias_count = conn.execute(
            "SELECT COUNT(*) FROM level_publication_source_aliases"
        ).fetchone()[0]
        assert alias_count == 2
        assert not verify_level_chain(conn)
        try:
            append_level_publications(conn, [{**publication, "band_high": 96.0}])
        except RuntimeError:
            pass
        else:
            raise AssertionError("Level publication drift was not rejected")
        normalized, preserved = preserve_first_write_levels(
            conn, [{**publication, "band_high": 96.0, "level_status": "inactive"}]
        )
        assert preserved == 1
        assert normalized[0]["band_high"] == 95.0
        assert normalized[0]["level_status"] == "active"
        assert append_level_publications(conn, normalized) == (0, 1)
        published = conn.execute(
            "SELECT level_id,row_sha256 FROM level_publication_ledger WHERE ticker='TEST'"
        ).fetchone()
        assert published is not None
        resolution = {
            "publication_row_sha256": str(published["row_sha256"]),
            "level_id": str(published["level_id"]), "ticker": "TEST",
            "published_as_of": "2026-07-01", "band_type": "starter",
            "resolved_through": "2026-12-16", "first_touch_date": "2026-07-10",
            "trading_days_to_touch": 7, "touched_flag": 1,
            "maximum_favorable_excursion": 0.08, "maximum_adverse_excursion": -0.02,
            "resolution_available_at_utc": "2026-12-16T22:00:00+00:00",
        }
        assert append_level_resolutions(conn, [resolution]) == (1, 0)
        assert append_level_resolutions(conn, [resolution]) == (0, 1)
        assert not verify_level_resolution_chain(conn)
        for statement in (
            "UPDATE level_publication_ledger SET band_high=99 WHERE ticker='TEST'",
            "DELETE FROM level_resolution_ledger",
        ):
            try:
                conn.execute(statement)
            except sqlite3.DatabaseError:
                conn.rollback()
            else:
                raise AssertionError("Append-only level ledger mutation was not rejected")
        conn.close()
    sample = pd.DataFrame(
        [
            {"date": "2026-07-02", "open": 99, "high": 101, "low": 98, "close": 100, "split_factor": 1},
            {"date": "2026-07-03", "open": 50, "high": 52, "low": 49, "close": 51, "split_factor": 2},
        ]
    )
    converted = _publication_basis_window(sample, horizon=2)
    assert converted["publication_basis_close"].tolist() == [100.0, 102.0]
    assert _touch_fill_price(
        band_type="starter", band_low=90, band_high=95, opening_price=93
    ) == 93
    assert _touch_fill_price(
        band_type="trim", band_low=110, band_high=115, opening_price=112
    ) == 112
    print("level outcome ledger selftest: PASS")


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
    levels_cfg = cfg_get(config, "levels", {})
    if not isinstance(levels_cfg, dict):
        raise ValueError("levels config must be a mapping")
    input_dir = args.input_dir or paths.output_dir / "runs" / args.as_of.isoformat() / "levels"
    levels_path = input_dir / "levels.csv"
    manifest_path = input_dir / "levels_manifest.json"
    manifest = read_manifest(manifest_path)
    if (
        manifest.get("acceptance") not in {"PASS", "PASS_WITH_DEFERRED"}
        or manifest.get("as_of_date") != args.as_of.isoformat()
    ):
        raise ValueError("Validated same-date levels are required")
    if dict(manifest.get("outputs_sha256", {})).get(levels_path.name) != sha256_file(levels_path):
        raise ValueError("Levels CSV hash mismatch")
    rows = read_csv(levels_path)
    market_dir = (
        args.market_data_dir
        or paths.output_dir
        / "runs"
        / args.as_of.isoformat()
        / monitor_output_subdir(config)
        / "market_data"
    )
    market_manifest_path = market_dir / "monitor_ohlcv_manifest.json"
    market_validation_path = market_dir / "monitor_ohlcv_validation_manifest.json"
    market_manifest = read_manifest(market_manifest_path)
    market_validation = read_manifest(market_validation_path)
    selected_path = market_dir / SELECTED_OHLCV_FILENAME
    if (
        market_manifest.get("acceptance") not in {"PASS", "PASS_WITH_WARNINGS"}
        or market_validation.get("acceptance") not in {"PASS", "PASS_WITH_WARNINGS"}
        or market_manifest.get("as_of_date") != args.as_of.isoformat()
        or market_validation.get("as_of_date") != args.as_of.isoformat()
        or market_validation.get("producer_manifest_sha256")
        != sha256_file(market_manifest_path)
        or dict(market_manifest.get("outputs_sha256", {})).get(selected_path.name)
        != sha256_file(selected_path)
    ):
        raise ValueError("Validated same-date monitor OHLCV is required for level outcomes")
    ohlcv = pd.DataFrame(read_gzip_csv(selected_path))
    ohlcv["date"] = pd.to_datetime(ohlcv["date"], errors="raise")
    config_sha = sha256_file(config_path)
    manifest_sha = sha256_file(manifest_path)
    code_sha = sha256_file(Path(__file__).with_name("levels_common.py"))
    published_at = utc_now()
    publications: list[dict[str, Any]] = []
    for row in rows:
        market = json.loads(row["market_structure_json"])
        freshness = json.loads(row["data_freshness_json"])
        latest_price = (
            optional_float(market.get("latest_price"))
            if freshness.get("market_data_status") == "current"
            else None
        )
        for band_type, low_field, high_field in (
            ("starter", "starter_band_low", "starter_band_high"),
            ("add", "add_band_low", "add_band_high"),
            ("trim", "trim_band_low", "trim_band_high"),
        ):
            low = row[low_field].strip()
            high = row[high_field].strip()
            publications.append(
                {
                    "published_as_of": args.as_of.isoformat(),
                    "published_at_utc": published_at,
                    "ticker": row["ticker"],
                    "band_type": band_type,
                    "band_low": None if not low else float(low),
                    "band_high": None if not high else float(high),
                    "level_status": row["level_status"],
                    "inactive_reason": row["inactive_reason"],
                    "market_price_at_publish": latest_price,
                    "model_version": LEVELS_MODEL_VERSION,
                    "config_sha256": config_sha,
                    "input_manifest_sha256": manifest_sha,
                    "code_sha256": code_sha,
                }
            )
    db_path = ensure_not_prod_path(
        resolve_path(levels_cfg.get("database_path", "db/levels.sqlite"), base_dir=config_path.parent),
        label="levels database",
    )
    conn = connect_levels_db(db_path, timeout_sec=float(levels_cfg.get("writer_lock_timeout_sec", 30.0)))
    try:
        publications, first_write_drifts_preserved = preserve_first_write_levels(
            conn, publications
        )
        inserted, duplicates = append_level_publications(conn, publications)
        horizon = int(dict(levels_cfg.get("outcomes", {})).get("horizon_trading_days", 120))
        unresolved = conn.execute(
            """
            SELECT publication.* FROM level_publication_ledger publication
            LEFT JOIN level_resolution_ledger resolution
              ON resolution.publication_row_sha256=publication.row_sha256
            WHERE resolution.row_sha256 IS NULL AND publication.level_status='active'
              AND publication.band_low IS NOT NULL AND publication.band_high IS NOT NULL
              AND publication.model_version=?
            ORDER BY publication.row_sequence
            """,
            (LEVELS_MODEL_VERSION,),
        ).fetchall()
        resolutions: list[dict[str, Any]] = []
        missing_ticker_count = 0
        insufficient_history_count = 0
        for publication in unresolved:
            ticker = str(publication["ticker"])
            frame = ohlcv.loc[ohlcv["ticker"] == ticker].sort_values("date")
            frame = frame.loc[frame["date"] > pd.Timestamp(str(publication["published_as_of"]))]
            if frame.empty:
                missing_ticker_count += 1
                continue
            window = _publication_basis_window(frame, horizon=horizon)
            if window.empty:
                insufficient_history_count += 1
                continue
            low_band = float(publication["band_low"])
            high_band = float(publication["band_high"])
            low_series = numeric_series(window["publication_basis_low"])
            high_series = numeric_series(window["publication_basis_high"])
            touched = window.loc[(low_series <= high_band) & (high_series >= low_band)]
            first_touch_date = ""
            days_to_touch: int | None = None
            maximum_favorable_excursion = 0.0
            maximum_adverse_excursion = 0.0
            if not touched.empty:
                touch_index = int(touched.index[0])
                touch_row = window.iloc[touch_index]
                first_touch_date = str(pd.Timestamp(touch_row["date"]).date())
                days_to_touch = touch_index + 1
                reference = _touch_fill_price(
                    band_type=str(publication["band_type"]),
                    band_low=low_band,
                    band_high=high_band,
                    opening_price=float(touch_row["publication_basis_open"]),
                )
                post_touch = window.iloc[touch_index:]
                high_path = (
                    numeric_series(post_touch["publication_basis_high"])
                    / reference
                    - 1.0
                )
                low_path = (
                    numeric_series(post_touch["publication_basis_low"])
                    / reference
                    - 1.0
                )
                maximum_favorable_excursion = float(high_path.max())
                maximum_adverse_excursion = float(low_path.min())
            resolutions.append(
                {
                    "publication_row_sha256": str(publication["row_sha256"]),
                    "level_id": str(publication["level_id"]),
                    "ticker": ticker,
                    "published_as_of": str(publication["published_as_of"]),
                    "band_type": str(publication["band_type"]),
                    "resolved_through": str(pd.Timestamp(window.iloc[-1]["date"]).date()),
                    "first_touch_date": first_touch_date,
                    "trading_days_to_touch": days_to_touch,
                    "touched_flag": int(not touched.empty),
                    "maximum_favorable_excursion": maximum_favorable_excursion,
                    "maximum_adverse_excursion": maximum_adverse_excursion,
                    "resolution_available_at_utc": utc_now(),
                }
            )
        inserted_resolutions, duplicate_resolutions = append_level_resolutions(conn, resolutions)
        errors = verify_level_chain(conn)
        resolution_errors = verify_level_resolution_chain(conn)
        ledger_rows = [dict(row) for row in conn.execute("SELECT * FROM level_publication_ledger ORDER BY row_sequence").fetchall()]
        source_alias_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM level_publication_source_aliases
                ORDER BY publication_row_sha256,config_sha256,
                         input_manifest_sha256,code_sha256
                """
            ).fetchall()
        ]
        resolution_rows = [dict(row) for row in conn.execute("SELECT * FROM level_resolution_ledger ORDER BY row_sequence").fetchall()]
    finally:
        conn.close()
    output_dir = paths.output_dir / "levels" / "outcomes"
    ledger_path = output_dir / "level_publication_ledger.csv"
    source_alias_path = output_dir / "level_publication_source_aliases.csv"
    resolution_path = output_dir / "level_resolution_ledger.csv"
    ledger_manifest_path = output_dir / "level_outcome_ledger_manifest.json"
    write_csv(ledger_path, LEDGER_FIELDS, ledger_rows)
    write_csv(source_alias_path, SOURCE_ALIAS_FIELDS, source_alias_rows)
    write_csv(resolution_path, RESOLUTION_FIELDS, resolution_rows)
    deferred = bool(missing_ticker_count or insufficient_history_count)
    acceptance = (
        "FAIL"
        if errors or resolution_errors
        else "PASS_WITH_DEFERRED"
        if deferred
        else "PASS"
    )
    write_manifest(
        ledger_manifest_path,
        {
            "schema_version": "level_outcome_ledger_manifest_v2",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "inserted": inserted,
            "idempotent_duplicates": duplicates,
            "first_write_level_drifts_preserved": first_write_drifts_preserved,
            "inserted_resolutions": inserted_resolutions,
            "idempotent_resolutions": duplicate_resolutions,
            "resolution_model_version": LEVELS_MODEL_VERSION,
            "resolution_price_basis": "raw_unadjusted_nominal_on_publication_share_basis",
            "resolution_excursion_window": "first_touch_through_horizon",
            "unresolved_current_model_rows": len(unresolved),
            "missing_ticker_rows": missing_ticker_count,
            "insufficient_history_rows": insufficient_history_count,
            "chain_errors": errors,
            "resolution_chain_errors": resolution_errors,
            "inputs_sha256": {
                str(config_path): config_sha,
                str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
                str(Path(__file__).with_name("levels_common.py")): code_sha,
                str(manifest_path): manifest_sha,
                str(market_manifest_path): sha256_file(market_manifest_path),
                str(market_validation_path): sha256_file(market_validation_path),
            },
            "outputs_sha256": {
                ledger_path.name: sha256_file(ledger_path),
                source_alias_path.name: sha256_file(source_alias_path),
                resolution_path.name: sha256_file(resolution_path),
            },
        },
    )
    print(f"LEVEL OUTCOME LEDGER: {acceptance}")
    print(
        f"rows={len(ledger_rows)}; inserted={inserted}; "
        f"first_write_drifts_preserved={first_write_drifts_preserved}; "
        f"manifest={ledger_manifest_path}"
    )
    return 1 if errors or resolution_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
