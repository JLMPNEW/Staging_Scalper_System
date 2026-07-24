from __future__ import annotations

import runpy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from industrials.core.config import family_config, load_yaml, resolve_path


TRANSPORTATION_ROOT = Path(__file__).resolve().parents[1]
INDUSTRIALS_ROOT = TRANSPORTATION_ROOT.parent
PROJECT_ROOT = INDUSTRIALS_ROOT.parent
DEFAULT_CONFIG = INDUSTRIALS_ROOT / "config.yaml"
MODEL_FAMILY = "transportation"
BENCHMARKS = "IYT,XTN,SPY"
PRIMARY_BENCHMARK = "IYT"
STAGE3_OUTPUT_DIR = PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY / "stage3"

MARKET_STAGE_ARGS = {
    "03_sync_industrials_yahoo_adjusted_prices.py": [
        "--output-csv",
        str(STAGE3_OUTPUT_DIR / "yahoo_adjusted_price_coverage.csv"),
    ],
    "04_audit_industrials_market_data_policy.py": [
        "--output-csv",
        str(STAGE3_OUTPUT_DIR / "market_data_policy_audit.csv"),
    ],
    "05_build_industrials_market_features.py": [
        "--primary-benchmark",
        PRIMARY_BENCHMARK,
        "--output-csv",
        str(STAGE3_OUTPUT_DIR / "market_feature_coverage.csv"),
    ],
    "06_validate_industrials_market_stage.py": [
        "--policy",
        str(TRANSPORTATION_ROOT / "data" / "transportation_universe_policy.yaml"),
    ],
}

FINANCIAL_STAGE_ARGS = {
    "07_sync_industrials_sec_fundamentals.py": [
        "--output-csv",
        str(PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY / "stage4" / "sec_fundamentals_sync.csv"),
    ],
    "08_build_industrials_financial_features.py": [
        "--output-csv",
        str(PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY / "stage4" / "financial_feature_coverage.csv"),
    ],
    "08_validate_industrials_financial_stage.py": [],
    "10_validate_industrials_scoring_eligibility_policy.py": [
        "--output-csv",
        str(PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY / "stage6" / "scoring_eligibility_policy_audit.csv"),
    ],
}


def run_market_shared(script_name: str) -> None:
    script = INDUSTRIALS_ROOT / "scripts" / script_name
    if script_name not in MARKET_STAGE_ARGS or not script.exists():
        raise FileNotFoundError(f"Unsupported shared transportation market stage: {script}")
    user_args = list(sys.argv[1:])
    pinned = {"--model-family", "--benchmark-tickers", "--primary-benchmark", "--output-csv", "--policy"}
    overridden = sorted({arg.split("=", 1)[0] for arg in user_args if arg.split("=", 1)[0] in pinned})
    if overridden:
        raise ValueError(f"Transportation wrapper arguments are pinned and cannot be overridden: {overridden}")
    sys.argv = [
        str(script),
        "--model-family",
        MODEL_FAMILY,
        "--benchmark-tickers",
        BENCHMARKS,
        *MARKET_STAGE_ARGS[script_name],
        *user_args,
    ]
    runpy.run_path(str(script), run_name="__main__")


def run_financial_shared(script_name: str) -> None:
    script = INDUSTRIALS_ROOT / "scripts" / script_name
    if script_name not in FINANCIAL_STAGE_ARGS or not script.exists():
        raise FileNotFoundError(f"Unsupported shared transportation financial stage: {script}")
    user_args = list(sys.argv[1:])
    pinned = {"--model-family", "--output-csv", "--availability-output-csv"}
    overridden = sorted({arg.split("=", 1)[0] for arg in user_args if arg.split("=", 1)[0] in pinned})
    if overridden:
        raise ValueError(f"Transportation wrapper arguments are pinned and cannot be overridden: {overridden}")
    sys.argv = [
        str(script),
        "--model-family",
        MODEL_FAMILY,
        *FINANCIAL_STAGE_ARGS[script_name],
        *user_args,
    ]
    runpy.run_path(str(script), run_name="__main__")


