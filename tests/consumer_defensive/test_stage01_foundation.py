from __future__ import annotations

import ast
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from consumer_defensive.core.config import cfg_get, load_config, resolve_path, validate_config
from consumer_defensive.core.db import (
    FORBIDDEN_TABLE_FRAGMENTS,
    REQUIRED_FOUNDATION_TABLES,
    connect,
    init_db,
    table_names,
)
from consumer_defensive.core.metric_registry import load_metric_registry, upsert_metric_registry
from consumer_defensive.core.source_registry import load_source_registry, upsert_source_registry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "consumer_defensive"
CONFIG_PATH = PACKAGE_ROOT / "config.yaml"


def test_stage0_config_contract_and_history_dates() -> None:
    bundle = load_config(CONFIG_PATH)
    config = bundle.payload
    assert cfg_get(config, "runtime.model_family") == "consumer_defensive"
    assert cfg_get(config, "runtime.internal_sector") == "Consumer Defensive"
    assert cfg_get(config, "runtime.portfolio_sector") == "Consumer Staples"
    assert cfg_get(config, "historical_contract.requested_snapshot_start") == "2019-01-02"
    assert cfg_get(config, "historical_contract.minimum_market_history_start") == "2017-11-28"
    assert cfg_get(config, "historical_contract.market_history_buffer_calendar_days") == 400
    assert cfg_get(config, "historical_contract.trading_calendar_ticker") == "SPY"
    assert cfg_get(config, "historical_contract.sector_benchmark_ticker") == "XLP"
    assert cfg_get(config, "oos_provenance.deep_replay_oos_score_valid_flag") == 0
    assert cfg_get(config, "portfolio_layer.enabled") is False
    assert cfg_get(config, "portfolio_layer.sector_weight_cap") == 0.0


def test_config_rejects_unknown_root_and_cross_sector_identity() -> None:
    bundle = load_config(CONFIG_PATH)
    unknown = dict(bundle.payload)
    unknown["unexpected"] = {}
    with pytest.raises(ValueError, match="Unknown Consumer Defensive config"):
        validate_config(unknown)

    wrong_family = dict(bundle.payload)
    wrong_family["runtime"] = dict(bundle.payload["runtime"])
    wrong_family["runtime"]["model_family"] = "transportation"
    with pytest.raises(ValueError, match="runtime.model_family"):
        validate_config(wrong_family)


def test_database_path_supports_consumer_defensive_env_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = load_config(CONFIG_PATH)
    monkeypatch.setenv("CONSUMER_DEFENSIVE_DB_DIR", str(tmp_path))
    resolved = resolve_path(
        cfg_get(bundle.payload, "paths.database_path"),
        base_dir=bundle.base_dir,
    )
    assert resolved == (tmp_path / "consumer_defensive.sqlite").resolve()


def test_source_and_metric_registries_are_independent_and_zero_weight() -> None:
    bundle = load_config(CONFIG_PATH)
    source_path = resolve_path(
        cfg_get(bundle.payload, "source_registry.path"),
        base_dir=bundle.base_dir,
    )
    metric_path = resolve_path(
        cfg_get(bundle.payload, "specialized_metrics.registry_path"),
        base_dir=bundle.base_dir,
    )
    sources = load_source_registry(source_path)
    version, metrics = load_metric_registry(metric_path)
    assert version == "consumer_defensive_specialized_metrics_v2"
    assert len(sources) >= 10
    assert all(row.subsector_scope == "consumer_defensive" for row in sources)
    assert len(metrics) >= 30
    assert len({metric.metric_id for metric in metrics}) == len(metrics)
    assert all(metric.production_weight == 0.0 for metric in metrics)


def test_stage1_schema_is_idempotent_and_empty_before_universe_load(tmp_path: Path) -> None:
    bundle = load_config(CONFIG_PATH)
    db_path = tmp_path / "consumer_defensive.sqlite"
    source_path = resolve_path(
        cfg_get(bundle.payload, "source_registry.path"),
        base_dir=bundle.base_dir,
    )
    metric_path = resolve_path(
        cfg_get(bundle.payload, "specialized_metrics.registry_path"),
        base_dir=bundle.base_dir,
    )

    with connect(db_path) as conn:
        init_db(conn)
        init_db(conn)
        sources = load_source_registry(source_path)
        source_count = upsert_source_registry(conn, sources)
        version, metrics = load_metric_registry(metric_path)
        metric_count = upsert_metric_registry(
            conn,
            registry_version=version,
            metrics=metrics,
        )
        names = set(table_names(conn))
        assert REQUIRED_FOUNDATION_TABLES.issubset(names)
        assert not {
            name
            for name in names
            if any(fragment in name.casefold() for fragment in FORBIDDEN_TABLE_FRAGMENTS)
        }
        assert source_count == len(sources)
        assert metric_count == len(metrics)
        assert conn.execute("SELECT COUNT(*) FROM source_registry").fetchone()[0] == len(sources)
        assert (
            conn.execute("SELECT COUNT(*) FROM dim_specialized_metric").fetchone()[0]
            == len(metrics)
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM dim_specialized_metric WHERE production_weight <> 0"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM dim_company").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM dim_security").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM dim_universe_membership").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM feature_scoring_model_output").fetchone()[0] == 0


def test_existing_v1_canonical_fact_key_is_migrated() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        conn.execute("DROP TABLE fact_financial_statement_canonical")
        conn.execute(
            """CREATE TABLE fact_financial_statement_canonical(
                canonical_fact_id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
                canonical_metric TEXT NOT NULL, statement_type TEXT NOT NULL, period_start TEXT,
                period_end TEXT NOT NULL, accepted_at TEXT NOT NULL, frequency TEXT, value REAL,
                reported_currency TEXT, value_usd REAL, fx_rate REAL, source_raw_fact_id INTEGER,
                source_id TEXT NOT NULL, definition_version TEXT, quality_status TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(ticker,canonical_metric,period_end,accepted_at,source_id))"""
        )
        init_db(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(fact_financial_statement_canonical)")}
        assert "canonical_component" in columns
        unique_columns = [row[2] for row in conn.execute("PRAGMA index_info(sqlite_autoindex_fact_financial_statement_canonical_1)")]
        assert unique_columns == ["ticker", "canonical_metric", "canonical_component", "period_start", "period_end", "accepted_at", "source_id"]
    finally:
        conn.close()


def test_stage1_cli_initializes_scratch_db(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_consumer_defensive.sqlite"
    completed = subprocess.run(
        [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "00_init_consumer_defensive_db.py"),
            "--config",
            str(CONFIG_PATH),
            "--db",
            str(db_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    with sqlite3.connect(db_path) as conn:
        run = conn.execute(
            "SELECT status, row_count FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        assert run is not None
        assert run[0] == "success"
        assert int(run[1]) > 0
        assert conn.execute("SELECT COUNT(*) FROM dim_company").fetchone()[0] == 0


def test_consumer_defensive_python_has_no_cross_sector_imports() -> None:
    forbidden_roots = {"technology", "industrials", "med_devices", "biotech_index"}
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {str(node.module or "").split(".", 1)[0]}
            else:
                continue
            overlap = sorted(forbidden_roots.intersection(roots))
            if overlap:
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{overlap}")
    assert not violations, chr(10).join(violations)
