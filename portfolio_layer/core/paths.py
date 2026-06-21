from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from portfolio_layer.core.config import cfg_get, resolve_path


# core/paths.py -> core -> portfolio_layer (package) -> Staging_Scalper_System (project)
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved filesystem locations for the portfolio layer, all rooted in Staging."""

    config_path: Path
    base_dir: Path
    database_path: Path
    output_dir: Path
    cache_dir: Path
    macro_serving_db_path: Path


def resolve_runtime_paths(config: dict[str, Any], config_path: Path) -> RuntimePaths:
    """Resolve every path the layer needs from config, relative to the config file."""
    base_dir = config_path.parent
    return RuntimePaths(
        config_path=config_path,
        base_dir=base_dir,
        database_path=resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir),
        output_dir=resolve_path(cfg_get(config, "paths.output_dir", "output"), base_dir=base_dir),
        cache_dir=resolve_path(cfg_get(config, "paths.cache_dir", "output/cache"), base_dir=base_dir),
        macro_serving_db_path=resolve_path(
            cfg_get(config, "paths.macro_serving_db_path", "macro/macro_serving.sqlite"),
            base_dir=base_dir,
        ),
    )


def ensure_not_prod_path(path: Path, *, label: str = "path") -> Path:
    """Resolve a path and reject accidental writes into the PROD tree."""
    resolved = path.expanduser().resolve()
    if "prod_scalper_system" in str(resolved).lower():
        raise ValueError(f"Refusing to use {label} in the PROD tree: {resolved}")
    return resolved


def resolve_database_path(paths: RuntimePaths, override: Path | None = None) -> Path:
    """Resolve the portfolio-layer DB path, honoring CLI override and PROD guard."""
    return ensure_not_prod_path(override if override is not None else paths.database_path, label="database path")


def is_within(path: Path, root: Path) -> bool:
    """True when ``path`` resolves inside ``root`` (used by the independence gate)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
