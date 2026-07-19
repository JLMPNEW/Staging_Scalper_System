#!/usr/bin/env python3
"""Step 5 of the H1 plan - SHADOW-ONLY diagnostic comparison of V1 vs H1 macro-regime labels
and the allocation paths they imply.

HISTORICAL RESULTS ARE DIAGNOSTIC ONLY. Per H1_CANDIDATE_SPEC.md, no historical run may promote
H1; the frozen promotion contract is prospective-only. This script exists to SEE how the V1 and
H1 (`macro_regime_h1_hybrid_v1`) regime labels differ and what those differences would imply if
they were pushed through the existing regime->gross and regime->sleeve-budget mappings. It is a
pure mapping-level diagnostic: it does NOT run Stage 6/7/8, does NOT model returns, and its
"turnover proxy" is the label->gross mapping churn only, not a backtested trading turnover.

Read-only inputs (macro_serving.sqlite opened mode=ro; config.yaml). Writes ONLY under
portfolio_layer/output/h1_v1_comparison/<end_date>/. It modifies no config, book, or production
artifact.

Over the common covered date range (both sources coverage_flag=1 on the same as_of_date) it reports:
  1. label agreement (overall fraction, 4x4 V1xH1 confusion matrix, per-year agreement);
  2. transition stats (regime switches per year per source);
  3. mapped allocation paths (daily gross via regime_to_gross_scalar; daily sleeve budget via
     risk_off_regimes membership) with missing label -> 'default';
  4. an implied turnover proxy (sum |gross_t - gross_{t-1}| per year per source) plus the count of
     days the two sources' gross differ and the mean absolute gross difference;
  5. the CURRENT state on the latest common covered date (both labels, both gross, both budgets).

--selftest runs a synthetic in-memory check of the agreement / confusion / switch / turnover math,
including a deliberate V1!=H1 disagreement and a missing-label -> 'default' gross mapping.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402

LOGGER = logging.getLogger("compare_h1_v1_allocation")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

H1_MODEL_VERSION = "macro_regime_h1_hybrid_v1"
# canonical live four-quadrant vocabulary (V1 decision layer + v2-family/H1 candidates)
CANONICAL_REGIMES = ["EXPANSION_DISINFLATION", "HEATING_UP", "SLOW_GROWTH", "STAGFLATION"]
CSV_FIELDS = ["date", "v1_label", "h1_label", "v1_gross", "h1_gross", "agree_flag"]
DIAGNOSTIC_BANNER = (
    "DIAGNOSTIC ONLY - historical V1/H1 comparison; NOT promotable evidence (see H1_CANDIDATE_SPEC.md). "
    "The turnover figure is a label->gross MAPPING proxy, not a backtested trading turnover."
)


# ---------------------------------------------------------------------------
# pure mapping helpers (self-tested)
# ---------------------------------------------------------------------------
def gross_for(label: str, gross_map: dict[str, float]) -> float:
    """Map a regime label through regime_to_gross_scalar; a missing label falls back to 'default'."""
    if label in gross_map:
        return float(gross_map[label])
    if "default" not in gross_map:
        raise ValueError("regime_to_gross_scalar has no 'default' entry to fall back to")
    return float(gross_map["default"])


def sleeve_budget_key(label: str, risk_off_regimes: set[str]) -> str:
    """'risk_off' if the label is a risk-off regime, else 'default' (missing label -> default)."""
    return "risk_off" if label in risk_off_regimes else "default"


def agreement_fraction(pairs: list[tuple[str, str]]) -> float:
    """Fraction of (v1, h1) pairs whose labels are identical."""
    if not pairs:
        return 0.0
    return sum(1 for v, h in pairs if v == h) / len(pairs)


def confusion_matrix(pairs: list[tuple[str, str]], order: list[str]) -> dict[str, dict[str, int]]:
    """V1 (row) x H1 (col) count matrix over `order` (labels outside `order` are ignored)."""
    order_set = set(order)
    mat = {v: {h: 0 for h in order} for v in order}
    for v, h in pairs:
        if v in order_set and h in order_set:
            mat[v][h] += 1
    return mat


def per_year_agreement(dated_pairs: list[tuple[str, str, str]]) -> dict[str, dict[str, Any]]:
    """{year: {n, agree, fraction}} from (date, v1, h1) rows keyed by the date's year."""
    by_year: dict[str, list[tuple[str, str]]] = {}
    for d, v, h in dated_pairs:
        by_year.setdefault(d[:4], []).append((v, h))
    out: dict[str, dict[str, Any]] = {}
    for year in sorted(by_year):
        rows = by_year[year]
        agree = sum(1 for v, h in rows if v == h)
        out[year] = {"n": len(rows), "agree": agree, "fraction": round(agree / len(rows), 6)}
    return out


