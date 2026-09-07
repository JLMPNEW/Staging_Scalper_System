"""F0.2 terminal-distribution evidence and overlay regression tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from basic_materials.core.config import load_config
from basic_materials.core.db import connect, init_db, utc_now
from basic_materials.core.historical_membership import (
    load_historical_reconciliation,
    load_historical_reconciliation_policy,
    read_and_validate_historical_reconciliation,
    validate_historical_reconciliation_manifest,
)
from basic_materials.core.input_manifest import validate_authoritative_input
from basic_materials.core.market_data_contract import (
    load_market_data_contract,
    load_market_data_policy,
    read_and_validate_market_contract,
    validate_market_data_manifest,
)
from basic_materials.core.source_registry import (
    load_source_registry,
    upsert_source_registry,
)
from basic_materials.core.terminal_distribution_contract import (
    TerminalDistributionContractError,
    load_terminal_distribution_policy,
    read_terminal_distribution_reviews,
    validate_terminal_distribution_manifest,
)
from basic_materials.core.terminal_distribution_reviews import (
    apply_terminal_distribution_reviews,
    hydrate_terminal_distribution_evidence,
    validate_terminal_distribution_database,
)
from basic_materials.core.terminal_returns import reconcile_terminal_returns
from basic_materials.core.universe import load_universe, load_universe_policy


def _loaded_stage3(tmp_path: Path):
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
    policy = load_terminal_distribution_policy(
        config.paths.terminal_distribution_policy
    )
    manifest = validate_terminal_distribution_manifest(
        config.paths.terminal_distribution_manifest,
        policy=policy,
        package_root=config.package_root,
    )
    reviews = read_terminal_distribution_reviews(
        config.paths.terminal_distribution_reviews_csv,
        policy=policy,
        manifest=manifest,
    )
    conn = connect(tmp_path / "basic_materials.sqlite")
    init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    upsert_source_registry(
        conn,
        load_source_registry(config.paths.source_registry),
        utc_now(),
    )
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
    return (
        config,
        conn,
        policy,
        manifest,
        reviews,
        market_policy,
        market_manifest,
        market_bundle,
    )


def _synthetic_evidence(
    tmp_path: Path,
    *,
    config,
    manifest,
    reviews,
):
    phrases: dict[str, list[str]] = {
        key: [] for key in manifest.source_documents
    }
    for review in reviews:
        phrases[review.primary_document_key].append(review.primary_evidence_text)
        if review.supporting_document_key and review.supporting_evidence_text:
            phrases[review.supporting_document_key].append(
                review.supporting_evidence_text
            )
    cache_root = tmp_path / "cache"
    documents = {}
    for key, document in manifest.source_documents.items():
        payload = (
            "<html><body>"
            + " ".join(phrases[key])
            + "</body></html>"
        ).encode()
        relative = f"terminal_distribution_reviews/test/{key}.html"
        target = cache_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        documents[key] = replace(
            document,
            cache_relative_path=relative,
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
        )
    test_config = replace(
        config,
        paths=replace(config.paths, cache_root=cache_root),
    )
    test_manifest = replace(manifest, source_documents=documents)
    evidence = hydrate_terminal_distribution_evidence(
        config=test_config,
        policy=load_terminal_distribution_policy(
            config.paths.terminal_distribution_policy
        ),
        manifest=test_manifest,
        reviews=reviews,
        cache_only=True,
    )
    return test_config, test_manifest, evidence


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
        (
            instrument_id,
            bar_date,
            close,
            close,
            snapshot_key,
            "f" * 64,
            now,
            now,
            now,
        ),
    )


def _seed_terminal_prices(
    conn,
    *,
    market_manifest,
    tmp_path: Path,
) -> str:
    snapshot_key = "fixture:f0-2:2026-09-05"
    now = utc_now()
    conn.execute(
        """
        INSERT INTO fact_market_provider_snapshot (
            snapshot_key, provider_source_id, extraction_asof_date,
            database_fingerprint_json, contract_manifest_sha256,
            raw_manifest_sha256, instrument_count, bar_count, cache_root,
            status, created_at_utc
        ) VALUES (?, 'norgate_us_equities_total_return', '2026-09-05',
                  '{}', ?, ?, 158, 0, ?, 'loaded', ?)
        """,
        (
            snapshot_key,
            market_manifest.checksum,
            "b" * 64,
            str(tmp_path),
            now,
        ),
    )
    terminal_rows = conn.execute(
        """
        SELECT event_key, security_id, evidence_json
        FROM fact_terminal_event_reconciliation
        """
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
    return snapshot_key


def test_terminal_distribution_contract_is_exact() -> None:
    config = load_config()
    policy = load_terminal_distribution_policy(
        config.paths.terminal_distribution_policy
    )
    manifest = validate_terminal_distribution_manifest(
        config.paths.terminal_distribution_manifest,
        policy=policy,
        package_root=config.package_root,
    )
    reviews = read_terminal_distribution_reviews(
        config.paths.terminal_distribution_reviews_csv,
        policy=policy,
        manifest=manifest,
    )
    assert policy.checksum == (
        "589b6e7c8cd55a55de73c1e4ed58f29c2a0cc35f37b505e85ba09c768474f672"
    )
    assert manifest.checksum == (
        "8edf81724a052a93ee890844772b6f53645dd9d8b44a8014b4f5128dee4549d4"
    )
    assert len(reviews) == 4
    assert len(manifest.source_documents) == 5
    assert {review.ticker for review in reviews} == {"ANV", "MCP", "GMO", "BIOA"}
    assert all(review.bankruptcy_distribution_value == 0 for review in reviews)


def test_evidence_replay_rejects_tampered_cached_bytes(tmp_path: Path) -> None:
    config = load_config()
    policy = load_terminal_distribution_policy(
        config.paths.terminal_distribution_policy
    )
    manifest = validate_terminal_distribution_manifest(
        config.paths.terminal_distribution_manifest,
        policy=policy,
        package_root=config.package_root,
    )
    reviews = read_terminal_distribution_reviews(
        config.paths.terminal_distribution_reviews_csv,
        policy=policy,
        manifest=manifest,
    )
    test_config, test_manifest, evidence = _synthetic_evidence(
        tmp_path,
        config=config,
        manifest=manifest,
        reviews=reviews,
    )
    assert len(evidence) == 5
    changed = test_manifest.source_documents["anv_confirmed_plan"]
    (test_config.paths.cache_root / changed.cache_relative_path).write_bytes(
        b"<html>tampered</html>"
    )
    with pytest.raises(
        TerminalDistributionContractError,
        match="cached bytes differ",
    ):
        hydrate_terminal_distribution_evidence(
            config=test_config,
            policy=policy,
            manifest=test_manifest,
            reviews=reviews,
            cache_only=True,
        )


def test_overlay_resolves_four_zero_recoveries_and_survives_stage3_reload(
    tmp_path: Path,
) -> None:
    (
        config,
        conn,
        policy,
        manifest,
        reviews,
        market_policy,
        market_manifest,
        market_bundle,
    ) = _loaded_stage3(tmp_path)
    _, test_manifest, evidence = _synthetic_evidence(
        tmp_path,
        config=config,
        manifest=manifest,
        reviews=reviews,
    )
    try:
        loaded = apply_terminal_distribution_reviews(
            conn,
            policy=policy,
            manifest=test_manifest,
            reviews=reviews,
            evidence=evidence,
        )
        assert loaded.review_rows == 4
        assert loaded.source_documents == 5
        assert loaded.zero_distribution_rows == 4
        assert loaded.calibration_activated is False
        snapshot_key = _seed_terminal_prices(
            conn,
            market_manifest=market_manifest,
            tmp_path=tmp_path,
        )
        terminal = reconcile_terminal_returns(
            conn,
            policy=market_policy,
            as_of="2026-09-05",
            snapshot_key=snapshot_key,
        )
        assert terminal["resolved_terminal_events"] == 20
        assert terminal["unresolved_terminal_events"] == 0
        validation = validate_terminal_distribution_database(
            conn,
            policy=policy,
            manifest=test_manifest,
            reviews=reviews,
            calculation_asof_date="2026-09-05",
            require_reconciled=True,
        )
        assert validation["passed"] is True
        assert validation["database_review_rows"] == 4
        zero_rows = conn.execute(
            """
            SELECT c.event_key, c.distribution_component, c.terminal_value,
                   c.calculation_status, c.resolved
            FROM fact_terminal_return_calculation AS c
            JOIN fact_terminal_distribution_review AS d ON d.event_key = c.event_key
            ORDER BY c.event_key
            """
        ).fetchall()
        assert len(zero_rows) == 4
        assert all(
            (
                float(row["distribution_component"]),
                float(row["terminal_value"]),
                str(row["calculation_status"]),
                int(row["resolved"]),
            )
            == (0.0, 0.0, "resolved_bankruptcy_distribution", 1)
            for row in zero_rows
        )

        load_market_data_contract(
            conn,
            policy=market_policy,
            manifest=market_manifest,
            bundle=market_bundle,
        )
        preserved = conn.execute(
            """
            SELECT COUNT(*) FROM dim_terminal_return_rule
            WHERE source_id = 'basic_materials_terminal_distribution_review'
              AND rule_status = 'ready_for_calculation'
              AND bankruptcy_distribution_value = 0
            """
        ).fetchone()[0]
        assert preserved == 4
        second = apply_terminal_distribution_reviews(
            conn,
            policy=policy,
            manifest=test_manifest,
            reviews=reviews,
            evidence=evidence,
        )
        assert second.review_rows == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_terminal_distribution_review"
        ).fetchone()[0] == 4
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_overlay_rejects_conflicting_rule_without_partial_database_write(
    tmp_path: Path,
) -> None:
    config, conn, policy, manifest, reviews, *_ = _loaded_stage3(tmp_path)
    _, test_manifest, evidence = _synthetic_evidence(
        tmp_path,
        config=config,
        manifest=manifest,
        reviews=reviews,
    )
    try:
        conn.execute(
            """
            UPDATE dim_terminal_return_rule
            SET bankruptcy_distribution_value = 1,
                rule_status = 'ready_for_calculation'
            WHERE event_key = 'terminal_ANV_20150310'
            """
        )
        conn.commit()
        with pytest.raises(
            TerminalDistributionContractError,
            match="refusing to overwrite",
        ):
            apply_terminal_distribution_reviews(
                conn,
                policy=policy,
                manifest=test_manifest,
                reviews=reviews,
                evidence=evidence,
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_terminal_distribution_review"
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM raw_source_payloads
            WHERE source_id = 'basic_materials_terminal_distribution_review'
            """
        ).fetchone()[0] == 0
    finally:
        conn.close()
