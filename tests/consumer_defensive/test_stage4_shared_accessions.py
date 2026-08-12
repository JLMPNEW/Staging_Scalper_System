from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import consumer_defensive.core.stage4 as stage4_module

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.db import connect, init_db
from consumer_defensive.core.stage4 import (
    STAGE4_MIGRATION_HISTORY,
    bootstrap_stage4,
    build_financial_features,
    ensure_stage4_schema,
    sync_sec_fundamentals,
    validate_stage4,
)
from consumer_defensive.core.universe import load_current_universe, load_policy
from dedicated_parser.catalog import _cache_seal_valid, filing_rows
from dedicated_parser.contracts import (
    AdapterRegistry,
    DocumentRef,
    FilingRef,
    MetricRequest,
)
from dedicated_parser.planner import (
    _validate_consumer_defensive_direct_documents,
    _validate_consumer_defensive_direct_filings,
    audit_cache_completeness,
    build_plan,
)
from dedicated_parser.promotion import _filing_metadata, promote_run
from dedicated_parser.path_io import (
    filesystem_path,
    mkdir_path,
    open_path,
    read_bytes,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "consumer_defensive" / "config.yaml"
POLICY = ROOT / "consumer_defensive" / "data" / "consumer_defensive_universe_policy.yaml"
SHARED = "0001193125-24-123456"  # Filing-agent prefix: not either issuer CIK.
CIKS = {"KO": "0000021344", "PEP": "0000077476"}


def _prepared_db(tmp_path: Path, name: str) -> tuple[ConfigBundle, sqlite3.Connection]:
    source = load_config(CONFIG)
    payload = copy.deepcopy(source.payload)
    payload["sec_fundamentals"]["cache_dir"] = str(tmp_path / f"cache-{name}")
    bundle = ConfigBundle(source.path, source.base_dir, payload)
    conn = connect(tmp_path / f"{name}.sqlite")
    bootstrap_stage4(conn, bundle)
    load_current_universe(conn, load_policy(POLICY))
    return bundle, conn


def _submissions(*, form: str, accepted: str, document: str, accession: str = SHARED):
    return {"filings": {"recent": {
        "accessionNumber": [accession],
        "filingDate": [accepted[:10]],
        "acceptanceDateTime": [accepted],
        "reportDate": ["2024-03-31"],
        "form": [form],
        "primaryDocument": [document],
    }, "files": []}}


def _companyfacts(*, form: str, accession: str = SHARED):
    return {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [{
        "start": "2024-01-01", "end": "2024-03-31", "val": 100.0,
        "accn": accession, "form": form, "filed": "2024-04-30",
    }]}}}}}


class Provider:
    def __init__(self, submissions, companyfacts=None):
        self.submissions = submissions
        self.companyfacts = companyfacts or {ticker: {"facts": {}} for ticker in CIKS}

    def __call__(self, url: str) -> bytes:
        for ticker, cik in CIKS.items():
            if f"CIK{cik}.json" in url:
                payload = copy.deepcopy(
                    self.companyfacts[ticker]
                    if "companyfacts" in url else self.submissions[ticker]
                )
                payload.setdefault("cik", str(int(cik)))
                return json.dumps(payload).encode()
            if f"/data/{int(cik)}/" in url:
                return f"<html><body>{ticker} document</body></html>".encode()
        raise AssertionError(url)


def _sync(conn, bundle, provider, ticker):
    result = sync_sec_fundamentals(
        conn, bundle, tickers=[ticker], as_of="2025-12-31",
        force_refresh=True, fetch=provider,
    )
    assert result["failures"] == []


def _filing_digest(conn: sqlite3.Connection):
    base = tuple(conn.execute(
        """SELECT accession_number,company_id,ticker,cik,form_type,filing_date,
                  accepted_at,report_date,primary_document,source_id,source_url,
                  metadata_quality_flags_json
           FROM fact_sec_filing WHERE accession_number=?""", (SHARED,)
    ).fetchone())
    bridge = [tuple(row) for row in conn.execute(
        """SELECT accession_number,issuer_company_id,issuer_ticker,issuer_cik,
                  relationship,relationship_evidence,form_type,filing_date,
                  accepted_at,report_date,primary_document,source_id,source_url
           FROM bridge_sec_filing_company WHERE accession_number=?
           ORDER BY issuer_ticker""", (SHARED,)
    )]
    return base, bridge


def test_core_pfgc_style_shared_425_is_order_independent_and_owner_neutral(tmp_path: Path):
    provider = Provider({
        "KO": _submissions(form="425", accepted="2024-04-30T12:00:00Z", document="shared.htm"),
        "PEP": _submissions(form="425", accepted="2024-04-30T17:00:00Z", document="shared.htm"),
    })
    digests = []
    for position, order in enumerate((("KO", "PEP"), ("PEP", "KO"))):
        bundle, conn = _prepared_db(tmp_path, f"order-{position}")
        try:
            for ticker in order:
                _sync(conn, bundle, provider, ticker)
            digests.append(_filing_digest(conn))
            base, bridge = digests[-1]
            assert base[1:4] == (None, "ACCESSION_NEUTRAL", None)
            assert base[6] == "2024-04-30T17:00:00Z"
            assert base[10] is None
            assert json.loads(base[11]) == ["association_accepted_at_conflict"]
            assert len(bridge) == 2
            assert "/data/21344/" in bridge[0][-1]
            assert "/data/77476/" in bridge[1][-1]
        finally:
            conn.close()
    assert digests[0] == digests[1]


def test_svu_unfi_style_metadata_conflict_resolves_deterministically_and_is_flagged(tmp_path: Path):
    provider = Provider({
        "KO": _submissions(form="DFAN14A", accepted="2024-05-01T12:00:00Z", document="ko.htm"),
        "PEP": _submissions(form="425", accepted="2024-05-01T12:00:00Z", document="pep.htm"),
    })
    bundle, conn = _prepared_db(tmp_path, "metadata-conflict")
    try:
        _sync(conn, bundle, provider, "PEP")
        _sync(conn, bundle, provider, "KO")
        row = conn.execute(
            "SELECT form_type,primary_document,metadata_quality_flags_json FROM fact_sec_filing WHERE accession_number=?",
            (SHARED,),
        ).fetchone()
        assert tuple(row[:2]) == ("DFAN14A", "ko.htm")  # lowest archive CIK wins as one context
        assert json.loads(row[2]) == ["association_metadata_conflict"]
        assert conn.execute(
            "SELECT form_type FROM bridge_sec_filing_company WHERE accession_number=? AND issuer_ticker='PEP'",
            (SHARED,),
        ).fetchone()[0] == "425"
    finally:
        conn.close()


def test_reverse_cache_snapshot_replay_after_s2_is_zero_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_provider = Provider({
        'KO': _submissions(form='425', accepted='2024-04-30T12:00:00Z', document='shared.htm'),
        'PEP': _submissions(form='425', accepted='2024-04-30T17:00:00Z', document='shared.htm'),
    })
    second_provider = Provider({
        'KO': _submissions(form='425', accepted='2024-05-01T12:00:00Z', document='shared.htm'),
        'PEP': _submissions(form='425', accepted='2024-05-01T17:00:00Z', document='shared.htm'),
    })
    bundle, conn = _prepared_db(tmp_path, 'sealed-replay')
    try:
        conn.execute(
            '''DELETE FROM dim_consumer_defensive_taxonomy
               WHERE ticker NOT IN ('KO','PEP')'''
        )
        conn.commit()
        first = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-30', force_refresh=True,
            fetch=first_provider,
        )
        assert first['failures'] == []
        assert first['full_scope_reconciled'] is True
        first_manifest = first['association_manifest']
        second = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True,
            fetch=second_provider,
        )
        assert second['failures'] == []
        assert second['association_manifest'] != first_manifest
        assert conn.execute(
            'SELECT COUNT(*) FROM consumer_defensive_sec_cache_snapshot'
        ).fetchone()[0] == 2
        monkeypatch.setenv('CONSUMER_DEFENSIVE_CACHE_ONLY', '1')

        def no_network(_url: str) -> bytes:
            raise AssertionError('cache-only replay attempted network access')

        before = [tuple(row) for row in conn.execute('''SELECT accession_number,
            issuer_company_id,accepted_at,association_status
            FROM bridge_sec_filing_company
            ORDER BY accession_number,issuer_company_id''')]
        with pytest.raises(
            RuntimeError,match='reverse replay|immutable snapshot and reconciliation'
        ):
            sync_sec_fundamentals(
                conn, bundle, as_of='2025-12-30', fetch=no_network,
            )
        after = [tuple(row) for row in conn.execute('''SELECT accession_number,
            issuer_company_id,accepted_at,association_status
            FROM bridge_sec_filing_company
            ORDER BY accession_number,issuer_company_id''')]
        assert after == before
    finally:
        conn.close()


def test_full_scope_reconciliation_retires_and_reactivates_associations(
    tmp_path: Path,
) -> None:
    replacement = '0000000000-24-000002'
    first_provider = Provider({
        'KO': _submissions(form='425', accepted='2024-04-30T12:00:00Z',
                           document='shared.htm'),
        'PEP': _submissions(form='425', accepted='2024-04-30T17:00:00Z',
                            document='shared.htm'),
    })
    second_provider = Provider({
        'KO': _submissions(form='425', accepted='2024-05-01T12:00:00Z',
                           document='new.htm', accession=replacement),
        'PEP': _submissions(form='425', accepted='2024-04-30T17:00:00Z',
                            document='shared.htm'),
    })
    bundle, conn = _prepared_db(tmp_path, 'association-retirement')
    try:
        conn.execute(
            '''DELETE FROM dim_consumer_defensive_taxonomy
               WHERE ticker NOT IN ('KO','PEP')'''
        )
        conn.commit()
        first = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-29', force_refresh=True,
            fetch=first_provider,
        )
        assert first['failures'] == []
        targeted = sync_sec_fundamentals(
            conn, bundle, tickers=['KO'], as_of='2025-12-30',
            force_refresh=True, fetch=second_provider,
        )
        assert targeted['failures'] == []
        assert conn.execute(
            '''SELECT association_status FROM bridge_sec_filing_company
               WHERE accession_number=? AND issuer_ticker='KO' ''', (SHARED,)
        ).fetchone()[0] == 'active'
        second = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True,
            fetch=second_provider,
        )
        assert second['failures'] == []
        assert conn.execute(
            '''SELECT association_status,retirement_effective_asof
               FROM bridge_sec_filing_company
               WHERE accession_number=? AND issuer_ticker='KO' ''', (SHARED,)
        ).fetchone()[:] == ('retired', '2025-12-31')
        third = sync_sec_fundamentals(
            conn, bundle, as_of='2026-01-01', force_refresh=True,
            fetch=first_provider,
        )
        assert third['failures'] == []
        assert conn.execute(
            '''SELECT association_status,retirement_effective_asof
               FROM bridge_sec_filing_company
               WHERE accession_number=? AND issuer_ticker='KO' ''', (SHARED,)
        ).fetchone()[:] == ('active', None)
        assert conn.execute(
            '''SELECT association_status FROM bridge_sec_filing_company
               WHERE accession_number=? AND issuer_ticker='KO' ''', (replacement,)
        ).fetchone()[0] == 'retired'
        events = [tuple(row) for row in conn.execute(
            '''SELECT event_type,substr(effective_asof,1,10)
               FROM sec_filing_company_association_event e
               JOIN dim_company c ON c.company_id=e.issuer_company_id
               WHERE e.accession_number=? AND c.primary_ticker='KO'
               ORDER BY e.effective_asof,e.event_id''',(SHARED,)
        )]
        assert events == [
            ('observed','2024-04-30'),('retired','2025-12-31'),
            ('reactivated','2026-01-01'),
        ]
        assert _filing_metadata(
            conn,model_family='consumer_defensive',ticker='KO',
            accession_number=SHARED,asof_date='2025-12-29',
        )['form_type'] == '425'
        with pytest.raises(RuntimeError,match='no issuer association'):
            _filing_metadata(
                conn,model_family='consumer_defensive',ticker='KO',
                accession_number=SHARED,asof_date='2025-12-31',
            )
        assert _filing_metadata(
            conn,model_family='consumer_defensive',ticker='KO',
            accession_number=SHARED,asof_date='2026-01-01',
        )['form_type'] == '425'
    finally:
        conn.close()


