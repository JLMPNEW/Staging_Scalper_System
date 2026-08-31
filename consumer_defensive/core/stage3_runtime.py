from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from consumer_defensive.core.config import ConfigBundle, cfg_get, resolve_path
from consumer_defensive.core.db import init_db
from consumer_defensive.core.market_data import MarketDataPolicy, ensure_stage3_schema, load_market_policy
from consumer_defensive.core.source_registry import load_source_registry, upsert_source_registry
from consumer_defensive.core.terminal_events import ensure_terminal_event_schema
from consumer_defensive.core.universe import (
    active_universe_tickers,
    load_policy,
    upsert_stage2_sources,
)
from consumer_defensive.core.universe_validation import validate_stage2


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARKET_POLICY = PACKAGE_ROOT / "data" / "consumer_defensive_market_data_policy.yaml"
DEFAULT_TERMINAL_POLICY = PACKAGE_ROOT / "data" / "consumer_defensive_terminal_event_policy.yaml"


def database_path(bundle: ConfigBundle, override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    return resolve_path(cfg_get(bundle.payload, "paths.database_path"), base_dir=bundle.base_dir)


def stage3_output_dir(
    bundle: ConfigBundle,
    policy: MarketDataPolicy,
    *,
    as_of: str,
    override: Path | None,
) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    try:
        root = policy.resolve("outputs.root")
    except ValueError:
        root = resolve_path(cfg_get(bundle.payload, "paths.output_dir"), base_dir=bundle.base_dir) / "stage3"
    return root / as_of


def bootstrap_stage3(conn: sqlite3.Connection, bundle: ConfigBundle) -> None:
    init_db(conn)
    upsert_source_registry(
        conn,
        load_source_registry(
            resolve_path(cfg_get(bundle.payload, "source_registry.path"), base_dir=bundle.base_dir)
        ),
    )
    upsert_stage2_sources(
        conn,
        load_source_registry(
            resolve_path(cfg_get(bundle.payload, "source_registry.stage2_path"), base_dir=bundle.base_dir)
        ),
    )
    ensure_stage3_schema(conn)
    ensure_terminal_event_schema(conn)


def assert_stage2_ready(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    universe_policy = load_policy(
        resolve_path(
            cfg_get(bundle.payload, "universe.policy_path"),
            base_dir=bundle.base_dir,
        )
    )
    validation = validate_stage2(
        conn,
        universe_policy,
        require_pit_membership=True,
        as_of=as_of,
    )
    expected = int(cfg_get(bundle.payload, "universe.expected_current_rows"))
    active = len(active_universe_tickers(conn))
    delisted = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM dim_security s
            JOIN dim_company c ON c.company_id=s.company_id
            JOIN dim_consumer_defensive_taxonomy t ON t.security_id=s.security_id
            WHERE s.listing_status<>'active' AND c.is_active=0
            """
        ).fetchone()[0]
    )
    unresolved_symbols = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM dim_security s
            JOIN dim_consumer_defensive_taxonomy t ON t.security_id=s.security_id
            WHERE COALESCE(TRIM(s.provider_price_symbol), '')=''
            """
        ).fetchone()[0]
    )
    pit_rows = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM dim_universe_membership
            WHERE model_family='consumer_defensive'
              AND membership_source_id='norgate_us_equities_pit_membership'
            """
        ).fetchone()[0]
    )
    errors: list[str] = []
    if validation["status"] != "PASS":
        errors.extend(str(error) for error in validation["errors"])
    if active != expected:
        errors.append(f"active_universe_rows={active}; expected={expected}")
    if delisted < 1:
        errors.append("no_delisted_securities_loaded")
    if unresolved_symbols:
        errors.append(f"provider_price_symbols_missing={unresolved_symbols}")
    if pit_rows < 1:
        errors.append("norgate_pit_membership_not_loaded")
    if errors:
        raise RuntimeError("Stage 2 must pass before Stage 3: " + "; ".join(errors))
    return {
        "active_securities": active,
        "delisted_securities": delisted,
        "pit_membership_rows": pit_rows,
        "provider_price_symbols_missing": unresolved_symbols,
        "recognized_current_members": int(validation["recognized_current_members"]),
        "membership_as_of": validation["membership_as_of"],
        "recognized_members_as_of": int(validation["recognized_members_as_of"]),
        "major_exchange_listings_as_of": int(
            validation["major_exchange_listings_as_of"]
        ),
        "complete_four_index_rows_as_of": int(validation["complete_four_index_rows_as_of"]),
        "norgate_asset_identities": int(validation["norgate_asset_identities"]),
        "complete_four_index_daily_series": int(
            validation["complete_four_index_daily_series"]
        ),
        "historical_candidates_expected": int(
            validation["historical_candidates_expected"]
        ),
        "historical_taxonomy_rows": int(validation["historical_taxonomy_rows"]),
        "historical_norgate_asset_identities": int(
            validation["historical_norgate_asset_identities"]
        ),
        "historical_complete_four_index_daily_series": int(
            validation["historical_complete_four_index_daily_series"]
        ),
        "historical_recognized_members": int(
            validation["historical_recognized_members"]
        ),
    }


def load_stage3_policy(path: Path | None) -> MarketDataPolicy:
    return load_market_policy(path or DEFAULT_MARKET_POLICY)
