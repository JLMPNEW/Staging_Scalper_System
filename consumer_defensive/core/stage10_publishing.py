'''Deterministic, research-only Stage 10 publishing for Consumer Defensive.

The publisher is intentionally downstream-only.  It opens the accepted rehearsal
database read-only, binds every report to the frozen Stage 7 score snapshot and
accepted Stage 9 artifacts, and cannot promote a score or write a portfolio.
'''

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from .atomic_io import atomic_text_writer
from .config import ConfigBundle, cfg_get, load_yaml
from .scoring_features import CORE_COMPONENT_SPECS
from .stage9_backtest import (
    CONTRACT_FILE as STAGE9_CONTRACT_FILE,
    DECISION_FILE as STAGE9_DECISION_FILE,
    MANIFEST_FILE as STAGE9_MANIFEST_FILE,
    SUMMARY_FILE as STAGE9_SUMMARY_FILE,
    VALIDATION_FILE as STAGE9_VALIDATION_FILE,
    validate_stage9_artifacts,
)


POLICY_FILE = 'consumer_defensive_stage10_publishing.yaml'
CONTRACT_FILE = 'consumer_defensive_stage10_contract.json'
FINAL_RANK_FILE = 'consumer_defensive_final_rank_table.csv'
SCORECARD_FILE = 'consumer_defensive_company_scorecards.csv'
COHORT_FILE = 'consumer_defensive_cohort_summary.csv'
RISK_FILE = 'consumer_defensive_risk_flags.csv'
REVIEW_FILE = 'consumer_defensive_review_queue.csv'
SPECIALIZED_FILE = 'consumer_defensive_specialized_coverage.csv'
TICKER_SPECIALIZED_FILE = 'consumer_defensive_ticker_specialized_coverage.csv'
STAGE9_BASELINE_FILE = 'consumer_defensive_stage9_baseline_summary.csv'
TIEOUT_FILE = 'consumer_defensive_source_tieout.csv'
PAYLOAD_FILE = 'consumer_defensive_dashboard_payload.json'
HTML_FILE = 'index.html'
MANIFEST_FILE = 'consumer_defensive_dashboard_manifest.json'
VALIDATION_FILE = 'consumer_defensive_stage10_validation.json'

CSV_FILES = (
    FINAL_RANK_FILE,
    SCORECARD_FILE,
    COHORT_FILE,
    RISK_FILE,
    REVIEW_FILE,
    SPECIALIZED_FILE,
    TICKER_SPECIALIZED_FILE,
    STAGE9_BASELINE_FILE,
    TIEOUT_FILE,
)

FINAL_RANK_REQUIRED_FIELDS = (
    'asof_date', 'ticker', 'company_name', 'sector', 'industry',
    'industry_aggregate', 'calibration_cohort', 'final_score', 'final_rank',
    'rank_ready_flag', 'model_status', 'promotion_state', 'score_confidence',
    'score_model_version', 'model_version', 'scoring_contract_version',
    'portfolio_candidate_gate', 'portfolio_candidate_score',
    'portfolio_candidate_status', 'portfolio_candidate_reason',
    'calibration_eligible_flag', 'research_calibration_input_eligible_flag',
    'research_calibration_reason', 'calibration_sample_role',
    'stage11_calibration_panel_source',
    'stage11_calibration_input_eligible_flag',
    'stage11_calibration_input_reason', 'survivorship_corrected_panel_flag',
    'oos_score_valid_flag', 'oos_score_asof_date', 'oos_invalid_reason',
    'calibration_lock_date', 'market_cap', 'avg_dollar_volume_60d',
    'valuation_score', 'quality_score', 'durable_growth_score',
    'operating_resilience_score', 'market_behavior_score',
    'positioning_score', 'specialized_operating_metrics_score',
)

_ALLOWED_POLICY_KEYS = {
    'mode', 'output_version', 'readiness_label', 'permitted_use',
    'production_promotion_enabled', 'portfolio_write_enabled',
    'portfolio_candidate_gate', 'oos_score_valid_flag',
    'require_stage9_validation_pass', 'require_stage9_permitted_use',
    'minimum_ticker_count', 'minimum_rank_ready_fraction',
    'expected_core_component_count', 'expected_specialized_component_count',
    'top_rank_rows_html', 'maximum_review_rows_html', 'canonical_sector',
    'internal_sector', 'portfolio_adapter',
    'stage11_calibration_panel_source', 'survivorship_corrected_panel_flag',
    'citation_policy', 'latest_snapshot_enabled', 'dated_snapshot_required',
}

_CORE_GROUPS = {spec.name: spec.group for spec in CORE_COMPONENT_SPECS}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_json(value: Any, *, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'Invalid JSON in {context}.') from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f'Expected JSON object in {context}.')
    return payload


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise RuntimeError(f'Expected JSON object: {path}')
    return payload


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open('r', encoding='utf-8', newline='') as handle:
        return list(csv.DictReader(handle))


def _columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if str(key) not in columns:
                columns.append(str(key))
    return columns or ['status']


def _csv_text(rows: Sequence[Mapping[str, Any]]) -> str:
    handle = io.StringIO(newline='')
    writer = csv.DictWriter(
        handle, fieldnames=_columns(rows), extrasaction='ignore',
        lineterminator='\n',
    )
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue()


def _json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + '\n'


def _row_hash(row: Mapping[str, Any], field: str = 'row_sha256') -> dict[str, Any]:
    payload = {str(key): value for key, value in row.items() if key != field}
    return {**payload, field: _sha256(payload)}


def _self_hash(payload: Mapping[str, Any], field: str) -> str:
    return _sha256({key: value for key, value in payload.items() if key != field})


def _immutable_text(path: Path, content: str) -> None:
    if path.is_symlink():
        raise RuntimeError(f'Refusing Stage 10 symlink artifact: {path}')
    encoded = content.encode('utf-8')
    if path.exists():
        if path.read_bytes() != encoded:
            raise FileExistsError(
                f'Immutable Stage 10 artifact content changed: {path}'
            )
        return
    with atomic_text_writer(path, encoding='utf-8', newline='') as handle:
        handle.write(content)


def _replace_text(path: Path, content: str) -> None:
    if path.is_symlink():
        raise RuntimeError(f'Refusing Stage 10 symlink artifact: {path}')
    with atomic_text_writer(
        path, encoding='utf-8', newline='',
    ) as handle:
        handle.write(content)


