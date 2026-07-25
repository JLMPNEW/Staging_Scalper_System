from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Callable
from typing import cast

from dedicated_parser.contracts import (
    AdapterRegistry,
    MetricEvidence,
    NormalizedFact,
    WorkItem,
)


def _module_name(adapter_path: str) -> str:
    module_name, separator, _ = adapter_path.partition(":")
    if not separator or not module_name:
        raise ValueError(
            "Adapter path must use 'module:function' syntax, "
            f"received {adapter_path!r}"
        )
    return module_name


def load_registry(adapter_path: str) -> AdapterRegistry:
    module = importlib.import_module(_module_name(adapter_path))
    function = getattr(module, "get_registry", None)
    if not callable(function):
        raise TypeError(f"Adapter {adapter_path!r} does not expose get_registry()")
    registry = function()
    if not isinstance(registry, AdapterRegistry):
        raise TypeError(
            f"Adapter {adapter_path!r} returned {type(registry).__name__}, "
            "expected AdapterRegistry"
        )
    return registry


def load_extractor(
    adapter_path: str,
) -> Callable[[WorkItem], tuple[MetricEvidence, ...]]:
    module_name, _, function_name = adapter_path.partition(":")
    module = importlib.import_module(module_name)
    function = getattr(module, function_name, None)
    if not callable(function):
        raise TypeError(f"Adapter extractor is not callable: {adapter_path!r}")
    return cast(Callable[[WorkItem], tuple[MetricEvidence, ...]], function)


def load_fact_mapper(
    adapter_path: str,
) -> Callable[[WorkItem, tuple[NormalizedFact, ...]], tuple[MetricEvidence, ...]] | None:
    module = importlib.import_module(_module_name(adapter_path))
    function = getattr(module, "map_normalized_facts", None)
    if function is None:
        return None
    if not callable(function):
        raise TypeError(
            f"Adapter {adapter_path!r} exposes a non-callable map_normalized_facts"
        )
    return cast(
        Callable[
            [WorkItem, tuple[NormalizedFact, ...]],
            tuple[MetricEvidence, ...],
        ],
        function,
    )


def load_ticker_selector(
    adapter_path: str,
) -> Callable[[sqlite3.Connection, str], list[str]] | None:
    module = importlib.import_module(_module_name(adapter_path))
    function = getattr(module, "select_tickers", None)
    if function is None:
        return None
    if not callable(function):
        raise TypeError(
            f"Adapter {adapter_path!r} exposes a non-callable select_tickers"
        )
    return cast(Callable[[sqlite3.Connection, str], list[str]], function)
