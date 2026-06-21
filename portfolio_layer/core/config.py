from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)
ENV_BRACE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
ENV_SIMPLE_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
ENV_PERCENT_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def expand_env_vars(raw: Any) -> str:
    """Expand $VAR, %VAR%, ${VAR}, and ${VAR:-default} in one pass.

    Defaults are literal text, not a second expansion surface. Unresolved variables
    fail fast so a typo cannot become a path component like ``$BAR``.
    """
    text = str(raw)
    if "$" not in text and "%" not in text:
        return text

    literal_defaults: list[str] = []

    def replace_braced(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        value = os.environ.get(name)
        if value is not None:
            return value
        if default is not None:
            literal_defaults.append(default)
            return f"\0PORTFOLIO_DEFAULT_{len(literal_defaults) - 1}\0"
        raise ValueError(f"Unresolved environment variable in config value: {name}")

    def replace_simple(match: re.Match[str]) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            raise ValueError(f"Unresolved environment variable in config value: {name}")
        return value

    text = ENV_BRACE_RE.sub(replace_braced, text)
    text = ENV_SIMPLE_RE.sub(replace_simple, text)
    text = ENV_PERCENT_RE.sub(replace_simple, text)
    for idx, value in enumerate(literal_defaults):
        text = text.replace(f"\0PORTFOLIO_DEFAULT_{idx}\0", value)
    return text


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config YAML not found: {path}")
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load portfolio_layer config. Install package 'pyyaml'.") from exc
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
    parts = dotted_key.split(".")
    for idx, part in enumerate(parts):
        if not isinstance(cur, dict):
            parent_key = ".".join(parts[:idx]) or "<root>"
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
