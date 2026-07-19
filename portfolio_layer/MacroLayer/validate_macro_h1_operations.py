#!/usr/bin/env python3
"""Operational canary and checkpointing for the frozen H1 campaign.

This is deliberately separate from H1's statistical promotion evaluator. It verifies the
artifacts that a daily operator needs to trust: ledger-chain integrity, sealed evidence,
evidence/decision date parity, A1.7 status, and the production-source guard. A
NOT_PROMOTABLE candidate is operationally healthy while production remains on V1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from portfolio_layer.macro.contract import h1_promotion_status  # noqa: E402

from build_macro_h1_hybrid import (  # noqa: E402
    H1_MODEL_VERSION,
    LEDGER_COLUMNS,
    LEDGER_FILENAME,
    OUTCOMES_LEDGER_COLUMNS,
    OUTCOMES_LEDGER_FILENAME,
    read_ledger_rows,
    verify_ledger_chain,
)
from macro_raw_config import (  # noqa: E402
    cfg_get,
    configure_pipeline_logging,
    load_macro_raw_config,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path  # noqa: E402

LOGGER = logging.getLogger(__name__)

PROSPECTIVE_DIGEST_FIELDS = tuple(
    column for column in LEDGER_COLUMNS if column not in ("capture_date_utc", "prev_row_digest", "row_digest")
)
OUTCOMES_DIGEST_FIELDS = tuple(
    column for column in OUTCOMES_LEDGER_COLUMNS if column not in ("capture_date_utc", "prev_row_digest", "row_digest")
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate H1 daily operations and write immutable chain checkpoints.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--serving-db-path", type=Path, default=None)
    parser.add_argument("--portfolio-config", type=Path, default=None)
    parser.add_argument("--checkpoint-dir", type=Path, default=None)
    parser.add_argument("--status-dir", type=Path, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--layer-block", type=str, default="probability_h1")
    parser.add_argument("--no-checkpoint", action="store_true")
    parser.add_argument("--require-post-cutoff-capture", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _record(checks: list[dict[str, str]], name: str, status: str, detail: str) -> None:
    checks.append({"check": name, "status": status, "detail": detail})
    LOGGER.info("[%s] %s -- %s", status, name, detail)


def _production_source(portfolio_config: Path) -> str:
    raw = yaml.safe_load(portfolio_config.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid portfolio config object: {portfolio_config}")
    macro = raw.get("macro") or {}
    if not isinstance(macro, dict):
        raise ValueError(f"Invalid macro config block: {portfolio_config}")
    return str(macro.get("regime_source") or "").strip().lower()


def _latest_h1_decision(db_path: Path, end: str) -> dict[str, Any] | None:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT as_of_date, active_current_regime, current_top_probability,
                   current_confidence, coverage_flag
            FROM macro_regime_v2_decision_daily
            WHERE model_version = ? AND as_of_date <= ?
            ORDER BY as_of_date DESC LIMIT 1
            """,
            (H1_MODEL_VERSION, end),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _capture_lag_days(row: dict[str, Any]) -> int | None:
    try:
        as_of = date.fromisoformat(str(row.get("as_of_date") or ""))
        capture = date.fromisoformat(str(row.get("capture_date_utc") or "")[:10])
    except ValueError:
        return None
    return (capture - as_of).days


