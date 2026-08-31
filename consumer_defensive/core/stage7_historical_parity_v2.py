from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ConfigBundle, cfg_get
from .market_data import MarketDataPolicy
from .scoring_features import CORE_COMPONENT_SPECS
from .stage7_scoring import (
    OUTPUT_IDENTITY_FIELDS,
    score_observation_id,
    stage7_component_weights,
)
from .stage8_calibration import (
    _financial_features_for_date,
    _market_features_for_date,
    _normalize_component_rows,
    _positioning_features_for_date,
    _price_selection_and_history,
    _rank_requirements,
)


STAGE7_MANIFEST_FILE = 'stage7_build_manifest.json'
STAGE7_SCORE_FILE = 'stage7_shadow_scores.csv'

_FLOAT_IDENTITY_FIELDS = {
    'core_score',
    'final_score',
    'final_percentile',
    'cohort_percentile',
    'data_quality_confidence',
    'full_data_quality_confidence',
}
_INTEGER_IDENTITY_FIELDS = {
    'final_rank',
    'cohort_rank',
    'rank_ready_flag',
    'calibration_eligible_flag',
    'portfolio_candidate_gate',
    'oos_score_valid_flag',
}
_OPTIONAL_IDENTITY_FIELDS = {
    'final_rank',
    'final_percentile',
    'cohort_rank',
    'cohort_percentile',
    'review_reason',
}
_CURRENT_PARITY_FIELDS = (
    'component_weights_json',
    'component_scores_json',
    'component_quality_json',
    'core_score',
    'data_quality_confidence',
    'rank_ready_flag',
    'calibration_eligible_flag',
)
_LEGACY_REQUIRED_PROVENANCE_FIELDS = (
    'stage7_stage8_current_asof_parity_manifest_sha256',
    'historical_price_bar_manifest_sha256',
    'historical_component_source_manifest_sha256',
    'historical_score_panel_manifest_sha256',
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
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


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f'Invalid JSON object in {field}.') from exc
    if not isinstance(parsed, dict):
        raise ValueError(f'Expected JSON object in {field}.')
    return parsed


def _typed_identity_row(row: Mapping[str, Any]) -> dict[str, Any]:
    typed: dict[str, Any] = {}
    for field in OUTPUT_IDENTITY_FIELDS:
        value = row.get(field)
        if field in _OPTIONAL_IDENTITY_FIELDS and value in {'', None}:
            typed[field] = None
        elif field in _FLOAT_IDENTITY_FIELDS:
            number = _finite(value)
            if number is None:
                raise ValueError(f'Stage 7 identity field {field} is not finite.')
            typed[field] = number
        elif field in _INTEGER_IDENTITY_FIELDS:
            try:
                typed[field] = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f'Stage 7 identity field {field} is not an integer.'
                ) from exc
        else:
            typed[field] = str(value)
    return typed