def test_shared_financial_accession_reconciles_raw_documents_profiles_and_view(tmp_path: Path):
    provider = Provider(
        {
            "KO": _submissions(form="10-Q", accepted="2024-04-30T12:00:00Z", document="ko10q.htm"),
            "PEP": _submissions(form="10-Q", accepted="2024-04-30T17:00:00Z", document="pep10q.htm"),
        },
        {"KO": _companyfacts(form="10-Q"), "PEP": _companyfacts(form="10-Q")},
    )
    bundle, conn = _prepared_db(tmp_path, "shared-financial")
    try:
        _sync(conn, bundle, provider, "KO")
        assert conn.execute(
            "SELECT accepted_at FROM fact_sec_xbrl_fact_raw WHERE ticker='KO'"
        ).fetchone()[0] == "2024-04-30T12:00:00Z"
        _sync(conn, bundle, provider, "PEP")
        expected = "2024-04-30T17:00:00Z"
        assert {row[0] for row in conn.execute(
            "SELECT accepted_at FROM fact_sec_xbrl_fact_raw WHERE accession_number=?", (SHARED,)
        )} == {expected}
        assert {row[0] for row in conn.execute(
            "SELECT accepted_at FROM bridge_sec_filing_document_company WHERE accession_number=?", (SHARED,)
        )} == {expected}
        profiles = conn.execute(
            """SELECT latest_filing_accepted_at,latest_companyfacts_accepted_at,
                      companyfacts_lag_days,inline_xbrl_fallback_required,coverage_status
               FROM dim_issuer_reporting_profile WHERE ticker IN ('KO','PEP')"""
        ).fetchall()
        assert {tuple(row) for row in profiles} == {
            (expected, expected, 0, 0, "covered")
        }
        view = conn.execute(
            """SELECT ticker,accepted_at,observed_accepted_at,source_id
               FROM consumer_defensive_sec_parser_filing_input
               WHERE accession_number=? ORDER BY ticker""", (SHARED,)
        ).fetchall()
        assert [tuple(row[:3]) for row in view] == [
            ("KO", expected, "2024-04-30T12:00:00Z"),
            ("PEP", expected, "2024-04-30T17:00:00Z"),
        ]
        assert {row[3] for row in view} == {"sec_submissions"}
    finally:
        conn.close()


