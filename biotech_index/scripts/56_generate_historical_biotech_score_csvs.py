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
    parser.add_argument("--source-table", choices=["daily_scores", "daily_features"], default="daily_scores")
    parser.add_argument("--fridays-only", action="store_true")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--manifest-json", type=Path, default=None)
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--min-rows", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--allow-invalid", action="store_true", help="Write the summary/manifest and exit 0 even when validation fails.")
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


def to_float(raw: object, default: float | None = None) -> float | None:
    try:
        value = float(str(raw).strip()) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


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
        rows = conn.execute(
            f"""
            SELECT DISTINCT asof_date
            FROM {source_table}
            WHERE asof_date IS NOT NULL
            ORDER BY asof_date
            """
        ).fetchall()
        dates = []
        for row in rows:
            parsed = parse_date(row["asof_date"])
            if parsed is None:
                continue
            if start is not None and parsed < start:
                continue
            if end is not None and parsed > end:
                continue
            dates.append(parsed.isoformat())
    return sorted(dict.fromkeys(dates))


def load_calibration_tickers(config: dict[str, Any], *, config_path: Path) -> set[str]:
    settings = cfg_get(config, "biotech_scoring.calibration_cohorts", {}) or {}
    if not isinstance(settings, dict):
        settings = {}
    csv_path = resolve_path(settings.get("csv", "data/biotech_calibration_cohorts.csv"), base_dir=config_path.parent)
    out: set[str] = set()
    for path, columns in (
        (csv_path, ("ticker",)),
        (config_path.parent / "data" / "delisted_biotech_calibration_universe.csv", ("ticker", "calibration_company_ticker")),
    ):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                for column in columns:
                    ticker = str(row.get(column) or "").strip().upper()
                    if ticker:
                        out.add(ticker)
    return out


