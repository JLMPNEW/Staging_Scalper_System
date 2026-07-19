#!/usr/bin/env python3
"""Build the H1 hybrid regime probabilities (H1_CANDIDATE_SPEC.md).

H1 composes existing sealed daily probabilities — V1 growth (now/lead), V2.2 inflation-now,
V2.1 inflation-lead (raw) — into hybrid quadrant rows under model_version
macro_regime_h1_hybrid_v1. It fits nothing, forward-fills nothing, and fails closed: any
missing, uncovered, non-finite, or date-mismatched component yields an uncovered H1 row.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from macro_probability_v2 import regime_probabilities
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_boolish,
    parse_iso_date,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import finish_serving_run, init_db, start_serving_run

import logging

logger = logging.getLogger(__name__)

H1_MODEL_VERSION = "macro_regime_h1_hybrid_v1"
PROBABILITY_KEYS = ("P_G_NOW_V2", "P_G_LEAD_V2", "P_PI_NOW_V2", "P_PI_LEAD_V2")

# A1.1 append-only prospective ledger. Fixed column order (byte-stable header) and the
# quadrant field names emitted by build_h1_rows (current_/next_ per regime). AMENDMENT 2 adds
# the four V1 comparator columns (frozen at build time) and the hash-chain columns.
LEDGER_FILENAME = "prospective_ledger.csv"
OUTCOMES_LEDGER_FILENAME = "outcomes_ledger.csv"
_QUADRANT_SUFFIXES = ("expansion_disinflation", "heating_up", "slow_growth", "stagflation")
LEDGER_COLUMNS = (
    "as_of_date",
    "capture_date_utc",
    "p_g_now",
    "p_g_lead",
    "p_pi_now",
    "p_pi_lead",
    "v1_p_g_now",
    "v1_p_g_lead",
    "v1_p_pi_now",
    "v1_p_pi_lead",
    "coverage_flag",
    "current_regime",
    "next_regime",
    *(f"current_{suffix}" for suffix in _QUADRANT_SUFFIXES),
    *(f"next_{suffix}" for suffix in _QUADRANT_SUFFIXES),
    "prev_row_digest",
    "row_digest",
)
# Fields covered by row_digest: the immutable payload of a captured row. capture_date_utc, the
# chain columns (prev_row_digest, row_digest) are excluded so the payload is stable across
# rebuilds (first-write-wins); the chain link is folded in separately (A2.2).
_LEDGER_DIGEST_FIELDS = tuple(
    col for col in LEDGER_COLUMNS if col not in ("capture_date_utc", "prev_row_digest", "row_digest")
)

# AMENDMENT 2 outcomes ledger (A2.1): append-only, first-write-wins on (component,
# predictor_as_of_date), hash-chained (A2.2). Labels come from the sealed V2.1 target rows.
OUTCOMES_LEDGER_COLUMNS = (
    "component",
    "predictor_as_of_date",
    "label_value",
    "label_available_date",
    "capture_date_utc",
    "prev_row_digest",
    "row_digest",
)
_OUTCOMES_DIGEST_FIELDS = ("component", "predictor_as_of_date", "label_value", "label_available_date")
OUTCOMES_LABEL_MODEL = "macro_regime_v2_1_independent_outcomes_v1"
# (ledger component name, V2.1 target probability_key)
OUTCOME_COMPONENTS = (
    ("growth_now", "P_G_NOW_V2"),
    ("pi_now", "P_PI_NOW_V2"),
    ("pi_lead", "P_PI_LEAD_V2"),
)

# A2.2 tamper-evident hash chain shared primitives.
CHAIN_GENESIS = "H1-GENESIS"
_LEDGER_LOCK_SUFFIX = ".lock"

# Frozen component provenance (H1_CANDIDATE_SPEC.md). Growth from V1; inflation from the
# validated v2-family cells; energy fields from V2.1 regime rows.
V1_GROWTH_KEYS = {"P_G_NOW_V2": "P_G_NOW", "P_G_LEAD_V2": "P_G_LEAD"}
PI_NOW_SOURCE = ("macro_regime_v2_2_recalibrated_v1", "P_PI_NOW_V2")
PI_LEAD_SOURCE = ("macro_regime_v2_1_independent_outcomes_v1", "P_PI_LEAD_V2")
ENERGY_SOURCE_MODEL = "macro_regime_v2_1_independent_outcomes_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build H1 hybrid regime probabilities from sealed components.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--serving-db-path", type=Path, default=None)
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--layer-block", type=str, default="probability_h1")
    parser.add_argument("--selftest", action="store_true", help="Run the in-memory selftest and exit.")
    return parser.parse_args()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _load_v1_growth(conn: sqlite3.Connection, *, start: str, end: str) -> dict[str, dict[str, float]]:
    """{as_of_date: {h1_key: probability}} for dates where BOTH V1 growth cells are covered."""
    rows = conn.execute(
        """
        SELECT as_of_date, probability_key, probability_value, coverage_flag
        FROM macro_probabilities_daily
        WHERE as_of_date >= ? AND as_of_date <= ? AND probability_key IN ('P_G_NOW', 'P_G_LEAD')
        """,
        (start, end),
    ).fetchall()
    staged: dict[str, dict[str, float]] = {}
    for row in rows:
        if int(row["coverage_flag"] or 0) != 1:
            continue
        value = _finite(row["probability_value"])
        if value is None:
            continue
        staged.setdefault(str(row["as_of_date"]), {})[str(row["probability_key"])] = value
    return {
        date: {"P_G_NOW_V2": values["P_G_NOW"], "P_G_LEAD_V2": values["P_G_LEAD"]}
        for date, values in staged.items()
        if "P_G_NOW" in values and "P_G_LEAD" in values
    }


def _load_v1_inflation(conn: sqlite3.Connection, *, start: str, end: str) -> dict[str, dict[str, float]]:
    """{as_of_date: {'P_PI_NOW': v, 'P_PI_LEAD': v}} V1 comparators (A2.1); covered+finite only."""
    rows = conn.execute(
        """
        SELECT as_of_date, probability_key, probability_value, coverage_flag
        FROM macro_probabilities_daily
        WHERE as_of_date >= ? AND as_of_date <= ? AND probability_key IN ('P_PI_NOW', 'P_PI_LEAD')
        """,
        (start, end),
    ).fetchall()
    staged: dict[str, dict[str, float]] = {}
    for row in rows:
        if int(row["coverage_flag"] or 0) != 1:
            continue
        value = _finite(row["probability_value"])
        if value is None:
            continue
        staged.setdefault(str(row["as_of_date"]), {})[str(row["probability_key"])] = value
    return staged


def _load_v2_cell(
    conn: sqlite3.Connection, *, model_version: str, probability_key: str, start: str, end: str
) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT as_of_date, probability_value, coverage_flag
        FROM macro_probability_v2_daily
        WHERE model_version = ? AND probability_key = ? AND as_of_date >= ? AND as_of_date <= ?
        """,
        (model_version, probability_key, start, end),
    ).fetchall()
    out: dict[str, float] = {}
    for row in rows:
        if int(row["coverage_flag"] or 0) != 1:
            continue
        value = _finite(row["probability_value"])
        if value is not None:
            out[str(row["as_of_date"])] = value
    return out


