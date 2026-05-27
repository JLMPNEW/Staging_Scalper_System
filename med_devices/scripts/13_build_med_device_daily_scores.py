#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_med_device_daily_scores")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_WEIGHTS = {
    "fundamental_quality": 0.25,
    "durable_growth": 0.15,
    "fda_product": 0.15,
    "reimbursement": 0.10,
    "valuation": 0.20,
    "technical_entry": 0.10,
    "sentiment_catalyst": 0.05,
}
FIELDNAMES = [
    "asof_date",
    "rank",
    "company_id",
    "ticker",
    "company_name",
    "subsector",
    "composite_score",
    "fundamental_quality_score",
    "durable_growth_score",
    "fda_product_score",
    "fda_data_available",
    "reimbursement_score",
    "valuation_score",
    "technical_entry_score",
    "sentiment_catalyst_score",
    "value_trap_score",
    "data_completeness_score",
    "live_component_count",
    "composite_score_delta",
    "rank_delta",
    "classification_change",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "classification",
    "gate_status",
    "review_reason",
]


@dataclass
class ScoreRow:
    asof_date: str
    rank: int
    company_id: int
    ticker: str
    company_name: str
    subsector: str
    composite_score: float = 0.0
    fundamental_quality_score: float = 0.0
    durable_growth_score: float = 50.0
    fda_product_score: float = 50.0
    fda_data_available: int = 0
    reimbursement_score: float = 50.0
    valuation_score: float = 0.0
    technical_entry_score: float = 50.0
    sentiment_catalyst_score: float = 50.0
    value_trap_score: float = 0.0
    data_completeness_score: float = 0.0
    live_component_count: int = 0
    composite_score_delta: float | None = None
    rank_delta: int | None = None
    classification_change: str = ""
    hard_red_flag: int = 0
    hard_red_flag_reasons: str = ""
    classification: str = "unclassified"
    gate_status: str = "fail"
    review_reason: str = ""
    top_positive_drivers: list[str] = field(default_factory=list)
    top_negative_drivers: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device daily composite scores.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def cfg_float(config: dict[str, Any], dotted_key: str, default: float) -> float:
    value = to_float(cfg_get(config, dotted_key, default))
    if value is None:
        raise ValueError(f"Config value must be numeric: {dotted_key}")
    return value


def component_neutral(config: dict[str, Any], component: str, legacy_key: str, default: float) -> float:
    nested_key = f"scoring.component_neutral_defaults.{component}"
    raw = cfg_get(config, nested_key, None)
    if raw is not None:
        value = to_float(raw)
        if value is None:
            raise ValueError(f"Config value must be numeric: {nested_key}")
        return value
    return cfg_float(config, legacy_key, default)


def score_or(raw: object, default: float) -> float:
    value = to_float(raw)
    return default if value is None else value


def latest_financial_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM feature_financial_valuation").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    if not asof:
        raise ValueError("No feature_financial_valuation rows found; run script 06 first.")
    return asof


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def load_weights(config: dict[str, Any]) -> dict[str, float]:
    raw = cfg_get(config, "scoring.composite_weights", DEFAULT_WEIGHTS)
    if not isinstance(raw, dict):
        return dict(DEFAULT_WEIGHTS)
    unknown = sorted(set(str(key) for key in raw) - set(DEFAULT_WEIGHTS))
    if unknown:
        LOGGER.warning("Ignoring unknown composite scoring weight key(s): %s", ", ".join(unknown))
    out = dict(DEFAULT_WEIGHTS)
    for key, raw_value in raw.items():
        if str(key) not in DEFAULT_WEIGHTS:
            continue
        value = to_float(raw_value)
        if value is None or value < 0:
            raise ValueError(f"Composite score weight must be non-negative numeric: {key}")
        out[str(key)] = value
    total = sum(out.values())
    if abs(total - 1.0) > 0.0001:
        raise ValueError(f"Composite score weights must sum to 1.0: {total:.6f}")
    return out


