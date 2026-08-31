"""Execute native V7 census reconciliation using the immutable V5 I/O shell."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from consumer_defensive.core.historical_census_reconciliation_v7 import (  # noqa: E402
    reconcile_historical_candidates_v7,
)


def _runner() -> ModuleType:
    path = Path(__file__).with_name(
        '00i_reconcile_historical_candidate_census_v5.py'
    )
    spec = importlib.util.spec_from_file_location(
        'consumer_defensive_census_v7_io_runner', path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load census I/O runner: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__doc__ = __doc__
    module.reconcile_historical_candidates_v5 = (
        reconcile_historical_candidates_v7
    )
    return module


def main() -> int:
    return int(_runner().main())


if __name__ == '__main__':
    raise SystemExit(main())
