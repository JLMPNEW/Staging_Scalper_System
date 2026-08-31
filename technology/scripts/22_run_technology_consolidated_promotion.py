"""Run technology calibration chains and the consolidated promotion engine."""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.consolidated_promotion import run_consolidated_evaluation  # noqa: E402


LOGGER = logging.getLogger("technology_consolidated_promotion_runner")
DEFAULT_CONFIG = PROJECT_ROOT / "technology" / "config.yaml"
DEFAULT_POLICY = PROJECT_ROOT / "technology" / "data" / "technology_consolidated_promotion_policy_v1.yaml"
FAMILY_ORDER = ("semiconductors", "technology_hardware", "software_infrastructure")
CALIBRATION_CHAINS: dict[str, tuple[str, ...]] = {
    "semiconductors": (
        "technology/semiconductors/scripts/07_run_semiconductor_signal_diagnostics.py",
        "technology/semiconductors/scripts/11_run_semiconductor_optuna_calibration.py",
        "technology/semiconductors/scripts/11_validate_semiconductor_optuna_calibration.py",
        "technology/semiconductors/scripts/13_run_semiconductor_walk_forward_calibration.py",
        "technology/semiconductors/scripts/13_validate_semiconductor_walk_forward_calibration.py",
        "technology/semiconductors/scripts/09b_run_semiconductor_portfolio_backtest.py",
        "technology/semiconductors/scripts/12_validate_semiconductor_research_hardening.py",
    ),
    "technology_hardware": (
        "technology/technology_hardware/scripts/07_run_technology_hardware_signal_diagnostics.py",
        "technology/technology_hardware/scripts/07_validate_technology_hardware_signal_diagnostics.py",
        "technology/technology_hardware/scripts/08_run_technology_hardware_optuna_calibration.py",
        "technology/technology_hardware/scripts/08_validate_technology_hardware_optuna_calibration.py",
        "technology/technology_hardware/scripts/08c_run_technology_hardware_walk_forward_calibration.py",
        "technology/technology_hardware/scripts/08c_validate_technology_hardware_walk_forward_calibration.py",
        "technology/technology_hardware/scripts/09_run_technology_hardware_portfolio_backtest.py",
        "technology/technology_hardware/scripts/09_validate_technology_hardware_portfolio_backtest.py",
    ),
    "software_infrastructure": (
        "technology/software_infrastructure/scripts/07_run_software_infrastructure_signal_diagnostics.py",
        "technology/software_infrastructure/scripts/07_validate_software_infrastructure_signal_diagnostics.py",
        "technology/software_infrastructure/scripts/08_run_software_infrastructure_optuna_calibration.py",
        "technology/software_infrastructure/scripts/08_validate_software_infrastructure_optuna_calibration.py",
        "technology/software_infrastructure/scripts/08c_run_software_infrastructure_walk_forward_calibration.py",
        "technology/software_infrastructure/scripts/08c_validate_software_infrastructure_walk_forward_calibration.py",
        "technology/software_infrastructure/scripts/09_run_software_infrastructure_portfolio_backtest.py",
        "technology/software_infrastructure/scripts/09_validate_software_infrastructure_portfolio_backtest.py",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run governed calibration and economic promotion evaluation for technology families."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--families",
        default=",".join(FAMILY_ORDER),
        help="Comma-separated subset; execution is always semiconductors, hardware, then software.",
    )
    parser.add_argument(
        "--run-calibration",
        action="store_true",
        help="Rebuild diagnostics, Stage 8, walk-forward, and Stage 9 sequentially before evaluation.",
    )
    parser.add_argument(
        "--bootstrap-repetitions",
        type=int,
        default=None,
        help="Override policy repetitions; intended for deterministic smoke tests only.",
    )
    return parser.parse_args()


def _selected_families(raw: str) -> list[str]:
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = sorted(requested - set(FAMILY_ORDER))
    if unknown:
        raise ValueError(f"Unknown technology families: {unknown}")
    return [family for family in FAMILY_ORDER if family in requested]


def _run_calibration_chain(family: str, config_path: Path) -> None:
    for relative_script in CALIBRATION_CHAINS[family]:
        script = PROJECT_ROOT / relative_script
        command = [sys.executable, str(script), "--config", str(config_path)]
        LOGGER.info("[%s] running %s", family, script.name)
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)  # noqa: S603


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    families = _selected_families(args.families)
    if not families:
        raise RuntimeError("At least one technology family is required")
    if args.run_calibration:
        for family in families:
            _run_calibration_chain(family, config_path)
    results, manifest = run_consolidated_evaluation(
        policy_path=policy_path,
        technology_config_path=config_path,
        families=families,
        output_dir=args.output_dir.expanduser().resolve() if args.output_dir else None,
        bootstrap_repetitions=args.bootstrap_repetitions,
    )
    for result in results:
        scores = result["scores"]
        LOGGER.info(
            "%s decision=%s adjusted_score=%.2f confidence=%.3f hard_safety_pass=%s",
            result["family"],
            result["decision"],
            scores["adjusted_score"],
            scores["confidence"],
            result["hard_safety_pass"],
        )
    LOGGER.info("Sealed consolidated promotion run: %s", manifest["run_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
