from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
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
from consumer_defensive.core.stage8_independent_evidence_v5 import (  # noqa: E402
    absolute_baseline_independent_evidence_v5,
    independent_evidence_gate_v5,
)
from consumer_defensive.core.stage9_backtest import (  # noqa: E402
    _candidate,
    _load_panel,
)


SCHEMA_VERSION = 'consumer_defensive_validation_evidence_v5'


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Persist an immutable, hash-bound Consumer Defensive validation '
            'audit after the V5 turnover and independent-evidence fixes.'
        )
    )
    parser.add_argument('--stage8-root', type=Path, required=True)
    parser.add_argument('--stage9-root', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument(
        '--config', type=Path, default=PACKAGE_ROOT / 'config.yaml'
    )
    parser.add_argument('--output-dir', type=Path, required=True)
    return parser.parse_args()


def _script23() -> ModuleType:
    path = Path(__file__).with_name(
        '23_audit_consumer_defensive_validation_v4.py'
    )
    spec = importlib.util.spec_from_file_location(
        'consumer_defensive_validation_audit_script23', path
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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _file_identity(path: Path, *, role: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    return {
        'role': role,
        'path': str(resolved),
        'bytes': resolved.stat().st_size,
        'sha256': _file_sha256(resolved),
    }


def _write_json(path: Path, payload: MappingPayload) -> None:
    content = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + '\n'
    with atomic_text_writer(path, encoding='utf-8', newline='') as handle:
        handle.write(content)


MappingPayload = dict[str, Any]


def _v5_evidence(
    args: argparse.Namespace,
    base: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    stage8_root = args.stage8_root.expanduser().resolve()
    database = args.database.expanduser().resolve()
    bundle = load_config(args.config.expanduser().resolve())
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
    evidence = absolute_baseline_independent_evidence_v5(
        era_panel,
        holdout_dates,
        baseline,
        bundle,
        calendar=calendar,
        entry_lag=int(stage6c['entry_lag_trading_days']),
        schedule_dates=era_dates,
    )
    gate = independent_evidence_gate_v5(
        evidence,
        minimum_independent_dates={21: 12, 63: 6, 126: 4},
        maximum_sign_pvalue=0.10,
        invariants=base['promotion_invariants'],
    )
    return evidence, gate


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(
            'Validation evidence is immutable; choose a new output directory: '
            f'{output_dir}'
        )
    output_dir.mkdir(parents=True, exist_ok=False)
    base = _script23().run(args)
    evidence, gate = _v5_evidence(args, base)
    blockers = set(base['limited_production_gate_v4']['blockers'])
    blockers.update(gate['blockers'])
    report = {
        **base,
        'schema_version': SCHEMA_VERSION,
        'calendar_independent_baseline_evidence_v5': evidence,
        'calendar_independent_gate_v5': gate,
        'limited_production_gate_v5': {
            'limited_production_ready_flag': 0,
            'action': 'remain_shadow_fail_closed',
            'blockers': sorted(blockers),
            'portfolio_write_enabled': False,
            'production_promotion_enabled': False,
        },
    }
    future_plan = dict(report['future_monthly_target_plan_template'])
    future_plan.update({
        'candidate_registry_sha256': '',
        'split_manifest_sha256': '',
        'plan_sha256': '',
        'train_dates': [],
        'first_embargo_dates': [],
        'validation_dates': [],
        'second_embargo_dates': [],
        'holdout_dates': [],
        'plan_validation_pass_flag': 0,
    })
    report_path = output_dir / 'validation_audit_v5.json'
    plan_path = output_dir / 'future_monthly_target_plan_draft.json'
    _write_json(report_path, report)
    _write_json(plan_path, future_plan)

    stage8_root = args.stage8_root.expanduser().resolve()
    stage9_root = args.stage9_root.expanduser().resolve()
    input_paths = [
        *sorted(stage8_root.iterdir()),
        *sorted(stage9_root.iterdir()),
        args.config.expanduser().resolve(),
        args.database.expanduser().resolve(),
        PACKAGE_ROOT / 'core' / 'portfolio_turnover_v2.py',
        PACKAGE_ROOT / 'core' / 'stage8_validation_v5.py',
        PACKAGE_ROOT / 'core' / 'stage8_independent_evidence_v5.py',
        Path(__file__).resolve(),
    ]
    inputs = [
        _file_identity(path, role='input')
        for path in input_paths if path.is_file()
    ]
    outputs = [
        _file_identity(report_path, role='validation_report'),
        _file_identity(plan_path, role='future_plan_draft'),
    ]
    manifest: dict[str, Any] = {
        'schema_version': 'consumer_defensive_validation_artifact_manifest_v1',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'output_dir': str(output_dir),
        'inputs': inputs,
        'outputs': outputs,
        'promotion_action': 'remain_shadow_fail_closed',
        'legacy_evidence_classification': 'retrospective_diagnostic_only',
    }
    manifest['manifest_payload_sha256'] = _canonical_sha256(manifest)
    manifest_path = output_dir / 'artifact_manifest.json'
    _write_json(manifest_path, manifest)
    return {
        'output_dir': str(output_dir),
        'report_path': str(report_path),
        'future_plan_path': str(plan_path),
        'manifest_path': str(manifest_path),
        'report_sha256': outputs[0]['sha256'],
        'future_plan_sha256': outputs[1]['sha256'],
        'manifest_file_sha256': _file_sha256(manifest_path),
        'manifest_payload_sha256': manifest['manifest_payload_sha256'],
        'promotion_action': 'remain_shadow_fail_closed',
    }


def main() -> int:
    args = _arguments()
    print(json.dumps(run(args), indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
