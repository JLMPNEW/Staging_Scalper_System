#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("generate_historical_biotech_score_csvs")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SCORING_SCRIPT = PACKAGE_ROOT / "scripts" / "11_score_biotech_index.py"

ALLOWED_CALIBRATION_COHORTS = frozenset(
    {
        "commercial_profitable_quality_or_mature",
        "commercial_turnaround_or_unprofitable_growth",
        "late_clinical_pivotal_or_registrational",
        "platform_partnered_modality_pipeline",
        "early_clinical_speculative_or_single_asset_pipeline",
    }
)

REQUIRED_PRESENT_COLUMNS = [
    "asof_date",
    "rank",
    "company_id",
    "ticker",
    "company_name",
    "sector",
    "industry",
    "subsector",
    "country",
    "currency",
    "score_model_version",
    "model_family",
    "model_version",
    "scoring_contract_version",
    "portfolio_candidate_gate",
    "portfolio_candidate_score",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
    "calibration_eligible_flag",
    "score_confidence",
    "avg_dollar_volume_60d",
    "review_reason",
    "eligibility_reason",
    "universe_status",
    "historical_universe_source",
    "price_start_date",
    "price_end_date",
    "terminal_date",
    "historical_price_ticker",
    "calibration_only",
    "recovery_type",
    "equity_recovery",
    "drop_otc_tape",
    "latest_price_date",
    "source_snapshot_asof_date",
    "price_data_asof_date",
    "feature_data_asof_date",
    "clinical_data_asof_date",
    "financial_data_asof_date",
    "short_interest_asof_date",
    "institutional_data_asof_date",
    "insider_data_asof_date",
    "borrow_data_asof_date",
    "calibration_cohort",
    "calibration_status",
    "calibration_status_reason",
    "native_score_field",
    "native_score_value",
    "score_scale_min",
    "score_scale_max",
    "score_neutral_value",
    "score_zero_is_missing_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_status",
    "research_calibration_reason",
    "calibration_sample_role",
    "oos_score_valid_flag",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "capacity_bucket",
    "min_position_size_feasible",
    "max_position_size_feasible",
    "liquidity_score",
    "forward_catalyst_event_date",
    "forward_catalyst_asof_date",
    "bucket",
    "opportunity_score",
    "allocation_opportunity_score",
    "allocation_bucket",
    "production_rank_score",
    "production_rank_risk_score",
    "production_rank_score_field",
    "production_score_source",
    "discovery_opportunity_score",
    "investment_score",
    "discovery_investment_score",
    "biotech_primary_cohort",
    "biotech_cohort_investible_flag",
    "biotech_cohort_calibration_eligible_flag",
    "clinical_opportunity_score",
    "tier1_selection_gate_score",
    "discovery_selection_gate_score",
    "data_quality_confidence_multiplier",
    "effective_total_risk_drag",
    "catalyst_score",
    "credibility_score",
    "financial_quality_score",
    "risk_score",
    "momentum_score",
]

REQUIRED_NONBLANK_COLUMNS = [
    "asof_date",
    "rank",
    "company_id",
    "ticker",
    "company_name",
    "sector",
    "industry",
    "subsector",
    "country",
    "currency",
    "score_model_version",
    "model_family",
    "model_version",
    "scoring_contract_version",
    "portfolio_candidate_gate",
    "portfolio_candidate_score",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
    "calibration_eligible_flag",
    "score_confidence",
    "universe_status",
    "historical_universe_source",
    "historical_price_ticker",
    "calibration_only",
    "source_snapshot_asof_date",
    "feature_data_asof_date",
    "calibration_cohort",
    "calibration_status",
    "native_score_field",
    "native_score_value",
    "score_scale_min",
    "score_scale_max",
    "score_neutral_value",
    "score_zero_is_missing_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_status",
    "research_calibration_reason",
    "calibration_sample_role",
    "oos_score_valid_flag",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "bucket",
    "opportunity_score",
    "allocation_opportunity_score",
    "allocation_bucket",
    "production_rank_score_field",
    "production_score_source",
    "discovery_opportunity_score",
    "biotech_primary_cohort",
    "biotech_cohort_investible_flag",
    "tier1_selection_gate_score",
    "data_quality_confidence_multiplier",
]

STAGE11_SIDECAR_COLUMNS = [
    "ticker",
    "company_name",
    "asof_date",
    "native_score_value",
    "opportunity_score",
    "portfolio_candidate_gate",
    "portfolio_candidate_reason",
    "calibration_eligible_flag",
    "price_data_asof_date",
    "score_zero_is_missing_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_status",
    "research_calibration_reason",
    "calibration_sample_role",
    "oos_score_valid_flag",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "score_model_version",
    "model_family",
    "model_version",
    "scoring_contract_version",
    "sector",
    "industry",
    "subsector",
    "country",
    "currency",
    "avg_dollar_volume_60d",
    "universe_status",
    "historical_universe_source",
    "price_start_date",
    "price_end_date",
    "terminal_date",
    "historical_price_ticker",
    "calibration_only",
    "recovery_type",
    "equity_recovery",
    "drop_otc_tape",
]

SOURCE_DATE_COLUMNS_NOT_AFTER_ASOF = [
    "float_shares_asof_date",
    "float_shares_source_asof_date",
    "public_float_price_date",
    "source_snapshot_asof_date",
    "price_data_asof_date",
    "feature_data_asof_date",
    "clinical_data_asof_date",
    "financial_data_asof_date",
    "short_interest_asof_date",
    "institutional_data_asof_date",
    "insider_data_asof_date",
    "borrow_data_asof_date",
    "forward_catalyst_asof_date",
    "latest_price_date",
]
COMPANY_SUFFIX_RE = re.compile(
    r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|AG|SA|SE|NV|LP|LLC|DE)\b",
    re.IGNORECASE,
)
NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate and validate historical portfolio-layer biotech_daily_scores.csv files "
            "from daily_scores without contacting external data sources."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--dates", type=str, default="", help="Optional comma-separated YYYY-MM-DD/YYYMMDD dates.")
    parser.add_argument("--source-table", choices=["daily_scores", "daily_features", "market_bars_daily"], default="daily_scores")
    parser.add_argument(
        "--carry-forward-scores",
        action="store_true",
        help=(
            "When a selected date has no exact daily_scores rows, export the latest prior score snapshot "
            "with the selected date as asof_date and the original snapshot preserved in provenance fields."
        ),
    )
    parser.add_argument("--fridays-only", action="store_true")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--manifest-json", type=Path, default=None)
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--min-rows", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--allow-invalid", action="store_true", help="Write the summary/manifest and exit 0 even when validation fails.")
    panel_group = parser.add_mutually_exclusive_group()
    panel_group.add_argument(
        "--survivorship-corrected-panel",
        dest="survivorship_corrected_panel",
        action="store_true",
        default=None,
        help=(
            "Generate Stage 11 flags as a PIT/survivorship-corrected calibration panel. "
            "This uses the delisted calibration universe metadata and requires valid "
            "score, price, PIT provenance, and historical membership before setting "
            "stage11_calibration_input_eligible_flag=1."
        ),
    )
    panel_group.add_argument(
        "--current-universe-replay",
        dest="survivorship_corrected_panel",
        action="store_false",
        default=None,
        help="Generate a current-universe replay and explicitly mark Stage 11 rows not survivorship-corrected.",
    )
    parser.add_argument(
        "--stage11-sidecar-name",
        type=str,
        default="biotech_stage11_calibration_inputs.csv",
        help="Per-date Stage 11 sidecar CSV name written next to biotech_daily_scores.csv.",
    )
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def compact_date(raw: object) -> str:
    parsed = parse_date(raw)
    if parsed is None:
        raise ValueError(f"Invalid as-of date: {raw!r}")
    return parsed.strftime("%Y%m%d")


