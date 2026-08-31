from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.stage7_scoring import stage7_component_weights
from consumer_defensive.core.stage8_calibration import (
    SECTOR_SCOPE,
    _immutable_text,
    _make_candidate,
    _run_research_family,
    _score_candidate,
    build_candidate_registry,
    calibration_date_census,
    chronological_split,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


def _bundle(**stage8_overrides: object) -> ConfigBundle:
    original = load_config(CONFIG)
    payload = copy.deepcopy(original.payload)
    payload['stage8_calibration'].update(stage8_overrides)
    return ConfigBundle(original.path, original.base_dir, payload)


def _dates(count: int = 86) -> list[str]:
    start = date(2019, 1, 2)
    return [
        (start + timedelta(days=30 * index)).isoformat()
        for index in range(count)
    ]


def test_stage8_split_has_two_full_embargoes_and_sealed_holdout() -> None:
    split = chronological_split(
        _dates(),
        minimum_train_dates=48,
        validation_dates=12,
        holdout_dates=12,
        embargo_panel_dates=7,
    )
    assert len(split.train_dates) == 48
    assert len(split.first_embargo_dates) == 7
    assert len(split.validation_dates) == 12
    assert len(split.second_embargo_dates) == 7
    assert len(split.holdout_dates) == 12
    assert max(split.train_dates) < min(split.first_embargo_dates)
    assert max(split.validation_dates) < min(split.second_embargo_dates)
    with pytest.raises(ValueError, match='shorter'):
        chronological_split(
            _dates(),
            minimum_train_dates=48,
            validation_dates=12,
            holdout_dates=12,
            embargo_panel_dates=6,
        )


def test_candidate_registry_is_deterministic_capped_and_cohort_shrunk() -> None:
    bundle = _bundle(candidate_count_per_scope=8)
    dates = _dates()
    membership = [
        {
            'asof_date': as_of,
            'ticker': f'{cohort[:2]}{index:02d}',
            'cohort_id': cohort,
            'applicability_subtype': 'all_operating_issuers',
        }
        for as_of in dates
        for cohort in ('beverages', 'distribution')
        for index in range(20)
    ]
    first = build_candidate_registry(
        bundle,
        membership_rows=membership,
        accepted_factor_cells=[],
    )
    second = build_candidate_registry(
        bundle,
        membership_rows=membership,
        accepted_factor_cells=[],
    )
    assert first == second
    assert len(first) == 8 * 3
    baseline = stage7_component_weights(bundle)
    for candidate in first:
        assert sum(candidate.core_weights.values()) == pytest.approx(1.0)
        assert not candidate.specialized_weights
        assert max(candidate.core_weights.values()) <= 0.15 + 1e-12
        l1 = sum(
            abs(candidate.core_weights[name] - baseline[name])
            for name in baseline
        )
        assert l1 <= 0.30 + 1e-10
        if candidate.candidate_kind == 'cohort_core_reweight_shrunk':
            assert 0.0 < candidate.shrinkage_alpha <= 0.65


def test_specialized_candidate_requires_accepted_registered_cell() -> None:
    bundle = _bundle(candidate_count_per_scope=4)
    membership = [
        {
            'asof_date': as_of,
            'ticker': f'B{index:02d}',
            'cohort_id': 'beverages',
            'applicability_subtype': 'alcohol',
        }
        for as_of in _dates()
        for index in range(20)
    ]
    without = build_candidate_registry(
        bundle,
        membership_rows=membership,
        accepted_factor_cells=[],
    )
    assert not any(row.specialized_weights for row in without)
    with_evidence = build_candidate_registry(
        bundle,
        membership_rows=membership,
        accepted_factor_cells=[{
            'cell_id': 'accepted-cell',
            'factor_id': 'alcohol_depletion_growth_pct',
            'scope_id': 'beverages',
        }],
    )
    specialized = [
        row for row in with_evidence if row.specialized_weights
    ]
    assert len(specialized) == 1
    assert specialized[0].evidence_references == ('accepted-cell',)
    assert sum(specialized[0].specialized_weights.values()) == pytest.approx(
        0.05
    )


def test_missing_component_is_neutral_and_weight_is_not_redistributed() -> None:
    bundle = _bundle()
    baseline = stage7_component_weights(bundle)
    candidate = _make_candidate(
        scope_id=SECTOR_SCOPE,
        candidate_kind='stage7_core_baseline',
        core_weights=baseline,
        specialized_weights={},
        parent_candidate_id=None,
        shrinkage_alpha=0.0,
        evidence_references=('baseline',),
    )
    scores = {spec.name: 80.0 for spec in CORE_COMPONENT_SPECS}
    quality = {spec.name: 1.0 for spec in CORE_COMPONENT_SPECS}
    quality['gross_margin'] = 0.0
    row = {
        'calibration_eligible_flag': 1,
        '_component_scores': scores,
        '_component_quality': quality,
        '_specialized_scores': {},
    }
    score, available, missing, eligible = _score_candidate(
        row, candidate, bundle
    )
    expected = (
        80.0 * (1.0 - baseline['gross_margin'])
        + 50.0 * baseline['gross_margin']
    )
    assert score == pytest.approx(expected)
    assert available == pytest.approx(1.0 - baseline['gross_margin'])
    assert missing == pytest.approx(baseline['gross_margin'])
    assert eligible


def test_date_census_uses_frozen_baseline_eligibility_not_outcomes() -> None:
    bundle = _bundle(minimum_sector_cross_section=6)
    baseline_weights = stage7_component_weights(bundle)
    baseline = _make_candidate(
        scope_id=SECTOR_SCOPE,
        candidate_kind='stage7_core_baseline',
        core_weights=baseline_weights,
        specialized_weights={},
        parent_candidate_id=None,
        shrinkage_alpha=0.0,
        evidence_references=('baseline',),
    )
    rows = []
    for as_of, eligible_count in (('2022-01-31', 5), ('2022-02-28', 6)):
        for index in range(6):
            rows.append({
                'asof_date': as_of,
                'calibration_eligible_flag': int(index < eligible_count),
                '_component_scores': {
                    spec.name: 50.0 for spec in CORE_COMPONENT_SPECS
                },
                '_component_quality': {
                    spec.name: 1.0 for spec in CORE_COMPONENT_SPECS
                },
                '_specialized_scores': {},
                'forward_xlp_residual_return_126d': 999.0 - index,
            })
    selected, census = calibration_date_census(rows, baseline, bundle)
    assert selected == ['2022-02-28']
    assert [row['eligible_count'] for row in census] == [5, 6]
    assert [row['included_flag'] for row in census] == [0, 1]


def test_immutable_text_is_lf_stable_and_replay_safe(tmp_path: Path) -> None:
    path = tmp_path / 'artifact.json'
    content = '{\n  "status": "PASS"\n}\n'
    _immutable_text(path, content)
    assert path.read_bytes() == content.encode('utf-8')
    _immutable_text(path, content)
    with pytest.raises(FileExistsError, match='content changed'):
        _immutable_text(path, content.replace('PASS', 'FAIL'))


def test_failed_validation_never_opens_or_reads_final_holdout() -> None:
    bundle = _bundle(
        candidate_count_per_scope=2,
        maximum_top_cohort_share=1.0,
    )
    dates = _dates()
    split = chronological_split(
        dates,
        minimum_train_dates=48,
        validation_dates=12,
        holdout_dates=12,
        embargo_panel_dates=7,
    )
    baseline_weights = stage7_component_weights(bundle)
    baseline = _make_candidate(
        scope_id=SECTOR_SCOPE,
        candidate_kind='stage7_core_baseline',
        core_weights=baseline_weights,
        specialized_weights={},
        parent_candidate_id=None,
        shrinkage_alpha=0.0,
        evidence_references=('baseline',),
    )
    candidate_weights = {
        spec.name: (1.0 if spec.name == 'gross_margin' else 0.0)
        for spec in CORE_COMPONENT_SPECS
    }
    candidate = _make_candidate(
        scope_id=SECTOR_SCOPE,
        candidate_kind='sector_core_reweight',
        core_weights=candidate_weights,
        specialized_weights={},
        parent_candidate_id=baseline.candidate_id,
        shrinkage_alpha=1.0,
        evidence_references=('baseline',),
    )
    rows = []
    validation_set = set(split.validation_dates)
    for as_of in dates:
        for index in range(40):
            signal = float(index)
            target = -signal if as_of in validation_set else signal
            component_scores = {
                spec.name: (
                    signal if spec.name == 'gross_margin' else 50.0
                )
                for spec in CORE_COMPONENT_SPECS
            }
            rows.append({
                'asof_date': as_of,
                'ticker': f'T{index:02d}',
                'cohort_id': (
                    'beverages' if index % 2 == 0 else 'distribution'
                ),
                'calibration_eligible_flag': 1,
                '_component_scores': component_scores,
                '_component_quality': {
                    spec.name: 1.0 for spec in CORE_COMPONENT_SPECS
                },
                '_specialized_scores': {},
                '_specialized_applicability': {},
                'forward_xlp_residual_return_21d': target,
                'forward_xlp_residual_return_63d': target,
                'forward_xlp_residual_return_126d': target,
            })
    result_rows, _walk_rows, decision = _run_research_family(
        rows,
        dates,
        split=split,
        candidates=(baseline, candidate),
        baseline=baseline,
        bundle=bundle,
        family_id='consumer_defensive__core',
    )
    assert decision['verdict'] == 'rejected'
    assert decision['validation_gate_pass'] == 0
    assert decision['holdout_opened'] == 0
    assert not any(row['phase'] == 'holdout' for row in result_rows)