def load_score_rows(conn: sqlite3.Connection, asof: str, *, calibration_tickers: set[str]) -> list[dict[str, Any]]:
    tickers = sorted(ticker for ticker in calibration_tickers if ticker)
    ticker_filter = ""
    params: tuple[Any, ...] = (asof,)
    if tickers:
        placeholders = ", ".join("?" for _ in tickers)
        ticker_filter = f"AND UPPER(s.ticker) IN ({placeholders})"
        params = (asof, *tickers)
    rows = conn.execute(
        f"""
        SELECT
            s.*,
            c.company_name AS company_company_name,
            c.sector AS company_sector,
            c.industry AS company_industry,
            c.industry_aggregate AS company_industry_aggregate,
            c.country AS company_country,
            c.currency AS company_currency
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
    return [dict(row) for row in rows]


def prepare_score_rows_for_export(
    rows: list[dict[str, Any]],
    export_module: Any,
    *,
    model_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in rows]
    enrich = getattr(export_module, "enrich_portfolio_layer_contract_rows", None)
    if callable(enrich):
        enrich(rows)
    for row in rows:
        def fill_blank(field: str, value: object) -> None:
            if is_blank(row.get(field)):
                row[field] = value

        asof = str(row.get("asof_date") or "")
        ticker = str(row.get("ticker") or "").strip().upper()
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
        fill_blank("production_rank_score_field", "opportunity_score")
        fill_blank("production_score_source", "legacy_allocation")
        fill_blank("allocation_opportunity_score", row.get("opportunity_score") or "")
        fill_blank("allocation_bucket", row.get("bucket") or "")
        fill_blank("production_rank_score", row.get("opportunity_score") or "")
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
        investible = to_float(row.get("biotech_cohort_investible_flag"), 0.0) > 0.0
        core_veto = to_float(row.get("core_structural_veto_flag"), 0.0) > 0.0
        rank_veto = to_float(row.get("rank_quality_cap_vetoed"), 0.0) > 0.0
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
        row["portfolio_candidate_score"] = candidate_score if not is_blank(candidate_score) else ""
        row["portfolio_candidate_status"] = candidate_status
        row["portfolio_candidate_reason"] = candidate_reason
        fill_blank("calibration_eligible_flag", calibration_eligible)
        fill_blank("score_confidence", row.get("data_quality_confidence_multiplier") or "")
        fill_blank("avg_dollar_volume_60d", "")
        fill_blank("review_reason", review_reason)
        fill_blank("eligibility_reason", row.get("portfolio_candidate_reason") or "")
        fill_blank("universe_status", "live")
        fill_blank("historical_universe_source", "current_final_scoring_universe")
        fill_blank("price_start_date", "")
        fill_blank("price_end_date", "")
        fill_blank("terminal_date", "")
        fill_blank("historical_price_ticker", ticker)
        fill_blank("calibration_only", 0.0)
        fill_blank("recovery_type", "")
        fill_blank("equity_recovery", "")
        fill_blank("drop_otc_tape", 0.0)
        fill_blank("latest_price_date", row.get("price_data_asof_date") or "")
        fill_blank("source_snapshot_asof_date", asof)
        fill_blank("price_data_asof_date", "")
        fill_blank("feature_data_asof_date", asof)
        fill_blank("clinical_data_asof_date", "")
        fill_blank("financial_data_asof_date", "")
        fill_blank("short_interest_asof_date", "")
        fill_blank("institutional_data_asof_date", "")
        fill_blank("insider_data_asof_date", "")
        fill_blank("borrow_data_asof_date", "")
        fill_blank("calibration_cohort", row.get("biotech_primary_cohort") or "")
        fill_blank("calibration_status", "eligible" if to_float(calibration_eligible, 0.0) > 0.0 else "excluded")
        fill_blank(
            "calibration_status_reason",
            "eligible" if to_float(calibration_eligible, 0.0) > 0.0 else row.get("biotech_cohort_exclusion_reason") or "not_calibration_eligible",
        )
        fill_blank("native_score_field", native_score_field)
        row["native_score_value"] = native_score_value if not is_blank(native_score_value) else ""
        fill_blank("score_scale_min", 0.0)
        fill_blank("score_scale_max", 100.0)
        fill_blank("score_neutral_value", 50.0)
        row["score_zero_is_missing_flag"] = 1.0 if missing_score else 0.0
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
    calibration_tickers = load_calibration_tickers(config, config_path=config_path)
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
            dates = [item for item in dates if parse_date(item) is not None and parse_date(item).weekday() == 4]
        if int(args.max_dates or 0) > 0:
            dates = dates[: int(args.max_dates)]
        if not dates:
            raise RuntimeError("No historical score dates selected.")

        summary_rows: list[dict[str, Any]] = []
        invalid_dates: list[str] = []
        for asof in dates:
            output_dir = output_root / compact_date(asof)
            output_path = output_dir / output_csv_name
            action = "validated_existing"
            validation_path = output_path
            generated_temp_path: Path | None = None
            row_count = 0
            column_count = 0
            if not args.validate_only and (args.overwrite or not output_path.exists()):
                score_rows = load_score_rows(conn, asof, calibration_tickers=calibration_tickers)
                if not score_rows:
                    action = "missing_db_rows"
                else:
                    score_rows = prepare_score_rows_for_export(
                        score_rows,
                        export_module,
                        model_metadata=model_metadata,
                    )
                    output_dir.mkdir(parents=True, exist_ok=True)
                    generated_temp_path = output_path.with_name(output_path.name + ".tmp")
                    export_module.write_csv(generated_temp_path, score_rows)
                    validation_path = generated_temp_path
                    action = "generated_pending_validation"
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
            status = "PASS" if not failures else "FAIL"
            if failures:
                invalid_dates.append(asof)
                if generated_temp_path is not None and generated_temp_path.exists():
                    generated_temp_path.unlink()
                    action = "generated_invalid_rejected"
            elif generated_temp_path is not None:
                generated_temp_path.replace(output_path)
                validation_path = output_path
                action = "generated"
            summary_rows.append(
                {
                    "asof_date": asof,
                    "dated_folder": compact_date(asof),
                    "status": status,
                    "action": action,
                    "row_count": row_count,
                    "column_count": column_count,
                    "csv_path": str(output_path),
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
