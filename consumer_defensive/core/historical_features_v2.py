"""Fresh point-in-time historical features for calibration framework v2.

This module intentionally owns the v2 assembly path.  It reuses stable
Consumer Defensive market, financial, and panel helpers, but it does not read
historical ``feature_positioning`` snapshots and it does not call the legacy
Stage 8 panel builder.  Positioning observations are reconstructed for every
panel date with source-birthdate and freshness rules applied *before*
cross-sectional normalization.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any, Callable, Mapping, Sequence

from .config import ConfigBundle, cfg_get
from .institutional_history_v2 import load_institutional_history_v2
from .market_data import MarketDataPolicy
from .scoring_features import CORE_COMPONENT_SPECS
from .stage7_scoring import stage7_component_weights
from .stage8_calibration import (
    HORIZONS,
    RESEARCH_SAMPLE_ROLE,
    _canonical_json,
    _financial_features_for_date,
    _finite,
    _label_rows,
    _market_features_for_date,
    _normalize_component_rows,
    _panel_row_hash,
    _price_selection_and_history,
    _rank_requirements,
    _sha256,
    _specialized_rows,
)
from .stage8_calibration_v2 import positioning_rows_for_date_v2


HISTORICAL_FEATURES_V2 = 'consumer_defensive_historical_features_v2'


def _pit_insider_aggregate(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    ticker: str,
) -> dict[str, Any]:
    """Aggregate only Form 4 transactions observable at ``as_of``.

    Both SEC acceptance time and transaction/availability date are bounded.
    The complete ordered observation-id set is retained in lineage so a change
    to any contributing transaction changes the downstream panel hash.
    """

    evaluation = date.fromisoformat(as_of)
    birth = str(cfg_get(
        bundle.payload, 'positioning.source_birthdates.sec_form4'
    ))
    if as_of < birth:
        return {
            'net_value': None,
            'event_count': 0,
            'latest_accepted': '',
            'source_observation_ids': (),
            'source_state': 'structurally_unavailable',
        }

    lookback_days = int(cfg_get(
        bundle.payload, 'positioning.lookback_days.insider', 90
    ))
    if lookback_days < 0:
        raise ValueError('positioning.lookback_days.insider cannot be negative')
    window_start = evaluation - timedelta(days=lookback_days)
    maximum_age = cfg_get(
        bundle.payload, 'positioning.maximum_age_days.sec_form4'
    )
    if maximum_age is not None:
        maximum_age_days = int(maximum_age)
        if maximum_age_days < 0:
            raise ValueError(
                'positioning.maximum_age_days.sec_form4 cannot be negative'
            )
        window_start = max(
            window_start, evaluation - timedelta(days=maximum_age_days)
        )
    birth_date = date.fromisoformat(birth)
    window_start = max(window_start, birth_date)
    source_id = str(cfg_get(
        bundle.payload, 'positioning.ownership_source_id'
    ))
    cutoff = f'{as_of}T23:59:59Z'
    rows = conn.execute(
        '''SELECT transaction_id,accepted_at,availability_date,
                  transaction_date,acquired_disposed,shares,price,
                  source_observation_id
           FROM fact_sec_ownership_transaction
           WHERE ticker=? AND source_id=? AND is_current_truth=1
             AND accepted_at<=? AND availability_date<=?
             AND COALESCE(transaction_date,availability_date)>=?
             AND COALESCE(transaction_date,availability_date)<=?
             AND shares IS NOT NULL AND price IS NOT NULL
           ORDER BY accepted_at,transaction_id,source_observation_id''',
        (
            ticker,
            source_id,
            cutoff,
            as_of,
            window_start.isoformat(),
            as_of,
        ),
    ).fetchall()
    if not rows:
        return {
            'net_value': None,
            'event_count': 0,
            'latest_accepted': '',
            'source_observation_ids': (),
            'source_state': 'missing_or_stale',
        }
    net_value = 0.0
    for row in rows:
        disposition = str(row['acquired_disposed'] or '').upper()
        direction = 1.0 if disposition == 'A' else -1.0 if disposition == 'D' else 0.0
        net_value += direction * float(row['shares']) * float(row['price'])
    return {
        'net_value': net_value,
        'event_count': len(rows),
        'latest_accepted': max(str(row['accepted_at']) for row in rows),
        'source_observation_ids': tuple(
            str(row['source_observation_id']) for row in rows
        ),
        'source_state': 'fresh',
    }


def _latest_derived_institutional(
    history: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    ticker: str,
    as_of: str,
    maximum_age_days: int,
) -> Mapping[str, Any] | None:
    lower = (date.fromisoformat(as_of) - timedelta(days=maximum_age_days)).isoformat()
    for row in reversed(history.get(ticker, ())):
        publication = str(row["publication_date"])
        if publication <= as_of:
            return row if publication >= lower else None
    return None


def positioning_features_for_date_v2(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    tickers: set[str],
    institutional_history: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build freshness-filtered positioning features for one panel date."""

    births = {
        'sec_form4': str(cfg_get(
            bundle.payload, 'positioning.source_birthdates.sec_form4'
        )),
        'institutional_13f': str(cfg_get(
            bundle.payload,
            'positioning.source_birthdates.institutional_13f',
        )),
        'short_interest': str(cfg_get(
            bundle.payload, 'positioning.source_birthdates.short_interest'
        )),
        'borrow': str(cfg_get(
            bundle.payload, 'positioning.source_birthdates.borrow'
        )),
    }
    maximum_ages = {
        key: cfg_get(
            bundle.payload, f'positioning.maximum_age_days.{key}'
        )
        for key in births
    }
    output: dict[str, dict[str, Any]] = {}
    for ticker in sorted(tickers):
        sources = positioning_rows_for_date_v2(
            conn, bundle, as_of=as_of, ticker=ticker
        )
        insider = _pit_insider_aggregate(
            conn, bundle, as_of=as_of, ticker=ticker
        )
        states = dict(sources['source_states'])
        if institutional_history is None:
            institutional = sources['institutional_13f']
        elif as_of < births['institutional_13f']:
            institutional = None
            states['institutional_13f'] = 'structurally_unavailable'
        else:
            institutional = _latest_derived_institutional(
                institutional_history,
                ticker=ticker,
                as_of=as_of,
                maximum_age_days=int(maximum_ages['institutional_13f']),
            )
            states['institutional_13f'] = (
                'fresh' if institutional is not None else 'missing_or_stale'
            )
        short = sources['short_interest']
        borrow = sources['borrow']
        institutional_flow = (
            _finite(institutional['institutional_ownership_delta_pct'])
            if institutional is not None else None
        )
        short_pct = (
            _finite(short['short_float_pct']) if short is not None else None
        )
        short_days = (
            _finite(short['days_to_cover']) if short is not None else None
        )
        borrow_fee = (
            _finite(borrow['borrow_fee']) if borrow is not None else None
        )
        required_values = {
            'institutional_13f': institutional_flow,
            'short_interest': (
                short_pct if short_pct is not None else short_days
            ),
        }
        required_sources = [
            key for key in required_values if as_of >= births[key]
        ]
        present = sum(
            required_values[key] is not None for key in required_sources
        )
        if not required_sources:
            quality = 'unavailable'
        elif present == 0:
            quality = 'missing'
        elif present < len(required_sources) or any(
            states[key] != 'fresh' for key in required_sources
        ):
            quality = 'partial'
        else:
            quality = 'complete'
        lineage = {
            'definition_version': HISTORICAL_FEATURES_V2,
            'asof_date': as_of,
            'source_birthdates': births,
            'maximum_age_days': maximum_ages,
            'source_states': {
                **states,
                'sec_form4': insider['source_state'],
            },
            'ownership': {
                'event_count': insider['event_count'],
                'latest_accepted': insider['latest_accepted'],
                'source_observation_ids': list(
                    insider['source_observation_ids']
                ),
            },
            'institutional_observation_id': (
                str(institutional['source_observation_id'])
                if institutional is not None else ''
            ),
            'short_observation_id': (
                str(short['source_observation_id'])
                if short is not None else ''
            ),
            'borrow_observation_id': (
                str(borrow['source_observation_id'])
                if borrow is not None else ''
            ),
        }
        output[ticker] = {
            'insider_net_buying': _finite(insider['net_value']),
            'institutional_flow': institutional_flow,
            'short_float_pct': short_pct,
            'short_days_to_cover': short_days,
            'borrow_fee': borrow_fee,
            'quality_status': quality,
            'source_hash': _sha256(lineage),
            'source_states': lineage['source_states'],
        }
    return output


