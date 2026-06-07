#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_ASOF = "2026-06-05"


FORM4_FIELDS = [
    "insider_accumulation_score",
    "insider_buy_count_90d",
    "insider_buy_value_90d",
    "insider_buy_cluster_count_90d",
]
BORROW_SHORT_13F_FIELDS = [
    "short_interest_shares",
    "float_shares",
    "short_interest_pct_float",
    "days_to_cover",
    "float_shares_source",
    "float_shares_asof_date",
    "float_shares_source_asof_date",
    "float_shares_staleness_days",
    "float_shares_measurement_staleness_days",
    "float_shares_proxy_flag",
    "public_float_usd",
    "public_float_price_date",
    "public_float_close_price",
    "short_interest_pct_float_available_flag",
    "short_interest_pct_score",
    "short_interest_days_to_cover_score",
    "short_interest_signal_basis",
    "short_interest_signal_max_possible_score",
    "short_interest_signal_score",
    "borrow_rate_current",
    "borrow_fee_data_available_flag",
    "shortable_data_available_flag",
    "borrow_fee_stale_flag",
    "shortable_stale_flag",
    "borrow_fee_staleness_days",
    "shortable_staleness_days",
    "borrow_fee_history_count_30d",
    "borrow_fee_history_count_90d",
    "borrow_rate_spike_flag",
    "hard_to_borrow_flag",
    "shares_shortable_k",
    "borrow_pressure_score",
    "high_borrow_pressure_flag",
    "elevated_borrow_pressure_flag",
    "borrow_rate_high_flag",
    "borrow_squeeze_setup_flag",
    "borrow_distress_flag",
    "institutional_ownership_delta_pct",
    "institutional_accumulation_score",
]
CTGOV_FIELDS = [
    "forward_catalyst_score",
    "forward_catalyst_unfiltered_score",
    "ctgov_forward_catalyst_score",
    "ctgov_forward_catalyst_guardrail_pass",
    "forward_catalyst_source",
]
SHADOW_MOVE_FIELDS = [
    "insider_accumulation_score",
    "short_interest_signal_score",
    "short_interest_signal_max_possible_score",
    "float_shares_source",
    "float_shares_proxy_flag",
    "borrow_pressure_score",
    "borrow_rate_current",
    "borrow_fee_data_available_flag",
    "shortable_data_available_flag",
    "borrow_fee_history_count_90d",
    "borrow_rate_spike_flag",
    "hard_to_borrow_flag",
    "high_borrow_pressure_flag",
    "elevated_borrow_pressure_flag",
    "borrow_rate_high_flag",
    "borrow_squeeze_setup_flag",
    "borrow_distress_flag",
    "institutional_accumulation_score",
    "institutional_ownership_delta_pct",
    "forward_catalyst_unfiltered_score",
    "ctgov_forward_catalyst_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit a biotech daily report run for allocation/discovery separation, "
            "data-source population, and rank-change watchlist diagnostics."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default=DEFAULT_ASOF)
    parser.add_argument("--prior-asof", type=str, default="")
    parser.add_argument("--report-dir", type=Path, default=None)
    parser.add_argument("--prior-report-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--rank-move-threshold", type=int, default=10)
    return parser.parse_args()


def parse_date_token(raw: str) -> str:
    clean = str(raw or "").strip()
    if not clean:
        raise ValueError("Missing as-of date.")
    if len(clean) == 8 and clean.isdigit():
        return f"{clean[:4]}-{clean[4:6]}-{clean[6:]}"
    parsed = date.fromisoformat(clean)
    return parsed.isoformat()


