#!/usr/bin/env python3
"""H1 promotion evidence under the frozen prospective contract (H1_CANDIDATE_SPEC.md + AMENDMENT 1).

ALL promotion evidence is computed from the append-only prospective ledger
(``out/regime_h1/prospective_ledger.csv``), never from rebuildable DB tables (A1.1). Only
ledger rows with ``capture_date_utc - as_of_date <= 7 calendar days`` are evidence-eligible.
The V1 comparison side is read from ``macro_probabilities_daily``; realized labels come from
the sealed ``macro_probability_v2_target`` rows and count ONLY when they resolve strictly after
the prospective cutoff (2026-07-19). Until every gate is satisfied on post-cutoff data,
acceptance is NOT_PROMOTABLE by construction.

Every evidence run also (A1.5) maintains ``h1_prospective_baseline.json`` and fails on
component drift, and (A1.6) seals ``h1_promotion_manifest.json`` hashing every artifact the
promotion decision depends on. ``macro/contract.py::h1_promotion_status`` re-verifies that seal.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

import logging

from build_macro_h1_hybrid import (
    CHAIN_GENESIS,
    ENERGY_SOURCE_MODEL,
    H1_MODEL_VERSION,
    LEDGER_COLUMNS,
    LEDGER_FILENAME,
    OUTCOMES_LEDGER_FILENAME,
    PI_LEAD_SOURCE,
    PI_NOW_SOURCE,
    _LEDGER_DIGEST_FIELDS,
    _OUTCOMES_DIGEST_FIELDS,
    _append_outcomes_ledger,
    _append_prospective_ledger,
    chain_row_digest,
    read_ledger_rows,
    verify_ledger_chain,
)
from macro_probability_v2 import regime_probabilities
from macro_raw_config import cfg_get, configure_pipeline_logging, connect_sqlite, load_macro_raw_config, resolve_path, utc_now_iso
from macro_serving_common import resolve_serving_db_path

logger = logging.getLogger(__name__)

# Realized-quadrant labels come from the sealed V2.1 independent-outcome target rows (A1.3),
# now captured into the append-only outcomes ledger (A2.1).
QUADRANT_LABEL_MODEL = ENERGY_SOURCE_MODEL  # "macro_regime_v2_1_independent_outcomes_v1"
QUADRANT_ORDER = ("expansion_disinflation", "heating_up", "slow_growth", "stagflation")

# Frozen AMENDMENT 1 constants (may not be edited after the freeze).
LEDGER_ELIGIBILITY_MAX_DAYS = 7
FIRST_REVIEW_MIN_PI_NOW = 12
FIRST_REVIEW_MIN_PI_LEAD = 4
FINAL_MIN_PI_NOW = 18
FINAL_MIN_PI_NOW_PER_CLASS = 4
FINAL_MIN_PI_LEAD = 8
FINAL_MIN_PI_LEAD_PER_CLASS = 2
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20260719
BOOTSTRAP_CI = 0.90

# AMENDMENT 2 statistical constants (A2.7). Circular block bootstrap block lengths respect the
# serial dependence of overlapping monthly PI_NOW (block 3) and quarterly PI_LEAD (block 2).
PI_NOW_BLOCK_LENGTH = 3
PI_LEAD_BLOCK_LENGTH = 2
QUADRANT_BLOCK_LENGTH = 3
QUADRANT_MIN_PAIRED_OUTCOMES = 12
QUADRANT_BRIER_CI_MAX_UPPER = 0.005

# A2.5 A1.7 gate consumption.
A17_GATE_RELPATH = ("output", "h1_walkforward", "latest_a17_gate.json")
A17_GATE_MAX_AGE_DAYS = 400

# A1.5/A2.4 drift-guard inputs. Builders sealed in the baseline drift-guard AND the manifest.
BUILDER_SOURCES = (
    "build_macro_probabilities_v2.py",
    "macro_probability_v2.py",
    "build_macro_h1_hybrid.py",
    "build_macro_probabilities.py",
    "build_macro_regime_v2_decision.py",
    "validate_macro_h1_promotion.py",
)
# Sources outside MacroLayer (resolved from portfolio_root), drift-guarded + sealed (A2.4).
PORTFOLIO_BUILDER_SOURCES = ("backtest/16d_run_h1_v1_regime_arms.py",)
CONFIG_BLOCKS = ("probability_h1", "probability_v2_1", "probability_v2_2", "probability_layer", "regime_layer")
BASELINE_FILENAME = "h1_prospective_baseline.json"
MANIFEST_FILENAME = "h1_promotion_manifest.json"
EVIDENCE_FILENAME = "h1_promotion_evidence.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate H1 prospective promotion evidence (fail-closed).")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--serving-db-path", type=Path, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--layer-block", type=str, default="probability_h1")
    parser.add_argument(
        "--initialize-baseline",
        action="store_true",
        help="Explicitly create the immutable H1 drift baseline when none exists; the initialization run is not promotable.",
    )
    parser.add_argument("--selftest", action="store_true", help="Run the in-memory selftest and exit.")
    return parser.parse_args()


# --------------------------------------------------------------------------------------------
# Ledger + label helpers
# --------------------------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if np.isfinite(parsed) else None


def _capture_to_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _read_ledger(ledger_path: Path) -> list[dict[str, Any]]:
    """Read raw ledger rows in file order (no filtering)."""
    return read_ledger_rows(ledger_path)


def _labels_from_outcomes_ledger(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """A2.1: {component: {predictor_as_of_date: label_value}} from the outcomes ledger.

    First-write-wins on (component, predictor_as_of_date); the ledger holds only post-cutoff
    resolved labels by construction, so no live label read occurs in the gate math.
    """
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        component = str(row.get("component") or "").strip()
        as_of = str(row.get("predictor_as_of_date") or "").strip()
        label = _to_float(row.get("label_value"))
        if not component or not as_of or label is None:
            continue
        bucket = out.setdefault(component, {})
        if as_of in bucket:
            continue  # first-write-wins
        bucket[as_of] = int(round(label))
    return out


def _eligible_ledger(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """First-write-wins, live-capture-eligible ledger keyed by as_of_date (A1.1).

    A row is eligible only when ``0 <= capture_date_utc - as_of_date <= 7`` calendar days.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        as_of_text = str(row.get("as_of_date") or "").strip()
        if not as_of_text or as_of_text in out:
            continue  # first-write-wins: keep the first capture of each as_of_date
        try:
            as_of = date.fromisoformat(as_of_text)
        except ValueError:
            continue
        capture = _capture_to_date(row.get("capture_date_utc"))
        if capture is None:
            continue
        lag = (capture - as_of).days
        if lag < 0 or lag > LEDGER_ELIGIBILITY_MAX_DAYS:
            continue
        out[as_of_text] = row
    return out