def load_stage7_scores(path: Path) -> list[dict[str, Any]]:
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def audit_stage7_artifact_seal(stage7_root: Path) -> dict[str, Any]:
    manifest_path = stage7_root / STAGE7_MANIFEST_FILE
    score_path = stage7_root / STAGE7_SCORE_FILE
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict):
        raise ValueError('Stage 7 build manifest must be a JSON object.')
    build = manifest.get('build')
    validation = manifest.get('validation')
    if not isinstance(build, dict) or not isinstance(validation, dict):
        raise ValueError('Stage 7 build manifest is missing build/validation objects.')
    rows = load_stage7_scores(score_path)
    identity_errors: list[str] = []
    typed_rows: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row.get('ticker', ''))
        try:
            typed = _typed_identity_row(row)
            expected_id = score_observation_id(typed)
        except ValueError:
            identity_errors.append(ticker)
            continue
        typed_rows.append(typed)
        if str(row.get('score_observation_id', '')) != expected_id:
            identity_errors.append(ticker)

    ordered = sorted(rows, key=lambda row: str(row.get('ticker', '')))
    output_manifest = _sha256([
        str(row.get('score_observation_id', '')) for row in ordered
    ])
    baseline_manifest = _sha256([
        str(row.get('baseline_input_observation_id', '')) for row in ordered
    ])
    unique_tickers = {str(row.get('ticker', '')) for row in rows}
    consistent_fields = all(
        str(row.get('asof_date', '')) == str(build.get('asof_date', ''))
        and str(row.get('source_id', '')) == str(build.get('source_id', ''))
        and str(row.get('model_version', ''))
        == str(build.get('model_version', ''))
        and str(row.get('model_contract_sha256', ''))
        == str(build.get('contract_sha256', ''))
        for row in rows
    )
    validation_checks = validation.get('checks', [])
    validation_checks_pass = (
        isinstance(validation_checks, list)
        and bool(validation_checks)
        and all(
            isinstance(check, dict) and bool(check.get('passed'))
            for check in validation_checks
        )
    )
    checks = {
        'build_status_pass': str(build.get('status')) == 'PASS',
        'validation_status_pass': str(validation.get('status')) == 'PASS',
        'validation_checks_pass': validation_checks_pass,
        'ticker_rows_unique_and_complete': (
            bool(rows)
            and len(rows) == len(unique_tickers)
            and len(rows) == int(build.get('ticker_count', -1))
        ),
        'row_identity_recomputed_exact': not identity_errors,
        'output_manifest_recomputed_exact': (
            output_manifest == str(build.get('output_manifest_sha256', ''))
        ),
        'baseline_input_manifest_recomputed_exact': (
            baseline_manifest
            == str(build.get('baseline_input_manifest_sha256', ''))
        ),
        'score_contract_fields_consistent': consistent_fields,
        'validation_contract_consistent': (
            str(validation.get('contract_sha256', ''))
            == str(build.get('contract_sha256', ''))
            and str(validation.get('source_id', ''))
            == str(build.get('source_id', ''))
        ),
    }
    return {
        'stage7_output_identity_sealed': all(checks.values()),
        'checks': checks,
        'asof_date': str(build.get('asof_date', '')),
        'source_id': str(build.get('source_id', '')),
        'model_version': str(build.get('model_version', '')),
        'contract_sha256': str(build.get('contract_sha256', '')),
        'baseline_input_manifest_sha256': baseline_manifest,
        'output_manifest_sha256': output_manifest,
        'ticker_count': len(rows),
        'rank_ready_count': sum(
            int(row.get('rank_ready_flag', 0)) == 1 for row in rows
        ),
        'identity_error_tickers': sorted(set(identity_errors)),
        'score_file_sha256': _file_sha256(score_path),
        'manifest_file_sha256': _file_sha256(manifest_path),
        'rows': rows,
    }


def methodology_identity_audit(
    *,
    stage8_contract: Mapping[str, Any],
    package_root: Path,
) -> dict[str, Any]:
    expected = stage8_contract.get('methodology_file_sha256s', {})
    if not isinstance(expected, dict):
        expected = {}
    locations = {
        'config.yaml': package_root / 'config.yaml',
        'financial_pipeline.py': package_root / 'core' / 'financial_pipeline.py',
        'market_data.py': package_root / 'core' / 'market_data.py',
        'scoring_features.py': package_root / 'core' / 'scoring_features.py',
        'stage7_scoring.py': package_root / 'core' / 'stage7_scoring.py',
        'stage8_calibration.py': package_root / 'core' / 'stage8_calibration.py',
    }
    rows: list[dict[str, Any]] = []
    for name, path in locations.items():
        observed = _file_sha256(path) if path.is_file() else ''
        expected_hash = str(expected.get(name, ''))
        rows.append({
            'file': name,
            'expected_sha256': expected_hash,
            'current_sha256': observed,
            'match_flag': int(bool(expected_hash) and observed == expected_hash),
        })
    return {
        'methodology_files_exact': bool(rows)
        and all(bool(row['match_flag']) for row in rows),
        'files': rows,
    }


