from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from industrials.core.config import cfg_get


DISCLOSURE_PARSER_VERSION = "2026-07-23-v10"
HISTORICAL_BUILD_CONTRACT_VERSION = "machinery_history_v3"
HISTORICAL_BUILD_METADATA_FILENAME = "machinery_historical_build_metadata.json"
PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
HISTORICAL_SEMANTIC_SOURCE_PATHS = {
    '05_build_industrials_market_features.py': (
        PROJECT_ROOT / 'industrials' / 'scripts' / '05_build_industrials_market_features.py'
    ),
    '07_sync_industrials_sec_fundamentals.py': (
        PROJECT_ROOT / 'industrials' / 'scripts' / '07_sync_industrials_sec_fundamentals.py'
    ),
    '08_build_industrials_financial_features.py': (
        PROJECT_ROOT / 'industrials' / 'scripts' / '08_build_industrials_financial_features.py'
    ),
    '09_import_industrials_positioning.py': (
        PROJECT_ROOT / 'industrials' / 'scripts' / '09_import_industrials_positioning.py'
    ),
    'financial_contract.py': PACKAGE_ROOT / 'financial_contract.py',
    'historical_coverage.py': PACKAGE_ROOT / 'historical_coverage.py',
    'scoring.py': PACKAGE_ROOT / 'scoring.py',
}


def historical_semantic_source_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in sorted(HISTORICAL_SEMANTIC_SOURCE_PATHS.items())
    }


def historical_build_metadata(
    config: dict[str, Any],
    *,
    policy_lock_date: str,
    required_metrics: Iterable[str],
) -> dict[str, Any]:
    payload = {
        "historical_build_contract_version": HISTORICAL_BUILD_CONTRACT_VERSION,
        "disclosure_parser_version": DISCLOSURE_PARSER_VERSION,
        "scoring_contract_version": str(
            cfg_get(config, "machinery_scoring.contract_version", "")
        ),
        "score_model_version": str(
            cfg_get(config, "machinery_scoring.score_model_version", "")
        ),
        "model_version": str(
            cfg_get(config, "machinery_scoring.model_version", "")
        ),
        "policy_lock_date": policy_lock_date,
        "required_metrics": sorted(str(metric) for metric in required_metrics),
        "semantic_source_sha256": historical_semantic_source_hashes(),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **payload,
        "historical_build_signature": hashlib.sha256(encoded).hexdigest(),
    }
