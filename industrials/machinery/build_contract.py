from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from industrials.core.config import cfg_get


DISCLOSURE_PARSER_VERSION = "2026-07-23-v10"
HISTORICAL_BUILD_CONTRACT_VERSION = "machinery_history_v2"
HISTORICAL_BUILD_METADATA_FILENAME = "machinery_historical_build_metadata.json"


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