def reconstruct_current_asof_stage8_scores(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    members: Sequence[Mapping[str, Any]],
    market_policy: MarketDataPolicy,
) -> dict[str, Any]:
    ticker_to_cohort: dict[str, str] = {}
    for member in members:
        ticker = str(member['ticker'])
        cohort = str(
            member.get('calibration_cohort_id', member.get('cohort_id', ''))
        )
        if not ticker or not cohort:
            raise ValueError('Parity members require ticker and cohort.')
        if ticker in ticker_to_cohort and ticker_to_cohort[ticker] != cohort:
            raise ValueError(f'Conflicting parity cohort for {ticker}.')
        ticker_to_cohort[ticker] = cohort
    tickers = set(ticker_to_cohort)
    if not tickers:
        raise ValueError('Parity reconstruction requires at least one ticker.')

    selection, history, selection_sha = _price_selection_and_history(
        conn,
        tickers=tickers,
        maximum_date=as_of,
    )
    market = _market_features_for_date(
        as_of=as_of,
        tickers=tickers,
        selection=selection,
        history=history,
        policy=market_policy,
    )
    financial = _financial_features_for_date(
        conn, bundle, as_of=as_of, tickers=tickers
    )
    positioning = _positioning_features_for_date(
        conn, bundle, as_of=as_of, tickers=tickers
    )
    accepted_statuses = {
        'market': set(cfg_get(
            bundle.payload,
            'scoring_features.accepted_market_quality_statuses',
        )),
        'financial': set(cfg_get(
            bundle.payload,
            'scoring_features.accepted_financial_quality_statuses',
        )),
        'positioning': set(cfg_get(
            bundle.payload,
            'scoring_features.accepted_positioning_quality_statuses',
        )),
    }
    component_rows: list[dict[str, Any]] = []
    for ticker in sorted(tickers):
        cohort = ticker_to_cohort[ticker]
        upstreams = {
            'market': market.get(ticker),
            'financial': financial.get(ticker),
            'positioning': positioning.get(ticker),
        }
        for spec in CORE_COMPONENT_SPECS:
            upstream = upstreams[spec.group]
            quality_field = (
                'financial_quality_status'
                if spec.group == 'financial'
                else 'quality_status'
            )
            quality = (
                str(upstream[quality_field])
                if upstream is not None else 'missing'
            )
            raw = (
                _finite(upstream.get(spec.source_field))
                if upstream is not None else None
            )
            available = (
                upstream is not None
                and quality in accepted_statuses[spec.group]
                and raw is not None
            )
            component_rows.append({
                'ticker': ticker,
                'cohort_id': cohort,
                'component_name': spec.name,
                'raw_value': raw,
                'component_score': None,
                'direction': spec.direction,
                'rank_requirement': spec.rank_requirement,
                'availability_status': (
                    'available' if available
                    else 'missing_or_quality_rejected'
                ),
                'source_hash': (
                    str(upstream['source_hash']) if upstream is not None else ''
                ),
            })
    _normalize_component_rows(
        component_rows,
        minimum_peers=int(cfg_get(
            bundle.payload,
            'scoring_features.minimum_normalization_peer_count',
        )),
    )
    components_by_ticker: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for component in component_rows:
        components_by_ticker[str(component['ticker'])][
            str(component['component_name'])
        ] = component

    weights = stage7_component_weights(bundle)
    neutral = float(cfg_get(bundle.payload, 'stage7_scoring.neutral_score'))
    minimum_quality = float(cfg_get(
        bundle.payload,
        'stage7_scoring.minimum_data_quality_confidence',
    ))
    maximum_missing = float(cfg_get(
        bundle.payload,
        'stage7_scoring.maximum_missing_component_weight',
    ))
    reconstructed: list[dict[str, Any]] = []
    source_manifest_rows: list[dict[str, Any]] = []
    for ticker in sorted(tickers):
        components = components_by_ticker[ticker]
        ready, _readiness_reasons = _rank_requirements(components)
        scores: dict[str, float] = {}
        quality: dict[str, float] = {}
        source_hashes: dict[str, str] = {}
        core_score = 0.0
        available_weight = 0.0
        missing_weight = 0.0
        for spec in CORE_COMPONENT_SPECS:
            component = components[spec.name]
            score = _finite(component['component_score'])
            available = (
                component['availability_status'] == 'available'
                and score is not None
            )
            effective = min(100.0, max(0.0, score)) if available else neutral
            weight = weights[spec.name]
            scores[spec.name] = effective
            quality[spec.name] = 1.0 if available else 0.0
            source_hashes[spec.name] = str(component['source_hash'])
            core_score += weight * effective
            if available:
                available_weight += weight
            else:
                missing_weight += weight
            source_manifest_rows.append({
                'ticker': ticker,
                'component_name': spec.name,
                'source_hash': str(component['source_hash']),
                'availability_status': str(component['availability_status']),
            })
        rank_ready = int(
            ready
            and available_weight >= minimum_quality
            and missing_weight <= maximum_missing
        )
        reconstructed.append({
            'ticker': ticker,
            'asof_date': as_of,
            'calibration_cohort_id': ticker_to_cohort[ticker],
            'component_weights_json': _canonical_json(weights),
            'component_scores_json': _canonical_json(scores),
            'component_quality_json': _canonical_json(quality),
            'component_source_hashes_json': _canonical_json(source_hashes),
            'core_score': core_score,
            'data_quality_confidence': available_weight,
            'missing_weight': missing_weight,
            'rank_ready_flag': rank_ready,
            'calibration_eligible_flag': rank_ready,
        })

    price_bar_manifest_rows = [
        {
            'ticker': ticker,
            'source_id': selection[ticker],
            'bars': history[ticker],
        }
        for ticker in sorted(history)
    ]
    score_panel_manifest = _sha256([
        {
            field: row[field]
            for field in _CURRENT_PARITY_FIELDS
        }
        | {
            'ticker': row['ticker'],
            'asof_date': row['asof_date'],
            'calibration_cohort_id': row['calibration_cohort_id'],
        }
        for row in reconstructed
    ])
    return {
        'rows': reconstructed,
        'frozen_price_selection_sha256': selection_sha,
        'price_bar_manifest_sha256': _sha256(price_bar_manifest_rows),
        'component_source_manifest_sha256': _sha256(source_manifest_rows),
        'score_panel_manifest_sha256': score_panel_manifest,
        'ticker_count': len(reconstructed),
    }


