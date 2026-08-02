#!/usr/bin/env python3
"""Append state publications and matured outcomes to tamper-evident ledgers."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import read_csv, read_manifest, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    connect_monitor_db,
    database_writer_lock,
    monitor_output_subdir,
)
from portfolio_layer.expectations_monitor.state_common import (  # noqa: E402
    append_state_outcome_rows,
    append_state_resolution_rows,
    ensure_state_schema,
    utc_now,
    verify_state_outcome_chain,
    verify_state_resolution_chain,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PUBLICATION_FIELDS = [
    "row_sequence", "previous_row_sha256", "row_sha256", "ticker", "published_as_of",
    "published_at_utc", "action_state", "internal_state", "les_total",
    "market_price_at_publish", "source_manifest_sha256", "resolution_json",
    "resolution_available_at_utc",
]


def _numeric_series(values: Any) -> pd.Series:
    return pd.Series(
        pd.to_numeric(values, errors="coerce"),
        index=getattr(values, "index", None),
        dtype=float,
    )
RESOLUTION_FIELDS = [
    "row_sequence", "previous_row_sha256", "row_sha256", "publication_row_sha256", "ticker",
    "published_as_of", "resolved_through", "forward_returns_json", "sector_excess_returns_json",
    "maximum_favorable_excursion", "maximum_adverse_excursion", "state_changes_json",
    "event_occurrences_json", "resolution_available_at_utc",
]
SOURCE_ALIAS_FIELDS = [
    "publication_row_sha256", "source_manifest_sha256", "recorded_at_utc",
]


def preserve_first_write_publications(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Normalize reruns to immutable same-date publications before append."""
    normalized: list[dict[str, Any]] = []
    drift_count = 0
    immutable_fields = (
        "action_state",
        "internal_state",
        "les_total",
        "market_price_at_publish",
    )
    for raw in rows:
        row = dict(raw)
        existing = conn.execute(
            """
            SELECT action_state,internal_state,les_total,market_price_at_publish
            FROM monitor_state_outcome_ledger
            WHERE ticker=? AND published_as_of=?
            """,
            (row["ticker"], row["published_as_of"]),
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
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def run_selftest() -> None:
    prices = pd.Series([100.0, 102.0, 99.0, 104.0])
    returns = prices / prices.iloc[0] - 1.0
    assert round(float(returns.max()), 6) == 0.04
    assert round(float(returns.min()), 6) == -0.01
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_state_schema(conn)
    publication = {
        "ticker": "TEST", "published_as_of": "2026-07-01",
        "published_at_utc": "2026-07-01T22:00:00+00:00",
        "action_state": "watch", "internal_state": "stable", "les_total": 2.0,
        "market_price_at_publish": 100.0, "source_manifest_sha256": "a" * 64,
    }
    assert append_state_outcome_rows(conn, [publication]) == (1, 0)
    assert append_state_outcome_rows(conn, [publication]) == (0, 1)
    resealed = {**publication, "source_manifest_sha256": "b" * 64}
    assert append_state_outcome_rows(conn, [resealed]) == (0, 1)
    assert conn.execute(
        "SELECT COUNT(*) FROM monitor_state_publication_source_aliases"
    ).fetchone()[0] == 2
    assert not verify_state_outcome_chain(conn)
    try:
        append_state_outcome_rows(conn, [{**publication, "les_total": 3.0}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("Publication drift was not rejected")
    normalized, preserved = preserve_first_write_publications(
        conn, [{**publication, "les_total": 3.0, "action_state": "hold"}]
    )
    assert preserved == 1
    assert normalized[0]["les_total"] == 2.0
    assert normalized[0]["action_state"] == "watch"
    assert append_state_outcome_rows(conn, normalized) == (0, 1)
    published = conn.execute(
        "SELECT row_sha256 FROM monitor_state_outcome_ledger WHERE ticker='TEST'"
    ).fetchone()
    assert published is not None
    resolution = {
        "publication_row_sha256": str(published["row_sha256"]),
        "ticker": "TEST", "published_as_of": "2026-07-01", "resolved_through": "2026-07-08",
        "forward_returns_json": json.dumps({"5": 0.02}, separators=(",", ":")),
        "sector_excess_returns_json": json.dumps({"5": 0.01}, separators=(",", ":")),
        "maximum_favorable_excursion": 0.03, "maximum_adverse_excursion": -0.01,
        "state_changes_json": "[]", "event_occurrences_json": "[]",
        "resolution_available_at_utc": "2026-07-08T22:00:00+00:00",
    }
    assert append_state_resolution_rows(conn, [resolution]) == (1, 0)
    assert append_state_resolution_rows(conn, [resolution]) == (0, 1)
    assert not verify_state_resolution_chain(conn)
    for statement in (
        "UPDATE monitor_state_outcome_ledger SET les_total=9 WHERE ticker='TEST'",
        "DELETE FROM monitor_state_resolution_ledger",
    ):
        try:
            conn.execute(statement)
        except sqlite3.DatabaseError:
            conn.rollback()
        else:
            raise AssertionError("Append-only ledger mutation was not rejected")
    conn.close()
    print("monitor outcome ledger selftest: PASS")


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
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    input_dir = (
        args.input_dir
        or paths.output_dir
        / "runs"
        / args.as_of.isoformat()
        / monitor_output_subdir(config)
    )
    state_path = input_dir / "expectations_state.csv"
    state_manifest_path = input_dir / "expectations_state_manifest.json"
    validation_manifest_path = input_dir / "validation" / "expectations_state_validation_manifest.json"
    state_manifest = read_manifest(state_manifest_path)
    validation_manifest = read_manifest(validation_manifest_path)
    if state_manifest.get("acceptance") != "PASS" or validation_manifest.get("acceptance") != "PASS":
        raise ValueError("Validated expectations state is required before ledger append")
    if state_manifest.get("as_of_date") != args.as_of.isoformat() or validation_manifest.get("as_of_date") != args.as_of.isoformat():
        raise ValueError("State/validation date mismatch")
    if dict(state_manifest.get("outputs_sha256", {})).get(state_path.name) != sha256_file(state_path):
        raise ValueError("State CSV hash mismatch")
    state_rows = read_csv(state_path)
    db_path = ensure_not_prod_path(
        resolve_path(monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"), base_dir=config_path.parent),
        label="expectations monitor database",
    )
    timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    state_manifest_sha = sha256_file(state_manifest_path)
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        ensure_state_schema(conn)
        prices_by_ticker: dict[str, float | None] = {}
        for row in conn.execute(
            "SELECT ticker,inputs_json FROM market_signals_daily WHERE asof_date=?",
            (args.as_of.isoformat(),),
        ).fetchall():
            payload = json.loads(str(row["inputs_json"]))
            value = payload.get("latest_adj_close")
            prices_by_ticker[str(row["ticker"])] = None if value is None else float(value)
        publication_enabled = bool(
            monitor_cfg.get("state_publication_enabled", False)
        )
        publications = [
            {
                "ticker": row["ticker"],
                "published_as_of": row["run_as_of"],
                "published_at_utc": row["asof_ts"],
                "action_state": row["action_state"],
                "internal_state": row["internal_state"],
                "les_total": float(row["les_total"]),
                "market_price_at_publish": prices_by_ticker.get(row["ticker"]),
                "source_manifest_sha256": state_manifest_sha,
            }
            for row in state_rows
        ] if publication_enabled else []
        publications, first_write_drifts_preserved = preserve_first_write_publications(
            conn, publications
        )
        with database_writer_lock(db_path, timeout_sec=timeout):
            inserted_publications, duplicate_publications = append_state_outcome_rows(conn, publications)
        risk_dir = paths.output_dir / "runs" / args.as_of.isoformat() / "risk"
        risk_manifest_path = risk_dir / "risk_manifest.json"
        price_path = risk_dir / "prices_adjclose.csv"
        risk_available = risk_manifest_path.is_file() and price_path.is_file()
        if risk_available:
            risk_manifest = read_manifest(risk_manifest_path)
            expected = dict(risk_manifest.get("files", {})).get(
                price_path.name, {}
            ).get("sha256")
            if risk_manifest.get("acceptance") != "PASS" or expected != sha256_file(
                price_path
            ):
                raise ValueError(
                    "Present same-date Stage 2 prices failed integrity validation"
                )
            prices = pd.read_csv(price_path, index_col=0, parse_dates=True)
            resolution_data_status = "same_date_stage2_available"
        else:
            prices = pd.DataFrame()
            resolution_data_status = "deferred_missing_same_date_stage2"
        etf_map = {str(key): str(value).upper() for key, value in dict(cfg_get(config, "risk_panel.sector_etf_map", {})).items()}
        unresolved = conn.execute(
            """
            SELECT publication.*
            FROM monitor_state_outcome_ledger publication
            LEFT JOIN monitor_state_resolution_ledger resolution
              ON resolution.publication_row_sha256=publication.row_sha256
            WHERE resolution.row_sha256 IS NULL
            ORDER BY publication.row_sequence
            """
        ).fetchall()
        resolutions: list[dict[str, Any]] = []
        horizons = (1, 5, 20, 60, 120)
        for publication in unresolved:
            ticker = str(publication["ticker"])
            published = pd.Timestamp(str(publication["published_as_of"]))
            if ticker not in prices.columns:
                continue
            series = _numeric_series(prices.loc[:, ticker]).dropna()
            eligible = series.loc[series.index >= published]
            if len(eligible) <= max(horizons):
                continue
            start_date = eligible.index[0]
            start_price = float(eligible.iloc[0])
            if start_price <= 0:
                continue
            pipeline_row = conn.execute(
                "SELECT source_pipeline FROM monitor_universe WHERE ticker=? AND run_as_of<=? ORDER BY run_as_of DESC LIMIT 1",
                (ticker, str(publication["published_as_of"])),
            ).fetchone()
            benchmark_ticker = etf_map.get(str(pipeline_row["source_pipeline"]) if pipeline_row else "", "SPY")
            if benchmark_ticker not in prices.columns:
                continue
            benchmark = _numeric_series(prices.loc[:, benchmark_ticker]).dropna()
            returns: dict[str, float] = {}
            excess: dict[str, float] = {}
            for horizon in horizons:
                end_date = eligible.index[horizon]
                end_price = float(eligible.iloc[horizon])
                if start_date not in benchmark.index or end_date not in benchmark.index:
                    break
                name_return = end_price / start_price - 1.0
                benchmark_return = float(benchmark.loc[end_date]) / float(benchmark.loc[start_date]) - 1.0
                returns[str(horizon)] = name_return
                excess[str(horizon)] = name_return - benchmark_return
            if len(returns) != len(horizons):
                continue
            window = eligible.iloc[: max(horizons) + 1] / start_price - 1.0
            resolved_date = str(eligible.index[max(horizons)])[:10]
            state_changes = [
                dict(row)
                for row in conn.execute(
                    "SELECT run_as_of,from_state,to_state,trigger FROM state_transitions WHERE ticker=? AND run_as_of>? AND run_as_of<=? ORDER BY run_as_of",
                    (ticker, str(publication["published_as_of"]), resolved_date),
                ).fetchall()
            ]
            event_ids = [
                str(row["event_id"])
                for row in conn.execute(
                    "SELECT event_id FROM events WHERE ticker=? AND event_date>? AND event_date<=? ORDER BY event_date,event_id",
                    (ticker, str(publication["published_as_of"]), resolved_date),
                ).fetchall()
            ]
            resolutions.append(
                {
                    "publication_row_sha256": str(publication["row_sha256"]),
                    "ticker": ticker,
                    "published_as_of": str(publication["published_as_of"]),
                    "resolved_through": resolved_date,
                    "forward_returns_json": json.dumps(returns, sort_keys=True, separators=(",", ":")),
                    "sector_excess_returns_json": json.dumps(excess, sort_keys=True, separators=(",", ":")),
                    "maximum_favorable_excursion": float(window.max()),
                    "maximum_adverse_excursion": float(window.min()),
                    "state_changes_json": json.dumps(state_changes, sort_keys=True, separators=(",", ":")),
                    "event_occurrences_json": json.dumps(event_ids, separators=(",", ":")),
                    "resolution_available_at_utc": utc_now(),
                }
            )
        with database_writer_lock(db_path, timeout_sec=timeout):
            inserted_resolutions, duplicate_resolutions = append_state_resolution_rows(conn, resolutions)
        publication_errors = verify_state_outcome_chain(conn)
        resolution_errors = verify_state_resolution_chain(conn)
        publication_rows = [dict(row) for row in conn.execute("SELECT * FROM monitor_state_outcome_ledger ORDER BY row_sequence").fetchall()]
        resolution_rows = [dict(row) for row in conn.execute("SELECT * FROM monitor_state_resolution_ledger ORDER BY row_sequence").fetchall()]
        source_alias_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM monitor_state_publication_source_aliases "
                "ORDER BY publication_row_sha256,source_manifest_sha256"
            ).fetchall()
        ]
    finally:
        conn.close()
    output_dir = paths.output_dir / monitor_output_subdir(config) / "outcomes"
    output_dir.mkdir(parents=True, exist_ok=True)
    publication_path = output_dir / "state_publication_ledger.csv"
    resolution_path = output_dir / "state_resolution_ledger.csv"
    source_alias_path = output_dir / "state_publication_source_aliases.csv"
    manifest_path = output_dir / "state_outcome_ledger_manifest.json"
    write_csv(publication_path, PUBLICATION_FIELDS, publication_rows)
    write_csv(resolution_path, RESOLUTION_FIELDS, resolution_rows)
    write_csv(source_alias_path, SOURCE_ALIAS_FIELDS, source_alias_rows)
    acceptance = (
        "FAIL"
        if publication_errors or resolution_errors
        else "PASS"
        if risk_available
        else "PASS_WITH_DEFERRED"
    )
    input_hashes = {
        str(config_path): sha256_file(config_path),
        str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
        str(Path(__file__).with_name("state_common.py")): sha256_file(
            Path(__file__).with_name("state_common.py")
        ),
        str(state_manifest_path): state_manifest_sha,
        str(validation_manifest_path): sha256_file(validation_manifest_path),
    }
    if risk_available:
        input_hashes[str(risk_manifest_path)] = sha256_file(risk_manifest_path)
    write_manifest(
        manifest_path,
        {
            "schema_version": "state_outcome_ledger_manifest_v1",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "inserted_publications": inserted_publications,
            "idempotent_publications": duplicate_publications,
            "first_write_publication_drifts_preserved": first_write_drifts_preserved,
            "state_publication_enabled": publication_enabled,
            "publication_status": (
                "enabled" if publication_enabled else "disabled_by_policy"
            ),
            "inserted_resolutions": inserted_resolutions,
            "idempotent_resolutions": duplicate_resolutions,
            "publication_chain_errors": publication_errors,
            "resolution_chain_errors": resolution_errors,
            "resolution_data_status": resolution_data_status,
            "inputs_sha256": input_hashes,
            "outputs_sha256": {
                publication_path.name: sha256_file(publication_path),
                resolution_path.name: sha256_file(resolution_path),
                source_alias_path.name: sha256_file(source_alias_path),
            },
        },
    )
    print(f"MONITOR OUTCOME LEDGER: {acceptance}")
    print(
        f"publications={len(publication_rows)}; resolutions={len(resolution_rows)}; "
        f"first_write_drifts_preserved={first_write_drifts_preserved}; "
        f"manifest={manifest_path}"
    )
    return 1 if acceptance == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