def test_companyfacts_acceptance_lookup_does_not_leak_between_issuers(tmp_path: Path):
    provider = Provider(
        {"KO": _submissions(form="425", accepted="2024-04-30T12:00:00Z", document="other.htm", accession="0000000000-24-000777"),
         "PEP": _submissions(form="10-Q", accepted="2024-04-30T17:00:00Z", document="pep.htm")},
        {"KO": _companyfacts(form="10-Q"), "PEP": {"facts": {}}},
    )
    bundle, conn = _prepared_db(tmp_path, "issuer-safe")
    try:
        _sync(conn, bundle, provider, "PEP")
        _sync(conn, bundle, provider, "KO")
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE ticker='KO' AND accession_number=?",
            (SHARED,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_parser_catalog_and_promotion_use_association_view_and_fail_closed(tmp_path: Path):
    provider = Provider({
        "KO": _submissions(form="425", accepted="2024-04-30T12:00:00Z", document="shared.htm"),
        "PEP": _submissions(form="425", accepted="2024-04-30T17:00:00Z", document="shared.htm"),
    })
    bundle, conn = _prepared_db(tmp_path, "parser")
    try:
        conn.execute(
            "DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker NOT IN ('KO','PEP')"
        )
        conn.commit()
        result = sync_sec_fundamentals(
            conn, bundle, as_of="2025-12-31", force_refresh=True, fetch=provider
        )
        assert result["full_scope_reconciled"] is True
        expected_config_sha256 = stage4_module._sec_ingestion_config_sha256(
            bundle.payload['sec_fundamentals']
        )
        with pytest.raises(RuntimeError,match='independently supplied'):
            filing_rows(
                conn,model_family='consumer_defensive',asof_date='2025-12-31',
                tickers=['KO'],accessions=[SHARED],supported_forms=('425',),
                max_filings_per_ticker=1,
            )
        with pytest.raises(RuntimeError,match='reconciliation seal'):
            filing_rows(
                conn,model_family='consumer_defensive',asof_date='2025-12-31',
                tickers=['KO'],accessions=[SHARED],supported_forms=('425',),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256='0' * 64,
            )
        rows = filing_rows(
            conn, model_family="consumer_defensive", asof_date="2025-12-31",
            tickers=["KO", "PEP"], accessions=[SHARED], supported_forms=("425",),
            max_filings_per_ticker=2,
            expected_ingestion_config_sha256=expected_config_sha256,
        )
        assert rows["KO"][0].cik == rows["KO"][0].archive_cik == CIKS["KO"]
        assert rows["PEP"][0].cik == rows["PEP"][0].archive_cik == CIKS["PEP"]
        assert _filing_metadata(
            conn, model_family="consumer_defensive", ticker="KO", accession_number=SHARED,
            asof_date='2025-12-31',
        )["accepted_at"] == "2024-04-30T17:00:00Z"
        conn.execute(
            "UPDATE bridge_sec_filing_company SET primary_document='tampered.htm' "
            "WHERE accession_number=? AND issuer_ticker='KO'",
            (SHARED,),
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="reconciliation seal"):
            filing_rows(
                conn, model_family="consumer_defensive", asof_date="2025-12-31",
                tickers=["KO"], accessions=[SHARED], supported_forms=("425",),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256=expected_config_sha256,
            )
        with pytest.raises(RuntimeError, match="no issuer association"):
            _filing_metadata(
                conn, model_family="consumer_defensive", ticker="WMT", accession_number=SHARED,
                asof_date='2025-12-31',
            )
    finally:
        conn.close()

    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        with pytest.raises(RuntimeError, match="requires consumer_defensive_sec_parser"):
            filing_rows(
                memory, model_family="consumer_defensive", asof_date="2025-12-31",
                tickers=["KO"], accessions=None, supported_forms=("10-K",),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256=expected_config_sha256,
            )
        with pytest.raises(RuntimeError, match="requires consumer_defensive_sec_parser"):
            _filing_metadata(
                memory, model_family="consumer_defensive", ticker="KO", accession_number=SHARED
            )
    finally:
        memory.close()


def test_parser_catalog_rejects_missing_or_tampered_lifecycle_events(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path,'parser-lifecycle-integrity')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='425',accepted='2024-04-30T12:00:00Z',document='ko.htm'
    ),'PEP': _submissions(
        form='425',accepted='2024-04-30T12:00:00Z',document='pep.htm'
    )})
    try:
        assert sync_sec_fundamentals(
            conn,bundle,as_of='2025-12-31',force_refresh=True,fetch=provider,
        )['full_scope_reconciled'] is True
        expected = stage4_module._sec_ingestion_config_sha256(
            bundle.payload['sec_fundamentals']
        )
        conn.execute('DROP TRIGGER trg_stage4_association_event_no_update')
        event = conn.execute('''SELECT *
            FROM sec_filing_company_association_event LIMIT 1''').fetchone()
        conn.execute('''UPDATE sec_filing_company_association_event
            SET event_sha256=? WHERE event_id=?''',('0' * 64,int(event['event_id'])))
        conn.commit()
        with pytest.raises(RuntimeError,match='lifecycle identity and hashes'):
            filing_rows(
                conn,model_family='consumer_defensive',asof_date='2025-12-31',
                tickers=['KO'],accessions=[SHARED],supported_forms=('425',),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256=expected,
            )
        digest = stage4_module._association_event_sha256(
            str(event['accession_number']),int(event['issuer_company_id']),
            str(event['issuer_ticker']),str(event['issuer_cik']),
            str(event['effective_asof']),str(event['event_type']),str(event['reason']),
        )
        conn.execute('''UPDATE sec_filing_company_association_event
            SET event_sha256=? WHERE event_id=?''',(digest,int(event['event_id'])))
        conn.execute('DROP TRIGGER trg_stage4_association_event_no_delete')
        conn.execute('''DELETE FROM sec_filing_company_association_event
            WHERE event_id=?''',(int(event['event_id']),))
        conn.commit()
        with pytest.raises(RuntimeError,match='lifecycle events; missing=1'):
            filing_rows(
                conn,model_family='consumer_defensive',asof_date='2025-12-31',
                tickers=['KO'],accessions=[SHARED],supported_forms=('425',),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256=expected,
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ('column', 'replacement'),
    [
        ('primary_document', 'changed.htm'),
        ('content_sha256', '0' * 64),
        ('hydration_status', 'parse_unavailable'),
    ],
)
def test_v8_document_bridge_material_mutation_invalidates_parser_seal(
    tmp_path: Path, column: str, replacement: str,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'v7-doc-' + column)
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    nested_document = 'xslF345X06/ko.xml'
    provider = Provider({'KO': _submissions(
        form='10-K', accepted='2024-04-30T12:00:00Z', document=nested_document
    )})
    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert result['full_scope_reconciled'] is True
        assert result['documents'] == 1
        expected = stage4_module._sec_ingestion_config_sha256(
            bundle.payload['sec_fundamentals']
        )
        conn.execute('''UPDATE bridge_sec_filing_document_company
            SET updated_at='2099-01-01T00:00:00Z' WHERE issuer_ticker='KO' ''')
        conn.commit()
        assert conn.execute('''SELECT trust_state
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[0] == (
                'trusted_current'
            )

        conn.execute(
            f'''UPDATE bridge_sec_filing_document_company SET {column}=?
                WHERE issuer_ticker='KO' ''',
            (replacement,),
        )
        conn.commit()
        assert conn.execute('''SELECT trust_state
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[0] == (
                'invalidated_by_mutation'
            )
        assert conn.execute('''SELECT COUNT(*)
            FROM consumer_defensive_sec_cache_snapshot''').fetchone()[0] == 1
        with pytest.raises(RuntimeError, match='reconciliation seal'):
            filing_rows(
                conn, model_family='consumer_defensive', asof_date='2025-12-31',
                tickers=['KO'], accessions=[SHARED], supported_forms=('10-K',),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256=expected,
            )
    finally:
        conn.close()


def test_catalog_recomputes_pit_association_manifest_when_triggers_bypassed(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'catalog-association-manifest')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='10-K', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )})
    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert result['full_scope_reconciled'] is True
        expected = stage4_module._sec_ingestion_config_sha256(
            bundle.payload['sec_fundamentals']
        )
        conn.execute('DROP TRIGGER trg_sec_bridge_invalidate_reconciliation_delete')
        conn.execute('DROP TRIGGER trg_stage4_document_bridge_invalidate_delete')
        conn.execute('''DELETE FROM bridge_sec_filing_company
            WHERE issuer_ticker='KO' ''')
        conn.commit()
        assert conn.execute('''SELECT trust_state
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[0] == (
                'trusted_current'
            )
        with pytest.raises(RuntimeError, match='association manifest'):
            filing_rows(
                conn, model_family='consumer_defensive',
                asof_date='2025-12-31', tickers=['KO'],
                accessions=[SHARED], supported_forms=('10-K',),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256=expected,
            )
    finally:
        conn.close()


def test_v8_same_day_lifecycle_event_invalidates_then_filters_at_eod(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'v7-same-day-event')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )})
    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert result['full_scope_reconciled'] is True
        expected = stage4_module._sec_ingestion_config_sha256(
            bundle.payload['sec_fundamentals']
        )
        sealed = conn.execute('''SELECT *
            FROM consumer_defensive_sec_reconciliation_state
            WHERE asof_date='2025-12-31' ''').fetchone()
        assert sealed is not None
        company_id = int(conn.execute('''SELECT issuer_company_id
            FROM bridge_sec_filing_company WHERE issuer_ticker='KO' ''').fetchone()[0])

        def restore_reconciliation() -> None:
            conn.execute('''UPDATE consumer_defensive_sec_reconciliation_state
                SET trust_state='trusted_current',quarantine_reason=NULL
                WHERE asof_date='2025-12-31' ''')
            conn.commit()

        stage4_module._append_association_event(
            conn, accession=SHARED, company_id=company_id, ticker='KO',
            cik=CIKS['KO'], effective_asof='2025-12-31T12:00:00Z',
            event_type='retired', reason='same_day_retirement_regression',
        )
        conn.commit()
        assert conn.execute('''SELECT trust_state
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[0] == (
                'invalidated_by_mutation'
            )
        with pytest.raises(RuntimeError, match='reconciliation seal'):
            filing_rows(
                conn, model_family='consumer_defensive', asof_date='2025-12-31',
                tickers=['KO'], accessions=[SHARED], supported_forms=('425',),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256=expected,
            )

        restore_reconciliation()
        with pytest.raises(RuntimeError, match='association manifest'):
            filing_rows(
                conn, model_family='consumer_defensive',
                asof_date='2025-12-31', tickers=['KO'],
                accessions=[SHARED], supported_forms=('425',),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256=expected,
            )

        stage4_module._append_association_event(
            conn, accession=SHARED, company_id=company_id, ticker='KO',
            cik=CIKS['KO'], effective_asof='2025-12-31T20:00:00Z',
            event_type='reactivated', reason='same_day_reactivation_regression',
        )
        conn.commit()
        assert conn.execute('''SELECT trust_state
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[0] == (
                'invalidated_by_mutation'
            )
        restore_reconciliation()
        reactivated = filing_rows(
            conn, model_family='consumer_defensive', asof_date='2025-12-31',
            tickers=['KO'], accessions=[SHARED], supported_forms=('425',),
            max_filings_per_ticker=1,
            expected_ingestion_config_sha256=expected,
        )
        assert [row.accession_number for row in reactivated['KO']] == [SHARED]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ('target', 'sql'),
    [
        (
            'filing',
            "UPDATE fact_sec_filing SET accepted_at='2025-06-01T12:00:00Z' "
            "WHERE accession_number=?",
        ),
        (
            'currency',
            "UPDATE dim_company SET reporting_currency='EUR' "
            "WHERE primary_ticker='KO'",
        ),
    ],
)
def test_v8_parser_semantic_mutation_invalidates_trusted_seal(
    tmp_path: Path, target: str, sql: str,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'v8-semantic-' + target)
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )})
    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert result['full_scope_reconciled'] is True
        expected = stage4_module._sec_ingestion_config_sha256(
            bundle.payload['sec_fundamentals']
        )
        conn.execute('''UPDATE fact_sec_filing
            SET updated_at='2099-01-01T00:00:00Z'
            WHERE accession_number=?''',(SHARED,))
        conn.commit()
        assert conn.execute('''SELECT trust_state
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[0] == (
                'trusted_current'
            )
        conn.execute(sql, (SHARED,) if '?' in sql else ())
        conn.commit()
        state = conn.execute('''SELECT trust_state,quarantine_reason
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()
        snapshot_state = conn.execute('''SELECT trust_state,quarantine_reason
            FROM consumer_defensive_sec_cache_snapshot''').fetchone()
        if target == 'currency':
            assert state is None
            assert tuple(snapshot_state) == (
                'quarantined_scope_change',
                'dim_company_semantic_scope_update',
            )
        else:
            assert tuple(state) == (
                'invalidated_by_mutation', 'fact_sec_filing_update'
            )
            assert snapshot_state[0] == 'trusted_current'
        with pytest.raises(RuntimeError, match='reconciliation seal'):
            filing_rows(
                conn, model_family='consumer_defensive',
                asof_date='2025-12-31', tickers=['KO'],
                accessions=[SHARED], supported_forms=('425',),
                max_filings_per_ticker=1,
                expected_ingestion_config_sha256=expected,
            )
    finally:
        conn.close()


def test_consumer_defensive_direct_filings_require_exact_active_pit_identity(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'direct-filing-pit')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )})
    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert result['full_scope_reconciled'] is True
        valid = FilingRef(
            ticker='KO', cik=CIKS['KO'], archive_cik=CIKS['KO'],
            accession_number=SHARED, form_type='425',
            filing_date='2024-04-30', accepted_at='2024-04-30T12:00:00Z',
            report_date='2024-03-31', primary_document='ko.htm',
            source_id='sec_submissions',
        )
        _validate_consumer_defensive_direct_filings(
            conn, direct_filings={('KO', SHARED): valid},
            asof_date='2025-12-31',
        )

        def changed(**updates: str) -> FilingRef:
            values = dict(vars(valid))
            values.update(updates)
            return FilingRef(**values)

        invalid = (
            (('PEP', SHARED), changed(ticker='PEP')),
            (('KO', '0000021344-24-999999'), changed(
                accession_number='0000021344-24-999999'
            )),
        )
        for key, filing in invalid:
            with pytest.raises(ValueError, match='not an active PIT association'):
                _validate_consumer_defensive_direct_filings(
                    conn, direct_filings={key: filing},
                    asof_date='2025-12-31',
                )
        with pytest.raises(ValueError, match='metadata does not match'):
            _validate_consumer_defensive_direct_filings(
                conn,
                direct_filings={('KO', SHARED): changed(form_type='10-K')},
                asof_date='2025-12-31',
            )

        company_id = int(conn.execute('''SELECT issuer_company_id
            FROM bridge_sec_filing_company WHERE issuer_ticker='KO' ''').fetchone()[0])
        stage4_module._append_association_event(
            conn, accession=SHARED, company_id=company_id, ticker='KO',
            cik=CIKS['KO'], effective_asof='2025-12-31T12:00:00Z',
            event_type='retired', reason='direct_manifest_pit_regression',
        )
        conn.commit()
        with pytest.raises(ValueError, match='not an active PIT association'):
            _validate_consumer_defensive_direct_filings(
                conn, direct_filings={('KO', SHARED): valid},
                asof_date='2025-12-31',
            )
    finally:
        conn.close()


def test_direct_documents_bind_to_exact_stage4_seal_before_planning(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'direct-document-seal')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    nested_document = 'xslF345X06/ko.xml'
    provider = Provider({'KO': _submissions(
        form='10-K', accepted='2024-04-30T12:00:00Z', document=nested_document
    )})
    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert result['full_scope_reconciled'] is True
        snapshot = conn.execute('''SELECT seal_relative_path,cache_manifest_json
            FROM consumer_defensive_sec_cache_snapshot
            WHERE asof_date='2025-12-31' ''').fetchone()
        entries = {
            item['logical_path']: item for item in json.loads(str(snapshot[1]))
        }
        logical = f'filings/{CIKS["KO"]}/{SHARED}/{nested_document}'
        entry = entries[logical]
        cache_dir = Path(bundle.payload['sec_fundamentals']['cache_dir'])
        sealed = (cache_dir / str(snapshot[0]) / entry['object_path']).resolve()
        local = tmp_path / 'copied-sealed.htm'
        shutil.copyfile(sealed, local)
        filing = FilingRef(
            ticker='KO', cik=CIKS['KO'], archive_cik=CIKS['KO'],
            accession_number=SHARED, form_type='10-K',
            filing_date='2024-04-30', accepted_at='2024-04-30T12:00:00Z',
            report_date='2024-03-31', primary_document=nested_document,
            source_id='sec_submissions', company_currency='USD',
        )

        def document(
            path: Path, *, name: str = nested_document, primary: bool = True,
            full_submission: bool = False,
        ) -> DocumentRef:
            raw = path.read_bytes()
            stat = path.stat()
            return DocumentRef(
                name=name, path=str(path),
                content_sha256=hashlib.sha256(raw).hexdigest(),
                file_size=len(raw), modified_ns=stat.st_mtime_ns,
                is_primary=primary, is_full_submission=full_submission,
            )

        direct_filings = {('KO', SHARED): filing}
        direct_documents = {('KO', SHARED): (
            document(local, full_submission=True),
        )}
        rebound = _validate_consumer_defensive_direct_documents(
            conn, direct_filings=direct_filings,
            direct_documents=direct_documents, asof_date='2025-12-31',
            cache_dir=cache_dir,
        )
        assert Path(rebound[('KO', SHARED)][0].path) == sealed
        assert rebound[('KO', SHARED)][0].source_kind == 'stage4_sealed_cas'
        assert rebound[('KO', SHARED)][0].is_full_submission is False

        config_hash = stage4_module._sec_ingestion_config_sha256(
            bundle.payload['sec_fundamentals']
        )
        registry = AdapterRegistry(
            model_family='consumer_defensive', adapter_version='test_v1',
            supported_forms=('10-K',),
            source_metrics=(MetricRequest('test_metric'),),
            metric_dependencies={}, document_keywords=('test',),
        )
        plan, summary = build_plan(
            conn, registry=registry,
            adapter_path='consumer_defensive.adapters:extract',
            asof_date='2025-12-31', cache_dir=cache_dir,
            tickers=['KO'], force=True, all_metrics=True,
            direct_filings=direct_filings, direct_documents=direct_documents,
            expected_ingestion_config_sha256=config_hash,
            catalog_documents_enabled=False,
        )
        assert summary.scheduled_accessions == 1
        assert len(plan) == 1
        assert Path(plan[0].documents[0].path) == sealed
        assert plan[0].documents[0].is_full_submission is False
        audit = audit_cache_completeness(
            conn, registry=registry,
            adapter_path='consumer_defensive.adapters:extract',
            asof_date='2025-12-31', cache_dir=cache_dir,
            tickers=['KO'], force=True, all_metrics=True,
            direct_filings=direct_filings, direct_documents=direct_documents,
            expected_ingestion_config_sha256=config_hash,
        )
        assert audit.scheduled_accessions == 1
        with pytest.raises(RuntimeError, match='paired direct_filings'):
            build_plan(
                conn, registry=registry,
                adapter_path='consumer_defensive.adapters:extract',
                asof_date='2025-12-31', cache_dir=cache_dir,
                force=True, all_metrics=True, direct_filings=None,
                direct_documents=direct_documents,
                expected_ingestion_config_sha256=config_hash,
            )
        with pytest.raises(RuntimeError, match='paired immutable'):
            audit_cache_completeness(
                conn, registry=registry,
                adapter_path='consumer_defensive.adapters:extract',
                asof_date='2025-12-31', cache_dir=cache_dir,
                force=True, all_metrics=True, direct_filings=direct_filings,
                direct_documents=None,
                expected_ingestion_config_sha256=config_hash,
            )

        arbitrary = tmp_path / 'arbitrary.htm'
        arbitrary.write_bytes(b'<html>caller substituted bytes</html>')
        with pytest.raises(ValueError, match='exact Stage4 seal'):
            _validate_consumer_defensive_direct_documents(
                conn, direct_filings=direct_filings,
                direct_documents={('KO', SHARED): (document(arbitrary),)},
                asof_date='2025-12-31', cache_dir=cache_dir,
            )
        with pytest.raises(ValueError, match='active hydrated PIT document'):
            _validate_consumer_defensive_direct_documents(
                conn, direct_filings=direct_filings,
                direct_documents={('KO', SHARED): (
                    document(local, name='other.htm'),
                )}, asof_date='2025-12-31', cache_dir=cache_dir,
            )
        with pytest.raises(ValueError, match='exactly one PIT primary'):
            _validate_consumer_defensive_direct_documents(
                conn, direct_filings=direct_filings,
                direct_documents={('KO', SHARED): (
                    document(local, primary=False),
                )}, asof_date='2025-12-31', cache_dir=cache_dir,
            )
        with pytest.raises(ValueError, match='metadata does not match'):
            _validate_consumer_defensive_direct_filings(
                conn, direct_filings={('KO', SHARED): FilingRef(
                    **{**vars(filing), 'company_currency': 'EUR'}
                )}, asof_date='2025-12-31',
            )
    finally:
        conn.close()


def test_populated_legacy_bootstrap_backfills_documents_and_is_idempotent(tmp_path: Path):
    bundle, conn = _prepared_db(tmp_path, "migration")
    accession = "0000000000-24-000999"
    try:
        company_id = conn.execute("SELECT company_id FROM dim_company WHERE primary_ticker='KO'").fetchone()[0]
        conn.execute(
            """INSERT INTO fact_sec_filing(
                   accession_number,company_id,ticker,cik,form_type,filing_date,accepted_at,
                   report_date,primary_document,source_id,source_url,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (accession, company_id, "KO", "21344", "10-K", "2024-02-20",
             "2024-02-20T16:00:00Z", "2023-12-31", "ko.htm", "sec_submissions",
             "https://wrong.example/mixed", "2024-02-20T16:00:00Z", "2024-02-20T16:00:00Z"),
        )
        conn.execute(
            """INSERT INTO fact_sec_filing_document VALUES(
                   ?,?,?,?,?,?,?,?,?,?,?)""",
            (accession, "KO", "10-K", "2024-02-20T16:00:00Z", "ko.htm",
             "https://wrong.example/document", "abc", "cache/ko.htm", "hydrated",
             "sec_inline_xbrl_fallback", "2024-02-20T16:00:00Z"),
        )
        conn.commit()
        ensure_stage4_schema(conn)
        ensure_stage4_schema(conn)
        bridge = conn.execute(
            "SELECT source_url FROM bridge_sec_filing_company WHERE accession_number=?", (accession,)
        ).fetchall()
        assert len(bridge) == 1
        assert bridge[0][0] == (
            "https://www.sec.gov/Archives/edgar/data/21344/000000000024000999/ko.htm"
        )
        documents = conn.execute(
            "SELECT issuer_ticker,source_url FROM bridge_sec_filing_document_company WHERE accession_number=?",
            (accession,),
        ).fetchall()
        assert [tuple(row) for row in documents] == [("KO", bridge[0][0])]
    finally:
        conn.close()


def test_stage4_migration_history_is_cumulative_and_tamper_evident(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'migration-ledger')
    try:
        expected = [
            (version, name, checksum, 'complete')
            for version, name, checksum in STAGE4_MIGRATION_HISTORY
        ]
        assert [tuple(row) for row in conn.execute(
            '''SELECT migration_version,migration_name,migration_sha256,status
               FROM consumer_defensive_stage4_schema_migration
               ORDER BY migration_version'''
        )] == expected
        ensure_stage4_schema(conn)
        assert conn.execute(
            'SELECT COUNT(*) FROM consumer_defensive_stage4_schema_migration'
        ).fetchone()[0] == len(expected)

        conn.execute(
            'DELETE FROM consumer_defensive_stage4_schema_migration '
            'WHERE migration_version IN (3,4)'
        )
        conn.commit()
        with pytest.raises(RuntimeError, match='gap or future version'):
            ensure_stage4_schema(conn)
        assert conn.execute(
            'SELECT COUNT(*) FROM consumer_defensive_stage4_schema_migration'
        ).fetchone()[0] == len(expected) - 2
        conn.executemany(
            '''INSERT INTO consumer_defensive_stage4_schema_migration(
                   migration_version,migration_name,migration_sha256,status,applied_at)
               VALUES(?,?,?,'complete','2025-01-01')''',
            [(version,name,checksum) for version,name,checksum in STAGE4_MIGRATION_HISTORY
             if version in (3,4)],
        )
        conn.commit()

        conn.execute(
            '''UPDATE consumer_defensive_stage4_schema_migration
               SET migration_sha256='tampered' WHERE migration_version=3'''
        )
        conn.commit()
        with pytest.raises(RuntimeError, match='checksum mismatch'):
            ensure_stage4_schema(conn)
        assert conn.execute(
            '''SELECT migration_sha256
               FROM consumer_defensive_stage4_schema_migration
               WHERE migration_version=3'''
        ).fetchone()[0] == 'tampered'
    finally:
        conn.close()


def test_v6_watermark_backfill_uses_latest_existing_stage4_or_parser_asof(
    tmp_path: Path,
) -> None:
    _bundle, conn = _prepared_db(tmp_path,'watermark-migration-backfill')
    try:
        conn.execute('''INSERT INTO sec_parser_run(
            model_family,asof_date,parser_release,adapter_version,mode,
            worker_count,started_at,status)
            VALUES('consumer_defensive','2026-01-03','test','test','shadow',
                   1,'2026-01-03T00:00:00Z','failed')''')
        conn.execute('''DELETE FROM consumer_defensive_sec_ingestion_watermark''')
        conn.execute('''DELETE FROM consumer_defensive_stage4_schema_migration
            WHERE migration_version>=6''')
        conn.commit()
        ensure_stage4_schema(conn)
        row = conn.execute('''SELECT asof_date,cutoff,mutation_kind
            FROM consumer_defensive_sec_ingestion_watermark
            WHERE model_family='consumer_defensive' ''').fetchone()
        assert tuple(row) == (
            '2026-01-03','2026-01-03T23:59:59Z','v6_conservative_backfill'
        )
    finally:
        conn.close()


def test_v8_upgrade_quarantines_then_rebuilds_current_snapshot_trust(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'v7-same-date-rebuild')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='10-K', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )})
    try:
        first = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert first['full_scope_reconciled'] is True
        conn.execute('''UPDATE consumer_defensive_sec_cache_snapshot
            SET scope_contract_version=2,
                trust_state='quarantined_legacy_scope_v2',
                quarantine_reason='simulated_pre_v8' ''')
        conn.execute('''DELETE FROM consumer_defensive_stage4_schema_migration
            WHERE migration_version>=7''')
        conn.commit()
        ensure_stage4_schema(conn)
        assert conn.execute('''SELECT COUNT(*)
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[0] == 0
        assert conn.execute('''SELECT COUNT(*)
            FROM consumer_defensive_sec_cache_snapshot''').fetchone()[0] == 1
        assert conn.execute('''SELECT scope_contract_version,trust_state
            FROM consumer_defensive_sec_cache_snapshot''').fetchone()[:] == (
                2, 'quarantined_legacy_scope_v2'
            )

        rebuilt = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert rebuilt['failures'] == []
        assert rebuilt['full_scope_reconciled'] is True
        assert conn.execute('''SELECT COUNT(*)
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[0] == 1
        assert conn.execute('''SELECT COUNT(*)
            FROM consumer_defensive_sec_cache_snapshot''').fetchone()[0] == 1
        assert conn.execute('''SELECT scope_contract_version,trust_state
            FROM consumer_defensive_sec_cache_snapshot''').fetchone()[:] == (
                3, 'trusted_current'
            )
        replay = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert replay['cache_manifest'] == {'immutable_replay': True}
    finally:
        conn.close()


def test_v8_scope_quarantine_rehabilitates_from_exact_seal_without_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'v8-scope-sealed-rehab')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='10-K', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )})
    try:
        first = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert first['full_scope_reconciled'] is True
        cache = Path(bundle.payload['sec_fundamentals']['cache_dir'])
        mutable_before = {
            path.relative_to(cache).as_posix(): path.read_bytes()
            for path in cache.rglob('*')
            if path.is_file()
            and path.relative_to(cache).parts[0] not in {'objects', 'sealed'}
        }
        conn.execute('''UPDATE dim_company SET reporting_currency='EUR'
            WHERE primary_ticker='KO' ''')
        conn.commit()
        assert conn.execute('''SELECT trust_state
            FROM consumer_defensive_sec_cache_snapshot''').fetchone()[0] == (
                'quarantined_scope_change'
            )
        assert conn.execute('''SELECT COUNT(*)
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[0] == 0

        monkeypatch.setenv('CONSUMER_DEFENSIVE_CACHE_ONLY', '1')

        def provider_must_not_run(_url: str) -> bytes:
            raise AssertionError('provider called during exact-seal rehabilitation')

        rebuilt = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True,
            fetch=provider_must_not_run,
        )
        assert rebuilt['failures'] == []
        assert rebuilt['full_scope_reconciled'] is True
        assert conn.execute('''SELECT scope_contract_version,trust_state
            FROM consumer_defensive_sec_cache_snapshot''').fetchone()[:] == (
                3, 'trusted_current'
            )
        assert conn.execute('''SELECT scope_contract_version,trust_state
            FROM consumer_defensive_sec_reconciliation_state''').fetchone()[:] == (
                3, 'trusted_current'
            )
        mutable_after = {
            path.relative_to(cache).as_posix(): path.read_bytes()
            for path in cache.rglob('*')
            if path.is_file()
            and path.relative_to(cache).parts[0] not in {'objects', 'sealed'}
        }
        assert mutable_after == mutable_before
    finally:
        conn.close()


def test_v8_rehabilitation_manifest_conflict_is_zero_mutation(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'v8-rehab-conflict')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='10-K', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )})
    try:
        first = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert first['full_scope_reconciled'] is True
        snapshot = conn.execute('''SELECT cache_manifest_json
            FROM consumer_defensive_sec_cache_snapshot''').fetchone()
        entries = json.loads(str(snapshot[0]))
        extra = dict(entries[0])
        extra['logical_path'] = 'unused/extra.json'
        entries.append(extra)
        entries.sort(key=lambda row: row['logical_path'])
        manifest_json = json.dumps(
            entries, sort_keys=True, separators=(',', ':')
        )
        manifest_sha = hashlib.sha256(manifest_json.encode()).hexdigest()
        conn.execute('''UPDATE consumer_defensive_sec_cache_snapshot
            SET cache_manifest_json=?,cache_manifest_sha256=?,
                trust_state='quarantined_scope_change',
                quarantine_reason='adversarial_extra_manifest_entry'
            WHERE asof_date='2025-12-31' ''',(manifest_json,manifest_sha))
        conn.execute('''DELETE FROM consumer_defensive_sec_reconciliation_state''')
        conn.commit()
        cache = Path(bundle.payload['sec_fundamentals']['cache_dir'])
        files_before = {
            path.relative_to(cache).as_posix(): path.read_bytes()
            for path in cache.rglob('*') if path.is_file()
        }
        database_before = tuple(conn.iterdump())
        watermark_before = tuple(conn.execute('''SELECT *
            FROM consumer_defensive_sec_ingestion_watermark''').fetchone())

        def provider_must_not_run(_url: str) -> bytes:
            raise AssertionError('provider called during rehabilitation preflight')

        with pytest.raises(
            RuntimeError, match='Immutable SEC cache snapshot conflict'
        ):
            sync_sec_fundamentals(
                conn, bundle, as_of='2025-12-31', force_refresh=True,
                fetch=provider_must_not_run,
            )
        assert tuple(conn.iterdump()) == database_before
        assert tuple(conn.execute('''SELECT *
            FROM consumer_defensive_sec_ingestion_watermark''').fetchone()) == (
                watermark_before
            )
        assert {
            path.relative_to(cache).as_posix(): path.read_bytes()
            for path in cache.rglob('*') if path.is_file()
        } == files_before
    finally:
        conn.close()


