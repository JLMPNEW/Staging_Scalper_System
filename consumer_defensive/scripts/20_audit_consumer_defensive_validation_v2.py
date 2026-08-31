from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from consumer_defensive.core.atomic_io import atomic_text_writer  # noqa: E402
from consumer_defensive.core.config import cfg_get, load_config  # noqa: E402
from consumer_defensive.core.stage8_calibration_v2 import (  # noqa: E402
    absolute_baseline_evidence,
    calibration_date_census_same_sample,
    complete_month_evaluation_dates,
    fail_closed_limited_production_gate,
    prepare_validation_panel_v2,
)
from consumer_defensive.core.stage9_backtest import (  # noqa: E402
    _candidate,
    _load_panel,
)
from consumer_defensive.core.stage9_backtest_v2 import (  # noqa: E402
    audit_existing_holdout_access,
    decision_from_bound_stage8,
    phase_summary_rows,
    price_selection_sha256,
    validate_primary_target_contract,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Audit legacy Consumer Defensive Stage 8/9 evidence through the '
            'fail-closed V2 validation contract.'
        )
    )
    parser.add_argument('--stage8-root', type=Path, required=True)
    parser.add_argument('--stage9-root', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument(
        '--config', type=Path, default=PACKAGE_ROOT / 'config.yaml'
    )
    parser.add_argument('--output', type=Path)
    return parser.parse_args()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return payload


def _freshness_audit(
    conn: sqlite3.Connection,
    panel: list[Mapping[str, Any]],
    bundle: Any,
) -> dict[str, Any]:
    source_id = str(cfg_get(
        bundle.payload, 'positioning.market_positioning_source_id'
    ))
    specifications = {
        'institutional_13f': (
            'fact_13f_positioning', 'publication_date',
            ('institutional_flow',),
        ),
        'short_interest': (
            'fact_short_interest', 'publication_date',
            ('short_float_pct', 'short_days_to_cover'),
        ),
        'borrow': (
            'fact_borrow_snapshot', 'asof_date', ('borrow_fee',),
        ),
    }
    output: dict[str, Any] = {}
    for source_key, (table, date_column, components) in specifications.items():
        maximum_age_raw = cfg_get(
            bundle.payload, f'positioning.maximum_age_days.{source_key}'
        )
        maximum_age = (
            None if maximum_age_raw is None else int(maximum_age_raw)
        )
        birth = str(cfg_get(
            bundle.payload, f'positioning.source_birthdates.{source_key}'
        ))
        observations: dict[str, list[str]] = defaultdict(list)
        for row in conn.execute(
            f'''SELECT ticker,{date_column} AS observation_date
                FROM {table} WHERE source_id=?
                ORDER BY ticker,{date_column}''',
            (source_id,),
        ):
            observations[str(row['ticker'])].append(
                str(row['observation_date'])[:10]
            )
        counts: Counter[str] = Counter()
        maximum_observed_age: int | None = None
        for row in panel:
            as_of = str(row['asof_date'])
            if as_of < birth:
                counts['structurally_unavailable_panel_rows'] += 1
                continue
            dates = observations.get(str(row['ticker']), [])
            index = bisect.bisect_right(dates, as_of) - 1
            if index < 0:
                counts['missing_prior_observation_rows'] += 1
                continue
            age = (
                date.fromisoformat(as_of)
                - date.fromisoformat(dates[index])
            ).days
            maximum_observed_age = (
                age if maximum_observed_age is None
                else max(maximum_observed_age, age)
            )
            stale = maximum_age is not None and age > maximum_age
            counts['latest_prior_rows'] += 1
            counts['stale_latest_prior_rows'] += int(stale)
            quality_used = any(
                float(row['_component_quality'].get(component, 0.0)) > 0.0
                for component in components
            )
            counts['quality_available_rows'] += int(quality_used)
            counts['stale_consumed_rows'] += int(stale and quality_used)
        output[source_key] = {
            **dict(counts),
            'maximum_age_days': maximum_age,
            'maximum_observed_age_days': maximum_observed_age,
        }
    return output


def _membership_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        '''SELECT COUNT(DISTINCT ticker) AS total,
                  COUNT(DISTINCT CASE WHEN is_current_member=1
                                      THEN ticker END) AS current_count,
                  COUNT(DISTINCT CASE WHEN is_current_member=0
                                      THEN ticker END) AS historical_exit_count
           FROM dim_universe_membership'''
    ).fetchone()
    terminal = conn.execute(
        'SELECT COUNT(DISTINCT ticker) FROM fact_terminal_event_reconciliation'
    ).fetchone()
    return {
        'membership_ticker_count': int(row['total']),
        'current_member_count': int(row['current_count']),
        'historical_exit_count': int(row['historical_exit_count']),
        'terminal_event_ticker_count': int(terminal[0]),
        'full_historical_constituent_census_flag': 0,
        'reason': (
            'candidate census is current taxonomy plus reviewed terminal seed; '
            'it does not enumerate the full historical constituent union'
        ),
    }


