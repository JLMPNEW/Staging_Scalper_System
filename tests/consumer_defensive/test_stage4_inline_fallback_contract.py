from __future__ import annotations

from pathlib import Path

from consumer_defensive.core.config import load_config
from consumer_defensive.core.db import connect
from consumer_defensive.core.stage4 import (
    INLINE_PARSER_VERSION,
    STAGE4_MIGRATION_HISTORY,
    STAGE4_SCHEMA_VERSION,
    _contains_financial_inline_xbrl_markup,
    bootstrap_stage4,
    ensure_stage4_schema,
)


CONFIG = Path(__file__).resolve().parents[2] / "consumer_defensive" / "config.yaml"


def test_financial_inline_detector_rejects_dei_only_document() -> None:
    metadata_only = (
        b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
        b'<ix:nonNumeric name="dei:DocumentType">6-K</ix:nonNumeric></html>'
    )
    financial = (
        b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
        b'<ix:nonFraction name="ifrs-full:Revenue">100</ix:nonFraction></html>'
    )
    assert not _contains_financial_inline_xbrl_markup(metadata_only)
    assert _contains_financial_inline_xbrl_markup(financial)


def test_stage4_v10_fallback_schema_is_additive_and_idempotent(tmp_path: Path) -> None:
    bundle = load_config(CONFIG)
    with connect(tmp_path / "inline_v10.sqlite") as conn:
        bootstrap_stage4(conn, bundle)
        ensure_stage4_schema(conn)
        assert STAGE4_SCHEMA_VERSION == 10
        assert STAGE4_MIGRATION_HISTORY[-1][0] == 10
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(dim_issuer_reporting_profile)")
        }
        assert {
            "latest_fallback_accepted_at",
            "fallback_document_sha256",
            "fallback_parser_version",
        }.issubset(columns)
        assert conn.execute(
            "SELECT COUNT(*) FROM consumer_defensive_stage4_schema_migration WHERE migration_version=10"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='fact_sec_inline_xbrl_fallback_run'"
        ).fetchone()[0] == 1
        assert INLINE_PARSER_VERSION == "consumer_defensive_inline_xbrl_v1"
