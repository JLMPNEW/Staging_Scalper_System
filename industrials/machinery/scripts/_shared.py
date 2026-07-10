from __future__ import annotations

import runpy
import sys
from pathlib import Path


MACHINERY_ROOT = Path(__file__).resolve().parents[1]
INDUSTRIALS_ROOT = MACHINERY_ROOT.parent
DEFAULT_CONFIG = MACHINERY_ROOT / "config.yaml"


def run_shared(script_name: str, *, pin_model_family: bool = True) -> None:
    script = INDUSTRIALS_ROOT / "scripts" / script_name
    if not script.exists():
        raise FileNotFoundError(f"Shared industrials stage does not exist: {script}")
    args = list(sys.argv[1:])
    if "--config" not in args:
        args = ["--config", str(DEFAULT_CONFIG), *args]
    if pin_model_family:
        args.extend(["--model-family", "machinery"])
    sys.argv = [str(script), *args]
    runpy.run_path(str(script), run_name="__main__")
