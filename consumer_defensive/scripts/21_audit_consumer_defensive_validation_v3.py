from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from consumer_defensive.core.atomic_io import atomic_text_writer  # noqa: E402
from consumer_defensive.core.config import load_config  # noqa: E402
from consumer_defensive.core.stage8_calibration_v2 import (  # noqa: E402
    calibration_date_census_same_sample,
    prepare_validation_panel_v2,
)
from consumer_defensive.core.stage8_era_quality_v2 import (  # noqa: E402
    prepare_era_adjusted_panel,
)
from consumer_defensive.core.stage8_independent_evidence_v2 import (  # noqa: E402
    absolute_baseline_independent_evidence,
    independent_evidence_gate,
)
from consumer_defensive.core.stage8_monthly_target_v2 import (  # noqa: E402
    MONTHLY_TARGET_FIELD,
)
from consumer_defensive.core.stage9_backtest import (  # noqa: E402
    _candidate,
    _load_panel,
)


SCHEMA_VERSION = 'consumer_defensive_validation_audit_v3'


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Run the Consumer Defensive V3 validation audit with source-era '
            'quality, independent endpoint evidence, and a future monthly '
            'target preregistration template.'
        )
    )
    parser.add_argument('--stage8-root', type=Path, required=True)
    parser.add_argument('--stage9-root', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument(
        '--config', type=Path, default=PACKAGE_ROOT / 'config.yaml'
    )
    parser.add_argument('--output', type=Path)
    parser.add_argument('--future-plan-output', type=Path)
    return parser.parse_args()


def _script20() -> ModuleType:
    path = Path(__file__).with_name(
        '20_audit_consumer_defensive_validation_v2.py'
    )
    spec = importlib.util.spec_from_file_location(
        'consumer_defensive_validation_audit_script20', path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load prerequisite audit: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return payload


def future_monthly_target_plan_template(
    *,
    stage8_root: Path,
) -> dict[str, Any]:
    """Return a non-binding template; it is deliberately not preregistered."""

    return {
        'schema_version': 'consumer_defensive_monthly_target_plan_v1_draft',
        'plan_id': 'consumer_defensive_future_monthly_target_v1_draft',
        'status': 'draft_requires_new_registry_and_source_seal',
        'permitted_use': 'future_run_planning_only',
        'prohibited_use': 'legacy_holdout_reuse_or_production_promotion',
        'legacy_stage8_root_reference': str(stage8_root),
        'candidate_registry_sha256': '',
        'target_field': MONTHLY_TARGET_FIELD,
        'scoring_frequency': 'monthly',
        'rebalance_frequency': 'monthly',
        'entry_policy': 'next_frozen_calendar_session_after_true_month_end',
        'exit_policy': 'next_monthly_rebalance_entry',
        'benchmark': 'XLP',
        'primary_objective': 'mean_rank_ic',
        'secondary_diagnostics': [
            'net_top_bottom_spread', 'turnover', 'capacity',
            'cohort_concentration', 'candidate_data_quality',
        ],
        'holdout_provenance': 'fresh_forward_oos',
        'registered_before_target_access': False,
        'holdout_sealed': True,
        'legacy_holdout_reuse_allowed': False,
        'required_before_registration': [
            'bind_new_candidate_registry_sha256',
            'bind_full_historical_constituent_census_sha256',
            'bind_freshness_corrected_panel_builder_sha256',
            'bind_frozen_price_selection_and_bar_manifest_sha256s',
            'set_registration_timestamp_before_any_new_target_access',
        ],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    stage8_root = args.stage8_root.expanduser().resolve()
    database = args.database.expanduser().resolve()
    bundle = load_config(args.config.expanduser().resolve())
    base = _script20().run(args)
    registry = _json(stage8_root / 'stage8_candidate_registry.json')
    split = _json(stage8_root / 'stage8_split_manifest.json')
    panel = _load_panel(stage8_root / 'stage8_historical_core_panel.csv')
    candidates = [_candidate(row) for row in registry['candidates']]
    baseline = next(
        candidate for candidate in candidates
        if candidate.scope_id == 'consumer_defensive'
        and candidate.candidate_kind == 'stage7_core_baseline'
    )
    partial = set(base['partial_or_immature_panel_dates'])
    complete_dates = sorted({
        str(row['asof_date']) for row in panel
        if str(row['asof_date']) not in partial
    })
    complete_set = set(complete_dates)
    complete_panel = [
        row for row in panel if str(row['asof_date']) in complete_set
    ]
    hard_waiver_panel = prepare_validation_panel_v2(
        complete_panel,
        baseline,
        bundle,
        complete_month_dates=complete_dates,
    )
    old_dates, old_census = calibration_date_census_same_sample(
        hard_waiver_panel, baseline, bundle
    )
    era_panel = prepare_era_adjusted_panel(
        complete_panel, baseline, bundle
    )
    era_dates, era_census = calibration_date_census_same_sample(
        era_panel, baseline, bundle
    )
    holdout_dates = [
        value for value in split['holdout_dates'] if value in set(era_dates)
    ]

    conn = sqlite3.connect(
        f'file:{database.as_posix()}?mode=ro', uri=True
    )
    conn.row_factory = sqlite3.Row
    try:
        source = conn.execute(
            '''SELECT selected_source_id FROM dim_price_series_selection
               WHERE ticker='SPY' AND purpose='scoring_return_series' '''
        ).fetchone()
        calendar = [
            str(row['bar_date'])
            for row in conn.execute(
                '''SELECT bar_date FROM fact_price_ohlcv
                   WHERE ticker='SPY' AND source_id=? ORDER BY bar_date''',
                (str(source[0]),),
            )
        ]
        stage6c = conn.execute(
            'SELECT entry_lag_trading_days FROM stage6c_panel_run '
            'WHERE stage6c_run_id=?',
            (int(_json(stage8_root / 'stage8_contract.json')[
                'stage6c_run_id'
            ]),),
        ).fetchone()
    finally:
        conn.close()
    independent = absolute_baseline_independent_evidence(
        era_panel,
        holdout_dates,
        baseline,
        bundle,
        calendar=calendar,
        entry_lag=int(stage6c['entry_lag_trading_days']),
    )
    independent_gate = independent_evidence_gate(
        independent,
        minimum_independent_dates={21: 12, 63: 6, 126: 4},
        maximum_sign_pvalue=0.10,
        invariants=base['promotion_invariants'],
    )
    future_plan = future_monthly_target_plan_template(
        stage8_root=stage8_root
    )
    blockers = set(base['limited_production_gate']['blockers'])
    blockers.update(independent_gate['blockers'])
    blockers.update({
        'monthly_target_plan_not_preregistered',
        'legacy_holdout_is_diagnostic_only',
    })
    report = {
        **base,
        'schema_version': SCHEMA_VERSION,
        'legacy_evidence_status': {
            'classification': 'retrospective_diagnostic_only',
            'holdout_burned_flag': 1,
            'may_support_production_promotion_flag': 0,
        },
        'era_quality_evidence': {
            'score_policy': (
                'fixed_weights_structural_components_neutral_no_redistribution'
            ),
            'quality_gate_policy': (
                'minimum_quality_and_missingness_over_observable_weight_only'
            ),
            'complete_month_date_count': len(complete_dates),
            'hard_short_waiver_calibration_date_count': len(old_dates),
            'observable_denominator_calibration_date_count': len(era_dates),
            'additional_dates_recovered_by_denominator_change': sorted(
                set(era_dates) - set(old_dates)
            ),
            'eligible_row_gains_by_date': [
                {
                    'asof_date': new['asof_date'],
                    'hard_waiver_eligible_count': int(old['eligible_count']),
                    'observable_denominator_eligible_count': int(
                        new['eligible_count']
                    ),
                }
                for old, new in zip(old_census, era_census, strict=True)
                if int(new['eligible_count']) > int(old['eligible_count'])
            ],
        },
        'calendar_independent_baseline_evidence': independent,
        'calendar_independent_gate': independent_gate,
        'future_monthly_target_plan_template': future_plan,
        'limited_production_gate_v3': {
            'limited_production_ready_flag': 0,
            'action': 'remain_shadow_fail_closed',
            'blockers': sorted(blockers),
            'portfolio_write_enabled': False,
            'production_promotion_enabled': False,
        },
    }
    return report


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + '\n'
    with atomic_text_writer(path, encoding='utf-8', newline='') as handle:
        handle.write(content)


def main() -> int:
    args = _arguments()
    report = run(args)
    if args.output is not None:
        _write(args.output.expanduser().resolve(), report)
    if args.future_plan_output is not None:
        _write(
            args.future_plan_output.expanduser().resolve(),
            report['future_monthly_target_plan_template'],
        )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
