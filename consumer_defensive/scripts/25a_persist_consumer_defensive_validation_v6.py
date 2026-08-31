"""Executable V6 lineage sealer with fail-closed blocker-set compatibility.

The immutable initial V6 sealer used ``set.append`` only on the failure branch.
This additive entry point supplies a set subtype whose ``append`` is the same
operation as ``add``; both the original implementation and this correction are
included in the emitted code lineage.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any


class _AppendableSet(set[Any]):
    def append(self, value: Any) -> None:
        self.add(value)


def _implementation() -> ModuleType:
    path = Path(__file__).with_name(
        '25_persist_consumer_defensive_validation_v6.py'
    )
    spec = importlib.util.spec_from_file_location(
        'consumer_defensive_validation_persistence_v6_implementation', path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load V6 persistence implementation: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.set = _AppendableSet
    module.__file__ = __file__
    return module


def main() -> int:
    module = _implementation()
    print(json.dumps(
        module.run(module._arguments()), indent=2, sort_keys=True
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