def build_historical_core_panel_v2(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    stage6c_run_id: int,
    membership_rows: Sequence[Mapping[str, Any]],
    accepted_factor_cells: Sequence[Mapping[str, Any]],
    market_policy: MarketDataPolicy,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the v2 PIT panel with freshness filtering before normalization."""

    if not membership_rows:
        raise RuntimeError('Historical feature v2 membership census is empty.')
    all_labels = {
        (str(row['asof_date']), str(row['ticker'])): row
        for row in _label_rows(conn, stage6c_run_id=stage6c_run_id)
    }
    membership_identities = {
        (str(row['asof_date']), str(row['ticker']))
        for row in membership_rows
    }
    labels = {
        key: row for key, row in all_labels.items()
        if key in membership_identities
    }
    if set(labels) != membership_identities:
        raise RuntimeError(
            'Historical feature v2 membership and forward-label identities '
            'do not tie.'
        )
    directions: dict[str, str] = {}
    accepted_ids: set[str] = set()
    for cell in accepted_factor_cells:
        factor_id = str(cell['factor_id'])
        direction = str(cell['factor_direction'])
        if factor_id in directions and directions[factor_id] != direction:
            raise RuntimeError(
                f'Accepted factor direction conflict for {factor_id}.'
            )
        directions[factor_id] = direction
        accepted_ids.add(factor_id)
    specialized_rows = _specialized_rows(
        conn,
        stage6c_run_id=stage6c_run_id,
        factor_ids=accepted_ids,
    )
    specialized_by_key: dict[
        tuple[str, str], dict[str, dict[str, Any]]
    ] = defaultdict(dict)
    for row in specialized_rows:
        specialized_by_key[
            (str(row['asof_date']), str(row['ticker']))
        ][str(row['factor_id'])] = row

    dates = sorted({str(row['asof_date']) for row in membership_rows})
    tickers = {str(row['ticker']) for row in membership_rows}
    institutional_history, institutional_summary = load_institutional_history_v2(
        bundle,
        tickers=tickers,
        history_start=dates[0],
        maximum_date=dates[-1],
    )
    members_by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in membership_rows:
        members_by_date[str(row['asof_date'])].append(row)
    selection, price_history, selection_sha = _price_selection_and_history(
        conn, tickers=tickers, maximum_date=dates[-1]
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
    minimum_peers = int(cfg_get(
        bundle.payload, 'scoring_features.minimum_normalization_peer_count'
    ))
    neutral = float(cfg_get(
        bundle.payload, 'stage7_scoring.neutral_score'
    ))
    baseline_weights = stage7_component_weights(bundle)
    minimum_quality = float(cfg_get(
        bundle.payload,
        'stage7_scoring.minimum_data_quality_confidence',
    ))
    maximum_missing = float(cfg_get(
        bundle.payload,
        'stage7_scoring.maximum_missing_component_weight',
    ))
    panel: list[dict[str, Any]] = []
    financial_quality_counts: Counter[str] = Counter()
    positioning_quality_counts: Counter[str] = Counter()
    positioning_source_state_counts: Counter[str] = Counter()
    market_quality_counts: Counter[str] = Counter()

    for position, as_of in enumerate(dates, start=1):
        members = members_by_date[as_of]
        date_tickers = {str(row['ticker']) for row in members}
        market = _market_features_for_date(
            as_of=as_of,
            tickers=date_tickers,
            selection=selection,
            history=price_history,
            policy=market_policy,
        )
        financial = _financial_features_for_date(
            conn, bundle, as_of=as_of, tickers=date_tickers
        )
        positioning = positioning_features_for_date_v2(
            conn,
            bundle,
            as_of=as_of,
            tickers=date_tickers,
            institutional_history=institutional_history,
        )
        market_quality_counts.update(
            str(row['quality_status']) for row in market.values()
        )
        financial_quality_counts.update(
            str(row['financial_quality_status'])
            for row in financial.values()
        )
        positioning_quality_counts.update(
            str(row['quality_status']) for row in positioning.values()
        )
        for row in positioning.values():
            positioning_source_state_counts.update(
                f'{key}:{value}'
                for key, value in row['source_states'].items()
            )

        # Freshness and quality are resolved above.  Only rows marked available
        # below become peers, so stale observations cannot affect percentiles.
        component_rows: list[dict[str, Any]] = []
        for member in members:
            ticker = str(member['ticker'])
            cohort = str(member['cohort_id'])
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
                        str(upstream['source_hash'])
                        if upstream is not None else ''
                    ),
                })
        _normalize_component_rows(
            component_rows, minimum_peers=minimum_peers
        )
        components_by_ticker: dict[
            str, dict[str, dict[str, Any]]
        ] = defaultdict(dict)
        for row in component_rows:
            components_by_ticker[str(row['ticker'])][
                str(row['component_name'])
            ] = row

        specialized_components: list[dict[str, Any]] = []
        for member in members:
            ticker = str(member['ticker'])
            cohort = str(member['cohort_id'])
            available = specialized_by_key.get((as_of, ticker), {})
            for factor_id in sorted(accepted_ids):
                source = available.get(factor_id)
                if source is None:
                    continue
                raw = (
                    _finite(source['factor_value'])
                    if str(source['availability_status']) == 'available'
                    else None
                )
                specialized_components.append({
                    'ticker': ticker,
                    'cohort_id': cohort,
                    'component_name': factor_id,
                    'raw_value': raw,
                    'component_score': None,
                    'direction': (
                        'higher'
                        if directions[factor_id] == 'higher_is_better'
                        else 'lower'
                    ),
                    'availability_status': (
                        'available' if raw is not None
                        else 'missing_or_quality_rejected'
                    ),
                })
        _normalize_component_rows(
            specialized_components, minimum_peers=minimum_peers
        )
        specialized_scores_by_ticker: dict[str, dict[str, float]] = (
            defaultdict(dict)
        )
        for row in specialized_components:
            score = _finite(row['component_score'])
            if (
                row['availability_status'] == 'available'
                and score is not None
            ):
                specialized_scores_by_ticker[str(row['ticker'])][
                    str(row['component_name'])
                ] = score

        for member in members:
            ticker = str(member['ticker'])
            label = labels[(as_of, ticker)]
            components = components_by_ticker[ticker]
            ready, readiness_reasons = _rank_requirements(components)
            scores: dict[str, float] = {}
            quality: dict[str, float] = {}
            raw_values: dict[str, float | None] = {}
            source_hashes: dict[str, str] = {}
            weighted_score = 0.0
            available_weight = 0.0
            missing_weight = 0.0
            missing_components: list[str] = []
            for spec in CORE_COMPONENT_SPECS:
                component = components[spec.name]
                score = _finite(component['component_score'])
                available = (
                    component['availability_status'] == 'available'
                    and score is not None
                )
                effective = (
                    min(100.0, max(0.0, score)) if available else neutral
                )
                weight = baseline_weights[spec.name]
                scores[spec.name] = effective
                quality[spec.name] = 1.0 if available else 0.0
                raw_values[spec.name] = _finite(component['raw_value'])
                source_hashes[spec.name] = str(component['source_hash'])
                weighted_score += weight * effective
                if available:
                    available_weight += weight
                else:
                    missing_weight += weight
                    if weight > 0.0:
                        missing_components.append(spec.name)
            reasons = list(readiness_reasons)
            if available_weight < minimum_quality:
                reasons.append(f'low_data_quality={available_weight:.6f}')
            if missing_weight > maximum_missing:
                reasons.append(
                    f'missing_component_weight={missing_weight:.6f}:'
                    + ','.join(missing_components)
                )
            rank_ready = int(ready and not (
                available_weight < minimum_quality
                or missing_weight > maximum_missing
            ))
            membership_eligible = int(label['membership_eligible_flag'])
            investable = int(label['investable_flag'])
            specialized_applicable = {
                factor_id: int(
                    factor_id in specialized_by_key.get((as_of, ticker), {})
                )
                for factor_id in sorted(accepted_ids)
            }
            row = {
                'asof_date': as_of,
                'ticker': ticker,
                'cohort_id': str(member['cohort_id']),
                'applicability_subtype': str(
                    member['applicability_subtype']
                ),
                'sample_role': str(label['sample_role']),
                'membership_eligible_flag': membership_eligible,
                'investable_flag': investable,
                'label_status': str(label['label_status']),
                'market_regime': str(label['market_regime'] or ''),
                'terminal_event_status': str(
                    label['terminal_event_status'] or ''
                ),
                'baseline_rank_ready_flag': rank_ready,
                'calibration_eligible_flag': int(
                    rank_ready == 1
                    and membership_eligible == 1
                    and investable == 1
                ),
                'review_reason': ';'.join(sorted(reasons)),
                'core_score': weighted_score,
                'available_weight': available_weight,
                'missing_weight': missing_weight,
                'component_raw_values_json': _canonical_json(raw_values),
                'component_scores_json': _canonical_json(scores),
                'component_quality_json': _canonical_json(quality),
                'component_source_hashes_json': _canonical_json(source_hashes),
                'specialized_scores_json': _canonical_json(
                    specialized_scores_by_ticker.get(ticker, {})
                ),
                'specialized_applicability_json': _canonical_json(
                    specialized_applicable
                ),
                **{
                    f'forward_xlp_residual_return_{horizon}d': _finite(
                        label[f'forward_xlp_residual_return_{horizon}d']
                    )
                    for horizon in HORIZONS
                },
            }
            row['row_sha256'] = _panel_row_hash(row)
            panel.append(row)
        if progress is not None:
            progress(position, len(dates), as_of)

    panel.sort(key=lambda row: (row['asof_date'], row['ticker']))
    panel_sha = _sha256([row['row_sha256'] for row in panel])
    return panel, {
        'schema_version': HISTORICAL_FEATURES_V2,
        'panel_sha256': panel_sha,
        'row_count': len(panel),
        'date_count': len(dates),
        'ticker_count': len(tickers),
        'first_date': dates[0],
        'last_date': dates[-1],
        'frozen_price_selection_sha256': selection_sha,
        'accepted_specialized_factor_count': len(accepted_ids),
        'institutional_history': institutional_summary,
        'market_quality_counts': dict(sorted(market_quality_counts.items())),
        'financial_quality_counts': dict(
            sorted(financial_quality_counts.items())
        ),
        'positioning_quality_counts': dict(
            sorted(positioning_quality_counts.items())
        ),
        'positioning_source_state_counts': dict(
            sorted(positioning_source_state_counts.items())
        ),
        'sample_role': RESEARCH_SAMPLE_ROLE,
    }


__all__ = [
    'HISTORICAL_FEATURES_V2',
    'build_historical_core_panel_v2',
    'positioning_features_for_date_v2',
]