def _assert_directory_safe(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.exists() and current.is_symlink():
            raise RuntimeError(f'Refusing symlinked Stage 10 output path: {current}')
        current = current.parent


def validate_stage10_policy(section: Mapping[str, Any]) -> None:
    unknown = sorted(set(section) - _ALLOWED_POLICY_KEYS)
    missing = sorted(_ALLOWED_POLICY_KEYS - set(section))
    if unknown or missing:
        raise ValueError(
            f'Stage 10 policy key mismatch: missing={missing} unknown={unknown}.'
        )
    exact = {
        'mode': 'research_only_static_publish',
        'output_version': 'v4',
        'permitted_use': 'research_reporting_only',
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'portfolio_candidate_gate': 0,
        'oos_score_valid_flag': 0,
        'require_stage9_validation_pass': True,
        'require_stage9_permitted_use': 'stage10_reporting_input',
        'canonical_sector': 'Consumer Staples',
        'internal_sector': 'Consumer Defensive',
        'portfolio_adapter': 'consumer_defensive',
        'survivorship_corrected_panel_flag': 0,
        'citation_policy': 'strict',
        'latest_snapshot_enabled': True,
        'dated_snapshot_required': True,
    }
    for key, required in exact.items():
        if section.get(key) != required:
            raise ValueError(
                f'stage10_publishing.{key} must be {required!r}; '
                f'got {section.get(key)!r}.'
            )
    if 'not investable' not in str(section['readiness_label']).casefold():
        raise ValueError('Stage 10 readiness label must state not investable.')
    for key in (
        'minimum_ticker_count', 'expected_core_component_count',
        'expected_specialized_component_count', 'top_rank_rows_html',
        'maximum_review_rows_html',
    ):
        if int(section[key]) < 1:
            raise ValueError(f'stage10_publishing.{key} must be positive.')
    fraction = float(section['minimum_rank_ready_fraction'])
    if not 0.0 < fraction <= 1.0:
        raise ValueError(
            'stage10_publishing.minimum_rank_ready_fraction must be in (0,1].'
        )
    if not str(section['stage11_calibration_panel_source']).strip():
        raise ValueError('Stage 10 Stage 11 panel source cannot be empty.')


def stage10_policy(bundle: ConfigBundle) -> dict[str, Any]:
    path = bundle.base_dir / 'data' / POLICY_FILE
    payload = load_yaml(path)
    if set(payload) != {'schema_version', 'stage10_publishing'}:
        raise ValueError(
            'Stage 10 policy must contain only schema_version and '
            'stage10_publishing.'
        )
    if payload['schema_version'] != 'consumer_defensive_stage10_publishing_policy_v2':
        raise ValueError('Unknown Consumer Defensive Stage 10 policy version.')
    section = payload['stage10_publishing']
    if not isinstance(section, dict):
        raise ValueError('stage10_publishing must be a mapping.')
    validate_stage10_policy(section)
    return json.loads(_canonical_json(section))


def _verify_stage9_root(root: Path, policy: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        STAGE9_CONTRACT_FILE, STAGE9_DECISION_FILE, STAGE9_MANIFEST_FILE,
        STAGE9_SUMMARY_FILE, STAGE9_VALIDATION_FILE,
    }
    missing = sorted(name for name in required if not (root / name).is_file())
    if missing:
        raise RuntimeError(f'Stage 10 missing Stage 9 artifacts: {missing}')
    contract = _read_json(root / STAGE9_CONTRACT_FILE)
    decision = _read_json(root / STAGE9_DECISION_FILE)
    manifest = _read_json(root / STAGE9_MANIFEST_FILE)
    validation = _read_json(root / STAGE9_VALIDATION_FILE)
    failures: list[str] = []
    contract_core = {
        key: value for key, value in contract.items()
        if key not in {'stage9_run_id', 'contract_sha256'}
    }
    if str(contract.get('contract_sha256', '')) != _sha256(contract_core):
        failures.append('contract_self_hash')
    if contract.get('stage9_run_id') != (
        'cds9_' + str(contract.get('contract_sha256', ''))[:24]
    ):
        failures.append('contract_run_id')
    for payload, field, label in (
        (decision, 'decision_sha256', 'decision'),
        (manifest, 'manifest_sha256', 'manifest'),
    ):
        if str(payload.get(field, '')) != _self_hash(payload, field):
            failures.append(f'{label}_self_hash')
    for name, digest in dict(manifest.get('file_sha256s') or {}).items():
        path = root / str(name)
        if not path.is_file() or _file_sha256(path) != str(digest):
            failures.append(f'artifact_hash:{name}')
    if validation.get('status') != 'PASS':
        failures.append('stored_validation_status')
    if validation.get('passed_check_count') != validation.get('check_count'):
        failures.append('stored_validation_check_census')
    if str(validation.get('manifest_sha256', '')) != str(
        manifest.get('manifest_sha256', '')
    ):
        failures.append('stored_validation_manifest_binding')
    if validation.get('permitted_use') != policy['require_stage9_permitted_use']:
        failures.append('stored_validation_permitted_use')
    if any((
        bool(contract.get('production_promotion_enabled')),
        bool(contract.get('portfolio_write_enabled')),
        int(contract.get('oos_score_valid_flag', -1)) != 0,
        bool(decision.get('production_promotion_enabled')),
        bool(decision.get('portfolio_write_enabled')),
        decision.get('stage10_scoring_source')
        != contract.get('stage7_source_id'),
        int(manifest.get('database_write_count', -1)) != 0,
    )):
        failures.append('stage9_not_report_only')
    if failures:
        raise RuntimeError(f'Stage 9 pre-publish verification failed: {failures}')
    return {
        'contract': contract,
        'decision': decision,
        'manifest': manifest,
        'validation': validation,
        'summary': _read_csv(root / STAGE9_SUMMARY_FILE),
    }


def _stage7_material(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    asof_date: str,
) -> dict[str, Any]:
    source_id = str(cfg_get(bundle.payload, 'stage7_scoring.source_id'))
    snapshot_row = conn.execute(
        '''SELECT * FROM stage7_score_snapshot
           WHERE source_id=? AND model_family='consumer_defensive'
             AND asof_date=?''',
        (source_id, asof_date),
    ).fetchone()
    if snapshot_row is None:
        raise RuntimeError(f'No accepted Stage 7 snapshot at {asof_date}.')
    snapshot = dict(snapshot_row)
    outputs = [
        dict(row) for row in conn.execute(
            '''SELECT o.*,c.company_name,c.cik,c.issuer_domicile,
                      t.sector,t.portfolio_sector,t.calibration_cohort,
                      t.applicability_subtype
               FROM feature_scoring_model_output o
               JOIN dim_consumer_defensive_taxonomy t
                 ON t.ticker=o.ticker AND t.model_family=o.model_family
               JOIN dim_company c ON c.company_id=t.company_id
               WHERE o.source_id=? AND o.model_family='consumer_defensive'
                 AND o.asof_date=? ORDER BY o.ticker''',
            (source_id, asof_date),
        )
    ]
    inputs = {
        str(row['ticker']): dict(row) for row in conn.execute(
            '''SELECT * FROM feature_scoring_input
               WHERE model_family='consumer_defensive' AND asof_date=?
                 AND source_id=? ORDER BY ticker''',
            (
                asof_date,
                str(cfg_get(bundle.payload, 'stage7_scoring.baseline_source_id')),
            ),
        )
    }
    components = [
        dict(row) for row in conn.execute(
            '''SELECT * FROM feature_scoring_component
               WHERE model_family='consumer_defensive' AND asof_date=?
               ORDER BY ticker,component_group,component_name''',
            (asof_date,),
        )
    ]
    weight_contract = [
        dict(row) for row in conn.execute(
            '''SELECT * FROM stage7_component_weight_contract
               WHERE source_id=? AND model_family='consumer_defensive'
               ORDER BY component_name''',
            (source_id,),
        )
    ]
    model_contract_row = conn.execute(
        'SELECT * FROM stage7_model_contract WHERE source_id=?', (source_id,)
    ).fetchone()
    if model_contract_row is None:
        raise RuntimeError(f'Missing Stage 7 model contract: {source_id}.')
    model_contract = dict(model_contract_row)
    failures: list[str] = []
    tickers = [str(row['ticker']) for row in outputs]
    if len(tickers) != len(set(tickers)):
        failures.append('duplicate_output_tickers')
    if set(tickers) != set(inputs):
        failures.append('input_output_ticker_mismatch')
    if len(components) != len(tickers) * (
        len(CORE_COMPONENT_SPECS)
        + int(stage10_policy(bundle)['expected_specialized_component_count'])
    ):
        failures.append('component_census')
    if str(snapshot['contract_sha256']) != str(model_contract['contract_sha256']):
        failures.append('snapshot_contract_binding')
    if int(snapshot['ticker_count']) != len(outputs):
        failures.append('snapshot_ticker_count')
    rank_ready = sum(int(row['rank_ready_flag']) for row in outputs)
    if int(snapshot['rank_ready_count']) != rank_ready:
        failures.append('snapshot_rank_ready_count')
    if int(snapshot['review_required_count']) != len(outputs) - rank_ready:
        failures.append('snapshot_review_count')
    if any(
        str(row['promotion_state']) != 'shadow_monitor'
        or int(row['portfolio_candidate_gate']) != 0
        or int(row['oos_score_valid_flag']) != 0
        for row in outputs
    ):
        failures.append('noninvestable_gate_drift')
    expected_ranks = sorted(
        (row for row in outputs if int(row['rank_ready_flag']) == 1),
        key=lambda row: (-float(row['final_score']), str(row['ticker'])),
    )
    if any(int(row['final_rank']) != index for index, row in enumerate(expected_ranks, 1)):
        failures.append('rank_order')
    core_weights = {
        str(row['component_name']): float(row['component_weight'])
        for row in weight_contract if str(row['component_group']) != 'specialized'
    }
    specialized_weights = [
        float(row['component_weight']) for row in weight_contract
        if str(row['component_group']) == 'specialized'
    ]
    if set(core_weights) != set(_CORE_GROUPS) or not math.isclose(
        sum(core_weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        failures.append('core_weight_contract')
    if not specialized_weights or any(value != 0.0 for value in specialized_weights):
        failures.append('specialized_weight_contract')
    for row in outputs:
        weights = _safe_json(
            row['component_weights_json'], context=f"{row['ticker']} weights"
        )
        scores = _safe_json(
            row['component_scores_json'], context=f"{row['ticker']} scores"
        )
        quality = _safe_json(
            row['component_quality_json'], context=f"{row['ticker']} quality"
        )
        if set(weights) != set(core_weights) or set(scores) != set(core_weights):
            failures.append(f"component_json:{row['ticker']}")
            continue
        tied = sum(float(weights[name]) * float(scores[name]) for name in core_weights)
        confidence = sum(
            float(weights[name]) * float(quality[name]) for name in core_weights
        )
        if not math.isclose(tied, float(row['final_score']), abs_tol=1e-10):
            failures.append(f"score_arithmetic:{row['ticker']}")
        if not math.isclose(
            confidence, float(row['data_quality_confidence']), abs_tol=1e-12
        ):
            failures.append(f"confidence_arithmetic:{row['ticker']}")
    if failures:
        raise RuntimeError(f'Stage 7 pre-publish verification failed: {failures[:25]}')
    return {
        'source_id': source_id,
        'snapshot': snapshot,
        'model_contract': model_contract,
        'outputs': outputs,
        'inputs': inputs,
        'components': components,
        'core_weights': core_weights,
        'factor_validation_verdict': str(model_contract['factor_validation_verdict']),
    }


def _group_score(
    scores: Mapping[str, Any],
    weights: Mapping[str, Any],
    group: str,
) -> float | str:
    names = [name for name, value in _CORE_GROUPS.items() if value == group]
    denominator = sum(float(weights[name]) for name in names)
    if denominator <= 0.0:
        return ''
    return sum(
        float(weights[name]) * float(scores[name]) for name in names
    ) / denominator


def _metric_label(metric_id: str) -> str:
    replacements = {
        'pct': '%', 'bps': '(bps)', 'ebitda': 'EBITDA',
        'ngp': 'NGP', 'nab': 'NAB', 'rpu': 'RPU',
    }
    words = [replacements.get(word, word) for word in metric_id.split('_')]
    label = ' '.join(words).replace(' %', ' (%)')
    return label[:1].upper() + label[1:]


def _rank_rows(
    material: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    asof_date: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    components_by_ticker: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for component in material['components']:
        components_by_ticker[str(component['ticker'])][
            str(component['component_name'])
        ] = component
    for source in material['outputs']:
        ticker = str(source['ticker'])
        scores = _safe_json(
            source['component_scores_json'],
            context=f'{ticker} Stage 7 component scores',
        )
        weights = _safe_json(
            source['component_weights_json'],
            context=f'{ticker} Stage 7 component weights',
        )
        input_row = material['inputs'][ticker]
        input_lineage = _safe_json(
            input_row['lineage_json'],
            context=f'{ticker} Stage 6A input lineage',
        )
        core_lineage = _safe_json(
            source['lineage_json'],
            context=f'{ticker} Stage 7 output lineage',
        )
        calibration_eligible = int(source['calibration_eligible_flag'])
        adv_component = components_by_ticker[ticker].get('avg_dollar_volume_63d')
        raw_adv = _finite(adv_component['raw_value']) if adv_component else None
        row = {
            'asof_date': asof_date,
            'ticker': ticker,
            'company_name': str(source['company_name']),
            'sector': policy['canonical_sector'],
            'industry': str(source['calibration_cohort']),
            'industry_aggregate': policy['canonical_sector'],
            'calibration_cohort': str(source['calibration_cohort_id']),
            'final_score': float(source['final_score']),
            'final_rank': '' if source['final_rank'] is None else int(source['final_rank']),
            'rank_ready_flag': int(source['rank_ready_flag']),
            'model_status': str(source['model_status']),
            'promotion_state': str(source['promotion_state']),
            'score_confidence': float(source['data_quality_confidence']),
            'score_model_version': str(source['model_version']),
            'model_version': str(source['model_version']),
            'scoring_contract_version': 'consumer_defensive_stage7_contract_v3',
            'portfolio_candidate_gate': int(source['portfolio_candidate_gate']),
            'portfolio_candidate_score': '',
            'portfolio_candidate_status': 'blocked',
            'portfolio_candidate_reason': 'research_only_no_valid_oos_score',
            'calibration_eligible_flag': calibration_eligible,
            'research_calibration_input_eligible_flag': calibration_eligible,
            'research_calibration_reason': (
                'stage7_rank_ready_reconstructed_pit_calibration'
                if calibration_eligible
                else str(source['review_reason'] or 'stage7_not_calibration_eligible')
            ),
            'calibration_sample_role': 'deep_replay_research',
            'stage11_calibration_panel_source': policy[
                'stage11_calibration_panel_source'
            ],
            'stage11_calibration_input_eligible_flag': 0,
            'stage11_calibration_input_reason': (
                'stage11_sidecar_not_built_and_no_contemporaneous_oos_score'
            ),
            'survivorship_corrected_panel_flag': int(
                policy['survivorship_corrected_panel_flag']
            ),
            'oos_score_valid_flag': int(source['oos_score_valid_flag']),
            'oos_score_asof_date': '',
            'oos_invalid_reason': 'deep_replay_research_not_contemporaneous_oos',
            'calibration_lock_date': '',
            'market_cap': '',
            'avg_dollar_volume_60d': '' if raw_adv is None else raw_adv,
            'valuation_score': '',
            'quality_score': _group_score(scores, weights, 'financial'),
            'durable_growth_score': '',
            'operating_resilience_score': '',
            'market_behavior_score': _group_score(scores, weights, 'market'),
            'positioning_score': _group_score(scores, weights, 'positioning'),
            'specialized_operating_metrics_score': '',
            'core_score': float(source['core_score']),
            'financial_score': _group_score(scores, weights, 'financial'),
            'full_data_quality_confidence': float(
                source['full_data_quality_confidence']
            ),
            'cohort_rank': '' if source['cohort_rank'] is None else int(source['cohort_rank']),
            'cohort_percentile': (
                '' if source['cohort_percentile'] is None
                else float(source['cohort_percentile'])
            ),
            'review_reason': str(source['review_reason'] or ''),
            'applicability_subtype': str(source['applicability_subtype'] or ''),
            'score_observation_id': str(source['score_observation_id']),
            'input_observation_id': str(input_row['input_observation_id']),
            'model_contract_sha256': str(source['model_contract_sha256']),
            'stage6_contract_sha256': str(input_row['contract_sha256']),
            'missing_core_components': ','.join(
                str(value) for value in core_lineage.get('missing_components', [])
            ),
            'specialized_applicable_count': int(
                input_lineage.get('specialized_applicable_count', 0)
            ),
            'specialized_measurement_count': int(
                input_lineage.get('specialized_available_count', 0)
            ),
        }
        if tuple(row)[:len(FINAL_RANK_REQUIRED_FIELDS)] != FINAL_RANK_REQUIRED_FIELDS:
            raise RuntimeError('Final-rank field order drifted from its frozen schema.')
        rows.append(_row_hash(row))
    rows.sort(
        key=lambda row: (
            int(row['rank_ready_flag']) != 1,
            int(row['final_rank']) if row['final_rank'] != '' else 10**9,
            str(row['ticker']),
        )
    )
    return rows


def _scorecard_rows(
    material: Mapping[str, Any],
    rank_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rank_lookup = {str(row['ticker']): row for row in rank_rows}
    output_lookup = {str(row['ticker']): row for row in material['outputs']}
    contract_weights = dict(material['core_weights'])
    rows: list[dict[str, Any]] = []
    for component in material['components']:
        ticker = str(component['ticker'])
        rank = rank_lookup[ticker]
        output = output_lookup[ticker]
        name = str(component['component_name'])
        specialized = str(component['component_group']) == 'specialized'
        weight = 0.0 if specialized else float(contract_weights[name])
        stored_score = _finite(component['component_score'])
        if specialized:
            effective_score: float | str = ''
            contribution: float | str = ''
        else:
            effective_scores = _safe_json(
                output['component_scores_json'],
                context=f'{ticker} Stage 7 effective scores',
            )
            effective_score = float(effective_scores[name])
            contribution = weight * effective_score
        measurement_qualified = int(
            specialized
            and str(component['availability_status']) == 'measurement_only'
            and component['raw_value'] is not None
        )
        metric_id = name.removeprefix('specialized:')
        row = {
            'asof_date': str(component['asof_date']),
            'ticker': ticker,
            'company_name': str(rank['company_name']),
            'calibration_cohort': str(rank['calibration_cohort']),
            'applicability_subtype': str(rank['applicability_subtype']),
            'component_name': name,
            'metric_id': metric_id,
            'metric_label': _metric_label(metric_id),
            'component_group': str(component['component_group']),
            'direction': str(component['direction']),
            'unit': str(component['unit']),
            'raw_value': '' if component['raw_value'] is None else component['raw_value'],
            'normalized_value': (
                '' if component['normalized_value'] is None
                else component['normalized_value']
            ),
            'stored_component_score': '' if stored_score is None else stored_score,
            'stage7_effective_score': effective_score,
            'stage7_component_weight': weight,
            'stage7_score_contribution': contribution,
            'availability_status': str(component['availability_status']),
            'quality_status': str(component['quality_status'] or ''),
            'measurement_qualified_flag': measurement_qualified,
            'model_weight_qualified_flag': 0,
            'model_weight_status': (
                'locked_zero_no_directionally_accepted_factor_evidence'
                if specialized else 'reviewed_shadow_baseline'
            ),
            'source_asof_date': str(component['source_asof_date'] or ''),
            'source_id': str(component['source_id'] or ''),
            'source_table': str(component['source_table']),
            'source_field': str(component['source_field']),
            'exclusion_reason': str(component['exclusion_reason'] or ''),
            'production_status': str(component['production_status']),
            'component_observation_id': str(component['component_observation_id']),
            'contract_sha256': str(component['contract_sha256']),
        }
        rows.append(_row_hash(row))
    return rows


def _specialized_coverage_rows(
    scorecards: Sequence[Mapping[str, Any]],
    rank_rows: Sequence[Mapping[str, Any]],
    *,
    factor_verdict: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    specialized = [
        row for row in scorecards if row['component_group'] == 'specialized'
    ]
    ticker_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    cohort_metric_groups: dict[
        tuple[str, str], list[Mapping[str, Any]]
    ] = defaultdict(list)
    sector_metric_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in specialized:
        ticker_groups[str(row['ticker'])].append(row)
        cohort_metric_groups[(
            str(row['calibration_cohort']), str(row['metric_id'])
        )].append(row)
        sector_metric_groups[str(row['metric_id'])].append(row)

    ticker_rows: list[dict[str, Any]] = []
    rank_lookup = {str(row['ticker']): row for row in rank_rows}
    for ticker in sorted(ticker_groups):
        values = ticker_groups[ticker]
        applicable = [
            row for row in values if row['availability_status'] != 'not_applicable'
        ]
        qualified = [
            row for row in applicable
            if int(row['measurement_qualified_flag']) == 1
        ]
        missing = [
            str(row['metric_id']) for row in applicable
            if int(row['measurement_qualified_flag']) == 0
        ]
        source = rank_lookup[ticker]
        ticker_rows.append(_row_hash({
            'asof_date': source['asof_date'],
            'ticker': ticker,
            'company_name': source['company_name'],
            'calibration_cohort': source['calibration_cohort'],
            'applicability_subtype': source['applicability_subtype'],
            'registered_specialized_metric_count': len(values),
            'applicable_metric_count': len(applicable),
            'measurement_qualified_metric_count': len(qualified),
            'missing_applicable_metric_count': len(missing),
            'measurement_coverage_pct': (
                100.0 * len(qualified) / len(applicable) if applicable else ''
            ),
            'model_weight_qualified_metric_count': 0,
            'model_weight_coverage_pct': 0.0,
            'missing_applicable_metric_ids': ','.join(sorted(missing)),
            'factor_validation_verdict': factor_verdict,
        }))

    coverage_rows: list[dict[str, Any]] = []

    def append_scope(
        scope_type: str,
        scope_id: str,
        metric_id: str,
        values: Sequence[Mapping[str, Any]],
    ) -> None:
        applicable = [
            row for row in values if row['availability_status'] != 'not_applicable'
        ]
        qualified = [
            row for row in applicable
            if int(row['measurement_qualified_flag']) == 1
        ]
        available_tickers = sorted(str(row['ticker']) for row in qualified)
        missing_tickers = sorted(
            str(row['ticker']) for row in applicable
            if int(row['measurement_qualified_flag']) == 0
        )
        coverage_rows.append(_row_hash({
            'asof_date': str(values[0]['asof_date']),
            'scope_type': scope_type,
            'scope_id': scope_id,
            'metric_id': metric_id,
            'metric_label': str(values[0]['metric_label']),
            'applicable_ticker_count': len(applicable),
            'measurement_qualified_ticker_count': len(qualified),
            'missing_applicable_ticker_count': len(missing_tickers),
            'measurement_coverage_pct': (
                100.0 * len(qualified) / len(applicable) if applicable else ''
            ),
            'measurement_qualified_tickers': ','.join(available_tickers),
            'missing_applicable_tickers': ','.join(missing_tickers),
            'model_weight_qualified_flag': 0,
            'factor_validation_verdict': factor_verdict,
        }))

    for (cohort, metric_id), values in sorted(cohort_metric_groups.items()):
        append_scope('cohort', cohort, metric_id, values)
    for metric_id, values in sorted(sector_metric_groups.items()):
        append_scope('sector', 'consumer_defensive', metric_id, values)
    return coverage_rows, ticker_rows


def _cohort_rows(
    rank_rows: Sequence[Mapping[str, Any]],
    ticker_coverage: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_cohort: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rank_rows:
        by_cohort[str(row['calibration_cohort'])].append(row)
    coverage_lookup = {str(row['ticker']): row for row in ticker_coverage}
    scopes = [('sector', 'consumer_defensive', list(rank_rows))]
    scopes.extend(
        ('cohort', cohort, values) for cohort, values in sorted(by_cohort.items())
    )
    rows: list[dict[str, Any]] = []
    for scope_type, scope_id, values in scopes:
        ready = [row for row in values if int(row['rank_ready_flag']) == 1]
        qualified = sum(
            int(coverage_lookup[str(row['ticker'])][
                'measurement_qualified_metric_count'
            ])
            for row in values
        )
        applicable = sum(
            int(coverage_lookup[str(row['ticker'])]['applicable_metric_count'])
            for row in values
        )
        ordered = sorted(
            ready, key=lambda row: (-float(row['final_score']), str(row['ticker']))
        )
        rows.append(_row_hash({
            'asof_date': str(values[0]['asof_date']),
            'scope_type': scope_type,
            'scope_id': scope_id,
            'ticker_count': len(values),
            'rank_ready_count': len(ready),
            'review_required_count': len(values) - len(ready),
            'rank_ready_pct': 100.0 * len(ready) / len(values),
            'median_final_score': median(
                float(row['final_score']) for row in values
            ),
            'average_score_confidence': sum(
                float(row['score_confidence']) for row in values
            ) / len(values),
            'top_ranked_ticker': str(ordered[0]['ticker']) if ordered else '',
            'top_ranked_score': float(ordered[0]['final_score']) if ordered else '',
            'specialized_applicable_observation_count': applicable,
            'specialized_measurement_qualified_observation_count': qualified,
            'specialized_measurement_coverage_pct': (
                100.0 * qualified / applicable if applicable else ''
            ),
            'specialized_model_weight_qualified_metric_count': 0,
            'oos_valid_ticker_count': sum(
                int(row['oos_score_valid_flag']) for row in values
            ),
            'portfolio_eligible_ticker_count': sum(
                int(row['portfolio_candidate_gate']) for row in values
            ),
        }))
    return rows


def _stage9_baseline_rows(stage9: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in stage9['summary']:
        if str(source.get('candidate_kind')) != 'stage7_core_baseline':
            continue
        if (
            int(source.get('oos_score_valid_flag', -1)) != 0
            or int(source.get('promotion_eligible_flag', -1)) != 0
        ):
            raise RuntimeError(
                'Stage 9 baseline summary contains an investable or OOS-valid row.'
            )
        row = dict(source)
        row['stage9_summary_row_sha256'] = row.pop('summary_row_sha256')
        rows.append(_row_hash(row))
    rows.sort(key=lambda row: (
        str(row['scope_id']), str(row['portfolio_name']),
        str(row['weight_method']), str(row['return_basis']),
    ))
    if len(rows) != 40:
        raise RuntimeError(
            f'Expected 40 Stage 7 baseline backtest views; found {len(rows)}.'
        )
    return rows


def _review_rows(
    rank_rows: Sequence[Mapping[str, Any]],
    ticker_coverage: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    coverage = {str(row['ticker']): row for row in ticker_coverage}
    rows: list[dict[str, Any]] = []
    for rank in rank_rows:
        if int(rank['rank_ready_flag']) == 1:
            continue
        ticker = str(rank['ticker'])
        overlay = coverage[ticker]
        confidence = float(rank['score_confidence'])
        rows.append(_row_hash({
            'asof_date': rank['asof_date'],
            'review_priority': 1 if confidence < 0.65 else 2,
            'ticker': ticker,
            'company_name': rank['company_name'],
            'calibration_cohort': rank['calibration_cohort'],
            'model_status': rank['model_status'],
            'review_reason': rank['review_reason'],
            'missing_core_components': rank['missing_core_components'],
            'score_confidence': confidence,
            'full_data_quality_confidence': rank[
                'full_data_quality_confidence'
            ],
            'specialized_applicable_metric_count': overlay[
                'applicable_metric_count'
            ],
            'specialized_measurement_qualified_metric_count': overlay[
                'measurement_qualified_metric_count'
            ],
            'missing_applicable_specialized_metric_ids': overlay[
                'missing_applicable_metric_ids'
            ],
            'required_action': (
                'resolve_core_data_requirements_then_rebuild_from_frozen_inputs'
            ),
            'portfolio_impact': 'none_research_only_gate_zero',
            'resolution_status': 'open',
        }))
    rows.sort(key=lambda row: (
        int(row['review_priority']), float(row['score_confidence']),
        str(row['ticker']),
    ))
    return rows


def _risk_row(
    asof_date: str,
    risk_type: str,
    severity: str,
    scope_id: str,
    detail: str,
    evidence: str,
    action: str,
) -> dict[str, Any]:
    payload = {
        'asof_date': asof_date,
        'risk_id': _sha256({
            'asof_date': asof_date, 'risk_type': risk_type,
            'scope_id': scope_id, 'detail': detail,
        })[:24],
        'severity': severity,
        'risk_type': risk_type,
        'scope_id': scope_id,
        'risk_detail': detail,
        'evidence': evidence,
        'required_action': action,
        'portfolio_impact': 'blocked_no_portfolio_action',
        'status': 'open',
    }
    return _row_hash(payload)


def _risk_rows(
    rank_rows: Sequence[Mapping[str, Any]],
    baseline_rows: Sequence[Mapping[str, Any]],
    *,
    asof_date: str,
    factor_verdict: str,
) -> list[dict[str, Any]]:
    oos_count = sum(int(row['oos_score_valid_flag']) for row in rank_rows)
    gate_count = sum(int(row['portfolio_candidate_gate']) for row in rank_rows)
    rows = [
        _risk_row(
            asof_date, 'research_only_noninvestable', 'critical',
            'consumer_defensive',
            'All rows retain shadow status, gate 0, and no valid OOS score.',
            f'tickers={len(rank_rows)};oos_valid={oos_count};'
            f'portfolio_eligible={gate_count}',
            'Complete Stage 10B, Stage 11, Stage 12, and Stage 13 gates.',
        ),
        _risk_row(
            asof_date, 'specialized_factor_not_accepted', 'high',
            'consumer_defensive',
            'SEC measurements improve coverage but no specialized metric has '
            'directionally accepted factor evidence for a nonzero weight.',
            factor_verdict,
            'Retain zero weights until corrected independent validation passes.',
        ),
        _risk_row(
            asof_date, 'survivorship_and_oos_limitation', 'high',
            'consumer_defensive',
            'Current ranks are not a survivorship-corrected historical sidecar; '
            'deep replay is calibration evidence, not OOS evidence.',
            'survivorship_corrected_panel_flag=0;oos_score_valid_flag=0',
            'Build Stage 11 sidecar and start a post-lock OOS window.',
        ),
    ]
    for row in baseline_rows:
        observed = float(row['reference_nav_capacity_pass_fraction'])
        stress = float(row['stress_reference_nav_capacity_pass_fraction'])
        if observed >= 1.0 and stress >= 1.0:
            continue
        scope = '{}:{}:{}:{}'.format(
            row['scope_id'], row['portfolio_name'],
            row['weight_method'], row['return_basis'],
        )
        evidence = (
            f'observed_pass_fraction={observed};stress_pass_fraction={stress};'
            'minimum_capacity_usd={};maximum_days={}'.format(
                row['minimum_portfolio_capacity_usd'],
                row['maximum_days_to_liquidate_reference_nav'],
            )
        )
        rows.append(_risk_row(
            asof_date, 'stage9_reference_nav_capacity',
            'high' if stress < 0.5 else 'medium', scope,
            'Stage 7 baseline does not pass the $100m reference NAV capacity '
            'test in every invested period.',
            evidence,
            'Use smaller sizing or tighter liquidity constraints in Stage 12.',
        ))
    severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    rows.sort(key=lambda row: (
        severity_order[str(row['severity'])], str(row['risk_type']),
        str(row['scope_id']),
    ))
    return rows


def _contract_payload(
    bundle: ConfigBundle,
    policy: Mapping[str, Any],
    material: Mapping[str, Any],
    stage9: Mapping[str, Any],
    *,
    asof_date: str,
    database_path: Path,
    database_sha256: str,
    stage9_root: Path,
    stage8_root: Path,
    factor_root: Path,
) -> dict[str, Any]:
    snapshot = material['snapshot']
    payload = {
        'schema_version': 'consumer_defensive_stage10_publishing_contract_v1',
        'model_family': 'consumer_defensive',
        'asof_date': asof_date,
        'generation_timestamp': str(snapshot['created_at']),
        'mode': policy['mode'],
        'readiness_label': policy['readiness_label'],
        'permitted_use': policy['permitted_use'],
        'prohibited_use': 'portfolio_action_weight_promotion_or_oos_claim',
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'portfolio_candidate_gate': 0,
        'oos_score_valid_flag': 0,
        'database_access_mode': 'read_only',
        'database_write_count': 0,
        'database_path': str(database_path),
        'database_sha256': database_sha256,
        'stage7_source_id': material['source_id'],
        'stage7_model_version': str(snapshot['model_version']),
        'stage7_contract_sha256': str(snapshot['contract_sha256']),
        'stage7_baseline_input_manifest_sha256': str(
            snapshot['baseline_input_manifest_sha256']
        ),
        'stage7_output_manifest_sha256': str(
            snapshot['output_manifest_sha256']
        ),
        'stage7_ticker_count': int(snapshot['ticker_count']),
        'stage7_rank_ready_count': int(snapshot['rank_ready_count']),
        'stage7_review_required_count': int(snapshot['review_required_count']),
        'stage9_root': str(stage9_root),
        'stage9_run_id': str(stage9['contract']['stage9_run_id']),
        'stage9_contract_sha256': str(stage9['contract']['contract_sha256']),
        'stage9_manifest_sha256': str(stage9['manifest']['manifest_sha256']),
        'stage9_decision_sha256': str(stage9['decision']['decision_sha256']),
        'stage9_validation_file_sha256': _file_sha256(
            stage9_root / STAGE9_VALIDATION_FILE
        ),
        'stage8_root': str(stage8_root),
        'factor_validation_root': str(factor_root),
        'factor_validation_verdict': material['factor_validation_verdict'],
        'specialized_weight_policy': 'all_specialized_weights_locked_zero',
        'methodology_file_sha256s': {
            'config.yaml': _file_sha256(bundle.path),
            POLICY_FILE: _file_sha256(bundle.base_dir / 'data' / POLICY_FILE),
            'stage10_publishing.py': _file_sha256(Path(__file__)),
        },
        'stage10_policy': dict(policy),
        'score_semantics': {
            'final_score': 'frozen Stage 7 core score',
            'blank_subscores': (
                'not present in frozen Stage 7 contract; never imputed'
            ),
            'quality_score': 'weighted mean of Stage 7 financial components',
            'market_behavior_score': (
                'weighted mean of Stage 7 market components'
            ),
            'positioning_score': (
                'weighted mean of Stage 7 positioning components'
            ),
            'specialized_measurement_qualified': (
                'accepted measurement-only evidence; not factor acceptance'
            ),
        },
    }
    payload['contract_sha256'] = _sha256(payload)
    payload['stage10_run_id'] = 'cds10_' + payload['contract_sha256'][:24]
    return payload


def _source_tieout_rows(
    contract: Mapping[str, Any],
    stage9: Mapping[str, Any],
    *,
    database_path: Path,
    stage9_root: Path,
) -> list[dict[str, Any]]:
    sources = [
        (
            'S1', 'stage7_score_snapshot', str(database_path),
            str(contract['stage7_output_manifest_sha256']),
            int(contract['stage7_ticker_count']),
            'accepted_frozen_stage7_score_snapshot',
        ),
        (
            'S2', 'stage7_model_contract', str(database_path),
            str(contract['stage7_contract_sha256']), 1,
            'accepted_shadow_only_model_contract',
        ),
        (
            'S3', 'stage9_validation',
            str(stage9_root / STAGE9_VALIDATION_FILE),
            str(contract['stage9_validation_file_sha256']),
            int(stage9['validation']['check_count']),
            'pass_all_checks',
        ),
        (
            'S4', 'stage9_decision',
            str(stage9_root / STAGE9_DECISION_FILE),
            str(stage9['decision']['decision_sha256']), 1,
            'ready_with_caveats_stage7_retained',
        ),
        (
            'S5', 'stage9_summary',
            str(stage9_root / STAGE9_SUMMARY_FILE),
            str(stage9['manifest']['file_sha256s'][STAGE9_SUMMARY_FILE]),
            int(stage9['manifest']['row_counts']['summary']),
            'accepted_report_only_backtest_summary',
        ),
        (
            'S6', 'stage10_policy',
            str(Path(__file__).resolve().parents[1] / 'data' / POLICY_FILE),
            str(contract['methodology_file_sha256s'][POLICY_FILE]), 1,
            'strict_research_only_publishing_policy',
        ),
        (
            'S7', 'rehearsal_database_file', str(database_path),
            str(contract['database_sha256']), 1,
            'read_only_checksum_unchanged',
        ),
    ]
    return [
        _row_hash({
            'citation_id': citation_id,
            'source_name': name,
            'source_path': path,
            'source_sha256': digest,
            'source_row_or_check_count': count,
            'source_status': status,
            'asof_date': contract['asof_date'],
        })
        for citation_id, name, path, digest, count, status in sources
    ]


def _dashboard_cards(
    sector: Mapping[str, Any],
    baseline_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    capacity_count = sum(
        float(row['stress_reference_nav_capacity_pass_fraction']) < 1.0
        for row in baseline_rows
    )
    return [
        {
            'label': 'Score census', 'value': int(sector['ticker_count']),
            'detail': '{} rank-ready / {} review'.format(
                sector['rank_ready_count'], sector['review_required_count']
            ),
            'citation_ids': ['S1'],
        },
        {
            'label': 'Rank-ready coverage',
            'value': float(sector['rank_ready_pct']), 'format': 'percent',
            'detail': 'Frozen Stage 7 shadow score census',
            'citation_ids': ['S1'],
        },
        {
            'label': 'SEC metric coverage',
            'value': float(
                sector['specialized_measurement_coverage_pct'] or 0.0
            ),
            'format': 'percent',
            'detail': '{} of {} pairs'.format(
                sector['specialized_measurement_qualified_observation_count'],
                sector['specialized_applicable_observation_count'],
            ),
            'citation_ids': ['S1'],
        },
        {
            'label': 'Model-weight qualified', 'value': 0,
            'detail': 'of 38 specialized metrics', 'citation_ids': ['S2'],
        },
        {
            'label': 'Stage 9 validation', 'value': '31 / 31',
            'detail': 'Independent checks passed', 'citation_ids': ['S3'],
        },
        {
            'label': 'Reference NAV caveats', 'value': capacity_count,
            'detail': 'stress baseline views below full pass',
            'citation_ids': ['S5'],
        },
    ]


def _dashboard_payload(
    contract: Mapping[str, Any],
    rank_rows: Sequence[Mapping[str, Any]],
    cohort_rows: Sequence[Mapping[str, Any]],
    coverage_rows: Sequence[Mapping[str, Any]],
    baseline_rows: Sequence[Mapping[str, Any]],
    risk_rows: Sequence[Mapping[str, Any]],
    review_rows: Sequence[Mapping[str, Any]],
    tieout_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    sector = next(row for row in cohort_rows if row['scope_type'] == 'sector')
    sector_coverage = [
        row for row in coverage_rows if row['scope_type'] == 'sector'
    ]
    sector_coverage.sort(key=lambda row: (
        row['measurement_coverage_pct'] == '',
        float(row['measurement_coverage_pct'] or 0.0),
        str(row['metric_id']),
    ))
    return {
        'schema_version': 'consumer_defensive_stage10_dashboard_payload_v1',
        'title': 'Consumer Staples Research Dashboard',
        'subtitle': 'Consumer Defensive cohort scoring and evidence monitor',
        'asof_date': contract['asof_date'],
        'generation_timestamp': contract['generation_timestamp'],
        'readiness': {
            'label': contract['readiness_label'],
            'status': 'research_only_not_investable',
            'portfolio_candidate_gate': 0, 'oos_score_valid_flag': 0,
            'production_promotion_enabled': False,
            'portfolio_write_enabled': False,
            'citation_ids': ['S1', 'S3', 'S4'],
        },
        'first_read': {
            'what_changed': (
                'Stage 10 publishes frozen Stage 7 ranks and accepted Stage 9 '
                'risk evidence without changing scores or portfolio state.'
            ),
            'what_looks_attractive': (
                'Top shadow ranks guide research triage, not trading.'
            ),
            'what_can_break': (
                'No OOS score exists; review, factor, and capacity gaps remain.'
            ),
            'decision': (
                'Keep shadow monitoring and proceed to Stage 10B comparison.'
            ),
            'citation_ids': ['S1', 'S3', 'S4', 'S5'],
        },
        'cards': _dashboard_cards(sector, baseline_rows),
        'top_ranks': list(rank_rows),
        'cohorts': list(cohort_rows),
        'specialized_sector_coverage': sector_coverage,
        'stage9_baseline': list(baseline_rows),
        'risks': list(risk_rows),
        'review_queue': list(review_rows),
        'sources': list(tieout_rows),
        'definitions': {
            'measurement_qualified': (
                'Accepted numeric measurement-only SEC evidence with lineage.'
            ),
            'model_weight_qualified': (
                'Independently accepted directional factor evidence; current '
                'count is zero, so every specialized weight remains zero.'
            ),
            'rank_ready': 'Passed frozen Stage 7 core-data thresholds.',
        },
        'downloads': [
            FINAL_RANK_FILE, SCORECARD_FILE, COHORT_FILE, RISK_FILE, REVIEW_FILE,
            SPECIALIZED_FILE, TICKER_SPECIALIZED_FILE, STAGE9_BASELINE_FILE,
            TIEOUT_FILE, CONTRACT_FILE, MANIFEST_FILE,
        ],
        'stage10_run_id': contract['stage10_run_id'],
        'contract_sha256': contract['contract_sha256'],
    }


def _fmt(value: Any, kind: str = '') -> str:
    if value in (None, ''):
        return ''
    if kind == 'percent':
        return f'{float(value):.1f}%'
    if kind == 'score':
        return f'{float(value):.2f}'
    if kind == 'ratio_percent':
        return f'{100.0 * float(value):.1f}%'
    if kind == 'usd':
        number = float(value)
        if abs(number) >= 1_000_000_000:
            return f'USD {number / 1_000_000_000:.1f}B'
        if abs(number) >= 1_000_000:
            return f'USD {number / 1_000_000:.1f}M'
        return f'USD {number:,.0f}'
    return str(value)


def _cite(ids: Iterable[str]) -> str:
    return ''.join(
        f'<a class=cite href=#source-{html.escape(value)}>'
        f'[{html.escape(value)}]</a>'
        for value in ids
    )


def _table(
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[tuple[str, str, str]],
    *,
    css_class: str = '',
) -> str:
    heads = ''.join(f'<th>{html.escape(label)}</th>' for _, label, _ in columns)
    bodies: list[str] = []
    for row in rows:
        cells = ''.join(
            f'<td>{html.escape(_fmt(row.get(key), kind))}</td>'
            for key, _label, kind in columns
        )
        bodies.append(f'<tr>{cells}</tr>')
    body = ''.join(bodies)
    return (
        f'<div class=table-wrap><table class={html.escape(css_class)}>'
        f'<thead><tr>{heads}</tr></thead><tbody>'
        f'{body}</tbody></table></div>'
    )


def render_dashboard(payload: Mapping[str, Any]) -> str:
    cards = ''.join(
        '<article class=metric-card><p>{}</p><strong>{}</strong>'
        '<span>{} {}</span></article>'.format(
            html.escape(str(card['label'])),
            html.escape(_fmt(card['value'], str(card.get('format', '')))),
            html.escape(str(card['detail'])),
            _cite(card.get('citation_ids', [])),
        )
        for card in payload['cards']
    )
    first = payload['first_read']
    first_read = ''.join(
        '<article><h3>{}</h3><p>{}</p></article>'.format(
            label, html.escape(str(first[key]))
        )
        for key, label in (
            ('what_changed', 'What changed'),
            ('what_looks_attractive', 'What looks attractive'),
            ('what_can_break', 'What can break'),
            ('decision', 'Decision now'),
        )
    )
    ranks = _table(
        payload['top_ranks'][:25],
        (
            ('final_rank', 'Rank', ''), ('ticker', 'Ticker', ''),
            ('company_name', 'Company', ''), ('final_score', 'Score', 'score'),
            ('calibration_cohort', 'Cohort', ''),
            ('score_confidence', 'Confidence', 'ratio_percent'),
            ('model_status', 'Status', ''),
        ),
        css_class='ranks',
    )
    cohorts = _table(
        payload['cohorts'],
        (
            ('scope_id', 'Scope', ''), ('ticker_count', 'Names', ''),
            ('rank_ready_pct', 'Rank ready', 'percent'),
            ('median_final_score', 'Median score', 'score'),
            ('top_ranked_ticker', 'Leader', ''),
            ('specialized_measurement_coverage_pct', 'SEC coverage', 'percent'),
            ('portfolio_eligible_ticker_count', 'Portfolio eligible', ''),
        ),
    )
    coverage = _table(
        payload['specialized_sector_coverage'],
        (
            ('metric_label', 'Specialized metric', ''),
            ('applicable_ticker_count', 'Applicable', ''),
            ('measurement_qualified_ticker_count', 'Qualified', ''),
            ('measurement_coverage_pct', 'Coverage', 'percent'),
            ('missing_applicable_tickers', 'Missing tickers', ''),
        ),
    )
    backtest = _table(
        payload['stage9_baseline'],
        (
            ('scope_id', 'Scope', ''), ('portfolio_name', 'Portfolio', ''),
            ('weight_method', 'Weighting', ''), ('return_basis', 'Basis', ''),
            ('observed_annualized_return', 'Annual return', 'ratio_percent'),
            ('observed_maximum_drawdown', 'Max drawdown', 'ratio_percent'),
            ('stress_reference_nav_capacity_pass_fraction', 'Stress capacity', 'ratio_percent'),
            ('maximum_days_to_liquidate_reference_nav', 'Max exit days', 'score'),
        ),
    )
    reviews = _table(
        payload['review_queue'],
        (
            ('review_priority', 'Priority', ''), ('ticker', 'Ticker', ''),
            ('company_name', 'Company', ''), ('score_confidence', 'Confidence', 'ratio_percent'),
            ('review_reason', 'Why review', ''),
            ('missing_core_components', 'Missing core', ''),
        ),
    )
    risks = ''.join(
        '<article class=risk><span class=severity>{}</span><h3>{}</h3>'
        '<p>{}</p><small>{}</small></article>'.format(
            html.escape(str(row['severity']).upper()),
            html.escape(str(row['scope_id'])),
            html.escape(str(row['risk_detail'])),
            html.escape(str(row['evidence'])),
        )
        for row in payload['risks']
    )
    sources = ''.join(
        '<li id=source-{}><strong>[{}] {}</strong><span>{}</span>'
        '<code>{}</code></li>'.format(
            html.escape(str(row['citation_id'])),
            html.escape(str(row['citation_id'])),
            html.escape(str(row['source_name'])),
            html.escape(str(row['source_status'])),
            html.escape(str(row['source_sha256'])),
        )
        for row in payload['sources']
    )
    downloads = ''.join(
        '<a href={} download>{}</a>'.format(
            html.escape(str(name)), html.escape(str(name))
        )
        for name in payload['downloads']
    )
    return _dashboard_template().format(
        title=html.escape(str(payload['title'])),
        subtitle=html.escape(str(payload['subtitle'])),
        asof=html.escape(str(payload['asof_date'])),
        readiness=html.escape(str(payload['readiness']['label'])),
        readiness_cites=_cite(payload['readiness']['citation_ids']),
        cards=cards, first_read=first_read,
        first_cites=_cite(first['citation_ids']),
        ranks=ranks, cohorts=cohorts, coverage=coverage, backtest=backtest,
        reviews=reviews, risks=risks, sources=sources, downloads=downloads,
        run_id=html.escape(str(payload['stage10_run_id'])),
        generated=html.escape(str(payload['generation_timestamp'])),
    )


def _dashboard_template() -> str:
    return '''<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta name=viewport content=width=device-width,initial-scale=1>
<title>{title}</title>
<style>
:root {{
  --ink:#10262b;--muted:#597077;--paper:#f4f1e9;--card:#fffdf7;
  --line:#d8d5ca;--teal:#0b6b65;--navy:#123c4a;--orange:#ec6b36;
  --red:#a63131;--shadow:0 14px 40px rgba(16,38,43,.10)
}}
* {{box-sizing:border-box}}
html {{scroll-behavior:smooth}}
body {{
  margin:0;background:var(--paper);color:var(--ink);
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;
  font-variant-numeric:tabular-nums;overflow-x:hidden
}}
a {{color:var(--teal)}} .cite {{font-size:.7em;margin-left:.25rem}}
.shell {{max-width:1440px;margin:auto;padding:28px}}
.hero {{
  position:relative;overflow:hidden;border-radius:26px;padding:42px;
  color:white;background:linear-gradient(120deg,var(--navy),#075d59);
  box-shadow:var(--shadow)
}}
.hero:after {{
  content:'';position:absolute;width:420px;height:420px;border-radius:50%;
  right:-160px;top:-230px;background:rgba(236,107,54,.28)
}}
.eyebrow {{text-transform:uppercase;letter-spacing:.16em;font-size:.72rem;font-weight:800}}
h1 {{font-size:clamp(2rem,4vw,4.2rem);line-height:.98;margin:.5rem 0 1rem;max-width:850px;overflow-wrap:anywhere}}
.subtitle {{font-size:1.1rem;color:#d3ece8;max-width:760px}}
.readiness {{
  display:inline-flex;align-items:center;gap:.5rem;margin-top:20px;padding:9px 14px;
  border:1px solid rgba(255,255,255,.32);border-radius:99px;
  background:rgba(4,24,29,.28);font-weight:800
}}
.readiness:before {{content:'';width:9px;height:9px;border-radius:50%;background:#ff986e}}
.meta {{margin-top:16px;color:#c8dedc;font-size:.82rem}}
.metrics {{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin:18px 0}}
.metric-card,.panel {{
  background:var(--card);border:1px solid var(--line);border-radius:18px;
  box-shadow:0 7px 25px rgba(16,38,43,.055)
}}
.metric-card {{padding:18px;min-height:150px;min-width:0}}
.metric-card p {{margin:0;color:var(--muted);font-size:.78rem;font-weight:800;text-transform:uppercase}}
.metric-card strong {{display:block;font-size:2rem;margin:16px 0 8px}}
.metric-card span {{display:block;font-size:.78rem;color:var(--muted);line-height:1.4}}
.panel {{padding:24px;margin:18px 0}}
.panel-head {{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-bottom:18px}}
.panel-head h2 {{margin:0;font-size:1.35rem}} .panel-head p {{margin:0;color:var(--muted);font-size:.82rem}}
.first-read {{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.first-read article {{padding:16px;border-left:3px solid var(--teal);background:#f3f7f4;border-radius:8px}}
.first-read h3 {{font-size:.78rem;text-transform:uppercase;margin:0 0 8px;color:var(--teal)}}
.first-read p {{font-size:.9rem;line-height:1.5;margin:0}}
.table-wrap {{width:100%;overflow:auto;border:1px solid var(--line);border-radius:12px}}
table {{border-collapse:collapse;width:100%;min-width:820px;background:white}}
th,td {{padding:11px 12px;text-align:left;border-bottom:1px solid #ece9df;vertical-align:top}}
th {{
  position:sticky;top:0;background:#eef3f0;color:#38565b;
  font-size:.7rem;text-transform:uppercase;letter-spacing:.06em
}}
td {{font-size:.79rem;line-height:1.35}} tbody tr:hover {{background:#fff8ee}}
.risk-grid {{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;max-height:520px;overflow:auto}}
.risk {{border:1px solid #ead9cf;border-left:4px solid var(--orange);padding:14px;border-radius:10px;background:#fffaf3}}
.risk h3 {{font-size:.85rem;margin:7px 0}} .risk p {{font-size:.8rem;margin:0 0 8px;line-height:1.4}}
.risk small {{color:var(--muted);word-break:break-word}}
.severity {{font-size:.65rem;font-weight:900;color:var(--red);letter-spacing:.08em}}
.downloads {{display:flex;flex-wrap:wrap;gap:8px}}
.downloads a {{padding:8px 10px;border-radius:8px;background:#e8f2ef;text-decoration:none;font-size:.75rem}}
.sources {{list-style:none;padding:0;margin:0;display:grid;gap:8px}}
.sources li {{display:grid;gap:4px;padding:12px;background:#f2f4ef;border-radius:9px}}
.sources span {{font-size:.76rem;color:var(--muted)}} .sources code {{font-size:.67rem;word-break:break-all}}
footer {{padding:22px 4px 40px;color:var(--muted);font-size:.75rem}}
@media(max-width:1100px) {{
  .metrics {{grid-template-columns:repeat(3,1fr)}}
  .first-read {{grid-template-columns:repeat(2,1fr)}}
}}
@media(max-width:680px) {{
  .shell {{padding:12px}} .hero {{padding:26px 20px;border-radius:18px}}
  .metrics {{grid-template-columns:1fr}}
  .readiness {{max-width:100%;flex-wrap:wrap}}
  .metric-card {{min-height:130px}} .first-read,.risk-grid {{grid-template-columns:1fr}}
  .panel {{padding:16px}} .panel-head {{align-items:start;flex-direction:column}}
}}
</style>
</head>
<body><main class=shell>
<header class=hero>
  <div class=eyebrow>Stage 10 / deterministic publishing</div>
  <h1>{title}</h1><p class=subtitle>{subtitle}</p>
  <div class=readiness>{readiness} {readiness_cites}</div>
  <div class=meta>As of {asof} / frozen source time {generated}</div>
</header>
<section class=metrics>{cards}</section>
<section class=panel>
  <div class=panel-head><h2>First read {first_cites}</h2><p>Decision-oriented research framing</p></div>
  <div class=first-read>{first_read}</div>
</section>
<section class=panel>
  <div class=panel-head><h2>Top shadow ranks [S1]</h2><p>Top 25 of the complete downloadable rank table</p></div>{ranks}
</section>
<section class=panel>
  <div class=panel-head><h2>Cohort readiness [S1]</h2><p>Independent cohorts; no cross-cohort promotion</p></div>{cohorts}
</section>
<section class=panel>
  <div class=panel-head><h2>Specialized SEC evidence [S1][S2]</h2><p>Measurement coverage is not model-weight acceptance</p></div>{coverage}
</section>
<section class=panel>
  <div class=panel-head><h2>Stage 9 baseline risk and capacity [S3][S5]</h2><p>Report-only deep replay</p></div>{backtest}
</section>
<section class=panel>
  <div class=panel-head><h2>Open risks</h2><p>Visible blockers and sizing constraints</p></div>
  <div class=risk-grid>{risks}</div>
</section>
<section class=panel>
  <div class=panel-head><h2>Review queue [S1]</h2><p>Core-data exceptions requiring resolution</p></div>{reviews}
</section>
<section class=panel>
  <div class=panel-head><h2>Downloads</h2><p>Machine-readable deterministic artifacts</p></div>
  <div class=downloads>{downloads}</div>
</section>
<section class=panel>
  <div class=panel-head><h2>Source ledger</h2><p>Every first-read claim ties to accepted evidence</p></div>
  <ol class=sources>{sources}</ol>
</section>
<footer>Run {run_id} / Research only / No portfolio write / No production promotion.</footer>
</main></body></html>
'''


def _artifact_texts(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    asof_date: str,
    database_path: Path,
    stage9_root: Path,
    stage8_root: Path,
    factor_root: Path,
) -> tuple[dict[str, str], dict[str, Any]]:
    policy = stage10_policy(bundle)
    database_sha256 = _file_sha256(database_path)
    stage9 = _verify_stage9_root(stage9_root, policy)
    material = _stage7_material(conn, bundle, asof_date=asof_date)
    if (
        stage9['contract']['stage7_source_id'] != material['source_id']
        or stage9['contract']['stage7_contract_sha256']
        != material['snapshot']['contract_sha256']
    ):
        raise RuntimeError('Stage 9 does not bind the selected Stage 7 snapshot.')
    rank_rows = _rank_rows(material, policy, asof_date=asof_date)
    if len(rank_rows) < int(policy['minimum_ticker_count']):
        raise RuntimeError('Stage 10 score census is below its frozen floor.')
    rank_fraction = sum(
        int(row['rank_ready_flag']) for row in rank_rows
    ) / len(rank_rows)
    if rank_fraction < float(policy['minimum_rank_ready_fraction']):
        raise RuntimeError('Stage 10 rank-ready coverage is below its floor.')
    scorecards = _scorecard_rows(material, rank_rows)
    coverage, ticker_coverage = _specialized_coverage_rows(
        scorecards, rank_rows,
        factor_verdict=material['factor_validation_verdict'],
    )
    cohorts = _cohort_rows(rank_rows, ticker_coverage)
    baseline = _stage9_baseline_rows(stage9)
    reviews = _review_rows(rank_rows, ticker_coverage)
    risks = _risk_rows(
        rank_rows, baseline, asof_date=asof_date,
        factor_verdict=material['factor_validation_verdict'],
    )
    contract = _contract_payload(
        bundle, policy, material, stage9, asof_date=asof_date,
        database_path=database_path, database_sha256=database_sha256,
        stage9_root=stage9_root, stage8_root=stage8_root,
        factor_root=factor_root,
    )
    tieout = _source_tieout_rows(
        contract, stage9, database_path=database_path, stage9_root=stage9_root
    )
    payload = _dashboard_payload(
        contract, rank_rows, cohorts, coverage, baseline, risks, reviews, tieout
    )
    html_text = render_dashboard(payload)
    row_sets = {
        FINAL_RANK_FILE: rank_rows,
        SCORECARD_FILE: scorecards,
        COHORT_FILE: cohorts,
        RISK_FILE: risks,
        REVIEW_FILE: reviews,
        SPECIALIZED_FILE: coverage,
        TICKER_SPECIALIZED_FILE: ticker_coverage,
        STAGE9_BASELINE_FILE: baseline,
        TIEOUT_FILE: tieout,
    }
    texts = {
        CONTRACT_FILE: _json_text(contract),
        **{name: _csv_text(rows) for name, rows in row_sets.items()},
        PAYLOAD_FILE: _json_text(payload),
        HTML_FILE: html_text,
    }
    file_hashes = {
        name: hashlib.sha256(value.encode('utf-8')).hexdigest()
        for name, value in texts.items()
    }
    manifest = {
        'schema_version': 'consumer_defensive_stage10_artifact_manifest_v1',
        'stage10_run_id': contract['stage10_run_id'],
        'asof_date': asof_date,
        'contract_sha256': contract['contract_sha256'],
        'readiness_status': 'research_only_not_investable',
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'database_write_count': 0,
        'file_sha256s': file_hashes,
        'row_counts': {name: len(rows) for name, rows in row_sets.items()},
        'logical_sha256s': {
            name: _sha256([row['row_sha256'] for row in rows])
            for name, rows in row_sets.items()
        },
    }
    manifest['manifest_sha256'] = _sha256(manifest)
    texts[MANIFEST_FILE] = _json_text(manifest)
    return texts, {
        'contract': contract, 'manifest': manifest, 'row_sets': row_sets,
        'payload': payload, 'database_sha256': database_sha256,
    }


def publish_stage10(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    asof_date: str,
    database_path: Path,
    stage9_root: Path,
    stage8_root: Path,
    factor_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    date.fromisoformat(asof_date)
    database = database_path.expanduser().resolve()
    stage9 = stage9_root.expanduser().resolve()
    stage8 = stage8_root.expanduser().resolve()
    factor = factor_root.expanduser().resolve()
    root = output_root.expanduser().resolve()
    dated = root / asof_date / str(stage10_policy(bundle)['output_version'])
    _assert_directory_safe(dated)
    texts, computed = _artifact_texts(
        conn, bundle, asof_date=asof_date, database_path=database,
        stage9_root=stage9, stage8_root=stage8, factor_root=factor,
    )
    for name, content in texts.items():
        _immutable_text(dated / name, content)
    after_sha256 = _file_sha256(database)
    if after_sha256 != computed['database_sha256']:
        raise RuntimeError('Rehearsal database changed during Stage 10 publishing.')
    return {
        'status': 'PASS',
        'stage': 'stage10_deterministic_publishing',
        'asof_date': asof_date,
        'output_dir': str(dated),
        'latest_status': 'pending_independent_validation',
        'stage10_run_id': computed['contract']['stage10_run_id'],
        'contract_sha256': computed['contract']['contract_sha256'],
        'manifest_sha256': computed['manifest']['manifest_sha256'],
        'ticker_count': computed['manifest']['row_counts'][FINAL_RANK_FILE],
        'rank_ready_count': computed['contract']['stage7_rank_ready_count'],
        'review_required_count': computed['contract'][
            'stage7_review_required_count'
        ],
        'specialized_measurement_coverage_pct': computed['payload']['cards'][2][
            'value'
        ],
        'specialized_model_weight_qualified_count': 0,
        'database_access_mode': 'read_only',
        'database_write_count': 0,
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'portfolio_candidate_gate': 0,
        'oos_score_valid_flag': 0,
    }


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    **details: Any,
) -> None:
    checks.append({
        'check': name, 'status': 'PASS' if passed else 'FAIL', **details,
    })


def validate_stage10_artifacts(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    asof_date: str,
    database_path: Path,
    stage9_root: Path,
    stage8_root: Path,
    factor_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    database = database_path.expanduser().resolve()
    stage9 = stage9_root.expanduser().resolve()
    stage8 = stage8_root.expanduser().resolve()
    factor = factor_root.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    database_before = _file_sha256(database)
    try:
        stage9_validation = validate_stage9_artifacts(
            conn, bundle, stage8_root=stage8, factor_root=factor,
            output_dir=stage9,
        )
        _check(
            checks, 'upstream_stage9_independent_validation',
            stage9_validation.get('status') == 'PASS'
            and stage9_validation.get('passed_check_count')
            == stage9_validation.get('check_count'),
            upstream_checks=stage9_validation.get('check_count'),
        )
        expected, computed = _artifact_texts(
            conn, bundle, asof_date=asof_date, database_path=database,
            stage9_root=stage9, stage8_root=stage8, factor_root=factor,
        )
    except Exception as exc:
        errors.append(str(exc))
        return {
            'schema_version': 'consumer_defensive_stage10_validation_v1',
            'status': 'FAIL', 'asof_date': asof_date,
            'errors': errors, 'checks': checks,
            'check_count': max(1, len(checks)),
            'passed_check_count': sum(
                row['status'] == 'PASS' for row in checks
            ),
            'production_promotion_enabled': False,
            'portfolio_write_enabled': False,
        }

    required = set(expected)
    observed = {
        path.name for path in output.iterdir()
        if path.is_file() and path.name != VALIDATION_FILE
    } if output.is_dir() else set()
    _check(
        checks, 'artifact_file_census_exact', observed == required,
        missing=sorted(required - observed), unexpected=sorted(observed - required),
    )
    mismatched = [
        name for name, content in expected.items()
        if not (output / name).is_file()
        or (output / name).read_text(encoding='utf-8') != content
    ]
    _check(
        checks, 'all_artifact_bytes_recompute_exactly', not mismatched,
        mismatched=mismatched,
    )
    manifest = _read_json(output / MANIFEST_FILE) if (
        output / MANIFEST_FILE
    ).is_file() else {}
    _check(
        checks, 'manifest_self_hash',
        manifest.get('manifest_sha256')
        == _self_hash(manifest, 'manifest_sha256'),
    )
    file_hash_errors = [
        name for name, digest in dict(manifest.get('file_sha256s') or {}).items()
        if not (output / name).is_file()
        or _file_sha256(output / name) != str(digest)
    ]
    _check(
        checks, 'manifest_file_hashes_exact', not file_hash_errors,
        mismatched=file_hash_errors,
    )
    contract = _read_json(output / CONTRACT_FILE) if (
        output / CONTRACT_FILE
    ).is_file() else {}
    contract_core = dict(contract)
    observed_contract_hash = str(contract_core.pop('contract_sha256', ''))
    observed_run_id = str(contract_core.pop('stage10_run_id', ''))
    _check(
        checks, 'contract_self_hash_and_run_id',
        observed_contract_hash == _sha256(contract_core)
        and observed_run_id == 'cds10_' + observed_contract_hash[:24],
    )
    _check(
        checks, 'contract_source_bindings_exact',
        contract.get('stage7_contract_sha256')
        == computed['contract']['stage7_contract_sha256']
        and contract.get('stage7_output_manifest_sha256')
        == computed['contract']['stage7_output_manifest_sha256']
        and contract.get('stage9_contract_sha256')
        == computed['contract']['stage9_contract_sha256']
        and contract.get('stage9_manifest_sha256')
        == computed['contract']['stage9_manifest_sha256'],
    )
    rank_rows = computed['row_sets'][FINAL_RANK_FILE]
    _check(
        checks, 'final_rank_schema_complete',
        bool(rank_rows)
        and set(FINAL_RANK_REQUIRED_FIELDS).issubset(rank_rows[0]),
    )
    _check(
        checks, 'all_rank_rows_shadow_and_noninvestable',
        all(
            row['promotion_state'] == 'shadow_monitor'
            and int(row['portfolio_candidate_gate']) == 0
            and int(row['oos_score_valid_flag']) == 0
            and row['portfolio_candidate_status'] == 'blocked'
            for row in rank_rows
        ),
    )
    rank_ready = [
        row for row in rank_rows if int(row['rank_ready_flag']) == 1
    ]
    expected_order = sorted(
        rank_ready,
        key=lambda row: (-float(row['final_score']), str(row['ticker'])),
    )
    _check(
        checks, 'rank_order_and_tie_break_exact',
        all(
            int(row['final_rank']) == index
            for index, row in enumerate(expected_order, 1)
        ),
    )
    scorecards = computed['row_sets'][SCORECARD_FILE]
    core_count = sum(
        row['component_group'] != 'specialized' for row in scorecards
    )
    specialized_count = len(scorecards) - core_count
    _check(
        checks, 'company_scorecard_census_exact',
        core_count == len(rank_rows) * len(CORE_COMPONENT_SPECS)
        and specialized_count
        == len(rank_rows)
        * int(stage10_policy(bundle)['expected_specialized_component_count']),
        core_rows=core_count, specialized_rows=specialized_count,
    )
    specialized_rows = computed['row_sets'][SPECIALIZED_FILE]
    _check(
        checks, 'specialized_qualification_semantics_separated',
        all(int(row['model_weight_qualified_flag']) == 0 for row in specialized_rows)
        and any(
            int(row['measurement_qualified_ticker_count']) > 0
            for row in specialized_rows
        ),
    )
    ticker_coverage = computed['row_sets'][TICKER_SPECIALIZED_FILE]
    _check(
        checks, 'specialized_ticker_coverage_arithmetic',
        all(
            int(row['measurement_qualified_metric_count'])
            + int(row['missing_applicable_metric_count'])
            == int(row['applicable_metric_count'])
            for row in ticker_coverage
        ),
    )
    review_rows = computed['row_sets'][REVIEW_FILE]
    _check(
        checks, 'review_queue_matches_unranked_census',
        {row['ticker'] for row in review_rows}
        == {
            row['ticker'] for row in rank_rows
            if int(row['rank_ready_flag']) == 0
        },
    )
    baseline_rows = computed['row_sets'][STAGE9_BASELINE_FILE]
    _check(
        checks, 'stage9_baseline_views_report_only',
        len(baseline_rows) == 40
        and all(
            int(row['oos_score_valid_flag']) == 0
            and int(row['promotion_eligible_flag']) == 0
            for row in baseline_rows
        ),
    )
    html_text = expected[HTML_FILE]
    _check(
        checks, 'dashboard_readiness_and_source_labels_visible',
        'not investable' in html_text.casefold()
        and 'No portfolio write' in html_text
        and all(
            'source-' + citation in html_text
            for citation in ('S1', 'S2', 'S3', 'S4', 'S5')
        )
        and 'TODO' not in html_text,
    )
    database_after = _file_sha256(database)
    _check(
        checks, 'database_write_count_zero_and_checksum_unchanged',
        database_before == database_after == computed['database_sha256'],
    )
    failed = [row['check'] for row in checks if row['status'] != 'PASS']
    return {
        'schema_version': 'consumer_defensive_stage10_validation_v1',
        'status': 'PASS' if not failed else 'FAIL',
        'asof_date': asof_date,
        'stage10_run_id': computed['contract']['stage10_run_id'],
        'contract_sha256': computed['contract']['contract_sha256'],
        'manifest_sha256': computed['manifest']['manifest_sha256'],
        'checks': checks, 'errors': errors, 'failed_checks': failed,
        'check_count': len(checks),
        'passed_check_count': sum(row['status'] == 'PASS' for row in checks),
        'permitted_use': 'stage10b_cross_sector_comparison_input',
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'portfolio_candidate_gate': 0,
        'oos_score_valid_flag': 0,
        'database_access_mode': 'read_only',
        'database_write_count': 0,
        'ticker_count': len(rank_rows),
        'rank_ready_count': len(rank_ready),
        'review_required_count': len(review_rows),
        'specialized_model_weight_qualified_count': 0,
    }


def write_stage10_validation(
    output_dir: Path,
    output_root: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    dated = output_dir.expanduser().resolve()
    root = output_root.expanduser().resolve()
    validation_text = _json_text(dict(payload))
    _replace_text(dated / VALIDATION_FILE, validation_text)
    if payload.get('status') != 'PASS':
        return {'latest_status': 'not_updated_validation_failed'}
    latest = root / 'latest'
    _assert_directory_safe(latest)
    existing_contract_path = latest / CONTRACT_FILE
    if existing_contract_path.is_file():
        existing = _read_json(existing_contract_path)
        if str(existing.get('asof_date', '')) > str(payload['asof_date']):
            raise RuntimeError(
                'Refusing to regress the Stage 10 latest snapshot date.'
            )
    manifest = _read_json(dated / MANIFEST_FILE)
    names = [
        *dict(manifest['file_sha256s']),
        MANIFEST_FILE,
        VALIDATION_FILE,
    ]
    for name in names:
        source = dated / name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f'Invalid dated artifact for latest: {source}')
        _replace_text(latest / name, source.read_text(encoding='utf-8'))
    mismatched = [
        name for name in names
        if _file_sha256(dated / name) != _file_sha256(latest / name)
    ]
    if mismatched:
        raise RuntimeError(f'Latest Stage 10 sync mismatch: {mismatched}')
    return {
        'latest_status': 'updated_after_passing_validation',
        'latest_dir': str(latest),
        'latest_file_count': len(names),
    }
