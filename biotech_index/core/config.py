from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config YAML not found: {path}")
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise RuntimeError("PyYAML is required to load biotech_index config. Install package 'pyyaml'.") from exc

    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Config YAML root must be a mapping: {path}")
    return payload


def cfg_get(config: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    cur: Any = config
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve_path(raw: Any, *, base_dir: Path) -> Path:
    if raw is None or str(raw).strip() == "":
        raise ValueError("Path config value is empty")
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def resolve_optional_path(raw: Any, *, base_dir: Path) -> Optional[Path]:
    if raw is None or str(raw).strip() == "":
        return None
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def normalize_string_list(raw: Any, default: list[str] | None = None) -> list[str]:
    if raw is None:
        return list(default or [])
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw]
    raise ValueError(f"Expected string list config value, got {type(raw).__name__}")