def _write_checkpoint(
    *,
    checkpoint_dir: Path,
    end: str,
    prospective_rows: list[dict[str, Any]],
    outcomes_rows: list[dict[str, Any]],
    prospective_head: str,
    outcomes_head: str,
    evidence_path: Path,
    manifest_path: Path,
) -> Path:
    manifest_sha = _sha256(manifest_path)
    payload = {
        "campaign": H1_MODEL_VERSION,
        "evidence_as_of_date": end,
        "prospective": {
            "rows": len(prospective_rows),
            "chain_head": prospective_head,
            "file_sha256": _sha256(evidence_path.parent.parent / LEDGER_FILENAME)
            if (evidence_path.parent.parent / LEDGER_FILENAME).is_file()
            else None,
        },
        "outcomes": {
            "rows": len(outcomes_rows),
            "chain_head": outcomes_head,
            "file_sha256": _sha256(evidence_path.parent.parent / OUTCOMES_LEDGER_FILENAME)
            if (evidence_path.parent.parent / OUTCOMES_LEDGER_FILENAME).is_file()
            else None,
        },
        "evidence_sha256": _sha256(evidence_path),
        "promotion_manifest_sha256": manifest_sha,
        "created_at_utc": utc_now_iso(),
    }
    filename = (
        f"{end}__p-{prospective_head[:12]}__o-{outcomes_head[:12]}__m-{manifest_sha[:12]}.json"
    )
    path = checkpoint_dir / filename
    if path.exists():
        existing = _read_json(path)
        for key in ("campaign", "evidence_as_of_date", "prospective", "outcomes", "evidence_sha256", "promotion_manifest_sha256"):
            if existing.get(key) != payload.get(key):
                raise ValueError(f"Immutable H1 checkpoint collision at {path}: field={key}")
        return path
    _atomic_write_json(path, payload)
    return path


def _overall_status(checks: list[dict[str, str]]) -> str:
    return "FAIL" if any(check["status"] == "FAIL" for check in checks) else "PASS"


