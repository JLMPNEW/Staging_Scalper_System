from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from consumer_defensive.core.atomic_io import atomic_text_writer  # noqa: E402
from consumer_defensive.core.config import load_config  # noqa: E402
from consumer_defensive.core.market_data import load_market_policy  # noqa: E402
from consumer_defensive.core.stage7_historical_parity_v2 import (  # noqa: E402
    assess_provenance_binding,
    audit_stage7_artifact_seal,
    compare_reconstructed_scores,
    methodology_identity_audit,
    reconstruct_current_asof_stage8_scores,
)


DEFAULT_MARKET_POLICY = (
    PACKAGE_ROOT / 'data' / 'consumer_defensive_market_data_policy.yaml'
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Audit exact Stage 7 score identity and Stage 7/8 replay parity '
            'without changing the accepted legacy verdict.'
        )
    )
    parser.add_argument('--stage7-root', type=Path, required=True)
    parser.add_argument('--stage8-root', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument(
        '--config', type=Path, default=PACKAGE_ROOT / 'config.yaml'
    )
    parser.add_argument(
        '--market-policy', type=Path, default=DEFAULT_MARKET_POLICY
    )
    parser.add_argument('--output', type=Path)
    return parser.parse_args()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return payload


def _report_sha256(report: dict[str, Any]) -> str:
    canonical = json.dumps(
        report,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def run(args: argparse.Namespace) -> dict[str, Any]:
    stage7_root = args.stage7_root.expanduser().resolve()
    stage8_root = args.stage8_root.expanduser().resolve()
    database = args.database.expanduser().resolve()
    bundle = load_config(args.config.expanduser().resolve())
    market_policy = load_market_policy(
        args.market_policy.expanduser().resolve()
    )
    contract = _json(stage8_root / 'stage8_contract.json')
    decision = _json(stage8_root / 'stage8_decision.json')
    stage7_seal = audit_stage7_artifact_seal(stage7_root)
    sealed_rows = list(stage7_seal.pop('rows'))
    methodology = methodology_identity_audit(
        stage8_contract=contract,
        package_root=PACKAGE_ROOT,
    )

    conn = sqlite3.connect(
        f'file:{database.as_posix()}?mode=ro', uri=True
    )
    conn.row_factory = sqlite3.Row
    try:
        reconstruction = reconstruct_current_asof_stage8_scores(
            conn,
            bundle,
            as_of=str(stage7_seal['asof_date']),
            members=sealed_rows,
            market_policy=market_policy,
        )
    finally:
        conn.close()
    reconstructed_rows = list(reconstruction.pop('rows'))
    score_parity = compare_reconstructed_scores(
        sealed_rows, reconstructed_rows
    )
    provenance = assess_provenance_binding(
        stage8_contract=contract,
        stage8_decision=decision,
        stage7_seal=stage7_seal,
        methodology_identity=methodology,
        reconstruction=reconstruction,
        score_parity=score_parity,
    )
    report: dict[str, Any] = {
        'schema_version': 'consumer_defensive_stage7_historical_parity_audit_v2',
        'stage7_root': str(stage7_root),
        'stage8_root': str(stage8_root),
        'database': str(database),
        'stage7_artifact_seal': stage7_seal,
        'methodology_identity': methodology,
        'current_asof_stage8_reconstruction': reconstruction,
        'current_asof_score_parity': score_parity,
        'provenance_binding': provenance,
        'legacy_v3_verdict_unchanged': True,
        'promotion_action': 'remain_shadow_fail_closed',
        'future_fresh_run_requirements': [
            (
                'Preregister one monthly next-rebalance target and candidate '
                'registry before target access.'
            ),
            (
                'Seal the Stage 7/8 current-as-of parity manifest in the new '
                'Stage 8 contract.'
            ),
            (
                'Seal exact historical price-bar and component-source manifests '
                'before scoring or validation.'
            ),
            (
                'Bind the historical score-panel manifest and price-selection '
                'manifest into Stage 8 and Stage 9 contracts.'
            ),
            (
                'Use an unopened future holdout and keep survivorship, freshness, '
                'same-sample, and complete-month gates fail-closed.'
            ),
        ],
    }
    report['audit_sha256'] = _report_sha256(report)
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
