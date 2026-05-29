from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)
ENV_DEFAULT_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
DEFAULT_NEUTRAL_SCORE = 50.0


def expand_env_vars(raw: Any) -> str:
    """Expand $VAR, ${VAR}, and ${VAR:-default} in config values."""
    text = str(raw)
    if "$" not in text and "%" not in text:
        return text

    def replace_default(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        value = os.environ.get(name)
        if value is not None:
            return value
        return default if default is not None else match.group(0)

    return os.path.expandvars(ENV_DEFAULT_RE.sub(replace_default, text))


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config YAML not found: {path}")
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load med_devices config. Install package 'pyyaml'.") from exc
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Config YAML root must be a mapping: {path}")
    return payload


def cfg_get(config: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    cur: Any = config
    for idx, part in enumerate(dotted_key.split(".")):
        if not isinstance(cur, dict):
            parent_key = ".".join(dotted_key.split(".")[:idx]) or "<root>"
            LOGGER.warning(
                "Config key %s expected mapping at %s but found %s; using default",
                dotted_key,
                parent_key,
                type(cur).__name__,
            )
            return default
        if part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve_path(raw: Any, *, base_dir: Path) -> Path:
    if raw is None or str(raw).strip() == "":
        raise ValueError("Path config value is empty")
    path = Path(expand_env_vars(raw)).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()
