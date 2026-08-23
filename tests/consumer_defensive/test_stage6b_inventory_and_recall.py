from __future__ import annotations

import json
from pathlib import Path

import pytest

import consumer_defensive.core.specialized_metrics as specialized_metrics
from consumer_defensive.core.db import connect, init_db, utc_now
from consumer_defensive.core.historical_filing_inventory import (
    FilingRow,
    _capture_schedule,
    _parse_event_filing_index_html,
    _parse_event_index,
    _select_event_documents,
    execute_historical_filing_replay,
    load_historical_replay_plan,
)
from consumer_defensive.core.metric_registry import (
    SpecializedMetric,
    load_metric_registry,
    upsert_metric_registry,
)
from consumer_defensive.core.stage6b_schema import ensure_stage6b_schema
from consumer_defensive.core.stage4 import _additive_raw_fact_rows
from consumer_defensive.core.specialized_recall import (
    load_reviewed_expectation_matrix,
)


ROOT = Path(__file__).resolve().parents[2]
METRICS = (
    ROOT
    / 'consumer_defensive'
    / 'data'
    / 'consumer_defensive_specialized_metric_registry.yaml'
)


def _filing(index: int) -> FilingRow:
    day = index + 1
    return FilingRow(
        ticker='TEST',
        accession_number=f'0000000001-24-{index:06d}',
        form_type='10-Q',
        accepted_at=f'2024-01-{day:02d}T12:00:00Z',
        filing_date=f'2024-01-{day:02d}',
        report_date='2023-12-31',
        primary_document=f'filing-{index}.htm',
        existing_hydration_status='not_hydrated',
    )


def test_adaptive_chronological_schedule_proves_every_core_capture() -> None:
    targets = [_filing(index) for index in range(10)]
    cutoffs, captures = _capture_schedule(
        targets,
        targets,
        maximum_documents=8,
    )
    assert cutoffs == ['2024-01-01', '2024-01-02', '2024-01-10']
    assert set(captures) == {
        (row.ticker, row.accession_number) for row in targets
    }
    assert all(1 <= rank <= 8 for _, rank in captures.values())


def test_capture_proof_fails_closed_at_unordered_acceptance_tie() -> None:
    tied = [
        FilingRow(
            ticker='TEST',
            accession_number=f'0000000001-24-{index:06d}',
            form_type='10-Q',
            accepted_at='2024-01-10T12:00:00Z',
            filing_date='2024-01-10',
            report_date='2023-12-31',
            primary_document=f'filing-{index}.htm',
            existing_hydration_status='not_hydrated',
        )
        for index in range(9)
    ]
    cutoffs, captures = _capture_schedule(
        tied,
        tied,
        maximum_documents=8,
    )
    assert cutoffs == ['2024-01-10']
    assert captures == {}


def test_metric_registry_separates_non_sec_from_sec_addressable() -> None:
    _, metrics = load_metric_registry(METRICS)
    non_sec = {
        metric.metric_id for metric in metrics
        if metric.source_availability_class == 'non_sec'
    }
    assert non_sec == {
        'capacity_utilization_pct',
        'distribution_points_growth_pct',
        'innovation_sales_mix_pct',
    }
    assert sum(metric.sec_addressable for metric in metrics) == 35


def test_event_index_selects_only_primary_and_governed_results_documents() -> None:
    payload = b'''{"directory":{"item":[
      {"name":"event.htm","type":"8-K","description":"Current report"},
      {"name":"earnings.htm","type":"EX-99.1","description":"Earnings release"},
      {"name":"deck.pdf","type":"EX-99.2","description":"Investor presentation"},
      {"name":"contract.htm","type":"EX-10.1","description":"Material contract"},
      {"name":"logo.png","type":"GRAPHIC","description":"Logo"}
    ]}}'''
    items = _parse_event_index(payload, logical_path='index.json')
    selected = _select_event_documents(
        items, primary_document='event.htm', maximum_documents=4
    )
    assert [(row['name'], row['document_role']) for row in selected] == [
        ('event.htm', 'primary_event_filing'),
        ('earnings.htm', 'earnings_exhibit'),
        ('deck.pdf', 'earnings_exhibit'),
    ]