def _stage9_baseline_phase_evidence(
    stage9_root: Path,
    baseline_id: str,
) -> list[dict[str, Any]]:
    import csv
    import gzip

    path = stage9_root / 'stage9_period_results.csv.gz'
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if row['candidate_id'] == baseline_id
            and row['portfolio_name'] == 'long_short_top_bottom_quintile'
            and row['weight_method'] == 'equal_weight'
        ]
    return phase_summary_rows(rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    stage8_root = args.stage8_root.expanduser().resolve()
    stage9_root = args.stage9_root.expanduser().resolve()
    bundle = load_config(args.config.expanduser().resolve())
    decision = _json(stage8_root / 'stage8_decision.json')
    contract = _json(stage8_root / 'stage8_contract.json')
    registry = _json(stage8_root / 'stage8_candidate_registry.json')
    split = _json(stage8_root / 'stage8_split_manifest.json')
    panel = _load_panel(stage8_root / 'stage8_historical_core_panel.csv')
    candidates = [_candidate(row) for row in registry['candidates']]
    baseline = next(
        candidate for candidate in candidates
        if candidate.scope_id == 'consumer_defensive'
        and candidate.candidate_kind == 'stage7_core_baseline'
    )

    database = args.database.expanduser().resolve()
    conn = sqlite3.connect(
        f'file:{database.as_posix()}?mode=ro', uri=True
    )
    conn.row_factory = sqlite3.Row
    try:
        stage6c = conn.execute(
            'SELECT * FROM stage6c_panel_run WHERE stage6c_run_id=?',
            (int(contract['stage6c_run_id']),),
        ).fetchone()
        if stage6c is None:
            raise RuntimeError('Bound Stage 6C run is absent from database.')
        source = conn.execute(
            '''SELECT selected_source_id FROM dim_price_series_selection
               WHERE ticker='SPY' AND purpose='scoring_return_series' '''
        ).fetchone()
        spy = {
            str(row['bar_date']): float(row['adjusted_close'])
            for row in conn.execute(
                '''SELECT bar_date,adjusted_close FROM fact_price_ohlcv
                   WHERE ticker='SPY' AND source_id=? AND bar_date<=?
                     AND adjusted_close>0 ORDER BY bar_date''',
                (str(source[0]), str(stage6c['asof_date'])),
            )
        }
        horizons = json.loads(str(stage6c['horizons_json']))
        _calendar, complete_dates = complete_month_evaluation_dates(
            spy,
            history_start=str(stage6c['history_start']),
            as_of=str(stage6c['asof_date']),
            entry_lag=int(stage6c['entry_lag_trading_days']),
            maximum_horizon=max(int(value) for value in horizons),
        )
        panel_dates = sorted({str(row['asof_date']) for row in panel})
        prepared = prepare_validation_panel_v2(
            panel,
            baseline,
            bundle,
            complete_month_dates=complete_dates,
        )
        calibration_dates, census = calibration_date_census_same_sample(
            prepared, baseline, bundle
        )
        holdout_dates = [
            value for value in split['holdout_dates']
            if value in calibration_dates
        ]
        baseline_evidence = absolute_baseline_evidence(
            prepared, holdout_dates, baseline, bundle
        )
        freshness = _freshness_audit(conn, panel, bundle)
        membership = _membership_audit(conn)
        current_selection_sha = price_selection_sha256(conn)
    finally:
        conn.close()

    holdout_access = audit_existing_holdout_access(
        stage8_root=stage8_root, stage9_root=stage9_root
    )
    target_contract = validate_primary_target_contract(
        stage8_primary_target='weighted_21_63_126_rank_ic',
        stage9_return_target='fixed_21_session_xlp_relative_return',
        scoring_frequency='monthly',
        rebalance_frequency='21_sessions_with_greedy_overlap_suppression',
    )
    complete_month_ok = set(panel_dates).issubset(complete_dates)
    freshness_ok = all(
        int(row.get('stale_consumed_rows', 0)) == 0
        for row in freshness.values()
    )
    price_selection_tied = (
        current_selection_sha
        == str(decision['panel_summary']['frozen_price_selection_sha256'])
    )
    invariants = {
        'same_sample_correct': True,
        'positioning_freshness_correct': freshness_ok,
        'complete_month_cadence_correct': complete_month_ok,
        'source_identity_tied': False,
        'survivorship_correct': False,
        'holdout_unexposed': bool(
            holdout_access['holdout_unexposed_flag']
        ),
        'strict_oos': bool(cfg_get(
            bundle.payload, 'oos_provenance.strict_oos_start_date'
        )),
    }
    gate = fail_closed_limited_production_gate(
        baseline_evidence,
        minimum_holdout_dates=12,
        maximum_sign_pvalue=0.10,
        invariants=invariants,
    )
    stage9_decision = decision_from_bound_stage8(
        decision,
        absolute_baseline_gate=gate,
        holdout_violation_count=int(
            holdout_access['unauthorized_holdout_candidate_count']
        ),
        target_contract_pass=bool(target_contract['pass_flag']),
    )
    report = {
        'schema_version': 'consumer_defensive_validation_audit_v2',
        'stage8_root': str(stage8_root),
        'stage9_root': str(stage9_root),
        'database': str(database),
        'legacy_panel_date_count': len(panel_dates),
        'complete_month_date_count': len(complete_dates),
        'partial_or_immature_panel_dates': sorted(
            set(panel_dates) - set(complete_dates)
        ),
        'era_aware_calibration_date_count': len(calibration_dates),
        'era_aware_excluded_date_count': sum(
            int(row['included_flag']) == 0 for row in census
        ),
        'baseline_absolute_holdout_evidence': baseline_evidence,
        'stage9_baseline_phase_evidence': _stage9_baseline_phase_evidence(
            stage9_root, baseline.candidate_id
        ),
        'positioning_freshness': freshness,
        'membership_survivorship': membership,
        'holdout_access': {
            key: value for key, value in holdout_access.items()
            if key != 'violations'
        },
        'price_selection': {
            'expected_sha256': str(
                decision['panel_summary']['frozen_price_selection_sha256']
            ),
            'current_sha256': current_selection_sha,
            'price_selection_match_flag': int(price_selection_tied),
            'stage7_score_panel_parity_proven_flag': 0,
        },
        'target_contract': target_contract,
        'promotion_invariants': invariants,
        'limited_production_gate': gate,
        'stage9_v2_decision': stage9_decision,
    }
    return report


def main() -> int:
    args = _arguments()
    report = run(args)
    content = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if args.output is not None:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with atomic_text_writer(
            output, encoding='utf-8', newline=''
        ) as handle:
            handle.write(content)
    print(content, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