# --------------------------------------------------------------------------------------------
# A2.7 seeded circular block bootstrap (replaces the IID bootstrap)
# --------------------------------------------------------------------------------------------
def _paired_block_bootstrap_ci(
    diffs: np.ndarray,
    *,
    block_length: int,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
    ci: float = BOOTSTRAP_CI,
) -> tuple[float | None, float | None]:
    """Seeded CIRCULAR BLOCK bootstrap CI on an ordered per-outcome difference series (A2.7).

    Blocks of ``block_length`` consecutive outcomes wrap around the series end, preserving local
    serial dependence. The series MUST be passed in chronological (as_of_date) order.
    """
    values = np.asarray(diffs, dtype=float)
    n = int(values.shape[0])
    if n == 0:
        return None, None
    block = max(1, int(block_length))
    n_blocks = int(np.ceil(n / block))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(int(resamples), n_blocks))
    # Circular block offsets: each block contributes `block` consecutive (mod n) indices.
    offsets = np.arange(block)
    means = np.empty(int(resamples), dtype=float)
    for r in range(int(resamples)):
        idx = (starts[r][:, None] + offsets[None, :]).reshape(-1) % n
        means[r] = values[idx[:n]].mean()
    low = float(np.percentile(means, (1.0 - ci) / 2.0 * 100.0))
    high = float(np.percentile(means, (1.0 + ci) / 2.0 * 100.0))
    return low, high


# --------------------------------------------------------------------------------------------
# A1.2/A2.1 component superiority (ledger H1 side vs frozen V1 comparator), block-bootstrap CI
# --------------------------------------------------------------------------------------------
def _component_superiority_ledger(
    *,
    ledger: dict[str, dict[str, Any]],
    labels: dict[str, int],
    h1_field: str,
    v1_field: str,
    block_length: int,
) -> dict[str, Any]:
    """Paired post-cutoff Brier comparison for one inflation cell: H1 vs V1, BOTH from the ledger.

    Labels come from the outcomes ledger; the V1 comparator probability is the frozen
    ``v1_field`` column captured at build time. No live DB read occurs here (A2.1).
    """
    ys: list[float] = []
    p_h1: list[float] = []
    p_v1: list[float] = []
    for as_of in sorted(ledger):
        row = ledger[as_of]
        if int(_to_float(row.get("coverage_flag")) or 0) != 1:
            continue
        ph = _to_float(row.get(h1_field))
        pv = _to_float(row.get(v1_field))
        if ph is None or pv is None or as_of not in labels:
            continue
        ys.append(float(labels[as_of]))
        p_h1.append(ph)
        p_v1.append(pv)
    n = len(ys)
    if n == 0:
        return {
            "resolved_outcomes": 0,
            "class_counts": {"0": 0, "1": 0},
            "brier_h1": None,
            "brier_v1": None,
            "brier_improvement": None,
            "bootstrap_block_length": int(block_length),
            "bootstrap_ci_low": None,
            "bootstrap_ci_high": None,
            "bootstrap_ci_excludes_zero": False,
        }
    y = np.asarray(ys)
    ph = np.asarray(p_h1)
    pv = np.asarray(p_v1)
    brier_h1 = (ph - y) ** 2
    brier_v1 = (pv - y) ** 2
    improvement_per_outcome = brier_v1 - brier_h1  # positive = H1 better
    ci_low, ci_high = _paired_block_bootstrap_ci(improvement_per_outcome, block_length=block_length)
    excludes_zero = ci_low is not None and ci_high is not None and not (ci_low <= 0.0 <= ci_high)
    return {
        "resolved_outcomes": n,
        "class_counts": {"0": int((y == 0.0).sum()), "1": int((y == 1.0).sum())},
        "brier_h1": float(brier_h1.mean()),
        "brier_v1": float(brier_v1.mean()),
        "brier_improvement": float(improvement_per_outcome.mean()),
        "bootstrap_block_length": int(block_length),
        "bootstrap_ci_low": ci_low,
        "bootstrap_ci_high": ci_high,
        "bootstrap_ci_excludes_zero": bool(excludes_zero),
    }


# --------------------------------------------------------------------------------------------
# A1.3 multiclass quadrant Brier
# --------------------------------------------------------------------------------------------
def _realized_quadrant_index(growth_label: int, inflation_label: int) -> int:
    """Quadrant index (QUADRANT_ORDER) implied by binary growth/inflation labels."""
    g = int(growth_label)
    pi = int(inflation_label)
    if g == 1 and pi == 0:
        return 0  # EXPANSION_DISINFLATION
    if g == 1 and pi == 1:
        return 1  # HEATING_UP
    if g == 0 and pi == 0:
        return 2  # SLOW_GROWTH
    return 3  # STAGFLATION (g==0, pi==1)


def _v1_quadrant_vector(growth_probability: float, inflation_probability: float) -> list[float]:
    quad = regime_probabilities(growth_probability, inflation_probability)
    return [float(quad[name]) for name in QUADRANT_ORDER]


def _multiclass_brier(prob_vector: list[float], onehot: list[float]) -> float:
    return float(sum((p - o) ** 2 for p, o in zip(prob_vector, onehot)))