def test_true_v2_fixture_upgrades_atomically_and_idempotently(tmp_path: Path) -> None:
    conn = connect(tmp_path / 'true-v2.sqlite')
    try:
        init_db(conn)
        with conn:
            stage4_module._stage4_migration_v2(conn)
            conn.execute('''CREATE TABLE consumer_defensive_stage4_schema_migration(
                migration_version INTEGER PRIMARY KEY,migration_name TEXT NOT NULL,
                migration_sha256 TEXT NOT NULL,status TEXT NOT NULL,
                applied_at TEXT NOT NULL)''')
            version,name,checksum = STAGE4_MIGRATION_HISTORY[0]
            conn.execute('''INSERT INTO consumer_defensive_stage4_schema_migration
                VALUES(?,?,?,'complete','2025-01-01')''',(version,name,checksum))
        assert conn.execute('''SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='consumer_defensive_sec_cache_snapshot' ''').fetchone() is None
        original_v3 = stage4_module._STAGE4_MIGRATION_UNITS[3]

        def fail_v3(db: sqlite3.Connection) -> None:
            db.execute('CREATE TABLE migration_rollback_probe(value INTEGER)')
            raise RuntimeError('injected-v3-failure')

        stage4_module._STAGE4_MIGRATION_UNITS[3] = fail_v3
        try:
            with pytest.raises(RuntimeError,match='injected-v3-failure'):
                ensure_stage4_schema(conn)
        finally:
            stage4_module._STAGE4_MIGRATION_UNITS[3] = original_v3
        assert conn.execute('''SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='migration_rollback_probe' ''').fetchone() is None
        assert conn.execute(
            'SELECT COUNT(*) FROM consumer_defensive_stage4_schema_migration'
        ).fetchone()[0] == 1
        ensure_stage4_schema(conn)
        before = [tuple(row) for row in conn.execute('''SELECT migration_version,
            migration_name,migration_sha256,status
            FROM consumer_defensive_stage4_schema_migration ORDER BY migration_version''')]
        ensure_stage4_schema(conn)
        after = [tuple(row) for row in conn.execute('''SELECT migration_version,
            migration_name,migration_sha256,status
            FROM consumer_defensive_stage4_schema_migration ORDER BY migration_version''')]
        assert before == after == [
            (version,name,checksum,'complete')
            for version,name,checksum in STAGE4_MIGRATION_HISTORY
        ]
    finally:
        conn.close()


