#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.portfolio_live_monitor import (  # noqa: E402
    evaluate_live_monitoring_windows,
    overall_monitoring_action,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor an active biotech champion/challenger contract.")
    parser.add_argument("--active-contract", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--daily-returns-csv", type=Path, required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    required = {"date", "contract_id", "candidate_net_return", "incumbent_net_return"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"Live monitoring CSV must contain {sorted(required)}")
    return rows


def write_json_immutable(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Immutable monitoring artifact already exists: {path}")
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _number(raw: object, default: float) -> float:
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return default


def main() -> int:
    args = parse_args()
    contract_path = args.active_contract.expanduser().resolve()
    expected_sha = str(args.expected_sha256).strip().lower()
    actual_sha = sha256_file(contract_path)
    if actual_sha.lower() != expected_sha:
        raise ValueError(f"Active contract hash mismatch: expected={expected_sha} actual={actual_sha}")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if not isinstance(contract, dict) or contract.get("activation_status") != "active":
        raise ValueError("Champion/challenger monitoring requires an active contract")
    contract_id = str(contract.get("contract_id") or "").strip()
    if not contract_id:
        raise ValueError("Active contract has no contract_id")
    monitoring = contract.get("monitoring_contract") or {}
    if not isinstance(monitoring, Mapping):
        raise ValueError("Active contract monitoring_contract must be a mapping")
    triggers = monitoring.get("rollback_triggers") or {}
    if not isinstance(triggers, Mapping):
        raise ValueError("Active contract rollback_triggers must be a mapping")
    evidence = contract.get("profitability_evidence") or {}
    if not isinstance(evidence, Mapping):
        raise ValueError("Active contract profitability_evidence must be a mapping")
    asof_date = date.fromisoformat(str(args.asof))
    rows = read_csv(args.daily_returns_csv.expanduser().resolve())
    window_rows = evaluate_live_monitoring_windows(
        rows,
        asof_date=asof_date,
        windows_days=monitoring.get("review_windows_days") or (30, 60, 90),
        expected_contract_id=contract_id,
        effective_trials=max(1, int(_number(evidence.get("effective_trial_count"), 1.0))),
        min_live_paired_days=max(1, int(_number(triggers.get("min_live_paired_dates"), 20.0))),
        max_drawdown_deterioration_pct=_number(triggers.get("max_drawdown_deterioration_pct"), 5.0),
        max_daily_cvar_deterioration_pct=_number(triggers.get("max_daily_cvar_deterioration_pct"), 0.5),
    )
    status, action = overall_monitoring_action(window_rows)
    payload: dict[str, Any] = {
        "status": status,
        "action": action,
        "asof_date": asof_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract_id": contract_id,
        "active_contract_path": str(contract_path),
        "active_contract_sha256": actual_sha,
        "daily_returns_path": str(args.daily_returns_csv.expanduser().resolve()),
        "daily_returns_sha256": sha256_file(args.daily_returns_csv.expanduser().resolve()),
        "window_results": window_rows,
    }
    output_path = args.output_dir.expanduser().resolve() / f"{asof_date.isoformat()}_{contract_id}_monitoring.json"
    write_json_immutable(output_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