def _quadrant_brier_side(
    *,
    ledger: dict[str, dict[str, Any]],
    growth_labels: dict[str, int],
    inflation_labels: dict[str, int],
    v1_growth_field: str,
    v1_inflation_field: str,
    prefix: str,
    block_length: int = QUADRANT_BLOCK_LENGTH,
) -> dict[str, Any]:
    """Paired multiclass Brier for one horizon (current/next). BOTH H1 and V1 come from the
    ledger (A2.1): the V1 quadrant is recomputed from the frozen V1 comparator columns. Adds a
    paired circular block-bootstrap 90% CI on the (H1 - V1) per-date Brier difference (A2.7)."""
    fields = [f"{prefix}_{suffix}" for suffix in QUADRANT_ORDER]
    paired_dates: list[str] = []
    brier_h1_series: list[float] = []
    brier_v1_series: list[float] = []
    for as_of in sorted(ledger):
        row = ledger[as_of]
        if int(_to_float(row.get("coverage_flag")) or 0) != 1:
            continue
        if as_of not in growth_labels or as_of not in inflation_labels:
            continue
        v1_g = _to_float(row.get(v1_growth_field))
        v1_pi = _to_float(row.get(v1_inflation_field))
        if v1_g is None or v1_pi is None:
            continue
        h1_vec = [_to_float(row.get(field)) for field in fields]
        if any(value is None for value in h1_vec):
            continue
        h1_vector = [float(value) for value in h1_vec if value is not None]
        index = _realized_quadrant_index(growth_labels[as_of], inflation_labels[as_of])
        onehot = [0.0, 0.0, 0.0, 0.0]
        onehot[index] = 1.0
        v1_vector = _v1_quadrant_vector(v1_g, v1_pi)
        brier_h1_series.append(_multiclass_brier(h1_vector, onehot))
        brier_v1_series.append(_multiclass_brier(v1_vector, onehot))
        paired_dates.append(as_of)
    n = len(paired_dates)
    if n == 0:
        return {
            "paired_dates": 0, "brier_h1": None, "brier_v1": None, "h1_not_worse": False,
            "bootstrap_block_length": int(block_length), "diff_ci_low": None, "diff_ci_high": None,
            "diff_ci_upper_within_tol": False,
        }
    h1_arr = np.asarray(brier_h1_series)
    v1_arr = np.asarray(brier_v1_series)
    diff = h1_arr - v1_arr  # (H1 - V1); <= 0 means H1 no worse
    ci_low, ci_high = _paired_block_bootstrap_ci(diff, block_length=block_length)
    upper_ok = ci_high is not None and ci_high <= QUADRANT_BRIER_CI_MAX_UPPER
    return {
        "paired_dates": n,
        "brier_h1": float(h1_arr.mean()),
        "brier_v1": float(v1_arr.mean()),
        "h1_not_worse": bool(h1_arr.mean() <= v1_arr.mean()),
        "bootstrap_block_length": int(block_length),
        "diff_ci_low": ci_low,
        "diff_ci_high": ci_high,
        "diff_ci_upper_within_tol": bool(upper_ok),
    }


# --------------------------------------------------------------------------------------------
# A1.4/A2.7 coverage over the system's own sealed calendar
# --------------------------------------------------------------------------------------------
def _coverage_window(ledger: dict[str, dict[str, Any]], *, cutoff: str, end: str) -> dict[str, Any]:
    """Post-cutoff coverage over the distinct as_of_dates PRESENT IN THE LEDGER inside the
    [cutoff+1, end - 5 business days] window (A2.7) - the system's own sealed calendar, not numpy
    weekday arithmetic. The two window bounds are still computed with business-day offsets."""
    window_start = str(np.busday_offset(np.datetime64(cutoff, "D") + np.timedelta64(1, "D"), 0, roll="forward"))
    window_end = str(np.busday_offset(np.datetime64(end, "D"), -5, roll="backward"))
    if window_end < window_start:
        return {"total": 0, "covered": 0, "fraction": 0.0, "window_start": window_start, "window_end": window_end}
    dates_in_window = sorted(day for day in ledger if window_start <= day <= window_end)
    covered = sum(
        1 for day in dates_in_window if int(_to_float(ledger[day].get("coverage_flag")) or 0) == 1
    )
    total = len(dates_in_window)
    fraction = (covered / total) if total else 0.0
    return {
        "total": total,
        "covered": covered,
        "fraction": round(fraction, 6),
        "window_start": window_start,
        "window_end": window_end,
    }


# --------------------------------------------------------------------------------------------
# A2.5 A1.7 economic-non-inferiority gate consumption
# --------------------------------------------------------------------------------------------
def _a17_gate_path(macro_root: Path) -> Path:
    return macro_root.parent.joinpath(*A17_GATE_RELPATH)


def _evaluate_a17_gate(macro_root: Path, *, evidence_end: str) -> dict[str, Any]:
    """Read the sealed A1.7 gate and return reasons (a17_gate_missing/stale/fail) + a summary."""
    gate_path = _a17_gate_path(macro_root)
    summary: dict[str, Any] = {"path": str(gate_path)}
    reasons: list[str] = []
    if not gate_path.is_file():
        return {"reasons": ["a17_gate_missing"], "summary": {**summary, "present": False}}
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"reasons": ["a17_gate_missing"], "summary": {**summary, "present": False, "error": type(exc).__name__}}
    generated_at = str(gate.get("generated_at") or "")
    gate_pass = bool(gate.get("a17_gate_pass") is True)
    gate_date = _capture_to_date(generated_at)
    try:
        evidence_date = date.fromisoformat(str(evidence_end)[:10])
    except ValueError:
        evidence_date = None
    age_days: int | None = None
    if gate_date is not None and evidence_date is not None:
        age_days = abs((evidence_date - gate_date).days)
    if gate_date is None or evidence_date is None or age_days is None or age_days > A17_GATE_MAX_AGE_DAYS:
        reasons.append("a17_gate_stale")
    if not gate_pass:
        reasons.append("a17_gate_fail")
    summary.update(
        {
            "present": True,
            "a17_gate_pass": gate_pass,
            "generated_at": generated_at,
            "age_days": age_days,
            "max_age_days": A17_GATE_MAX_AGE_DAYS,
        }
    )
    return {"reasons": reasons, "summary": summary}


# --------------------------------------------------------------------------------------------
# A1.5 component drift baseline
# --------------------------------------------------------------------------------------------
def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(sql, params):
        line = "\x1f".join("" if value is None else str(value) for value in row)
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _component_pre_cutoff_digests(conn: sqlite3.Connection, component_end_date: str) -> dict[str, str]:
    return {
        "v1_growth": _digest_query(
            conn,
            """
            SELECT as_of_date, probability_key, probability_value, coverage_flag
            FROM macro_probabilities_daily
            WHERE probability_key IN ('P_G_NOW', 'P_G_LEAD') AND as_of_date <= ?
            ORDER BY as_of_date, probability_key
            """,
            (component_end_date,),
        ),
        "pi_now": _digest_query(
            conn,
            """
            SELECT as_of_date, probability_value, coverage_flag
            FROM macro_probability_v2_daily
            WHERE model_version = ? AND probability_key = ? AND as_of_date <= ?
            ORDER BY as_of_date
            """,
            (PI_NOW_SOURCE[0], PI_NOW_SOURCE[1], component_end_date),
        ),
        "pi_lead": _digest_query(
            conn,
            """
            SELECT as_of_date, probability_value, coverage_flag
            FROM macro_probability_v2_daily
            WHERE model_version = ? AND probability_key = ? AND as_of_date <= ?
            ORDER BY as_of_date
            """,
            (PI_LEAD_SOURCE[0], PI_LEAD_SOURCE[1], component_end_date),
        ),
        "energy": _digest_query(
            conn,
            """
            SELECT as_of_date, energy_shock_score, energy_shock_flag
            FROM macro_regime_v2_daily
            WHERE model_version = ? AND as_of_date <= ?
            ORDER BY as_of_date
            """,
            (ENERGY_SOURCE_MODEL, component_end_date),
        ),
    }