def test_full_scope_reconciliation_seal_is_idempotent_and_detects_tamper(tmp_path: Path):
    provider = Provider({
        "KO": _submissions(form="425", accepted="2024-04-30T12:00:00Z", document="ko.htm"),
        "PEP": _submissions(form="425", accepted="2024-04-30T17:00:00Z", document="pep.htm"),
    })
    bundle, conn = _prepared_db(tmp_path, "reconciliation-seal")
    try:
        conn.execute(
            "DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker NOT IN ('KO','PEP')"
        )
        conn.commit()
        first = sync_sec_fundamentals(
            conn, bundle, as_of="2025-12-31", force_refresh=True, fetch=provider
        )
        assert first["failures"] == [] and first["full_scope_reconciled"] is True
        seal = tuple(conn.execute(
            """SELECT scope_issuer_count,association_count,accession_count,
                      shared_accession_count,association_sha256,status
               FROM consumer_defensive_sec_reconciliation_state
               WHERE asof_date='2025-12-31'"""
        ).fetchone())
        assert seal[:4] == (2, 2, 1, 1)
        assert len(seal[4]) == 64 and seal[5] == "complete"
        second = sync_sec_fundamentals(
            conn, bundle, as_of="2025-12-31", force_refresh=True, fetch=provider
        )
        assert second["association_manifest"] == first["association_manifest"]
        assert conn.execute(
            "SELECT COUNT(*) FROM consumer_defensive_sec_reconciliation_state"
        ).fetchone()[0] == 1
        assert validate_stage4(conn, bundle, as_of="2025-12-31")["checks"][
            "filing_association_reconciliation_complete"
        ] is True
        conn.execute(
            """UPDATE bridge_sec_filing_company SET primary_document='tampered.htm'
               WHERE accession_number=? AND issuer_ticker='KO'""", (SHARED,)
        )
        conn.commit()
        assert validate_stage4(conn, bundle, as_of="2025-12-31")["checks"][
            "filing_association_reconciliation_complete"
        ] is False
    finally:
        conn.close()


def test_consumer_defensive_promotion_preflight_has_zero_mutations() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    registry = AdapterRegistry(
        model_family="consumer_defensive",
        adapter_version="test",
        supported_forms=(),
        source_metrics=(),
        metric_dependencies={},
        document_keywords=(),
    )
    before_changes = conn.total_changes
    before_schema = list(conn.execute("SELECT name FROM sqlite_master ORDER BY name"))
    try:
        with pytest.raises(RuntimeError, match="storage adapter is implemented"):
            promote_run(
                conn, run_id=1, registry=registry,
                source_id="shared_dedicated_sec_parser",
            )
        assert conn.total_changes == before_changes
        assert list(conn.execute("SELECT name FROM sqlite_master ORDER BY name")) == before_schema
    finally:
        conn.close()


