"""Static and configuration independence checks."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from basic_materials.core.config import BasicMaterialsConfig
from basic_materials.core.input_manifest import validate_authoritative_input
from basic_materials.core.source_registry import load_source_registry
from basic_materials.core.universe import load_universe_policy


FORBIDDEN_TOP_LEVEL_IMPORTS = {
    "biotech",
    "biotech_index",
    "consumer_defensive",
    "consumer_discretionary",
    "financial_services",
    "healthcare",
    "industrials",
    "machinery",
    "med_dev",
    "medical_devices",
    "technology",
}


@dataclass(frozen=True)
class IndependenceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class IndependenceReport:
    passed: bool
    checks: tuple[IndependenceCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [asdict(check) for check in self.checks],
        }


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def run_independence_checks(config: BasicMaterialsConfig) -> IndependenceReport:
    checks: list[IndependenceCheck] = []

    python_files = sorted(
        path
        for path in config.package_root.rglob("*.py")
        if not any(part in {".scratch", "__pycache__"} for part in path.parts)
    )
    forbidden_findings: list[str] = []
    syntax_findings: list[str] = []
    for path in python_files:
        try:
            forbidden = sorted(_import_roots(path) & FORBIDDEN_TOP_LEVEL_IMPORTS)
        except SyntaxError as exc:
            syntax_findings.append(f"{path.relative_to(config.package_root)}:{exc.lineno}: {exc.msg}")
            continue
        if forbidden:
            forbidden_findings.append(
                f"{path.relative_to(config.package_root)} imports {', '.join(forbidden)}"
            )
    checks.append(
        IndependenceCheck(
            name="python_syntax",
            passed=not syntax_findings,
            detail="all package Python files parse" if not syntax_findings else "; ".join(syntax_findings),
        )
    )
    checks.append(
        IndependenceCheck(
            name="forbidden_sector_imports",
            passed=not forbidden_findings,
            detail=(
                f"no forbidden imports across {len(python_files)} Python files"
                if not forbidden_findings
                else "; ".join(forbidden_findings)
            ),
        )
    )

    expected_output = (config.repository_root / "output" / "basic_materials").resolve(strict=False)
    checks.append(
        IndependenceCheck(
            name="owned_output_root",
            passed=config.paths.output_root == expected_output,
            detail=str(config.paths.output_root),
        )
    )
    checks.append(
        IndependenceCheck(
            name="owned_database_name",
            passed=config.paths.database.name.lower() == "basic_materials.sqlite",
            detail=str(config.paths.database),
        )
    )
    checks.append(
        IndependenceCheck(
            name="promotion_fail_closed",
            passed=(
                config.model.promotion_state == "shadow_monitor"
                and not config.model.portfolio_candidate_gate
                and not config.model.oos_score_valid_flag
            ),
            detail="shadow_monitor; portfolio_candidate_gate=0; oos_score_valid_flag=0",
        )
    )

    manifest = validate_authoritative_input(
        config.paths.authoritative_input_manifest,
        config.paths.universe_csv,
    )
    checks.append(
        IndependenceCheck(
            name="authoritative_input_manifest",
            passed=True,
            detail=f"{manifest.row_count} rows; sha256={manifest.sha256}",
        )
    )
    policy = load_universe_policy(config.paths.universe_policy)
    checks.append(
        IndependenceCheck(
            name="package_owned_universe_policy",
            passed=policy.expected_current_rows == manifest.row_count,
            detail=f"{policy.policy_version}; {len(policy.cohorts)} cohorts",
        )
    )
    registry = load_source_registry(config.paths.source_registry)
    checks.append(
        IndependenceCheck(
            name="package_owned_source_registry",
            passed=any(source.source_id == policy.source_id and source.active for source in registry.sources),
            detail=f"{registry.version}; {len(registry.sources)} registered sources",
        )
    )
    return IndependenceReport(
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
    )