def run_fx_shared() -> None:
    script = INDUSTRIALS_ROOT / "scripts" / "11_sync_industrials_yahoo_fx_rates.py"
    user_args = list(sys.argv[1:])
    pinned = {"--pairs", "--output-csv"}
    overridden = sorted({arg.split("=", 1)[0] for arg in user_args if arg.split("=", 1)[0] in pinned})
    if overridden:
        raise ValueError(f"Transportation FX wrapper arguments are pinned and cannot be overridden: {overridden}")
    sys.argv = [
        str(script),
        "--pairs",
        "BRLUSD,CADUSD,CLPUSD,CNYUSD,COPUSD,EURUSD,GBPUSD,MXNUSD,NOKUSD",
        "--output-csv",
        str(PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY / "stage4" / "fx_rate_coverage.csv"),
        *user_args,
    ]
    runpy.run_path(str(script), run_name="__main__")


@dataclass(frozen=True)
class FoundationPaths:
    config_path: Path
    config: dict[str, Any]
    db_path: Path
    registry_path: Path
    active_path: Path
    delisted_path: Path
    historical_path: Path
    aliases_path: Path
    listing_path: Path
    policy_path: Path
    cohort_path: Path
    seed_source_id: str
    cohort_source_id: str
    historical_source_id: str
    delisted_source_id: str
    alias_source_id: str
    default_start_date: str
    timeout_sec: float


def resolve_foundation(config_path: Path, db_override: Path | None = None) -> FoundationPaths:
    resolved_config = config_path.expanduser().resolve()
    config = load_yaml(resolved_config)
    family = family_config(config, MODEL_FAMILY)
    universe = family.get("universe")
    if not isinstance(universe, dict):
        raise KeyError(f"model_families.{MODEL_FAMILY}.universe must be a mapping")
    base = resolved_config.parent

    def required(key: str) -> Any:
        value = universe.get(key)
        if value is None or str(value).strip() == "":
            raise KeyError(f"Missing model_families.{MODEL_FAMILY}.universe.{key}")
        return value

    paths = config.get("paths")
    if not isinstance(paths, dict) or not paths.get("database_path"):
        raise KeyError("Missing paths.database_path")
    source_registry = config.get("source_registry")
    if not isinstance(source_registry, dict) or not source_registry.get("path"):
        raise KeyError("Missing source_registry.path")
    raw_runtime = config.get("runtime")
    runtime: dict[str, Any] = raw_runtime if isinstance(raw_runtime, dict) else {}
    return FoundationPaths(
        config_path=resolved_config,
        config=config,
        db_path=db_override.expanduser().resolve() if db_override else resolve_path(paths["database_path"], base_dir=base),
        registry_path=resolve_path(source_registry["path"], base_dir=base),
        active_path=resolve_path(required("seed_csv"), base_dir=base),
        delisted_path=resolve_path(required("delisted_seed_csv"), base_dir=base),
        historical_path=resolve_path(required("historical_membership_csv"), base_dir=base),
        aliases_path=resolve_path(required("ticker_aliases_csv"), base_dir=base),
        listing_path=resolve_path(required("listing_dates_csv"), base_dir=base),
        policy_path=resolve_path(required("policy_path"), base_dir=base),
        cohort_path=resolve_path(required("cohort_path"), base_dir=base),
        seed_source_id=str(required("seed_source_id")),
        cohort_source_id=str(required("cohort_source_id")),
        historical_source_id=str(required("historical_membership_source_id")),
        delisted_source_id=str(required("delisted_source_id")),
        alias_source_id=str(required("ticker_aliases_source_id")),
        default_start_date=str(universe.get("delisted_default_start_date") or "2000-01-01"),
        timeout_sec=float(runtime.get("sqlite_timeout_sec") or 120.0),
    )
