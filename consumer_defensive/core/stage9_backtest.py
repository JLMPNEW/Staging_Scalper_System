from __future__ import annotations

import bisect
import csv
import gzip
import hashlib
import io
import json
import math
import os
import sqlite3
import statistics
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .atomic_io import atomic_text_writer
from .config import ConfigBundle, cfg_get, load_yaml
from .stage8_calibration import (
    CANDIDATE_FILE as STAGE8_CANDIDATE_FILE,
    CONTRACT_FILE as STAGE8_CONTRACT_FILE,
    MANIFEST_FILE as STAGE8_MANIFEST_FILE,
    PANEL_FILE as STAGE8_PANEL_FILE,
    SPLIT_FILE as STAGE8_SPLIT_FILE,
    Candidate,
    validate_stage8_artifacts,
)


STAGE9_VERSION = 'consumer_defensive_stage9_portfolio_backtest_v1'
SECTOR_SCOPE = 'consumer_defensive'
CONTRACT_FILE = 'stage9_contract.json'
SCHEDULE_FILE = 'stage9_date_schedule.csv'
SUMMARY_FILE = 'stage9_summary.csv'
PERIOD_FILE = 'stage9_period_results.csv.gz'
HOLDING_FILE = 'stage9_holdings.csv.gz'
TIEOUT_FILE = 'stage9_source_tieout.csv'
DECISION_FILE = 'stage9_decision.json'
MANIFEST_FILE = 'stage9_artifact_manifest.json'
VALIDATION_FILE = 'stage9_validation.json'
STAGE9_POLICY_FILE = 'consumer_defensive_stage9_backtest.yaml'


@dataclass(frozen=True)
class PortfolioSpec:
    portfolio_name: str
    weight_method: str
    exposure_mode: str


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


def _close(left: Any, right: Any, tolerance: float = 1e-10) -> bool:
    first = _finite(left)
    second = _finite(right)
    if first is None or second is None:
        return first is None and second is None
    return abs(first - second) <= tolerance * max(1.0, abs(first), abs(second))


def _immutable_text(path: Path, content: str) -> None:
    if path.is_symlink():
        raise RuntimeError(f'Refusing Stage 9 symlink artifact: {path}')
    encoded = content.encode('utf-8')
    if path.exists():
        if path.read_bytes() != encoded:
            raise FileExistsError(
                f'Immutable Stage 9 artifact content changed: {path}'
            )
        return
    with atomic_text_writer(path, encoding='utf-8', newline='') as handle:
        handle.write(content)


def _immutable_json(path: Path, payload: Any) -> None:
    _immutable_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + '\n',
    )


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
        handle,
        fieldnames=_columns(rows),
        extrasaction='ignore',
        lineterminator='\n',
    )
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue()