def _selftest() -> None:
    assert _overall_status([{"status": "PASS"}]) == "PASS"
    assert _overall_status([{"status": "WAIT"}]) == "PASS"
    assert _overall_status([{"status": "FAIL"}]) == "FAIL"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        evidence_dir = root / "regime_h1" / "2026-07-20"
        evidence_dir.mkdir(parents=True)
        evidence = evidence_dir / "h1_promotion_evidence.json"
        manifest = evidence_dir / "h1_promotion_manifest.json"
        evidence.write_text("{}\n", encoding="utf-8")
        manifest.write_text("{}\n", encoding="utf-8")
        first = _write_checkpoint(
            checkpoint_dir=root / "checkpoints",
            end="2026-07-20",
            prospective_rows=[],
            outcomes_rows=[],
            prospective_head="H1-GENESIS",
            outcomes_head="H1-GENESIS",
            evidence_path=evidence,
            manifest_path=manifest,
        )
        second = _write_checkpoint(
            checkpoint_dir=root / "checkpoints",
            end="2026-07-20",
            prospective_rows=[],
            outcomes_rows=[],
            prospective_head="H1-GENESIS",
            outcomes_head="H1-GENESIS",
            evidence_path=evidence,
            manifest_path=manifest,
        )
        assert first == second and first.is_file()
    print("h1 operations self-test: PASS")


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return

    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = cfg_get(cfg, str(args.layer_block), default={}) or {}
    if not isinstance(layer_cfg, dict) or not layer_cfg:
        raise ValueError(f"Config block {args.layer_block!r} is missing or empty.")
    operations_cfg = cfg_get(cfg, "h1_operations", default={}) or {}
    output_root = resolve_path(config_path, str(cfg_get(layer_cfg, "output_dir", default="MacroLayer/out/regime_h1")))
    if output_root is None:
        raise ValueError("Unable to resolve H1 output root.")
    output_root = Path(output_root)
    cutoff = str(cfg_get(layer_cfg, "prospective_cutoff_date", default="2026-07-19"))
    db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)

    if args.end_date:
        end = str(args.end_date).strip()
    else:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT MAX(as_of_date) FROM macro_regime_v2_decision_daily WHERE model_version = ?",
                (H1_MODEL_VERSION,),
            ).fetchone()
            if row is None or not row[0]:
                raise ValueError("No H1 decision rows; run the H1 serving chain first.")
            end = str(row[0])
        finally:
            conn.close()

    evidence_path = output_root / end / "h1_promotion_evidence.json"
    manifest_path = output_root / end / "h1_promotion_manifest.json"
    if not evidence_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(f"Missing H1 evidence/manifest for {end}; run the H1 serving chain first.")
    evidence = _read_json(evidence_path)

    prospective_path = output_root / LEDGER_FILENAME
    outcomes_path = output_root / OUTCOMES_LEDGER_FILENAME
    prospective_rows = read_ledger_rows(prospective_path)
    outcomes_rows = read_ledger_rows(outcomes_path)
    prospective_ok, prospective_head, prospective_error = verify_ledger_chain(
        prospective_rows, PROSPECTIVE_DIGEST_FIELDS
    )
    outcomes_ok, outcomes_head, outcomes_error = verify_ledger_chain(outcomes_rows, OUTCOMES_DIGEST_FIELDS)

    checks: list[dict[str, str]] = []
    _record(
        checks,
        "prospective_ledger_chain",
        "PASS" if prospective_ok else "FAIL",
        f"rows={len(prospective_rows)} head={prospective_head} error={prospective_error}",
    )
    _record(
        checks,
        "outcomes_ledger_chain",
        "PASS" if outcomes_ok else "FAIL",
        f"rows={len(outcomes_rows)} head={outcomes_head} error={outcomes_error}",
    )

    expected_heads = {"prospective": prospective_head, "outcomes": outcomes_head}
    evidence_heads = ((evidence.get("ledger_integrity") or {}).get("chain_heads") or {})
    baseline_path = output_root / "h1_prospective_baseline.json"
    baseline = _read_json(baseline_path) if baseline_path.is_file() else {}
    baseline_heads = baseline.get("ledger_chain_heads") or {}
    _record(
        checks,
        "evidence_chain_heads_current",
        "PASS" if evidence_heads == expected_heads else "FAIL",
        f"evidence={evidence_heads} actual={expected_heads}",
    )
    _record(
        checks,
        "baseline_chain_heads_current",
        "PASS" if baseline_heads == expected_heads else "FAIL",
        f"baseline={baseline_heads} actual={expected_heads}",
    )

    sealed_path, seal_errors = h1_promotion_status(
        output_root=output_root, run_as_of=end, model_version=H1_MODEL_VERSION
    )
    non_acceptance_errors = [error for error in seal_errors if not str(error).startswith("acceptance=")]
    _record(
        checks,
        "promotion_seal_integrity",
        "PASS" if sealed_path == evidence_path and not non_acceptance_errors else "FAIL",
        f"evidence={sealed_path} errors={non_acceptance_errors}",
    )

    decision = _latest_h1_decision(db_path, end)
    decision_date = str((decision or {}).get("as_of_date") or "")
    evidence_date = str(evidence.get("evidence_as_of_date") or "")
    evidence_decision_date = str((evidence.get("latest_decision") or {}).get("as_of_date") or "")
    parity_ok = bool(decision_date) and evidence_date == decision_date == evidence_decision_date == end
    _record(
        checks,
        "evidence_decision_date_parity",
        "PASS" if parity_ok else "FAIL",
        f"requested={end} evidence={evidence_date} evidence_decision={evidence_decision_date} db={decision_date}",
    )

    a17 = evidence.get("a17_gate") or {}
    age_days = a17.get("age_days")
    max_age_days = a17.get("max_age_days")
    a17_age_ok = (
        isinstance(age_days, int)
        and isinstance(max_age_days, int)
        and 0 <= age_days <= max_age_days
    )
    a17_ok = bool(a17.get("present")) and a17.get("a17_gate_pass") is True and a17_age_ok
    _record(
        checks,
        "a17_economic_gate",
        "PASS" if a17_ok else "FAIL",
        f"present={a17.get('present')} pass={a17.get('a17_gate_pass')} "
        f"age_days={age_days}/{max_age_days}",
    )

    portfolio_config = (args.portfolio_config or (config_path.resolve().parent.parent / "config.yaml")).resolve()
    production_source = _production_source(portfolio_config)
    candidate_promotable = str(evidence.get("acceptance") or "") == "PROMOTABLE" and not seal_errors
    production_ok = production_source in {"v1", "v2"} or (
        production_source == "h1" and candidate_promotable
    )
    _record(
        checks,
        "production_source_guard",
        "PASS" if production_ok else "FAIL",
        f"production={production_source} candidate_acceptance={evidence.get('acceptance')} seal_errors={seal_errors}",
    )

    post_cutoff_rows = [row for row in prospective_rows if str(row.get("as_of_date") or "") > cutoff]
    exact_rows = [row for row in post_cutoff_rows if str(row.get("as_of_date") or "") == end]
    if end <= cutoff:
        capture_status = "FAIL" if args.require_post_cutoff_capture else "WAIT"
        capture_detail = f"end={end} has not crossed cutoff={cutoff}"
    elif not exact_rows:
        capture_status = "FAIL"
        capture_detail = f"missing prospective ledger capture for end={end}"
    else:
        lag = _capture_lag_days(exact_rows[0])
        covered = int(float(exact_rows[0].get("coverage_flag") or 0)) == 1
        capture_valid = lag is not None and 0 <= lag <= 7
        if not capture_valid:
            capture_status = "FAIL"
        elif covered:
            capture_status = "PASS"
        else:
            capture_status = "FAIL" if args.require_post_cutoff_capture else "WARN"
        capture_detail = f"date={end} lag_days={lag} covered={covered}"
    _record(checks, "post_cutoff_live_capture", capture_status, capture_detail)

    pi_now = int((evidence.get("pi_now_vs_v1") or {}).get("resolved_outcomes") or 0)
    pi_lead = int((evidence.get("pi_lead_vs_v1") or {}).get("resolved_outcomes") or 0)
    quadrant = int((evidence.get("quadrant_brier_current") or {}).get("paired_dates") or 0)
    review_detail = (
        f"stage={evidence.get('review_stage')} pi_now={pi_now}/18 pi_lead={pi_lead}/8 "
        f"quadrant={quadrant}/12; first review expected late-2027, earliest final review late-2028"
    )
    _record(checks, "promotion_review_progress", "PASS" if candidate_promotable else "WAIT", review_detail)

    status = _overall_status(checks)
    checkpoint_dir = args.checkpoint_dir
    if checkpoint_dir is None:
        checkpoint_dir = resolve_path(
            config_path,
            str(cfg_get(operations_cfg, "checkpoint_dir", default="output/h1_chain_checkpoints")),
        )
    status_dir = args.status_dir
    if status_dir is None:
        status_dir = resolve_path(config_path, str(cfg_get(operations_cfg, "status_dir", default="output/h1_operations")))
    if checkpoint_dir is None or status_dir is None:
        raise ValueError("Unable to resolve H1 operations output directories.")

    checkpoint_path: Path | None = None
    if status == "PASS" and not args.no_checkpoint:
        checkpoint_path = _write_checkpoint(
            checkpoint_dir=Path(checkpoint_dir),
            end=end,
            prospective_rows=prospective_rows,
            outcomes_rows=outcomes_rows,
            prospective_head=prospective_head,
            outcomes_head=outcomes_head,
            evidence_path=evidence_path,
            manifest_path=manifest_path,
        )

    payload = {
        "stage": "h1_daily_operations",
        "campaign": H1_MODEL_VERSION,
        "as_of_date": end,
        "acceptance": status,
        "candidate_acceptance": evidence.get("acceptance"),
        "production_regime_source": production_source,
        "checks": checks,
        "chain_heads": expected_heads,
        "row_counts": {"prospective": len(prospective_rows), "outcomes": len(outcomes_rows)},
        "checkpoint_path": str(checkpoint_path) if checkpoint_path else "",
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    status_dir = Path(status_dir)
    immutable_status = status_dir / "history" / (
        f"{end}__{_sha256(manifest_path)[:12]}__{prospective_head[:12]}__{outcomes_head[:12]}.json"
    )
    if not immutable_status.exists():
        _atomic_write_json(immutable_status, payload)
    _atomic_write_json(status_dir / "latest_status.json", payload)
    LOGGER.info("H1 OPERATIONS: %s -> %s", status, status_dir / "latest_status.json")
    if status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