def test_real_sec_filing_index_html_supplies_exhibit_types_and_descriptions() -> None:
    payload = b'''<html><body><table class="tableFile">
      <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th>
          <th>Size</th></tr>
      <tr><td>1</td><td>Current report</td><td><a>event.htm</a>
          <span class="inlineXBRL"> iXBRL</span></td>
          <td>8-K</td><td>100</td></tr>
      <tr><td>2</td><td>Earnings release</td><td><a>earnings.htm</a></td>
          <td>EX-99.1</td><td>200</td></tr>
      <tr><td>3</td><td>Material contract</td><td><a>contract.htm</a></td>
          <td>EX-10.1</td><td>300</td></tr>
    </table></body></html>'''
    items = _parse_event_filing_index_html(
        payload, logical_path='0000000001-24-000001-index.html'
    )
    selected = _select_event_documents(
        items, primary_document='event.htm', maximum_documents=4
    )
    assert [(row['name'], row['type'], row['document_role']) for row in selected] == [
        ('event.htm', '8-K', 'primary_event_filing'),
        ('earnings.htm', 'EX-99.1', 'earnings_exhibit'),
    ]


@pytest.mark.parametrize('name', ['../escape.htm', 'CON.htm', 'x\\bad.htm'])
def test_event_index_rejects_unsafe_document_names(name: str) -> None:
    payload = (
        '{"directory":{"item":[{"name":'
        + json.dumps(name)
        + ',"type":"EX-99.1"}]}}'
    ).encode()
    with pytest.raises(ValueError):
        _parse_event_index(payload, logical_path='index.json')


def test_reviewed_matrix_normalizes_expected_and_excluded_metrics(
    tmp_path: Path,
) -> None:
    matrix = tmp_path / 'matrix.csv'
    matrix.write_text(
        'Ticker,Company Name,SEC Filing Type,'
        'Metrics Available via SEC Filings (Direct & Derived),'
        'Non-SEC Excluded Metrics (Third-Party)\n'
        'ACI,Albertsons,Form 10-K,'
        '"Comparable-sales growth, Inventory turnover (Derived)",'
        'Market-share change\n',
        encoding='utf-8',
    )
    rows = load_reviewed_expectation_matrix(
        matrix,
        cohort_id='consumer_staples_distribution_retail',
    )
    assert {
        (row['metric_id'], row['expectation_class'])
        for row in rows
    } == {
        ('comparable_sales_growth_pct', 'sec_expected'),
        ('inventory_turnover', 'sec_expected'),
        ('market_share_change_bps', 'non_sec_excluded'),
    }


