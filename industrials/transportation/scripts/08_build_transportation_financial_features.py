#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SHARED_BUILDER = (
    PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py"
)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.transportation.xbrl_backfill import (  # noqa: E402
    repair_transportation_mapped_xbrl_facts,
)


def load_shared_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "transportation_shared_financial_builder",
        SHARED_BUILDER,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load shared financial builder: {SHARED_BUILDER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    setattr(
        module,
        "backfill_mapped_xbrl_facts",
        repair_transportation_mapped_xbrl_facts,
    )
    return module


def main() -> None:
    load_shared_builder().main()


if __name__ == "__main__":
    main()