def _immutable_csv_gzip(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    if path.is_symlink():
        raise RuntimeError(f'Refusing Stage 9 symlink artifact: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f'.{path.name}.',
        suffix='.tmp',
    )
    temporary = Path(temporary_name)
    raw: io.BufferedWriter | None = None
    compressed: gzip.GzipFile | None = None
    text_handle: io.TextIOWrapper | None = None
    try:
        raw = os.fdopen(descriptor, 'wb')
        descriptor = -1
        compressed = gzip.GzipFile(
            filename='', mode='wb', fileobj=raw, mtime=0
        )
        text_handle = io.TextIOWrapper(
            compressed, encoding='utf-8', newline=''
        )
        writer = csv.DictWriter(
            text_handle,
            fieldnames=_columns(rows),
            extrasaction='ignore',
            lineterminator='\n',
        )
        writer.writeheader()
        writer.writerows(rows)
        text_handle.flush()
        text_handle.detach()
        text_handle = None
        compressed.close()
        compressed = None
        raw.flush()
        os.fsync(raw.fileno())
        raw.close()
        raw = None
        if path.exists():
            if _file_sha256(path) != _file_sha256(temporary):
                raise FileExistsError(
                    f'Immutable Stage 9 artifact content changed: {path}'
                )
            temporary.unlink()
        else:
            os.replace(temporary, path)
    finally:
        if text_handle is not None:
            text_handle.close()
        if compressed is not None:
            compressed.close()
        if raw is not None:
            raw.close()
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return payload


def _read_csv(path: Path) -> list[dict[str, str]]:
    if path.suffix == '.gz':
        with gzip.open(path, mode='rt', encoding='utf-8', newline='') as handle:
            return list(csv.DictReader(handle))
    with path.open(mode='r', encoding='utf-8', newline='') as handle:
        return list(csv.DictReader(handle))


def validate_stage9_policy(section: Mapping[str, Any]) -> None:
    expected = {
        'mode': 'report_only',
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'oos_score_valid_flag': 0,
        'evaluation_horizon_days': 21,
        'nonoverlap_policy': 'earliest_start_greedy',
        'weight_methods': ['equal_weight', 'score_weight'],
        'exposure_modes': ['long_only', 'dollar_neutral'],
        'require_terminal_value_reconciliation': True,
        'candidate_scope': 'all_registered_stage8_candidates',
    }
    for key, required in expected.items():
        actual = section.get(key)
        if actual != required:
            raise ValueError(
                f'stage9_backtest.{key} must be {required!r}; got {actual!r}'
            )
    allowed = {
        *expected,
        'top_quantile', 'minimum_positions',
        'minimum_sector_cross_section', 'minimum_cohort_cross_section',
        'transaction_cost_bps', 'unavailable_borrow_stress_annual_rate',
        'reference_nav_usd', 'adv_participation_rate',
        'stress_adv_participation_rate', 'maximum_exit_days',
    }
    if unknown := sorted(set(section) - allowed):
        raise ValueError(f'Unknown Stage 9 policy keys: {unknown}')
    for key in (
        'minimum_positions', 'minimum_sector_cross_section',
        'minimum_cohort_cross_section',
    ):
        if int(section.get(key) or 0) < 1:
            raise ValueError(f'stage9_backtest.{key} must be a positive integer.')
    minimum_positions = int(section['minimum_positions'])
    if minimum_positions < 3:
        raise ValueError('stage9_backtest.minimum_positions must be at least 3.')
    for scope in ('sector', 'cohort'):
        if int(section[f'minimum_{scope}_cross_section']) < 2 * minimum_positions:
            raise ValueError(
                f'stage9_backtest.minimum_{scope}_cross_section must support '
                'disjoint long and short legs.'
            )
    for key in (
        'top_quantile', 'adv_participation_rate',
        'stress_adv_participation_rate',
    ):
        value = float(section.get(key) or 0.0)
        if not 0.0 < value <= 1.0:
            raise ValueError(f'stage9_backtest.{key} must be in (0,1].')
    if float(section['stress_adv_participation_rate']) > float(
        section['adv_participation_rate']
    ):
        raise ValueError(
            'stage9_backtest.stress_adv_participation_rate cannot exceed '
            'adv_participation_rate.'
        )
    for key in (
        'transaction_cost_bps', 'unavailable_borrow_stress_annual_rate',
    ):
        if float(section.get(key) or 0.0) < 0.0:
            raise ValueError(f'stage9_backtest.{key} cannot be negative.')
    for key in ('reference_nav_usd', 'maximum_exit_days'):
        if float(section.get(key) or 0.0) <= 0.0:
            raise ValueError(f'stage9_backtest.{key} must be positive.')


def stage9_config_payload(bundle: ConfigBundle) -> dict[str, Any]:
    policy_path = bundle.base_dir / 'data' / STAGE9_POLICY_FILE
    payload = load_yaml(policy_path)
    if set(payload) != {'schema_version', 'stage9_backtest'}:
        raise ValueError('Stage 9 policy must contain only schema_version and stage9_backtest.')
    if payload['schema_version'] != 'consumer_defensive_stage9_backtest_policy_v1':
        raise ValueError('Unknown Consumer Defensive Stage 9 policy version.')
    section = payload['stage9_backtest']
    if not isinstance(section, dict):
        raise ValueError('stage9_backtest must be a mapping.')
    validate_stage9_policy(section)
    return json.loads(_canonical_json(section))


def _candidate(payload: Mapping[str, Any]) -> Candidate:
    return Candidate(
        candidate_id=str(payload['candidate_id']),
        scope_id=str(payload['scope_id']),
        candidate_kind=str(payload['candidate_kind']),
        core_weights={
            str(key): float(value)
            for key, value in dict(payload['core_weights']).items()
        },
        specialized_weights={
            str(key): float(value)
            for key, value in dict(payload['specialized_weights']).items()
        },
        parent_candidate_id=(
            None
            if payload.get('parent_candidate_id') in (None, '')
            else str(payload['parent_candidate_id'])
        ),
        shrinkage_alpha=float(payload.get('shrinkage_alpha') or 0.0),
        evidence_references=tuple(
            str(value) for value in payload.get('evidence_references', [])
        ),
        preregistration_sha256=str(payload['preregistration_sha256']),
    )


def _parse_panel_row(source: Mapping[str, str]) -> dict[str, Any]:
    row: dict[str, Any] = dict(source)
    for key in (
        'membership_eligible_flag', 'investable_flag',
        'baseline_rank_ready_flag', 'calibration_eligible_flag',
    ):
        row[key] = int(row[key])
    for key in (
        'core_score', 'available_weight', 'missing_weight',
        'forward_xlp_residual_return_21d',
        'forward_xlp_residual_return_63d',
        'forward_xlp_residual_return_126d',
    ):
        row[key] = _finite(row[key])
    for source_key, target_key in (
        ('component_raw_values_json', '_component_raw_values'),
        ('component_scores_json', '_component_scores'),
        ('component_quality_json', '_component_quality'),
        ('specialized_scores_json', '_specialized_scores'),
        ('specialized_applicability_json', '_specialized_applicability'),
    ):
        value = json.loads(str(row[source_key]))
        if not isinstance(value, dict):
            raise ValueError(f'Invalid Stage 8 panel mapping: {source_key}')
        row[target_key] = value
    return row


def _load_panel(path: Path) -> list[dict[str, Any]]:
    rows = [_parse_panel_row(row) for row in _read_csv(path)]
    if not rows:
        raise RuntimeError('Stage 8 panel is empty.')
    keys = [(str(row['asof_date']), str(row['ticker'])) for row in rows]
    if len(keys) != len(set(keys)):
        raise RuntimeError('Stage 8 panel has duplicate date/ticker rows.')
    return rows


def _load_stage6c_labels(
    conn: sqlite3.Connection,
    *,
    stage6c_run_id: int,
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = conn.execute(
        '''SELECT asof_date,ticker,
                  COUNT(DISTINCT terminal_event_status) AS status_count,
                  MIN(terminal_event_status) AS terminal_event_status,
                  COUNT(DISTINCT forward_total_return_21d) AS total21_count,
                  MIN(forward_total_return_21d) AS forward_total_return_21d,
                  COUNT(DISTINCT forward_total_return_63d) AS total63_count,
                  MIN(forward_total_return_63d) AS forward_total_return_63d,
                  COUNT(DISTINCT forward_total_return_126d) AS total126_count,
                  MIN(forward_total_return_126d) AS forward_total_return_126d,
                  COUNT(*) AS factor_rows
           FROM stage6c_specialized_factor_panel
           WHERE stage6c_run_id=?
           GROUP BY asof_date,ticker
           ORDER BY asof_date,ticker''',
        (stage6c_run_id,),
    ).fetchall()
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for source in rows:
        row = dict(source)
        if int(row['status_count']) != 1 or any(
            int(row[key]) > 1
            for key in ('total21_count', 'total63_count', 'total126_count')
        ):
            raise RuntimeError(
                'Stage 6C duplicate factor rows disagree on labels for '
                '{}:{}'.format(row['asof_date'], row['ticker'])
            )
        output[(str(row['asof_date']), str(row['ticker']))] = row
    return output


def _selected_price_sources(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        '''SELECT ticker,selected_source_id
           FROM dim_price_series_selection
           WHERE purpose='scoring_return_series' '''
    ).fetchall()
    output = {str(row['ticker']): str(row['selected_source_id']) for row in rows}
    if {'SPY', 'XLP'} - set(output):
        raise RuntimeError('Stage 9 requires frozen SPY and XLP selections.')
    return output


def _trading_calendar(
    conn: sqlite3.Connection,
    *,
    selected_sources: Mapping[str, str],
) -> list[str]:
    rows = conn.execute(
        '''SELECT bar_date FROM fact_price_ohlcv
           WHERE ticker='SPY' AND source_id=?
           ORDER BY bar_date''',
        (selected_sources['SPY'],),
    ).fetchall()
    calendar = [str(row['bar_date']) for row in rows]
    if not calendar:
        raise RuntimeError('Frozen SPY trading calendar is empty.')
    return calendar


def _split_roles(split: Mapping[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    for key, role in (
        ('train_dates', 'train'),
        ('first_embargo_dates', 'embargo_1'),
        ('validation_dates', 'validation'),
        ('second_embargo_dates', 'embargo_2'),
        ('holdout_dates', 'holdout'),
    ):
        for value in split.get(key, []):
            if str(value) in output:
                raise RuntimeError(f'Duplicate Stage 8 split date: {value}')
            output[str(value)] = role
    return output


def build_nonoverlap_schedule(
    split: Mapping[str, Any],
    calendar: Sequence[str],
    *,
    entry_lag: int,
    horizon_days: int,
) -> list[dict[str, Any]]:
    roles = _split_roles(split)
    census_dates = [
        str(row['asof_date'])
        for row in split.get('calibration_date_census', [])
        if int(row.get('included_flag') or 0) == 1
    ]
    if census_dates != sorted(roles):
        raise RuntimeError(
            'Stage 8 calibration census and chronological split disagree.'
        )
    previous_exit: str | None = None
    output: list[dict[str, Any]] = []
    for as_of in census_dates:
        evaluation_index = bisect.bisect_left(calendar, as_of)
        if evaluation_index >= len(calendar) or calendar[evaluation_index] != as_of:
            raise RuntimeError(f'Stage 9 evaluation date is not a SPY session: {as_of}')
        entry_index = evaluation_index + entry_lag
        exit_index = entry_index + horizon_days
        if exit_index >= len(calendar):
            raise RuntimeError(f'Stage 9 horizon unavailable for {as_of}')
        entry_date = str(calendar[entry_index])
        exit_date = str(calendar[exit_index])
        selected = previous_exit is None or entry_date >= previous_exit
        reason = '' if selected else 'overlaps_previous_selected_21d_window'
        row = {
            'asof_date': as_of,
            'split_role': roles[as_of],
            'entry_date': entry_date,
            'exit_date': exit_date,
            'selected_nonoverlap_flag': int(selected),
            'exclusion_reason': reason,
        }
        row['schedule_row_sha256'] = _sha256(row)
        output.append(row)
        if selected:
            previous_exit = exit_date
    return output


def _terminal_price_presence(
    conn: sqlite3.Connection,
    *,
    schedule: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    dates = sorted({
        str(row['exit_date'])
        for row in schedule
        if int(row['selected_nonoverlap_flag']) == 1
    })
    if not dates:
        return set()
    placeholders = ','.join('?' for _ in dates)
    rows = conn.execute(
        f'''SELECT p.ticker,p.bar_date
            FROM fact_price_ohlcv p
            JOIN dim_price_series_selection s
              ON s.ticker=p.ticker
             AND s.purpose='scoring_return_series'
             AND s.selected_source_id=p.source_id
            WHERE p.bar_date IN ({placeholders})
              AND p.adjusted_close IS NOT NULL
              AND p.adjusted_close>0''',
        dates,
    ).fetchall()
    return {(str(row['ticker']), str(row['bar_date'])) for row in rows}


def _enrich_panel(
    rows: Sequence[dict[str, Any]],
    labels: Mapping[tuple[str, str], Mapping[str, Any]],
    schedule: Sequence[Mapping[str, Any]],
    price_presence: set[tuple[str, str]],
    *,
    horizon_days: int,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    schedule_by_date = {
        str(row['asof_date']): row for row in schedule
    }
    xlp_by_date: dict[str, float] = {}
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        key = (str(row['asof_date']), str(row['ticker']))
        label = labels.get(key)
        if label is None:
            raise RuntimeError(f'Stage 6C total-return label missing: {key}')
        total = _finite(label[f'forward_total_return_{horizon_days}d'])
        residual = _finite(row[f'forward_xlp_residual_return_{horizon_days}d'])
        if total is None or residual is None:
            raise RuntimeError(f'Complete Stage 9 label missing: {key}')
        xlp_return = total - residual
        existing = xlp_by_date.get(key[0])
        if existing is not None and not _close(existing, xlp_return, 1e-11):
            raise RuntimeError(f'XLP residual reconciliation failed: {key[0]}')
        xlp_by_date[key[0]] = xlp_return
        stage6c_status = str(label['terminal_event_status'])
        if stage6c_status != str(row['terminal_event_status']):
            raise RuntimeError(f'Terminal status drift: {key}')
        schedule_row = schedule_by_date.get(key[0])
        terminal_used = int(
            stage6c_status == 'terminal_value_resolved'
            and schedule_row is not None
            and int(schedule_row['selected_nonoverlap_flag']) == 1
            and (key[1], str(schedule_row['exit_date'])) not in price_presence
        )
        row['_forward_total_return'] = total
        row['_forward_xlp_residual_return'] = residual
        row['_xlp_return'] = xlp_return
        row['_terminal_return_used_flag'] = terminal_used
        output.append(row)
    return output, xlp_by_date


def _score_candidate(
    row: Mapping[str, Any],
    candidate: Candidate,
    *,
    neutral_score: float,
    minimum_quality: float,
    maximum_missing: float,
) -> tuple[float, float, float, bool]:
    weighted = 0.0
    available_weight = 0.0
    missing_weight = 0.0
    scores = row['_component_scores']
    quality = row['_component_quality']
    for name, weight in candidate.core_weights.items():
        score = _finite(scores.get(name))
        available = float(quality.get(name, 0.0)) > 0.0 and score is not None
        effective = score if available else neutral_score
        weighted += weight * min(100.0, max(0.0, float(effective)))
        if available:
            available_weight += weight
        else:
            missing_weight += weight
    specialized_scores = row['_specialized_scores']
    for name, weight in candidate.specialized_weights.items():
        score = _finite(specialized_scores.get(name))
        effective = score if score is not None else neutral_score
        weighted += weight * min(100.0, max(0.0, float(effective)))
        if score is not None:
            available_weight += weight
        else:
            missing_weight += weight
    eligible = (
        int(row['calibration_eligible_flag']) == 1
        and available_weight >= minimum_quality
        and missing_weight <= maximum_missing
    )
    return weighted, available_weight, missing_weight, eligible


def _weighted_leg(
    rows: Sequence[Mapping[str, Any]],
    *,
    side: str,
    method: str,
) -> dict[str, float]:
    if not rows:
        return {}
    if method == 'equal_weight':
        return {str(row['ticker']): 1.0 / len(rows) for row in rows}
    if method != 'score_weight':
        raise ValueError(f'Unknown Stage 9 weight method: {method}')
    scores = [float(row['_candidate_score']) for row in rows]
    if side == 'long':
        floor = min(scores)
        raw = [max(0.0, score - floor) + 1e-9 for score in scores]
    else:
        ceiling = max(scores)
        raw = [max(0.0, ceiling - score) + 1e-9 for score in scores]
    total = sum(raw)
    if total <= 0.0:
        raise RuntimeError('Stage 9 score-weight leg has zero mass.')
    return {
        str(row['ticker']): value / total
        for row, value in zip(rows, raw, strict=True)
    }


def build_portfolio_weights(
    scored_rows: Sequence[Mapping[str, Any]],
    spec: PortfolioSpec,
    *,
    top_quantile: float,
    minimum_positions: int,
) -> dict[str, float]:
    ordered = sorted(
        scored_rows,
        key=lambda row: (-float(row['_candidate_score']), str(row['ticker'])),
    )
    count = len(ordered)
    leg_count = max(minimum_positions, int(math.ceil(count * top_quantile)))
    if spec.exposure_mode == 'dollar_neutral':
        leg_count = min(leg_count, count // 2)
    else:
        leg_count = min(leg_count, count)
    if leg_count < minimum_positions:
        return {}
    longs = ordered[:leg_count]
    long_weights = _weighted_leg(
        longs, side='long', method=spec.weight_method
    )
    if spec.exposure_mode == 'long_only':
        return long_weights
    if spec.exposure_mode != 'dollar_neutral':
        raise ValueError(f'Unknown Stage 9 exposure mode: {spec.exposure_mode}')
    shorts = list(reversed(ordered[-leg_count:]))
    if {str(row['ticker']) for row in longs} & {
        str(row['ticker']) for row in shorts
    }:
        raise RuntimeError('Stage 9 long and short legs overlap.')
    short_weights = _weighted_leg(
        shorts, side='short', method=spec.weight_method
    )
    output = {ticker: 0.5 * weight for ticker, weight in long_weights.items()}
    output.update({
        ticker: -0.5 * weight for ticker, weight in short_weights.items()
    })
    return output


def _annual_borrow_rate(value: Any) -> float | None:
    rate = _finite(value)
    if rate is None:
        return None
    if abs(rate) > 1.0:
        rate /= 100.0
    return max(0.0, rate)


def _portfolio_specs(bundle: ConfigBundle) -> list[PortfolioSpec]:
    methods = [
        str(value) for value in stage9_config_payload(bundle)['weight_methods']
    ]
    return [
        PortfolioSpec('long_only_top_quintile', method, 'long_only')
        for method in methods
    ] + [
        PortfolioSpec(
            'long_short_top_bottom_quintile', method, 'dollar_neutral'
        )
        for method in methods
    ]


def _cohort_metrics(
    weights: Mapping[str, float],
    rows_by_ticker: Mapping[str, Mapping[str, Any]],
) -> tuple[float, float, float]:
    gross = sum(abs(value) for value in weights.values())
    if gross <= 0.0:
        return 0.0, 0.0, 0.0
    by_cohort: dict[str, float] = defaultdict(float)
    single_name_hhi = 0.0
    maximum_name = 0.0
    for ticker, weight in weights.items():
        share = abs(weight) / gross
        maximum_name = max(maximum_name, share)
        single_name_hhi += share * share
        cohort = str(rows_by_ticker[ticker]['cohort_id'])
        by_cohort[cohort] += share
    return max(by_cohort.values()), single_name_hhi, maximum_name


def _position_capacity(
    *,
    adv: float | None,
    weight: float,
    participation_rate: float,
    maximum_exit_days: float,
) -> float | None:
    if adv is None or adv <= 0.0 or weight == 0.0:
        return None
    return adv * participation_rate * maximum_exit_days / abs(weight)


def _drifted_end_weights(
    weights: Mapping[str, float],
    rows_by_ticker: Mapping[str, Mapping[str, Any]],
    gross_return: float,
) -> dict[str, float]:
    denominator = 1.0 + gross_return
    if denominator <= 0.0:
        raise RuntimeError('Stage 9 portfolio equity became non-positive.')
    return {
        ticker: weight * (
            1.0 + float(rows_by_ticker[ticker]['_forward_total_return'])
        ) / denominator
        for ticker, weight in weights.items()
    }


def _simulate(
    panel_by_date: Mapping[str, Sequence[Mapping[str, Any]]],
    schedule: Sequence[Mapping[str, Any]],
    candidate: Candidate,
    spec: PortfolioSpec,
    bundle: ConfigBundle,
    *,
    stage9_run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    settings = stage9_config_payload(bundle)
    neutral = float(cfg_get(bundle.payload, 'stage7_scoring.neutral_score'))
    minimum_quality = float(cfg_get(
        bundle.payload, 'stage7_scoring.minimum_data_quality_confidence'
    ))
    maximum_missing = float(cfg_get(
        bundle.payload, 'stage7_scoring.maximum_missing_component_weight'
    ))
    minimum_cross_section = int(settings[
        'minimum_sector_cross_section'
        if candidate.scope_id == SECTOR_SCOPE
        else 'minimum_cohort_cross_section'
    ])
    horizon = int(settings['evaluation_horizon_days'])
    transaction_bps = float(settings['transaction_cost_bps'])
    borrow_stress_rate = float(settings['unavailable_borrow_stress_annual_rate'])
    participation = float(settings['adv_participation_rate'])
    stress_participation = float(settings['stress_adv_participation_rate'])
    maximum_exit_days = float(settings['maximum_exit_days'])
    reference_nav = float(settings['reference_nav_usd'])

    period_rows: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    previous_end_weights: dict[str, float] | None = None
    previous_exit_date: str | None = None
    selected_schedule = [
        row for row in schedule
        if int(row['selected_nonoverlap_flag']) == 1
    ]
    for schedule_row in selected_schedule:
        as_of = str(schedule_row['asof_date'])
        scoped_rows: list[dict[str, Any]] = []
        for source in panel_by_date.get(as_of, []):
            if (
                candidate.scope_id != SECTOR_SCOPE
                and str(source['cohort_id']) != candidate.scope_id
            ):
                continue
            score, available, missing, eligible = _score_candidate(
                source,
                candidate,
                neutral_score=neutral,
                minimum_quality=minimum_quality,
                maximum_missing=maximum_missing,
            )
            if not eligible:
                continue
            row = dict(source)
            row['_candidate_score'] = score
            row['_candidate_available_weight'] = available
            row['_candidate_missing_weight'] = missing
            scoped_rows.append(row)
        if len(scoped_rows) < minimum_cross_section:
            continue
        weights = build_portfolio_weights(
            scoped_rows,
            spec,
            top_quantile=float(settings['top_quantile']),
            minimum_positions=int(settings['minimum_positions']),
        )
        if not weights:
            continue
        by_ticker = {str(row['ticker']): row for row in scoped_rows}
        gross_total_return = sum(
            weight * float(by_ticker[ticker]['_forward_total_return'])
            for ticker, weight in weights.items()
        )
        gross_xlp_relative = sum(
            weight * float(by_ticker[ticker]['_forward_xlp_residual_return'])
            for ticker, weight in weights.items()
        )
        equal_weight_total = statistics.fmean(
            float(row['_forward_total_return']) for row in scoped_rows
        )
        equal_weight_active = statistics.fmean(
            float(row['_forward_xlp_residual_return']) for row in scoped_rows
        )
        xlp_return = float(scoped_rows[0]['_xlp_return'])
        if any(
            not _close(row['_xlp_return'], xlp_return, 1e-11)
            for row in scoped_rows[1:]
        ):
            raise RuntimeError(f'XLP return differs within {as_of}.')

        current_gross = sum(abs(value) for value in weights.values())
        if previous_end_weights is None:
            entry_turnover = current_gross
            gap_liquidation_turnover = 0.0
            transition_kind = 'initial_entry'
        elif str(schedule_row['entry_date']) == previous_exit_date:
            tickers = set(previous_end_weights) | set(weights)
            entry_turnover = sum(
                abs(
                    weights.get(ticker, 0.0)
                    - previous_end_weights.get(ticker, 0.0)
                )
                for ticker in tickers
            )
            gap_liquidation_turnover = 0.0
            transition_kind = 'direct_rebalance_from_drifted_weights'
        elif str(schedule_row['entry_date']) > str(previous_exit_date):
            gap_liquidation_turnover = sum(
                abs(value) for value in previous_end_weights.values()
            )
            entry_turnover = current_gross
            transition_kind = 'liquidate_to_cash_then_reenter'
        else:
            raise RuntimeError('Non-overlap schedule produced overlapping positions.')
        trade_turnover = entry_turnover + gap_liquidation_turnover

        observed_borrow_cost = 0.0
        missing_borrow_stress_cost = 0.0
        observed_short_gross = 0.0
        missing_short_gross = 0.0
        capacities: list[float] = []
        stress_capacities: list[float] = []
        terminal_count = 0
        max_days_to_liquidate = 0.0
        for ticker, weight in weights.items():
            source = by_ticker[ticker]
            raw = source['_component_raw_values']
            borrow_rate = _annual_borrow_rate(raw.get('borrow_fee'))
            position_borrow = 0.0
            position_missing_stress = 0.0
            if weight < 0.0:
                if borrow_rate is None:
                    missing_short_gross += abs(weight)
                    position_missing_stress = (
                        abs(weight) * borrow_stress_rate * horizon / 252.0
                    )
                    missing_borrow_stress_cost += position_missing_stress
                else:
                    observed_short_gross += abs(weight)
                    position_borrow = (
                        abs(weight) * borrow_rate * horizon / 252.0
                    )
                    observed_borrow_cost += position_borrow
            adv = _finite(raw.get('avg_dollar_volume_63d'))
            capacity = _position_capacity(
                adv=adv,
                weight=weight,
                participation_rate=participation,
                maximum_exit_days=maximum_exit_days,
            )
            stress_capacity = _position_capacity(
                adv=adv,
                weight=weight,
                participation_rate=stress_participation,
                maximum_exit_days=maximum_exit_days,
            )
            if capacity is not None:
                capacities.append(capacity)
                max_days_to_liquidate = max(
                    max_days_to_liquidate,
                    abs(weight) * reference_nav / (adv * participation),
                )
            if stress_capacity is not None:
                stress_capacities.append(stress_capacity)
            terminal_used = int(source['_terminal_return_used_flag'])
            terminal_count += terminal_used
            holding = {
                'stage9_run_id': stage9_run_id,
                'candidate_id': candidate.candidate_id,
                'scope_id': candidate.scope_id,
                'candidate_kind': candidate.candidate_kind,
                'portfolio_name': spec.portfolio_name,
                'weight_method': spec.weight_method,
                'exposure_mode': spec.exposure_mode,
                'asof_date': as_of,
                'split_role': str(schedule_row['split_role']),
                'entry_date': str(schedule_row['entry_date']),
                'exit_date': str(schedule_row['exit_date']),
                'ticker': ticker,
                'cohort_id': str(source['cohort_id']),
                'side': 'long' if weight > 0 else 'short',
                'weight': weight,
                'candidate_score': float(source['_candidate_score']),
                'available_weight': float(source['_candidate_available_weight']),
                'missing_weight': float(source['_candidate_missing_weight']),
                'forward_total_return_21d': float(source['_forward_total_return']),
                'forward_xlp_residual_return_21d': float(
                    source['_forward_xlp_residual_return']
                ),
                'total_return_contribution': (
                    weight * float(source['_forward_total_return'])
                ),
                'xlp_relative_return_contribution': (
                    weight * float(source['_forward_xlp_residual_return'])
                ),
                'borrow_fee_annual_rate': (
                    '' if borrow_rate is None else borrow_rate
                ),
                'borrow_fee_available_flag': int(borrow_rate is not None),
                'observed_borrow_cost': position_borrow,
                'missing_borrow_stress_cost': position_missing_stress,
                'avg_dollar_volume_63d': '' if adv is None else adv,
                'position_capacity_usd': '' if capacity is None else capacity,
                'stress_position_capacity_usd': (
                    '' if stress_capacity is None else stress_capacity
                ),
                'terminal_event_status': str(source['terminal_event_status']),
                'terminal_return_used_flag': terminal_used,
                'source_panel_row_sha256': str(source['row_sha256']),
            }
            holding_rows.append(holding)

        max_cohort_share, single_name_hhi, maximum_name_share = _cohort_metrics(
            weights, by_ticker
        )
        gross_exposure = sum(abs(value) for value in weights.values())
        net_exposure = sum(weights.values())
        transaction_cost = trade_turnover * transaction_bps / 10000.0
        stress_borrow_cost = observed_borrow_cost + missing_borrow_stress_cost
        total_observed_cost = transaction_cost + observed_borrow_cost
        total_stress_cost = transaction_cost + stress_borrow_cost
        short_gross = observed_short_gross + missing_short_gross
        period = {
            'stage9_run_id': stage9_run_id,
            'candidate_id': candidate.candidate_id,
            'scope_id': candidate.scope_id,
            'candidate_kind': candidate.candidate_kind,
            'portfolio_name': spec.portfolio_name,
            'weight_method': spec.weight_method,
            'exposure_mode': spec.exposure_mode,
            'asof_date': as_of,
            'split_role': str(schedule_row['split_role']),
            'entry_date': str(schedule_row['entry_date']),
            'exit_date': str(schedule_row['exit_date']),
            'transition_kind': transition_kind,
            'cross_section_count': len(scoped_rows),
            'position_count': len(weights),
            'long_count': sum(value > 0 for value in weights.values()),
            'short_count': sum(value < 0 for value in weights.values()),
            'gross_exposure': gross_exposure,
            'net_exposure': net_exposure,
            'gross_total_return': gross_total_return,
            'gross_xlp_relative_return': gross_xlp_relative,
            'xlp_return': xlp_return,
            'equal_weight_scope_total_return': equal_weight_total,
            'equal_weight_scope_xlp_relative_return': equal_weight_active,
            'entry_rebalance_turnover': entry_turnover,
            'gap_liquidation_turnover': gap_liquidation_turnover,
            'final_liquidation_turnover': 0.0,
            'trade_notional_turnover': trade_turnover,
            'two_sided_turnover': 0.5 * trade_turnover,
            'transaction_cost': transaction_cost,
            'observed_borrow_cost': observed_borrow_cost,
            'missing_borrow_stress_cost': missing_borrow_stress_cost,
            'stress_borrow_cost': stress_borrow_cost,
            'total_observed_cost': total_observed_cost,
            'total_stress_cost': total_stress_cost,
            'net_total_return_observed_cost': (
                gross_total_return - total_observed_cost
            ),
            'net_xlp_relative_return_observed_cost': (
                gross_xlp_relative - total_observed_cost
            ),
            'net_total_return_stress_cost': (
                gross_total_return - total_stress_cost
            ),
            'net_xlp_relative_return_stress_cost': (
                gross_xlp_relative - total_stress_cost
            ),
            'observed_short_gross': observed_short_gross,
            'missing_borrow_short_gross': missing_short_gross,
            'borrow_fee_coverage_fraction': (
                observed_short_gross / short_gross if short_gross > 0 else 1.0
            ),
            'portfolio_capacity_usd': min(capacities) if capacities else '',
            'stress_portfolio_capacity_usd': (
                min(stress_capacities) if stress_capacities else ''
            ),
            'reference_nav_capacity_pass_flag': int(
                bool(capacities) and min(capacities) >= reference_nav
            ),
            'stress_reference_nav_capacity_pass_flag': int(
                bool(stress_capacities)
                and min(stress_capacities) >= reference_nav
            ),
            'maximum_days_to_liquidate_reference_nav': max_days_to_liquidate,
            'max_cohort_gross_share': max_cohort_share,
            'single_name_gross_hhi': single_name_hhi,
            'maximum_single_name_gross_share': maximum_name_share,
            'terminal_return_position_count': terminal_count,
        }
        end_weights = _drifted_end_weights(
            weights, by_ticker, gross_total_return
        )
        period['_end_weights'] = end_weights
        period_rows.append(period)
        previous_end_weights = end_weights
        previous_exit_date = str(schedule_row['exit_date'])

    if period_rows:
        last = period_rows[-1]
        final_turnover = sum(abs(value) for value in last['_end_weights'].values())
        last['final_liquidation_turnover'] = final_turnover
        last['trade_notional_turnover'] += final_turnover
        last['two_sided_turnover'] = 0.5 * last['trade_notional_turnover']
        last['transaction_cost'] = (
            last['trade_notional_turnover'] * transaction_bps / 10000.0
        )
        last['total_observed_cost'] = (
            last['transaction_cost'] + last['observed_borrow_cost']
        )
        last['total_stress_cost'] = (
            last['transaction_cost'] + last['stress_borrow_cost']
        )
        for suffix, gross_field in (
            ('total_return', 'gross_total_return'),
            ('xlp_relative_return', 'gross_xlp_relative_return'),
        ):
            last[f'net_{suffix}_observed_cost'] = (
                last[gross_field] - last['total_observed_cost']
            )
            last[f'net_{suffix}_stress_cost'] = (
                last[gross_field] - last['total_stress_cost']
            )

    for row in period_rows:
        row.pop('_end_weights', None)
        row['period_row_sha256'] = _sha256(row)
    for row in holding_rows:
        row['holding_row_sha256'] = _sha256(row)
    return period_rows, holding_rows


def _max_drawdown(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    maximum = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        maximum = min(maximum, equity / peak - 1.0)
    return maximum


def _nearest_percentile(values: Sequence[float], fraction: float) -> float | str:
    if not values:
        return ''
    ordered = sorted(values)
    index = int(math.floor(fraction * (len(ordered) - 1)))
    return ordered[index]


def _return_metrics(
    periods: Sequence[Mapping[str, Any]],
    schedule: Sequence[Mapping[str, Any]],
    *,
    return_basis: str,
) -> dict[str, Any]:
    if return_basis == 'total_return':
        observed_field = 'net_total_return_observed_cost'
        stress_field = 'net_total_return_stress_cost'
    elif return_basis == 'xlp_relative':
        observed_field = 'net_xlp_relative_return_observed_cost'
        stress_field = 'net_xlp_relative_return_stress_cost'
    else:
        raise ValueError(f'Unknown return basis: {return_basis}')
    by_date = {str(row['asof_date']): row for row in periods}
    all_dates = [str(row['asof_date']) for row in schedule]
    observed = [
        float(by_date[value][observed_field]) if value in by_date else 0.0
        for value in all_dates
    ]
    stress = [
        float(by_date[value][stress_field]) if value in by_date else 0.0
        for value in all_dates
    ]
    slots = len(all_dates)
    if slots == 0:
        raise RuntimeError('Stage 9 summary has no calendar slots.')

    def statistics_for(values: Sequence[float]) -> dict[str, float | str]:
        total = math.prod(1.0 + value for value in values) - 1.0
        annualized = (
            (1.0 + total) ** (12.0 / slots) - 1.0
            if total > -1.0 else -1.0
        )
        volatility = (
            statistics.stdev(values) * math.sqrt(12.0)
            if len(values) > 1 else 0.0
        )
        mean = statistics.fmean(values)
        return {
            'total_return': total,
            'annualized_return': annualized,
            'annualized_volatility': volatility,
            'risk_adjusted_return': (
                mean * 12.0 / volatility if volatility > 0 else ''
            ),
            'maximum_drawdown': _max_drawdown(values),
        }

    observed_stats = statistics_for(observed)
    stress_stats = statistics_for(stress)
    capacities = [
        float(row['portfolio_capacity_usd'])
        for row in periods
        if row.get('portfolio_capacity_usd') not in ('', None)
    ]
    stress_capacities = [
        float(row['stress_portfolio_capacity_usd'])
        for row in periods
        if row.get('stress_portfolio_capacity_usd') not in ('', None)
    ]
    split_counts: dict[str, int] = defaultdict(int)
    for row in periods:
        split_counts[str(row['split_role'])] += 1
    invested_returns = [float(row[observed_field]) for row in periods]
    return {
        'return_basis': return_basis,
        'calendar_slot_count': slots,
        'invested_period_count': len(periods),
        'cash_slot_count': slots - len(periods),
        'invested_fraction': len(periods) / slots,
        'observed_total_return': observed_stats['total_return'],
        'observed_annualized_return': observed_stats['annualized_return'],
        'observed_annualized_volatility': observed_stats['annualized_volatility'],
        'observed_risk_adjusted_return': observed_stats['risk_adjusted_return'],
        'observed_maximum_drawdown': observed_stats['maximum_drawdown'],
        'stress_total_return': stress_stats['total_return'],
        'stress_annualized_return': stress_stats['annualized_return'],
        'stress_annualized_volatility': stress_stats['annualized_volatility'],
        'stress_risk_adjusted_return': stress_stats['risk_adjusted_return'],
        'stress_maximum_drawdown': stress_stats['maximum_drawdown'],
        'invested_period_hit_rate': (
            sum(value > 0 for value in invested_returns) / len(invested_returns)
            if invested_returns else 0.0
        ),
        'average_trade_notional_turnover': (
            statistics.fmean(
                float(row['trade_notional_turnover']) for row in periods
            ) if periods else 0.0
        ),
        'total_transaction_cost': sum(
            float(row['transaction_cost']) for row in periods
        ),
        'total_observed_borrow_cost': sum(
            float(row['observed_borrow_cost']) for row in periods
        ),
        'total_missing_borrow_stress_cost': sum(
            float(row['missing_borrow_stress_cost']) for row in periods
        ),
        'average_borrow_fee_coverage_fraction': (
            statistics.fmean(
                float(row['borrow_fee_coverage_fraction']) for row in periods
            ) if periods else 0.0
        ),
        'minimum_portfolio_capacity_usd': min(capacities) if capacities else '',
        'p05_portfolio_capacity_usd': _nearest_percentile(capacities, 0.05),
        'median_portfolio_capacity_usd': (
            statistics.median(capacities) if capacities else ''
        ),
        'minimum_stress_portfolio_capacity_usd': (
            min(stress_capacities) if stress_capacities else ''
        ),
        'reference_nav_capacity_pass_fraction': (
            statistics.fmean(
                int(row['reference_nav_capacity_pass_flag']) for row in periods
            ) if periods else 0.0
        ),
        'stress_reference_nav_capacity_pass_fraction': (
            statistics.fmean(
                int(row['stress_reference_nav_capacity_pass_flag'])
                for row in periods
            ) if periods else 0.0
        ),
        'maximum_days_to_liquidate_reference_nav': max(
            (
                float(row['maximum_days_to_liquidate_reference_nav'])
                for row in periods
            ),
            default=0.0,
        ),
        'average_max_cohort_gross_share': (
            statistics.fmean(
                float(row['max_cohort_gross_share']) for row in periods
            ) if periods else 0.0
        ),
        'maximum_cohort_gross_share': max(
            (float(row['max_cohort_gross_share']) for row in periods),
            default=0.0,
        ),
        'maximum_single_name_gross_share': max(
            (
                float(row['maximum_single_name_gross_share'])
                for row in periods
            ),
            default=0.0,
        ),
        'terminal_return_position_count': sum(
            int(row['terminal_return_position_count']) for row in periods
        ),
        'train_period_count': split_counts['train'],
        'embargo_period_count': (
            split_counts['embargo_1'] + split_counts['embargo_2']
        ),
        'validation_period_count': split_counts['validation'],
        'holdout_period_count': split_counts['holdout'],
    }


def _summary_rows(
    periods: Sequence[Mapping[str, Any]],
    schedule: Sequence[Mapping[str, Any]],
    candidates: Sequence[Candidate],
    specs: Sequence[PortfolioSpec],
    *,
    stage9_run_id: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = (
        defaultdict(list)
    )
    for row in periods:
        grouped[(
            str(row['candidate_id']), str(row['portfolio_name']),
            str(row['weight_method']), str(row['exposure_mode']),
        )].append(row)
    output: list[dict[str, Any]] = []
    for candidate in candidates:
        for spec in specs:
            key = (
                candidate.candidate_id, spec.portfolio_name,
                spec.weight_method, spec.exposure_mode,
            )
            values = sorted(
                grouped.get(key, []), key=lambda row: str(row['asof_date'])
            )
            for basis in ('total_return', 'xlp_relative'):
                output.append({
                    'stage9_run_id': stage9_run_id,
                    'candidate_id': candidate.candidate_id,
                    'scope_id': candidate.scope_id,
                    'candidate_kind': candidate.candidate_kind,
                    'portfolio_name': spec.portfolio_name,
                    'weight_method': spec.weight_method,
                    'exposure_mode': spec.exposure_mode,
                    'oos_score_valid_flag': 0,
                    'promotion_eligible_flag': 0,
                    **_return_metrics(values, schedule, return_basis=basis),
                })
    baseline_by_scope = {
        candidate.scope_id: candidate.candidate_id
        for candidate in candidates
        if candidate.candidate_kind == 'stage7_core_baseline'
    }
    by_key = {
        (
            str(row['candidate_id']), str(row['portfolio_name']),
            str(row['weight_method']), str(row['exposure_mode']),
            str(row['return_basis']),
        ): row
        for row in output
    }
    for row in output:
        baseline_id = baseline_by_scope[str(row['scope_id'])]
        baseline = by_key[(
            baseline_id, str(row['portfolio_name']),
            str(row['weight_method']), str(row['exposure_mode']),
            str(row['return_basis']),
        )]
        row['stage7_reference_candidate_id'] = baseline_id
        row['observed_annualized_return_delta_vs_stage7'] = (
            float(row['observed_annualized_return'])
            - float(baseline['observed_annualized_return'])
        )
        row['stress_annualized_return_delta_vs_stage7'] = (
            float(row['stress_annualized_return'])
            - float(baseline['stress_annualized_return'])
        )
        row['summary_row_sha256'] = _sha256(row)
    output.sort(key=lambda row: (
        str(row['scope_id']), str(row['candidate_id']),
        str(row['portfolio_name']), str(row['weight_method']),
        str(row['return_basis']),
    ))
    return output


def _source_tieout_rows(
    *,
    stage8_contract: Mapping[str, Any],
    stage8_manifest: Mapping[str, Any],
    registry: Mapping[str, Any],
    split: Mapping[str, Any],
    stage6c_run: Mapping[str, Any],
    panel_row_count: int,
    terminal_row_count: int,
) -> list[dict[str, Any]]:
    values = [
        (
            'stage8_historical_panel', STAGE8_PANEL_FILE,
            'stage8_artifact_manifest',
            (
                stage8_manifest.get('artifacts', {})
                .get(STAGE8_PANEL_FILE, {})
                .get('sha256')
            ),
            stage8_contract.get('stage6c_asof_date'), 'fact_source_reported',
            panel_row_count,
        ),
        (
            'stage8_candidate_registry', STAGE8_CANDIDATE_FILE,
            'stage8_candidate_registry', registry.get('registry_sha256'),
            stage8_contract.get('stage6c_asof_date'), 'fact_source_reported',
            registry.get('candidate_count'),
        ),
        (
            'stage8_chronological_split', STAGE8_SPLIT_FILE,
            'stage8_split_manifest', split.get('split_sha256'),
            stage8_contract.get('stage6c_asof_date'), 'fact_source_reported',
            len(_split_roles(split)),
        ),
        (
            'stage6c_total_return_labels', 'stage6c_specialized_factor_panel',
            'sealed_rehearsal_database', stage6c_run.get('panel_sha256'),
            stage6c_run.get('asof_date'), 'fact_source_reported', panel_row_count,
        ),
        (
            'xlp_residual_reconciliation',
            'forward_total_return_21d-forward_xlp_residual_return_21d',
            'sealed_stage6c_and_stage8_labels',
            stage8_contract.get('contract_sha256'),
            stage6c_run.get('asof_date'), 'derived_calculation', panel_row_count,
        ),
        (
            'terminal_value_consumption', 'terminal_event_status',
            'sealed_stage6c_terminal_policy',
            stage6c_run.get('metric_policy_sha256'),
            stage6c_run.get('asof_date'), 'derived_calculation', terminal_row_count,
        ),
    ]
    output: list[dict[str, Any]] = []
    for item, location, source, source_hash, as_of, label, count in values:
        if (
            not isinstance(source_hash, str)
            or len(source_hash) != 64
            or any(character not in '0123456789abcdef' for character in source_hash)
        ):
            raise RuntimeError(
                f'Stage 9 source tieout requires a SHA-256 for {item}; '
                f'got {source_hash!r}.'
            )
        row = {
            'output_or_driver': item,
            'model_location': location,
            'source_name': source,
            'source_sha256': source_hash,
            'tie_status': 'ties',
            'evidence_label': label,
            'as_of_date': as_of,
            'row_count': count,
            'decision_impact': 'high',
        }
        row['tieout_row_sha256'] = _sha256(row)
        output.append(row)
    return output


def run_stage9_backtest(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    stage8_root: Path,
    factor_root: Path,
    output_dir: Path,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    root = stage8_root.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    upstream_validation = validate_stage8_artifacts(
        conn, bundle, output_dir=root, factor_root=factor_root
    )
    if upstream_validation['status'] != 'PASS':
        upstream_errors = upstream_validation['errors']
        raise RuntimeError(f'Stage 9 rejected Stage 8 artifacts: {upstream_errors}')
    stage8_contract = _read_json(root / STAGE8_CONTRACT_FILE)
    stage8_manifest = _read_json(root / STAGE8_MANIFEST_FILE)
    registry = _read_json(root / STAGE8_CANDIDATE_FILE)
    split = _read_json(root / STAGE8_SPLIT_FILE)
    if (
        registry.get('registered_before_label_evaluation') is not True
        or registry.get('production_promotion_enabled') is not False
        or registry.get('portfolio_write_enabled') is not False
    ):
        raise RuntimeError(
            'Stage 8 candidate registry is not report-only preregistered.'
        )
    raw_candidates = registry.get('candidates')
    if not isinstance(raw_candidates, list) or len(raw_candidates) != int(
        registry.get('candidate_count') or 0
    ):
        raise RuntimeError('Stage 8 candidate registry count mismatch.')
    candidates = [_candidate(row) for row in raw_candidates]
    if len({value.candidate_id for value in candidates}) != len(candidates):
        raise RuntimeError('Duplicate Stage 8 candidate IDs.')

    stage6c_run_id = int(stage8_contract['stage6c_run_id'])
    stage6c_source = conn.execute(
        'SELECT * FROM stage6c_panel_run WHERE stage6c_run_id=?',
        (stage6c_run_id,),
    ).fetchone()
    if stage6c_source is None:
        raise RuntimeError(f'Unknown Stage 6C run: {stage6c_run_id}')
    stage6c_run = dict(stage6c_source)
    if (
        str(stage6c_run['status']) != 'complete'
        or str(stage6c_run['panel_sha256'])
        != str(stage8_contract['stage6c_panel_sha256'])
    ):
        raise RuntimeError('Stage 6C run no longer matches Stage 8 contract.')
    panel = _load_panel(root / STAGE8_PANEL_FILE)
    labels = _load_stage6c_labels(conn, stage6c_run_id=stage6c_run_id)
    if set(labels) != {
        (str(row['asof_date']), str(row['ticker'])) for row in panel
    }:
        raise RuntimeError('Stage 6C and Stage 8 panel date/ticker keys differ.')
    selected_sources = _selected_price_sources(conn)
    calendar = _trading_calendar(conn, selected_sources=selected_sources)
    settings = stage9_config_payload(bundle)
    schedule = build_nonoverlap_schedule(
        split,
        calendar,
        entry_lag=int(stage6c_run['entry_lag_trading_days']),
        horizon_days=int(settings['evaluation_horizon_days']),
    )
    price_presence = _terminal_price_presence(conn, schedule=schedule)
    panel, xlp_by_date = _enrich_panel(
        panel,
        labels,
        schedule,
        price_presence,
        horizon_days=int(settings['evaluation_horizon_days']),
    )
    panel_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in panel:
        panel_by_date[str(row['asof_date'])].append(row)

    methodology = {
        'config.yaml': _file_sha256(bundle.path),
        STAGE9_POLICY_FILE: _file_sha256(
            bundle.base_dir / 'data' / STAGE9_POLICY_FILE
        ),
        'stage8_calibration.py': _file_sha256(
            Path(__file__).with_name('stage8_calibration.py')
        ),
        'stage9_backtest.py': _file_sha256(Path(__file__)),
    }
    contract_payload = {
        'schema_version': STAGE9_VERSION,
        'model_family': 'consumer_defensive',
        'mode': 'report_only',
        'sample_role': 'deep_replay_research',
        'decision_use': 'research_reporting_only_no_portfolio_action',
        'database_access_mode': 'read_only',
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'oos_score_valid_flag': 0,
        'stage9_config': settings,
        'stage8_root': str(root),
        'stage8_contract_sha256': stage8_contract['contract_sha256'],
        'stage8_manifest_sha256': stage8_manifest['manifest_sha256'],
        'stage8_panel_sha256': split['panel_sha256'],
        'stage8_split_sha256': split['split_sha256'],
        'stage8_candidate_registry_sha256': registry['registry_sha256'],
        'stage6c_run_id': stage6c_run_id,
        'stage6c_panel_sha256': stage6c_run['panel_sha256'],
        'stage7_source_id': stage8_contract['stage7_source_id'],
        'stage7_contract_sha256': stage8_contract['stage7_contract_sha256'],
        'candidate_count': len(candidates),
        'portfolio_spec_count': len(_portfolio_specs(bundle)),
        'calendar_slot_count': len(schedule),
        'selected_nonoverlap_date_count': sum(
            int(row['selected_nonoverlap_flag']) for row in schedule
        ),
        'nonoverlap_return_policy': (
            'fixed_21_session_stage6c_labels; earliest-start greedy windows; '
            'unselected calendar slots treated as cash for annualization'
        ),
        'transaction_cost_policy': (
            'one-way basis points times gross traded notional; initial entry, '
            'gap liquidation/re-entry, drifted direct rebalance and final '
            'liquidation are charged'
        ),
        'borrow_cost_policy': (
            'observed annualized borrow fee on short weights; separately '
            'reported stress rate for unavailable fees'
        ),
        'capacity_policy': (
            'minimum position capacity from 63-day ADV, participation rate, '
            'maximum exit days and absolute target weight'
        ),
        'candidate_creation_after_results_prohibited': True,
        'methodology_file_sha256s': methodology,
    }
    contract_sha256 = _sha256(contract_payload)
    stage9_run_id = f'cds9_{contract_sha256[:24]}'
    contract = {
        **contract_payload,
        'stage9_run_id': stage9_run_id,
        'contract_sha256': contract_sha256,
    }

    specs = _portfolio_specs(bundle)
    all_periods: list[dict[str, Any]] = []
    all_holdings: list[dict[str, Any]] = []
    for position, candidate in enumerate(candidates, start=1):
        for spec in specs:
            periods, holdings = _simulate(
                panel_by_date,
                schedule,
                candidate,
                spec,
                bundle,
                stage9_run_id=stage9_run_id,
            )
            all_periods.extend(periods)
            all_holdings.extend(holdings)
        if progress is not None:
            progress(position, len(candidates), candidate.candidate_id)
    all_periods.sort(key=lambda row: (
        str(row['candidate_id']), str(row['portfolio_name']),
        str(row['weight_method']), str(row['asof_date']),
    ))
    all_holdings.sort(key=lambda row: (
        str(row['candidate_id']), str(row['portfolio_name']),
        str(row['weight_method']), str(row['asof_date']),
        -abs(float(row['weight'])), str(row['ticker']),
    ))
    summary = _summary_rows(
        all_periods,
        schedule,
        candidates,
        specs,
        stage9_run_id=stage9_run_id,
    )
    terminal_rows = sum(
        int(row['_terminal_return_used_flag']) for row in panel
    )
    tieout = _source_tieout_rows(
        stage8_contract=stage8_contract,
        stage8_manifest=stage8_manifest,
        registry=registry,
        split=split,
        stage6c_run=stage6c_run,
        panel_row_count=len(panel),
        terminal_row_count=terminal_rows,
    )
    decision_payload = {
        'schema_version': 'consumer_defensive_stage9_decision_v1',
        'stage9_run_id': stage9_run_id,
        'contract_sha256': contract_sha256,
        'generation_status': 'complete',
        'independent_validation_required': True,
        'decision_readiness': 'ready_with_caveats',
        'permitted_use': 'report_only_research_and_stage10_publishing_input',
        'prohibited_use': 'portfolio_action_weight_promotion_or_oos_claim',
        'production_weight_decision': 'retain_frozen_stage7_core_baseline',
        'stage8_candidate_promotion_count': 0,
        'stage10_scoring_source': stage8_contract['stage7_source_id'],
        'candidate_creation_after_results_prohibited': True,
        'portfolio_write_enabled': False,
        'production_promotion_enabled': False,
    }
    decision = {
        **decision_payload,
        'decision_sha256': _sha256(decision_payload),
    }

    output.mkdir(parents=True, exist_ok=True)
    _immutable_json(output / CONTRACT_FILE, contract)
    _immutable_text(output / SCHEDULE_FILE, _csv_text(schedule))
    _immutable_text(output / SUMMARY_FILE, _csv_text(summary))
    _immutable_csv_gzip(output / PERIOD_FILE, all_periods)
    _immutable_csv_gzip(output / HOLDING_FILE, all_holdings)
    _immutable_text(output / TIEOUT_FILE, _csv_text(tieout))
    _immutable_json(output / DECISION_FILE, decision)

    artifact_names = (
        CONTRACT_FILE, SCHEDULE_FILE, SUMMARY_FILE, PERIOD_FILE,
        HOLDING_FILE, TIEOUT_FILE, DECISION_FILE,
    )
    manifest_payload = {
        'schema_version': 'consumer_defensive_stage9_artifact_manifest_v1',
        'stage9_run_id': stage9_run_id,
        'contract_sha256': contract_sha256,
        'file_sha256s': {
            name: _file_sha256(output / name) for name in artifact_names
        },
        'logical_sha256s': {
            'schedule': _sha256([
                row['schedule_row_sha256'] for row in schedule
            ]),
            'summary': _sha256([
                row['summary_row_sha256'] for row in summary
            ]),
            'periods': _sha256([
                row['period_row_sha256'] for row in all_periods
            ]),
            'holdings': _sha256([
                row['holding_row_sha256'] for row in all_holdings
            ]),
            'source_tieout': _sha256([
                row['tieout_row_sha256'] for row in tieout
            ]),
        },
        'row_counts': {
            'schedule': len(schedule),
            'summary': len(summary),
            'periods': len(all_periods),
            'holdings': len(all_holdings),
            'source_tieout': len(tieout),
        },
        'candidate_count': len(candidates),
        'portfolio_spec_count': len(specs),
        'return_basis_count': 2,
        'calendar_slot_count': len(schedule),
        'selected_nonoverlap_date_count': sum(
            int(row['selected_nonoverlap_flag']) for row in schedule
        ),
        'xlp_reconciled_date_count': len(xlp_by_date),
        'terminal_21d_panel_row_count': terminal_rows,
        'database_write_count': 0,
        'portfolio_write_enabled': False,
        'production_promotion_enabled': False,
    }
    manifest = {
        **manifest_payload,
        'manifest_sha256': _sha256(manifest_payload),
    }
    _immutable_json(output / MANIFEST_FILE, manifest)
    return {
        'status': 'PASS',
        'stage9_run_id': stage9_run_id,
        'contract_sha256': contract_sha256,
        'candidate_count': len(candidates),
        'portfolio_spec_count': len(specs),
        'summary_row_count': len(summary),
        'period_row_count': len(all_periods),
        'holding_row_count': len(all_holdings),
        'calendar_slot_count': len(schedule),
        'selected_nonoverlap_date_count': manifest[
            'selected_nonoverlap_date_count'
        ],
        'output_dir': str(output),
        'manifest_sha256': manifest['manifest_sha256'],
    }


def _numeric_row(row: Mapping[str, str]) -> dict[str, Any]:
    output: dict[str, Any] = dict(row)
    for key, value in row.items():
        if value == '':
            continue
        if key.endswith('_count') or key.endswith('_flag'):
            try:
                output[key] = int(value)
                continue
            except ValueError:
                pass
        parsed = _finite(value)
        if parsed is not None:
            output[key] = parsed
    return output


def _check_row_hashes(
    rows: Sequence[Mapping[str, Any]],
    hash_field: str,
) -> bool:
    for source in rows:
        row = dict(source)
        observed = str(row.pop(hash_field, ''))
        if observed != _sha256(row):
            return False
    return True


def _validation_header(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    stage8_root: Path,
    factor_root: Path,
    output_dir: Path,
) -> tuple[dict[str, Any], list[str], list[tuple[str, bool]], dict[str, Any]]:
    root = output_dir.expanduser().resolve()
    required = {
        CONTRACT_FILE, SCHEDULE_FILE, SUMMARY_FILE, PERIOD_FILE,
        HOLDING_FILE, TIEOUT_FILE, DECISION_FILE, MANIFEST_FILE,
    }
    missing = sorted(
        name for name in required
        if not (root / name).is_file() or (root / name).is_symlink()
    )
    if missing:
        return {}, [f'missing_or_unsafe_artifacts:{missing}'], [], {}
    upstream = validate_stage8_artifacts(
        conn,
        bundle,
        output_dir=stage8_root.expanduser().resolve(),
        factor_root=factor_root,
    )
    if upstream['status'] != 'PASS':
        upstream_errors = upstream['errors']
        return {}, [f'stage8_validation_failed:{upstream_errors}'], [], {}
    contract = _read_json(root / CONTRACT_FILE)
    manifest = _read_json(root / MANIFEST_FILE)
    decision = _read_json(root / DECISION_FILE)
    schedule_raw = _read_csv(root / SCHEDULE_FILE)
    summary_raw = _read_csv(root / SUMMARY_FILE)
    periods_raw = _read_csv(root / PERIOD_FILE)
    holdings_raw = _read_csv(root / HOLDING_FILE)
    tieout_raw = _read_csv(root / TIEOUT_FILE)
    data = {
        'root': root,
        'contract': contract,
        'manifest': manifest,
        'decision': decision,
        'schedule_raw': schedule_raw,
        'summary_raw': summary_raw,
        'periods_raw': periods_raw,
        'holdings_raw': holdings_raw,
        'tieout_raw': tieout_raw,
        'schedule': [_numeric_row(row) for row in schedule_raw],
        'summary': [_numeric_row(row) for row in summary_raw],
        'periods': [_numeric_row(row) for row in periods_raw],
        'holdings': [_numeric_row(row) for row in holdings_raw],
    }
    errors: list[str] = []
    checks: list[tuple[str, bool]] = []

    def check(name: str, condition: bool) -> None:
        checks.append((name, bool(condition)))
        if not condition:
            errors.append(name)

    manifest_payload = dict(manifest)
    observed_manifest_hash = str(manifest_payload.pop('manifest_sha256', ''))
    check('manifest_self_hash', observed_manifest_hash == _sha256(manifest_payload))
    contract_payload = {
        key: value for key, value in contract.items()
        if key not in {'stage9_run_id', 'contract_sha256'}
    }
    check(
        'contract_self_hash',
        str(contract.get('contract_sha256')) == _sha256(contract_payload),
    )
    check(
        'contract_config_exact',
        contract.get('stage9_config') == stage9_config_payload(bundle),
    )
    current_methodology = {
        'config.yaml': _file_sha256(bundle.path),
        STAGE9_POLICY_FILE: _file_sha256(
            bundle.base_dir / 'data' / STAGE9_POLICY_FILE
        ),
        'stage8_calibration.py': _file_sha256(
            Path(__file__).with_name('stage8_calibration.py')
        ),
        'stage9_backtest.py': _file_sha256(Path(__file__)),
    }
    check(
        'methodology_files_unchanged',
        contract.get('methodology_file_sha256s') == current_methodology,
    )
    expected_run_id = 'cds9_' + str(contract.get('contract_sha256', ''))[:24]
    check('contract_run_id_exact', contract.get('stage9_run_id') == expected_run_id)
    decision_payload = dict(decision)
    observed_decision_hash = str(decision_payload.pop('decision_sha256', ''))
    check('decision_self_hash', observed_decision_hash == _sha256(decision_payload))
    check('decision_contract_binding', (
        decision.get('stage9_run_id') == contract.get('stage9_run_id')
        and decision.get('contract_sha256') == contract.get('contract_sha256')
    ))
    check('manifest_contract_binding', (
        manifest.get('stage9_run_id') == contract.get('stage9_run_id')
        and manifest.get('contract_sha256') == contract.get('contract_sha256')
    ))
    check('contract_report_only', (
        contract.get('mode') == 'report_only'
        and contract.get('portfolio_write_enabled') is False
        and contract.get('production_promotion_enabled') is False
        and int(contract.get('oos_score_valid_flag') or 0) == 0
    ))
    check('decision_retains_stage7', (
        decision.get('production_weight_decision')
        == 'retain_frozen_stage7_core_baseline'
        and decision.get('portfolio_write_enabled') is False
        and decision.get('production_promotion_enabled') is False
    ))
    expected_artifacts = {
        CONTRACT_FILE, SCHEDULE_FILE, SUMMARY_FILE, PERIOD_FILE,
        HOLDING_FILE, TIEOUT_FILE, DECISION_FILE,
    }
    artifact_hashes = manifest.get('file_sha256s', {})
    check('artifact_file_hash_census', set(artifact_hashes) == expected_artifacts)
    check('artifact_file_hashes', (
        set(artifact_hashes) == expected_artifacts
        and all(
            _file_sha256(root / name) == expected
            for name, expected in artifact_hashes.items()
        )
    ))
    check('artifact_row_counts', manifest.get('row_counts') == {
        'schedule': len(data['schedule']),
        'summary': len(data['summary']),
        'periods': len(data['periods']),
        'holdings': len(data['holdings']),
        'source_tieout': len(tieout_raw),
    })
    check(
        'schedule_row_hashes',
        _check_row_hashes(data['schedule'], 'schedule_row_sha256'),
    )
    check(
        'summary_row_hashes',
        _check_row_hashes(data['summary'], 'summary_row_sha256'),
    )
    check(
        'period_row_hashes',
        _check_row_hashes(data['periods'], 'period_row_sha256'),
    )
    check(
        'holding_row_hashes',
        _check_row_hashes(data['holdings'], 'holding_row_sha256'),
    )
    check(
        'tieout_row_hashes',
        _check_row_hashes(
            [_numeric_row(row) for row in tieout_raw], 'tieout_row_sha256'
        ),
    )
    check('logical_hashes', manifest.get('logical_sha256s') == {
        'schedule': _sha256([
            row['schedule_row_sha256'] for row in schedule_raw
        ]),
        'summary': _sha256([
            row['summary_row_sha256'] for row in summary_raw
        ]),
        'periods': _sha256([
            row['period_row_sha256'] for row in periods_raw
        ]),
        'holdings': _sha256([
            row['holding_row_sha256'] for row in holdings_raw
        ]),
        'source_tieout': _sha256([
            row['tieout_row_sha256'] for row in tieout_raw
        ]),
    })
    return data, errors, checks, {'check': check}


def validate_stage9_artifacts(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    stage8_root: Path,
    factor_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    data, errors, checks, helpers = _validation_header(
        conn,
        bundle,
        stage8_root=stage8_root,
        factor_root=factor_root,
        output_dir=output_dir,
    )
    if not data:
        return {
            'status': 'FAIL',
            'errors': errors,
            'check_count': max(1, len(checks)),
            'passed_check_count': 0,
        }
    check = helpers['check']
    contract = data['contract']
    manifest = data['manifest']
    schedule = data['schedule']
    summary = data['summary']
    periods = data['periods']
    holdings = data['holdings']
    stage8 = stage8_root.expanduser().resolve()
    stage8_contract = _read_json(stage8 / STAGE8_CONTRACT_FILE)
    stage8_manifest = _read_json(stage8 / STAGE8_MANIFEST_FILE)
    registry = _read_json(stage8 / STAGE8_CANDIDATE_FILE)
    split = _read_json(stage8 / STAGE8_SPLIT_FILE)
    candidates = [_candidate(row) for row in registry['candidates']]
    candidate_lookup = {
        candidate.candidate_id: candidate for candidate in candidates
    }
    stage6c_run_id = int(contract['stage6c_run_id'])
    selected_sources = _selected_price_sources(conn)
    expected_schedule = build_nonoverlap_schedule(
        split,
        _trading_calendar(conn, selected_sources=selected_sources),
        entry_lag=int(conn.execute(
            'SELECT entry_lag_trading_days FROM stage6c_panel_run '
            'WHERE stage6c_run_id=?',
            (stage6c_run_id,),
        ).fetchone()[0]),
        horizon_days=int(stage9_config_payload(bundle)['evaluation_horizon_days']),
    )
    check('nonoverlap_schedule_exact', schedule == expected_schedule)
    check('candidate_census_exact', (
        int(manifest['candidate_count']) == len(candidates)
        and {str(row['candidate_id']) for row in summary}
        == set(candidate_lookup)
    ))
    check('summary_variant_census', len(summary) == len(candidates) * 4 * 2)

    panel = _load_panel(stage8 / STAGE8_PANEL_FILE)
    labels = _load_stage6c_labels(conn, stage6c_run_id=stage6c_run_id)
    panel, _xlp_by_date = _enrich_panel(
        panel,
        labels,
        expected_schedule,
        _terminal_price_presence(conn, schedule=expected_schedule),
        horizon_days=int(stage9_config_payload(bundle)['evaluation_horizon_days']),
    )
    stage6c_source = conn.execute(
        'SELECT * FROM stage6c_panel_run WHERE stage6c_run_id=?',
        (stage6c_run_id,),
    ).fetchone()
    if stage6c_source is None:
        raise RuntimeError(f'Unknown Stage 6C run: {stage6c_run_id}')
    stage6c_run = dict(stage6c_source)
    check('upstream_contract_bindings_exact', all((
        contract.get('stage8_contract_sha256')
        == stage8_contract.get('contract_sha256'),
        contract.get('stage8_manifest_sha256')
        == stage8_manifest.get('manifest_sha256'),
        contract.get('stage8_panel_sha256') == split.get('panel_sha256'),
        contract.get('stage8_split_sha256') == split.get('split_sha256'),
        contract.get('stage8_candidate_registry_sha256')
        == registry.get('registry_sha256'),
        contract.get('stage6c_panel_sha256') == stage6c_run.get('panel_sha256'),
        contract.get('stage7_source_id') == stage8_contract.get('stage7_source_id'),
        contract.get('stage7_contract_sha256')
        == stage8_contract.get('stage7_contract_sha256'),
    )))
    expected_tieout = _source_tieout_rows(
        stage8_contract=stage8_contract,
        stage8_manifest=stage8_manifest,
        registry=registry,
        split=split,
        stage6c_run=stage6c_run,
        panel_row_count=len(panel),
        terminal_row_count=sum(
            int(row['_terminal_return_used_flag']) for row in panel
        ),
    )
    check(
        'source_tieout_exact',
        [_numeric_row(row) for row in data['tieout_raw']] == expected_tieout,
    )
    panel_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    panel_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in panel:
        panel_by_date[str(row['asof_date'])].append(row)
        panel_lookup[(str(row['asof_date']), str(row['ticker']))] = row

    holdings_by_period: dict[
        tuple[str, str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for row in holdings:
        holdings_by_period[(
            str(row['candidate_id']), str(row['portfolio_name']),
            str(row['weight_method']), str(row['asof_date']),
        )].append(row)
    periods_by_key = {
        (
            str(row['candidate_id']), str(row['portfolio_name']),
            str(row['weight_method']), str(row['asof_date']),
        ): row
        for row in periods
    }
    check('period_keys_unique', len(periods_by_key) == len(periods))

    settings = stage9_config_payload(bundle)
    neutral = float(cfg_get(bundle.payload, 'stage7_scoring.neutral_score'))
    minimum_quality = float(cfg_get(
        bundle.payload, 'stage7_scoring.minimum_data_quality_confidence'
    ))
    maximum_missing = float(cfg_get(
        bundle.payload, 'stage7_scoring.maximum_missing_component_weight'
    ))
    formula_errors = 0
    for key, period in periods_by_key.items():
        values = holdings_by_period.get(key, [])
        candidate = candidate_lookup.get(key[0])
        if candidate is None or not values:
            formula_errors += 1
            continue
        scored_rows: list[dict[str, Any]] = []
        for source in panel_by_date[str(period['asof_date'])]:
            if (
                candidate.scope_id != SECTOR_SCOPE
                and str(source['cohort_id']) != candidate.scope_id
            ):
                continue
            score, available, missing, eligible = _score_candidate(
                source,
                candidate,
                neutral_score=neutral,
                minimum_quality=minimum_quality,
                maximum_missing=maximum_missing,
            )
            if eligible:
                row = dict(source)
                row['_candidate_score'] = score
                row['_candidate_available_weight'] = available
                row['_candidate_missing_weight'] = missing
                scored_rows.append(row)
        spec = PortfolioSpec(
            str(period['portfolio_name']),
            str(period['weight_method']),
            str(period['exposure_mode']),
        )
        expected_weights = build_portfolio_weights(
            scored_rows,
            spec,
            top_quantile=float(settings['top_quantile']),
            minimum_positions=int(settings['minimum_positions']),
        )
        actual_weights = {
            str(row['ticker']): float(row['weight']) for row in values
        }
        weight_match = set(expected_weights) == set(actual_weights) and all(
            _close(actual_weights[ticker], weight)
            for ticker, weight in expected_weights.items()
        )
        gross = sum(abs(value) for value in actual_weights.values())
        net = sum(actual_weights.values())
        total_return = sum(
            float(row['total_return_contribution']) for row in values
        )
        active_return = sum(
            float(row['xlp_relative_return_contribution']) for row in values
        )
        observed_borrow = sum(
            float(row['observed_borrow_cost']) for row in values
        )
        missing_borrow = sum(
            float(row['missing_borrow_stress_cost']) for row in values
        )
        terminal = sum(
            int(row['terminal_return_used_flag']) for row in values
        )
        score_match = all(
            _close(
                row['candidate_score'],
                _score_candidate(
                    panel_lookup[(
                        str(period['asof_date']), str(row['ticker'])
                    )],
                    candidate,
                    neutral_score=neutral,
                    minimum_quality=minimum_quality,
                    maximum_missing=maximum_missing,
                )[0],
            )
            for row in values
        )
        source_match = all(
            str(row['source_panel_row_sha256'])
            == str(panel_lookup[(
                str(period['asof_date']), str(row['ticker'])
            )]['row_sha256'])
            for row in values
        )
        if not all((
            int(period['cross_section_count']) == len(scored_rows),
            int(period['position_count']) == len(values),
            weight_match,
            _close(gross, period['gross_exposure']),
            _close(net, period['net_exposure']),
            _close(total_return, period['gross_total_return']),
            _close(active_return, period['gross_xlp_relative_return']),
            _close(observed_borrow, period['observed_borrow_cost']),
            _close(missing_borrow, period['missing_borrow_stress_cost']),
            terminal == int(period['terminal_return_position_count']),
            score_match,
            source_match,
        )):
            formula_errors += 1
    check('candidate_selection_and_holdings_reconciliation', formula_errors == 0)

    transaction_errors = 0
    grouped_periods: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for row in periods:
        grouped_periods[(
            str(row['candidate_id']), str(row['portfolio_name']),
            str(row['weight_method']),
        )].append(row)
    transaction_bps = float(settings['transaction_cost_bps'])
    for group_key, group in grouped_periods.items():
        ordered = sorted(group, key=lambda row: str(row['asof_date']))
        previous_end: dict[str, float] | None = None
        previous_exit: str | None = None
        for position, period in enumerate(ordered):
            key = (*group_key, str(period['asof_date']))
            values = holdings_by_period[key]
            weights = {
                str(row['ticker']): float(row['weight']) for row in values
            }
            if previous_end is None:
                entry = sum(abs(value) for value in weights.values())
                gap = 0.0
            elif str(period['entry_date']) == previous_exit:
                entry = sum(
                    abs(weights.get(ticker, 0.0) - previous_end.get(ticker, 0.0))
                    for ticker in set(weights) | set(previous_end)
                )
                gap = 0.0
            else:
                gap = sum(abs(value) for value in previous_end.values())
                entry = sum(abs(value) for value in weights.values())
            gross_return = float(period['gross_total_return'])
            denominator = 1.0 + gross_return
            end_weights = {
                ticker: weight * (
                    1.0 + float(
                        panel_lookup[(str(period['asof_date']), ticker)][
                            '_forward_total_return'
                        ]
                    )
                ) / denominator
                for ticker, weight in weights.items()
            }
            final = (
                sum(abs(value) for value in end_weights.values())
                if position == len(ordered) - 1 else 0.0
            )
            trade = entry + gap + final
            transaction = trade * transaction_bps / 10000.0
            observed_cost = transaction + float(period['observed_borrow_cost'])
            stress_cost = transaction + float(period['stress_borrow_cost'])
            if not all((
                _close(entry, period['entry_rebalance_turnover']),
                _close(gap, period['gap_liquidation_turnover']),
                _close(final, period['final_liquidation_turnover']),
                _close(trade, period['trade_notional_turnover']),
                _close(transaction, period['transaction_cost']),
                _close(observed_cost, period['total_observed_cost']),
                _close(stress_cost, period['total_stress_cost']),
                _close(
                    float(period['gross_total_return']) - observed_cost,
                    period['net_total_return_observed_cost'],
                ),
                _close(
                    float(period['gross_xlp_relative_return']) - observed_cost,
                    period['net_xlp_relative_return_observed_cost'],
                ),
                _close(
                    float(period['gross_total_return']) - stress_cost,
                    period['net_total_return_stress_cost'],
                ),
                _close(
                    float(period['gross_xlp_relative_return']) - stress_cost,
                    period['net_xlp_relative_return_stress_cost'],
                ),
            )):
                transaction_errors += 1
            previous_end = end_weights
            previous_exit = str(period['exit_date'])
    check('turnover_cost_and_net_return_reconciliation', transaction_errors == 0)

    period_groups: dict[
        tuple[str, str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for row in periods:
        period_groups[(
            str(row['candidate_id']), str(row['portfolio_name']),
            str(row['weight_method']), str(row['exposure_mode']),
        )].append(row)
    summary_lookup = {
        (
            str(row['candidate_id']), str(row['portfolio_name']),
            str(row['weight_method']), str(row['exposure_mode']),
            str(row['return_basis']),
        ): row
        for row in summary
    }
    summary_errors = 0
    for key, values in period_groups.items():
        for basis in ('total_return', 'xlp_relative'):
            observed = summary_lookup.get((*key, basis))
            if observed is None:
                summary_errors += 1
                continue
            expected = _return_metrics(values, schedule, return_basis=basis)
            for field, expected_value in expected.items():
                actual_value = observed.get(field)
                if isinstance(expected_value, (int, float)):
                    if not _close(actual_value, expected_value):
                        summary_errors += 1
                        break
                elif str(actual_value) != str(expected_value):
                    summary_errors += 1
                    break
    check('period_to_summary_reconciliation', summary_errors == 0)

    baseline_by_scope = {
        candidate.scope_id: candidate.candidate_id
        for candidate in candidates
        if candidate.candidate_kind == 'stage7_core_baseline'
    }
    baseline_errors = 0
    for row in summary:
        baseline_id = baseline_by_scope[str(row['scope_id'])]
        baseline = summary_lookup[(
            baseline_id, str(row['portfolio_name']),
            str(row['weight_method']), str(row['exposure_mode']),
            str(row['return_basis']),
        )]
        if not all((
            str(row['stage7_reference_candidate_id']) == baseline_id,
            _close(
                row['observed_annualized_return_delta_vs_stage7'],
                float(row['observed_annualized_return'])
                - float(baseline['observed_annualized_return']),
            ),
            _close(
                row['stress_annualized_return_delta_vs_stage7'],
                float(row['stress_annualized_return'])
                - float(baseline['stress_annualized_return']),
            ),
        )):
            baseline_errors += 1
    check('stage7_delta_reconciliation', baseline_errors == 0)
    check('terminal_values_present_when_required', (
        not bool(settings['require_terminal_value_reconciliation'])
        or int(manifest['terminal_21d_panel_row_count']) > 0
    ))
    check(
        'database_write_count_zero',
        int(manifest.get('database_write_count', -1)) == 0,
    )

    result = {
        'schema_version': 'consumer_defensive_stage9_validation_v1',
        'status': 'PASS' if not errors else 'FAIL',
        'stage9_run_id': contract.get('stage9_run_id'),
        'manifest_sha256': manifest.get('manifest_sha256'),
        'check_count': len(checks),
        'passed_check_count': sum(condition for _name, condition in checks),
        'errors': errors,
        'checks': [
            {'check': name, 'status': 'PASS' if condition else 'FAIL'}
            for name, condition in checks
        ],
        'candidate_count': len(candidates),
        'summary_row_count': len(summary),
        'period_row_count': len(periods),
        'holding_row_count': len(holdings),
        'permitted_use': (
            'stage10_reporting_input' if not errors else 'none_fail_closed'
        ),
        'portfolio_write_enabled': False,
        'production_promotion_enabled': False,
    }
    return result


def write_stage9_validation(path: Path, payload: Mapping[str, Any]) -> None:
    _immutable_json(path, dict(payload))