def load_financial_rows(conn: Any, *, asof: str, ticker_filter: set[str], max_tickers: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM feature_financial_valuation
        WHERE asof_date = ?
        ORDER BY ticker
        """,
        (asof,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        ticker = normalize_ticker(item.get("ticker"))
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(item)
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def load_latest_feature(conn: Any, table: str, score_col: str, *, asof: str) -> dict[int, dict[str, Any]]:
    if not table_exists(conn, table):
        return {}
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if not {"company_id", "asof_date", score_col}.issubset(columns):
        return {}
    rows = conn.execute(
        f"""
        SELECT t.*
        FROM {table} t
        JOIN (
            SELECT company_id, MAX(asof_date) AS asof_date
            FROM {table}
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest ON latest.company_id = t.company_id AND latest.asof_date = t.asof_date
        """,
        (asof,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def percentile(values: list[tuple[int, float]], *, higher_is_better: bool) -> dict[int, float]:
    if not values:
        return {}
    values.sort(key=lambda item: item[1])
    if len(values) == 1:
        return {values[0][0]: 50.0}
    out: dict[int, float] = {}
    denominator = len(values) - 1
    for rank, (company_id, _) in enumerate(values):
        pct = 100.0 * rank / denominator
        out[company_id] = pct if higher_is_better else 100.0 - pct
    return out


def durable_growth_proxy(financial_rows: list[dict[str, Any]]) -> dict[int, float]:
    growth_pairs: list[tuple[int, float]] = []
    cagr_pairs: list[tuple[int, float]] = []
    stability_pairs: list[tuple[int, float]] = []
    rule_pairs: list[tuple[int, float]] = []
    margin_trend_pairs: list[tuple[int, float]] = []
    for row in financial_rows:
        company_id = int(row["company_id"])
        growth = to_float(row.get("revenue_yoy_growth"))
        cagr = to_float(row.get("revenue_cagr_3y"))
        stability = to_float(row.get("revenue_growth_stability_5y"))
        rule = to_float(row.get("rule_of_40"))
        margin_trend = to_float(row.get("operating_margin_trend_3y")) or to_float(row.get("gross_margin_trend_3y"))
        if growth is not None:
            growth_pairs.append((company_id, growth))
        if cagr is not None:
            cagr_pairs.append((company_id, cagr))
        if stability is not None:
            stability_pairs.append((company_id, stability))
        if rule is not None:
            rule_pairs.append((company_id, rule))
        if margin_trend is not None:
            margin_trend_pairs.append((company_id, margin_trend))
    growth_scores = percentile(growth_pairs, higher_is_better=True)
    cagr_scores = percentile(cagr_pairs, higher_is_better=True)
    stability_scores = percentile(stability_pairs, higher_is_better=False)
    rule_scores = percentile(rule_pairs, higher_is_better=True)
    margin_scores = percentile(margin_trend_pairs, higher_is_better=True)
    out: dict[int, float] = {}
    for row in financial_rows:
        company_id = int(row["company_id"])
        out[company_id] = round(
            0.35 * growth_scores.get(company_id, 50.0)
            + 0.30 * cagr_scores.get(company_id, 50.0)
            + 0.15 * stability_scores.get(company_id, 50.0)
            + 0.10 * rule_scores.get(company_id, 50.0)
            + 0.10 * margin_scores.get(company_id, 50.0),
            2,
        )
    return out


def score_drivers(row: ScoreRow) -> tuple[list[str], list[str]]:
    items = [
        ("fundamental", row.fundamental_quality_score),
        ("durable_growth", row.durable_growth_score),
        ("fda_product", row.fda_product_score),
        ("reimbursement", row.reimbursement_score),
        ("valuation", row.valuation_score),
        ("technical_entry", row.technical_entry_score),
        ("sentiment_catalyst", row.sentiment_catalyst_score),
    ]
    positives = [f"{name}:{score:.1f}" for name, score in sorted(items, key=lambda item: item[1], reverse=True)[:3]]
    below_neutral = [(name, score) for name, score in items if score < 50.0]
    negatives = [f"{name}:{score:.1f}" for name, score in sorted(below_neutral, key=lambda item: item[1])[:3]]
    return positives, negatives


def load_previous_scores(conn: Any, *, asof: str) -> dict[int, dict[str, Any]]:
    previous = conn.execute(
        """
        SELECT MAX(asof_date) AS asof_date
        FROM med_device_daily_scores
        WHERE asof_date < ?
        """,
        (asof,),
    ).fetchone()
    previous_asof = str(previous["asof_date"] or "") if previous is not None else ""
    if not previous_asof:
        return {}
    rows = conn.execute(
        """
        SELECT company_id, composite_score, rank, classification
        FROM med_device_daily_scores
        WHERE asof_date = ?
        """,
        (previous_asof,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def classify(row: ScoreRow, *, gates: dict[str, float]) -> None:
    reasons: list[str] = []
    if row.composite_score < gates["composite_min"]:
        reasons.append("composite_below_gate")
    if row.fundamental_quality_score < gates["fundamental_quality_min"]:
        reasons.append("fundamental_below_gate")
    if row.fda_data_available and row.fda_product_score < gates["fda_product_min"]:
        reasons.append("fda_below_gate")
    if row.reimbursement_score < gates["reimbursement_min"]:
        reasons.append("reimbursement_below_gate")
    if row.valuation_score < gates["valuation_min"]:
        reasons.append("valuation_below_gate")
    if row.technical_entry_score < gates["technical_entry_min"]:
        reasons.append("technical_below_gate")
    if row.hard_red_flag:
        reasons.append("hard_red_flag")
    if row.value_trap_score >= gates["value_trap_max"]:
        reasons.append("value_trap")

    row.review_reason = ";".join(reasons)
    row.gate_status = "pass" if not reasons else "fail"
    if row.gate_status == "pass":
        row.classification = "tier_1_long_candidate"
    elif row.hard_red_flag or (row.fda_data_available and row.fda_product_score < gates["fda_product_min"]):
        row.classification = "manual_review_regulatory_risk"
    elif row.fundamental_quality_score >= 75 and row.valuation_score < gates["valuation_min"]:
        if row.valuation_score >= gates["valuation_min"] - 5.0:
            row.classification = "quality_watchlist_near_price_gate"
        elif row.valuation_score < 30.0:
            row.classification = "quality_watchlist_significantly_overvalued"
        else:
            row.classification = "quality_watchlist_wait_for_price"
    elif row.valuation_score >= 70 and row.fundamental_quality_score < gates["fundamental_quality_min"]:
        row.classification = "cheap_but_needs_proof"
    elif row.composite_score >= 65:
        row.classification = "watchlist"
    else:
        row.classification = "avoid"


def build_rows(
    conn: Any,
    *,
    asof: str,
    weights: dict[str, float],
    config: dict[str, Any],
    ticker_filter: set[str],
    max_tickers: int,
) -> list[ScoreRow]:
    financial_rows = load_financial_rows(conn, asof=asof, ticker_filter=ticker_filter, max_tickers=max_tickers)
    fda_rows = load_latest_feature(conn, "feature_fda_product_risk", "fda_product_score", asof=asof)
    reimbursement_rows = load_latest_feature(conn, "feature_reimbursement", "score", asof=asof)
    technical_rows = load_latest_feature(conn, "feature_technical_entry", "score", asof=asof)
    durable_rows = load_latest_feature(conn, "feature_durable_growth", "score", asof=asof)
    sentiment_rows = load_latest_feature(conn, "feature_sentiment_catalyst", "score", asof=asof)
    durable_proxy = durable_growth_proxy(financial_rows)
    neutral_fundamental = component_neutral(config, "fundamental_quality", "scoring.neutral_fundamental_quality_score", 50.0)
    neutral_durable = component_neutral(config, "durable_growth", "scoring.neutral_durable_growth_score", 50.0)
    neutral_reimbursement = component_neutral(config, "reimbursement", "scoring.neutral_reimbursement_score", 50.0)
    neutral_fda_no_data = component_neutral(config, "fda_product", "scoring.neutral_fda_no_data_score", 55.0)
    neutral_valuation = component_neutral(config, "valuation", "scoring.neutral_valuation_score", 50.0)
    neutral_technical = component_neutral(config, "technical_entry", "scoring.neutral_technical_entry_score", 50.0)
    neutral_sentiment = component_neutral(config, "sentiment_catalyst", "scoring.neutral_sentiment_catalyst_score", 50.0)
    gates = {
        "composite_min": cfg_float(config, "scoring.gates.composite_min", 75.0),
        "fundamental_quality_min": cfg_float(config, "scoring.gates.fundamental_quality_min", 65.0),
        "fda_product_min": cfg_float(config, "scoring.gates.fda_product_min", 55.0),
        "reimbursement_min": cfg_float(config, "scoring.gates.reimbursement_min", 50.0),
        "valuation_min": cfg_float(config, "scoring.gates.valuation_min", 60.0),
        "technical_entry_min": cfg_float(config, "scoring.gates.technical_entry_min", 55.0),
        "value_trap_max": cfg_float(config, "scoring.gates.value_trap_max", 40.0),
    }
    rows: list[ScoreRow] = []
    for item in financial_rows:
        company_id = int(item["company_id"])
        fda_item = fda_rows.get(company_id, {})
        reimbursement_item = reimbursement_rows.get(company_id, {})
        technical_item = technical_rows.get(company_id, {})
        durable_item = durable_rows.get(company_id, {})
        sentiment_item = sentiment_rows.get(company_id, {})
        fda_hard_flag = int(fda_item.get("hard_red_flag") or 0) if fda_item else 0
        fda_data_available = int(fda_item.get("fda_data_available") or 0) if fda_item else 0
        reimbursement_hard_flag = int(reimbursement_item.get("hard_red_flag") or 0) if reimbursement_item else 0
        fda_score = score_or(fda_item.get("fda_product_score"), neutral_fda_no_data) if fda_item else neutral_fda_no_data
        if fda_item and not fda_data_available:
            fda_score = neutral_fda_no_data
        durable_score = (
            score_or(durable_item.get("score"), durable_proxy.get(company_id, neutral_durable))
            if durable_item
            else durable_proxy.get(company_id, neutral_durable)
        )
        live_components = [
            to_float(item.get("fundamental_quality_score_v1")) is not None,
            bool(durable_item) or any(to_float(item.get(key)) is not None for key in ("revenue_yoy_growth", "revenue_cagr_3y", "rule_of_40")),
            bool(fda_item) and bool(fda_data_available),
            bool(reimbursement_item),
            to_float(item.get("valuation_score_v1")) is not None,
            bool(technical_item),
            bool(sentiment_item),
        ]
        row = ScoreRow(
            asof_date=asof,
            rank=0,
            company_id=company_id,
            ticker=normalize_ticker(item.get("ticker")),
            company_name=str(item.get("company_name") or ""),
            subsector=str(item.get("subsector") or ""),
            fundamental_quality_score=score_or(item.get("fundamental_quality_score_v1"), neutral_fundamental),
            durable_growth_score=durable_score,
            fda_product_score=fda_score,
            fda_data_available=fda_data_available,
            reimbursement_score=score_or(reimbursement_item.get("score"), neutral_reimbursement) if reimbursement_item else neutral_reimbursement,
            valuation_score=score_or(item.get("valuation_score_v1"), neutral_valuation),
            technical_entry_score=score_or(technical_item.get("score"), neutral_technical) if technical_item else neutral_technical,
            sentiment_catalyst_score=score_or(sentiment_item.get("score"), neutral_sentiment) if sentiment_item else neutral_sentiment,
            value_trap_score=to_float(item.get("value_trap_score")) or 0.0,
            live_component_count=sum(1 for value in live_components if value),
            data_completeness_score=round(100.0 * sum(1 for value in live_components if value) / len(live_components), 2),
            hard_red_flag=1 if fda_hard_flag or reimbursement_hard_flag else 0,
            hard_red_flag_reasons=";".join(
                reason
                for reason in [
                    str(fda_item.get("hard_red_flag_reasons") or ""),
                    str(reimbursement_item.get("hard_red_flag_reasons") or ""),
                ]
                if reason
            ),
        )
        row.fda_product_score = row.fda_product_score if row.fda_product_score is not None else 50.0
        row.durable_growth_score = row.durable_growth_score if row.durable_growth_score is not None else 50.0
        row.reimbursement_score = row.reimbursement_score if row.reimbursement_score is not None else neutral_reimbursement
        row.technical_entry_score = row.technical_entry_score if row.technical_entry_score is not None else neutral_technical
        row.sentiment_catalyst_score = row.sentiment_catalyst_score if row.sentiment_catalyst_score is not None else neutral_sentiment
        row.composite_score = round(
            clamp(
                weights["fundamental_quality"] * row.fundamental_quality_score
                + weights["durable_growth"] * row.durable_growth_score
                + weights["fda_product"] * row.fda_product_score
                + weights["reimbursement"] * row.reimbursement_score
                + weights["valuation"] * row.valuation_score
                + weights["technical_entry"] * row.technical_entry_score
                + weights["sentiment_catalyst"] * row.sentiment_catalyst_score
            ),
            2,
        )
        classify(row, gates=gates)
        row.top_positive_drivers, row.top_negative_drivers = score_drivers(row)
        rows.append(row)
    rows.sort(key=lambda item: item.composite_score, reverse=True)
    for rank, row in enumerate(rows, start=1):
        row.rank = rank
    previous_scores = load_previous_scores(conn, asof=asof)
    for row in rows:
        previous = previous_scores.get(row.company_id)
        if not previous:
            continue
        previous_score = to_float(previous.get("composite_score"))
        previous_rank = int(previous["rank"]) if previous.get("rank") is not None else None
        previous_classification = str(previous.get("classification") or "")
        row.composite_score_delta = round(row.composite_score - previous_score, 2) if previous_score is not None else None
        row.rank_delta = previous_rank - row.rank if previous_rank is not None else None
        if previous_classification and previous_classification != row.classification:
            row.classification_change = f"{previous_classification}->{row.classification}"
    return rows


def upsert_rows(conn: Any, rows: list[ScoreRow]) -> int:
    if not rows:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO med_device_daily_scores(
            asof_date, company_id, composite_score, fundamental_quality_score,
            durable_growth_score, fda_product_score, reimbursement_score, valuation_score,
            technical_entry_score, sentiment_catalyst_score, value_trap_score, rank,
            data_completeness_score, live_component_count, composite_score_delta, rank_delta,
            classification_change, classification, gate_status, review_reason, hard_red_flag, hard_red_flag_reasons,
            top_positive_drivers_json, top_negative_drivers_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            composite_score = excluded.composite_score,
            fundamental_quality_score = excluded.fundamental_quality_score,
            durable_growth_score = excluded.durable_growth_score,
            fda_product_score = excluded.fda_product_score,
            reimbursement_score = excluded.reimbursement_score,
            valuation_score = excluded.valuation_score,
            technical_entry_score = excluded.technical_entry_score,
            sentiment_catalyst_score = excluded.sentiment_catalyst_score,
            value_trap_score = excluded.value_trap_score,
            rank = excluded.rank,
            data_completeness_score = excluded.data_completeness_score,
            live_component_count = excluded.live_component_count,
            composite_score_delta = excluded.composite_score_delta,
            rank_delta = excluded.rank_delta,
            classification_change = excluded.classification_change,
            classification = excluded.classification,
            gate_status = excluded.gate_status,
            review_reason = excluded.review_reason,
            hard_red_flag = excluded.hard_red_flag,
            hard_red_flag_reasons = excluded.hard_red_flag_reasons,
            top_positive_drivers_json = excluded.top_positive_drivers_json,
            top_negative_drivers_json = excluded.top_negative_drivers_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row.asof_date,
                row.company_id,
                row.composite_score,
                row.fundamental_quality_score,
                row.durable_growth_score,
                row.fda_product_score,
                row.reimbursement_score,
                row.valuation_score,
                row.technical_entry_score,
                row.sentiment_catalyst_score,
                row.value_trap_score,
                row.rank,
                row.data_completeness_score,
                row.live_component_count,
                row.composite_score_delta,
                row.rank_delta,
                row.classification_change,
                row.classification,
                row.gate_status,
                row.review_reason,
                row.hard_red_flag,
                row.hard_red_flag_reasons,
                json.dumps(row.top_positive_drivers, ensure_ascii=True),
                json.dumps(row.top_negative_drivers, ensure_ascii=True),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def row_to_dict(row: ScoreRow) -> dict[str, Any]:
    return {field: getattr(row, field) for field in FIELDNAMES if hasattr(row, field)}


def write_csv(path: Path, rows: list[ScoreRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_to_dict(row) for row in rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "scoring.output_csv", "../output/med_devices_reports/med_device_daily_composite_scores.csv"),
            base_dir=base_dir,
        )
    )
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    weights = load_weights(config)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_med_device_daily_scores", input_path=config_path)
        try:
            asof = args.asof.strip() or latest_financial_asof(conn)
            rows = build_rows(
                conn,
                asof=asof,
                weights=weights,
                config=config,
                ticker_filter=ticker_filter,
                max_tickers=int(args.max_tickers),
            )
            upserted = upsert_rows(conn, rows)
            write_csv(output_csv, rows)
            message = f"asof={asof} rows={upserted} output={output_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=upserted, message=message)
            LOGGER.info("Daily composite scores complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
