"""Stable semantic seal for machinery's portfolio-optimizer contract.

Machinery production governance must fail closed when shared cap, sizing, or
post-processing behavior changes. It must not be invalidated by edits confined
to another sector's overlay inside the same runner. The digest below combines
the optimizer-core call graph with only the runner statements that implement
group caps, equal-weight sleeves, constrained sizing, and their published
diagnostics.
"""

from __future__ import annotations

import ast
import json
from hashlib import sha256
from pathlib import Path


SEMANTIC_SEAL_VERSION = "machinery_optimizer_ast_v1"
_CORE_ROOTS = (
    "constraint_aware_invested_gross",
    "finalize_long_only_weights",
    "finalize_with_group_caps",
    "maximum_investable_gross",
    "rescale_group_caps_for_invested_gross",
    "snap_rounded_weights",
    "solve_long_only_mv",
    "weight_sensitivity_band",
)
_RUNNER_CONTRACT_SYMBOLS = frozenset(
    {
        "constraint_aware_invested_gross",
        "equal_weight_groups",
        "finalize_long_only_weights",
        "finalize_with_group_caps",
        "fixed_equal_sleeves",
        "group_caps",
        "maximum_investable_gross",
        "rescale_group_caps_for_invested_gross",
        "scope_cap_summary",
        "scope_caps_cfg",
        "sector_cap_summary",
        "sector_caps_cfg",
        "snap_rounded_weights",
        "solve_group_caps",
        "solve_long_only_mv",
        "weight_sensitivity_band",
    }
)
_REQUIRED_RUNNER_SYMBOLS = frozenset(
    {
        "constraint_aware_invested_gross",
        "equal_weight_groups",
        "finalize_with_group_caps",
        "fixed_equal_sleeves",
        "group_caps",
        "maximum_investable_gross",
        "scope_caps_cfg",
        "sector_caps_cfg",
        "snap_rounded_weights",
        "solve_long_only_mv",
        "weight_sensitivity_band",
    }
)


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
            raise ValueError(f"Optimizer semantic root or dependency is missing: {name}")
        visited.add(name)
        nodes[name] = ast.dump(node, annotate_fields=True, include_attributes=False)
        for child in ast.walk(node):
            if not isinstance(child, ast.Name) or not isinstance(child.ctx, ast.Load):
                continue
            dependency = child.id
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


def _runner_contract_ast(source: str) -> dict[str, object]:
    tree = ast.parse(source)
    definitions, _imports = _module_members(tree)
    main = definitions.get("main")
    if not isinstance(main, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise ValueError("Optimizer runner must define main()")

    selected: list[str] = []
    observed: set[str] = set()
    for statement in main.body:
        symbols = {child.id for child in ast.walk(statement) if isinstance(child, ast.Name)}
        relevant = symbols & _RUNNER_CONTRACT_SYMBOLS
        if not relevant:
            continue
        observed.update(relevant)
        selected.append(ast.dump(statement, annotate_fields=True, include_attributes=False))

    missing = sorted(_REQUIRED_RUNNER_SYMBOLS - observed)
    if missing:
        raise ValueError("Optimizer runner semantic contract is incomplete: " + ",".join(missing))
    return {"main_statements": selected, "symbols": sorted(observed)}


def machinery_optimizer_semantic_payload(
    *,
    optimizer_source: str | None = None,
    runner_source: str | None = None,
) -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    optimizer_path = root / "portfolio_layer" / "optimizer" / "optimizer_core.py"
    runner_path = root / "portfolio_layer" / "optimizer" / "09_run_portfolio_optimizer.py"
    optimizer_text = optimizer_source if optimizer_source is not None else optimizer_path.read_text(encoding="utf-8")
    runner_text = runner_source if runner_source is not None else runner_path.read_text(encoding="utf-8")
    return {
        "version": SEMANTIC_SEAL_VERSION,
        "optimizer_core": _reachable_ast(optimizer_text, _CORE_ROOTS),
        "runner_contract": _runner_contract_ast(runner_text),
    }


def machinery_optimizer_semantic_sha256(
    *,
    optimizer_source: str | None = None,
    runner_source: str | None = None,
) -> str:
    payload = machinery_optimizer_semantic_payload(
        optimizer_source=optimizer_source,
        runner_source=runner_source,
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()