def date_dir_name(asof: str) -> str:
    return parse_date_token(asof).replace("-", "")


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fieldnames = keys or ["message"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def to_float(raw: object, default: float | None = None) -> float | None:
    try:
        value = float(str(raw).strip()) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        return default
    if value is None or not math.isfinite(value):
        return default
    return value


def nonblank(raw: object) -> bool:
    return str(raw if raw is not None else "").strip() != ""


def status_row(check: str, status: str, value: object, details: str = "") -> dict[str, Any]:
    return {"check": check, "status": status, "value": value, "details": details}


def latest_prior_report_dir(base_output_dir: Path, asof: str) -> Path | None:
    current = date_dir_name(asof)
    candidates: list[Path] = []
    if not base_output_dir.exists():
        return None
    for path in base_output_dir.iterdir():
        if not path.is_dir() or not path.name.isdigit() or len(path.name) != 8:
            continue
        if path.name >= current:
            continue
        if (path / "biotech_daily_scores.csv").exists():
            candidates.append(path)
    return sorted(candidates, key=lambda item: item.name)[-1] if candidates else None


def field_population(rows: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    total = len(rows)
    out: list[dict[str, Any]] = []
    for field in fields:
        present = field in rows[0] if rows else False
        populated = sum(1 for row in rows if nonblank(row.get(field)))
        nonzero = sum(1 for row in rows if (to_float(row.get(field), 0.0) or 0.0) != 0.0)
        out.append(
            {
                "field": field,
                "column_present": 1 if present else 0,
                "rows": total,
                "populated_rows": populated,
                "populated_pct": round(100.0 * populated / total, 2) if total else 0.0,
                "nonzero_numeric_rows": nonzero,
                "nonzero_numeric_pct": round(100.0 * nonzero / total, 2) if total else 0.0,
            }
        )
    return out


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def load_daily_score_source_rows(db_path: Path, asof: str, fields: list[str]) -> list[dict[str, Any]]:
    try:
        with connect(db_path, timeout_sec=10.0) as conn:
            columns = table_columns(conn, "daily_scores")
            if "asof_date" not in columns:
                return []
            select_fields = [field for field in fields if field in columns]
            if not select_fields:
                return []
            ticker_expr = "ticker" if "ticker" in columns else "'' AS ticker"
            selected = ", ".join([ticker_expr, *select_fields])
            rows = conn.execute(
                f"SELECT {selected} FROM daily_scores WHERE asof_date = ?",
                (asof,),
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception:  # noqa: BLE001 - caller falls back to published CSV surface.
        return []


def report_surface_rows(rows: list[dict[str, Any]], fields: list[str], *, report_name: str) -> list[dict[str, Any]]:
    if not rows:
        return [
            {
                "group": f"{report_name}_field_surface",
                "field": field,
                "column_present": 0,
                "rows": 0,
                "populated_rows": 0,
                "populated_pct": 0.0,
                "nonzero_numeric_rows": 0,
                "nonzero_numeric_pct": 0.0,
            }
            for field in fields
        ]
    return [
        {"group": f"{report_name}_field_surface", **row}
        for row in field_population(rows, fields)
    ]


def allocation_leakage_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        rank_purpose = str(row.get("rank_purpose") or "")
        rank_source = str(row.get("rank_source") or "")
        allocation_bucket = str(row.get("allocation_bucket") or row.get("bucket") or "").strip().lower()
        rank_cap_vetoed = (to_float(row.get("rank_quality_cap_vetoed"), 0.0) or 0.0) > 0.0
        investible = (to_float(row.get("biotech_cohort_investible_flag"), 1.0) or 0.0) > 0.0
        reasons: list[str] = []
        if rank_purpose != "allocation":
            reasons.append(f"rank_purpose={rank_purpose or '<blank>'}")
        if rank_source != "allocation_opportunity_score":
            reasons.append(f"rank_source={rank_source or '<blank>'}")
        if allocation_bucket == "avoid":
            reasons.append("allocation_bucket=avoid")
        if rank_cap_vetoed:
            reasons.append("rank_quality_cap_vetoed")
        if not investible:
            reasons.append("non_investible_cohort")
        if reasons:
            violations.append({"ticker": ticker, "rank": row.get("rank", ""), "violation": "|".join(reasons)})
    return violations


def discovery_action_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        rank_purpose = str(row.get("rank_purpose") or "")
        rank_source = str(row.get("rank_source") or "")
        allocation_bucket = str(row.get("allocation_bucket") or row.get("bucket") or "").strip().lower()
        action_tier = str(row.get("discovery_action_tier") or row.get("action_tier") or "")
        reasons: list[str] = []
        if rank_purpose != "discovery":
            reasons.append(f"rank_purpose={rank_purpose or '<blank>'}")
        if rank_source != "discovery_opportunity_score":
            reasons.append(f"rank_source={rank_source or '<blank>'}")
        if allocation_bucket == "avoid" and "research_only_allocation_avoid" not in action_tier:
            reasons.append("avoid_bucket_not_research_only")
        if reasons:
            out.append({"ticker": ticker, "discovery_rank": row.get("discovery_rank", row.get("rank", "")), "violation": "|".join(reasons)})
    return out


def ctgov_shadow_guardrail_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows where CTGov appears to have leaked into primary catalyst scoring."""
    violations: list[dict[str, Any]] = []
    for row in rows:
        ctgov_score = to_float(row.get("ctgov_forward_catalyst_score"), 0.0) or 0.0
        primary_score = to_float(row.get("forward_catalyst_score"), 0.0) or 0.0
        guardrail_pass = (to_float(row.get("ctgov_forward_catalyst_guardrail_pass"), 0.0) or 0.0) > 0.0
        source = str(row.get("forward_catalyst_source") or "").strip().lower()
        if ctgov_score > 0.0 and primary_score > 0.0 and not guardrail_pass and "ctgov" in source:
            violations.append(
                {
                    "ticker": str(row.get("ticker") or "").upper(),
                    "forward_catalyst_source": row.get("forward_catalyst_source", ""),
                    "forward_catalyst_score": primary_score,
                    "ctgov_forward_catalyst_score": ctgov_score,
                    "ctgov_forward_catalyst_guardrail_pass": row.get("ctgov_forward_catalyst_guardrail_pass", ""),
                    "violation": "ctgov_primary_score_without_guardrail_pass",
                }
            )
    return violations


def ranked_by_ticker(rows: list[dict[str, Any]], rank_field: str = "rank") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper().strip()
        if ticker:
            out[ticker] = row
    return out


def compare_scores(
    current_rows: list[dict[str, Any]],
    prior_rows: list[dict[str, Any]],
    *,
    rank_move_threshold: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current_by_ticker = ranked_by_ticker(current_rows)
    prior_by_ticker = ranked_by_ticker(prior_rows)
    comparison: list[dict[str, Any]] = []
    watchlist: list[dict[str, Any]] = []
    for ticker in sorted(set(current_by_ticker) | set(prior_by_ticker)):
        cur = current_by_ticker.get(ticker, {})
        prior = prior_by_ticker.get(ticker, {})
        cur_rank = to_float(cur.get("rank"))
        prior_rank = to_float(prior.get("rank"))
        rank_delta = (cur_rank - prior_rank) if cur_rank is not None and prior_rank is not None else None
        cur_score = to_float(cur.get("allocation_opportunity_score"), to_float(cur.get("opportunity_score")))
        prior_score = to_float(prior.get("allocation_opportunity_score"), to_float(prior.get("opportunity_score")))
        score_delta = (cur_score - prior_score) if cur_score is not None and prior_score is not None else None
        row = {
            "ticker": ticker,
            "current_rank": cur.get("rank", ""),
            "prior_rank": prior.get("rank", ""),
            "rank_delta": "" if rank_delta is None else round(rank_delta, 4),
            "current_allocation_score": "" if cur_score is None else round(cur_score, 4),
            "prior_allocation_score": "" if prior_score is None else round(prior_score, 4),
            "allocation_score_delta": "" if score_delta is None else round(score_delta, 4),
            "current_discovery_score": cur.get("discovery_opportunity_score", ""),
            "prior_discovery_score": prior.get("discovery_opportunity_score", ""),
            "current_cohort": cur.get("biotech_primary_cohort", ""),
            "prior_cohort": prior.get("biotech_primary_cohort", ""),
            "current_bucket": cur.get("allocation_bucket", cur.get("bucket", "")),
            "prior_bucket": prior.get("allocation_bucket", prior.get("bucket", "")),
        }
        comparison.append(row)
        large_move = rank_delta is not None and abs(rank_delta) >= rank_move_threshold
        if cur and (large_move or score_delta is not None and abs(score_delta) >= 5.0):
            shadow_fields: list[str] = []
            shadow_details: dict[str, Any] = {}
            for field in SHADOW_MOVE_FIELDS:
                current_value = to_float(cur.get(field))
                prior_value = to_float(prior.get(field)) if prior else None
                if current_value is None:
                    continue
                delta = current_value - prior_value if prior_value is not None else None
                if current_value >= 60.0 or (delta is not None and abs(delta) >= 10.0):
                    shadow_fields.append(field)
                    shadow_details[f"{field}_current"] = round(current_value, 4)
                    if delta is not None:
                        shadow_details[f"{field}_delta"] = round(delta, 4)
            if shadow_fields:
                watch = {
                    **row,
                    "shadow_move_label": "correlation_only_shadow_fields_not_production_inputs",
                    "shadow_fields": "|".join(shadow_fields),
                }
                watch.update(shadow_details)
                watchlist.append(watch)
    comparison.sort(key=lambda item: (abs(to_float(item.get("rank_delta"), 0.0) or 0.0), str(item.get("ticker"))), reverse=True)
    return comparison, watchlist


def top_entry_exit_rows(
    current_top: list[dict[str, Any]],
    prior_top: list[dict[str, Any]],
    *,
    list_name: str,
) -> list[dict[str, Any]]:
    current = ranked_by_ticker(current_top)
    prior = ranked_by_ticker(prior_top)
    out: list[dict[str, Any]] = []
    for ticker in sorted(set(current) - set(prior)):
        row = current[ticker]
        out.append({"list": list_name, "change": "entrant", "ticker": ticker, "rank": row.get("rank", row.get("discovery_rank", "")), "score": row.get("allocation_opportunity_score", row.get("discovery_opportunity_score", ""))})
    for ticker in sorted(set(prior) - set(current)):
        row = prior[ticker]
        out.append({"list": list_name, "change": "exit", "ticker": ticker, "rank": row.get("rank", row.get("discovery_rank", "")), "score": row.get("allocation_opportunity_score", row.get("discovery_opportunity_score", ""))})
    return out


def load_form4_staging_state(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    form4_path = resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=PACKAGE_ROOT)
    state = {
        "form4_db_path": str(form4_path),
        "form4_db_exists": 1 if form4_path.exists() else 0,
        "form4_db_is_staging": 1 if "STAGING" in str(form4_path).upper() else 0,
        "form4_snapshot_date": "",
        "governance_rows_using_form4_staging": "",
    }
    if not form4_path.exists():
        return state
    try:
        with sqlite3.connect(f"file:{form4_path}?mode=ro", uri=True, timeout=10.0) as form4_conn:
            row = form4_conn.execute("SELECT last_index_date FROM sec_form4_daily_state LIMIT 1").fetchone()
            state["form4_snapshot_date"] = row[0] if row else ""
    except sqlite3.Error as exc:
        state["form4_snapshot_date"] = f"error:{type(exc).__name__}"
    try:
        with connect(db_path, timeout_sec=10.0) as conn:
            count = conn.execute(
                """
                SELECT COUNT(*)
                FROM governance_event_features_daily
                WHERE form4_source_db = ?
                """,
                (str(form4_path),),
            ).fetchone()[0]
            state["governance_rows_using_form4_staging"] = count
    except Exception as exc:  # noqa: BLE001 - QA should report, not hide, DB shape issues.
        state["governance_rows_using_form4_staging"] = f"error:{type(exc).__name__}"
    return state


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    base_dir = args.config.resolve().parent
    base_output_dir = resolve_path(cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    asof = parse_date_token(args.asof)
    report_dir = args.report_dir.resolve() if args.report_dir else base_output_dir / date_dir_name(asof)
    prior_report_dir = args.prior_report_dir.resolve() if args.prior_report_dir else None
    if prior_report_dir is None:
        if args.prior_asof:
            prior_report_dir = base_output_dir / date_dir_name(args.prior_asof)
        else:
            prior_report_dir = latest_prior_report_dir(base_output_dir, asof)
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else base_output_dir / f"report_quality_{date_dir_name(asof)}"
    )
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)

    allocation_rows = read_csv(report_dir / "biotech_top_candidates.csv")
    discovery_rows = read_csv(report_dir / "biotech_discovery_top20_candidates.csv")
    daily_rows = read_csv(report_dir / "biotech_daily_scores.csv")
    prior_daily_rows = read_csv(prior_report_dir / "biotech_daily_scores.csv") if prior_report_dir else []
    prior_allocation_rows = read_csv(prior_report_dir / "biotech_top_candidates.csv") if prior_report_dir and (prior_report_dir / "biotech_top_candidates.csv").exists() else []
    prior_discovery_rows = read_csv(prior_report_dir / "biotech_discovery_top20_candidates.csv") if prior_report_dir and (prior_report_dir / "biotech_discovery_top20_candidates.csv").exists() else []

    allocation_violations = allocation_leakage_audit(allocation_rows)
    discovery_violations = discovery_action_audit(discovery_rows)
    source_fields = sorted(set(FORM4_FIELDS + BORROW_SHORT_13F_FIELDS + CTGOV_FIELDS))
    db_source_rows = load_daily_score_source_rows(db_path, asof, source_fields)
    source_population_rows = db_source_rows or allocation_rows or daily_rows
    data_population = [
        {"group": "form4_daily_scores_db", **row} for row in field_population(source_population_rows, FORM4_FIELDS)
    ] + [
        {"group": "borrow_short_13f_daily_scores_db", **row} for row in field_population(source_population_rows, BORROW_SHORT_13F_FIELDS)
    ] + [
        {"group": "ctgov_forward_catalyst_shadow_daily_scores_db", **row}
        for row in field_population(source_population_rows, CTGOV_FIELDS)
    ] + report_surface_rows(allocation_rows, source_fields, report_name="allocation_top_candidates")
    form4_state = load_form4_staging_state(db_path, config)
    ctgov_guardrail_violations = ctgov_shadow_guardrail_audit(source_population_rows)
    data_population.append(
        {
            "group": "form4_staging_boundary",
            "field": "form4_db_state",
            "column_present": 1,
            "rows": len(daily_rows),
            "populated_rows": form4_state.get("governance_rows_using_form4_staging", ""),
            "populated_pct": "",
            "nonzero_numeric_rows": form4_state.get("form4_snapshot_date", ""),
            "nonzero_numeric_pct": json.dumps(form4_state, ensure_ascii=True, sort_keys=True),
        }
    )

    comparison_rows, watchlist_rows = compare_scores(
        daily_rows,
        prior_daily_rows,
        rank_move_threshold=max(1, int(args.rank_move_threshold)),
    )
    top_n = max(1, int(args.top_n))
    entry_exit_rows = []
    if prior_allocation_rows:
        entry_exit_rows.extend(
            top_entry_exit_rows(
                allocation_rows[:top_n],
                prior_allocation_rows[:top_n],
                list_name=f"allocation_top{top_n}",
            )
        )
    if prior_discovery_rows:
        entry_exit_rows.extend(
            top_entry_exit_rows(
                discovery_rows[:top_n],
                prior_discovery_rows[:top_n],
                list_name=f"discovery_top{top_n}",
            )
        )

    discovery_gap_rows: list[dict[str, Any]] = []
    for row in discovery_rows:
        gap = to_float(row.get("rank_gap_allocation_vs_discovery"))
        discovery_gap_rows.append(
            {
                "ticker": row.get("ticker", ""),
                "discovery_rank": row.get("discovery_rank", row.get("rank", "")),
                "allocation_rank": row.get("allocation_rank", ""),
                "rank_gap_allocation_vs_discovery": "" if gap is None else round(gap, 4),
                "allocation_bucket": row.get("allocation_bucket", row.get("bucket", "")),
                "discovery_action_tier": row.get("discovery_action_tier", ""),
                "discovery_opportunity_score": row.get("discovery_opportunity_score", ""),
                "allocation_opportunity_score": row.get("allocation_opportunity_score", ""),
            }
        )

    summary_rows = [
        status_row("asof", "INFO", asof, f"report_dir={report_dir}"),
        status_row("prior_asof_dir", "INFO", prior_report_dir.name if prior_report_dir else "", str(prior_report_dir or "")),
        status_row("allocation_file_rank_purpose", "PASS" if not allocation_violations else "FAIL", len(allocation_violations), "Allocation list must be allocation-only and investible."),
        status_row("discovery_file_rank_purpose", "PASS" if not discovery_violations else "FAIL", len(discovery_violations), "Discovery list must be discovery-only; avoid bucket names must be research-only."),
        status_row("form4_staging_db_exists", "PASS" if form4_state.get("form4_db_exists") else "FAIL", form4_state.get("form4_db_exists"), str(form4_state.get("form4_db_path"))),
        status_row("form4_staging_boundary", "PASS" if form4_state.get("form4_db_is_staging") else "FAIL", form4_state.get("form4_db_is_staging"), str(form4_state.get("form4_db_path"))),
        status_row("form4_snapshot_date", "PASS" if str(form4_state.get("form4_snapshot_date")) >= asof else "WARN", form4_state.get("form4_snapshot_date"), "Snapshot should be current for same-day pipeline consistency."),
        status_row("ctgov_shadow_guardrail", "PASS" if not ctgov_guardrail_violations else "FAIL", len(ctgov_guardrail_violations), "CTGov should remain shadow-only unless its guardrail passes."),
    ]
    for group in [
        "form4_daily_scores_db",
        "borrow_short_13f_daily_scores_db",
        "ctgov_forward_catalyst_shadow_daily_scores_db",
    ]:
        rows = [row for row in data_population if row.get("group") == group]
        populated = sum(1 for row in rows if (to_float(row.get("populated_rows"), 0.0) or 0.0) > 0.0)
        summary_rows.append(
            status_row(
                f"{group}_field_population",
                "PASS" if populated == len(rows) else "WARN",
                f"{populated}/{len(rows)}",
                "Every expected field should have at least one populated row; zero may be valid for sparse shadow signals but needs review.",
            )
        )
    report_surface_fields = [row for row in data_population if row.get("group") == "allocation_top_candidates_field_surface"]
    report_surface_present = sum(1 for row in report_surface_fields if (to_float(row.get("column_present"), 0.0) or 0.0) > 0.0)
    summary_rows.append(
        status_row(
            "allocation_report_shadow_field_surface",
            "PASS" if report_surface_present == len(report_surface_fields) else "WARN",
            f"{report_surface_present}/{len(report_surface_fields)}",
            "Top allocation report should expose the shadow factor fields for auditability.",
        )
    )

    write_csv(output_dir / "report_quality_summary.csv", summary_rows)
    write_csv(output_dir / "allocation_leakage_audit.csv", allocation_violations, ["ticker", "rank", "violation"])
    write_csv(output_dir / "discovery_action_audit.csv", discovery_violations, ["ticker", "discovery_rank", "violation"])
    write_csv(
        output_dir / "ctgov_shadow_guardrail_audit.csv",
        ctgov_guardrail_violations,
        [
            "ticker",
            "forward_catalyst_source",
            "forward_catalyst_score",
            "ctgov_forward_catalyst_score",
            "ctgov_forward_catalyst_guardrail_pass",
            "violation",
        ],
    )
    write_csv(output_dir / "data_source_population_qa.csv", data_population)
    write_csv(output_dir / "score_change_comparison.csv", comparison_rows)
    write_csv(output_dir / "top_entrant_exit_comparison.csv", entry_exit_rows)
    write_csv(output_dir / "allocation_vs_discovery_rank_gap.csv", discovery_gap_rows)
    write_csv(output_dir / "shadow_signal_rank_move_watchlist.csv", watchlist_rows)

    failures = [row for row in summary_rows if row["status"] == "FAIL"]
    print(f"Wrote report QA outputs to {output_dir}")
    if failures:
        raise SystemExit(f"Report QA failed: {', '.join(str(row['check']) for row in failures)}")


if __name__ == "__main__":
    main()
