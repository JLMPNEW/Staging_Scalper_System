from __future__ import annotations

from pathlib import Path

from portfolio_layer.scores.adapter_semantics import (
    industrial_adapter_semantic_sha256,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_PATH = PROJECT_ROOT / "portfolio_layer" / "scores" / "adapters.py"
CONTRACTS_PATH = PROJECT_ROOT / "portfolio_layer" / "core" / "contracts.py"


def _with_function_docstring(source: str, signature: str) -> str:
    marker = signature + "\n"
    assert marker in source
    return source.replace(marker, marker + '    """semantic seal test probe"""\n', 1)


def test_industrial_semantic_seal_ignores_med_device_only_change() -> None:
    adapter_source = ADAPTER_PATH.read_text(encoding="utf-8")
    contracts_source = CONTRACTS_PATH.read_text(encoding="utf-8")
    baseline = industrial_adapter_semantic_sha256(
        adapter_source=adapter_source,
        contracts_source=contracts_source,
    )
    changed = _with_function_docstring(
        adapter_source,
        "def _med_device_score_provenance_unsafe(row: dict[str, str]) -> bool:",
    )
    assert industrial_adapter_semantic_sha256(
        adapter_source=changed,
        contracts_source=contracts_source,
    ) == baseline


def test_industrial_semantic_seal_detects_industrial_change() -> None:
    adapter_source = ADAPTER_PATH.read_text(encoding="utf-8")
    contracts_source = CONTRACTS_PATH.read_text(encoding="utf-8")
    baseline = industrial_adapter_semantic_sha256(
        adapter_source=adapter_source,
        contracts_source=contracts_source,
    )
    changed = _with_function_docstring(
        adapter_source,
        "def _adapt_industrial_family(cfg: dict[str, Any], rows: list[dict[str, str]]) -> list[CanonicalScore]:",
    )
    assert industrial_adapter_semantic_sha256(
        adapter_source=changed,
        contracts_source=contracts_source,
    ) != baseline
