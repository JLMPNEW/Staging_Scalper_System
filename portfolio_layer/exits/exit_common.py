from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from portfolio_layer.core.contracts import sha256_file


EXIT_SIGNAL_FIELDS = [
    "ledger_as_of",
    "signal_as_of",
    "ticker",
    "asset_category",
    "currency",
    "quantity",
    "market_value",
    "actual_weight",
    "target_weight",
    "target_gap_weight",
    "close_price",
    "cost_basis",
    "unrealized_pl",
    "unrealized_return",
    "lot_count",
    "earliest_entry_date",
    "entry_date_unknown_lots",
    "source_pipeline",
    "sector",
    "rating",
    "final_score",
    "score_confidence",
    "investable_eligible",
    "score_status",
    "holding_status",
    "exit_signal",
    "exit_priority",
    "action_hint",
    "requires_review",
    "reason",
]

TARGET_GAP_FIELDS = [
    "ledger_as_of",
    "signal_as_of",
    "target_as_of",
    "ticker",
    "in_actual",
    "in_target",
    "actual_weight",
    "target_weight",
    "target_gap_weight",
    "actual_quantity",
    "market_value",
    "score_status",
    "action_hint",
    "target_source",
]

UNSUPPORTED_FIELDS = [
    "ledger_as_of",
    "signal_as_of",
    "ticker",
    "asset_category",
    "quantity",
    "market_value",
    "unsupported_reason",
]

EXIT_ACTION_FIELDS = [
    "ledger_as_of",
    "signal_as_of",
    "ticker",
    "action",
    "exit_signal",
    "exit_priority",
    "quantity",
    "proposed_exit_fraction",
    "proposed_exit_quantity",
    "market_value",
    "notional_to_exit",
    "estimated_realized_pl",
    "requires_review",
    "reason",
]

EXIT_VALIDATION_FIELDS = ["check", "status", "detail"]

VALID_ACTIONS = {"keep", "review", "soft_exit", "hard_exit"}


def finite_float(raw: Any) -> float | None:
    try:
        if raw is None or str(raw).strip() == "":
            return None
        value = float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def f0(raw: Any) -> float:
    value = finite_float(raw)
    return 0.0 if value is None else value


def i0(raw: Any) -> int:
    value = finite_float(raw)
    return 0 if value is None else int(value)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def latest_run_on_or_before(runs_root: Path, artifact: str, as_of: str) -> str | None:
    """Return the latest run directory <= as_of containing artifact."""
    cutoff = date.fromisoformat(as_of)
    candidates: list[str] = []
    children = runs_root.iterdir() if runs_root.exists() else []
    for child in children:
        if not child.is_dir():
            continue
        try:
            run_date = date.fromisoformat(child.name)
        except ValueError:
            continue
        if run_date <= cutoff and (child / artifact).exists():
            candidates.append(child.name)
    return max(candidates) if candidates else None


def date_lag_days(left: str, right: str) -> int:
    return (date.fromisoformat(right) - date.fromisoformat(left)).days


def source_hashes(package_root: Path, folder: str, files: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in files:
        path = package_root / folder / name
        if path.exists():
            out[name] = sha256_file(path)
    return out


def score_manifest_accepts(meta: dict[str, Any]) -> bool:
    acceptance = str(meta.get("acceptance", ""))
    hard = str(meta.get("hard_gate_acceptance", ""))
    return hard == "PASS" or acceptance == "PASS"


def manifest_hash_current(meta: dict[str, Any], *, rel_name: str, path: Path) -> bool:
    files = meta.get("files") or {}
    if rel_name in files and isinstance(files[rel_name], dict):
        return files[rel_name].get("sha256") == sha256_file(path)
    provenance = meta.get("provenance_sha256") or {}
    if rel_name in provenance:
        return provenance[rel_name] == sha256_file(path)
    outputs = meta.get("outputs_sha256") or {}
    if rel_name in outputs:
        return outputs[rel_name] == sha256_file(path)
    return False


def bool_text(value: bool) -> str:
    return "1" if value else "0"
