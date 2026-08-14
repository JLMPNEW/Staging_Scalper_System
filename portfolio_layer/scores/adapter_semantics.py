"""Stable semantic seals for shared portfolio score adapters.

The score-adapter module serves several independent sector families. A whole-file
hash therefore couples machinery production governance to unrelated biotech or
med-device edits. This module hashes the executable AST reachable from the
industrial adapter only, plus the canonical row contracts it constructs.
"""
from __future__ import annotations

import ast
import json
from hashlib import sha256
from pathlib import Path


SEMANTIC_SEAL_VERSION = "industrial_adapter_ast_v1"
_ADAPTER_ROOTS = ("run_adapter", "_adapt_industrial_family")
_CONTRACT_ROOTS = ("CanonicalScore", "AdapterResult", "read_csv")


def _module_members(
    tree: ast.Module,
) -> tuple[dict[str, ast.AST], dict[str, ast.AST]]:
    definitions: dict[str, ast.AST] = {}
    imports: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = node
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports[alias.asname or alias.name] = node
    return definitions, imports


def _reachable_ast(source: str, roots: tuple[str, ...]) -> dict[str, object]:
    tree = ast.parse(source)
    definitions, imports = _module_members(tree)
    queue = list(roots)
    visited: set[str] = set()
    nodes: dict[str, str] = {}
    used_imports: dict[str, str] = {}
    while queue:
        name = queue.pop()
        if name in visited:
            continue
        node = definitions.get(name)
        if node is None:
            raise ValueError(f"Semantic-seal root or dependency is missing: {name}")
        visited.add(name)
        nodes[name] = ast.dump(node, annotate_fields=True, include_attributes=False)
        for child in ast.walk(node):
            if not isinstance(child, ast.Name) or not isinstance(child.ctx, ast.Load):
                continue
            dependency = child.id
            if dependency == "_ADAPTERS":
                continue
            if dependency in definitions and dependency not in visited:
                queue.append(dependency)
            elif dependency in imports:
                used_imports[dependency] = ast.dump(
                    imports[dependency],
                    annotate_fields=True,
                    include_attributes=False,
                )
    return {
        "nodes": {name: nodes[name] for name in sorted(nodes)},
        "imports": {name: used_imports[name] for name in sorted(used_imports)},
    }


def _industrial_dispatch(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    definitions, _imports = _module_members(tree)
    assignment = definitions.get("_ADAPTERS")
    value = (
        assignment.value
        if isinstance(assignment, (ast.Assign, ast.AnnAssign))
        else None
    )
    if not isinstance(value, ast.Dict):
        raise ValueError("_ADAPTERS must be a literal dictionary for semantic sealing")
    for key, target in zip(value.keys, value.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "industrial_family":
            if not isinstance(target, ast.Name):
                raise ValueError("industrial_family dispatch target must be a named function")
            return {
                "adapter": "industrial_family",
                "target": target.id,
                "target_ast": ast.dump(
                    target, annotate_fields=True, include_attributes=False
                ),
            }
    raise ValueError("industrial_family is missing from _ADAPTERS")


def industrial_adapter_semantic_payload(
    *,
    adapter_source: str | None = None,
    contracts_source: str | None = None,
) -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    adapter_path = root / "portfolio_layer" / "scores" / "adapters.py"
    contracts_path = root / "portfolio_layer" / "core" / "contracts.py"
    adapter_text = (
        adapter_source
        if adapter_source is not None
        else adapter_path.read_text(encoding="utf-8")
    )
    contracts_text = (
        contracts_source
        if contracts_source is not None
        else contracts_path.read_text(encoding="utf-8")
    )
    return {
        "version": SEMANTIC_SEAL_VERSION,
        "adapter": _reachable_ast(adapter_text, _ADAPTER_ROOTS),
        "dispatch": _industrial_dispatch(adapter_text),
        "contracts": _reachable_ast(contracts_text, _CONTRACT_ROOTS),
    }


def industrial_adapter_semantic_sha256(
    *,
    adapter_source: str | None = None,
    contracts_source: str | None = None,
) -> str:
    payload = industrial_adapter_semantic_payload(
        adapter_source=adapter_source,
        contracts_source=contracts_source,
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()