def iso_date(raw: object) -> str:
    parsed = parse_date(raw)
    if parsed is None:
        raise ValueError(f"Invalid as-of date: {raw!r}")
    return parsed.isoformat()


def is_blank(raw: object) -> bool:
    return raw is None or str(raw).strip() == ""


def value_or_blank(raw: object) -> object:
    return "" if is_blank(raw) else raw


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def to_float(raw: object, default: float | None = None) -> float | None:
    try:
        value = float(str(raw).strip()) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if value != value:
        return low
    return max(low, min(high, value))


def linear_score(value: float, points: list[tuple[float, float]]) -> float:
    ordered = sorted(points)
    if value <= ordered[0][0]:
        return clamp(ordered[0][1])
    if value >= ordered[-1][0]:
        return clamp(ordered[-1][1])
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            span = max(1e-12, right_x - left_x)
            return clamp(left_y + (right_y - left_y) * (value - left_x) / span)
    return clamp(ordered[-1][1])


def source_date_after_asof(raw: object, asof: object) -> bool:
    parsed = parse_date(raw)
    asof_date = parse_date(asof)
    return parsed is not None and asof_date is not None and parsed > asof_date


def sanitize_short_interest_fields_for_pit(row: dict[str, Any], *, asof: object) -> None:
    """Drop short-interest percent-float components with future float-share provenance."""
    if not (
        source_date_after_asof(row.get("float_shares_source_asof_date"), asof)
        or source_date_after_asof(row.get("float_shares_asof_date"), asof)
    ):
        return
    days_to_cover = to_float(row.get("days_to_cover"), 0.0) or 0.0
    cover_score = linear_score(days_to_cover, [(0.0, 0.0), (2.0, 25.0), (5.0, 60.0), (10.0, 100.0)])
    cover_available = days_to_cover > 0.0
    row.update(
        {
            "float_shares": 0.0,
            "short_interest_pct_float": 0.0,
            "float_shares_source": "",
            "float_shares_asof_date": "",
            "float_shares_source_asof_date": "",
            "float_shares_staleness_days": "",
            "float_shares_measurement_staleness_days": "",
            "float_shares_proxy_flag": 0.0,
            "public_float_usd": 0.0,
            "public_float_price_date": "",
            "public_float_close_price": 0.0,
            "short_interest_pct_float_available_flag": 0.0,
            "short_interest_pct_score": 0.0,
            "short_interest_days_to_cover_score": round(clamp(cover_score), 4),
            "short_interest_signal_basis": "days_to_cover_only" if cover_available else "no_usable_short_interest_components",
            "short_interest_signal_max_possible_score": 25.0 if cover_available else 0.0,
            "short_interest_signal_score": round(clamp(0.25 * cover_score), 4) if cover_available else 0.0,
        }
    )


def normalize_company_name(raw: object) -> str:
    text = str(raw or "").upper()
    text = COMPANY_SUFFIX_RE.sub(" ", text)
    text = NON_ALNUM_RE.sub(" ", text)
    return " ".join(part for part in text.split() if part)


