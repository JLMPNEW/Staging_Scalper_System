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
)
from consumer_defensive.core.stage8_era_quality_v2 import (  # noqa: E402
    prepare_era_adjusted_panel,
)
from consumer_defensive.core.stage8_independent_evidence_v4 import (  # noqa: E402
    absolute_baseline_independent_evidence_v4,
    independent_evidence_gate_v4,
)
from consumer_defensive.core.stage9_backtest import (  # noqa: E402
    _candidate,
    _load_panel,
)


SCHEMA_VERSION = 'consumer_defensive_validation_audit_v4'


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Re-audit legacy Consumer Defensive evidence with full-schedule '
            'long/short costs and independent IC/spread gates.'
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


def _script21() -> ModuleType:
    path = Path(__file__).with_name(
        '21_audit_consumer_defensive_validation_v3.py'
    )
    spec = importlib.util.spec_from_file_location(
        'consumer_defensive_validation_audit_script21', path
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


def run(args: argparse.Namespace) -> dict[str, Any]:
    stage8_root = args.stage8_root.expanduser().resolve()
    database = args.database.expanduser().resolve()
    bundle = load_config(args.config.expanduser().resolve())
    base = _script21().run(args)
    registry = _json(stage8_root / 'stage8_candidate_registry.json')
    split = _json(stage8_root / 'stage8_split_manifest.json')
    contract = _json(stage8_root / 'stage8_contract.json')
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
    era_panel = prepare_era_adjusted_panel(
        [
            row for row in panel
            if str(row['asof_date']) in complete_set
        ],
        baseline,
        bundle,
    )
    era_dates, _census = calibration_date_census_same_sample(
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
            """SELECT selected_source_id FROM dim_price_series_selection
               WHERE ticker='SPY' AND purpose='scoring_return_series'"""
        ).fetchone()
        calendar = [
            str(row['bar_date'])
            for row in conn.execute(
                """SELECT bar_date FROM fact_price_ohlcv
                   WHERE ticker='SPY' AND source_id=? ORDER BY bar_date""",
                (str(source[0]),),
            )
        ]
        stage6c = conn.execute(
            'SELECT entry_lag_trading_days FROM stage6c_panel_run '
            'WHERE stage6c_run_id=?',
            (int(contract['stage6c_run_id']),),
        ).fetchone()
    finally:
        conn.close()
    evidence = absolute_baseline_independent_evidence_v4(
        era_panel,
        holdout_dates,
        baseline,
        bundle,
        calendar=calendar,
        entry_lag=int(stage6c['entry_lag_trading_days']),
        schedule_dates=era_dates,
    )
    gate = independent_evidence_gate_v4(
        evidence,
        minimum_independent_dates={21: 12, 63: 6, 126: 4},
        maximum_sign_pvalue=0.10,
        invariants=base['promotion_invariants'],
    )
    blockers = set(base['limited_production_gate_v3']['blockers'])
    blockers.update(gate['blockers'])
    blockers.update({
        'legacy_holdout_is_diagnostic_only',
        'future_monthly_plan_requires_exact_registry_split_and_self_hashes',
    })
    future_plan = dict(base['future_monthly_target_plan_template'])
    future_plan['required_before_registration'] = sorted(set(
        future_plan['required_before_registration']
    ) | {
        'bind_exact_64hex_candidate_registry_sha256',
        'bind_exact_64hex_split_manifest_sha256',
        'bind_train_embargo_validation_embargo_holdout_date_partitions',
        'bind_plan_self_sha256_over_all_preregistered_fields',
        'evaluate_holdings_on_full_schedule_then_slice_decision_roles',
    })
    return {
        **base,
        'schema_version': SCHEMA_VERSION,
        'calendar_independent_baseline_evidence_v4': evidence,
        'calendar_independent_gate_v4': gate,
        'future_monthly_target_plan_template': future_plan,
        'turnover_correction': {
            'top_and_bottom_sleeves_costed_separately': True,
            'initial_entry_costed': True,
            'final_liquidation_costed': True,
            'continuous_full_schedule_state': True,
            'decision_role_target_access_only': True,
        },
        'limited_production_gate_v4': {
            'limited_production_ready_flag': 0,
            'action': 'remain_shadow_fail_closed',
            'blockers': sorted(blockers),
            'portfolio_write_enabled': False,
            'production_promotion_enabled': False,
        },
    }


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
