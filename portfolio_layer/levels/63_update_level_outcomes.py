#!/usr/bin/env python3
"""Append all active and inactive level publications to the immutable evidence ledger."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from contextlib import ExitStack, closing
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
    LEVEL_RESOLUTION_VERSION,
    LEVELS_MODEL_VERSION,
    append_level_publications,
    append_level_retirements,
    append_level_resolutions,
    connect_levels_db,
    numeric_series,
    optional_float,
    utc_now,
    verify_level_chain,
    verify_level_retirement_chain,
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
    "resolution_schema_version", "resolution_status",
    "first_executable_fill_date", "entry_price_assumption",
    "forward_returns_by_horizon", "spread_and_cost_assumptions",
    "expectations_state_changes", "event_occurrences",
    "resolution_available_at_utc",
]
RETIREMENT_FIELDS = [
    "row_sequence", "previous_row_sha256", "row_sha256",
    "publication_row_sha256", "level_id", "ticker", "published_as_of",
    "band_type", "retired_through", "last_market_date",
    "retirement_reason", "retirement_available_at_utc",
]


def _connect_monitor_db_read_only(
    path: Path, *, timeout_sec: float
) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Expectations monitor database is missing: {resolved}")
    conn = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=timeout_sec,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)}")
    return conn


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


def _forward_returns(
    window: pd.DataFrame,
    *,
    touch_index: int,
    reference: float,
    horizons: list[int],
) -> dict[str, float]:
    returns: dict[str, float] = {}
    closes = numeric_series(window["publication_basis_close"])
    for horizon in horizons:
        index = touch_index + horizon - 1
        if index < len(closes):
            returns[str(horizon)] = float(closes.iloc[index] / reference - 1.0)
    return returns


def _monitor_evidence(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    start_exclusive: str,
    end_inclusive: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    transitions = [
        {
            "run_as_of": str(row["run_as_of"]),
            "from_state": str(row["from_state"]),
            "to_state": str(row["to_state"]),
            "trigger": str(row["trigger"]),
            "rule_id": str(row["rule_id"]),
        }
        for row in conn.execute(
            "SELECT run_as_of,from_state,to_state,trigger,rule_id "
            "FROM state_transitions WHERE ticker=? AND run_as_of>? "
            "AND run_as_of<=? ORDER BY run_as_of,transition_id",
            (ticker, start_exclusive, end_inclusive),
        ).fetchall()
    ]
    events = [
        {
            "event_id": str(row["event_id"]),
            "event_date": str(row["event_date"]),
            "event_type": str(row["event_type"]),
            "direction": float(row["direction"]),
            "severity": float(row["severity"]),
            "credibility": float(row["credibility"]),
            "review_status": str(row["review_status"]),
        }
        for row in conn.execute(
            "SELECT event_id,event_date,event_type,direction,severity,credibility,"
            "review_status FROM events WHERE ticker=? AND event_date>? "
            "AND event_date<=? ORDER BY event_date,event_id",
            (ticker, start_exclusive, end_inclusive),
        ).fetchall()
    ]
    return transitions, events


def _outcome_acceptance(
    *,
    chain_errors: list[str],
    resolution_errors: list[str],
    retirement_errors: list[str],
    first_write_drifts: int,
    deferred: bool,
    preserve_drifts_as_deferred: bool = False,
) -> str:
    if chain_errors or resolution_errors or retirement_errors:
        return "FAIL"
    if first_write_drifts and not preserve_drifts_as_deferred:
        return "FAIL"
    deferred = deferred or bool(first_write_drifts)
    return "PASS_WITH_DEFERRED" if deferred else "PASS"


def preserve_first_write_levels(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int, int]:
    """Normalize same-date reruns and distinguish drift from a new-model restatement."""
    normalized: list[dict[str, Any]] = []
    same_model_drift_count = 0
    cross_model_restatement_count = 0
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
            if changed:
                if row.get("model_version") == existing["model_version"]:
                    same_model_drift_count += 1
                else:
                    cross_model_restatement_count += 1
            for field in immutable_fields:
                row[field] = existing[field]
        normalized.append(row)
    return normalized, same_model_drift_count, cross_model_restatement_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--market-data-dir", type=Path)
    parser.add_argument(
        "--preserve-first-write-drifts-as-deferred",
        action="store_true",
        help=(
            "Late-broker supplement only: preserve immutable prior publications and "
            "report same-model recomputation differences as deferred, never as replacements."
        ),
    )
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
        normalized, preserved, cross_model = preserve_first_write_levels(
            conn, [{**publication, "band_high": 96.0, "level_status": "inactive"}]
        )
        assert preserved == 1
        assert cross_model == 0
        assert normalized[0]["band_high"] == 95.0
        assert normalized[0]["level_status"] == "active"
        assert append_level_publications(conn, normalized) == (0, 1)
        normalized, preserved, cross_model = preserve_first_write_levels(
            conn,
            [
                {
                    **publication,
                    "band_high": 97.0,
                    "model_version": "advisory_long_levels_v_next",
                }
            ],
        )
        assert preserved == 0
        assert cross_model == 1
        assert normalized[0]["band_high"] == 95.0
        assert normalized[0]["model_version"] == LEVELS_MODEL_VERSION
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
            "resolution_schema_version": LEVEL_RESOLUTION_VERSION,
            "resolution_status": "touched",
            "first_executable_fill_date": "2026-07-10",
            "entry_price_assumption": '{"price":95.0}',
            "forward_returns_by_horizon": '{"1":0.01}',
            "spread_and_cost_assumptions": '{"commission_per_trade_usd":1.25}',
            "expectations_state_changes": "[]",
            "event_occurrences": "[]",
            "resolution_available_at_utc": "2026-12-16T22:00:00+00:00",
        }
        assert append_level_resolutions(conn, [resolution]) == (1, 0)
        assert append_level_resolutions(conn, [resolution]) == (0, 1)
        assert not verify_level_resolution_chain(conn)
        retirement = {
            "publication_row_sha256": "d" * 64,
            "level_id": "retired-level",
            "ticker": "OLD",
            "published_as_of": "2025-01-01",
            "band_type": "starter",
            "retired_through": "2026-01-01",
            "last_market_date": "2025-02-01",
            "retirement_reason": "insufficient_market_history_after_expiry",
            "retirement_available_at_utc": "2026-01-01T22:00:00+00:00",
        }
        assert append_level_retirements(conn, [retirement]) == (1, 0)
        rerun_retirement = {
            **retirement,
            "retirement_available_at_utc": "2026-01-02T22:00:00+00:00",
        }
        assert append_level_retirements(conn, [rerun_retirement]) == (0, 1)
        assert not verify_level_retirement_chain(conn)
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
    converted = _publication_basis_window(sample, horizon=2)
    assert _forward_returns(
        converted,
        touch_index=0,
        reference=100.0,
        horizons=[1, 2],
    ) == {"1": 0.0, "2": 0.020000000000000018}
    assert _outcome_acceptance(
        chain_errors=[],
        resolution_errors=[],
        retirement_errors=[],
        first_write_drifts=1,
        deferred=False,
    ) == "FAIL"
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
    outcomes_cfg_raw = levels_cfg.get("outcomes", {})
    if not isinstance(outcomes_cfg_raw, dict):
        raise ValueError("levels.outcomes must be a mapping")
    outcomes_cfg = dict(outcomes_cfg_raw)
    horizon = int(outcomes_cfg.get("horizon_trading_days", 120))
    horizons_raw = outcomes_cfg.get(
        "forward_return_horizons", [1, 5, 20, 60, horizon]
    )
    if not isinstance(horizons_raw, list):
        raise ValueError("levels.outcomes.forward_return_horizons must be a list")
    forward_horizons = sorted({int(value) for value in horizons_raw})
    if not forward_horizons or forward_horizons[0] < 1:
        raise ValueError("Forward-return horizons must be positive")
    expiry_calendar_days = int(
        outcomes_cfg.get("resolution_expiry_calendar_days", 210)
    )
    minimum_expiry = (
        math.ceil((horizon + max(forward_horizons)) * 365.25 / 252) + 14
    )
    if expiry_calendar_days < minimum_expiry:
        raise ValueError(
            "resolution_expiry_calendar_days is too short for the touch horizon"
        )
    cost_assumptions_raw = outcomes_cfg.get("cost_assumptions", {})
    if not isinstance(cost_assumptions_raw, dict):
        raise ValueError("levels.outcomes.cost_assumptions must be a mapping")
    cost_assumptions = dict(cost_assumptions_raw)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    monitor_db_path = ensure_not_prod_path(
        resolve_path(
            monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="expectations monitor database",
    )
    monitor_timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    levels_timeout = float(levels_cfg.get("writer_lock_timeout_sec", 30.0))
    with ExitStack() as stack:
        monitor_conn = stack.enter_context(
            closing(
                _connect_monitor_db_read_only(
                    monitor_db_path, timeout_sec=monitor_timeout
                )
            )
        )
        conn = stack.enter_context(
            closing(connect_levels_db(db_path, timeout_sec=levels_timeout))
        )
        (
            publications,
            first_write_drifts_preserved,
            cross_model_restatements_skipped,
        ) = preserve_first_write_levels(conn, publications)
        inserted, duplicates = append_level_publications(conn, publications)
        unresolved = conn.execute(
            """
            SELECT publication.* FROM level_publication_ledger publication
            LEFT JOIN level_resolution_ledger resolution
              ON resolution.publication_row_sha256=publication.row_sha256
            LEFT JOIN level_retirement_ledger retirement
              ON retirement.publication_row_sha256=publication.row_sha256
            WHERE resolution.row_sha256 IS NULL AND publication.level_status='active'
              AND retirement.row_sha256 IS NULL
              AND publication.band_low IS NOT NULL AND publication.band_high IS NOT NULL
              AND publication.model_version=?
            ORDER BY publication.row_sequence
            """,
            (LEVELS_MODEL_VERSION,),
        ).fetchall()
        resolutions: list[dict[str, Any]] = []
        retirements: list[dict[str, Any]] = []
        missing_ticker_count = 0
        insufficient_history_count = 0
        for publication in unresolved:
            ticker = str(publication["ticker"])
            published_as_of = str(publication["published_as_of"])
            age_calendar_days = (
                args.as_of - date.fromisoformat(published_as_of)
            ).days
            frame = ohlcv.loc[ohlcv["ticker"] == ticker].sort_values("date")
            frame = frame.loc[frame["date"] > pd.Timestamp(published_as_of)]
            if frame.empty:
                if age_calendar_days >= expiry_calendar_days:
                    retirements.append(
                        {
                            "publication_row_sha256": str(publication["row_sha256"]),
                            "level_id": str(publication["level_id"]),
                            "ticker": ticker,
                            "published_as_of": published_as_of,
                            "band_type": str(publication["band_type"]),
                            "retired_through": args.as_of.isoformat(),
                            "last_market_date": "",
                            "retirement_reason": "no_future_market_data_after_expiry",
                            "retirement_available_at_utc": utc_now(),
                        }
                    )
                else:
                    missing_ticker_count += 1
                continue
            touch_window = _publication_basis_window(frame, horizon=horizon)
            if touch_window.empty:
                if age_calendar_days >= expiry_calendar_days:
                    retirements.append(
                        {
                            "publication_row_sha256": str(publication["row_sha256"]),
                            "level_id": str(publication["level_id"]),
                            "ticker": ticker,
                            "published_as_of": published_as_of,
                            "band_type": str(publication["band_type"]),
                            "retired_through": args.as_of.isoformat(),
                            "last_market_date": str(
                                pd.Timestamp(frame.iloc[-1]["date"]).date()
                            ),
                            "retirement_reason": (
                                "insufficient_market_history_after_expiry"
                            ),
                            "retirement_available_at_utc": utc_now(),
                        }
                    )
                else:
                    insufficient_history_count += 1
                continue
            low_band = float(publication["band_low"])
            high_band = float(publication["band_high"])
            low_series = numeric_series(touch_window["publication_basis_low"])
            high_series = numeric_series(touch_window["publication_basis_high"])
            touched = touch_window.loc[
                (low_series <= high_band) & (high_series >= low_band)
            ]
            first_touch_date = ""
            days_to_touch: int | None = None
            maximum_favorable_excursion = 0.0
            maximum_adverse_excursion = 0.0
            entry_price_assumption: dict[str, Any] = {}
            forward_returns: dict[str, float] = {}
            resolution_status = "no_touch"
            window = touch_window
            if not touched.empty:
                touch_index = int(touched.index[0])
                required_rows = touch_index + max(forward_horizons)
                window = _publication_basis_window(frame, horizon=required_rows)
                if window.empty:
                    if age_calendar_days >= expiry_calendar_days:
                        retirements.append(
                            {
                                "publication_row_sha256": str(
                                    publication["row_sha256"]
                                ),
                                "level_id": str(publication["level_id"]),
                                "ticker": ticker,
                                "published_as_of": published_as_of,
                                "band_type": str(publication["band_type"]),
                                "retired_through": args.as_of.isoformat(),
                                "last_market_date": str(
                                    pd.Timestamp(frame.iloc[-1]["date"]).date()
                                ),
                                "retirement_reason": (
                                    "touched_but_forward_history_incomplete_after_expiry"
                                ),
                                "retirement_available_at_utc": utc_now(),
                            }
                        )
                    else:
                        insufficient_history_count += 1
                    continue
                touch_row = window.iloc[touch_index]
                first_touch_date = str(pd.Timestamp(touch_row["date"]).date())
                days_to_touch = touch_index + 1
                reference = _touch_fill_price(
                    band_type=str(publication["band_type"]),
                    band_low=low_band,
                    band_high=high_band,
                    opening_price=float(touch_row["publication_basis_open"]),
                )
                resolution_status = "touched"
                entry_price_assumption = {
                    "fill_price": reference,
                    "fill_rule": "band_cross_with_same_day_open_price_improvement",
                    "opening_price": float(
                        touch_row["publication_basis_open"]
                    ),
                    "band_low": low_band,
                    "band_high": high_band,
                    "price_basis": (
                        "raw_unadjusted_nominal_on_publication_share_basis"
                    ),
                }
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
                forward_returns = _forward_returns(
                    window,
                    touch_index=touch_index,
                    reference=reference,
                    horizons=forward_horizons,
                )
            resolved_through = str(pd.Timestamp(window.iloc[-1]["date"]).date())
            transitions, event_occurrences = _monitor_evidence(
                monitor_conn,
                ticker=ticker,
                start_exclusive=published_as_of,
                end_inclusive=resolved_through,
            )
            resolutions.append(
                {
                    "publication_row_sha256": str(publication["row_sha256"]),
                    "level_id": str(publication["level_id"]),
                    "ticker": ticker,
                    "published_as_of": published_as_of,
                    "band_type": str(publication["band_type"]),
                    "resolved_through": resolved_through,
                    "first_touch_date": first_touch_date,
                    "trading_days_to_touch": days_to_touch,
                    "touched_flag": int(not touched.empty),
                    "maximum_favorable_excursion": maximum_favorable_excursion,
                    "maximum_adverse_excursion": maximum_adverse_excursion,
                    "resolution_schema_version": LEVEL_RESOLUTION_VERSION,
                    "resolution_status": resolution_status,
                    "first_executable_fill_date": first_touch_date,
                    "entry_price_assumption": json.dumps(
                        entry_price_assumption,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "forward_returns_by_horizon": json.dumps(
                        forward_returns, sort_keys=True, separators=(",", ":")
                    ),
                    "spread_and_cost_assumptions": json.dumps(
                        {
                            **cost_assumptions,
                            "returns_are_gross_of_cost": True,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "expectations_state_changes": json.dumps(
                        transitions, sort_keys=True, separators=(",", ":")
                    ),
                    "event_occurrences": json.dumps(
                        event_occurrences,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "resolution_available_at_utc": utc_now(),
                }
            )
        inserted_resolutions, duplicate_resolutions = append_level_resolutions(conn, resolutions)
        inserted_retirements, duplicate_retirements = append_level_retirements(
            conn, retirements
        )
        errors = verify_level_chain(conn)
        resolution_errors = verify_level_resolution_chain(conn)
        retirement_errors = verify_level_retirement_chain(conn)
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
        retirement_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM level_retirement_ledger ORDER BY row_sequence"
            ).fetchall()
        ]
    output_dir = paths.output_dir / "levels" / "outcomes"
    ledger_path = output_dir / "level_publication_ledger.csv"
    source_alias_path = output_dir / "level_publication_source_aliases.csv"
    resolution_path = output_dir / "level_resolution_ledger.csv"
    retirement_path = output_dir / "level_retirement_ledger.csv"
    ledger_manifest_path = output_dir / "level_outcome_ledger_manifest.json"
    write_csv(ledger_path, LEDGER_FIELDS, ledger_rows)
    write_csv(source_alias_path, SOURCE_ALIAS_FIELDS, source_alias_rows)
    write_csv(resolution_path, RESOLUTION_FIELDS, resolution_rows)
    write_csv(retirement_path, RETIREMENT_FIELDS, retirement_rows)
    deferred = bool(
        missing_ticker_count
        or insufficient_history_count
        or cross_model_restatements_skipped
    )
    acceptance = _outcome_acceptance(
        chain_errors=errors,
        resolution_errors=resolution_errors,
        retirement_errors=retirement_errors,
        first_write_drifts=first_write_drifts_preserved,
        deferred=deferred,
        preserve_drifts_as_deferred=args.preserve_first_write_drifts_as_deferred,
    )
    write_manifest(
        ledger_manifest_path,
        {
            "schema_version": "level_outcome_ledger_manifest_v3",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "inserted": inserted,
            "idempotent_duplicates": duplicates,
            "first_write_level_drifts_preserved": first_write_drifts_preserved,
            "preserve_first_write_drifts_as_deferred": bool(
                args.preserve_first_write_drifts_as_deferred
            ),
            "cross_model_restatements_skipped": cross_model_restatements_skipped,
            "cross_model_restatement_policy": (
                "preserve_first_write_and_defer_new_model_until_next_unpublished_as_of"
            ),
            "inserted_resolutions": inserted_resolutions,
            "idempotent_resolutions": duplicate_resolutions,
            "inserted_retirements": inserted_retirements,
            "idempotent_retirements": duplicate_retirements,
            "resolution_model_version": LEVELS_MODEL_VERSION,
            "resolution_schema_version": LEVEL_RESOLUTION_VERSION,
            "resolution_price_basis": "raw_unadjusted_nominal_on_publication_share_basis",
            "resolution_excursion_window": "first_touch_through_max_forward_horizon",
            "touch_window_trading_days": horizon,
            "forward_return_horizons": forward_horizons,
            "resolution_expiry_calendar_days": expiry_calendar_days,
            "unresolved_current_model_rows": len(unresolved),
            "missing_ticker_rows": missing_ticker_count,
            "insufficient_history_rows": insufficient_history_count,
            "chain_errors": errors,
            "resolution_chain_errors": resolution_errors,
            "retirement_chain_errors": retirement_errors,
            "monitor_evidence_database_path": str(monitor_db_path),
            "monitor_evidence_database_mode": "read_only",
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
                retirement_path.name: sha256_file(retirement_path),
            },
        },
    )
    print(f"LEVEL OUTCOME LEDGER: {acceptance}")
    print(
        f"rows={len(ledger_rows)}; inserted={inserted}; "
        f"first_write_drifts_preserved={first_write_drifts_preserved}; "
        f"cross_model_restatements_skipped={cross_model_restatements_skipped}; "
        f"manifest={ledger_manifest_path}"
    )
    return 1 if acceptance == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
