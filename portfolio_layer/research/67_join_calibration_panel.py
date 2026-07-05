#!/usr/bin/env python3
"""Stage 11 - calibration panel: PIT state joins + score standardization onto the 66 targets.

For every (as_of_date, ticker) label row from research/66, joins portfolio-layer state AS OF the
snapshot date (never later):

  macro regime + risk bucket   macro_regime_decision_daily via MAX(as_of_date) <= snapshot date
  sector macro fit             sector/industry/aggregate fit tables through the Stage 6 taxonomy
  stock macro fit              stock_macro_fit_daily (exact ticker only; no fabricated fallback)
  foreign sleeve budget        foreign_sleeve_budget_daily
  rotation state               Stage 5 pure functions over the survivorship panel sliced to <= as-of
  sleeve assignment            runs/<as_of>/sleeves/sleeve_assignments.csv when that run exists
  risk eligibility             runs/<as_of>/risk/risk_coverage.csv when that run exists
  liquidity half-spread        runs/<as_of>/risk/spread_snapshot.csv when that run exists
  tech Stage 11 sidecar        <family>_stage11_survivorship_calibration_panel.csv in the sector's
                               dated folder (survivorship-corrected flags for tech-family rows)
  standardization              score_z / score_pct within (source_pipeline, as_of_date); the raw
                               native score stays untouched in the carried columns

Every optional join is availability-flagged (*_available = 0/1); missing state is blank, never
fabricated. Reuses macro/contract.py + macro/taxonomy.py + rotation builders — no new state math.

LOCKBOX: input rows come from 66, which already excludes sealed snapshots; this script re-verifies
(gate FAILs on any in_lockbox row) and refuses if the protocol/config mirror diverge.

`--probe DATE` logs the joined date-level state (regime, fits, rotation, budget) without writing
anything — a real-data smoke test usable before dev-window snapshots exist. `--selftest` checks the
standardization math on synthetic data.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.macro.contract import (  # noqa: E402
    finite_or_blank,
    open_macro_serving_db,
    rows_at_latest,
    single_latest_row,
    staleness_days,
)
from portfolio_layer.macro.taxonomy import select_sleeve_macro_fit, sleeve_taxonomy  # noqa: E402
from portfolio_layer.research.stage11_common import load_lockbox  # noqa: E402
from portfolio_layer.rotation.sector_rotation_selector import build_sector_rotation  # noqa: E402
from portfolio_layer.scores.adapters import dated_candidates  # noqa: E402


LOGGER = logging.getLogger("join_calibration_panel")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

SIDECAR_SUFFIX = "_stage11_survivorship_calibration_panel.csv"
RANK_TABLE_SUFFIX = "_final_rank_table.csv"

STATE_FIELDS = [
    "regime_available", "macro_regime", "macro_regime_next", "macro_regime_confidence",
    "regime_risk_bucket", "macro_regime_as_of", "macro_regime_staleness_days",
    "sector_macro_fit", "sector_macro_fit_level", "sector_macro_fit_fallback",
    "sector_macro_fit_as_of", "sector_macro_fit_staleness_days",
    "stock_macro_fit_available", "stock_macro_fit_z", "stock_macro_fit_as_of",
    "foreign_budget_active", "foreign_budget", "foreign_budget_as_of",
    "rotation_available", "rotation_state", "rotation_score_pct", "rotation_trend_state",
    "rotation_trend_gate", "rotation_multiplier",
    "sleeve_join_available", "sleeve_assignment",
    "risk_join_available", "risk_eligible", "risk_status",
    "liquidity_join_available", "liquidity_half_spread_bps",
    "sidecar_available", "sidecar_survivorship_corrected", "sidecar_stage11_eligible",
    "sidecar_sample_role", "sidecar_membership_status", "sidecar_terminal_date",
    "sidecar_score_recomputed_pit",
    "score_z_pipeline_date", "score_pct_pipeline_date",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 calibration panel: PIT state joins + standardization.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--targets-build", type=iso_date_arg, default=None,
                   help="calibration_targets build (= survivorship panel date) to consume (default: latest).")
    p.add_argument("--probe", type=iso_date_arg, default=None, metavar="DATE",
                   help="Log joined date-level state for DATE and exit; writes nothing.")
    p.add_argument("--selftest", action="store_true", help="Run synthetic standardization self-tests and exit.")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# pure helpers (self-tested)
# ---------------------------------------------------------------------------
def risk_bucket(regime: str, risk_off_regimes: list[str]) -> str:
    return "risk_off" if str(regime).strip().upper() in {str(r).upper() for r in risk_off_regimes} else "default"


def standardize_groups(rows: list[dict[str, Any]], *, score_field: str = "native_score") -> None:
    """In-place score_z_pipeline_date / score_pct_pipeline_date within (source_pipeline, as_of_date).

    z uses ddof=1 and needs n >= 2 with positive spread; percentile is the average-rank percentile in
    [0, 100]. Rows whose score does not parse get blanks. Raw scores are never modified.
    """
    groups: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for i, r in enumerate(rows):
        r["score_z_pipeline_date"] = ""
        r["score_pct_pipeline_date"] = ""
        try:
            val = float(str(r.get(score_field, "")).strip())
        except (TypeError, ValueError):
            continue
        if not np.isfinite(val):
            continue
        groups.setdefault((str(r.get("source_pipeline", "")), str(r.get("as_of_date", ""))), []).append((i, val))
    for members in groups.values():
        vals = np.array([v for _, v in members], dtype=float)
        n = len(vals)
        order = vals.argsort(kind="mergesort")
        ranks = np.empty(n, dtype=float)
        ranks[order] = np.arange(n, dtype=float)
        # average ties so equal scores get equal percentiles
        for uniq in np.unique(vals):
            mask = vals == uniq
            if mask.sum() > 1:
                ranks[mask] = ranks[mask].mean()
        pct = (ranks + 0.5) / n * 100.0
        std = float(vals.std(ddof=1)) if n >= 2 else 0.0
        mean = float(vals.mean())
        for (i, _), z_ok_pct, raw in zip(members, pct, vals):
            rows[i]["score_pct_pipeline_date"] = round(float(z_ok_pct), 6)
            if std > 0.0:
                rows[i]["score_z_pipeline_date"] = round((raw - mean) / std, 8)


def sidecar_path_for(rank_table: Path) -> Path:
    return rank_table.with_name(rank_table.name.replace(RANK_TABLE_SUFFIX, SIDECAR_SUFFIX))


def _selftest() -> None:
    rows: list[dict[str, Any]] = [
        {"source_pipeline": "a", "as_of_date": "2024-01-02", "native_score": "1.0"},
        {"source_pipeline": "a", "as_of_date": "2024-01-02", "native_score": "2.0"},
        {"source_pipeline": "a", "as_of_date": "2024-01-02", "native_score": "3.0"},
        {"source_pipeline": "a", "as_of_date": "2024-01-03", "native_score": "7.0"},   # singleton group
        {"source_pipeline": "b", "as_of_date": "2024-01-02", "native_score": "5.0"},
        {"source_pipeline": "b", "as_of_date": "2024-01-02", "native_score": "5.0"},   # tie, zero spread
        {"source_pipeline": "b", "as_of_date": "2024-01-02", "native_score": ""},      # unparseable
    ]
    standardize_groups(rows)
    assert abs(rows[0]["score_z_pipeline_date"] + 1.0) < 1e-9, rows[0]
    assert abs(rows[1]["score_z_pipeline_date"]) < 1e-9, rows[1]
    assert abs(rows[2]["score_z_pipeline_date"] - 1.0) < 1e-9, rows[2]
    zs = [float(rows[i]["score_z_pipeline_date"]) for i in range(3)]
    assert abs(float(np.mean(zs))) < 1e-9 and abs(float(np.std(zs, ddof=1)) - 1.0) < 1e-9
    assert abs(rows[0]["score_pct_pipeline_date"] - 100.0 / 6.0) < 1e-6, rows[0]
    assert abs(rows[2]["score_pct_pipeline_date"] - 500.0 / 6.0) < 1e-6, rows[2]
    assert rows[3]["score_z_pipeline_date"] == "" and rows[3]["score_pct_pipeline_date"] == 50.0, rows[3]
    assert rows[4]["score_z_pipeline_date"] == "" and rows[4]["score_pct_pipeline_date"] == 50.0, rows[4]
    assert rows[6]["score_z_pipeline_date"] == "" and rows[6]["score_pct_pipeline_date"] == "", rows[6]
    assert risk_bucket("STAGFLATION", ["STAGFLATION", "CRISIS"]) == "risk_off"
    assert risk_bucket("expansion", ["STAGFLATION", "CRISIS"]) == "default"
    assert risk_bucket("", ["STAGFLATION"]) == "default"
    got = sidecar_path_for(Path("x/2026-01-02/semiconductor_final_rank_table.csv"))
    assert got.name == "semiconductor_stage11_survivorship_calibration_panel.csv", got
    print("calibration-panel self-test: PASS")


# ---------------------------------------------------------------------------
# per-date state (cached by the caller; every query is <= as_of)
# ---------------------------------------------------------------------------
def macro_state(conn, as_of: str, *, taxonomy: dict[str, dict[str, Any]], pipelines: list[str],
                risk_off_regimes: list[str]) -> dict[str, Any]:
    regime_row = single_latest_row(conn, "macro_regime_decision_daily", as_of)
    regime: dict[str, Any] = {
        "regime_available": 0, "macro_regime": "", "macro_regime_next": "",
        "macro_regime_confidence": "", "regime_risk_bucket": "", "macro_regime_as_of": "",
        "macro_regime_staleness_days": "",
    }
    if regime_row is not None:
        current = str(regime_row["active_current_regime"] or "")
        stale = staleness_days(as_of, str(regime_row["as_of_date"]))
        regime.update({
            "regime_available": 1, "macro_regime": current,
            "macro_regime_next": str(regime_row["active_next_regime"] or ""),
            "macro_regime_confidence": finite_or_blank(regime_row["current_confidence"]),
            "regime_risk_bucket": risk_bucket(current, risk_off_regimes),
            "macro_regime_as_of": str(regime_row["as_of_date"]),
            "macro_regime_staleness_days": "" if stale is None else stale,
        })
    sector_as_of, sector_rows = rows_at_latest(conn, "sector_macro_fit_daily", as_of)
    industry_as_of, industry_rows = rows_at_latest(conn, "industry_macro_fit_daily", as_of)
    aggregate_as_of, aggregate_rows = rows_at_latest(conn, "industry_aggregate_macro_fit_daily", as_of)
    fits: dict[str, dict[str, Any]] = {}
    for pipe in pipelines:
        fit = select_sleeve_macro_fit(
            run_as_of=as_of, source_pipeline=pipe, taxonomy=taxonomy.get(pipe, {}),
            sector_as_of=sector_as_of, sector_rows=sector_rows,
            industry_as_of=industry_as_of, industry_rows=industry_rows,
            aggregate_as_of=aggregate_as_of, aggregate_rows=aggregate_rows,
        )
        fits[pipe] = {
            "sector_macro_fit": fit.macro_fit_score, "sector_macro_fit_level": fit.macro_level,
            "sector_macro_fit_fallback": fit.fallback_used, "sector_macro_fit_as_of": fit.macro_as_of_date,
            "sector_macro_fit_staleness_days": fit.staleness_days,
        }
    stock_as_of, stock_rows = rows_at_latest(conn, "stock_macro_fit_daily", as_of)
    stock_fit = {
        str(r["ticker"]).strip().upper(): finite_or_blank(r["macro_stock_fit_z"])
        for r in stock_rows if r["ticker"] is not None
    }
    budget_as_of, budget_rows = rows_at_latest(conn, "foreign_sleeve_budget_daily", as_of)
    budget: dict[str, Any] = {"foreign_budget_active": "", "foreign_budget": "", "foreign_budget_as_of": ""}
    if budget_rows:
        budget = {
            "foreign_budget_active": finite_or_blank(budget_rows[0]["active_flag"]),
            "foreign_budget": finite_or_blank(budget_rows[0]["foreign_budget"]),
            "foreign_budget_as_of": budget_as_of or "",
        }
    return {"regime": regime, "fits": fits, "stock_fit": stock_fit, "stock_fit_as_of": stock_as_of or "",
            "budget": budget}


def rotation_state(prices: pd.DataFrame, as_of: str, *, config: dict[str, Any],
                   lookback: int) -> dict[str, dict[str, Any]]:
    """Per-pipeline rotation state from the survivorship panel sliced to trading days <= as_of."""
    idx = np.array([str(d) for d in prices.index])
    pos = int(np.searchsorted(idx, as_of, side="right"))
    if pos == 0:
        return {}
    window = prices.iloc[max(0, pos - lookback):pos].apply(pd.to_numeric, errors="coerce")
    returns = window.pct_change(fill_method=None)
    sector_etf_map = {str(k).strip(): str(v).strip().upper()
                      for k, v in (cfg_get(config, "risk_panel.sector_etf_map", {}) or {}).items()}
    rows = build_sector_rotation(
        window, returns,
        sector_etf_map=sector_etf_map,
        rank_universe=[str(t).strip().upper() for t in cfg_get(config, "rotation.rank_universe_etfs", []) or []],
        windows=[int(w) for w in cfg_get(config, "rotation.momentum_windows_days", [21, 63, 126])],
        weights=[float(w) for w in cfg_get(config, "rotation.momentum_weights", [0.5, 0.3, 0.2])],
        ma_days=int(cfg_get(config, "rotation.trend_filter.ma_days", 200)),
        slope_lookback=int(cfg_get(config, "rotation.trend_filter.slope_lookback_days", 21)),
        positive_score_pct=float(cfg_get(config, "rotation.state_thresholds.positive_score_pct", 60.0)),
        negative_score_pct=float(cfg_get(config, "rotation.state_thresholds.negative_score_pct", 40.0)),
        mult_min=float(cfg_get(config, "rotation.tilt.mult_min", 0.7)),
        mult_max=float(cfg_get(config, "rotation.tilt.mult_max", 1.3)),
    )
    return {str(r["source_pipeline"]): r for r in rows}


def run_dir_joins(runs_root: Path, as_of: str) -> dict[str, Any]:
    """Availability-flagged per-ticker joins from a same-date sealed run, when one exists."""
    out: dict[str, Any] = {"risk": None, "sleeve": None, "liquidity": None}
    run_dir = runs_root / as_of
    risk_path = run_dir / "risk" / "risk_coverage.csv"
    if risk_path.exists():
        out["risk"] = {str(r.get("ticker", "")).strip().upper():
                       (str(r.get("risk_eligible", "")), str(r.get("risk_status", "")))
                       for r in read_csv(risk_path)}
    sleeve_path = run_dir / "sleeves" / "sleeve_assignments.csv"
    if sleeve_path.exists():
        out["sleeve"] = {str(r.get("ticker", "")).strip().upper(): str(r.get("sleeve", ""))
                         for r in read_csv(sleeve_path)}
    spread_path = run_dir / "risk" / "spread_snapshot.csv"
    if spread_path.exists():
        liq: dict[str, str] = {}
        for r in read_csv(spread_path):
            ticker = str(r.get("ticker", "")).strip().upper()
            for col in ("median_half_spread_bps", "half_spread_bps"):
                if str(r.get(col, "")).strip():
                    liq[ticker] = str(r.get(col, "")).strip()
                    break
        out["liquidity"] = liq
    return out


def _sidecar_fields(row: dict[str, str]) -> dict[str, str]:
    return {
        "sidecar_survivorship_corrected": str(row.get("survivorship_corrected_panel_flag", "")),
        "sidecar_stage11_eligible": str(row.get("stage11_calibration_input_eligible_flag", "")),
        "sidecar_sample_role": str(row.get("calibration_sample_role", "")),
        "sidecar_membership_status": str(row.get("membership_status", "")),
        "sidecar_terminal_date": str(row.get("terminal_date", "")),
        "sidecar_score_recomputed_pit": str(row.get("score_recomputed_pit_flag", "")),
    }


def build_sidecar_index(config: dict[str, Any], config_path: Path,
                        used: dict[str, str]) -> dict[str, dict[str, dict[str, dict[str, str]]]]:
    """pipeline -> as_of (ISO) -> ticker -> sidecar flag fields, built ONCE over every source.

    The tech generators have published Stage 11 survivorship panels in two layouts:
      legacy:       a per-date sidecar CSV next to each dated rank table
      consolidated: <prefix>_stage11_survivorship_calibration_panel.csv at the dashboard root
                    (recent/live dates) plus stage11_combined/<prefix>_..._panel_<start>_<end>.csv
                    historical range chunks
    All sources are indexed; later sources override earlier per (as_of, ticker):
    range chunks (ascending range) -> root panel -> per-date sidecars (authoritative for their date).
    Every file consumed is sha256-recorded into `used`.
    """
    sector_root = resolve_path(
        cfg_get(config, "score_contract.sector_output_root", "../output"), base_dir=config_path.parent
    )
    index: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
    for cfg in cfg_get(config, "score_contract.sectors", []) or []:
        if not bool(cfg.get("enabled", True)) or str(cfg.get("file_mode", "flat")) != "dated":
            continue
        rank_name = Path(str(cfg.get("file_path", ""))).name
        if RANK_TABLE_SUFFIX not in rank_name:
            continue
        pipe = str(cfg.get("model_family"))
        candidates = dated_candidates(cfg, sector_root)
        if not candidates:
            continue
        prefix = rank_name.replace(RANK_TABLE_SUFFIX, "")
        dashboard_root = candidates[0][1].parent.parent
        per_pipe = index.setdefault(pipe, {})

        def ingest(path: Path, *, only_asof: str | None = None) -> None:
            for r in read_csv(path):
                ticker = str(r.get("ticker", "")).strip().upper()
                asof = str(r.get("asof_date", "")).strip()[:10] or (only_asof or "")
                if not ticker or not asof:
                    continue
                if only_asof is not None and asof != only_asof:
                    continue
                per_pipe.setdefault(asof, {})[ticker] = _sidecar_fields(r)
            used[f"{pipe}:{path.name}"] = sha256_file(path)

        # Precedence (later ingests override earlier per (as_of, ticker)): root panel first, then
        # range chunks ascending — a regenerated chunk supersedes both the stale root panel and any
        # older, narrower chunk covering the same dates. Legacy per-date sidecars stay authoritative.
        root_panel = dashboard_root / f"{prefix}{SIDECAR_SUFFIX}"
        if root_panel.exists():
            ingest(root_panel)
        chunk_dir = dashboard_root / "stage11_combined"
        if chunk_dir.exists():
            range_re = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.csv$")
            chunks = []
            for path in chunk_dir.glob(f"{prefix}_stage11_survivorship_calibration_panel_*.csv"):
                m = range_re.search(path.name)
                if m:
                    chunks.append(((m.group(2), m.group(1)), path))  # sort by (end, start), not name
            for _key, path in sorted(chunks):
                ingest(path)
        for compact, rank_path in candidates:
            sidecar = sidecar_path_for(rank_path)
            if sidecar.exists():
                iso = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
                ingest(sidecar, only_asof=iso)
    return index


def _latest_targets(targets_root: Path, wanted: str | None) -> Path | None:
    if wanted:
        cand = targets_root / wanted
        return cand if (cand / "targets_manifest.json").exists() else None
    if not targets_root.exists():
        return None
    builds = sorted(p for p in targets_root.iterdir()
                    if p.is_dir() and (p / "targets_manifest.json").exists())
    return builds[-1] if builds else None


def _latest_panel(panel_root: Path) -> Path | None:
    if not panel_root.exists():
        return None
    builds = sorted(p for p in panel_root.iterdir()
                    if p.is_dir() and (p / "survivorship_manifest.json").exists())
    return builds[-1] if builds else None


def _load_accepted_panel(
    panel_dir: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> tuple[pd.DataFrame | None, list[str]]:
    manifest_path = panel_dir / "survivorship_manifest.json"
    prices_path = panel_dir / "prices_adjclose.csv"
    errors: list[str] = []
    if not manifest_path.exists():
        return None, [f"{panel_dir.name}:missing_survivorship_manifest"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"{panel_dir.name}:unreadable_survivorship_manifest:{type(exc).__name__}"]
    if manifest.get("acceptance") != "PASS":
        errors.append(f"{panel_dir.name}:survivorship_acceptance={manifest.get('acceptance')}")
    actual_manifest_hash = sha256_file(manifest_path)
    if expected_manifest_sha256 and expected_manifest_sha256 != actual_manifest_hash:
        errors.append(
            f"{panel_dir.name}:survivorship_manifest_hash_mismatch "
            f"targets={expected_manifest_sha256[:12]} actual={actual_manifest_hash[:12]}"
        )
    if not prices_path.exists():
        errors.append(f"{panel_dir.name}:missing_prices_adjclose.csv")
    if errors:
        return None, errors
    panel_prices = pd.read_csv(prices_path, index_col=0)
    panel_prices.columns = [str(c).strip().upper() for c in panel_prices.columns]
    return panel_prices, []


def _rotation_row_available(rot: dict[str, Any] | None) -> bool:
    if not rot:
        return False
    return str(rot.get("present_in_panel", "")).strip().lower() in {"1", "1.0", "true", "yes", "y"}


def _rotation_required_history(config: dict[str, Any]) -> int:
    windows = [int(w) for w in cfg_get(config, "rotation.momentum_windows_days", [21, 63, 126]) or []]
    max_momentum_rows = (max(windows) + 1) if windows else 0
    trend_rows = int(cfg_get(config, "rotation.trend_filter.ma_days", 200)) + int(
        cfg_get(config, "rotation.trend_filter.slope_lookback_days", 21)
    )
    return max(max_momentum_rows, trend_rows)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        lockbox = load_lockbox(config, config_path)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    taxonomy = sleeve_taxonomy(config)
    pipelines = [str(s.get("model_family")) for s in cfg_get(config, "score_contract.sectors", []) or []
                 if bool(s.get("enabled", True))]
    risk_off = [str(r) for r in cfg_get(config, "sleeves.risk_off_regimes", []) or []]
    cp = cfg_get(config, "calibration_panel", {}) or {}
    rotation_lookback = int(cp.get("rotation_lookback_trading_days", 504))

    panel_root = paths.output_dir / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
    panel_prices: pd.DataFrame | None = None
    panel_build = ""

    if args.probe:
        as_of = args.probe
        probe_panel = _latest_panel(panel_root)
        if probe_panel is not None:
            panel_prices, panel_errors = _load_accepted_panel(probe_panel)
            if panel_errors:
                LOGGER.warning("probe %s rotation skipped; latest panel rejected: %s", as_of, panel_errors[:8])
            else:
                panel_build = probe_panel.name
        conn = open_macro_serving_db(paths.macro_serving_db_path)
        try:
            state = macro_state(conn, as_of, taxonomy=taxonomy, pipelines=pipelines, risk_off_regimes=risk_off)
        finally:
            conn.close()
        LOGGER.info("probe %s regime=%s", as_of, json.dumps(state["regime"]))
        for pipe, fit in state["fits"].items():
            LOGGER.info("probe %s fit %-24s %s", as_of, pipe, json.dumps(fit))
        LOGGER.info("probe %s foreign_budget=%s stock_fits=%d (as_of=%s)",
                    as_of, json.dumps(state["budget"]), len(state["stock_fit"]), state["stock_fit_as_of"])
        if panel_prices is not None:
            rot = rotation_state(panel_prices, as_of, config=config, lookback=rotation_lookback)
            for pipe, row in rot.items():
                LOGGER.info("probe %s rotation %-24s state=%s score_pct=%s trend=%s mult=%s present=%s",
                            as_of, pipe, row.get("state"), row.get("score_pct"), row.get("trend_state"),
                            row.get("rotation_multiplier"), row.get("present_in_panel"))
        else:
            LOGGER.info("probe %s rotation skipped (no survivorship panel build under %s)", as_of, panel_root)
        return 0

    targets_root = paths.output_dir / str(cfg_get(config, "calibration_targets.dir", "calibration_targets"))
    targets_dir = _latest_targets(targets_root, args.targets_build)
    if targets_dir is None:
        LOGGER.error("No calibration-targets build found under %s; run research/66 first", targets_root)
        return 1
    targets_manifest = json.loads((targets_dir / "targets_manifest.json").read_text(encoding="utf-8"))
    if targets_manifest.get("acceptance") != "PASS":
        LOGGER.error("Targets build %s acceptance=%s; refusing", targets_dir.name, targets_manifest.get("acceptance"))
        return 1
    if targets_manifest.get("panel_build") and str(targets_manifest.get("panel_build")) != targets_dir.name:
        LOGGER.error(
            "Targets build directory %s does not match manifest panel_build=%s",
            targets_dir.name,
            targets_manifest.get("panel_build"),
        )
        return 1
    target_panel_dir = panel_root / targets_dir.name
    panel_prices, panel_errors = _load_accepted_panel(
        target_panel_dir,
        expected_manifest_sha256=str(targets_manifest.get("panel_manifest_sha256") or ""),
    )
    if panel_errors or panel_prices is None:
        LOGGER.error("Targets build %s requires matching accepted survivorship panel; %s", targets_dir.name, panel_errors[:8])
        return 1
    panel_build = target_panel_dir.name
    with (targets_dir / "calibration_targets.csv").open(encoding="utf-8", newline="") as handle:
        header = handle.readline().strip()
    target_fields = header.split(",") if header else []
    rows: list[dict[str, Any]] = [dict(r) for r in read_csv(targets_dir / "calibration_targets.csv")]

    out_dir = paths.output_dir / str(cp.get("dir", "calibration_panel")) / targets_dir.name
    panel_path = out_dir / "calibration_panel.csv"
    manifest_path = out_dir / "calibration_panel_manifest.json"
    if args.force:
        for p in (panel_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([panel_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    runs_root = paths.output_dir / "runs"
    sidecar_hashes: dict[str, str] = {}
    macro_cache: dict[str, dict[str, Any]] = {}
    rotation_cache: dict[str, dict[str, dict[str, Any]]] = {}
    rundir_cache: dict[str, dict[str, Any]] = {}
    sidecar_index = build_sidecar_index(config, config_path, sidecar_hashes)
    conn = open_macro_serving_db(paths.macro_serving_db_path)
    try:
        for r in rows:
            as_of = str(r.get("as_of_date", ""))
            ticker = str(r.get("ticker", "")).strip().upper()
            pipe = str(r.get("source_pipeline", ""))
            if as_of not in macro_cache:
                macro_cache[as_of] = macro_state(conn, as_of, taxonomy=taxonomy, pipelines=pipelines,
                                                 risk_off_regimes=risk_off)
                rotation_cache[as_of] = (rotation_state(panel_prices, as_of, config=config,
                                                        lookback=rotation_lookback)
                                         if panel_prices is not None else {})
                rundir_cache[as_of] = run_dir_joins(runs_root, as_of)
            state = macro_cache[as_of]
            r.update(state["regime"])
            r.update(state["fits"].get(pipe, {
                "sector_macro_fit": "", "sector_macro_fit_level": "", "sector_macro_fit_fallback": "",
                "sector_macro_fit_as_of": "", "sector_macro_fit_staleness_days": "",
            }))
            stock_z = state["stock_fit"].get(ticker, "")
            r.update({
                "stock_macro_fit_available": 1 if stock_z != "" else 0,
                "stock_macro_fit_z": stock_z,
                "stock_macro_fit_as_of": state["stock_fit_as_of"] if stock_z != "" else "",
            })
            r.update(state["budget"])
            rot = rotation_cache[as_of].get(pipe)
            rot_available = _rotation_row_available(rot)
            rot_row = rot if rot_available and rot is not None else {}
            r.update({
                "rotation_available": 1 if rot_available else 0,
                "rotation_state": rot_row.get("state", ""),
                "rotation_score_pct": rot_row.get("score_pct", ""),
                "rotation_trend_state": rot_row.get("trend_state", ""),
                "rotation_trend_gate": rot_row.get("trend_gate", ""),
                "rotation_multiplier": rot_row.get("rotation_multiplier", ""),
            })
            joins = rundir_cache[as_of]
            risk_hit = joins["risk"].get(ticker) if joins["risk"] is not None else None
            r.update({
                "risk_join_available": 1 if joins["risk"] is not None else 0,
                "risk_eligible": risk_hit[0] if risk_hit else "",
                "risk_status": risk_hit[1] if risk_hit else "",
            })
            r.update({
                "sleeve_join_available": 1 if joins["sleeve"] is not None else 0,
                "sleeve_assignment": joins["sleeve"].get(ticker, "") if joins["sleeve"] is not None else "",
            })
            r.update({
                "liquidity_join_available": 1 if joins["liquidity"] is not None else 0,
                "liquidity_half_spread_bps": joins["liquidity"].get(ticker, "")
                if joins["liquidity"] is not None else "",
            })
            side = sidecar_index.get(pipe, {}).get(as_of, {}).get(ticker)
            r.update({"sidecar_available": 1 if side else 0,
                      "sidecar_survivorship_corrected": "", "sidecar_stage11_eligible": "",
                      "sidecar_sample_role": "", "sidecar_membership_status": "",
                      "sidecar_terminal_date": "", "sidecar_score_recomputed_pit": ""})
            if side:
                r.update(side)
    finally:
        conn.close()

    standardize_groups(rows)

    # ---- gates ----
    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    leaked = [r for r in rows if str(r.get("in_lockbox", "0")) == "1"]
    rec("lockbox_consistency", "PASS" if not leaked else "FAIL",
        f"targets build {targets_dir.name} acceptance=PASS, sealed rows=0"
        if not leaked else f"{len(leaked)} sealed-window rows reached the panel")
    missing_regime = [r for r in rows if int(r.get("regime_available", 0)) != 1]
    rec("regime_join_complete", "PASS" if not missing_regime else "FAIL",
        f"regime joined on all {len(rows)} rows" if not missing_regime
        else f"{len(missing_regime)} rows missing a PIT regime row")
    lookahead = []
    for r in rows:
        as_of = str(r.get("as_of_date", ""))
        for col in ("macro_regime_as_of", "sector_macro_fit_as_of", "stock_macro_fit_as_of", "foreign_budget_as_of"):
            val = str(r.get(col, ""))
            if val and val > as_of:
                lookahead.append(f"{as_of}:{r.get('ticker')}:{col}={val}")
    rec("pit_no_lookahead", "PASS" if not lookahead else "FAIL",
        "every joined macro as-of <= snapshot as-of" if not lookahead else f"{lookahead[:8]}")
    if rows:
        rotation_required_rows = _rotation_required_history(config)
        panel_index = np.array([str(d) for d in panel_prices.index]) if panel_prices is not None else np.array([])
        rotation_gate_rows = [
            r for r in rows
            if int(np.searchsorted(panel_index, str(r.get("as_of_date", "")), side="right")) >= rotation_required_rows
        ]
        warmup_rows = len(rows) - len(rotation_gate_rows)
        if rotation_gate_rows:
            rot_frac = sum(int(r.get("rotation_available", 0)) for r in rotation_gate_rows) / len(rotation_gate_rows)
            detail = (
                f"rotation state on {rot_frac:.1%} of eligible rows "
                f"(eligible={len(rotation_gate_rows)}, warmup_unavailable={warmup_rows}, "
                f"required_history_rows={rotation_required_rows}, panel build {panel_build or 'NONE'})"
            )
        else:
            rot_frac = 1.0
            detail = (
                f"no rows have enough trailing rotation history yet "
                f"(warmup_unavailable={warmup_rows}, required_history_rows={rotation_required_rows}, "
                f"panel build {panel_build or 'NONE'})"
            )
        rec("rotation_join_coverage",
            "PASS" if rot_frac >= 0.99 else ("WARN" if rot_frac > 0.0 else "FAIL"),
            detail)
    else:
        rec("rotation_join_coverage", "PASS", "no rows (all snapshots sealed or store empty)")
    z_bad: list[str] = []
    by_group: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        z = r.get("score_z_pipeline_date", "")
        if z != "":
            by_group.setdefault((str(r.get("source_pipeline", "")), str(r.get("as_of_date", ""))), []).append(float(z))
    for (pipe, as_of), zs in by_group.items():
        if len(zs) >= 8:
            if abs(float(np.mean(zs))) > 1e-8 or abs(float(np.std(zs, ddof=1)) - 1.0) > 1e-6:
                z_bad.append(f"{pipe}:{as_of}")
    rec("standardization_sane", "PASS" if not z_bad else "FAIL",
        f"{len(by_group)} (pipeline, date) groups standardized" if not z_bad else f"{z_bad[:8]}")
    tech_rows = [r for r in rows
                 if str(r.get("as_of_date", "")) in sidecar_index.get(str(r.get("source_pipeline", "")), {})]
    if tech_rows:
        side_frac = sum(int(r.get("sidecar_available", 0)) for r in tech_rows) / len(tech_rows)
        rec("tech_sidecar_join", "PASS" if side_frac >= 0.95 else "WARN",
            f"sidecar flags on {side_frac:.1%} of tech-family rows with a published sidecar")
    else:
        rec("tech_sidecar_join", "PASS", "no tech-family rows with a published sidecar (nothing to join)")

    out_fields = target_fields + STATE_FIELDS
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(panel_path, out_fields, rows)
    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    manifest = {
        "stage": "stage11_calibration_panel",
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "targets_build": targets_dir.name,
        "targets_manifest_sha256": sha256_file(targets_dir / "targets_manifest.json"),
        "survivorship_panel_build": panel_build,
        "survivorship_panel_manifest_sha256": sha256_file(panel_root / panel_build / "survivorship_manifest.json")
        if panel_build else "",
        "macro_serving_db": str(paths.macro_serving_db_path),
        "protocol_sha256": lockbox["protocol_sha256"],
        "rows": len(rows),
        "snapshots_joined": sorted(macro_cache),
        "sidecar_files_sha256": sidecar_hashes,
        "checks": checks,
        "files": {"calibration_panel.csv": {"sha256": sha256_file(panel_path), "rows": len(rows)}},
    }
    write_manifest(manifest_path, manifest)
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("CALIBRATION PANEL: %s (rows=%d, snapshots=%d) -> %s",
                "PASS" if passed else "FAIL", len(rows), len(macro_cache), out_dir)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