def compare_reconstructed_scores(
    sealed_rows: Sequence[Mapping[str, Any]],
    reconstructed_rows: Sequence[Mapping[str, Any]],
    *,
    absolute_tolerance: float = 1e-10,
) -> dict[str, Any]:
    sealed = {str(row['ticker']): row for row in sealed_rows}
    reconstructed = {str(row['ticker']): row for row in reconstructed_rows}
    ticker_mismatch = sorted(set(sealed) ^ set(reconstructed))
    component_score_errors: list[str] = []
    component_quality_errors: list[str] = []
    component_weight_errors: list[str] = []
    core_score_errors: list[str] = []
    data_quality_errors: list[str] = []
    rank_ready_errors: list[str] = []
    calibration_eligible_errors: list[str] = []
    for ticker in sorted(set(sealed) & set(reconstructed)):
        expected = sealed[ticker]
        observed = reconstructed[ticker]
        try:
            expected_scores = _json_object(
                expected['component_scores_json'], field='component_scores_json'
            )
            observed_scores = _json_object(
                observed['component_scores_json'], field='component_scores_json'
            )
            expected_quality = _json_object(
                expected['component_quality_json'], field='component_quality_json'
            )
            observed_quality = _json_object(
                observed['component_quality_json'], field='component_quality_json'
            )
            expected_weights = _json_object(
                expected['component_weights_json'], field='component_weights_json'
            )
            observed_weights = _json_object(
                observed['component_weights_json'], field='component_weights_json'
            )
        except (KeyError, ValueError):
            component_score_errors.append(ticker)
            component_quality_errors.append(ticker)
            component_weight_errors.append(ticker)
            continue

        def numeric_dict_equal(
            left: Mapping[str, Any], right: Mapping[str, Any]
        ) -> bool:
            if set(left) != set(right):
                return False
            return all(
                _finite(left[name]) is not None
                and _finite(right[name]) is not None
                and math.isclose(
                    float(left[name]),
                    float(right[name]),
                    rel_tol=0.0,
                    abs_tol=absolute_tolerance,
                )
                for name in left
            )

        if not numeric_dict_equal(expected_scores, observed_scores):
            component_score_errors.append(ticker)
        if not numeric_dict_equal(expected_quality, observed_quality):
            component_quality_errors.append(ticker)
        if not numeric_dict_equal(expected_weights, observed_weights):
            component_weight_errors.append(ticker)
        expected_core = _finite(expected.get('core_score'))
        observed_core = _finite(observed.get('core_score'))
        if (
            expected_core is None
            or observed_core is None
            or not math.isclose(
                expected_core,
                observed_core,
                rel_tol=0.0,
                abs_tol=absolute_tolerance,
            )
        ):
            core_score_errors.append(ticker)
        expected_quality_weight = _finite(expected.get('data_quality_confidence'))
        observed_quality_weight = _finite(observed.get('data_quality_confidence'))
        if (
            expected_quality_weight is None
            or observed_quality_weight is None
            or not math.isclose(
                expected_quality_weight,
                observed_quality_weight,
                rel_tol=0.0,
                abs_tol=absolute_tolerance,
            )
        ):
            data_quality_errors.append(ticker)
        if int(expected.get('rank_ready_flag', -1)) != int(
            observed.get('rank_ready_flag', -2)
        ):
            rank_ready_errors.append(ticker)
        if int(expected.get('calibration_eligible_flag', -1)) != int(
            observed.get('calibration_eligible_flag', -2)
        ):
            calibration_eligible_errors.append(ticker)

    error_groups = {
        'ticker_mismatch': ticker_mismatch,
        'component_score_errors': component_score_errors,
        'component_quality_errors': component_quality_errors,
        'component_weight_errors': component_weight_errors,
        'core_score_errors': core_score_errors,
        'data_quality_errors': data_quality_errors,
        'rank_ready_errors': rank_ready_errors,
        'calibration_eligible_errors': calibration_eligible_errors,
    }
    return {
        'current_asof_score_arithmetic_parity': not any(error_groups.values()),
        'sealed_ticker_count': len(sealed),
        'reconstructed_ticker_count': len(reconstructed),
        **{name: sorted(set(values)) for name, values in error_groups.items()},
    }


