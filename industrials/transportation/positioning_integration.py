from __future__ import annotations

import runpy
import sys
from pathlib import Path
from typing import Sequence

from industrials.core.config import load_yaml, resolve_path


TRANSPORTATION_ROOT = Path(__file__).resolve().parent
INDUSTRIALS_ROOT = TRANSPORTATION_ROOT.parent
DEFAULT_POSITIONING_CONFIG = (
    TRANSPORTATION_ROOT / "positioning_config.yaml"
)
MODEL_FAMILY = "transportation"
SHARED_SCRIPTS = frozenset(
    {
        "09_import_industrials_positioning.py",
        "13_sync_industrials_positioning_upstream.py",
        "14_validate_industrials_sec_positioning_stages.py",
    }
)
PINNED_ARGUMENTS = frozenset(
    {
        "--config",
        "--model-family",
        "--output-csv",
    }
)


def validate_positioning_config(path: Path) -> dict[str, object]:
    resolved = path.expanduser().resolve()
    config = load_yaml(resolved)
    if (
        str(
            (config.get("industrials_universe") or {}).get(
                "initial_subsector"
            )
            or ""
        )
        != MODEL_FAMILY
    ):
        raise ValueError("positioning config must be transportation-scoped")
    positioning = config.get("positioning_import") or {}
    required_paths = (
        "positioning_overrides_csv",
        "positioning_universe_csv",
        "output_csv",
        "feature_output_csv",
    )
    paths: dict[str, str] = {}
    for key in required_paths:
        value = str(positioning.get(key) or "")
        if not value:
            raise KeyError(f"positioning_import.{key} is required")
        candidate = resolve_path(value, base_dir=resolved.parent)
        lowered = str(candidate).lower()
        if "transportation" not in lowered:
            raise ValueError(
                f"positioning_import.{key} is not transportation-scoped"
            )
        if "defense" in lowered or "machinery" in lowered:
            raise ValueError(
                f"positioning_import.{key} crosses another model family"
            )
        paths[key] = str(candidate)
    validation = config.get("positioning_validation") or {}
    floor = float(validation.get("min_form4_covered_fraction") or 0)
    if not 0 < floor <= 1:
        raise ValueError("Form 4 routing-health floor must be within (0, 1]")
    return {
        "config_path": str(resolved),
        "model_family": MODEL_FAMILY,
        "paths": paths,
        "min_form4_covered_fraction": floor,
    }


def shared_argv(
    script_name: str,
    user_args: Sequence[str],
    *,
    config_path: Path = DEFAULT_POSITIONING_CONFIG,
) -> list[str]:
    if script_name not in SHARED_SCRIPTS:
        raise FileNotFoundError(
            f"unsupported transportation positioning stage={script_name}"
        )
    validate_positioning_config(config_path)

    def _matches_pinned(name: str) -> bool:
        # The shared parsers allow unambiguous abbreviations
        # (allow_abbrev=True), so any user token that is a prefix of a
        # pinned option would override the pin via argparse last-wins.
        if not name.startswith("--") or len(name) <= 2:
            return False
        return any(
            pinned == name or pinned.startswith(name)
            for pinned in PINNED_ARGUMENTS
        )

    overridden = sorted(
        {
            argument.split("=", 1)[0]
            for argument in user_args
            if _matches_pinned(argument.split("=", 1)[0])
        }
    )
    if overridden:
        raise ValueError(
            "transportation positioning wrapper arguments are pinned="
            f"{overridden}"
        )
    script = INDUSTRIALS_ROOT / "scripts" / script_name
    if not script.is_file():
        raise FileNotFoundError(script)
    return [
        str(script),
        "--config",
        str(config_path.resolve()),
        "--model-family",
        MODEL_FAMILY,
        *user_args,
    ]


def run_positioning_shared(script_name: str) -> None:
    argv = shared_argv(script_name, list(sys.argv[1:]))
    sys.argv = argv
    runpy.run_path(argv[0], run_name="__main__")
