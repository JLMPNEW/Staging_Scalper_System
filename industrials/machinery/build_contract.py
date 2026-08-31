from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from industrials.core.config import cfg_get, load_yaml, resolve_path


DISCLOSURE_PARSER_VERSION = "2026-07-23-v10"
HISTORICAL_BUILD_CONTRACT_VERSION = "machinery_history_v4"
HISTORICAL_BUILD_METADATA_FILENAME = "machinery_historical_build_metadata.json"
PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
HISTORICAL_SEMANTIC_SOURCE_PATHS = {
    'build_contract.py': PACKAGE_ROOT / 'build_contract.py',
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
    'stage12_activation.py': PACKAGE_ROOT / 'stage12_activation.py',
    'stage12_governance.py': PACKAGE_ROOT / 'stage12_governance.py',
    '18_backfill_machinery_historical_dashboard_reports.py': (
        PACKAGE_ROOT
        / 'scripts'
        / '18_backfill_machinery_historical_dashboard_reports.py'
    ),
    '18b_materialize_machinery_historical_promotions.py': (
        PACKAGE_ROOT
        / 'scripts'
        / '18b_materialize_machinery_historical_promotions.py'
    ),
    'adapter_semantics.py': (
        PROJECT_ROOT / 'portfolio_layer' / 'scores' / 'adapter_semantics.py'
    ),
}


def historical_semantic_source_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in sorted(HISTORICAL_SEMANTIC_SOURCE_PATHS.items())
    }


def historical_build_metadata(
    config: dict[str, Any],
    *,
    config_path: Path,
    policy_lock_date: str,
    required_metrics: Iterable[str],
) -> dict[str, Any]:
    from industrials.machinery.stage12_activation import (
        production_policy_source_hashes,
    )
    from industrials.machinery.stage12_governance import (
        machinery_portfolio_policy_fingerprint,
    )
    from portfolio_layer.scores.adapter_semantics import (
        industrial_adapter_semantic_sha256,
    )

    stage12_root_raw = str(
        cfg_get(config, "machinery_stage12.output_root", "") or ""
    ).strip()
    production_context: dict[str, Any] = {
        "configured": bool(stage12_root_raw),
        "production_source_sha256": production_policy_source_hashes(),
        "portfolio_adapter_semantic_sha256": (
            industrial_adapter_semantic_sha256()
        ),
    }
    if stage12_root_raw:
        stage12_root = resolve_path(
            stage12_root_raw,
            base_dir=config_path.parent,
        )
        state_path = stage12_root / "machinery_production_activation_state.json"
        production_context["activation_state_path"] = str(state_path)
        production_context["activation_state_sha256"] = (
            hashlib.sha256(state_path.read_bytes()).hexdigest()
            if state_path.is_file()
            else ""
        )
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            production_context["activation_asof"] = str(
                state.get("activation_asof") or ""
            )
            production_context["production_policy_status"] = str(
                state.get("production_policy_status") or ""
            )
            governance_lock_raw = str(
                state.get("governance_lock") or ""
            ).strip()
            governance_lock = Path(governance_lock_raw) if governance_lock_raw else None
            production_context["governance_lock_sha256"] = (
                hashlib.sha256(governance_lock.read_bytes()).hexdigest()
                if governance_lock is not None and governance_lock.is_file()
                else ""
            )
            portfolio_config_raw = str(
                state.get("portfolio_config")
                or cfg_get(config, "machinery_stage12.portfolio_config_path", "")
                or ""
            ).strip()
            if portfolio_config_raw:
                portfolio_config_path = resolve_path(
                    portfolio_config_raw,
                    base_dir=config_path.parent,
                )
                production_context["portfolio_policy_sha256"] = (
                    machinery_portfolio_policy_fingerprint(
                        load_yaml(portfolio_config_path)
                    )
                )
    eligibility_policy_raw = str(
        cfg_get(
            config,
            "scoring_policy.families.machinery.eligibility_policy_csv",
            "",
        )
        or ""
    ).strip()
    eligibility_policy_sha256 = ""
    if eligibility_policy_raw:
        eligibility_policy_path = resolve_path(
            eligibility_policy_raw,
            base_dir=config_path.parent,
        )
        eligibility_policy_sha256 = hashlib.sha256(
            eligibility_policy_path.read_bytes()
        ).hexdigest()
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
        "semantic_config": {
            "machinery_scoring": cfg_get(config, "machinery_scoring", {}),
            "market_data_policy": cfg_get(config, "market_data_policy", {}),
            "machinery_scoring_policy": cfg_get(
                config,
                "scoring_policy.families.machinery",
                {},
            ),
            "sec_companyfacts_source_id": cfg_get(
                config,
                "sec_fundamentals.companyfacts_source_id",
                "sec_companyfacts",
            ),
            "positioning_source_id": cfg_get(
                config,
                "positioning_import.source_id",
                "industrials_positioning_composite",
            ),
            "eligibility_policy_sha256": eligibility_policy_sha256,
            "machinery_stage12": cfg_get(config, "machinery_stage12", {}),
        },
        "production_policy_context": production_context,
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