def _load_energy(conn: sqlite3.Connection, *, start: str, end: str) -> dict[str, tuple[float | None, int]]:
    rows = conn.execute(
        """
        SELECT as_of_date, energy_shock_score, energy_shock_flag
        FROM macro_regime_v2_daily
        WHERE model_version = ? AND as_of_date >= ? AND as_of_date <= ?
        """,
        (ENERGY_SOURCE_MODEL, start, end),
    ).fetchall()
    return {
        str(row["as_of_date"]): (_finite(row["energy_shock_score"]), int(row["energy_shock_flag"] or 0))
        for row in rows
    }


def build_h1_rows(
    *,
    growth: dict[str, dict[str, float]],
    pi_now: dict[str, float],
    pi_lead: dict[str, float],
    energy: dict[str, tuple[float | None, int]],
    dates: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (probability_rows, regime_rows) for every date; uncovered dates carry nulls."""
    probability_rows: list[dict[str, Any]] = []
    regime_rows: list[dict[str, Any]] = []
    for date in dates:
        components: dict[str, float] = {}
        components.update(growth.get(date, {}))
        if date in pi_now:
            components["P_PI_NOW_V2"] = pi_now[date]
        if date in pi_lead:
            components["P_PI_LEAD_V2"] = pi_lead[date]
        # A2.6 stricter energy coverage: the energy row must exist, its flag must be in {0, 1},
        # and a flagged shock (flag==1) must carry a FINITE score. flag==0 with a null score is
        # explicitly allowed.
        energy_entry = energy.get(date)
        energy_ok = (
            energy_entry is not None
            and int(energy_entry[1]) in (0, 1)
            and (int(energy_entry[1]) == 0 or energy_entry[0] is not None)
        )
        covered = all(key in components for key in PROBABILITY_KEYS) and energy_ok
        for key in PROBABILITY_KEYS:
            probability_rows.append(
                {
                    "as_of_date": date,
                    "probability_key": key,
                    "probability_value": components.get(key) if covered else None,
                    "coverage_flag": int(covered),
                }
            )
        regime_row: dict[str, Any] = {
            "as_of_date": date,
            "p_g_now": components.get("P_G_NOW_V2") if covered else None,
            "p_g_lead": components.get("P_G_LEAD_V2") if covered else None,
            "p_pi_now": components.get("P_PI_NOW_V2") if covered else None,
            "p_pi_lead": components.get("P_PI_LEAD_V2") if covered else None,
            "coverage_flag": int(covered),
            "energy_shock_score": None,
            "energy_shock_flag": 0,
        }
        if covered:
            current = regime_probabilities(components["P_G_NOW_V2"], components["P_PI_NOW_V2"])
            next_regime = regime_probabilities(components["P_G_LEAD_V2"], components["P_PI_LEAD_V2"])
            for key, value in current.items():
                regime_row[f"current_{key}"] = value
            for key, value in next_regime.items():
                regime_row[f"next_{key}"] = value
            assert energy_entry is not None  # covered implies a valid energy row
            energy_score, energy_flag = energy_entry
            regime_row["energy_shock_score"] = energy_score
            regime_row["energy_shock_flag"] = energy_flag
        regime_rows.append(regime_row)
    return probability_rows, regime_rows


def _rows_digest(probability_rows: list[dict[str, Any]], regime_rows: list[dict[str, Any]]) -> str:
    payload = json.dumps([probability_rows, regime_rows], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ledger_data_fields(regime_row: dict[str, Any]) -> dict[str, Any]:
    """Map a regime row to the ledger's immutable payload fields (digest-covered subset).

    The four V1 comparator columns (A2.1) are captured verbatim from the enriched regime row and
    are NOT gated on H1 coverage: V1's own probabilities are frozen at build time regardless of
    whether the H1 quadrant is covered.
    """
    covered = int(regime_row.get("coverage_flag") or 0) == 1
    fields: dict[str, Any] = {
        "as_of_date": str(regime_row["as_of_date"]),
        "p_g_now": regime_row.get("p_g_now"),
        "p_g_lead": regime_row.get("p_g_lead"),
        "p_pi_now": regime_row.get("p_pi_now"),
        "p_pi_lead": regime_row.get("p_pi_lead"),
        "v1_p_g_now": regime_row.get("v1_p_g_now"),
        "v1_p_g_lead": regime_row.get("v1_p_g_lead"),
        "v1_p_pi_now": regime_row.get("v1_p_pi_now"),
        "v1_p_pi_lead": regime_row.get("v1_p_pi_lead"),
        "coverage_flag": int(regime_row.get("coverage_flag") or 0),
        "current_regime": regime_row.get("current_regime") if covered else None,
        "next_regime": regime_row.get("next_regime") if covered else None,
    }
    for prefix in ("current", "next"):
        for suffix in _QUADRANT_SUFFIXES:
            key = f"{prefix}_{suffix}"
            fields[key] = regime_row.get(key) if covered else None
    return fields


def attach_v1_comparators(
    regime_rows: list[dict[str, Any]],
    *,
    growth: dict[str, dict[str, float]],
    v1_inflation: dict[str, dict[str, float]],
) -> None:
    """A2.1: stamp each regime row with the frozen V1 comparator probabilities for its date."""
    for row in regime_rows:
        date = str(row["as_of_date"])
        g = growth.get(date, {})
        pi = v1_inflation.get(date, {})
        row["v1_p_g_now"] = g.get("P_G_NOW_V2")
        row["v1_p_g_lead"] = g.get("P_G_LEAD_V2")
        row["v1_p_pi_now"] = pi.get("P_PI_NOW")
        row["v1_p_pi_lead"] = pi.get("P_PI_LEAD")


# --------------------------------------------------------------------------------------------
# A2.2 tamper-evident hash-chain primitives (shared by both ledgers and the promotion verifier)
# --------------------------------------------------------------------------------------------
def _cell(value: Any) -> str:
    """Exact CSV cell rendering. Digests are taken over these strings so a written ledger round-
    trips byte-identically through csv.DictReader (write-time float == read-time string)."""
    return "" if value is None else str(value)


def _canonical_payload(fields: dict[str, Any], digest_fields: tuple[str, ...]) -> str:
    return json.dumps({key: _cell(fields.get(key)) for key in digest_fields}, sort_keys=True, separators=(",", ":"))


def chain_row_digest(fields: dict[str, Any], digest_fields: tuple[str, ...], prev_digest: str) -> str:
    """row_digest = sha256(canonical_payload || prev_row_digest). Genesis prev is CHAIN_GENESIS."""
    payload = _canonical_payload(fields, digest_fields)
    return hashlib.sha256((payload + "\x1e" + prev_digest).encode("utf-8")).hexdigest()


def read_ledger_rows(ledger_path: Path) -> list[dict[str, Any]]:
    """Every ledger row in physical file order (no filtering)."""
    if not ledger_path.exists():
        return []
    with ledger_path.open("r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _chain_head(rows: list[dict[str, Any]]) -> str:
    """The row_digest of the last row (chain head), or CHAIN_GENESIS for an empty ledger."""
    if not rows:
        return CHAIN_GENESIS
    return str(rows[-1].get("row_digest") or "")


def verify_ledger_chain(rows: list[dict[str, Any]], digest_fields: tuple[str, ...]) -> tuple[bool, str, str | None]:
    """Recompute every row digest and check prev-links. Returns (ok, head_digest, error_or_None)."""
    prev = CHAIN_GENESIS
    for index, row in enumerate(rows):
        stored_prev = str(row.get("prev_row_digest") or "")
        if stored_prev != prev:
            return False, prev, f"row{index}:broken_prev_link"
        recomputed = chain_row_digest(row, digest_fields, prev)
        if recomputed != str(row.get("row_digest") or ""):
            return False, prev, f"row{index}:row_digest_mismatch"
        prev = recomputed
    return True, prev, None


def _acquire_ledger_lock(ledger_path: Path, *, attempts: int = 300, delay: float = 0.05) -> int:
    """Exclusive O_CREAT|O_EXCL lock file with a bounded retry loop (A2.2)."""
    lock_path = ledger_path.with_name(ledger_path.name + _LEDGER_LOCK_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(attempts):
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            time.sleep(delay)
    raise TimeoutError(f"Could not acquire ledger lock {lock_path} after {attempts} attempts.")


def _release_ledger_lock(fd: int, ledger_path: Path) -> None:
    lock_path = ledger_path.with_name(ledger_path.name + _LEDGER_LOCK_SUFFIX)
    try:
        os.close(fd)
    finally:
        try:
            os.unlink(str(lock_path))
        except FileNotFoundError:
            pass


def _append_chained_rows(
    *,
    ledger_path: Path,
    columns: tuple[str, ...],
    digest_fields: tuple[str, ...],
    payloads: list[dict[str, Any]],
    capture_date_utc: str,
) -> int:
    """Append already-deduplicated, already-ordered payload dicts as new hash-chained rows.

    Holds the exclusive lock across the read-head / append so concurrent writers cannot
    interleave and break the chain. A true append (mode 'a'); existing rows are never rewritten.
    """
    if not payloads:
        return 0
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _acquire_ledger_lock(ledger_path)
    try:
        prev_digest = _chain_head(read_ledger_rows(ledger_path))
        write_header = (not ledger_path.exists()) or ledger_path.stat().st_size == 0
        with ledger_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns))
            if write_header:
                writer.writeheader()
            for data in payloads:
                digest = chain_row_digest(data, digest_fields, prev_digest)
                record: dict[str, Any] = dict(data)
                record["capture_date_utc"] = capture_date_utc
                record["prev_row_digest"] = prev_digest
                record["row_digest"] = digest
                writer.writerow({col: _cell(record.get(col)) for col in columns})
                prev_digest = digest
            handle.flush()
            os.fsync(handle.fileno())
        return len(payloads)
    finally:
        _release_ledger_lock(fd, ledger_path)


def _append_prospective_ledger(
    *,
    ledger_path: Path,
    regime_rows: list[dict[str, Any]],
    cutoff: str,
    capture_date_utc: str,
) -> int:
    """A1.1/A2.2: append genuinely new post-cutoff rows to the hash-chained prospective ledger.

    Post-cutoff = as_of_date > cutoff. FIRST-WRITE-WINS: an as_of_date already present in the
    ledger is never rewritten.
    """
    post_rows = [row for row in regime_rows if str(row["as_of_date"]) > cutoff]
    if not post_rows:
        return 0
    existing_dates = {
        str(row.get("as_of_date")) for row in read_ledger_rows(ledger_path) if row.get("as_of_date")
    }
    new_rows = sorted(
        (row for row in post_rows if str(row["as_of_date"]) not in existing_dates),
        key=lambda row: str(row["as_of_date"]),
    )
    payloads = [_ledger_data_fields(row) for row in new_rows]
    return _append_chained_rows(
        ledger_path=ledger_path,
        columns=LEDGER_COLUMNS,
        digest_fields=_LEDGER_DIGEST_FIELDS,
        payloads=payloads,
        capture_date_utc=capture_date_utc,
    )


def _load_new_outcome_labels(conn: sqlite3.Connection, *, cutoff: str, end: str) -> list[dict[str, Any]]:
    """Post-cutoff resolved labels for the three components from the sealed V2.1 target rows."""
    out: list[dict[str, Any]] = []
    for component, probability_key in OUTCOME_COMPONENTS:
        rows = conn.execute(
            """
            SELECT predictor_as_of_date AS d, label_value AS y, label_available_date AS la
            FROM macro_probability_v2_target
            WHERE model_version = ? AND probability_key = ?
              AND label_value IS NOT NULL
              AND label_available_date > ? AND label_available_date <= ?
            ORDER BY predictor_as_of_date
            """,
            (OUTCOMES_LABEL_MODEL, probability_key, cutoff, end),
        ).fetchall()
        for row in rows:
            out.append(
                {
                    "component": component,
                    "predictor_as_of_date": str(row["d"]),
                    "label_value": int(row["y"]),
                    "label_available_date": str(row["la"]),
                }
            )
    return out


def _append_outcomes_ledger(
    *,
    ledger_path: Path,
    conn: sqlite3.Connection,
    cutoff: str,
    end: str,
    capture_date_utc: str,
) -> int:
    """A2.1/A2.2: append NEWLY-resolved post-cutoff labels; first-write-wins on
    (component, predictor_as_of_date); hash-chained."""
    candidates = _load_new_outcome_labels(conn, cutoff=cutoff, end=end)
    if not candidates:
        return 0
    existing_keys = {
        (str(row.get("component")), str(row.get("predictor_as_of_date")))
        for row in read_ledger_rows(ledger_path)
    }
    new_rows = [
        record
        for record in candidates
        if (record["component"], record["predictor_as_of_date"]) not in existing_keys
    ]
    new_rows.sort(key=lambda record: (record["component"], record["predictor_as_of_date"]))
    return _append_chained_rows(
        ledger_path=ledger_path,
        columns=OUTCOMES_LEDGER_COLUMNS,
        digest_fields=_OUTCOMES_DIGEST_FIELDS,
        payloads=new_rows,
        capture_date_utc=capture_date_utc,
    )


def _write_rows(
    conn: sqlite3.Connection,
    *,
    probability_rows: list[dict[str, Any]],
    regime_rows: list[dict[str, Any]],
    start: str,
    end: str,
) -> int:
    now = utc_now_iso()
    try:
        conn.execute(
            "DELETE FROM macro_probability_v2_daily WHERE model_version = ? AND as_of_date >= ? AND as_of_date <= ?",
            (H1_MODEL_VERSION, start, end),
        )
        conn.execute(
            "DELETE FROM macro_regime_v2_daily WHERE model_version = ? AND as_of_date >= ? AND as_of_date <= ?",
            (H1_MODEL_VERSION, start, end),
        )
        conn.executemany(
            """
            INSERT INTO macro_probability_v2_daily (
                model_version, as_of_date, probability_key, probability_value,
                calibration_as_of_date, target_period_start, target_period_end,
                training_sample_count, positive_rate, predictor_coverage_ratio,
                coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, 0, NULL, NULL, ?, ?)
            """,
            [
                (H1_MODEL_VERSION, row["as_of_date"], row["probability_key"], row["probability_value"], row["coverage_flag"], now)
                for row in probability_rows
            ],
        )
        conn.executemany(
            """
            INSERT INTO macro_regime_v2_daily (
                model_version, as_of_date, p_g_now, p_g_lead, p_pi_now, p_pi_lead,
                p_current_expansion_disinflation, p_current_heating_up, p_current_slow_growth, p_current_stagflation,
                p_next_expansion_disinflation, p_next_heating_up, p_next_slow_growth, p_next_stagflation,
                current_regime, next_regime, current_regime_probability, next_regime_probability,
                current_regime_confidence, next_regime_confidence,
                energy_shock_score, energy_shock_flag, shadow_only_flag, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            [
                (
                    H1_MODEL_VERSION,
                    row["as_of_date"],
                    row["p_g_now"],
                    row["p_g_lead"],
                    row["p_pi_now"],
                    row["p_pi_lead"],
                    row.get("current_expansion_disinflation"),
                    row.get("current_heating_up"),
                    row.get("current_slow_growth"),
                    row.get("current_stagflation"),
                    row.get("next_expansion_disinflation"),
                    row.get("next_heating_up"),
                    row.get("next_slow_growth"),
                    row.get("next_stagflation"),
                    row.get("current_regime"),
                    row.get("next_regime"),
                    row.get("current_top_probability"),
                    row.get("next_top_probability"),
                    row.get("current_confidence"),
                    row.get("next_confidence"),
                    row["energy_shock_score"],
                    row["energy_shock_flag"],
                    row["coverage_flag"],
                    now,
                )
                for row in regime_rows
            ],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(probability_rows) + len(regime_rows)


def _selftest() -> None:
    growth = {"2026-01-02": {"P_G_NOW_V2": 0.60, "P_G_LEAD_V2": 0.55}}
    pi_now = {"2026-01-02": 0.70, "2026-01-03": 0.71}
    pi_lead = {"2026-01-02": 0.65}
    energy = {"2026-01-02": (0.4, 0), "2026-01-03": (0.4, 0)}
    dates = ["2026-01-02", "2026-01-03"]
    probability_rows, regime_rows = build_h1_rows(
        growth=growth, pi_now=pi_now, pi_lead=pi_lead, energy=energy, dates=dates
    )
    assert len(probability_rows) == 8 and len(regime_rows) == 2
    covered = {row["as_of_date"]: row for row in regime_rows}
    good, bad = covered["2026-01-02"], covered["2026-01-03"]
    assert good["coverage_flag"] == 1 and bad["coverage_flag"] == 0, "missing growth component must fail closed"
    assert bad["p_g_now"] is None and "current_regime" not in bad
    quadrant = [
        good["current_expansion_disinflation"],
        good["current_heating_up"],
        good["current_slow_growth"],
        good["current_stagflation"],
    ]
    assert abs(sum(quadrant) - 1.0) < 1e-9, "quadrant probabilities must conserve"
    assert good["current_regime"] == "HEATING_UP", "0.60 growth x 0.70 inflation is HEATING_UP"
    assert good["p_g_now"] == 0.60 and good["p_g_lead"] == 0.55, "growth pass-through must be byte-equal"
    first = _rows_digest(*build_h1_rows(growth=growth, pi_now=pi_now, pi_lead=pi_lead, energy=energy, dates=dates))
    second = _rows_digest(*build_h1_rows(growth=growth, pi_now=pi_now, pi_lead=pi_lead, energy=energy, dates=dates))
    assert first == second, "adapter must be deterministic"

    # A2.6 stricter energy coverage: a flagged shock (flag==1) with a null score is NOT covered,
    # but flag==0 with a null score is allowed.
    _, strict_rows = build_h1_rows(
        growth={"d1": {"P_G_NOW_V2": 0.6, "P_G_LEAD_V2": 0.55}, "d2": {"P_G_NOW_V2": 0.6, "P_G_LEAD_V2": 0.55}},
        pi_now={"d1": 0.7, "d2": 0.7}, pi_lead={"d1": 0.65, "d2": 0.65},
        energy={"d1": (None, 1), "d2": (None, 0)}, dates=["d1", "d2"],
    )
    strict = {row["as_of_date"]: row for row in strict_rows}
    assert strict["d1"]["coverage_flag"] == 0, "flag==1 with null score must fail closed"
    assert strict["d2"]["coverage_flag"] == 1, "flag==0 with null score is allowed"

    # A2.1/A2.2 ledger: post-cutoff dates append with V1 comparators; hash chain verifies and is
    # capture-independent (first-write-wins).
    attach_v1_comparators(
        regime_rows,
        growth=growth,
        v1_inflation={"2026-01-02": {"P_PI_NOW": 0.68, "P_PI_LEAD": 0.62}},
    )
    good = {row["as_of_date"]: row for row in regime_rows}["2026-01-02"]
    assert good["v1_p_pi_now"] == 0.68 and good["v1_p_g_now"] == 0.60, "V1 comparators must be captured"
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / LEDGER_FILENAME
        appended = _append_prospective_ledger(
            ledger_path=ledger, regime_rows=regime_rows, cutoff="2026-01-01", capture_date_utc="2026-01-02T00:00:00Z"
        )
        assert appended == 2, "both post-cutoff dates append on first write"
        stored = read_ledger_rows(ledger)
        ok, _head, err = verify_ledger_chain(stored, _LEDGER_DIGEST_FIELDS)
        assert ok and err is None, f"prospective chain must verify: {err}"
        assert stored[0]["prev_row_digest"] == CHAIN_GENESIS, "genesis prev must be H1-GENESIS"
        assert stored[0]["v1_p_pi_now"] == "0.68", "V1 comparator must round-trip through the ledger"
        again = _append_prospective_ledger(
            ledger_path=ledger, regime_rows=regime_rows, cutoff="2026-01-01", capture_date_utc="2026-09-09T00:00:00Z"
        )
        assert again == 0, "first-write-wins: existing as_of_dates never re-append"
    print("h1 hybrid adapter self-test: PASS")


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = cfg_get(cfg, str(args.layer_block), default={}) or {}
    if not layer_cfg:
        raise ValueError(f"Config block {args.layer_block!r} is missing or empty.")
    if not parse_boolish(cfg_get(layer_cfg, "shadow_only", default=None), default=False):
        raise ValueError(f"{args.layer_block}.shadow_only must remain true until formal promotion.")
    model_version = str(cfg_get(layer_cfg, "model_version", default=H1_MODEL_VERSION)).strip()
    if model_version != H1_MODEL_VERSION:
        raise ValueError(f"H1 adapter only builds {H1_MODEL_VERSION}; got {model_version!r}.")
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    try:
        init_db(conn)
        end_override = parse_iso_date(args.end_date)
        if end_override is None:
            row = conn.execute("SELECT MAX(as_of_date) AS max_date FROM macro_probabilities_daily").fetchone()
            if row is None or not row["max_date"]:
                raise ValueError("Unable to resolve H1 end date from macro_probabilities_daily.")
            end = str(row["max_date"])
        else:
            end = end_override.isoformat()
        configured_start = parse_iso_date(str(cfg_get(layer_cfg, "history_start_date", default="2001-01-01")))
        start_override = parse_iso_date(args.start_date)
        start = (start_override or configured_start).isoformat() if (start_override or configured_start) else end
        if start > end:
            raise ValueError(f"H1 start {start} is after end {end}.")

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="probability_h1_hybrid",
            raw_ingest_run_id=None,
            as_of_start_date=start,
            as_of_end_date=end,
            metric_count=len(PROBABILITY_KEYS),
            notes=f"H1 hybrid adapter model_version={H1_MODEL_VERSION}.",
        )
        run_started = True

        growth = _load_v1_growth(conn, start=start, end=end)
        pi_now = _load_v2_cell(
            conn, model_version=PI_NOW_SOURCE[0], probability_key=PI_NOW_SOURCE[1], start=start, end=end
        )
        pi_lead = _load_v2_cell(
            conn, model_version=PI_LEAD_SOURCE[0], probability_key=PI_LEAD_SOURCE[1], start=start, end=end
        )
        v1_inflation = _load_v1_inflation(conn, start=start, end=end)
        energy = _load_energy(conn, start=start, end=end)
        dates = sorted({*growth, *pi_now, *pi_lead})
        if not dates:
            raise ValueError("No component dates found for the H1 window; run V1 and v2-family builds first.")
        probability_rows, regime_rows = build_h1_rows(
            growth=growth, pi_now=pi_now, pi_lead=pi_lead, energy=energy, dates=dates
        )
        # Determinism digest is taken over the pure H1 quadrant rows (BEFORE V1 comparators are
        # stamped) so the validator's recompute matches the sealed digest.
        digest = _rows_digest(probability_rows, regime_rows)
        rows_written = _write_rows(
            conn, probability_rows=probability_rows, regime_rows=regime_rows, start=start, end=end
        )
        attach_v1_comparators(regime_rows, growth=growth, v1_inflation=v1_inflation)
        covered_count = sum(1 for row in regime_rows if row["coverage_flag"] == 1)

        output_root = resolve_path(config_path, str(cfg_get(layer_cfg, "output_dir", default="MacroLayer/out/regime_h1")))
        if output_root is None:
            raise ValueError(f"Unable to resolve {args.layer_block}.output_dir.")

        cutoff = str(cfg_get(layer_cfg, "prospective_cutoff_date", default="2026-07-19"))
        ledger_path = Path(output_root) / LEDGER_FILENAME
        ledger_appended = _append_prospective_ledger(
            ledger_path=ledger_path,
            regime_rows=regime_rows,
            cutoff=cutoff,
            capture_date_utc=utc_now_iso(),
        )
        outcomes_ledger_path = Path(output_root) / OUTCOMES_LEDGER_FILENAME
        outcomes_appended = _append_outcomes_ledger(
            ledger_path=outcomes_ledger_path,
            conn=conn,
            cutoff=cutoff,
            end=end,
            capture_date_utc=utc_now_iso(),
        )

        output_dir = Path(output_root) / end
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "model_version": H1_MODEL_VERSION,
            "spec": "H1_CANDIDATE_SPEC.md",
            "window": {"start": start, "end": end},
            "prospective_cutoff_date": cutoff,
            "prospective_ledger": {"path": str(ledger_path), "rows_appended": ledger_appended},
            "outcomes_ledger": {"path": str(outcomes_ledger_path), "rows_appended": outcomes_appended},
            "components": {
                "P_G_NOW": {"source": "v1", "table": "macro_probabilities_daily", "key": "P_G_NOW"},
                "P_G_LEAD": {"source": "v1", "table": "macro_probabilities_daily", "key": "P_G_LEAD"},
                "P_PI_NOW": {"source_model_version": PI_NOW_SOURCE[0], "key": PI_NOW_SOURCE[1]},
                "P_PI_LEAD": {"source_model_version": PI_LEAD_SOURCE[0], "key": PI_LEAD_SOURCE[1]},
                "energy": {"source_model_version": ENERGY_SOURCE_MODEL, "table": "macro_regime_v2_daily"},
            },
            "component_row_counts": {
                "v1_growth_dates": len(growth),
                "pi_now_dates": len(pi_now),
                "pi_lead_dates": len(pi_lead),
                "energy_dates": len(energy),
            },
            "dates_total": len(dates),
            "dates_covered": covered_count,
            "coverage_fraction": round(covered_count / len(dates), 6),
            "rows_written": rows_written,
            "output_digest_sha256": digest,
            "created_at_utc": utc_now_iso(),
        }
        manifest_path = output_dir / "h1_hybrid_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
        logger.info(
            "H1 HYBRID: dates=%d covered=%d (%.1f%%) rows=%d digest=%s -> %s",
            len(dates),
            covered_count,
            100.0 * covered_count / len(dates),
            rows_written,
            digest[:12],
            manifest_path,
        )
        finish_serving_run(conn, serving_run_id=serving_run_id, status="completed", rows_written=rows_written)
    except BaseException:
        if run_started:
            finish_serving_run(conn, serving_run_id=serving_run_id, status="failed", rows_written=0)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