def test_empty_explicit_scope_is_not_a_full_reconciliation(tmp_path: Path) -> None:
    bundle, conn = _prepared_db(tmp_path, "empty-scope")
    try:
        result = sync_sec_fundamentals(
            conn, bundle, tickers=[], as_of="2025-12-31",
            fetch=lambda url: (_ for _ in ()).throw(AssertionError(url)),
        )
        assert result["issuers"] == 0
        assert result["full_scope_reconciled"] is False
        assert conn.execute(
            "SELECT COUNT(*) FROM consumer_defensive_sec_reconciliation_state"
        ).fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ('payload_cik', 'expected_error'),
    [('77476', 'CIK mismatch'), (None, 'missing required root CIK')],
)
def test_invalid_cik_payload_preserves_last_good_mutable_cache(
    tmp_path: Path, payload_cik: str | None, expected_error: str
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'wrong-cik')
    conn.execute(
        'DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>?', ('KO',)
    )
    conn.commit()
    good = _submissions(form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm')
    cache_path = (
        Path(bundle.payload['sec_fundamentals']['cache_dir'])
        / 'submissions' / 'CIK0000021344.json'
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    good_bytes = json.dumps(good).encode()
    cache_path.write_bytes(good_bytes)

    def fetch(url: str) -> bytes:
        wrong = copy.deepcopy(good)
        if payload_cik is not None:
            wrong['cik'] = payload_cik
        return json.dumps(wrong).encode()

    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', fetch=fetch
        )
        assert expected_error in result['failures'][0]['error']
        assert result['full_scope_reconciled'] is False
        assert conn.execute(
            'SELECT COUNT(*) FROM consumer_defensive_sec_reconciliation_state'
        ).fetchone()[0] == 0
        assert cache_path.read_bytes() == good_bytes
    finally:
        conn.close()


def test_acceptance_change_invalidates_derived_rows(tmp_path: Path) -> None:
    provider = Provider({
        'KO': _submissions(form='10-Q', accepted='2024-04-30T12:00:00Z', document='ko.htm'),
        'PEP': _submissions(form='10-Q', accepted='2024-04-30T17:00:00Z', document='pep.htm'),
    })
    bundle, conn = _prepared_db(tmp_path, 'derived-invalidation')
    try:
        _sync(conn, bundle, provider, 'KO')
        conn.execute(
            '''INSERT INTO fact_financial_statement_canonical(
                   ticker,canonical_metric,accession_number,statement_type,
                   period_end,accepted_at,source_id,created_at)
               VALUES('KO','revenue',?,'income','2024-03-31',?,
                      'sec_companyfacts','2024-04-30')''',
            (SHARED, '2024-04-30T12:00:00Z'),
        )
        metric = conn.execute(
            'SELECT metric_id FROM dim_specialized_metric LIMIT 1'
        ).fetchone()[0]
        tax = conn.execute(
            '''SELECT calibration_cohort_id,applicability_subtype
               FROM dim_consumer_defensive_taxonomy WHERE ticker='KO' '''
        ).fetchone()
        conn.execute(
            '''INSERT INTO fact_specialized_metric_disclosure_census(
                   ticker,accession_number,metric_id,calibration_cohort_id,
                   applicability_subtype,accepted_at,form_type,hit_count,
                   matched_terms_json,evidence_json,parser_version,source_id,created_at)
               VALUES(?,?,?,?,?,?,'10-Q',0,'[]','[]','test',
                      'consumer_defensive_disclosure_census','2024-04-30')''',
            ('KO', SHARED, metric, tax[0], tax[1], '2024-04-30T12:00:00Z'),
        )
        conn.execute(
            '''INSERT INTO feature_financial_statement(
                   ticker,asof_date,source_id,created_at)
               VALUES('KO','2024-05-01','sec_companyfacts','2024-05-01')'''
        )
        conn.execute(
            '''INSERT INTO fact_specialized_metric_disclosure_summary(
                   ticker,metric_id,calibration_cohort_id,applicability_subtype,
                   asof_date,applicability_status,filings_searched,
                   filings_with_hits,disclosure_status,parser_version,
                   source_id,updated_at)
               VALUES('KO',?,?,?,'2024-05-01','applicable',1,0,
                      'applicable_no_term_hit','test',
                      'consumer_defensive_disclosure_census','2024-05-01')''',
            (metric, tax[0], tax[1]),
        )
        conn.commit()
        _sync(conn, bundle, provider, 'PEP')
        assert conn.execute(
            'SELECT COUNT(*) FROM fact_financial_statement_canonical WHERE accession_number=?',
            (SHARED,),
        ).fetchone()[0] == 0
        assert conn.execute(
            'SELECT COUNT(*) FROM fact_specialized_metric_disclosure_census WHERE accession_number=?',
            (SHARED,),
        ).fetchone()[0] == 0
        assert conn.execute(
            '''SELECT COUNT(*) FROM feature_financial_statement
               WHERE ticker='KO' '''
        ).fetchone()[0] == 0
        assert conn.execute(
            '''SELECT COUNT(*) FROM fact_specialized_metric_disclosure_summary
               WHERE ticker='KO' '''
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_identical_companyfacts_replay_preserves_raw_fact_ids(tmp_path: Path) -> None:
    provider = Provider(
        {'KO': _submissions(
            form='10-Q', accepted='2024-04-30T12:00:00Z', document='ko.htm'
        ), 'PEP': _submissions(
            form='425', accepted='2024-04-30T12:00:00Z', document='pep.htm'
        )},
        {'KO': _companyfacts(form='10-Q'), 'PEP': {'facts': {}}},
    )
    bundle, conn = _prepared_db(tmp_path, 'raw-id-replay')
    try:
        _sync(conn, bundle, provider, 'KO')
        first = [tuple(row) for row in conn.execute(
            """SELECT raw_fact_id,ticker,accession_number,taxonomy,concept,
                      accepted_at FROM fact_sec_xbrl_fact_raw
               WHERE ticker='KO' ORDER BY raw_fact_id"""
        )]
        _sync(conn, bundle, provider, 'KO')
        second = [tuple(row) for row in conn.execute(
            """SELECT raw_fact_id,ticker,accession_number,taxonomy,concept,
                      accepted_at FROM fact_sec_xbrl_fact_raw
               WHERE ticker='KO' ORDER BY raw_fact_id"""
        )]
        assert first and second == first
    finally:
        conn.close()


@pytest.mark.parametrize('descriptor', [
    '../escape.json','folder/archive.json','', '.', 'C:\\escape.json',
    ' padded.json ','archive.txt','CON.json.',
])
def test_submissions_archive_descriptor_is_fail_closed(
    tmp_path: Path, descriptor: str
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'archive-' + str(abs(hash(descriptor))))
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    payload = _submissions(
        form='425',accepted='2024-04-30T12:00:00Z',document='ko.htm'
    )
    payload['filings']['files'] = [{'name': descriptor}]
    provider = Provider({'KO': payload, 'PEP': {'filings': {'recent': {}, 'files': []}}})
    try:
        result = sync_sec_fundamentals(
            conn,bundle,as_of='2025-12-31',force_refresh=True,fetch=provider
        )
        assert result['failures']
        assert conn.execute(
            'SELECT COUNT(*) FROM fact_sec_filing WHERE accession_number=?',(SHARED,)
        ).fetchone()[0] == 0
        assert conn.execute(
            'SELECT COUNT(*) FROM consumer_defensive_sec_cache_snapshot'
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_duplicate_submissions_archive_descriptor_is_rejected(tmp_path: Path) -> None:
    bundle, conn = _prepared_db(tmp_path, 'duplicate-archive')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    payload = _submissions(
        form='425',accepted='2024-04-30T12:00:00Z',document='ko.htm'
    )
    payload['filings']['files'] = [
        {'name': 'old.json'},{'name': 'OLD.JSON'},
    ]
    try:
        result = sync_sec_fundamentals(
            conn,bundle,as_of='2025-12-31',force_refresh=True,
            fetch=Provider({'KO': payload,'PEP': payload}),
        )
        assert 'Duplicate SEC submissions archive descriptor' in result['failures'][0]['error']
    finally:
        conn.close()


def test_submissions_archive_descriptor_url_quotes_one_validated_segment(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'archive-url-quote')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    payload = _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )
    payload['cik'] = str(int(CIKS['KO']))
    payload['filings']['files'] = [{'name': 'old report%.json'}]
    empty_archive = {
        key: [] for key in (
            'accessionNumber', 'filingDate', 'acceptanceDateTime',
            'reportDate', 'form', 'primaryDocument',
        )
    }
    requested: list[str] = []

    def fetch(url: str) -> bytes:
        requested.append(url)
        if 'companyfacts' in url:
            return json.dumps({'cik': str(int(CIKS['KO'])), 'facts': {}}).encode()
        if url.endswith('old%20report%25.json'):
            return json.dumps(empty_archive).encode()
        if f"CIK{CIKS['KO']}.json" in url:
            return json.dumps(payload).encode()
        raise AssertionError(url)

    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=fetch,
        )
        assert result['failures'] == []
        archive_urls = [url for url in requested if 'old' in url]
        assert len(archive_urls) == 1
        assert archive_urls[0].endswith('/old%20report%25.json')
        assert 'old report%.json' not in archive_urls[0]
    finally:
        conn.close()


@pytest.mark.parametrize(
    'descriptor',
    ['CON.json', 'prn.JSON', 'Aux.json', 'nul.json', 'COM1.json', 'com9.json',
     'LPT1.json', 'lpt9.JSON'],
)
def test_windows_reserved_submissions_archive_descriptor_is_rejected(
    tmp_path: Path, descriptor: str,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'reserved-' + descriptor.casefold())
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    payload = _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )
    payload['filings']['files'] = [{'name': descriptor}]
    provider = Provider({'KO': payload, 'PEP': payload})
    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=provider,
        )
        assert 'Windows-reserved' in result['failures'][0]['error']
        assert conn.execute(
            'SELECT COUNT(*) FROM fact_sec_filing WHERE accession_number=?', (SHARED,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_cache_seal_is_inode_independent_from_mutable_alias(tmp_path: Path) -> None:
    from consumer_defensive.core.stage4 import (
        _seal_cache_manifest, _verify_cache_manifest,
    )

    cache = tmp_path / 'cache'
    alias = cache / 'submissions' / 'one.json'
    alias.parent.mkdir(parents=True)
    original = b'{"version":1}'
    alias.write_bytes(original)
    import hashlib
    entry = {
        'path': 'submissions/one.json', 'bytes': len(original),
        'sha256': hashlib.sha256(original).hexdigest(),
    }
    sealed_root, manifest = _seal_cache_manifest(
        cache, '2026-08-12', [entry]
    )
    sealed_json = json.dumps(manifest['entries'], separators=(',', ':'), sort_keys=True)
    sealed_object = sealed_root / manifest['entries'][0]['object_path']
    alias.write_bytes(b'{"version":2}')
    assert sealed_object.read_bytes() == original
    assert _verify_cache_manifest(sealed_root, sealed_json, manifest['sha256'])


def test_sec_binary_promotion_does_not_clobber_hardlinked_alias_or_legacy_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / 'cache-hardlink'
    cache.mkdir()
    outside_alias = tmp_path / 'outside-alias.txt'
    outside_temp = tmp_path / 'outside-temp.txt'
    outside_alias.write_bytes(b'alias-secret')
    outside_temp.write_bytes(b'temp-secret')
    alias = cache / 'payload.json'
    monkeypatch.setattr(stage4_module.os, 'getpid', lambda: 4242)
    monkeypatch.setattr(stage4_module.time, 'time_ns', lambda: 777)
    legacy_temporary = cache / 'payload.json.4242.777.tmp'
    try:
        stage4_module.os.link(outside_alias, alias)
        stage4_module.os.link(outside_temp, legacy_temporary)
    except OSError as exc:
        pytest.skip(f'hardlinks are unavailable: {exc}')
    stage4_module._atomic_promote_bytes(
        alias, b'published', cache_root=cache
    )
    assert alias.read_bytes() == b'published'
    assert outside_alias.read_bytes() == b'alias-secret'
    assert outside_temp.read_bytes() == b'temp-secret'
    assert legacy_temporary.read_bytes() == b'temp-secret'


def test_sec_mutable_alias_parent_symlink_fails_before_provider_io(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'cache-parent-symlink')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    cache = Path(bundle.payload['sec_fundamentals']['cache_dir'])
    cache.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / 'outside-alias'
    outside.mkdir()
    try:
        (cache / 'submissions').symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        conn.close()
        pytest.skip(f'directory symlinks are unavailable: {exc}')
    requested: list[str] = []

    def fetch(url: str) -> bytes:
        requested.append(url)
        raise AssertionError('provider must not run for an unsafe cache target')

    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=fetch,
        )
        assert result['failures']
        assert 'symlinked or non-directory cache parent' in result['failures'][0]['error']
        assert requested == []
        assert list(outside.iterdir()) == []
    finally:
        conn.close()


def test_cache_seal_and_parser_replay_support_final_object_over_max_path(
    tmp_path: Path,
) -> None:
    payload = b'long-root-seal'
    digest = hashlib.sha256(payload).hexdigest()
    long_root = tmp_path
    expected = long_root / 'sealed' / '2025-12-31' / 'objects' / 'sha256' / digest
    while len(str(expected)) <= 300:
        long_root /= 'long-cache-segment'
        expected = (
            long_root / 'sealed' / '2025-12-31'
            / 'objects' / 'sha256' / digest
        )
    mkdir_path(long_root, parents=True, exist_ok=True)
    alias = long_root / 'submissions' / 'CIK0000021344.json'
    mkdir_path(alias.parent, parents=True, exist_ok=True)
    with open_path(alias, 'wb') as handle:
        handle.write(payload)
    entry = stage4_module._cache_manifest_record(long_root, alias, payload)
    sealed_root, manifest = stage4_module._seal_cache_manifest(
        long_root, '2025-12-31', [entry]
    )
    sealed = sealed_root / manifest['entries'][0]['object_path']
    assert len(str(sealed)) > 260
    assert read_bytes(sealed) == payload
    manifest_json = json.dumps(
        manifest['entries'], sort_keys=True, separators=(',', ':'),
    )
    assert stage4_module._verify_cache_manifest(
        sealed_root, manifest_json, manifest['sha256']
    )

    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE consumer_defensive_sec_cache_snapshot(
        asof_date TEXT PRIMARY KEY,seal_relative_path TEXT,
        cache_manifest_json TEXT,cache_manifest_sha256 TEXT,
        cache_root TEXT,scope_contract_version INTEGER,trust_state TEXT)''')
    conn.execute(
        '''INSERT INTO consumer_defensive_sec_cache_snapshot
           VALUES(?,?,?,?,?,3,'trusted_current')''',
        (
            '2025-12-31', 'sealed/2025-12-31', manifest_json,
            manifest['sha256'], str(sealed_root),
        ),
    )
    lookup = stage4_module._sealed_cache_lookup(
        conn, long_root, '2025-12-31'
    )
    assert read_bytes(lookup[entry['path']]) == payload
    snapshot = conn.execute(
        '''SELECT asof_date,seal_relative_path,cache_manifest_json,
                  cache_manifest_sha256,cache_root
           FROM consumer_defensive_sec_cache_snapshot'''
    ).fetchone()
    assert _cache_seal_valid(snapshot, cache_dir=long_root)
    conn.close()
    assert not list(filesystem_path(sealed.parent).glob('.d.*.tmp'))


@pytest.mark.parametrize('redirected_child', ['sealed', 'objects'])
def test_sec_seal_target_symlink_never_writes_outside(
    tmp_path: Path, redirected_child: str,
) -> None:
    cache = tmp_path / ('cache-' + redirected_child)
    alias = cache / 'submissions' / 'one.json'
    alias.parent.mkdir(parents=True)
    payload = b'{"version":1}'
    alias.write_bytes(payload)
    outside = tmp_path / ('outside-' + redirected_child)
    outside.mkdir()
    try:
        (cache / redirected_child).symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'directory symlinks are unavailable: {exc}')
    entry = {
        'path': 'submissions/one.json', 'bytes': len(payload),
        'sha256': __import__('hashlib').sha256(payload).hexdigest(),
    }
    with pytest.raises(RuntimeError, match='symlinked or non-directory cache parent'):
        stage4_module._seal_cache_manifest(cache, '2026-08-12', [entry])
    assert list(outside.iterdir()) == []


def test_nested_and_blank_primary_document_metadata_is_compatible_and_quoted(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'nested-primary-metadata')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    nested_accession = '0000000000-24-000010'
    blank_accession = '0000000000-24-000011'
    payload = {
        'filings': {'recent': {
            'accessionNumber': [nested_accession, blank_accession],
            'filingDate': ['2024-04-30', '2024-04-29'],
            'acceptanceDateTime': [
                '2024-04-30T12:00:00Z', '2024-04-29T12:00:00Z',
            ],
            'reportDate': ['', ''], 'form': ['425', '425'],
            'primaryDocument': ['xslF345X06/doc 4.xml', ''],
        }, 'files': []},
    }
    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True,
            fetch=Provider({'KO': payload, 'PEP': payload}),
        )
        assert result['failures'] == []
        rows = conn.execute(
            '''SELECT accession_number,source_url FROM bridge_sec_filing_company
               WHERE issuer_ticker='KO' ORDER BY accession_number'''
        ).fetchall()
        assert str(rows[0][1]).endswith('/xslF345X06/doc%204.xml')
        assert rows[1][1] is None
    finally:
        conn.close()


def test_paper_primary_metadata_is_stored_but_not_hydrated(tmp_path: Path) -> None:
    bundle, conn = _prepared_db(tmp_path, 'paper-primary-metadata')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    payload = _submissions(
        form='10-K', accepted='2024-04-30T12:00:00Z', document='legacy.paper'
    )
    requested: list[str] = []
    provider = Provider({'KO': payload, 'PEP': payload})

    def fetch(url: str) -> bytes:
        requested.append(url)
        return provider(url)

    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=fetch,
        )
        assert result['failures'] == []
        assert result['documents'] == 0
        row = conn.execute('''SELECT primary_document,source_url
            FROM bridge_sec_filing_company WHERE issuer_ticker='KO' ''').fetchone()
        assert row[0] == 'legacy.paper'
        assert str(row[1]).endswith('/legacy.paper')
        assert not any(url.endswith('/legacy.paper') for url in requested)
        assert conn.execute('''SELECT COUNT(*)
            FROM bridge_sec_filing_document_company''').fetchone()[0] == 0
    finally:
        conn.close()


def test_conflicting_recent_and_archive_projection_preserves_all_last_good(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'aggregate-conflict')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    recent = _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='recent.htm'
    )
    recent['cik'] = str(int(CIKS['KO']))
    recent['filings']['files'] = [{'name': 'older.json'}]
    archive = {
        'accessionNumber': [SHARED], 'filingDate': ['2024-04-30'],
        'acceptanceDateTime': ['2024-04-30T12:00:00Z'],
        'reportDate': ['2024-03-31'], 'form': ['425'],
        'primaryDocument': ['conflict.htm'],
    }
    companyfacts = {'cik': str(int(CIKS['KO'])), 'facts': {}}
    cache = Path(bundle.payload['sec_fundamentals']['cache_dir'])
    aliases = {
        cache / 'submissions' / f'CIK{CIKS["KO"]}.json': b'old-submissions',
        cache / 'submissions' / 'older.json': b'old-archive',
        cache / 'companyfacts' / f'CIK{CIKS["KO"]}.json': b'old-companyfacts',
    }
    for path, payload in aliases.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url.endswith('/older.json'):
            return json.dumps(archive).encode()
        if 'companyfacts' in url:
            return json.dumps(companyfacts).encode()
        if f'CIK{CIKS["KO"]}.json' in url:
            return json.dumps(recent).encode()
        raise AssertionError(url)

    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True, fetch=fetch,
        )
        assert calls
        assert 'Conflicting staged SEC submissions metadata' in result['failures'][0]['error']
        assert {path: path.read_bytes() for path in aliases} == aliases
        assert conn.execute('''SELECT COUNT(*)
            FROM bridge_sec_filing_company''').fetchone()[0] == 0
        assert conn.execute('''SELECT COUNT(*)
            FROM consumer_defensive_sec_ingestion_watermark''').fetchone()[0] == 0
    finally:
        conn.close()


def test_invalid_hydrated_document_suffix_is_cache_and_database_noop(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'invalid-hydrated-document')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    cache = Path(bundle.payload['sec_fundamentals']['cache_dir'])
    submissions_alias = cache / 'submissions' / 'CIK0000021344.json'
    submissions_alias.parent.mkdir(parents=True, exist_ok=True)
    last_good = b'{"last":"good"}'
    submissions_alias.write_bytes(last_good)
    payload = _submissions(
        form='10-K', accepted='2024-04-30T12:00:00Z', document='bad.exe'
    )
    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True,
            fetch=Provider({'KO': payload, 'PEP': payload}),
        )
        assert result['failures']
        assert 'unsupported suffix' in result['failures'][0]['error']
        assert submissions_alias.read_bytes() == last_good
        assert conn.execute(
            "SELECT COUNT(*) FROM bridge_sec_filing_company WHERE issuer_ticker='KO'"
        ).fetchone()[0] == 0
        assert conn.execute(
            'SELECT COUNT(*) FROM consumer_defensive_sec_cache_snapshot'
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_invalid_archive_payload_preserves_last_good_alias_and_database(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'invalid-archive-payload')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    payload = _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )
    payload['filings']['files'] = [{'name': 'old.json'}]
    cache_root = Path(bundle.payload['sec_fundamentals']['cache_dir'])
    archive_path = cache_root / 'submissions' / 'old.json'
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    last_good = json.dumps({key: [] for key in (
        'accessionNumber', 'filingDate', 'acceptanceDateTime',
        'reportDate', 'form', 'primaryDocument',
    )}).encode()
    archive_path.write_bytes(last_good)

    class InvalidArchiveProvider(Provider):
        def __call__(self, url: str) -> bytes:
            if url.endswith('/old.json'):
                return json.dumps({'accessionNumber': ['truncated']}).encode()
            return super().__call__(url)

    try:
        result = sync_sec_fundamentals(
            conn, bundle, as_of='2025-12-31', force_refresh=True,
            fetch=InvalidArchiveProvider({'KO': payload, 'PEP': payload}),
        )
        assert result['failures']
        assert archive_path.read_bytes() == last_good
        assert conn.execute(
            'SELECT COUNT(*) FROM bridge_sec_filing_company WHERE issuer_ticker=?', ('KO',)
        ).fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ('case_id', 'mutator'),
    [
        ('taxonomy', lambda payload: payload['facts'].update({'us-gaap': []})),
        ('concept', lambda payload: payload['facts']['us-gaap'].update({'Revenues': []})),
        ('units', lambda payload: payload['facts']['us-gaap']['Revenues'].update({'units': []})),
        ('observations', lambda payload: payload['facts']['us-gaap']['Revenues']['units'].update({'USD': {}})),
        ('observation', lambda payload: payload['facts']['us-gaap']['Revenues']['units']['USD'].__setitem__(0, [])),
        ('numeric', lambda payload: payload['facts']['us-gaap']['Revenues']['units']['USD'][0].update({'val': '100'})),
        ('accession', lambda payload: payload['facts']['us-gaap']['Revenues']['units']['USD'][0].update({'accn': 'bad'})),
        ('date', lambda payload: payload['facts']['us-gaap']['Revenues']['units']['USD'][0].update({'filed': '2024-99-99'})),
    ],
)
def test_invalid_nested_companyfacts_preserves_cache_and_has_zero_issuer_mutation(
    tmp_path: Path, case_id: str, mutator,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'invalid-companyfacts-' + case_id)
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    submissions = _submissions(
        form='10-Q', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    )
    good_facts = _companyfacts(form='10-Q')
    invalid_facts = copy.deepcopy(good_facts)
    mutator(invalid_facts)
    cache_root = Path(bundle.payload['sec_fundamentals']['cache_dir'])
    submissions_path = cache_root / 'submissions' / 'CIK0000021344.json'
    companyfacts_path = cache_root / 'companyfacts' / 'CIK0000021344.json'
    submissions_path.parent.mkdir(parents=True, exist_ok=True)
    companyfacts_path.parent.mkdir(parents=True, exist_ok=True)
    old_submissions = b'{"last":"good-submissions"}'
    old_companyfacts = b'{"last":"good-companyfacts"}'
    submissions_path.write_bytes(old_submissions)
    companyfacts_path.write_bytes(old_companyfacts)
    provider = Provider({'KO': submissions, 'PEP': submissions}, {'KO': invalid_facts})
    try:
        result = sync_sec_fundamentals(
            conn, bundle, tickers=['KO'], as_of='2025-12-31',
            force_refresh=True, fetch=provider,
        )
        assert result['failures']
        assert submissions_path.read_bytes() == old_submissions
        assert companyfacts_path.read_bytes() == old_companyfacts
        assert conn.execute(
            'SELECT COUNT(*) FROM bridge_sec_filing_company WHERE issuer_ticker=?', ('KO',)
        ).fetchone()[0] == 0
        assert conn.execute(
            'SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE ticker=?', ('KO',)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_same_date_conflicting_refresh_is_database_and_cache_noop(tmp_path: Path) -> None:
    bundle, conn = _prepared_db(tmp_path, 'same-date-noop')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    first = Provider({'KO': _submissions(
        form='425',accepted='2024-04-30T12:00:00Z',document='ko.htm'
    ),'PEP': _submissions(form='425',accepted='2024-04-30T12:00:00Z',document='pep.htm')})
    conflicting = Provider({'KO': _submissions(
        form='425',accepted='2024-05-02T12:00:00Z',document='changed.htm'
    ),'PEP': _submissions(form='425',accepted='2024-05-02T12:00:00Z',document='changed.htm')})
    try:
        result = sync_sec_fundamentals(
            conn,bundle,as_of='2025-12-31',force_refresh=True,fetch=first
        )
        assert result['failures'] == []
        db_before = [tuple(row) for row in conn.execute(
            'SELECT * FROM fact_sec_filing ORDER BY accession_number'
        )]
        cache = Path(bundle.payload['sec_fundamentals']['cache_dir'])
        cache_before = {
            path.relative_to(cache).as_posix(): path.read_bytes()
            for path in cache.rglob('*') if path.is_file()
        }
        replay = sync_sec_fundamentals(
            conn,bundle,as_of='2025-12-31',force_refresh=True,fetch=conflicting
        )
        assert replay['full_scope_reconciled'] is True
        changed_payload = copy.deepcopy(bundle.payload)
        changed_payload['sec_fundamentals']['documents_per_issuer'] = (
            int(changed_payload['sec_fundamentals']['documents_per_issuer']) + 1
        )
        changed_bundle = ConfigBundle(
            bundle.path,bundle.base_dir,changed_payload
        )
        with pytest.raises(RuntimeError,match='config/scope conflict'):
            sync_sec_fundamentals(
                conn,changed_bundle,as_of='2025-12-31',force_refresh=True,
                fetch=conflicting,
            )
        assert [tuple(row) for row in conn.execute(
            'SELECT * FROM fact_sec_filing ORDER BY accession_number'
        )] == db_before
        assert {
            path.relative_to(cache).as_posix(): path.read_bytes()
            for path in cache.rglob('*') if path.is_file()
        } == cache_before
    finally:
        conn.close()


def test_targeted_sec_watermark_is_monotonic_and_reverse_replay_is_zero_mutation(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'targeted-watermark')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    ), 'PEP': _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='pep.htm'
    )})
    try:
        result = sync_sec_fundamentals(
            conn,bundle,tickers=['KO'],as_of='2025-12-31',
            force_refresh=True,fetch=provider,
        )
        assert result['failures'] == []
        watermark = conn.execute('''SELECT asof_date,mutation_kind
            FROM consumer_defensive_sec_ingestion_watermark
            WHERE model_family='consumer_defensive' ''').fetchone()
        assert tuple(watermark) == (
            '2025-12-31','targeted_financial_projection'
        )
        before = [tuple(row) for row in conn.execute('''SELECT accession_number,
            issuer_company_id,accepted_at,association_status
            FROM bridge_sec_filing_company
            ORDER BY accession_number,issuer_company_id''')]
        calls = 0

        def forbidden_fetch(_url: str) -> bytes:
            nonlocal calls
            calls += 1
            raise AssertionError('reverse replay reached provider')

        with pytest.raises(RuntimeError,match='reverse replay rejected'):
            sync_sec_fundamentals(
                conn,bundle,tickers=['KO'],as_of='2025-12-30',
                fetch=forbidden_fetch,
            )
        assert calls == 0
        after = [tuple(row) for row in conn.execute('''SELECT accession_number,
            issuer_company_id,accepted_at,association_status
            FROM bridge_sec_filing_company
            ORDER BY accession_number,issuer_company_id''')]
        assert after == before
        assert conn.execute('''SELECT asof_date
            FROM consumer_defensive_sec_ingestion_watermark
            WHERE model_family='consumer_defensive' ''').fetchone()[0] == '2025-12-31'
    finally:
        conn.close()


def test_full_sec_watermark_advances_in_final_reconciliation_transaction(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'full-watermark')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    ), 'PEP': _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='pep.htm'
    )})
    try:
        result = sync_sec_fundamentals(
            conn,bundle,as_of='2025-12-31',force_refresh=True,fetch=provider,
        )
        assert result['full_scope_reconciled'] is True
        assert tuple(conn.execute('''SELECT asof_date,mutation_kind
            FROM consumer_defensive_sec_ingestion_watermark
            WHERE model_family='consumer_defensive' ''').fetchone()) == (
                '2025-12-31','full_reconciliation_sealed'
            )
    finally:
        conn.close()


def test_failed_targeted_issuer_transaction_does_not_advance_watermark(
    tmp_path: Path,monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'failed-watermark')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    provider = Provider({'KO': _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='ko.htm'
    ), 'PEP': _submissions(
        form='425', accepted='2024-04-30T12:00:00Z', document='pep.htm'
    )})

    def fail_event(*_args,**_kwargs) -> None:
        raise RuntimeError('injected association transaction failure')

    monkeypatch.setattr(stage4_module,'_append_association_event',fail_event)
    try:
        result = sync_sec_fundamentals(
            conn,bundle,tickers=['KO'],as_of='2025-12-31',
            force_refresh=True,fetch=provider,
        )
        assert 'injected association transaction failure' in result['failures'][0]['error']
        assert conn.execute('''SELECT COUNT(*)
            FROM consumer_defensive_sec_ingestion_watermark''').fetchone()[0] == 0
        assert conn.execute('''SELECT COUNT(*) FROM bridge_sec_filing_company
            WHERE issuer_ticker='KO' ''').fetchone()[0] == 0
    finally:
        conn.close()


def test_companyfacts_s1_s2_s1_has_identical_semantic_artifact_lineage(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path, 'semantic-replay')
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    submissions = {'KO': _submissions(
        form='10-Q',accepted='2024-04-30T12:00:00Z',document='ko.htm'
    ),'PEP': _submissions(form='425',accepted='2024-04-30T12:00:00Z',document='pep.htm')}

    def facts(value: float) -> dict[str, object]:
        payload = _companyfacts(form='10-Q')
        payload['facts']['us-gaap']['Revenues']['units']['USD'][0]['val'] = value
        return payload

    def artifact(asof: str) -> tuple[list[tuple[object, ...]],str]:
        build_financial_features(conn,bundle,as_of=asof)
        canonical = [tuple(row) for row in conn.execute('''SELECT ticker,
            canonical_metric,canonical_component,accession_number,taxonomy,
            source_concept,statement_type,period_start,period_end,accepted_at,
            frequency,value,reported_value,reported_currency,value_usd,fx_rate,
            source_observation_id,source_id,definition_version,quality_status,
            selection_method,sign_normalization_method,quality_flags_json
            FROM fact_financial_statement_canonical ORDER BY ticker,canonical_metric,
            canonical_component,period_end,accepted_at''')]
        lineage = str(conn.execute('''SELECT lineage_json FROM feature_financial_statement
            WHERE ticker='KO' AND asof_date=?''',(asof,)).fetchone()[0])
        return canonical,lineage

    try:
        first_provider = Provider(submissions,{'KO': facts(100.0),'PEP': {'facts': {}}})
        second_provider = Provider(submissions,{'KO': facts(125.0),'PEP': {'facts': {}}})
        assert sync_sec_fundamentals(
            conn,bundle,as_of='2025-12-29',force_refresh=True,fetch=first_provider
        )['failures'] == []
        s1 = artifact('2025-12-29')
        assert sync_sec_fundamentals(
            conn,bundle,as_of='2025-12-30',force_refresh=True,fetch=second_provider
        )['failures'] == []
        s2 = artifact('2025-12-30')
        assert sync_sec_fundamentals(
            conn,bundle,as_of='2025-12-31',force_refresh=True,fetch=first_provider
        )['failures'] == []
        s1_replay = artifact('2025-12-31')
        assert s1[0] == s1_replay[0]
        assert s1[0] != s2[0]
        assert all(len(str(row[16])) == 64 for row in s1[0])
        assert 'raw_fact_id' not in s1_replay[1]
    finally:
        conn.close()


def test_acceptance_reconciliation_recomputes_exact_raw_observation_identity(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path,'acceptance-observation-identity')
    provider = Provider(
        {
            'KO': _submissions(
                form='10-Q',accepted='2024-04-30T12:00:00Z',document='ko.htm'
            ),
            'PEP': _submissions(
                form='10-Q',accepted='2024-04-30T17:00:00Z',document='pep.htm'
            ),
        },
        {'KO': _companyfacts(form='10-Q'),'PEP': {'facts': {}}},
    )
    try:
        _sync(conn,bundle,provider,'KO')
        first = conn.execute('''SELECT accepted_at,source_observation_id
            FROM fact_sec_xbrl_fact_raw WHERE ticker='KO' ''').fetchone()
        assert str(first['accepted_at']) == '2024-04-30T12:00:00Z'
        _sync(conn,bundle,provider,'PEP')
        reconciled = conn.execute('''SELECT accepted_at,source_observation_id
            FROM fact_sec_xbrl_fact_raw WHERE ticker='KO' ''').fetchone()
        assert str(reconciled['accepted_at']) == '2024-04-30T17:00:00Z'
        assert str(reconciled['source_observation_id']) != str(
            first['source_observation_id']
        )
        assert stage4_module._count_raw_observation_identity_mismatches(
            conn,cutoff='2025-12-31T23:59:59Z',
        ) == 0
        conn.execute('''UPDATE fact_sec_xbrl_fact_raw SET value_text='101.0'
            WHERE ticker='KO' ''')
        conn.commit()
        assert stage4_module._count_raw_observation_identity_mismatches(
            conn,cutoff='2025-12-31T23:59:59Z',
        ) == 1
    finally:
        conn.close()


def test_stage4_validator_does_not_mask_actual_foreign_key_violations(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path,'foreign-key-validator')
    try:
        conn.commit()
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('''INSERT INTO fact_sec_xbrl_fact_raw(
            ticker,taxonomy,concept,source_id,created_at)
            VALUES('KO','us-gaap','Revenue','missing_source','2025-01-01')''')
        conn.commit()
        conn.execute('PRAGMA foreign_keys=ON')
        result = validate_stage4(conn,bundle,as_of='2025-12-31')
        assert result['counts']['global_foreign_key_violations'] == 1
        assert result['checks']['foreign_keys_valid'] is False
    finally:
        conn.close()


def test_large_raw_identity_backfill_uses_bounded_keyset_batches(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path,'bounded-raw-backfill')
    del bundle
    row_count = 5_003
    batch_size = 97
    try:
        conn.executemany('''INSERT INTO fact_sec_xbrl_fact_raw(
            ticker,cik,accession_number,taxonomy,concept,value_text,numeric_value,
            unit,period_start,period_end,filed_date,accepted_at,form_type,frame,
            dimensions_json,source_id,source_detail,source_observation_id,created_at)
            VALUES('KO','0000021344',NULL,'us-gaap','Revenue',?,1.0,'USD',
                   '2024-01-01','2024-03-31','2024-04-30',
                   '2024-04-30T12:00:00Z','10-Q',NULL,'{}',
                   'sec_companyfacts','bounded-test',NULL,'2025-01-01')''',
            [(str(index),) for index in range(row_count)],
        )
        conn.commit()
        batch_selects = 0

        def trace(statement: str) -> None:
            nonlocal batch_selects
            normalized = ' '.join(statement.split()).casefold()
            if (
                normalized.startswith('select raw_fact_id')
                and 'from fact_sec_xbrl_fact_raw' in normalized
                and 'limit 97' in normalized
            ):
                batch_selects += 1

        conn.set_trace_callback(trace)
        stage4_module._backfill_source_observation_ids(
            conn,exact=True,batch_size=batch_size,
        )
        conn.set_trace_callback(None)
        assert batch_selects == (row_count + batch_size - 1) // batch_size + 1
        assert conn.execute('''SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw
            WHERE source_observation_id IS NULL
               OR length(source_observation_id)<>64''').fetchone()[0] == 0
    finally:
        conn.set_trace_callback(None)
        conn.close()


def test_large_lifecycle_backfill_uses_bounded_composite_key_batches(
    tmp_path: Path,
) -> None:
    _bundle, conn = _prepared_db(tmp_path,'bounded-lifecycle-backfill')
    row_count = 2_003
    batch_size = 89
    try:
        company_id = int(conn.execute('''SELECT company_id FROM dim_company
            WHERE primary_ticker='KO' ''').fetchone()[0])
        filings = [
            (f'0000021344-24-{index:06d}',index)
            for index in range(1,row_count + 1)
        ]
        conn.executemany('''INSERT INTO fact_sec_filing(
            accession_number,company_id,ticker,cik,form_type,filing_date,
            accepted_at,report_date,primary_document,source_id,source_url,
            content_sha256,created_at,updated_at)
            VALUES(?,NULL,'ACCESSION_NEUTRAL',NULL,'10-Q','2024-04-30',
                   '2024-04-30T12:00:00Z','2024-03-31','ko.htm',
                   'sec_submissions',NULL,NULL,'2025-01-01','2025-01-01')''',
            [(accession,) for accession,_ in filings],
        )
        conn.executemany('''INSERT INTO bridge_sec_filing_company(
            accession_number,issuer_company_id,issuer_ticker,issuer_cik,
            relationship,relationship_evidence,form_type,filing_date,accepted_at,
            report_date,primary_document,source_id,source_url,created_at,updated_at)
            VALUES(?,?,'KO','0000021344','associated_via_submissions',
                   'bounded-test','10-Q','2024-04-30','2024-04-30T12:00:00Z',
                   '2024-03-31','ko.htm','sec_submissions',?,
                   '2025-01-01','2025-01-01')''',
            [
                (
                    accession,company_id,
                    'https://www.sec.gov/Archives/edgar/data/21344/'
                    + accession.replace('-','') + '/ko.htm',
                )
                for accession,_ in filings
            ],
        )
        conn.commit()
        batch_selects = 0

        def trace(statement: str) -> None:
            nonlocal batch_selects
            normalized = ' '.join(statement.split()).casefold()
            if (
                normalized.startswith('select b.accession_number')
                and 'from bridge_sec_filing_company b' in normalized
                and 'limit 89' in normalized
            ):
                batch_selects += 1

        conn.set_trace_callback(trace)
        stage4_module._backfill_association_events(
            conn,batch_size=batch_size,
        )
        conn.set_trace_callback(None)
        assert batch_selects == (row_count + batch_size - 1) // batch_size + 1
        assert conn.execute('''SELECT COUNT(*)
            FROM sec_filing_company_association_event''').fetchone()[0] == row_count
    finally:
        conn.set_trace_callback(None)
        conn.close()