def load_scoring_export_module() -> Any:
    spec = importlib.util.spec_from_file_location("biotech_score_export_contract", SCORING_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load scoring export contract from {SCORING_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "write_csv"):
        raise RuntimeError(f"Scoring script does not expose write_csv: {SCORING_SCRIPT}")
    return module


def load_dates(conn: sqlite3.Connection, *, source_table: str, start_asof: str, end_asof: str, raw_dates: str) -> list[str]:
    if raw_dates.strip():
        dates = [iso_date(part) for part in raw_dates.replace(";", ",").split(",") if part.strip()]
    else:
        start = parse_date(start_asof)
        end = parse_date(end_asof)
        date_column = "bar_date" if source_table == "market_bars_daily" else "asof_date"
        rows = conn.execute(
            f"""
            SELECT DISTINCT {date_column} AS selected_date
            FROM {source_table}
            WHERE {date_column} IS NOT NULL
            ORDER BY {date_column}
            """
        ).fetchall()
        dates = []
        for row in rows:
            parsed = parse_date(row["selected_date"])
            if parsed is None:
                continue
            if start is not None and parsed < start:
                continue
            if end is not None and parsed > end:
                continue
            dates.append(parsed.isoformat())
    return sorted(dict.fromkeys(dates))


def resolve_score_snapshot_asof(
    conn: sqlite3.Connection,
    asof: str,
    *,
    calibration_tickers: set[str],
    carry_forward: bool,
) -> str | None:
    tickers = sorted(ticker for ticker in calibration_tickers if ticker)
    ticker_filter = ""
    params: tuple[object, ...] = (asof,)
    if tickers:
        placeholders = ", ".join("?" for _ in tickers)
        ticker_filter = f"AND UPPER(ticker) IN ({placeholders})"
        params = (asof, *tickers)
    exact = conn.execute(
        f"""
        SELECT COUNT(*) AS row_count
        FROM daily_scores
        WHERE asof_date = ?
          {ticker_filter}
        """,
        params,
    ).fetchone()
    if exact and int(exact["row_count"] or 0) > 0:
        return asof
    if not carry_forward:
        return asof
    params = (asof, *tickers) if tickers else (asof,)
    prior = conn.execute(
        f"""
        SELECT MAX(asof_date) AS score_asof
        FROM daily_scores
        WHERE asof_date <= ?
          {ticker_filter}
        """,
        params,
    ).fetchone()
    score_asof = str(prior["score_asof"] or "") if prior else ""
    return score_asof or None


def load_calibration_tickers(config: dict[str, Any], *, config_path: Path) -> set[str]:
    settings = cfg_get(config, "biotech_scoring.calibration_cohorts", {}) or {}
    if not isinstance(settings, dict):
        settings = {}
    csv_path = resolve_path(settings.get("csv", "data/biotech_calibration_cohorts.csv"), base_dir=config_path.parent)
    out: set[str] = set()
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = str(row.get("ticker") or "").strip().upper()
                if ticker:
                    out.add(ticker)
    delisted_path = config_path.parent / "data" / "delisted_biotech_calibration_universe.csv"
    if delisted_path.exists():
        with delisted_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                canonical = str(row.get("ticker") or "").strip().upper()
                historical_price_ticker = str(
                    row.get("norgate_symbol") or row.get("calibration_company_ticker") or canonical
                ).strip().upper()
                aliases = {
                    historical_price_ticker,
                    str(row.get("calibration_company_ticker") or "").strip().upper(),
                    str(row.get("norgate_symbol") or "").strip().upper(),
                }
                if canonical and canonical == historical_price_ticker:
                    aliases.add(canonical)
                for ticker in aliases:
                    if ticker:
                        out.add(ticker)
    return out


def load_delisted_universe_metadata(config_path: Path) -> dict[str, dict[str, Any]]:
    """Load calibration-only delisted biotech membership/terminal metadata."""
    path = config_path.parent / "data" / "delisted_biotech_calibration_universe.csv"
    if not path.exists():
        LOGGER.warning("Delisted calibration universe mapping is missing: %s", path)
        return {}
    out: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            row = dict(raw)
            ticker = str(row.get("ticker") or row.get("calibration_company_ticker") or "").strip().upper()
            if not ticker:
                continue
            row["ticker"] = ticker
            row["canonical_ticker"] = ticker
            row["drop_otc_tape"] = 1.0 if as_bool(row.get("drop_otc_tape")) else 0.0
            row["calibration_only"] = 1.0
            row["universe_status"] = "delisted_calibration"
            row["historical_universe_source"] = "delisted_biotech_calibration_universe"
            row["historical_price_ticker"] = (
                str(row.get("norgate_symbol") or row.get("calibration_company_ticker") or ticker).strip().upper()
            )
            aliases = {
                row["historical_price_ticker"],
                str(row.get("calibration_company_ticker") or "").strip().upper(),
                str(row.get("norgate_symbol") or "").strip().upper(),
            }
            if row["historical_price_ticker"] == ticker:
                aliases.add(ticker)
            for alias in aliases:
                if alias:
                    out[alias] = row
    return out


def _metadata_date(metadata: dict[str, Any], *fields: str) -> date | None:
    for field in fields:
        parsed = parse_date(metadata.get(field))
        if parsed is not None:
            return parsed
    return None


def delisted_membership_valid(metadata: dict[str, Any], asof_day: date | None) -> bool:
    """Return whether a delisted calibration ticker belongs to the PIT panel on asof."""
    if asof_day is None:
        return False
    start = _metadata_date(metadata, "price_start_date")
    if start is not None and asof_day < start:
        return False
    terminal = _metadata_date(metadata, "terminal_date")
    price_end = _metadata_date(metadata, "price_end_date")
    end = terminal or price_end
    if end is not None and asof_day > end:
        return False
    return True


def load_score_rows(
    conn: sqlite3.Connection,
    asof: str,
    *,
    calibration_tickers: set[str],
    score_snapshot_asof: str | None = None,
) -> list[dict[str, Any]]:
    source_asof = score_snapshot_asof or asof
    tickers = sorted(ticker for ticker in calibration_tickers if ticker)
    ticker_filter = ""
    params: tuple[Any, ...] = (source_asof,)
    if tickers:
        placeholders = ", ".join("?" for _ in tickers)
        ticker_filter = f"AND UPPER(s.ticker) IN ({placeholders})"
        params = (source_asof, *tickers)
    rows = conn.execute(
        f"""
        SELECT
            s.*,
            c.company_name AS company_company_name,
            c.sector AS company_sector,
            c.industry AS company_industry,
            c.industry_aggregate AS company_industry_aggregate,
            c.country AS company_country,
            c.currency AS company_currency,
            c.universe_status AS company_universe_status,
            c.is_active AS company_is_active
        FROM daily_scores s
        LEFT JOIN companies c ON c.company_id = s.company_id
        WHERE s.asof_date = ?
          {ticker_filter}
        ORDER BY
            CASE WHEN s.rank IS NULL THEN 1 ELSE 0 END,
            CAST(s.rank AS REAL),
            s.ticker
        """,
        params,
    ).fetchall()
    out = [dict(row) for row in rows]
    if source_asof != asof:
        for row in out:
            row["_score_snapshot_asof_date"] = source_asof
            row["source_snapshot_asof_date"] = row.get("source_snapshot_asof_date") or source_asof
            row["feature_data_asof_date"] = row.get("feature_data_asof_date") or source_asof
            row["asof_date"] = asof
    return out


def load_market_context(conn: sqlite3.Connection, asof: str, *, tickers: set[str]) -> dict[str, dict[str, Any]]:
    clean_tickers = sorted(ticker for ticker in tickers if ticker)
    if not clean_tickers:
        return {}
    placeholders = ", ".join("?" for _ in clean_tickers)
    params: tuple[Any, ...] = (asof, asof, *clean_tickers)
    rows = conn.execute(
        f"""
        WITH recent_bars AS (
            SELECT
                UPPER(ticker) AS ticker,
                bar_date,
                close,
                volume
            FROM market_bars_daily
            WHERE bar_date <= ?
              AND bar_date >= date(?, '-180 day')
              AND UPPER(ticker) IN ({placeholders})
        ),
        ranked_bars AS (
            SELECT
                ticker,
                bar_date,
                close,
                volume,
                ROW_NUMBER() OVER (PARTITION BY UPPER(ticker) ORDER BY bar_date DESC) AS rn
            FROM recent_bars
        ),
        avg60 AS (
            SELECT
                ticker,
                AVG(CASE WHEN close > 0 AND volume > 0 THEN close * volume ELSE NULL END) AS avg_dollar_volume_60d
            FROM ranked_bars
            WHERE rn <= 60
            GROUP BY ticker
        ),
        latest AS (
            SELECT
                ticker,
                MAX(bar_date) AS latest_price_date
            FROM recent_bars
            GROUP BY ticker
        )
        SELECT
            latest.ticker,
            latest.latest_price_date,
            avg60.avg_dollar_volume_60d
        FROM latest
        LEFT JOIN avg60 ON avg60.ticker = latest.ticker
        """,
        params,
    ).fetchall()
    return {str(row["ticker"] or "").upper(): dict(row) for row in rows}


def unique_delisted_metadata(metadata: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for row in metadata.values():
        key = str(row.get("canonical_ticker") or row.get("ticker") or "").strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def add_missing_delisted_membership_rows(
    rows: list[dict[str, Any]],
    *,
    asof: str,
    delisted_metadata: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add explicit excluded rows for alive delisted members missing from scores."""
    asof_day = parse_date(asof)
    if asof_day is None or not delisted_metadata:
        return rows
    out = [dict(row) for row in rows]
    existing = {str(row.get("ticker") or "").strip().upper() for row in out}
    for metadata in unique_delisted_metadata(delisted_metadata):
        if not delisted_membership_valid(metadata, asof_day):
            continue
        price_ticker = str(
            metadata.get("historical_price_ticker")
            or metadata.get("norgate_symbol")
            or metadata.get("calibration_company_ticker")
            or metadata.get("ticker")
            or ""
        ).strip().upper()
        canonical_ticker = str(metadata.get("canonical_ticker") or metadata.get("ticker") or "").strip().upper()
        if not price_ticker or price_ticker in existing or canonical_ticker in existing:
            continue
        existing.add(price_ticker)
        cohort = str(metadata.get("cohort") or "").strip()
        out.append(
            {
                "asof_date": asof,
                "rank": 999000 + len(out),
                "company_id": f"delisted:{price_ticker}",
                "ticker": price_ticker,
                "company_name": metadata.get("company_name") or canonical_ticker or price_ticker,
                "company_company_name": metadata.get("company_name") or canonical_ticker or price_ticker,
                "company_sector": "Health Care",
                "company_industry": "Biotechnology",
                "company_country": "US",
                "company_currency": "USD",
                "universe_status": "delisted_calibration",
                "historical_universe_source": "delisted_biotech_calibration_universe",
                "price_start_date": metadata.get("price_start_date") or "",
                "price_end_date": metadata.get("price_end_date") or "",
                "terminal_date": metadata.get("terminal_date") or "",
                "historical_price_ticker": price_ticker,
                "calibration_only": 1.0,
                "recovery_type": metadata.get("recovery_type") or "",
                "equity_recovery": metadata.get("equity_recovery") or "",
                "drop_otc_tape": metadata.get("drop_otc_tape") or 0.0,
                "biotech_primary_cohort": cohort,
                "biotech_cohort_calibration_eligible_flag": 1.0 if cohort in ALLOWED_CALIBRATION_COHORTS else 0.0,
                "biotech_cohort_investible_flag": 0.0,
                "biotech_cohort_exclusion_reason": "delisted_membership_missing_score",
                "bucket": "avoid",
                "allocation_bucket": "avoid",
                "opportunity_score": 0.0,
                "production_rank_score": 0.0,
                "production_rank_score_field": "opportunity_score",
                "production_score_source": "legacy_allocation",
                "discovery_opportunity_score": 0.0,
                "tier1_selection_gate_score": 0.0,
                "data_quality_confidence_multiplier": 1.0,
                "core_structural_veto_flag": 0.0,
                "rank_quality_cap_vetoed": 0.0,
            }
        )
    return out


def prepare_score_rows_for_export(
    rows: list[dict[str, Any]],
    export_module: Any,
    *,
    model_metadata: dict[str, Any],
    market_context: dict[str, dict[str, Any]] | None = None,
    delisted_metadata: dict[str, dict[str, Any]] | None = None,
    survivorship_corrected_panel: bool = False,
    strict_oos_start_date: date | None = None,
) -> list[dict[str, Any]]:
    market_context = market_context or {}
    delisted_metadata = delisted_metadata or {}
    rows = [dict(row) for row in rows]
    enrich = getattr(export_module, "enrich_portfolio_layer_contract_rows", None)
    if callable(enrich):
        enrich(rows)
    for row in rows:
        def fill_blank(field: str, value: object) -> None:
            if is_blank(row.get(field)):
                row[field] = value

        asof = str(row.get("asof_date") or "")
        asof_day = parse_date(asof)
        ticker = str(row.get("ticker") or "").strip().upper()
        delisted_info = delisted_metadata.get(ticker, {})
        if delisted_info:
            row["universe_status"] = "delisted_calibration"
            row["historical_universe_source"] = "delisted_biotech_calibration_universe"
            for source_field, target_field in (
                ("price_start_date", "price_start_date"),
                ("price_end_date", "price_end_date"),
                ("terminal_date", "terminal_date"),
                ("historical_price_ticker", "historical_price_ticker"),
                ("recovery_type", "recovery_type"),
                ("equity_recovery", "equity_recovery"),
                ("drop_otc_tape", "drop_otc_tape"),
                ("calibration_only", "calibration_only"),
            ):
                value = delisted_info.get(source_field)
                if not is_blank(value):
                    row[target_field] = value
            if is_blank(row.get("company_name")) and not is_blank(delisted_info.get("company_name")):
                row["company_name"] = delisted_info.get("company_name")
        raw_universe_status = str(
            row.get("universe_status") or row.get("company_universe_status") or "live"
        ).strip().lower()
        if delisted_info:
            pit_membership_valid = delisted_membership_valid(delisted_info, asof_day)
        else:
            pit_membership_valid = bool(ticker and raw_universe_status not in {"remove", "excluded", "inactive"})
        ticker_market = market_context.get(ticker, {})
        latest_price_date = str(ticker_market.get("latest_price_date") or "")
        avg60 = to_float(ticker_market.get("avg_dollar_volume_60d"), None)
        has_price_data = bool(latest_price_date)
        sanitize_short_interest_fields_for_pit(row, asof=asof)
        fill_blank("company_name", row.get("company_company_name") or ticker)
        fill_blank("sector", row.get("company_sector") or "Health Care")
        fill_blank("industry", row.get("company_industry") or "Biotechnology")
        fill_blank("subsector", row.get("biotech_primary_cohort") or row.get("company_industry") or "Biotechnology")
        fill_blank("country", row.get("company_country") or "US")
        fill_blank("currency", row.get("company_currency") or "USD")
        fill_blank("score_model_version", model_metadata.get("score_model_version") or "biotech_opportunity_score_historical_export")
        fill_blank("model_family", model_metadata.get("model_family") or "biotech_tier1_allocation_discovery")
        fill_blank("model_version", model_metadata.get("model_version") or "biotech_historical_export")
        fill_blank("scoring_contract_version", model_metadata.get("scoring_contract_version") or "biotech_daily_scores_contract_v1")
        row["production_rank_score_field"] = "opportunity_score"
        row["production_score_source"] = "legacy_allocation"
        row["allocation_opportunity_score"] = value_or_blank(row.get("opportunity_score"))
        fill_blank("allocation_bucket", row.get("bucket") or "")
        row["production_rank_score"] = value_or_blank(row.get("opportunity_score"))
        fill_blank("production_rank_risk_score", row.get("risk_score") or "")

        native_score_field = str(row.get("native_score_field") or row.get("production_rank_score_field") or "opportunity_score")
        native_score_value = row.get("native_score_value")
        if is_blank(native_score_value):
            native_score_value = row.get(native_score_field)
        if is_blank(native_score_value):
            native_score_value = row.get("production_rank_score") or row.get("opportunity_score")
        calibration_eligible = to_float(row.get("calibration_eligible_flag"), None)
        if calibration_eligible is None:
            calibration_eligible = to_float(row.get("biotech_cohort_calibration_eligible_flag"), 0.0)
        calibration_eligible_value = calibration_eligible if calibration_eligible is not None else 0.0
        investible_value = to_float(row.get("biotech_cohort_investible_flag"), 0.0)
        core_veto_value = to_float(row.get("core_structural_veto_flag"), 0.0)
        rank_veto_value = to_float(row.get("rank_quality_cap_vetoed"), 0.0)
        investible = (investible_value if investible_value is not None else 0.0) > 0.0
        core_veto = (core_veto_value if core_veto_value is not None else 0.0) > 0.0
        rank_veto = (rank_veto_value if rank_veto_value is not None else 0.0) > 0.0
        allocation_bucket = str(row.get("allocation_bucket") or row.get("bucket") or "").strip().lower()
        candidate_score = row.get("production_rank_score") if not is_blank(row.get("production_rank_score")) else row.get("opportunity_score")
        candidate_score_value = to_float(candidate_score, None)
        native_score_float = to_float(native_score_value, None)
        missing_score = (
            candidate_score_value is None
            or candidate_score_value <= 0.0
            or native_score_float is None
            or native_score_float <= 0.0
        )
        reason_parts: list[str] = []
        if missing_score:
            reason_parts.append("missing_score")
        if not has_price_data:
            reason_parts.append("missing_price_data")
        if not investible:
            reason_parts.append("not_investible")
        if core_veto:
            reason_parts.append("core_structural_veto")
        if rank_veto:
            reason_parts.append("rank_quality_cap_veto")
        if allocation_bucket == "avoid":
            reason_parts.append("allocation_bucket_avoid")
        candidate_status = "eligible"
        candidate_reason = "ok"
        if missing_score:
            candidate_status = "excluded"
            candidate_reason = "missing_score"
        elif not has_price_data:
            candidate_status = "excluded"
            candidate_reason = "missing_price_data"
        elif not investible:
            candidate_status = "excluded"
            candidate_reason = "not_investible"
        elif core_veto:
            candidate_status = "excluded"
            candidate_reason = "core_structural_veto"
        elif allocation_bucket == "avoid":
            candidate_status = "excluded"
            candidate_reason = "allocation_bucket_avoid"
        elif rank_veto:
            candidate_status = "review"
            candidate_reason = "rank_quality_cap_veto"
        elif reason_parts:
            candidate_status = "excluded"
            candidate_reason = "|".join(reason_parts)
        candidate_gate = bool(
            not missing_score
            and has_price_data
            and candidate_status == "eligible"
            and investible
            and not core_veto
        )
        review_reason = "|".join(
            str(row.get(field) or "").strip()
            for field in ("core_structural_veto_reasons", "biotech_cohort_exclusion_reason", "rank_quality_cap_reasons")
            if str(row.get(field) or "").strip()
        )

        row["portfolio_candidate_gate"] = 1.0 if candidate_gate else 0.0
        row["portfolio_candidate_score"] = candidate_score if candidate_score_value is not None and candidate_score_value > 0.0 else 0.0
        row["portfolio_candidate_status"] = candidate_status
        row["portfolio_candidate_reason"] = candidate_reason
        fill_blank("calibration_eligible_flag", calibration_eligible)
        fill_blank("score_confidence", row.get("data_quality_confidence_multiplier") or "")
        if avg60 is not None and avg60 > 0.0:
            row["avg_dollar_volume_60d"] = round(avg60, 4)
        else:
            fill_blank("avg_dollar_volume_60d", "")
        fill_blank("review_reason", review_reason)
        fill_blank("eligibility_reason", row.get("portfolio_candidate_reason") or "")
        fill_blank("universe_status", row.get("company_universe_status") or "live")
        fill_blank(
            "historical_universe_source",
            "delisted_biotech_calibration_universe" if delisted_info else "current_final_scoring_universe",
        )
        fill_blank("price_start_date", "")
        fill_blank("price_end_date", "")
        fill_blank("terminal_date", "")
        fill_blank("historical_price_ticker", ticker)
        fill_blank("calibration_only", 1.0 if delisted_info else 0.0)
        fill_blank("recovery_type", "")
        fill_blank("equity_recovery", "")
        fill_blank("drop_otc_tape", 0.0)
        fill_blank("latest_price_date", row.get("price_data_asof_date") or latest_price_date)
        fill_blank("source_snapshot_asof_date", asof)
        fill_blank("price_data_asof_date", latest_price_date)
        fill_blank("feature_data_asof_date", asof)
        fill_blank("clinical_data_asof_date", row.get("feature_data_asof_date") or row.get("source_snapshot_asof_date") or asof)
        fill_blank("financial_data_asof_date", row.get("feature_data_asof_date") or row.get("source_snapshot_asof_date") or asof)
        fill_blank("short_interest_asof_date", row.get("feature_data_asof_date") or row.get("source_snapshot_asof_date") or asof)
        fill_blank("institutional_data_asof_date", row.get("feature_data_asof_date") or row.get("source_snapshot_asof_date") or asof)
        fill_blank("insider_data_asof_date", row.get("feature_data_asof_date") or row.get("source_snapshot_asof_date") or asof)
        fill_blank("borrow_data_asof_date", row.get("feature_data_asof_date") or row.get("source_snapshot_asof_date") or asof)
        fill_blank("calibration_cohort", row.get("biotech_primary_cohort") or "")
        if calibration_eligible_value <= 0.0:
            calibration_status = "excluded"
            calibration_status_reason = row.get("biotech_cohort_exclusion_reason") or "not_calibration_eligible"
        elif missing_score:
            calibration_status = "excluded"
            calibration_status_reason = "missing_score"
        elif not has_price_data:
            calibration_status = "excluded"
            calibration_status_reason = "missing_price_data"
        else:
            calibration_status = "eligible"
            calibration_status_reason = "eligible"
        row["calibration_status"] = calibration_status
        row["calibration_status_reason"] = calibration_status_reason
        fill_blank("native_score_field", native_score_field)
        row["native_score_value"] = native_score_value if not is_blank(native_score_value) else ""
        fill_blank("score_scale_min", 0.0)
        fill_blank("score_scale_max", 100.0)
        fill_blank("score_neutral_value", 50.0)
        row["score_zero_is_missing_flag"] = 1.0 if missing_score else 0.0
        pit_valid = True
        if asof_day is not None:
            asof_check = asof_day.isoformat()
            for date_column in SOURCE_DATE_COLUMNS_NOT_AFTER_ASOF:
                if date_column in {"latest_price_date", "forward_catalyst_asof_date"}:
                    continue
                parsed = parse_date(row.get(date_column))
                if parsed is not None and parsed.isoformat() > asof_check:
                    pit_valid = False
                    break
        if not pit_membership_valid:
            pit_valid = False
        research_eligible = bool(
            ticker
            and calibration_eligible_value > 0.0
            and not missing_score
            and has_price_data
            and pit_valid
        )
        if research_eligible:
            research_status = "valid_research_calibration_input"
            research_reason = "ok"
        elif not ticker:
            research_status = "missing_ticker"
            research_reason = "missing_ticker"
        elif calibration_eligible_value <= 0.0:
            research_status = "not_calibration_eligible"
            research_reason = calibration_status_reason
        elif missing_score:
            research_status = "missing_score"
            research_reason = "missing_score"
        elif not has_price_data:
            research_status = "missing_price_data"
            research_reason = "missing_price_data"
        elif not pit_valid:
            research_status = "not_pit_valid"
            research_reason = "not_pit_valid"
        else:
            research_status = "not_calibration_eligible"
            research_reason = "not_calibration_eligible"
        survivorship_corrected = bool(survivorship_corrected_panel and pit_membership_valid)
        stage11_panel_source = (
            "biotech_survivorship_corrected_pit_score_recompute"
            if survivorship_corrected_panel
            else "biotech_current_universe_replay_not_survivorship_corrected"
        )
        stage11_eligible = bool(research_eligible and survivorship_corrected)
        if stage11_eligible:
            stage11_reason = "ok"
        elif not research_eligible:
            stage11_reason = research_reason
        elif not survivorship_corrected:
            stage11_reason = "not_survivorship_corrected"
        elif not pit_valid:
            stage11_reason = "not_pit_valid"
        else:
            stage11_reason = "excluded_by_calibration_policy"
        source_snapshot = str(row.get("source_snapshot_asof_date") or row.get("_score_snapshot_asof_date") or asof).strip()
        strict_oos = bool(
            research_eligible
            and strict_oos_start_date is not None
            and asof_day is not None
            and asof_day >= strict_oos_start_date
            and source_snapshot == asof
            and (to_float(row.get("calibration_only"), 0.0) or 0.0) <= 0.0
        )
        row["research_calibration_input_eligible_flag"] = 1.0 if research_eligible else 0.0
        row["research_calibration_status"] = research_status
        row["research_calibration_reason"] = research_reason
        row["calibration_sample_role"] = "strict_oos" if strict_oos else "pre_lock_research" if research_eligible else "excluded"
        row["oos_score_valid_flag"] = 1.0 if strict_oos else 0.0
        row["stage11_calibration_input_eligible_flag"] = 1.0 if stage11_eligible else 0.0
        row["stage11_calibration_input_reason"] = stage11_reason
        row["stage11_calibration_panel_source"] = stage11_panel_source
        row["survivorship_corrected_panel_flag"] = 1.0 if survivorship_corrected else 0.0
        fill_blank("capacity_bucket", "")
        fill_blank("min_position_size_feasible", "")
        fill_blank("max_position_size_feasible", "")
        fill_blank("liquidity_score", "")
        fill_blank("forward_catalyst_event_date", "")
        fill_blank("forward_catalyst_asof_date", "")
    return rows


def load_terminal_events(config_path: Path) -> dict[str, list[dict[str, Any]]]:
    mapping = config_path.parent / "data" / "delisted_biotech_calibration_universe.csv"
    if not mapping.exists():
        LOGGER.warning("Delisted calibration universe mapping is missing: %s", mapping)
        return {}
    terminal_events: dict[str, list[dict[str, Any]]] = {}
    with mapping.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = str(row.get("ticker") or "").strip().upper()
            terminal = parse_date(row.get("terminal_date") or row.get("delisting_date") or row.get("price_end_date"))
            if ticker and terminal is not None:
                terminal_events.setdefault(ticker, []).append(
                    {
                        "terminal_date": terminal,
                        "company_name": str(row.get("company_name") or ""),
                        "company_name_key": normalize_company_name(row.get("company_name")),
                        "calibration_company_ticker": str(row.get("calibration_company_ticker") or ""),
                    }
                )
    return terminal_events


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [str(field or "") for field in (reader.fieldnames or [])], [dict(row) for row in reader]


def validate_score_csv(
    path: Path,
    *,
    asof: str,
    min_rows: int,
    terminal_events: dict[str, list[dict[str, Any]]],
    calibration_tickers: set[str],
) -> list[str]:
    failures: list[str] = []
    if not path.exists():
        return [f"missing_csv:{path}"]
    fieldnames, rows = read_csv_rows(path)
    if len(rows) < min_rows:
        failures.append(f"row_count<{min_rows}:{len(rows)}")

    missing_columns = [column for column in REQUIRED_PRESENT_COLUMNS if column not in fieldnames]
    if missing_columns:
        failures.append("missing_columns:" + ",".join(missing_columns))

    asof_values = {str(row.get("asof_date") or "").strip() for row in rows}
    if asof_values != {asof}:
        failures.append("asof_mismatch:" + ",".join(sorted(asof_values)[:5]))

    tickers = [str(row.get("ticker") or "").strip().upper() for row in rows if str(row.get("ticker") or "").strip()]
    duplicate_tickers = sorted({ticker for ticker in tickers if tickers.count(ticker) > 1})
    if duplicate_tickers:
        failures.append("duplicate_tickers:" + ",".join(duplicate_tickers[:20]))

    asof_day = parse_date(asof)
    if asof_day is None:
        failures.append(f"invalid_asof:{asof}")
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if calibration_tickers and ticker and ticker not in calibration_tickers:
            failures.append(f"non_calibration_ticker:{ticker}")
        for column in REQUIRED_NONBLANK_COLUMNS:
            if column in fieldnames and is_blank(row.get(column)):
                failures.append(f"blank_{column}:{ticker}")
                break
        cohort = str(row.get("biotech_primary_cohort") or "").strip()
        if cohort and cohort not in ALLOWED_CALIBRATION_COHORTS:
            failures.append(f"old_or_unknown_cohort:{ticker}:{cohort}")
        # These columns were added in scoring v2.  Pre-v2 historical rows will
        # have blank values; only validate when the field is actually populated.
        src = str(row.get("production_score_source") or "").strip()
        if src and src != "legacy_allocation":
            failures.append(f"production_source_not_allocation:{ticker}")
        rank_field = str(row.get("production_rank_score_field") or "").strip()
        if rank_field and rank_field != "opportunity_score":
            failures.append(f"production_rank_field_not_opportunity:{ticker}")
        research_eligible = (to_float(row.get("research_calibration_input_eligible_flag"), 0.0) or 0.0) > 0.0
        stage11_eligible = (to_float(row.get("stage11_calibration_input_eligible_flag"), 0.0) or 0.0) > 0.0
        survivorship_flag = (to_float(row.get("survivorship_corrected_panel_flag"), 0.0) or 0.0) > 0.0
        score_missing = (to_float(row.get("score_zero_is_missing_flag"), 0.0) or 0.0) > 0.0
        native_score = to_float(row.get("native_score_value"), 0.0) or 0.0
        if research_eligible:
            if str(row.get("research_calibration_reason") or "").strip() != "ok":
                failures.append(f"research_eligible_reason_not_ok:{ticker}:{row.get('research_calibration_reason')}")
            if score_missing or native_score <= 0.0:
                failures.append(f"research_eligible_missing_score:{ticker}")
            if is_blank(row.get("price_data_asof_date")):
                failures.append(f"research_eligible_missing_price_data:{ticker}")
        if stage11_eligible:
            if not research_eligible:
                failures.append(f"stage11_eligible_without_research_eligible:{ticker}")
            if not survivorship_flag:
                failures.append(f"stage11_eligible_without_survivorship_flag:{ticker}")
            if str(row.get("stage11_calibration_input_reason") or "").strip() != "ok":
                failures.append(f"stage11_eligible_reason_not_ok:{ticker}:{row.get('stage11_calibration_input_reason')}")
            if str(row.get("stage11_calibration_panel_source") or "").strip() != "biotech_survivorship_corrected_pit_score_recompute":
                failures.append(f"stage11_eligible_wrong_panel_source:{ticker}")
        company_name_key = normalize_company_name(row.get("company_name"))
        if asof_day is not None and ticker in terminal_events:
            for event in terminal_events[ticker]:
                terminal_date = event["terminal_date"]
                if asof_day <= terminal_date:
                    continue
                event_name_key = str(event.get("company_name_key") or "")
                if company_name_key and event_name_key and company_name_key != event_name_key:
                    continue
                failures.append(
                    f"post_terminal_row:{ticker}:{terminal_date.isoformat()}:"
                    f"{event.get('calibration_company_ticker', '')}"
                )
        for column in SOURCE_DATE_COLUMNS_NOT_AFTER_ASOF:
            if column not in fieldnames or is_blank(row.get(column)):
                continue
            parsed = parse_date(row.get(column))
            if parsed is not None and asof_day is not None and parsed > asof_day:
                failures.append(f"future_source_date:{ticker}:{column}:{parsed.isoformat()}")

    return failures


def write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or ["message"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_stage11_sidecar(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STAGE11_SIDECAR_COLUMNS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in STAGE11_SIDECAR_COLUMNS})


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_root = (
        args.output_root.expanduser().resolve()
        if args.output_root
        else resolve_path(cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    )
    output_csv_name = str(cfg_get(config, "biotech_scoring.output_csv", "biotech_daily_scores.csv"))
    summary_csv = args.summary_csv.expanduser().resolve() if args.summary_csv else output_root / "historical_score_csv_generation_summary.csv"
    manifest_json = args.manifest_json.expanduser().resolve() if args.manifest_json else output_root / "historical_score_csv_generation_manifest.json"
    terminal_events = load_terminal_events(config_path)
    delisted_metadata = load_delisted_universe_metadata(config_path)
    calibration_tickers = load_calibration_tickers(config, config_path=config_path)
    survivorship_corrected_panel = (
        bool(args.survivorship_corrected_panel)
        if args.survivorship_corrected_panel is not None
        else as_bool(cfg_get(config, "biotech_historical_sequence.survivorship_corrected_panel", True), True)
    )
    strict_oos_start_date = parse_date(cfg_get(config, "biotech_historical_sequence.strict_oos_start_date", ""))
    model_metadata = cfg_get(config, "biotech_scoring.model_metadata", {}) or {}
    if not isinstance(model_metadata, dict):
        model_metadata = {}
    export_module = load_scoring_export_module()

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        dates = load_dates(
            conn,
            source_table=args.source_table,
            start_asof=args.start_asof,
            end_asof=args.end_asof,
            raw_dates=args.dates,
        )
        if args.fridays_only:
            friday_dates: list[str] = []
            for item in dates:
                parsed_item = parse_date(item)
                if parsed_item is not None and parsed_item.weekday() == 4:
                    friday_dates.append(item)
            dates = friday_dates
        if int(args.max_dates or 0) > 0:
            dates = dates[: int(args.max_dates)]
        if not dates:
            raise RuntimeError("No historical score dates selected.")

        summary_rows: list[dict[str, Any]] = []
        invalid_dates: list[str] = []
        for asof in dates:
            output_dir: Path = output_root / compact_date(asof)
            output_path: Path = output_dir / output_csv_name
            sidecar_path: Path = output_dir / str(args.stage11_sidecar_name)
            action = "validated_existing"
            validation_path: Path = output_path
            generated_temp_path: Path | None = None
            generated_sidecar_temp_path: Path | None = None
            score_snapshot_asof = ""
            carry_forwarded = 0.0
            row_count = 0
            column_count = 0
            stage11_eligible_count = 0
            research_eligible_count = 0
            delisted_row_count = 0
            if not args.validate_only and (args.overwrite or not output_path.exists()):
                resolved_snapshot = resolve_score_snapshot_asof(
                    conn,
                    asof,
                    calibration_tickers=calibration_tickers,
                    carry_forward=bool(args.carry_forward_scores),
                )
                score_snapshot_asof = resolved_snapshot or ""
                carry_forwarded = 1.0 if resolved_snapshot and resolved_snapshot != asof else 0.0
                if resolved_snapshot is None:
                    score_rows = []
                else:
                    score_rows = load_score_rows(
                        conn,
                        asof,
                        calibration_tickers=calibration_tickers,
                        score_snapshot_asof=resolved_snapshot,
                    )
                if not score_rows:
                    action = "missing_db_rows"
                else:
                    if survivorship_corrected_panel:
                        score_rows = add_missing_delisted_membership_rows(
                            score_rows,
                            asof=asof,
                            delisted_metadata=delisted_metadata,
                        )
                    market_context = load_market_context(
                        conn,
                        asof,
                        tickers={str(row.get("ticker") or "").strip().upper() for row in score_rows},
                    )
                    score_rows = prepare_score_rows_for_export(
                        score_rows,
                        export_module,
                        model_metadata=model_metadata,
                        market_context=market_context,
                        delisted_metadata=delisted_metadata,
                        survivorship_corrected_panel=survivorship_corrected_panel,
                        strict_oos_start_date=strict_oos_start_date,
                    )
                    output_dir.mkdir(parents=True, exist_ok=True)
                    generated_temp_path = output_path.with_name(output_path.name + ".tmp")
                    export_module.write_csv(generated_temp_path, score_rows)
                    generated_sidecar_temp_path = sidecar_path.with_name(sidecar_path.name + ".tmp")
                    write_stage11_sidecar(generated_sidecar_temp_path, score_rows)
                    validation_path = generated_temp_path
                    action = "generated_carry_forward_pending_validation" if carry_forwarded else "generated_pending_validation"
            failures = validate_score_csv(
                validation_path,
                asof=asof,
                min_rows=max(1, int(args.min_rows)),
                terminal_events=terminal_events,
                calibration_tickers=calibration_tickers,
            )
            if validation_path.exists():
                fieldnames, csv_rows = read_csv_rows(validation_path)
                row_count = len(csv_rows)
                column_count = len(fieldnames)
                research_eligible_count = sum(
                    1
                    for row in csv_rows
                    if (to_float(row.get("research_calibration_input_eligible_flag"), 0.0) or 0.0) > 0.0
                )
                stage11_eligible_count = sum(
                    1
                    for row in csv_rows
                    if (to_float(row.get("stage11_calibration_input_eligible_flag"), 0.0) or 0.0) > 0.0
                )
                delisted_row_count = sum(
                    1
                    for row in csv_rows
                    if str(row.get("universe_status") or "").strip().lower() == "delisted_calibration"
                )
            status = "PASS" if not failures else "FAIL"
            if failures:
                invalid_dates.append(asof)
                if generated_temp_path is not None and generated_temp_path.exists():
                    generated_temp_path.unlink()
                    action = "generated_invalid_rejected"
                if generated_sidecar_temp_path is not None and generated_sidecar_temp_path.exists():
                    generated_sidecar_temp_path.unlink()
            elif generated_temp_path is not None:
                generated_temp_path.replace(output_path)
                if generated_sidecar_temp_path is not None:
                    generated_sidecar_temp_path.replace(sidecar_path)
                validation_path = output_path
                action = "generated_carry_forward" if carry_forwarded else "generated"
            summary_rows.append(
                {
                    "asof_date": asof,
                    "dated_folder": compact_date(asof),
                    "status": status,
                    "action": action,
                    "row_count": row_count,
                    "column_count": column_count,
                    "research_calibration_input_eligible_count": research_eligible_count,
                    "stage11_calibration_input_eligible_count": stage11_eligible_count,
                    "delisted_calibration_row_count": delisted_row_count,
                    "score_snapshot_asof": score_snapshot_asof,
                    "carry_forwarded": carry_forwarded,
                    "csv_path": str(output_path),
                    "stage11_sidecar_path": str(sidecar_path),
                    "failure_count": len(failures),
                    "failures": "|".join(failures[:25]),
                }
            )
            LOGGER.info("%s %s rows=%d path=%s", status, asof, row_count, output_path)

    write_summary(summary_csv, summary_rows)
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "config": str(config_path),
        "db": str(db_path),
        "output_root": str(output_root),
        "output_csv_name": output_csv_name,
        "date_count": len(summary_rows),
        "invalid_date_count": len(invalid_dates),
        "invalid_dates": invalid_dates[:100],
        "summary_csv": str(summary_csv),
        "date_source_table": args.source_table,
        "carry_forward_scores": bool(args.carry_forward_scores),
        "survivorship_corrected_panel": bool(survivorship_corrected_panel),
        "strict_oos_start_date": strict_oos_start_date.isoformat() if strict_oos_start_date is not None else "",
        "stage11_sidecar_name": str(args.stage11_sidecar_name),
        "delisted_calibration_universe_count": len(delisted_metadata),
        "oos_contract_rules": {
            "dated_folder_format": "YYYYMMDD",
            "all_rows_match_asof_date": True,
            "duplicate_tickers_for_same_asof": False,
            "five_calibration_cohorts_only": sorted(ALLOWED_CALIBRATION_COHORTS),
            "production_rank_source": "legacy_allocation/opportunity_score",
            "no_post_terminal_delisted_rows": True,
            "selected_source_date_columns_not_after_asof": SOURCE_DATE_COLUMNS_NOT_AFTER_ASOF,
        },
    }
    manifest_json.parent.mkdir(parents=True, exist_ok=True)
    manifest_json.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if invalid_dates and not args.allow_invalid:
        raise RuntimeError(
            f"Historical biotech score CSV validation failed for {len(invalid_dates)} date(s). "
            f"Summary: {summary_csv}"
        )


if __name__ == "__main__":
    main()