def test_coverage_retains_registered_and_sec_addressable_denominators(
    tmp_path: Path,
    monkeypatch,
) -> None:
    conn = connect(tmp_path / 'coverage.sqlite')
    try:
        init_db(conn)
        ensure_stage6b_schema(conn)
        conn.execute(
            '''
            CREATE TABLE fact_specialized_metric_disclosure_summary(
                ticker TEXT NOT NULL,
                metric_id TEXT NOT NULL,
                asof_date TEXT NOT NULL,
                disclosure_status TEXT NOT NULL
            )
            '''
        )
        metrics = [
            SpecializedMetric(
                metric_id='sec_metric',
                cohorts=('beverages',),
                applicability_subtypes=('all_operating_issuers',),
                unit_family='percent',
                direction_hint='positive',
                purpose='SEC metric',
                initial_status='research_candidate',
                production_weight=0.0,
                source_availability_class='sec_direct',
            ),
            SpecializedMetric(
                metric_id='non_sec_metric',
                cohorts=('beverages',),
                applicability_subtypes=('all_operating_issuers',),
                unit_family='percent',
                direction_hint='positive',
                purpose='Non-SEC metric',
                initial_status='research_candidate',
                production_weight=0.0,
                source_availability_class='non_sec',
            ),
        ]
        upsert_metric_registry(
            conn,
            registry_version='test_v1',
            metrics=metrics,
        )
        now = utc_now()
        with conn:
            conn.execute(
                '''
                INSERT INTO stage6b_specialized_run(
                    asof_date,parser_run_id,adapter_version,policy_sha256,
                    source_manifest_sha256,seal_manifest_sha256,
                    ingestion_config_sha256,issuer_scope_sha256,started_at,
                    completed_at,status,inventory_document_count,
                    accepted_observation_count,metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ''',
                (
                    '2024-12-31', 1, 'test', 'a' * 64, 'b' * 64,
                    'c' * 64, 'd' * 64, 'e' * 64, now, now,
                    'measurement_only_complete', 1, 2, '{}',
                ),
            )
            run_id = int(conn.execute(
                'SELECT stage6b_run_id FROM stage6b_specialized_run'
            ).fetchone()[0])
            conn.execute(
                '''
                INSERT INTO stage6b_document_inventory(
                    asof_date,ticker,accession_number,document_name,form_type,
                    filing_date,accepted_at,report_date,content_sha256,bytes,
                    seal_manifest_sha256,ingestion_config_sha256,
                    issuer_scope_sha256,requested_metrics_json,
                    inventory_status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ''',
                (
                    '2024-12-31', 'TEST', '0000000001-24-000001',
                    'filing.htm', '10-K', '2024-12-31',
                    '2024-12-31T12:00:00Z', '2024-12-31', 'f' * 64,
                    1, 'c' * 64, 'd' * 64, 'e' * 64, '[]',
                    'sealed_current_snapshot', now,
                ),
            )
        monkeypatch.setattr(
            specialized_metrics,
            'bootstrap_stage6b',
            lambda *_args, **_kwargs: 'policy',
        )
        monkeypatch.setattr(
            specialized_metrics,
            '_metric_registry',
            lambda _bundle: ('test_v1', metrics),
        )
        monkeypatch.setattr(
            specialized_metrics,
            '_taxonomy',
            lambda _conn: {
                'TEST': {'cohort_id': 'beverages', 'subtype': 'non_alcohol'}
            },
        )
        monkeypatch.setattr(
            specialized_metrics,
            '_current_tickers',
            lambda _conn, *, as_of: {'TEST'},
        )
        monkeypatch.setattr(
            specialized_metrics,
            '_status_tickers',
            lambda _conn, *, parser_run_id: {},
        )
        monkeypatch.setattr(
            specialized_metrics,
            '_run_observations',
            lambda _conn, *, as_of, run: [
                conn.execute(
                    '''SELECT ? AS ticker, ? AS metric_id,
                              'accepted_measurement_only' AS evidence_status,
                              1.0 AS numeric_value, ? AS source_id,
                              '2024-12-31' AS period_end''',
                    (
                        'TEST', metric.metric_id,
                        specialized_metrics.DERIVED_SOURCE_ID
                        if metric.metric_id == 'non_sec_metric'
                        else specialized_metrics.SOURCE_ID,
                    ),
                ).fetchone()
                for metric in metrics
            ],
        )
        result = specialized_metrics.build_stage6b_coverage(
            conn,
            object(),
            as_of='2024-12-31',
            stage6b_run_id=run_id,
        )
        assert len(result['overall_rows']) == 6
        assert result['coverage_status_row_count'] > 0
        states = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                '''SELECT evidence_state,SUM(issuer_count)
                   FROM stage6b_metric_coverage_status
                   WHERE stage6b_run_id=? GROUP BY evidence_state''',
                (run_id,),
            )
        }
        assert states['direct_numeric'] > 0
        assert states['non_sec_required'] == 0
        for metric in metrics:
            total = int(conn.execute(
                '''SELECT SUM(issuer_count)
                   FROM stage6b_metric_coverage_status
                   WHERE stage6b_run_id=? AND scope_name='all_taxonomy'
                     AND cohort_id='*' AND applicability_subtype='*'
                     AND metric_id=?''',
                (run_id, metric.metric_id),
            ).fetchone()[0])
            assert total == 1
        assert result['denominator_summary'] == {
            'all_taxonomy': {
                'applicable_issuer_metric_pairs': 2,
                'measurement_issuer_metric_pairs': 2,
            },
            'all_taxonomy_sec_addressable': {
                'applicable_issuer_metric_pairs': 1,
                'measurement_issuer_metric_pairs': 1,
            },
            'current_live': {
                'applicable_issuer_metric_pairs': 2,
                'measurement_issuer_metric_pairs': 2,
            },
            'current_live_sec_addressable': {
                'applicable_issuer_metric_pairs': 1,
                'measurement_issuer_metric_pairs': 1,
            },
        }
        assert result['history_depth_summary']['all_taxonomy'] == {
            'observation_count': 2,
            'issuer_period_count': 2,
            'multi_period_issuer_metric_pairs': 0,
        }
        assert result['evidence_state_summary']['all_taxonomy'] == {
            'conflict': 0,
            'derived': 1,
            'direct_numeric': 1,
            'issuer_corpus_present_metric_not_targeted': 0,
            'metric_requested_work_incomplete': 0,
            'metric_targeted_corpus_no_candidate': 0,
            'metric_targeted_document_not_planned': 0,
            'non_sec_required': 0,
            'not_in_sealed_corpus': 0,
            'numeric_candidate_rejected': 0,
            'parser_failure': 0,
            'review_required': 0,
        }
        assert conn.execute(
            '''SELECT COUNT(*) FROM stage6b_metric_history_depth
               WHERE stage6b_run_id=? AND scope_name='all_taxonomy'
                 AND cohort_id='*' AND applicability_subtype='*' ''',
            (run_id,),
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_uncovered_evidence_states_use_metric_targeting_not_any_document() -> None:
    direct = SpecializedMetric(
        metric_id='direct_metric',
        cohorts=('beverages',),
        applicability_subtypes=('all_operating_issuers',),
        unit_family='percent',
        direction_hint='positive',
        purpose='Direct metric',
        initial_status='research_candidate',
        production_weight=0.0,
        source_availability_class='sec_direct',
    )
    states = specialized_metrics._uncovered_evidence_states(
        metric=direct,
        remaining={'AAA', 'BBB', 'CCC', 'DDD', 'EEE'},
        completed_work={'AAA'},
        requested_work={'AAA', 'BBB'},
        inventoried_metrics={'AAA', 'BBB', 'CCC'},
        inventory_tickers={'AAA', 'BBB', 'CCC', 'DDD'},
    )
    assert states == {
        'metric_targeted_corpus_no_candidate': {'AAA'},
        'metric_requested_work_incomplete': {'BBB'},
        'metric_targeted_document_not_planned': {'CCC'},
        'issuer_corpus_present_metric_not_targeted': {'DDD'},
        'not_in_sealed_corpus': {'EEE'},
    }

    selective = SpecializedMetric(
        **{
            **vars(direct),
            'metric_id': 'selective_metric',
            'source_availability_class': 'sec_selective',
        }
    )
    selective_states = specialized_metrics._uncovered_evidence_states(
        metric=selective,
        remaining={'AAA'},
        completed_work={'AAA'},
        requested_work={'AAA'},
        inventoried_metrics={'AAA'},
        inventory_tickers={'AAA'},
    )
    assert selective_states['selective_disclosure_not_confirmed'] == {'AAA'}
    assert 'metric_targeted_corpus_no_candidate' not in selective_states


def test_history_depth_preserves_periods_that_pair_breadth_collapses() -> None:
    observations = [
        {
            'ticker': 'AAA', 'metric_id': 'metric',
            'period_end': '2023-12-31',
            'evidence_status': 'accepted_measurement_only',
            'numeric_value': 1.0,
        },
        {
            'ticker': 'AAA', 'metric_id': 'metric',
            'period_end': '2024-12-31',
            'evidence_status': 'accepted_measurement_only',
            'numeric_value': 2.0,
        },
        {
            'ticker': 'BBB', 'metric_id': 'metric',
            'period_end': '2024-12-31',
            'evidence_status': 'accepted_measurement_only',
            'numeric_value': 3.0,
        },
    ]
    row = specialized_metrics._history_depth_row(
        observations,
        stage6b_run_id=1,
        scope_name='all_taxonomy',
        cohort_id='*',
        applicability_subtype='*',
        metric_id='metric',
        applicable={'AAA', 'BBB', 'CCC'},
        created_at='2026-08-22T00:00:00Z',
    )
    assert row[5:12] == (
        2, 3, 3, 1, 1.5, '2023-12-31', '2024-12-31'
    )
    assert json.loads(str(row[12])) == {'AAA': 2, 'BBB': 1}


def _write_replay_plan(tmp_path: Path):
    inventory = tmp_path / 'inventory.csv'
    inventory.write_text(
        'ticker,accession_number,form_family,primary_document,'
        'replay_sequence,replay_asof_date,capture_rank,inventory_status\n'
        'AAA,0000000001-24-000001,annual,annual.htm,1,2024-01-31,1,'
        'planned_chronological_replay\n'
        'AAA,0000000001-24-000002,quarterly,quarter.htm,2,2024-02-29,1,'
        'planned_chronological_replay\n'
        'AAA,0000000001-24-000003,event_report,event.htm,,,,'
        'requires_filing_index_discovery\n',
        encoding='utf-8',
    )
    schedule = tmp_path / 'schedule.csv'
    schedule.write_text(
        'replay_sequence,asof_date,target_filing_count,ticker_count,'
        'maximum_capture_rank\n'
        '1,2024-01-31,1,1,1\n'
        '2,2024-02-29,1,1,1\n',
        encoding='utf-8',
    )
    return load_historical_replay_plan(
        inventory_path=inventory,
        schedule_path=schedule,
    )


def _replay_db(tmp_path: Path):
    conn = connect(tmp_path / 'historical-replay.sqlite')
    conn.executescript(
        '''
        CREATE TABLE fact_sec_filing(accession_number TEXT PRIMARY KEY);
        CREATE TABLE bridge_sec_filing_company(
            accession_number TEXT,issuer_company_id INTEGER
        );
        CREATE TABLE bridge_sec_filing_document_company(
            accession_number TEXT NOT NULL,
            issuer_ticker TEXT NOT NULL,
            primary_document TEXT,
            hydration_status TEXT NOT NULL,
            content_sha256 TEXT
        );
        CREATE TABLE fact_sec_xbrl_fact_raw(raw_fact_id INTEGER PRIMARY KEY);
        CREATE TABLE consumer_defensive_sec_ingestion_watermark(
            model_family TEXT PRIMARY KEY,
            asof_date TEXT NOT NULL
        );
        CREATE TABLE consumer_defensive_sec_reconciliation_state(
            asof_date TEXT PRIMARY KEY,status TEXT NOT NULL,
            scope_contract_version INTEGER NOT NULL,trust_state TEXT NOT NULL
        );
        CREATE TABLE consumer_defensive_sec_cache_snapshot(
            asof_date TEXT PRIMARY KEY,scope_contract_version INTEGER NOT NULL,
            trust_state TEXT NOT NULL
        );
        '''
    )
    return conn


def test_historical_replay_executes_in_order_and_resumes_exactly(
    tmp_path: Path,
) -> None:
    plan = _write_replay_plan(tmp_path)
    assert plan.target_filing_count == 2
    assert plan.event_index_candidate_count == 1
    conn = _replay_db(tmp_path)
    calls: list[str] = []

    def sync_step(*, as_of, tickers, force_refresh, incremental_from_asof):
        assert tickers is None
        assert force_refresh is False
        expected_prior = None if as_of == '2024-01-31' else '2024-01-31'
        assert incremental_from_asof == expected_prior
        calls.append(as_of)
        step = next(item for item in plan.steps if item.asof_date == as_of)
        with conn:
            for target in step.targets:
                conn.execute(
                    '''INSERT INTO bridge_sec_filing_document_company(
                           accession_number,issuer_ticker,primary_document,
                           hydration_status,content_sha256
                       ) VALUES (?,?,?,?,?)''',
                    (
                        target.accession_number,
                        target.ticker,
                        target.primary_document,
                        'hydrated',
                        'a' * 64,
                    ),
                )
            conn.execute('DELETE FROM consumer_defensive_sec_reconciliation_state')
            conn.execute('DELETE FROM consumer_defensive_sec_cache_snapshot')
            conn.execute(
                '''INSERT INTO consumer_defensive_sec_reconciliation_state
                   VALUES (?,'complete',3,'trusted_current')''',
                (as_of,),
            )
            conn.execute(
                '''INSERT INTO consumer_defensive_sec_cache_snapshot
                   VALUES (?,3,'trusted_current')''',
                (as_of,),
            )
            conn.execute(
                '''INSERT INTO consumer_defensive_sec_ingestion_watermark
                   VALUES ('consumer_defensive',?)
                   ON CONFLICT(model_family) DO UPDATE SET asof_date=excluded.asof_date''',
                (as_of,),
            )
        return {
            'failures': [],
            'full_scope_reconciled': True,
            'documents': len(step.targets),
            'cache_manifest': {'sha256': 'b' * 64, 'files': 1, 'bytes': 1},
        }

    try:
        partial = execute_historical_filing_replay(
            conn,
            plan,
            output_dir=tmp_path / 'replay-output',
            sync_step=sync_step,
            stop_after_sequence=1,
        )
        assert partial['status'] == 'PARTIAL'
        assert partial['verified_target_count'] == 1
        with conn:
            conn.execute(
                '''UPDATE consumer_defensive_sec_ingestion_watermark
                   SET asof_date='2024-02-29'
                   WHERE model_family='consumer_defensive' '''
            )
            conn.execute('DELETE FROM consumer_defensive_sec_reconciliation_state')
        resumed = execute_historical_filing_replay(
            conn,
            plan,
            output_dir=tmp_path / 'replay-output',
            sync_step=sync_step,
        )
        assert resumed['status'] == 'PASS'
        assert resumed['initial_completed_sequence'] == 1
        assert resumed['verified_target_count'] == 2
        assert calls == ['2024-01-31', '2024-02-29']
    finally:
        conn.close()


def test_additive_raw_fact_growth_preserves_existing_observation_ids() -> None:
    existing = [
        ('semantic-a', 'observation-a'),
    ]
    staged = [
        ('semantic-a', 'observation-a', 'created-at'),
        ('semantic-b', 'observation-b', 'created-at'),
    ]
    assert _additive_raw_fact_rows(existing, staged) == [staged[1]]
    corrected = [
        ('semantic-b', 'observation-b', 'created-at'),
    ]
    assert _additive_raw_fact_rows(existing, corrected) is None


def test_historical_replay_rejects_populated_stage4_without_watermark(
    tmp_path: Path,
) -> None:
    plan = _write_replay_plan(tmp_path)
    conn = _replay_db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO fact_sec_filing(accession_number) VALUES ('existing')"
        )
        try:
            execute_historical_filing_replay(
                conn,
                plan,
                output_dir=tmp_path / 'rejected-output',
                sync_step=lambda **_kwargs: {},
            )
        except RuntimeError as exc:
            assert 'empty Stage 4 foundation' in str(exc)
        else:
            raise AssertionError('Populated Stage 4 replay foundation was accepted.')
    finally:
        conn.close()
