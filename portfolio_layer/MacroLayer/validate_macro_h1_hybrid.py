#!/usr/bin/env python3
"""Validate the H1 hybrid build (H1_CANDIDATE_SPEC.md validation gates).

Gates (all HARD): probability conservation on covered rows; coverage reporting;
determinism (recompute digest equals the sealed manifest digest); PIT lineage
(sampled covered rows re-verified against their component source tables, including
byte-equal V1 growth pass-through). Writes a sealed validation JSON next to the manifest.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import logging

from build_macro_h1_hybrid import (
    H1_MODEL_VERSION,
    PI_LEAD_SOURCE,
    PI_NOW_SOURCE,
    _load_energy,
    _load_v1_growth,
    _load_v2_cell,
    _rows_digest,
    build_h1_rows,
)
from macro_raw_config import cfg_get, configure_pipeline_logging, connect_sqlite, load_macro_raw_config, resolve_path, utc_now_iso
from macro_serving_common import resolve_serving_db_path

logger = logging.getLogger(__name__)

REGIME_LABELS = {"EXPANSION_DISINFLATION", "HEATING_UP", "SLOW_GROWTH", "STAGFLATION"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the H1 hybrid regime build.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--serving-db-path", type=Path, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--layer-block", type=str, default="probability_h1")
    return parser.parse_args()


def _gate(gates: list[dict[str, Any]], name: str, passed: bool, detail: str) -> None:
    gates.append({"gate": name, "status": "PASS" if passed else "FAIL", "detail": detail})
    logger.info("[%s] %s -- %s", "PASS" if passed else "FAIL", name, detail)


# DB regime column -> recomputed build_h1_rows field (A2.6 exact re-derivation).
_REGIME_COLUMN_SOURCES = {
    "p_g_now": "p_g_now", "p_g_lead": "p_g_lead", "p_pi_now": "p_pi_now", "p_pi_lead": "p_pi_lead",
    "p_current_expansion_disinflation": "current_expansion_disinflation",
    "p_current_heating_up": "current_heating_up",
    "p_current_slow_growth": "current_slow_growth",
    "p_current_stagflation": "current_stagflation",
    "p_next_expansion_disinflation": "next_expansion_disinflation",
    "p_next_heating_up": "next_heating_up",
    "p_next_slow_growth": "next_slow_growth",
    "p_next_stagflation": "next_stagflation",
    "current_regime": "current_regime", "next_regime": "next_regime",
    "current_regime_probability": "current_top_probability",
    "next_regime_probability": "next_top_probability",
    "current_regime_confidence": "current_confidence",
    "next_regime_confidence": "next_confidence",
    "energy_shock_score": "energy_shock_score",
    "energy_shock_flag": "energy_shock_flag",
    "coverage_flag": "coverage_flag",
}
_REGIME_PROB_COLUMNS = (
    "p_g_now", "p_g_lead", "p_pi_now", "p_pi_lead",
    "p_current_expansion_disinflation", "p_current_heating_up", "p_current_slow_growth", "p_current_stagflation",
    "p_next_expansion_disinflation", "p_next_heating_up", "p_next_slow_growth", "p_next_stagflation",
)


def _val_equal(a: Any, b: Any, *, tol: float = 1e-9) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return str(a) == str(b)


def _compare_probability_rows(db_rows: list[Any], recomputed: list[dict[str, Any]]) -> list[str]:
    expected = {
        (str(r["as_of_date"]), str(r["probability_key"])): (r["probability_value"], int(r["coverage_flag"]))
        for r in recomputed
    }
    mismatches: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in db_rows:
        key = (str(row["as_of_date"]), str(row["probability_key"]))
        seen.add(key)
        if key not in expected:
            mismatches.append(f"{key[0]}:{key[1]}:unexpected_db_row")
            continue
        exp_value, exp_cov = expected[key]
        if not _val_equal(row["probability_value"], exp_value):
            mismatches.append(f"{key[0]}:{key[1]}:value")
        if int(row["coverage_flag"] or 0) != exp_cov:
            mismatches.append(f"{key[0]}:{key[1]}:coverage_flag")
    for key in expected:
        if key not in seen:
            mismatches.append(f"{key[0]}:{key[1]}:missing_db_row")
    return mismatches


def _compare_regime_rows(db_rows: list[Any], recomputed: list[dict[str, Any]]) -> list[str]:
    expected = {str(r["as_of_date"]): r for r in recomputed}
    mismatches: list[str] = []
    seen: set[str] = set()
    for row in db_rows:
        date = str(row["as_of_date"])
        seen.add(date)
        if date not in expected:
            mismatches.append(f"{date}:unexpected_db_row")
            continue
        exp_row = expected[date]
        for column, source in _REGIME_COLUMN_SOURCES.items():
            if not _val_equal(row[column], exp_row.get(source)):
                mismatches.append(f"{date}:{column}")
    for date in expected:
        if date not in seen:
            mismatches.append(f"{date}:missing_db_row")
    return mismatches


def _probability_bound_errors(prob_db: list[Any], covered_regime_rows: list[Any]) -> list[str]:
    errors: list[str] = []
    for row in prob_db:
        if int(row["coverage_flag"] or 0) != 1:
            continue
        value = row["probability_value"]
        if value is None or not (0.0 <= float(value) <= 1.0):
            errors.append(f"{row['as_of_date']}:{row['probability_key']}={value}")
    for row in covered_regime_rows:
        for column in _REGIME_PROB_COLUMNS:
            value = row[column]
            if value is None or not (0.0 <= float(value) <= 1.0):
                errors.append(f"{row['as_of_date']}:{column}={value}")
    return errors


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = cfg_get(cfg, str(args.layer_block), default={}) or {}
    if not layer_cfg:
        raise ValueError(f"Config block {args.layer_block!r} is missing or empty.")
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    try:
        output_root = resolve_path(config_path, str(cfg_get(layer_cfg, "output_dir", default="MacroLayer/out/regime_h1")))
        if output_root is None:
            raise ValueError("Unable to resolve probability_h1.output_dir.")
        if args.end_date:
            end = str(args.end_date).strip()
        else:
            row = conn.execute(
                "SELECT MAX(as_of_date) AS d FROM macro_regime_v2_daily WHERE model_version = ?",
                (H1_MODEL_VERSION,),
            ).fetchone()
            if row is None or not row["d"]:
                raise ValueError("No H1 rows exist; run build_macro_h1_hybrid first.")
            end = str(row["d"])
        manifest_path = Path(output_root) / end / "h1_hybrid_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        start = str(manifest["window"]["start"])

        gates: list[dict[str, Any]] = []
        rows = conn.execute(
            """
            SELECT * FROM macro_regime_v2_daily
            WHERE model_version = ? AND as_of_date >= ? AND as_of_date <= ?
            ORDER BY as_of_date
            """,
            (H1_MODEL_VERSION, start, end),
        ).fetchall()
        covered = [row for row in rows if int(row["coverage_flag"] or 0) == 1]
        _gate(gates, "rows_exist", bool(rows), f"rows={len(rows)} covered={len(covered)} window={start}..{end}")

        conservation_errors = 0
        label_errors = 0
        for row in covered:
            for prefix in ("current", "next"):
                quadrant = [
                    float(row[f"p_{prefix}_expansion_disinflation"]),
                    float(row[f"p_{prefix}_heating_up"]),
                    float(row[f"p_{prefix}_slow_growth"]),
                    float(row[f"p_{prefix}_stagflation"]),
                ]
                if abs(sum(quadrant) - 1.0) > 1e-8:
                    conservation_errors += 1
                if str(row[f"{prefix}_regime"]) not in REGIME_LABELS:
                    label_errors += 1
        _gate(
            gates,
            "probability_conservation",
            conservation_errors == 0 and label_errors == 0,
            f"covered_rows={len(covered)} conservation_errors={conservation_errors} label_errors={label_errors}",
        )

        coverage_fraction = (len(covered) / len(rows)) if rows else 0.0
        _gate(
            gates,
            "coverage_reported",
            abs(coverage_fraction - float(manifest["coverage_fraction"])) < 1e-6,
            f"db_coverage={coverage_fraction:.6f} manifest={manifest['coverage_fraction']}",
        )

        growth = _load_v1_growth(conn, start=start, end=end)
        pi_now = _load_v2_cell(conn, model_version=PI_NOW_SOURCE[0], probability_key=PI_NOW_SOURCE[1], start=start, end=end)
        pi_lead = _load_v2_cell(conn, model_version=PI_LEAD_SOURCE[0], probability_key=PI_LEAD_SOURCE[1], start=start, end=end)
        energy = _load_energy(conn, start=start, end=end)
        dates = sorted({*growth, *pi_now, *pi_lead})
        recomputed_prob_rows, recomputed_regime_rows = build_h1_rows(
            growth=growth, pi_now=pi_now, pi_lead=pi_lead, energy=energy, dates=dates
        )
        digest = _rows_digest(recomputed_prob_rows, recomputed_regime_rows)
        _gate(
            gates,
            "determinism",
            digest == str(manifest["output_digest_sha256"]),
            f"recomputed={digest[:12]} sealed={str(manifest['output_digest_sha256'])[:12]}",
        )

        # A2.6 EVERY DB row (both tables, all value columns) is re-derived and compared exactly.
        prob_db = conn.execute(
            """
            SELECT as_of_date, probability_key, probability_value, coverage_flag
            FROM macro_probability_v2_daily
            WHERE model_version = ? AND as_of_date >= ? AND as_of_date <= ?
            """,
            (H1_MODEL_VERSION, start, end),
        ).fetchall()
        prob_mismatches = _compare_probability_rows(prob_db, recomputed_prob_rows)
        _gate(
            gates,
            "probability_rows_exact",
            not prob_mismatches,
            f"db_rows={len(prob_db)} recomputed={len(recomputed_prob_rows)} mismatches={prob_mismatches[:5]}",
        )
        regime_mismatches = _compare_regime_rows(rows, recomputed_regime_rows)
        _gate(
            gates,
            "regime_rows_exact",
            not regime_mismatches,
            f"db_rows={len(rows)} recomputed={len(recomputed_regime_rows)} mismatches={regime_mismatches[:5]}",
        )

        # A2.6 probability/quadrant bounds in [0, 1] on every covered row (both tables).
        bound_errors = _probability_bound_errors(prob_db, covered)
        _gate(
            gates,
            "probability_bounds",
            not bound_errors,
            f"out_of_bounds={bound_errors[:5]}",
        )

        lineage_errors = []
        sample = covered[:: max(1, len(covered) // 50)] if covered else []
        for row in sample:
            date = str(row["as_of_date"])
            expectations = (
                ("p_g_now", growth.get(date, {}).get("P_G_NOW_V2")),
                ("p_g_lead", growth.get(date, {}).get("P_G_LEAD_V2")),
                ("p_pi_now", pi_now.get(date)),
                ("p_pi_lead", pi_lead.get(date)),
            )
            for column, expected in expectations:
                actual = row[column]
                if expected is None or actual is None or float(actual) != float(expected):
                    lineage_errors.append(f"{date}:{column}")
        _gate(
            gates,
            "pit_lineage_and_growth_passthrough",
            not lineage_errors,
            f"sampled={len(sample)} mismatches={lineage_errors[:5]}",
        )

        acceptance = "PASS" if all(g["status"] == "PASS" for g in gates) else "FAIL"
        payload = {
            "model_version": H1_MODEL_VERSION,
            "validation_date": end,
            "acceptance": acceptance,
            "gates": gates,
            "created_at_utc": utc_now_iso(),
        }
        out_path = Path(output_root) / end / "h1_hybrid_validation.json"
        out_path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        logger.info("H1 HYBRID VALIDATION: %s -> %s", acceptance, out_path)
        if acceptance != "PASS":
            raise SystemExit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