def switches_per_year(dated_labels: list[tuple[str, str]]) -> dict[str, int]:
    """Regime switches keyed by the switch date's year. A switch = label != previous covered label.

    `dated_labels` must be sorted ascending by date; consecutive entries are consecutive covered dates.
    """
    out: Counter[str] = Counter()
    prev: str | None = None
    for d, label in dated_labels:
        if prev is not None and label != prev:
            out[d[:4]] += 1
        prev = label
    return dict(sorted(out.items()))


def gross_turnover_per_year(dated_gross: list[tuple[str, float]]) -> dict[str, float]:
    """sum |gross_t - gross_{t-1}| keyed by the year of date t (label->gross mapping proxy).

    `dated_gross` must be sorted ascending by date.
    """
    out: dict[str, float] = {}
    prev: float | None = None
    for d, g in dated_gross:
        if prev is not None:
            out[d[:4]] = out.get(d[:4], 0.0) + abs(g - prev)
        prev = g
    return {y: round(v, 8) for y, v in sorted(out.items())}


# ---------------------------------------------------------------------------
# read-only DB access
# ---------------------------------------------------------------------------
def connect_ro(db_path: Path, *, timeout: float, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open the macro serving DB strictly read-only (mode=ro, immutable OFF)."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=timeout)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return con


def _require_columns(con: sqlite3.Connection, table: str, needed: set[str]) -> None:
    cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if not cols:
        raise ValueError(f"Table {table} not found or has no columns")
    missing = needed - cols
    if missing:
        raise ValueError(f"Table {table} missing expected columns: {sorted(missing)}")


def load_labels(
    con: sqlite3.Connection,
    table: str,
    *,
    model_version: str | None,
    end_date: str | None,
) -> dict[str, str]:
    """{as_of_date: active_current_regime} for coverage_flag=1 rows (optionally <= end_date)."""
    _require_columns(con, table, {"as_of_date", "active_current_regime", "coverage_flag"})
    where = ["coverage_flag = 1", "active_current_regime IS NOT NULL", "TRIM(active_current_regime) <> ''"]
    params: list[Any] = []
    if model_version is not None:
        _require_columns(con, table, {"model_version"})
        where.append("model_version = ?")
        params.append(model_version)
    if end_date is not None:
        where.append("as_of_date <= ?")
        params.append(end_date)
    sql = f"SELECT as_of_date, active_current_regime FROM {table} WHERE {' AND '.join(where)}"  # noqa: S608
    out: dict[str, str] = {}
    for row in con.execute(sql, params):
        out[str(row["as_of_date"])] = str(row["active_current_regime"]).strip()
    return out


# ---------------------------------------------------------------------------
# comparison compute
# ---------------------------------------------------------------------------
def compute_comparison(
    common_dates: list[str],
    v1: dict[str, str],
    h1: dict[str, str],
    *,
    gross_map: dict[str, float],
    budgets: dict[str, dict[str, float]],
    risk_off_regimes: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the daily comparison rows and the full stats bundle over the common covered dates."""
    order = CANONICAL_REGIMES + sorted(
        {lbl for d in common_dates for lbl in (v1[d], h1[d])} - set(CANONICAL_REGIMES)
    )
    daily: list[dict[str, Any]] = []
    pairs: list[tuple[str, str]] = []
    dated_pairs: list[tuple[str, str, str]] = []
    v1_gross_series: list[tuple[str, float]] = []
    h1_gross_series: list[tuple[str, float]] = []
    v1_labels_series: list[tuple[str, str]] = []
    h1_labels_series: list[tuple[str, str]] = []
    gross_diff_days = 0
    abs_gross_diff_sum = 0.0
    v1_budget_key_counts: Counter[str] = Counter()
    h1_budget_key_counts: Counter[str] = Counter()

    for d in common_dates:
        vl, hl = v1[d], h1[d]
        vg, hg = gross_for(vl, gross_map), gross_for(hl, gross_map)
        agree = int(vl == hl)
        daily.append({
            "date": d, "v1_label": vl, "h1_label": hl,
            "v1_gross": vg, "h1_gross": hg, "agree_flag": agree,
        })
        pairs.append((vl, hl))
        dated_pairs.append((d, vl, hl))
        v1_gross_series.append((d, vg))
        h1_gross_series.append((d, hg))
        v1_labels_series.append((d, vl))
        h1_labels_series.append((d, hl))
        v1_budget_key_counts[sleeve_budget_key(vl, risk_off_regimes)] += 1
        h1_budget_key_counts[sleeve_budget_key(hl, risk_off_regimes)] += 1
        if vg != hg:
            gross_diff_days += 1
        abs_gross_diff_sum += abs(vg - hg)

    n = len(common_dates)
    v1_switches = switches_per_year(v1_labels_series)
    h1_switches = switches_per_year(h1_labels_series)
    stats: dict[str, Any] = {
        "n_common_covered_dates": n,
        "date_range": {"start": common_dates[0], "end": common_dates[-1]} if common_dates else {},
        "label_agreement": {
            "overall_fraction": round(agreement_fraction(pairs), 6),
            "n_agree": sum(1 for v, h in pairs if v == h),
            "confusion_matrix_v1_rows_h1_cols": confusion_matrix(pairs, order),
            "confusion_order": order,
            "per_year": per_year_agreement(dated_pairs),
        },
        "transition_stats": {
            "v1_switches_per_year": v1_switches,
            "h1_switches_per_year": h1_switches,
            "v1_total_switches": sum(v1_switches.values()),
            "h1_total_switches": sum(h1_switches.values()),
        },
        "mapped_allocation_paths": {
            "sleeve_budget_day_counts": {
                "v1": dict(v1_budget_key_counts),
                "h1": dict(h1_budget_key_counts),
            },
            "gross_label_day_counts": {
                "v1": {lbl: cnt for lbl, cnt in sorted(Counter(v for _d, v in v1_labels_series).items())},
                "h1": {lbl: cnt for lbl, cnt in sorted(Counter(h for _d, h in h1_labels_series).items())},
            },
        },
        "turnover_proxy": {
            "note": "label->gross MAPPING churn only (sum |gross_t - gross_{t-1}|), NOT trading turnover",
            "v1_per_year": gross_turnover_per_year(v1_gross_series),
            "h1_per_year": gross_turnover_per_year(h1_gross_series),
            "v1_total": round(sum(gross_turnover_per_year(v1_gross_series).values()), 8),
            "h1_total": round(sum(gross_turnover_per_year(h1_gross_series).values()), 8),
            "gross_differ_days": gross_diff_days,
            "mean_abs_gross_diff": round(abs_gross_diff_sum / n, 8) if n else 0.0,
        },
    }
    if common_dates:
        last = common_dates[-1]
        vl, hl = v1[last], h1[last]
        stats["current_state"] = {
            "latest_common_covered_date": last,
            "v1_label": vl,
            "h1_label": hl,
            "agree": int(vl == hl),
            "v1_gross": gross_for(vl, gross_map),
            "h1_gross": gross_for(hl, gross_map),
            "v1_sleeve_budget": budgets.get(sleeve_budget_key(vl, risk_off_regimes), {}),
            "h1_sleeve_budget": budgets.get(sleeve_budget_key(hl, risk_off_regimes), {}),
        }
    return daily, stats


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _selftest() -> None:
    gross_map = {"EXPANSION_DISINFLATION": 1.0, "HEATING_UP": 1.0, "SLOW_GROWTH": 0.85,
                 "STAGFLATION": 0.70, "default": 0.85}
    budgets = {"default": {"long_core": 0.65, "medium_rotation": 0.35},
               "risk_off": {"long_core": 0.80, "medium_rotation": 0.20}}
    risk_off = {"STAGFLATION", "CONTRACTION", "CRISIS"}

    # 5 covered dates. On 2020-01-03 V1 and H1 deliberately DISAGREE (SLOW_GROWTH vs STAGFLATION).
    dates = ["2020-01-01", "2020-01-02", "2020-01-03", "2021-01-01", "2021-01-02"]
    v1 = {"2020-01-01": "EXPANSION_DISINFLATION", "2020-01-02": "EXPANSION_DISINFLATION",
          "2020-01-03": "SLOW_GROWTH", "2021-01-01": "SLOW_GROWTH", "2021-01-02": "HEATING_UP"}
    h1 = {"2020-01-01": "EXPANSION_DISINFLATION", "2020-01-02": "EXPANSION_DISINFLATION",
          "2020-01-03": "STAGFLATION", "2021-01-01": "SLOW_GROWTH", "2021-01-02": "HEATING_UP"}

    daily, stats = compute_comparison(dates, v1, h1, gross_map=gross_map, budgets=budgets,
                                      risk_off_regimes=risk_off)

    # agreement: 4 of 5 agree
    assert stats["label_agreement"]["n_agree"] == 4, stats["label_agreement"]
    assert abs(stats["label_agreement"]["overall_fraction"] - 0.8) < 1e-9

    # confusion: the single disagreement is V1=SLOW_GROWTH x H1=STAGFLATION
    cm = stats["label_agreement"]["confusion_matrix_v1_rows_h1_cols"]
    assert cm["SLOW_GROWTH"]["STAGFLATION"] == 1, cm
    assert cm["SLOW_GROWTH"]["SLOW_GROWTH"] == 1, cm
    assert cm["EXPANSION_DISINFLATION"]["EXPANSION_DISINFLATION"] == 2, cm

    # per-year agreement: 2020 has 2/3, 2021 has 2/2
    assert stats["label_agreement"]["per_year"]["2020"]["agree"] == 2
    assert stats["label_agreement"]["per_year"]["2021"]["n"] == 2

    # switches: V1 EXP->SLOW (01-03) then SLOW->HEAT (2021-01-02); H1 EXP->STAG (01-03) then STAG->SLOW(2021-01-01) then SLOW->HEAT(01-02)
    assert stats["transition_stats"]["v1_total_switches"] == 2, stats["transition_stats"]
    assert stats["transition_stats"]["h1_total_switches"] == 3, stats["transition_stats"]
    assert stats["transition_stats"]["v1_switches_per_year"]["2020"] == 1

    # gross: 2020-01-03 differs (0.85 vs 0.70) -> 1 differ-day; abs diff = 0.15 over that day
    assert stats["turnover_proxy"]["gross_differ_days"] == 1, stats["turnover_proxy"]
    assert abs(stats["turnover_proxy"]["mean_abs_gross_diff"] - 0.15 / 5) < 1e-9

    # turnover proxy V1: 1.0->1.0 (0) ->0.85 (0.15) ->0.85 (0) ->1.0 (0.15) = 0.30
    assert abs(stats["turnover_proxy"]["v1_total"] - 0.30) < 1e-9, stats["turnover_proxy"]
    # H1: 1.0->1.0(0)->0.70(0.30)->0.85(0.15)->1.0(0.15) = 0.60
    assert abs(stats["turnover_proxy"]["h1_total"] - 0.60) < 1e-9, stats["turnover_proxy"]

    # missing label -> default mapping
    assert abs(gross_for("SOME_UNKNOWN_REGIME", gross_map) - 0.85) < 1e-9
    assert sleeve_budget_key("SOME_UNKNOWN_REGIME", risk_off) == "default"
    assert sleeve_budget_key("STAGFLATION", risk_off) == "risk_off"

    # current state = last date (2021-01-02, HEATING_UP, agree)
    assert stats["current_state"]["latest_common_covered_date"] == "2021-01-02"
    assert stats["current_state"]["v1_label"] == "HEATING_UP" and stats["current_state"]["agree"] == 1
    assert stats["current_state"]["v1_sleeve_budget"]["long_core"] == 0.65

    # daily rows well-formed
    assert len(daily) == 5 and set(daily[0].keys()) == set(CSV_FIELDS)
    assert daily[2]["agree_flag"] == 0 and daily[0]["agree_flag"] == 1

    print("h1-v1 allocation comparison self-test: PASS")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Shadow-only diagnostic comparison of V1 vs H1 regime allocations.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--end-date", default=None, help="Optional inclusive upper bound (YYYY-MM-DD) on as_of_date.")
    p.add_argument("--sqlite-timeout", type=float, default=30.0, help="sqlite connect timeout (seconds).")
    p.add_argument("--busy-timeout-ms", type=int, default=30000, help="sqlite PRAGMA busy_timeout (ms).")
    p.add_argument("--selftest", action="store_true")
    return p.parse_args()


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)

    gross_map_raw = cfg_get(config, "black_litterman_fusion.regime_to_gross_scalar", {}) or {}
    gross_map = {str(k): float(v) for k, v in gross_map_raw.items()}
    budgets_raw = cfg_get(config, "sleeves.sleeve_risk_budgets", {}) or {}
    budgets = {str(k): {str(kk): float(vv) for kk, vv in (v or {}).items()} for k, v in budgets_raw.items()}
    risk_off_regimes = {str(r) for r in (cfg_get(config, "sleeves.risk_off_regimes", []) or [])}
    if "default" not in gross_map:
        LOGGER.error("black_litterman_fusion.regime_to_gross_scalar has no 'default' entry")
        return 1

    db_path = paths.macro_serving_db_path
    if not db_path.exists():
        LOGGER.error("Macro serving DB not found: %s", db_path)
        return 1

    con = connect_ro(db_path, timeout=args.sqlite_timeout, busy_timeout_ms=args.busy_timeout_ms)
    try:
        v1 = load_labels(con, "macro_regime_decision_daily", model_version=None, end_date=args.end_date)
        h1 = load_labels(con, "macro_regime_v2_decision_daily",
                         model_version=H1_MODEL_VERSION, end_date=args.end_date)
    except (sqlite3.Error, ValueError) as exc:
        LOGGER.error("Failed reading macro serving DB: %s", exc)
        con.close()
        return 1
    finally:
        con.close()

    common_dates = sorted(set(v1) & set(h1))
    if not common_dates:
        LOGGER.error("No common covered dates between V1 and H1 (v1_covered=%d, h1_covered=%d)",
                     len(v1), len(h1))
        return 1

    daily, stats = compute_comparison(
        common_dates, v1, h1, gross_map=gross_map, budgets=budgets, risk_off_regimes=risk_off_regimes,
    )

    end_date_used = common_dates[-1]
    out_dir = paths.output_dir / "h1_v1_comparison" / end_date_used
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "h1_v1_label_comparison.csv"
    json_path = out_dir / "h1_v1_comparison_summary.json"

    write_csv(csv_path, CSV_FIELDS, daily)
    csv_sha256 = sha256_file(csv_path)

    summary = {
        "diagnostic_only": True,
        "notice": DIAGNOSTIC_BANNER,
        "created_at_utc": utc_now(),
        "provenance": {
            "macro_serving_db_path": str(db_path),
            "config_path": str(config_path),
            "h1_model_version": H1_MODEL_VERSION,
            "v1_covered_rows": len(v1),
            "h1_covered_rows": len(h1),
            "common_covered_dates": len(common_dates),
            "end_date_arg": args.end_date,
            "end_date_used": end_date_used,
            "regime_to_gross_scalar": gross_map,
            "sleeve_risk_budgets": budgets,
            "risk_off_regimes": sorted(risk_off_regimes),
        },
        "files": {"h1_v1_label_comparison.csv": {"sha256": csv_sha256, "rows": len(daily)}},
        **stats,
    }
    write_manifest(json_path, summary)

    # --- readable log summary ---
    la = stats["label_agreement"]
    ts = stats["transition_stats"]
    tp = stats["turnover_proxy"]
    cs = stats["current_state"]
    LOGGER.info("=" * 88)
    LOGGER.info(DIAGNOSTIC_BANNER)
    LOGGER.info("=" * 88)
    LOGGER.info("Common covered dates: %d  (%s .. %s)", stats["n_common_covered_dates"],
                common_dates[0], common_dates[-1])
    LOGGER.info("V1 covered rows=%d  H1 covered rows=%d", len(v1), len(h1))
    LOGGER.info("Overall label agreement: %.4f (%d/%d)", la["overall_fraction"], la["n_agree"],
                stats["n_common_covered_dates"])
    LOGGER.info("Confusion matrix (rows=V1, cols=H1) over %s:", la["confusion_order"])
    order = la["confusion_order"]
    header = "  {:>26}".format("V1\\H1") + "".join(f"{h[:12]:>14}" for h in order)
    LOGGER.info(header)
    for v in order:
        line = "  {:>26}".format(v[:26]) + "".join(f"{la['confusion_matrix_v1_rows_h1_cols'][v][h]:>14d}" for h in order)
        LOGGER.info(line)
    LOGGER.info("Switches - V1 total=%d per_year=%s", ts["v1_total_switches"], ts["v1_switches_per_year"])
    LOGGER.info("Switches - H1 total=%d per_year=%s", ts["h1_total_switches"], ts["h1_switches_per_year"])
    LOGGER.info("Gross turnover proxy (mapping churn) - V1 total=%.4f  H1 total=%.4f",
                tp["v1_total"], tp["h1_total"])
    LOGGER.info("Days V1/H1 gross differ: %d  mean_abs_gross_diff=%.5f",
                tp["gross_differ_days"], tp["mean_abs_gross_diff"])
    LOGGER.info("Per-year agreement: %s", {y: d["fraction"] for y, d in la["per_year"].items()})
    LOGGER.info("CURRENT (%s): V1=%s gross=%.2f budget=%s | H1=%s gross=%.2f budget=%s | agree=%d",
                cs["latest_common_covered_date"], cs["v1_label"], cs["v1_gross"], cs["v1_sleeve_budget"],
                cs["h1_label"], cs["h1_gross"], cs["h1_sleeve_budget"], cs["agree"])
    LOGGER.info("Wrote %s", csv_path)
    LOGGER.info("Wrote %s", json_path)
    LOGGER.info("DIAGNOSTIC comparison complete -> %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
