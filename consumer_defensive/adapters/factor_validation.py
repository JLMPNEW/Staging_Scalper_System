"""Consumer Defensive bridge into the shared factor-validation kernel.

The adapter consumes only a frozen Stage 6C panel. It is deliberately
research-only: it cannot write scoring, portfolio, or promotion tables.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from factor_validation import (
    CampaignRegistry,
    FDRFamily,
    FactorObservation,
    FactorValidationConfig,
    ProvenanceFileSet,
    ValidationCellRegistration,
    anchor_campaign_report,
    campaign_registry_path,
    load_campaign_registry,
    register_campaign,
    verify_campaign_ledger,
    verify_evidence_package,
    write_evidence_family,
)
from factor_validation.artifacts import evidence_package_path

from consumer_defensive.core.config import ConfigBundle, cfg_get
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.stage6c_panel import _panel_sha, _row_hash


ADAPTER_VERSION = 'consumer_defensive_factor_validation_v1'
SECTOR_ID = 'consumer_defensive'
REPORT_FILE = 'consumer_defensive_factor_validation_report.json'
TARGET_COLUMNS = {
    'forward_xlp_residual_return': 'forward_xlp_residual_return_{horizon}d',
    'forward_spy_beta_residual_return': (
        'forward_spy_beta_residual_return_{horizon}d'
    ),
}
_REQUIRED_PANEL_COLUMNS = {
    'stage6c_run_id', 'asof_date', 'ticker', 'cohort_id',
    'applicability_subtype', 'factor_id', 'factor_value',
    'availability_status', 'investable_flag', 'market_regime', 'row_sha256',
}


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


def _read_csv(path: Path) -> list[dict[str, str]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f'Factor input must be a regular non-symlink file: {resolved}')
    with resolved.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError('Factor panel has missing or duplicate CSV headers.')
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError('Factor panel is empty.')
    return rows


def _optional_float(value: Any, *, field_name: str) -> float | None:
    if value is None or str(value).strip() == '':
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f'{field_name} must be finite or blank.')
    return parsed


def _parsed_panel(
    conn: sqlite3.Connection,
    *,
    stage6c_run_id: int,
    panel_path: Path,
) -> tuple[list[dict[str, Any]], sqlite3.Row]:
    run = conn.execute(
        '''SELECT * FROM stage6c_panel_run
           WHERE stage6c_run_id=? AND status='complete' ''',
        (stage6c_run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f'Unknown or incomplete Stage 6C run: {stage6c_run_id}')
    raw_rows = _read_csv(panel_path)
    missing = _REQUIRED_PANEL_COLUMNS - set(raw_rows[0])
    if missing:
        raise ValueError(f'Stage 6C panel CSV is missing columns: {sorted(missing)}')
    parsed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, row in enumerate(raw_rows, start=2):
        if int(row['stage6c_run_id']) != stage6c_run_id:
            raise ValueError(f'Panel row {index} belongs to another Stage 6C run.')
        key = (row['asof_date'], row['ticker'], row['factor_id'])
        if key in seen:
            raise ValueError(f'Duplicate Stage 6C factor-panel key: {key}')
        seen.add(key)
        semantic: dict[str, Any] = dict(row)
        for field_name in (
            'factor_value',
            'forward_total_return_21d',
            'forward_total_return_63d',
            'forward_total_return_126d',
            'forward_xlp_residual_return_21d',
            'forward_xlp_residual_return_63d',
            'forward_xlp_residual_return_126d',
            'forward_spy_beta_residual_return_21d',
            'forward_spy_beta_residual_return_63d',
            'forward_spy_beta_residual_return_126d',
        ):
            semantic[field_name] = _optional_float(
                row.get(field_name), field_name=f'{field_name} row {index}'
            )
        for field_name in (
            'source_age_days', 'membership_eligible_flag', 'investable_flag'
        ):
            raw = row.get(field_name)
            semantic[field_name] = None if raw in {None, ''} else int(str(raw))
        for field_name in (
            'unit', 'source_accepted_at', 'source_period_end',
            'source_observation_sha256', 'source_definition_version',
        ):
            if semantic.get(field_name) == '':
                semantic[field_name] = None
        if _row_hash(semantic) != str(row['row_sha256']):
            raise ValueError(f'Stage 6C panel row hash mismatch at CSV row {index}.')
        parsed.append(semantic)
    parsed.sort(key=lambda row: (row['asof_date'], row['ticker'], row['factor_id']))
    if _panel_sha(parsed) != str(run['panel_sha256']):
        raise ValueError('Stage 6C exported panel does not match the database seal.')
    return parsed, run


def _cell_id(
    *, factor_id: str, scope_id: str, target_name: str, horizon: int
) -> str:
    digest = _sha256({
        'factor_id': factor_id,
        'scope_id': scope_id,
        'target_name': target_name,
        'horizon': horizon,
    })[:24]
    return f'cdfv_{digest}'


def _family_id(*, scope_id: str, target_name: str, horizon: int) -> str:
    digest = _sha256({
        'scope_id': scope_id,
        'target_name': target_name,
        'horizon': horizon,
    })[:20]
    return f'cdfam_{digest}'


def _campaign_id(
    *, as_of: str, panel_sha256: str, cell_keys: Iterable[str]
) -> str:
    selection = _sha256(sorted(cell_keys))[:12]
    return f'cdfv_{as_of.replace("-", "")}_{panel_sha256[:12]}_{selection}'


def _provenance(
    bundle: ConfigBundle,
    *,
    panel_path: Path,
    feature_manifest_path: Path,
) -> ProvenanceFileSet:
    project_root = Path(__file__).resolve().parents[2]
    return ProvenanceFileSet(
        config_path=bundle.path,
        source_paths={
            'consumer_defensive/stage6c_specialized_factor_panel.csv': panel_path,
            'consumer_defensive/stage6c_feature_manifest.csv': feature_manifest_path,
        },
        code_paths={
            'consumer_defensive/adapters/factor_validation.py': Path(__file__).resolve(),
            'consumer_defensive/core/stage6c_panel.py': (
                project_root / 'consumer_defensive' / 'core' / 'stage6c_panel.py'
            ),
            'factor_validation/core.py': project_root / 'factor_validation' / 'core.py',
            'factor_validation/acceptance.py': (
                project_root / 'factor_validation' / 'acceptance.py'
            ),
            'factor_validation/artifacts.py': (
                project_root / 'factor_validation' / 'artifacts.py'
            ),
            'factor_validation/evidence.py': (
                project_root / 'factor_validation' / 'evidence.py'
            ),
            'factor_validation/fdr.py': project_root / 'factor_validation' / 'fdr.py',
            'factor_validation/ledger.py': (
                project_root / 'factor_validation' / 'ledger.py'
            ),
            'factor_validation/registry.py': (
                project_root / 'factor_validation' / 'registry.py'
            ),
        },
    )


def _scope_rows(
    rows: list[dict[str, Any]], *, factor_id: str, scope_id: str
) -> list[dict[str, Any]]:
    selected = [row for row in rows if row['factor_id'] == factor_id]
    if scope_id != SECTOR_ID:
        selected = [row for row in selected if row['cohort_id'] == scope_id]
    return selected


def run_consumer_defensive_factor_validation(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    stage6c_run_id: int,
    panel_path: Path,
    feature_manifest_path: Path,
    output_root: Path,
    factor_ids: Iterable[str] | None = None,
    horizons: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Publish governed cohort and sector evidence from one frozen panel."""

    rows, panel_run = _parsed_panel(
        conn,
        stage6c_run_id=stage6c_run_id,
        panel_path=panel_path,
    )
    manifest_rows = {
        str(row['factor_id']): dict(row)
        for row in conn.execute(
            '''SELECT * FROM stage6c_feature_manifest
               WHERE stage6c_run_id=? ORDER BY factor_id''',
            (stage6c_run_id,),
        )
    }
    eligible_factors = {
        factor_id
        for factor_id, row in manifest_rows.items()
        if int(row['factor_validation_eligible']) == 1
    }
    requested_factors = (
        set(str(value) for value in factor_ids)
        if factor_ids is not None else eligible_factors
    )
    unknown = requested_factors - eligible_factors
    if unknown:
        raise ValueError(
            'Requested factors are not directionally registered SEC factors: '
            f'{sorted(unknown)}'
        )
    requested_horizons = tuple(sorted(
        {int(value) for value in (
            horizons if horizons is not None
            else cfg_get(bundle.payload, 'factor_validation.horizons')
        )}
    ))
    panel_horizons = tuple(json.loads(str(panel_run['horizons_json'])))
    if not requested_horizons or not set(requested_horizons).issubset(panel_horizons):
        raise ValueError('Requested horizons are absent from the Stage 6C panel.')
    targets = (
        str(cfg_get(bundle.payload, 'factor_validation.primary_target')),
        str(cfg_get(bundle.payload, 'factor_validation.robustness_target')),
    )
    if set(targets) != set(TARGET_COLUMNS):
        raise ValueError('Consumer Defensive factor targets drifted from the adapter contract.')
    provenance = _provenance(
        bundle,
        panel_path=panel_path,
        feature_manifest_path=feature_manifest_path,
    )
    observed = provenance.observe()
    cells: list[ValidationCellRegistration] = []
    configs: dict[str, FactorValidationConfig] = {}
    observations: dict[str, tuple[FactorObservation, ...]] = {}
    family_members: dict[str, list[str]] = defaultdict(list)
    cell_metadata: dict[str, dict[str, Any]] = {}
    for factor_id in sorted(requested_factors):
        manifest = manifest_rows[factor_id]
        scopes = [SECTOR_ID, *json.loads(str(manifest['cohorts_json']))]
        for scope_id in dict.fromkeys(scopes):
            scoped = _scope_rows(rows, factor_id=factor_id, scope_id=scope_id)
            if not scoped:
                continue
            minimum_cross_section = (
                int(cfg_get(bundle.payload, 'factor_validation.sector_minimum_cross_section'))
                if scope_id == SECTOR_ID
                else int(cfg_get(bundle.payload, 'factor_validation.cohort_exploratory_minimum_cross_section'))
            )
            for target_name in targets:
                for horizon in requested_horizons:
                    target_column = TARGET_COLUMNS[target_name].format(horizon=horizon)
                    cell_id = _cell_id(
                        factor_id=factor_id,
                        scope_id=scope_id,
                        target_name=target_name,
                        horizon=horizon,
                    )
                    family_id = _family_id(
                        scope_id=scope_id,
                        target_name=target_name,
                        horizon=horizon,
                    )
                    config = FactorValidationConfig(
                        horizon_trading_days=horizon,
                        entry_lag_trading_days=1,
                        min_cross_section=minimum_cross_section,
                        min_dates=12,
                        min_independent_windows=3,
                        min_regime_dates=3,
                        quantile_count=5,
                        min_extreme_bucket_size=2,
                        round_trip_cost=0.003,
                        target_name=target_name,
                        transition_cadence_trading_days=21,
                    )
                    cell_observations = tuple(
                        FactorObservation(
                            as_of_date=row['asof_date'],
                            entity_id=row['ticker'],
                            factor_value=(
                                row['factor_value']
                                if row['availability_status'] == 'available'
                                and int(row['investable_flag']) == 1
                                else None
                            ),
                            forward_return=row.get(target_column),
                            regime=(
                                row['market_regime']
                                if row['market_regime'] in {'risk_on', 'risk_off'}
                                else None
                            ),
                        )
                        for row in scoped
                    )
                    cell = ValidationCellRegistration(
                        cell_id=cell_id,
                        sector_id=scope_id,
                        factor_id=factor_id,
                        target_name=target_name,
                        horizon_trading_days=horizon,
                        entry_lag_trading_days=1,
                        factor_direction=str(manifest['factor_direction']),
                        evaluation_step_trading_days=21,
                        fdr_family_id=family_id,
                        fdr_member_id=cell_id,
                        config_sha256=observed.config_sha256,
                        source_files=observed.source_files,
                        code_files=observed.code_files,
                        validation_config=config,
                    )
                    cells.append(cell)
                    configs[cell_id] = config
                    observations[cell_id] = cell_observations
                    family_members[family_id].append(cell_id)
                    cell_metadata[cell_id] = {
                        'factor_id': factor_id,
                        'scope_id': scope_id,
                        'target_name': target_name,
                        'horizon_trading_days': horizon,
                        'observation_rows': len(cell_observations),
                    }
    if not cells:
        raise RuntimeError('No Consumer Defensive factor-validation cells were registered.')
    families = tuple(
        FDRFamily(
            family_id=family_id,
            member_ids=tuple(sorted(members)),
            alpha=0.05,
        )
        for family_id, members in sorted(family_members.items())
    )
    campaign_id = _campaign_id(
        as_of=str(panel_run['asof_date']),
        panel_sha256=str(panel_run['panel_sha256']),
        cell_keys=(cell.cell_id for cell in cells),
    )
    registry = CampaignRegistry(
        campaign_id=campaign_id,
        cells=tuple(cells),
        fdr_families=families,
    )
    output_root = output_root.expanduser().resolve()
    provenance_by_cell = {cell.cell_id: provenance for cell in cells}
    register_campaign(
        output_root,
        registry,
        provenance_files=provenance_by_cell,
    )
    packages = []
    for family in registry.fdr_families:
        members = set(family.member_ids)
        packages.extend(write_evidence_family(
            output_root,
            registry,
            family_id=family.family_id,
            observations={key: value for key, value in observations.items() if key in members},
            configs={key: value for key, value in configs.items() if key in members},
            provenance_files={key: value for key, value in provenance_by_cell.items() if key in members},
        ))
    states = Counter(package.state for package in packages)
    report = {
        'schema_version': 'consumer_defensive_factor_validation_report_v1',
        'adapter_version': ADAPTER_VERSION,
        'campaign_id': campaign_id,
        'registry_sha256': registry.registration_sha256,
        'stage6c_run_id': stage6c_run_id,
        'stage6c_panel_sha256': str(panel_run['panel_sha256']),
        'asof_date': str(panel_run['asof_date']),
        'cell_count': len(cells),
        'family_count': len(families),
        'package_state_counts': dict(sorted(states.items())),
        'cells': [cell_metadata[cell.cell_id] for cell in registry.cells],
        'mode': 'shadow',
        'shared_gate_active': False,
        'statistical_acceptance_only': True,
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
    }
    report_path = output_root / campaign_id / REPORT_FILE
    if report_path.is_file():
        existing = json.loads(report_path.read_text(encoding='utf-8'))
        if existing != report:
            raise FileExistsError('Immutable factor-validation report content changed.')
    else:
        write_json(report_path, report)
    anchor_campaign_report(
        output_root,
        registry,
        family_id=registry.fdr_families[0].family_id,
        report_path=report_path,
    )
    verified = validate_consumer_defensive_factor_validation(
        output_root,
        campaign_id=campaign_id,
    )
    if verified['status'] != 'PASS':
        raise RuntimeError(f'Factor-validation publication failed: {verified["errors"]}')
    return {**report, 'output_root': str(output_root), 'report_path': str(report_path)}