def assess_provenance_binding(
    *,
    stage8_contract: Mapping[str, Any],
    stage8_decision: Mapping[str, Any],
    stage7_seal: Mapping[str, Any],
    methodology_identity: Mapping[str, Any],
    reconstruction: Mapping[str, Any],
    score_parity: Mapping[str, Any],
) -> dict[str, Any]:
    stage7_baseline = stage8_decision.get('stage7_baseline', {})
    panel_summary = stage8_decision.get('panel_summary', {})
    if not isinstance(stage7_baseline, dict):
        stage7_baseline = {}
    if not isinstance(panel_summary, dict):
        panel_summary = {}
    contract_binding = (
        str(stage8_contract.get('stage7_contract_sha256', ''))
        == str(stage7_seal.get('contract_sha256', ''))
        and str(stage8_contract.get('stage7_source_id', ''))
        == str(stage7_seal.get('source_id', ''))
        and str(stage7_baseline.get('output_manifest_sha256', ''))
        == str(stage7_seal.get('output_manifest_sha256', ''))
    )
    price_selection_binding = (
        str(panel_summary.get('frozen_price_selection_sha256', ''))
        == str(reconstruction.get('frozen_price_selection_sha256', ''))
    )
    expected_provenance = stage8_contract.get('provenance_manifests', {})
    if not isinstance(expected_provenance, dict):
        expected_provenance = {}
    observed_provenance = {
        'stage7_stage8_current_asof_parity_manifest_sha256': _sha256({
            'stage7_output_manifest_sha256': stage7_seal.get(
                'output_manifest_sha256'
            ),
            'reconstructed_score_panel_manifest_sha256': reconstruction.get(
                'score_panel_manifest_sha256'
            ),
            'score_parity_pass': bool(
                score_parity.get('current_asof_score_arithmetic_parity')
            ),
        }),
        'historical_price_bar_manifest_sha256': str(
            reconstruction.get('price_bar_manifest_sha256', '')
        ),
        'historical_component_source_manifest_sha256': str(
            reconstruction.get('component_source_manifest_sha256', '')
        ),
        'historical_score_panel_manifest_sha256': str(
            reconstruction.get('score_panel_manifest_sha256', '')
        ),
    }
    manifest_checks = {
        name: bool(expected_provenance.get(name))
        and str(expected_provenance.get(name)) == observed_provenance[name]
        for name in _LEGACY_REQUIRED_PROVENANCE_FIELDS
    }
    historical_provenance_bound = all(manifest_checks.values())
    current_parity_prerequisites = (
        bool(stage7_seal.get('stage7_output_identity_sealed'))
        and bool(methodology_identity.get('methodology_files_exact'))
        and bool(score_parity.get('current_asof_score_arithmetic_parity'))
        and contract_binding
        and price_selection_binding
    )
    return {
        'stage7_contract_and_output_manifest_bound': contract_binding,
        'frozen_price_selection_bound': price_selection_binding,
        'current_asof_parity_prerequisites_pass': current_parity_prerequisites,
        'required_provenance_manifest_checks': manifest_checks,
        'observed_provenance_manifests': observed_provenance,
        'historical_provenance_bound': historical_provenance_bound,
        'source_identity_tied': (
            current_parity_prerequisites and historical_provenance_bound
        ),
        'fresh_future_source_identity_tie_capable': current_parity_prerequisites,
        'legacy_limitation': (
            'Current-as-of code parity can be diagnosed retrospectively, but '
            'legacy Stage 8 did not preregister and seal the exact historical '
            'price-bar, component-source, score-panel, and parity manifests.'
        ),
    }