def _config_blocks_sha256(cfg: dict[str, Any]) -> str:
    """Canonical serialization sha of the sealed config blocks (A2.4)."""
    blocks = {name: cfg_get(cfg, name, default={}) for name in CONFIG_BLOCKS}
    return hashlib.sha256(
        json.dumps(blocks, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _compute_baseline(
    conn: sqlite3.Connection,
    *,
    macro_root: Path,
    portfolio_root: Path,
    cfg: dict[str, Any],
    cutoff: str,
    component_end_date: str,
) -> dict[str, Any]:
    builders = {name: _file_sha256(macro_root / name) for name in BUILDER_SOURCES}
    portfolio_builders = {name: _file_sha256(portfolio_root / name) for name in PORTFOLIO_BUILDER_SOURCES}
    return {
        "cutoff": cutoff,
        "component_baseline_end_date": component_end_date,
        "builder_sha256": builders,
        "portfolio_builder_sha256": portfolio_builders,
        "config_blocks_sha256": _config_blocks_sha256(cfg),
        "component_pre_cutoff_digests": _component_pre_cutoff_digests(conn, component_end_date),
    }


def _baseline_drift(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Drift against the sealed baseline. Ledger chain-head fields are advanced monotonically and
    are deliberately NOT part of drift (see _verify_and_advance_chains)."""
    drift: list[str] = []
    if str(baseline.get("cutoff")) != str(current.get("cutoff")):
        drift.append("cutoff")
    if str(baseline.get("component_baseline_end_date")) != str(current.get("component_baseline_end_date")):
        drift.append("component_baseline_end_date")
    if baseline.get("config_blocks_sha256") != current.get("config_blocks_sha256"):
        drift.append("config_blocks")
    for field in ("builder_sha256", "portfolio_builder_sha256"):
        base = baseline.get(field) or {}
        for name, value in current.get(field, {}).items():
            if base.get(name) != value:
                drift.append(f"builder:{name}")
    base_components = baseline.get("component_pre_cutoff_digests") or {}
    for name, value in current.get("component_pre_cutoff_digests", {}).items():
        if base_components.get(name) != value:
            drift.append(f"component:{name}")
    return drift


# --------------------------------------------------------------------------------------------
# A2.2 ledger chain integrity + monotonic head advance
# --------------------------------------------------------------------------------------------
def _chain_digests(rows: list[dict[str, Any]], digest_fields: tuple[str, ...]) -> list[str]:
    prev = CHAIN_GENESIS
    out: list[str] = []
    for row in rows:
        prev = chain_row_digest(row, digest_fields, prev)
        out.append(prev)
    return out


def _head_extends(stored_head: str | None, rows: list[dict[str, Any]], digest_fields: tuple[str, ...]) -> bool:
    """True if the sealed head still appears in the recomputed chain (append-only continuity)."""
    if not stored_head or stored_head == CHAIN_GENESIS:
        return True
    return stored_head in _chain_digests(rows, digest_fields)


def _verify_and_advance_chains(
    *,
    prospective_rows: list[dict[str, Any]],
    outcomes_rows: list[dict[str, Any]],
    stored_heads: dict[str, str] | None,
) -> tuple[bool, dict[str, str]]:
    """Verify BOTH ledger chains and their continuity vs the sealed heads. Returns (ok, heads)."""
    p_ok, p_head, _p_err = verify_ledger_chain(prospective_rows, _LEDGER_DIGEST_FIELDS)
    o_ok, o_head, _o_err = verify_ledger_chain(outcomes_rows, _OUTCOMES_DIGEST_FIELDS)
    heads = {"prospective": p_head, "outcomes": o_head}
    if not p_ok or not o_ok:
        return False, heads
    if stored_heads is not None:
        if not _head_extends(stored_heads.get("prospective"), prospective_rows, _LEDGER_DIGEST_FIELDS):
            return False, heads
        if not _head_extends(stored_heads.get("outcomes"), outcomes_rows, _OUTCOMES_DIGEST_FIELDS):
            return False, heads
    return True, heads


# --------------------------------------------------------------------------------------------
# A1.6 sealed manifest
# --------------------------------------------------------------------------------------------
def _sha_if_present(path: Path) -> str | None:
    return _file_sha256(path) if path.is_file() else None


def _write_manifest(
    *,
    evidence_dir: Path,
    output_root: Path,
    macro_root: Path,
    portfolio_root: Path,
    cfg: dict[str, Any],
    end: str,
    cutoff: str,
) -> Path:
    """Seal a manifest hashing every artifact the promotion decision depends on (A1.6 + A2.4).

    Hashes are grouped by FOUR anchors that ``macro/contract.py`` can rederive from the manifest
    location alone: the evidence date dir, the ledger/baseline output root, the MacroLayer source
    root, and the portfolio-layer root (16d source + A1.7 gate). A null hash means the file was
    intentionally absent at seal time; verification requires the same present/absent state and
    matching bytes.
    """
    evidence_dir_names = ["h1_promotion_evidence.json", "h1_hybrid_manifest.json", "h1_hybrid_validation.json"]
    evidence_dir_names.extend(sorted(p.name for p in evidence_dir.glob("macro_regime_v2_decision_*")))
    evidence_dir_hashes = {name: _sha_if_present(evidence_dir / name) for name in evidence_dir_names}
    output_root_hashes = {
        LEDGER_FILENAME: _sha_if_present(output_root / LEDGER_FILENAME),
        OUTCOMES_LEDGER_FILENAME: _sha_if_present(output_root / OUTCOMES_LEDGER_FILENAME),
        BASELINE_FILENAME: _sha_if_present(output_root / BASELINE_FILENAME),
    }
    macro_root_names = ["H1_CANDIDATE_SPEC.md", "config_macro_raw.yaml", *BUILDER_SOURCES]
    macro_root_hashes = {name: _sha_if_present(macro_root / name) for name in macro_root_names}
    portfolio_root_names = [*PORTFOLIO_BUILDER_SOURCES, "/".join(A17_GATE_RELPATH)]
    portfolio_root_hashes = {name: _sha_if_present(portfolio_root / name) for name in portfolio_root_names}

    manifest = {
        "model_version": H1_MODEL_VERSION,
        "spec": "H1_CANDIDATE_SPEC.md",
        "evidence_as_of_date": end,
        "prospective_cutoff_date": cutoff,
        "config_block_sha256": _config_blocks_sha256(cfg),
        "component_model_versions": {
            "P_G_NOW": "v1_macro_probabilities_daily",
            "P_G_LEAD": "v1_macro_probabilities_daily",
            "P_PI_NOW": PI_NOW_SOURCE[0],
            "P_PI_LEAD": PI_LEAD_SOURCE[0],
            "energy": ENERGY_SOURCE_MODEL,
        },
        "anchors": {
            "evidence_dir": evidence_dir_hashes,
            "output_root": output_root_hashes,
            "macro_root": macro_root_hashes,
            "portfolio_root": portfolio_root_hashes,
        },
        "created_at_utc": utc_now_iso(),
    }
    manifest_path = evidence_dir / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest_path


# --------------------------------------------------------------------------------------------
# Gate composition
# --------------------------------------------------------------------------------------------
def _component_final_reasons(tag: str, evidence: dict[str, Any], *, min_outcomes: int, min_per_class: int) -> list[str]:
    reasons: list[str] = []
    n = int(evidence["resolved_outcomes"])
    if n < min_outcomes:
        reasons.append(f"{tag}_resolved_outcomes={n}<{min_outcomes}")
        return reasons  # class/CI reasons are meaningless below the sample floor
    counts = evidence["class_counts"]
    if int(counts["0"]) < min_per_class or int(counts["1"]) < min_per_class:
        reasons.append(f"{tag}_class_counts={counts['0']}/{counts['1']}<{min_per_class}")
    improvement = evidence["brier_improvement"]
    if not (improvement is not None and improvement > 0.0):
        reasons.append(f"{tag}_brier_improvement={improvement}<=0")
    elif not evidence["bootstrap_ci_excludes_zero"]:
        reasons.append(
            f"{tag}_bootstrap_ci=[{evidence['bootstrap_ci_low']},{evidence['bootstrap_ci_high']}]_includes_0"
        )
    return reasons


def _review_stage(pi_now: dict[str, Any], pi_lead: dict[str, Any], final_reasons: list[str]) -> str:
    first_ready = (
        int(pi_now["resolved_outcomes"]) >= FIRST_REVIEW_MIN_PI_NOW
        and int(pi_lead["resolved_outcomes"]) >= FIRST_REVIEW_MIN_PI_LEAD
    )
    if not final_reasons:
        return "final_promotable"
    if first_ready:
        return "first_review"
    return "pre_first_review"


def evaluate(
    conn: sqlite3.Connection,
    *,
    output_root: Path,
    macro_root: Path,
    portfolio_root: Path,
    cfg: dict[str, Any],
    cutoff: str,
    end: str,
    min_coverage: float,
    min_top: float,
    min_confidence: float,
    initialize_baseline: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    evidence_dir = output_root / end

    # Build validation gate (unchanged): the sealed hybrid build must have passed.
    validation_path = evidence_dir / "h1_hybrid_validation.json"
    validation_ok = False
    if validation_path.exists():
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        validation_ok = str(validation.get("acceptance")) == "PASS"
    if not validation_ok:
        reasons.append("h1_build_validation_not_pass")

    # Capture any NEWLY-resolved post-cutoff labels into the append-only outcomes ledger, then the
    # two ledgers are the SOLE evidence source for every probability and label (A2.1).
    ledger_path = output_root / LEDGER_FILENAME
    outcomes_ledger_path = output_root / OUTCOMES_LEDGER_FILENAME
    _append_outcomes_ledger(
        ledger_path=outcomes_ledger_path, conn=conn, cutoff=cutoff, end=end, capture_date_utc=utc_now_iso()
    )
    ledger_rows = _read_ledger(ledger_path)
    outcomes_rows = _read_ledger(outcomes_ledger_path)
    ledger = _eligible_ledger(ledger_rows)
    labels = _labels_from_outcomes_ledger(outcomes_rows)
    growth_now_labels = labels.get("growth_now", {})
    pi_now_labels = labels.get("pi_now", {})
    pi_lead_labels = labels.get("pi_lead", {})

    # A2.2 tamper-evident chain integrity + monotonic head continuity (verified BEFORE the gates).
    baseline_path = output_root / BASELINE_FILENAME
    stored_baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None
    stored_heads = (stored_baseline or {}).get("ledger_chain_heads") if stored_baseline else None
    chains_ok, chain_heads = _verify_and_advance_chains(
        prospective_rows=ledger_rows, outcomes_rows=outcomes_rows, stored_heads=stored_heads
    )
    if not chains_ok:
        reasons.append("ledger_integrity_failure")

    # A1.2/A2.1/A2.7 inflation-cell superiority (H1 vs frozen V1 comparator, both from the ledger).
    pi_now = _component_superiority_ledger(
        ledger=ledger, labels=pi_now_labels, h1_field="p_pi_now", v1_field="v1_p_pi_now",
        block_length=PI_NOW_BLOCK_LENGTH,
    )
    pi_lead = _component_superiority_ledger(
        ledger=ledger, labels=pi_lead_labels, h1_field="p_pi_lead", v1_field="v1_p_pi_lead",
        block_length=PI_LEAD_BLOCK_LENGTH,
    )
    final_reasons: list[str] = []
    final_reasons += _component_final_reasons("pi_now", pi_now, min_outcomes=FINAL_MIN_PI_NOW, min_per_class=FINAL_MIN_PI_NOW_PER_CLASS)
    final_reasons += _component_final_reasons("pi_lead", pi_lead, min_outcomes=FINAL_MIN_PI_LEAD, min_per_class=FINAL_MIN_PI_LEAD_PER_CLASS)
    reasons += final_reasons
    review_stage = _review_stage(pi_now, pi_lead, final_reasons)

    # A1.3/A2.7 multiclass current-quadrant Brier gate (labels + V1 from the ledgers). The
    # next-quadrant diagnostic is not computable under ledger-only evidence (the growth-lead label
    # is intentionally NOT captured in the outcomes ledger), so it is reported as unavailable.
    quadrant_current = _quadrant_brier_side(
        ledger=ledger, growth_labels=growth_now_labels, inflation_labels=pi_now_labels,
        v1_growth_field="v1_p_g_now", v1_inflation_field="v1_p_pi_now", prefix="current",
        block_length=QUADRANT_BLOCK_LENGTH,
    )
    quadrant_next = {
        "paired_dates": 0, "brier_h1": None, "brier_v1": None, "h1_not_worse": False,
        "note": "diagnostic not computed under ledger-only evidence (growth_lead label not captured)",
    }
    if quadrant_current["paired_dates"] < QUADRANT_MIN_PAIRED_OUTCOMES:
        reasons.append(
            f"current_quadrant_paired_outcomes={quadrant_current['paired_dates']}<{QUADRANT_MIN_PAIRED_OUTCOMES}"
        )
    else:
        if not quadrant_current["h1_not_worse"]:
            reasons.append(
                f"current_quadrant_brier_h1={quadrant_current['brier_h1']:.6f}>v1={quadrant_current['brier_v1']:.6f}"
            )
        if not quadrant_current["diff_ci_upper_within_tol"]:
            reasons.append(
                f"current_quadrant_brier_diff_ci_high={quadrant_current['diff_ci_high']}>{QUADRANT_BRIER_CI_MAX_UPPER}"
            )

    # A1.4/A2.7 coverage over the ledger's own sealed calendar in [cutoff+1, end - 5 bdays].
    coverage = _coverage_window(ledger, cutoff=cutoff, end=end)
    if coverage["total"] == 0:
        reasons.append("no_post_cutoff_business_days")
    elif coverage["fraction"] < min_coverage:
        reasons.append(f"post_cutoff_coverage={coverage['fraction']:.4f}<{min_coverage}")

    # A2.5 economic non-inferiority gate (A1.7): consume the sealed latest gate artifact.
    a17 = _evaluate_a17_gate(macro_root, evidence_end=end)
    reasons += a17["reasons"]

    # Decision-quality confidence gate (frozen gate 3) on the latest covered H1 decision. This is
    # H1's own live decision "on the latest covered date" - not a revisable historical probability.
    decision = conn.execute(
        """
        SELECT as_of_date, active_current_regime, current_top_probability, current_confidence
        FROM macro_regime_v2_decision_daily
        WHERE model_version = ? AND as_of_date <= ? AND coverage_flag = 1
        ORDER BY as_of_date DESC LIMIT 1
        """,
        (H1_MODEL_VERSION, end),
    ).fetchone()
    if decision is None:
        reasons.append("no_covered_h1_decision")
        decision_summary: dict[str, Any] = {}
    else:
        top = float(decision["current_top_probability"] or 0.0)
        confidence = float(decision["current_confidence"] or 0.0)
        decision_summary = {
            "as_of_date": str(decision["as_of_date"]),
            "current_regime": str(decision["active_current_regime"]),
            "top_probability": top,
            "confidence": confidence,
        }
        if top < min_top:
            reasons.append(f"decision_top_probability={top:.4f}<{min_top}")
        if confidence < min_confidence:
            reasons.append(f"decision_confidence={confidence:.4f}<{min_confidence}")

    # A1.5/A2.4 component drift guard + A2.2 monotonic chain-head advance against the baseline.
    component_baseline_end_date = (
        str(stored_baseline.get("component_baseline_end_date") or cutoff)
        if stored_baseline is not None
        else min(end, cutoff)
    )
    current_baseline = _compute_baseline(
        conn,
        macro_root=macro_root,
        portfolio_root=portfolio_root,
        cfg=cfg,
        cutoff=cutoff,
        component_end_date=component_baseline_end_date,
    )
    current_baseline["ledger_chain_heads"] = chain_heads
    current_baseline["ledger_row_counts"] = {"prospective": len(ledger_rows), "outcomes": len(outcomes_rows)}
    if stored_baseline is not None:
        drift = _baseline_drift(stored_baseline, current_baseline)
        if drift:
            reasons.append("component_drift")
        baseline_created = False
        # Advance the sealed heads monotonically only when integrity held (append-only continuity).
        if chains_ok:
            advanced = dict(stored_baseline)
            advanced["ledger_chain_heads"] = chain_heads
            advanced["ledger_row_counts"] = current_baseline["ledger_row_counts"]
            advanced["updated_at_utc"] = utc_now_iso()
            baseline_path.write_text(json.dumps(advanced, indent=1, sort_keys=True), encoding="utf-8")
    elif initialize_baseline:
        payload = dict(current_baseline)
        payload["created_at_utc"] = utc_now_iso()
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        drift = []
        baseline_created = True
        reasons.append("component_baseline_initialized")
    else:
        drift = ["baseline_missing"]
        baseline_created = False
        reasons.append("component_baseline_missing")

    acceptance = "PROMOTABLE" if not reasons else "NOT_PROMOTABLE"
    return {
        "model_version": H1_MODEL_VERSION,
        "spec": "H1_CANDIDATE_SPEC.md",
        "evidence_as_of_date": end,
        "prospective_cutoff_date": cutoff,
        "acceptance": acceptance,
        "reasons": reasons,
        "review_stage": review_stage,
        "ledger": {
            "path": str(ledger_path),
            "rows_total": len(ledger_rows),
            "rows_eligible": len(ledger),
            "eligibility_max_lag_days": LEDGER_ELIGIBILITY_MAX_DAYS,
        },
        "outcomes_ledger": {
            "path": str(outcomes_ledger_path),
            "rows_total": len(outcomes_rows),
            "component_resolved_counts": {
                "growth_now": len(growth_now_labels),
                "pi_now": len(pi_now_labels),
                "pi_lead": len(pi_lead_labels),
            },
        },
        "ledger_integrity": {"chains_ok": chains_ok, "chain_heads": chain_heads},
        "pi_now_vs_v1": pi_now,
        "pi_lead_vs_v1": pi_lead,
        "quadrant_brier_current": quadrant_current,
        "quadrant_brier_next_diagnostic": quadrant_next,
        "post_cutoff_coverage": coverage,
        "a17_gate": a17["summary"],
        "latest_decision": decision_summary,
        "component_drift": {"baseline_created": baseline_created, "drift": drift},
        "bootstrap": {
            "seed": BOOTSTRAP_SEED,
            "resamples": BOOTSTRAP_RESAMPLES,
            "confidence": BOOTSTRAP_CI,
            "block_lengths": {
                "pi_now": PI_NOW_BLOCK_LENGTH,
                "pi_lead": PI_LEAD_BLOCK_LENGTH,
                "quadrant": QUADRANT_BLOCK_LENGTH,
            },
        },
        "created_at_utc": utc_now_iso(),
    }


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return
    config_path, cfg = load_macro_raw_config(args.config)
    macro_root = Path(config_path).resolve().parent
    portfolio_root = macro_root.parent
    layer_cfg = cfg_get(cfg, str(args.layer_block), default={}) or {}
    if not layer_cfg:
        raise ValueError(f"Config block {args.layer_block!r} is missing or empty.")
    cutoff = str(cfg_get(layer_cfg, "prospective_cutoff_date", default="2026-07-19"))
    evidence_cfg = cfg_get(layer_cfg, "evidence", default={}) or {}
    min_coverage = float(cfg_get(evidence_cfg, "min_coverage_fraction", default=0.95))
    min_top = float(cfg_get(layer_cfg, "decision_min_top_probability", default=0.50))
    min_confidence = float(cfg_get(layer_cfg, "decision_min_confidence", default=0.10))

    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    try:
        output_root_raw = resolve_path(config_path, str(cfg_get(layer_cfg, "output_dir", default="MacroLayer/out/regime_h1")))
        if output_root_raw is None:
            raise ValueError("Unable to resolve probability_h1.output_dir.")
        output_root = Path(output_root_raw)

        if args.end_date:
            end = str(args.end_date).strip()
        else:
            row = conn.execute(
                "SELECT MAX(as_of_date) AS d FROM macro_regime_v2_decision_daily WHERE model_version = ?",
                (H1_MODEL_VERSION,),
            ).fetchone()
            if row is None or not row["d"]:
                raise ValueError("No H1 decision rows; run the H1 chain first.")
            end = str(row["d"])

        payload = evaluate(
            conn,
            output_root=output_root,
            macro_root=macro_root,
            portfolio_root=portfolio_root,
            cfg=cfg,
            cutoff=cutoff,
            end=end,
            min_coverage=min_coverage,
            min_top=min_top,
            min_confidence=min_confidence,
            initialize_baseline=bool(args.initialize_baseline),
        )
        evidence_dir = output_root / end
        evidence_dir.mkdir(parents=True, exist_ok=True)
        out_path = evidence_dir / EVIDENCE_FILENAME
        out_path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

        # A1.6/A2.4: seal the manifest AFTER the evidence JSON exists so its hash is covered.
        manifest_path = _write_manifest(
            evidence_dir=evidence_dir,
            output_root=output_root,
            macro_root=macro_root,
            portfolio_root=portfolio_root,
            cfg=cfg,
            end=end,
            cutoff=cutoff,
        )
        logger.info(
            "H1 PROMOTION: %s stage=%s reasons=%s -> %s (manifest %s)",
            payload["acceptance"],
            payload["review_stage"],
            payload["reasons"],
            out_path,
            manifest_path.name,
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------------------------
# Selftest (synthetic; in-memory DB only where a table is needed)
# --------------------------------------------------------------------------------------------
def _make_regime_row(as_of: str, *, p_pi_now: float = 0.7) -> dict[str, Any]:
    return {
        "as_of_date": as_of, "coverage_flag": 1, "p_g_now": 0.6, "p_g_lead": 0.55,
        "p_pi_now": p_pi_now, "p_pi_lead": 0.65,
        "v1_p_g_now": 0.6, "v1_p_g_lead": 0.55, "v1_p_pi_now": 0.5, "v1_p_pi_lead": 0.5,
        "current_regime": "HEATING_UP", "next_regime": "HEATING_UP",
        "current_expansion_disinflation": 0.18, "current_heating_up": 0.42,
        "current_slow_growth": 0.12, "current_stagflation": 0.28,
        "next_expansion_disinflation": 0.19, "next_heating_up": 0.36,
        "next_slow_growth": 0.16, "next_stagflation": 0.29,
    }


def _target_conn(rows: list[tuple[str, str, int, str]]) -> sqlite3.Connection:
    """In-memory macro_probability_v2_target with (key, predictor_as_of, label, label_available)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE macro_probability_v2_target ("
        "model_version TEXT, probability_key TEXT, predictor_as_of_date TEXT, "
        "label_value INTEGER, label_available_date TEXT)"
    )
    conn.executemany(
        "INSERT INTO macro_probability_v2_target VALUES (?, ?, ?, ?, ?)",
        [(QUADRANT_LABEL_MODEL, key, d, y, la) for key, d, y, la in rows],
    )
    conn.commit()
    return conn


def _selftest() -> None:
    # 1. Circular block bootstrap: DETERMINISM (same seed -> identical CI), zero-centred series
    #    includes 0, tight-positive series excludes 0.
    zero_centred = np.array([0.1, -0.1] * 15)
    low_a, high_a = _paired_block_bootstrap_ci(zero_centred, block_length=3)
    low_b, high_b = _paired_block_bootstrap_ci(zero_centred, block_length=3)
    assert (low_a, high_a) == (low_b, high_b), "block bootstrap must be deterministic under a fixed seed"
    assert low_a is not None and high_a is not None and low_a <= 0.0 <= high_a, "zero-centred CI includes 0"
    tight = np.full(30, 0.05)
    low2, high2 = _paired_block_bootstrap_ci(tight, block_length=2)
    assert low2 is not None and abs(low2 - 0.05) < 1e-12 and low2 > 0.0, "constant positive series -> CI at 0.05"

    # 2. Component-final reasons wiring: improvement>0 but CI includes 0 must be flagged.
    ev = {
        "resolved_outcomes": 20, "class_counts": {"0": 10, "1": 10}, "brier_improvement": 0.01,
        "bootstrap_ci_excludes_zero": False, "bootstrap_ci_low": -0.02, "bootstrap_ci_high": 0.04,
    }
    rs = _component_final_reasons("pi_now", ev, min_outcomes=FINAL_MIN_PI_NOW, min_per_class=FINAL_MIN_PI_NOW_PER_CLASS)
    assert any("includes_0" in r for r in rs), "positive improvement with CI including 0 must not promote"
    ev_low_n = {**ev, "resolved_outcomes": 5}
    assert _component_final_reasons("pi_lead", ev_low_n, min_outcomes=FINAL_MIN_PI_LEAD, min_per_class=FINAL_MIN_PI_LEAD_PER_CLASS)[0].endswith("<8")

    # 3. Quadrant multiclass Brier (ledger V1): H1 confident-correct beats diffuse V1.
    ledger = {
        "2026-08-03": {
            "coverage_flag": "1",
            "current_expansion_disinflation": "0.7", "current_heating_up": "0.1",
            "current_slow_growth": "0.1", "current_stagflation": "0.1",
            "v1_p_g_now": "0.5", "v1_p_pi_now": "0.5",  # V1 diffuse 0.25 each
        }
    }
    side = _quadrant_brier_side(
        ledger=ledger, growth_labels={"2026-08-03": 1}, inflation_labels={"2026-08-03": 0},
        v1_growth_field="v1_p_g_now", v1_inflation_field="v1_p_pi_now", prefix="current",
    )
    assert side["paired_dates"] == 1
    assert side["h1_not_worse"] is True and side["brier_h1"] < side["brier_v1"], "confident-correct H1 must beat diffuse V1"
    onehot = [1.0, 0.0, 0.0, 0.0]
    assert abs(_multiclass_brier([0.7, 0.1, 0.1, 0.1], onehot) - (0.09 + 0.01 + 0.01 + 0.01)) < 1e-12
    assert _realized_quadrant_index(1, 1) == 1 and _realized_quadrant_index(0, 1) == 3

    # 4. Ledger eligibility window: <=7 days kept, >7 days dropped, first-write-wins.
    rows = [
        {"as_of_date": "2026-08-03", "capture_date_utc": "2026-08-05T00:00:00Z", "coverage_flag": "1"},
        {"as_of_date": "2026-08-03", "capture_date_utc": "2026-08-06T00:00:00Z", "coverage_flag": "1"},
        {"as_of_date": "2026-08-10", "capture_date_utc": "2026-08-20T00:00:00Z", "coverage_flag": "1"},
    ]
    elig = _eligible_ledger(rows)
    assert set(elig) == {"2026-08-03"}, "only within-7-day, first-captured rows are eligible"
    assert elig["2026-08-03"]["capture_date_utc"] == "2026-08-05T00:00:00Z", "first-write-wins on eligibility read"

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)

        # 5. Prospective ledger: first-write-wins append + hash-chain verification.
        ledger_path = tmp / LEDGER_FILENAME
        base_rows = [_make_regime_row(d) for d in ("2026-08-03", "2026-08-04", "2026-08-05")]
        first = _append_prospective_ledger(ledger_path=ledger_path, regime_rows=base_rows, cutoff="2026-07-19", capture_date_utc="2026-08-06T00:00:00Z")
        revised = [_make_regime_row("2026-08-03", p_pi_now=0.99)]
        second = _append_prospective_ledger(ledger_path=ledger_path, regime_rows=revised, cutoff="2026-07-19", capture_date_utc="2026-09-01T00:00:00Z")
        assert first == 3 and second == 0, "existing as_of_date must never re-append"
        stored = _read_ledger(ledger_path)
        assert stored[0]["p_pi_now"] == "0.7", "first capture retained despite later revision"
        ok, full_head, err = verify_ledger_chain(stored, _LEDGER_DIGEST_FIELDS)
        assert ok and err is None, f"clean chain must verify: {err}"

        # 5a. Tamper: edit a MIDDLE row's payload -> chain verification fails.
        tampered = [dict(r) for r in stored]
        tampered[1]["p_pi_now"] = "0.999"  # digest of row 1 no longer matches its stored row_digest
        with ledger_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(LEDGER_COLUMNS))
            writer.writeheader()
            writer.writerows(tampered)
        ok_t, _h, err_t = verify_ledger_chain(_read_ledger(ledger_path), _LEDGER_DIGEST_FIELDS)
        assert not ok_t and err_t is not None, "editing a middle row must be caught"

        # 5b. Truncate the tail -> internal chain still consistent, but the sealed head no longer
        #     appears, so continuity vs the stored head fails (ledger_integrity_failure).
        truncated = stored[:-1]
        assert verify_ledger_chain(truncated, _LEDGER_DIGEST_FIELDS)[0] is True, "truncated prefix is internally consistent"
        assert not _head_extends(full_head, truncated, _LEDGER_DIGEST_FIELDS), "truncation must break head continuity"
        assert _head_extends(full_head, stored, _LEDGER_DIGEST_FIELDS), "the full chain still extends its own head"

        # 6. Outcomes ledger: first-write-wins across two capture runs + chain verification.
        outcomes_path = tmp / OUTCOMES_LEDGER_FILENAME
        conn = _target_conn([
            ("P_PI_NOW_V2", "2026-07-31", 1, "2026-08-15"),
            ("P_G_NOW_V2", "2026-07-31", 0, "2026-08-15"),
            ("P_PI_LEAD_V2", "2026-06-30", 1, "2026-09-30"),
        ])
        try:
            n1 = _append_outcomes_ledger(ledger_path=outcomes_path, conn=conn, cutoff="2026-07-19", end="2026-12-31", capture_date_utc="2026-08-16T00:00:00Z")
            n2 = _append_outcomes_ledger(ledger_path=outcomes_path, conn=conn, cutoff="2026-07-19", end="2026-12-31", capture_date_utc="2026-09-20T00:00:00Z")
        finally:
            conn.close()
        assert n1 == 3 and n2 == 0, "outcomes ledger is first-write-wins on (component, predictor_as_of_date)"
        out_rows = _read_ledger(outcomes_path)
        assert verify_ledger_chain(out_rows, _OUTCOMES_DIGEST_FIELDS)[0] is True, "outcomes chain must verify"
        parsed = _labels_from_outcomes_ledger(out_rows)
        assert parsed["pi_now"]["2026-07-31"] == 1 and parsed["growth_now"]["2026-07-31"] == 0

        # 7. A1.7 gate reasons: missing / stale / fail / pass.
        macro_root = tmp / "MacroLayer"
        gate_path = macro_root.parent / "output" / "h1_walkforward" / "latest_a17_gate.json"
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        assert _evaluate_a17_gate(macro_root, evidence_end="2026-08-05")["reasons"] == ["a17_gate_missing"]
        gate_path.write_text(json.dumps({"a17_gate_pass": True, "generated_at": "2020-01-01T00:00:00Z"}), encoding="utf-8")
        assert _evaluate_a17_gate(macro_root, evidence_end="2026-08-05")["reasons"] == ["a17_gate_stale"]
        gate_path.write_text(json.dumps({"a17_gate_pass": False, "generated_at": "2026-08-01T00:00:00Z"}), encoding="utf-8")
        assert _evaluate_a17_gate(macro_root, evidence_end="2026-08-05")["reasons"] == ["a17_gate_fail"]
        gate_path.write_text(json.dumps({"a17_gate_pass": True, "generated_at": "2026-08-01T00:00:00Z"}), encoding="utf-8")
        assert _evaluate_a17_gate(macro_root, evidence_end="2026-08-05")["reasons"] == []

    print("h1 promotion validator self-test: PASS")


if __name__ == "__main__":
    main()
