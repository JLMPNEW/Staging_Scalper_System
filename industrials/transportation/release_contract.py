from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Sequence


MODEL_FAMILY = "transportation"
DEFAULT_RELEASE_NAME = "code_aligned_zero_overlay_v3"
SOURCE_SUFFIXES = {".csv", ".md", ".py", ".yaml", ".yml"}


def _source_files(root: Path, *, suffixes: set[str]) -> list[Path]:
    return sorted(
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in suffixes
        and "__pycache__" not in path.parts
    )


def required_release_source_paths(project_root: Path) -> list[str]:
    """Return the explicit live-code dependency census for transportation."""
    roots = (
        project_root / "industrials" / "transportation",
        project_root / "industrials" / "core",
        project_root / "industrials" / "scripts",
        project_root / "dedicated_parser",
    )
    paths: set[Path] = {
        (project_root / "industrials" / "config.yaml").resolve(),
        (project_root / "portfolio_layer" / "scores" / "adapters.py").resolve(),
    }
    for root in roots:
        suffixes = SOURCE_SUFFIXES if root.name == "transportation" else {".py"}
        paths.update(_source_files(root, suffixes=suffixes))
    paths.update(
        path.resolve()
        for path in (project_root / "tests" / "industrials").glob(
            "test_transportation*.py"
        )
    )
    return sorted(path.relative_to(project_root).as_posix() for path in paths)


def git_source_state(
    project_root: Path,
    required_paths: Sequence[str],
    *,
    expected_commit: str | None = None,
) -> tuple[dict[str, object], list[str]]:
    """Validate that every declared release dependency is tracked and clean."""
    errors: list[str] = []
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked_output = subprocess.run(
        ["git", "ls-files"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    tracked = {path.replace("\\", "/") for path in tracked_output}
    untracked = sorted(path for path in required_paths if path not in tracked)
    if untracked:
        errors.append(f"untracked release source paths={untracked}")
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", *required_paths],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if dirty:
        errors.append(f"uncommitted release source paths={dirty}")
    if expected_commit and head != expected_commit:
        errors.append(
            f"release commit mismatch expected={expected_commit} actual={head}"
        )
    return {
        "git_commit_sha": head,
        "expected_git_commit_sha": expected_commit or head,
        "required_path_count": len(required_paths),
        "tracked_path_count": len(required_paths) - len(untracked),
        "untracked_paths": untracked,
        "dirty_entries": dirty,
        "release_sources_committed": not errors,
    }, errors


def iter_existing_files(
    roots: Iterable[Path],
    *,
    exclude_names: set[str] | None = None,
) -> list[Path]:
    excluded = exclude_names or set()
    files: set[Path] = set()
    for root in roots:
        if root.is_file() and root.name not in excluded:
            files.add(root.resolve())
        elif root.is_dir():
            files.update(
                path.resolve()
                for path in root.rglob("*")
                if path.is_file()
                and path.name not in excluded
                and "logs" not in path.parts
                and not path.name.endswith((".stdout.log", ".stderr.log"))
            )
    return sorted(files)