def validate_consumer_defensive_factor_validation(
    output_root: Path,
    *,
    campaign_id: str,
) -> dict[str, Any]:
    root = output_root.expanduser().resolve()
    registry = load_campaign_registry(
        root / campaign_id / 'campaign_registry.json'
    )
    errors: list[str] = []
    states: dict[str, str | None] = {}
    for cell in registry.cells:
        report = verify_evidence_package(
            evidence_package_path(root, registry, cell_id=cell.cell_id),
            expected_registry=registry,
            expected_cell_id=cell.cell_id,
            ledger_root=root,
        )
        states[cell.cell_id] = report.state
        errors.extend(f'{cell.cell_id}:{error}' for error in report.errors)
    ledger = verify_campaign_ledger(root)
    errors.extend(f'ledger:{error}' for error in ledger.errors)
    report_path = root / campaign_id / REPORT_FILE
    try:
        payload = json.loads(report_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f'report_unreadable:{type(exc).__name__}')
        payload = {}
    safety = {
        'mode': 'shadow',
        'shared_gate_active': False,
        'statistical_acceptance_only': True,
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
    }
    for key, expected in safety.items():
        if payload.get(key) != expected:
            errors.append(f'report_safety_lock_mismatch:{key}')
    if payload.get('campaign_id') != campaign_id:
        errors.append('report_campaign_id_mismatch')
    if payload.get('registry_sha256') != registry.registration_sha256:
        errors.append('report_registry_sha256_mismatch')
    if campaign_registry_path(root, registry) != root / campaign_id / 'campaign_registry.json':
        errors.append('campaign_registry_path_mismatch')
    return {
        'status': 'PASS' if not errors else 'FAIL',
        'campaign_id': campaign_id,
        'cell_count': len(registry.cells),
        'family_count': len(registry.fdr_families),
        'states': states,
        'ledger_entry_count': ledger.entry_count,
        'errors': errors,
        **safety,
    }
