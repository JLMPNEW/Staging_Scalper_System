"""Regression tests for the self-contained Stage 3 market-data release."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from basic_materials.core.config import load_config
from basic_materials.core.db import (
    FOUNDATION_SQL,
    HISTORICAL_RECONCILIATION_SQL,
    connect,
    database_counts,
    init_db,
    migration_checksum,
    utc_now,
)
from basic_materials.core.historical_membership import (
    load_historical_reconciliation,
    load_historical_reconciliation_policy,
    read_and_validate_historical_reconciliation,
    validate_historical_reconciliation_database,
    validate_historical_reconciliation_manifest,
)
from basic_materials.core.input_manifest import validate_authoritative_input
from basic_materials.core.market_data_contract import (
    MarketDataContractError,
    load_market_data_contract,
    load_market_data_policy,
    read_and_validate_market_contract,
    validate_market_data_manifest,
)
from basic_materials.core.norgate_prices import load_norgate_market_data
from basic_materials.core.source_registry import load_source_registry, upsert_source_registry
from basic_materials.core.terminal_returns import (
    calculate_terminal_components,
    reconcile_terminal_returns,
)
from basic_materials.core.universe import load_universe, load_universe_policy


def _contracts():
    config = load_config()
    current_manifest = validate_authoritative_input(
        config.paths.authoritative_input_manifest,
        config.paths.universe_csv,
    )
    current_policy = load_universe_policy(config.paths.universe_policy)
    historical_policy = load_historical_reconciliation_policy(
        config.paths.historical_reconciliation_policy
    )
    historical_manifest = validate_historical_reconciliation_manifest(
        config.paths.historical_reconciliation_manifest,
        historical_policy,
        config.package_root,
    )
    historical_bundle = read_and_validate_historical_reconciliation(
        policy=historical_policy,
        manifest=historical_manifest,
        candidate_policy_path=config.paths.historical_candidate_policy,
        candidate_manifest_path=config.paths.historical_candidate_manifest,
        candidate_path=config.paths.historical_candidates_csv,
    )
    market_policy = load_market_data_policy(config.paths.market_data_policy)
    market_manifest = validate_market_data_manifest(
        config.paths.market_data_manifest,
        market_policy,
        config.package_root,
    )
    market_bundle = read_and_validate_market_contract(
        policy=market_policy,
        manifest=market_manifest,
        universe_path=config.paths.universe_csv,
        historical_membership_path=config.paths.historical_membership_csv,
        terminal_events_path=config.paths.terminal_events_csv,
    )
    return (
        config,
        current_policy,
        current_manifest,
        historical_policy,
        historical_manifest,
        historical_bundle,
        market_policy,
        market_manifest,
        market_bundle,
    )


def _loaded_contract(tmp_path: Path):
    contracts = _contracts()
    (
        config,
        current_policy,
        current_manifest,
        historical_policy,
        historical_manifest,
        historical_bundle,
        market_policy,
        market_manifest,
        market_bundle,
    ) = contracts
    conn = connect(tmp_path / "basic_materials.sqlite")
    init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    upsert_source_registry(conn, load_source_registry(config.paths.source_registry), utc_now())
    conn.commit()
    load_universe(conn, policy=current_policy, manifest=current_manifest)
    load_historical_reconciliation(
        conn,
        policy=historical_policy,
        manifest=historical_manifest,
        bundle=historical_bundle,
    )
    load_market_data_contract(
        conn,
        policy=market_policy,
        manifest=market_manifest,
        bundle=market_bundle,
    )
    return conn, contracts


def test_market_contract_is_exact_and_uses_stable_provider_ids() -> None:
    *_, policy, manifest, bundle = _contracts()
    summary = bundle.summary_dict()
    assert summary["market_instrument_role_rows"] == 162
    assert summary["unique_market_instruments"] == 158
    assert summary["terminal_return_rule_rows"] == 20
    assert summary["role_counts"] == {
        "broad_benchmark": 1,
        "current_universe": 134,
        "historical_pilot": 20,
        "sector_benchmark": 1,
        "terminal_successor": 6,
    }
    assert summary["terminal_rule_status_counts"] == {
        "pending_distribution_evidence": 4,
        "ready_for_calculation": 16,
    }
    assert manifest.artifacts["market_instruments"].sha256 == (
        "0aad42b7f87ae2dea63977a1b005eb149724eefd5b17f4905c9016ba0a4a7613"
    )
    zeus = next(
        row for row in bundle.market_instruments if row["event_key"] == "terminal_ZEUS_20260213"
    )
    assert zeus["model_ticker"] == "RYI"
    assert zeus["provider_symbol"] == "RYZ"
    assert zeus["provider_asset_id"] == "1606887"
    assert policy.expected_unique_instruments == 158


def test_market_contract_loader_is_atomic_and_idempotent(tmp_path: Path) -> None:
    conn, contracts = _loaded_contract(tmp_path)
    *_, market_policy, market_manifest, market_bundle = contracts
    try:
        second = load_market_data_contract(
            conn,
            policy=market_policy,
            manifest=market_manifest,
            bundle=market_bundle,
        )
        counts = database_counts(conn)
        assert second.unique_instruments == 158
        assert counts["dim_market_instrument"] == 158
        assert counts["bridge_market_instrument_role"] == 162
        assert counts["dim_terminal_return_rule"] == 20
        assert counts["raw_source_payloads"] == 7
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_market_contract_rejects_provider_identity_tampering(tmp_path: Path) -> None:
    config, *_, policy, manifest, _ = _contracts()
    entry = manifest.artifacts["market_instruments"]
    tampered = tmp_path / "basic_materials_market_instruments.csv"
    text = entry.path.read_text(encoding="utf-8")
    tampered.write_text(
        text.replace(
            "norgate_us_equities_total_return:3439043,current_universe,WS",
            "norgate_us_equities_total_return:3439043,current_universe,WS",
            1,
        ).replace(",WS,3439043,2023-11-28,", ",WS,9999999,2023-11-28,", 1),
        encoding="utf-8",
    )
    payload = tampered.read_bytes()
    changed_entry = replace(
        entry,
        path=tampered,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
    )
    changed_manifest = replace(
        manifest,
        artifacts={**manifest.artifacts, "market_instruments": changed_entry},
    )
    with pytest.raises(MarketDataContractError, match="stable provider identity"):
        read_and_validate_market_contract(
            policy=policy,
            manifest=changed_manifest,
            universe_path=config.paths.universe_csv,
            historical_membership_path=config.paths.historical_membership_csv,
            terminal_events_path=config.paths.terminal_events_csv,
        )


def test_terminal_component_formulas() -> None:
    assert calculate_terminal_components(
        outcome_class="fixed_cash",
        cash_weight=1,
        stock_weight=0,
        cash_consideration=55,
        successor_share_ratio=None,
        successor_close=None,
        bankruptcy_distribution_value=None,
    ) == (55, None, None, 55)
    cash, stock, distribution, total = calculate_terminal_components(
        outcome_class="mixed_prorated",
        cash_weight=0.15,
        stock_weight=0.85,
        cash_consideration=3.92,
        successor_share_ratio=0.36,
        successor_close=100,
        bankruptcy_distribution_value=None,
    )
    assert cash == pytest.approx(0.588)
    assert stock == pytest.approx(30.6)
    assert distribution is None
    assert total == pytest.approx(31.188)


def _insert_price(
    conn,
    *,
    instrument_id: int,
    bar_date: str,
    close: float,
    snapshot_key: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO fact_adjusted_price_bar (
            instrument_id, bar_date, provider_source_id, close, adjusted_close,
            capital_event, adjustment_basis, snapshot_key, payload_sha256,
            source_timestamp_utc, created_at_utc, updated_at_utc
        ) VALUES (?, ?, 'norgate_us_equities_total_return', ?, ?, 0,
                  'norgate_total_return', ?, ?, ?, ?, ?)
        """,
        (instrument_id, bar_date, close, close, snapshot_key, "f" * 64, now, now, now),
    )


def test_terminal_reconciliation_resolves_calculable_events_without_lookahead(tmp_path: Path) -> None:
    conn, contracts = _loaded_contract(tmp_path)
    (
        _,
        current_policy,
        _,
        historical_policy,
        historical_manifest,
        historical_bundle,
        market_policy,
        _,
        _,
    ) = contracts
    snapshot_key = "fixture:2026-09-05"
    now = utc_now()
    try:
        conn.execute(
            """
            INSERT INTO fact_market_provider_snapshot (
                snapshot_key, provider_source_id, extraction_asof_date,
                database_fingerprint_json, contract_manifest_sha256,
                raw_manifest_sha256, instrument_count, bar_count, cache_root,
                status, created_at_utc
            ) VALUES (?, 'norgate_us_equities_total_return', '2026-09-05', '{}', ?, ?, 158, 0, ?, 'loaded', ?)
            """,
            (snapshot_key, "a" * 64, "b" * 64, str(tmp_path), now),
        )
        terminal_rows = conn.execute(
            "SELECT event_key, security_id, evidence_json FROM fact_terminal_event_reconciliation"
        ).fetchall()
        for terminal in terminal_rows:
            event = json.loads(str(terminal["evidence_json"]))
            historical_id = int(
                conn.execute(
                    """
                    SELECT instrument_id FROM bridge_market_instrument_role
                    WHERE role_type = 'historical_pilot' AND security_id = ?
                    """,
                    (terminal["security_id"],),
                ).fetchone()[0]
            )
            _insert_price(
                conn,
                instrument_id=historical_id,
                bar_date=event["last_trade_date"],
                close=10,
                snapshot_key=snapshot_key,
            )
            if event.get("successor_ticker"):
                successor = conn.execute(
                    """
                    SELECT instrument_id FROM bridge_market_instrument_role
                    WHERE event_key = ? AND role_type = 'terminal_successor'
                    """,
                    (terminal["event_key"],),
                ).fetchone()
                if successor is None:
                    successor = conn.execute(
                        """
                        SELECT instrument_id FROM bridge_market_instrument_role
                        WHERE UPPER(model_ticker) = UPPER(?)
                        ORDER BY CASE role_type WHEN 'current_universe' THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (event["successor_ticker"],),
                    ).fetchone()
                _insert_price(
                    conn,
                    instrument_id=int(successor[0]),
                    bar_date=event["successor_reference_date"],
                    close=100,
                    snapshot_key=snapshot_key,
                )
        conn.commit()

        stats = reconcile_terminal_returns(
            conn,
            policy=market_policy,
            as_of="2026-09-05",
            snapshot_key=snapshot_key,
        )
        assert stats["resolved_terminal_events"] == 16
        assert stats["unresolved_terminal_events"] == 4
        assert stats["calibration_activated"] is False
        zeus = conn.execute(
            """
            SELECT terminal_value, successor_reference_price_date, no_future_price_used
            FROM fact_terminal_return_calculation
            WHERE event_key = 'terminal_ZEUS_20260213'
            """
        ).fetchone()
        assert float(zeus["terminal_value"]) == pytest.approx(171.05)
        assert str(zeus["successor_reference_price_date"]) == "2026-02-13"
        assert int(zeus["no_future_price_used"]) == 1
        mmx = conn.execute(
            "SELECT terminal_value FROM fact_terminal_return_calculation WHERE event_key = 'terminal_MMX_20230119'"
        ).fetchone()
        assert float(mmx[0]) == pytest.approx(31.188)
        historical_report = validate_historical_reconciliation_database(
            conn,
            policy=historical_policy,
            manifest=historical_manifest,
            bundle=historical_bundle,
            expected_current_rows=current_policy.expected_current_rows,
        )
        assert historical_report.passed, historical_report.summary_dict()
        assert historical_report.unresolved_terminal_events == 4
    finally:
        conn.close()


class _Adjustment:
    NONE = 0
    TOTALRETURN = 3


class _FakeNorgate:
    StockPriceAdjustmentType = _Adjustment

    @staticmethod
    def status() -> bool:
        return True

    @staticmethod
    def last_database_update_time(database: str) -> str:
        return f"fixture:{database}:2026-01-06"

    @staticmethod
    def assetid(symbol: str) -> int:
        assert symbol == "SPY"
        return 999

    @staticmethod
    def price_timeseries(
        symbol: str,
        *,
        stock_price_adjustment_setting: int,
        start_date: str,
        end_date: str,
        timeseriesformat: str,
    ) -> pd.DataFrame:
        assert symbol == "SPY"
        assert start_date == "2026-01-02"
        assert end_date == "2026-01-06"
        assert timeseriesformat == "pandas-dataframe"
        multiplier = 2 if stock_price_adjustment_setting == _Adjustment.TOTALRETURN else 1
        index = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
        return pd.DataFrame(
            {
                "Open": [10, 11, 12],
                "High": [11, 12, 13],
                "Low": [9, 10, 11],
                "Close": [10, 11, 12] if multiplier == 1 else [20, 22, 24],
                "Volume": [100, 110, 120],
                "Dividend": [0, 0.5, 0],
            },
            index=index,
        )

    @staticmethod
    def capital_event_timeseries(
        symbol: str,
        *,
        start_date: str,
        end_date: str,
        timeseriesformat: str,
    ) -> pd.DataFrame:
        assert symbol == "SPY"
        return pd.DataFrame(
            {"Capital Event": [1]},
            index=pd.to_datetime(["2026-01-05"]),
        )


def test_norgate_adapter_fences_caches_and_publishes_atomically(tmp_path: Path) -> None:
    config = load_config()
    policy = load_market_data_policy(config.paths.market_data_policy)
    manifest = validate_market_data_manifest(
        config.paths.market_data_manifest,
        policy,
        config.package_root,
    )
    one_instrument_policy = replace(policy, expected_unique_instruments=1)
    conn = connect(tmp_path / "basic_materials.sqlite")
    try:
        init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        upsert_source_registry(conn, load_source_registry(config.paths.source_registry), utc_now())
        now = utc_now()
        conn.execute(
            """
            INSERT INTO dim_market_instrument (
                instrument_key, provider_source_id, provider_asset_id, provider_symbol,
                canonical_ticker, provider_database, trading_currency,
                provider_first_quoted_date, adjustment_basis, contract_version,
                contract_sha256, created_at_utc, updated_at_utc
            ) VALUES ('norgate_us_equities_total_return:999',
                      'norgate_us_equities_total_return', '999', 'SPY', 'SPY',
                      'US Equities', 'USD', '2026-01-02', 'norgate_total_return',
                      'fixture', ?, ?, ?)
            """,
            ("c" * 64, now, now),
        )
        instrument_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO bridge_market_instrument_role (
                role_key, instrument_id, role_type, model_ticker, security_scope,
                expected_start_date, required_for_stage3, required_for_current_gate,
                source_id, contract_version, contract_sha256, created_at_utc, updated_at_utc
            ) VALUES ('benchmark:broad:SPY', ?, 'broad_benchmark', 'SPY',
                      'broad_benchmark', '2026-01-02', 1, 1,
                      'basic_materials_market_instrument_review', 'fixture', ?, ?, ?)
            """,
            (instrument_id, "c" * 64, now, now),
        )
        conn.commit()

        stats = load_norgate_market_data(
            conn,
            policy=one_instrument_policy,
            manifest=manifest,
            provider=_FakeNorgate(),
            cache_root=tmp_path / "cache",
            as_of="2026-01-06",
        )
        assert stats["instrument_count"] == 1
        assert stats["bar_count"] == 3
        assert stats["corporate_action_count"] == 2
        assert stats["calendar_session_count"] == 3
        assert Path(stats["cache_manifest_path"]).is_file()
        bars = conn.execute(
            "SELECT close, adjusted_close FROM fact_adjusted_price_bar ORDER BY bar_date"
        ).fetchall()
        assert [(float(row[0]), float(row[1])) for row in bars] == [
            (10, 20),
            (11, 22),
            (12, 24),
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_schema_v2_database_migrates_to_v3(tmp_path: Path) -> None:
    conn = connect(tmp_path / "basic_materials.sqlite")
    try:
        conn.executescript(FOUNDATION_SQL)
        conn.executescript(HISTORICAL_RECONCILIATION_SQL)
        now = utc_now()
        conn.executemany(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at_utc) VALUES (?, ?, ?, ?)",
            (
                (1, "basic_materials_foundation", migration_checksum(FOUNDATION_SQL), now),
                (
                    2,
                    "basic_materials_historical_reconciliation",
                    migration_checksum(HISTORICAL_RECONCILIATION_SQL),
                    now,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO sector_database_identity (
                identity_id, model_family, sector, schema_owner, schema_version, created_at_utc
            ) VALUES (1, 'basic_materials', 'Basic Materials', 'basic_materials', 2, ?)
            """,
            (now,),
        )
        conn.commit()
        result = init_db(conn)
        assert result["schema_version"] == 3
        assert result["migrations_applied"] == [3]
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='feature_market_technical'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
