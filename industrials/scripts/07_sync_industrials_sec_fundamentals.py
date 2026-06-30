#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_industrials_sec_fundamentals")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "sync_industrials_sec_fundamentals"
REPORT_FIELDS = [
    "ticker",
    "cik",
    "company_name",
    "country",
    "status",
    "reporting_profile",
    "reporting_standard",
    "latest_filing_date",
    "latest_form_type",
    "filing_count",
    "raw_fact_count",
    "mapped_fact_count",
    "review_reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync SEC submissions and companyfacts for an industrials model family.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to sync, e.g. defense.")
    parser.add_argument("--tickers", default="", help="Optional comma-separated ticker filter.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Optional cap for smoke tests.")
    parser.add_argument("--include-historical", action="store_true", help="Also sync non-current historical/delisted members.")
    parser.add_argument("--force", action="store_true", help="Ignore cached JSON and refetch.")
    parser.add_argument("--allow-partial", action="store_true", help="Finish with success when individual tickers fail.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--skip-source-registry", action="store_true")
    return parser.parse_args()


def parse_ticker_list(raw: object) -> list[str]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = normalize_ticker(value)
        if ticker and ticker not in seen:
            out.append(ticker)
            seen.add(ticker)
    return out


def parse_date(raw: object) -> str:
    text = str(raw or "").strip()[:10]
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def as_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_path(cache_dir: Path, *, source_id: str, cik: str) -> Path:
    return cache_dir / source_id / f"CIK{cik}.json"


def request_json(url: str, *, user_agent: str, timeout_sec: float, max_retries: int, sleep_sec: float) -> tuple[int, dict[str, Any], str]:
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Package 'requests' is required for SEC sync.") from exc

    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }
    last_status = 0
    last_text = ""
    for attempt in range(max(1, max_retries)):
        response = requests.get(url, headers=headers, timeout=timeout_sec)
        last_status = int(response.status_code)
        last_text = response.text
        if response.status_code == 200:
            return last_status, response.json(), last_text
        if response.status_code not in {429, 500, 502, 503, 504}:
            break
        time.sleep(sleep_sec * (attempt + 1))
    raise RuntimeError(f"SEC request failed status={last_status} url={url} body={last_text[:200]}")


def load_or_fetch_json(
    url: str,
    *,
    cache_file: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> tuple[int, dict[str, Any], str, str]:
    if cache_file.exists() and not force:
        text = cache_file.read_text(encoding="utf-8")
        return 200, json.loads(text), text, "cache"
    status, payload, text = request_json(url, user_agent=user_agent, timeout_sec=timeout_sec, max_retries=max_retries, sleep_sec=sleep_sec)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text, encoding="utf-8")
    return status, payload, text, "network"


def add_issue(
    conn: Any,
    *,
    severity: str,
    ticker: str,
    source_id: str,
    issue_type: str,
    detail: str,
) -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, ticker, company_id, source_id, issue_type, detail, now, now),
    )


def load_universe(conn: Any, *, model_family: str, ticker_filter: list[str], include_historical: bool) -> list[dict[str, Any]]:
    filter_sql = ""
    params: list[Any] = [model_family]
    if ticker_filter:
        filter_sql = f"AND c.ticker IN ({','.join('?' for _ in ticker_filter)})"
        params.extend(ticker_filter)
    if include_historical:
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.company_id, c.ticker, c.cik, c.company_name, c.country, c.currency, c.is_active
            FROM dim_company c
            JOIN dim_universe_membership m
              ON m.company_id = c.company_id
             AND m.model_family = ?
            WHERE 1 = 1
              {filter_sql}
            ORDER BY c.ticker
            """,
            tuple(params),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.company_id, c.ticker, c.cik, c.company_name, c.country, c.currency, c.is_active
            FROM dim_company c
            JOIN dim_industrials_taxonomy t
              ON t.company_id = c.company_id
             AND t.model_family = ?
            WHERE c.is_active = 1
              {filter_sql}
            ORDER BY c.ticker
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def record_raw_response(
    conn: Any,
    *,
    source_id: str,
    endpoint: str,
    status: int,
    payload_text: str,
    asof_date: str,
    ingestion_run_id: int,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, '{}', ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, endpoint, now, status, payload_hash(payload_text), asof_date, payload_text, ingestion_run_id, now),
    )


def upsert_filings(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    payload: dict[str, Any],
    allowed_forms: set[str],
    start_date: str,
) -> int:
    recent = (payload.get("filings") or {}).get("recent") or {}
    if not isinstance(recent, dict):
        return 0
    forms = recent.get("form") or []
    count = 0
    now = utc_now()
    keys = [
        "accessionNumber",
        "filingDate",
        "acceptanceDateTime",
        "reportDate",
        "form",
        "primaryDocument",
        "fy",
        "fp",
    ]
    for idx, form in enumerate(forms):
        form_type = str(form or "").strip().upper()
        if allowed_forms and form_type not in allowed_forms:
            continue
        values = {key: (recent.get(key) or []) for key in keys}
        accession = str(values["accessionNumber"][idx] or "").strip() if idx < len(values["accessionNumber"]) else ""
        filing_date = parse_date(values["filingDate"][idx] if idx < len(values["filingDate"]) else "")
        if not accession or not filing_date or (start_date and filing_date < start_date):
            continue
        accepted_at = str(values["acceptanceDateTime"][idx] or "").strip() if idx < len(values["acceptanceDateTime"]) else ""
        report_date = parse_date(values["reportDate"][idx] if idx < len(values["reportDate"]) else "")
        primary_document = str(values["primaryDocument"][idx] or "").strip() if idx < len(values["primaryDocument"]) else ""
        fiscal_year_raw = values["fy"][idx] if idx < len(values["fy"]) else None
        fiscal_year = int(fiscal_year_raw) if str(fiscal_year_raw or "").strip().isdigit() else None
        fiscal_period = str(values["fp"][idx] or "").strip() if idx < len(values["fp"]) else ""
        accession_nodash = accession.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{primary_document}" if primary_document else ""
        conn.execute(
            """
            INSERT INTO fact_sec_filing(
                ticker, cik, source_id, accession_number, form_type, filing_date,
                accepted_at, report_date, fiscal_year, fiscal_period, primary_document,
                filing_url, source_detail, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_submissions_recent', ?, ?)
            ON CONFLICT(ticker, accession_number, source_id) DO UPDATE SET
                cik = excluded.cik,
                form_type = excluded.form_type,
                filing_date = excluded.filing_date,
                accepted_at = excluded.accepted_at,
                report_date = excluded.report_date,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                primary_document = excluded.primary_document,
                filing_url = excluded.filing_url,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                cik,
                source_id,
                accession,
                form_type,
                filing_date,
                accepted_at,
                report_date,
                fiscal_year,
                fiscal_period,
                primary_document,
                filing_url,
                now,
                now,
            ),
        )
        count += 1
    return count


def load_concept_map(conn: Any) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT taxonomy, concept_name, canonical_metric, financial_statement,
               period_type, sign_policy, priority
        FROM dim_xbrl_concept_map
        WHERE active_flag = 1
        ORDER BY priority, canonical_metric
        """
    ).fetchall()
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault((str(row["taxonomy"]), str(row["concept_name"])), []).append(dict(row))
    return out


def make_fact_key(*parts: object) -> str:
    text = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_sign(value: float | None, sign_policy: str) -> float | None:
    if value is None:
        return None
    if sign_policy in {"positive_abs", "abs"}:
        return abs(value)
    if sign_policy == "negative_abs":
        return -abs(value)
    return value


def upsert_companyfacts(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    payload: dict[str, Any],
    concept_map: dict[tuple[str, str], list[dict[str, Any]]],
    start_date: str,
) -> tuple[int, int]:
    facts = payload.get("facts") or {}
    if not isinstance(facts, dict):
        return 0, 0
    now = utc_now()
    raw_count = 0
    mapped_count = 0
    for taxonomy, concepts in facts.items():
        if not isinstance(concepts, dict):
            continue
        taxonomy_text = str(taxonomy)
        for concept_name, concept_payload in concepts.items():
            if not isinstance(concept_payload, dict):
                continue
            units = concept_payload.get("units") or {}
            if not isinstance(units, dict):
                continue
            mappings = concept_map.get((taxonomy_text, str(concept_name)), [])
            for unit, fact_rows in units.items():
                if not isinstance(fact_rows, list):
                    continue
                for fact in fact_rows:
                    if not isinstance(fact, dict):
                        continue
                    period_end = parse_date(fact.get("end"))
                    filing_date = parse_date(fact.get("filed"))
                    if not period_end or (start_date and filing_date and filing_date < start_date):
                        continue
                    value = as_float(fact.get("val"))
                    accession = str(fact.get("accn") or "").strip()
                    form_type = str(fact.get("form") or "").strip().upper()
                    fiscal_year_raw = fact.get("fy")
                    fiscal_year = int(fiscal_year_raw) if str(fiscal_year_raw or "").strip().isdigit() else None
                    fiscal_period = str(fact.get("fp") or "").strip()
                    period_start = parse_date(fact.get("start"))
                    frame = str(fact.get("frame") or "").strip()
                    fact_key = make_fact_key(ticker, source_id, accession, taxonomy_text, concept_name, unit, period_start, period_end, frame)
                    conn.execute(
                        """
                        INSERT INTO fact_sec_xbrl_fact_raw(
                            fact_key, ticker, cik, source_id, accession_number, form_type,
                            filing_date, fiscal_year, fiscal_period, period_start, period_end,
                            frame, taxonomy, concept_name, unit, raw_value, decimals,
                            source_detail, payload_json, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_companyfacts', ?, ?, ?)
                        ON CONFLICT(fact_key) DO UPDATE SET
                            filing_date = excluded.filing_date,
                            fiscal_year = excluded.fiscal_year,
                            fiscal_period = excluded.fiscal_period,
                            raw_value = excluded.raw_value,
                            decimals = excluded.decimals,
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            fact_key,
                            ticker,
                            cik,
                            source_id,
                            accession,
                            form_type,
                            filing_date,
                            fiscal_year,
                            fiscal_period,
                            period_start,
                            period_end,
                            frame,
                            taxonomy_text,
                            str(concept_name),
                            str(unit),
                            value,
                            str(fact.get("decimals") or ""),
                            compact_json(fact),
                            now,
                            now,
                        ),
                    )
                    raw_row = conn.execute("SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw WHERE fact_key = ?", (fact_key,)).fetchone()
                    raw_fact_id = int(raw_row["raw_fact_id"]) if raw_row is not None else None
                    raw_count += 1
                    for mapping in mappings:
                        mapped_value = apply_sign(value, str(mapping["sign_policy"]))
                        conn.execute(
                            """
                            INSERT INTO fact_sec_xbrl_fact(
                                raw_fact_id, ticker, cik, source_id, accession_number,
                                form_type, filing_date, fiscal_year, fiscal_period,
                                period_start, period_end, frame, taxonomy, concept_name,
                                canonical_metric, financial_statement, period_type, unit,
                                value, sign_policy, source_priority, source_detail,
                                created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_companyfacts_mapped', ?, ?)
                            ON CONFLICT(ticker, source_id, accession_number, taxonomy, concept_name, canonical_metric, unit, period_start, period_end, frame)
                            DO UPDATE SET
                                raw_fact_id = excluded.raw_fact_id,
                                filing_date = excluded.filing_date,
                                fiscal_year = excluded.fiscal_year,
                                fiscal_period = excluded.fiscal_period,
                                value = excluded.value,
                                sign_policy = excluded.sign_policy,
                                source_priority = excluded.source_priority,
                                updated_at = excluded.updated_at
                            """,
                            (
                                raw_fact_id,
                                ticker,
                                cik,
                                source_id,
                                accession,
                                form_type,
                                filing_date,
                                fiscal_year,
                                fiscal_period,
                                period_start,
                                period_end,
                                frame,
                                taxonomy_text,
                                str(concept_name),
                                str(mapping["canonical_metric"]),
                                str(mapping["financial_statement"]),
                                str(mapping["period_type"]),
                                str(unit),
                                mapped_value,
                                str(mapping["sign_policy"]),
                                int(mapping["priority"]),
                                now,
                                now,
                            ),
                        )
                        mapped_count += 1
    return raw_count, mapped_count


def classify_reporting_profile(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    country: str,
    model_family: str,
    source_id: str,
) -> dict[str, Any]:
    latest = conn.execute(
        """
        SELECT accession_number, filing_date, form_type
        FROM fact_sec_filing
        WHERE ticker = ?
        ORDER BY filing_date DESC, accession_number DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    tax_rows = conn.execute(
        """
        SELECT taxonomy, COUNT(*) AS n
        FROM fact_sec_xbrl_fact
        WHERE ticker = ?
        GROUP BY taxonomy
        """,
        (ticker,),
    ).fetchall()
    metrics = {
        str(row["canonical_metric"])
        for row in conn.execute(
            "SELECT DISTINCT canonical_metric FROM fact_sec_xbrl_fact WHERE ticker = ?",
            (ticker,),
        ).fetchall()
    }
    taxonomies = {str(row["taxonomy"]): int(row["n"] or 0) for row in tax_rows}
    has_core = {"revenue", "assets"} <= metrics
    latest_form = str(latest["form_type"]) if latest is not None else ""
    latest_filing = str(latest["filing_date"]) if latest is not None else ""
    latest_accession = str(latest["accession_number"]) if latest is not None else ""
    country_text = str(country or "").strip()

    if has_core and taxonomies.get("us-gaap", 0) > 0:
        profile = "SEC_XBRL_US_GAAP"
        standard = "US_GAAP"
        primary_taxonomy = "us-gaap"
        fallback = "none"
        confidence = 0.9
        usable_xbrl = 1
        reason = ""
    elif has_core and taxonomies.get("ifrs-full", 0) > 0:
        profile = "SEC_XBRL_IFRS"
        standard = "IFRS"
        primary_taxonomy = "ifrs-full"
        fallback = "none"
        confidence = 0.75
        usable_xbrl = 1
        reason = ""
    elif latest_form in {"20-F", "40-F", "6-K"}:
        profile = "SEC_20F_METADATA_ONLY"
        standard = "foreign_private_issuer_metadata"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "neutral_low_confidence"
        confidence = 0.35
        usable_xbrl = 0
        reason = f"foreign_issuer_without_mapped_core_xbrl form={latest_form}"
    elif country_text and country_text.upper() not in {"UNITED STATES", "USA", "US"}:
        profile = "FOREIGN_NEUTRAL_LOW_CONFIDENCE"
        standard = "foreign_no_sec_xbrl"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "neutral_low_confidence"
        confidence = 0.25
        usable_xbrl = 0
        reason = "foreign_issuer_no_usable_sec_xbrl"
    elif latest is None:
        profile = "NO_FINANCIALS_REVIEW"
        standard = "unavailable"
        primary_taxonomy = ""
        fallback = "review"
        confidence = 0.0
        usable_xbrl = 0
        reason = "no_sec_filings_loaded"
    else:
        profile = "NO_FINANCIALS_REVIEW"
        standard = "sec_metadata_no_mapped_core_xbrl"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "review"
        confidence = 0.2
        usable_xbrl = 0
        reason = "sec_filing_loaded_without_mapped_core_xbrl"

    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_issuer_reporting_profile(
            ticker, model_family, cik, country, reporting_profile, reporting_standard,
            primary_taxonomy, latest_filing_date, latest_form_type, latest_accession_number,
            fallback_status, financial_confidence, usable_xbrl_flag, source_id,
            review_reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family) DO UPDATE SET
            cik = excluded.cik,
            country = excluded.country,
            reporting_profile = excluded.reporting_profile,
            reporting_standard = excluded.reporting_standard,
            primary_taxonomy = excluded.primary_taxonomy,
            latest_filing_date = excluded.latest_filing_date,
            latest_form_type = excluded.latest_form_type,
            latest_accession_number = excluded.latest_accession_number,
            fallback_status = excluded.fallback_status,
            financial_confidence = excluded.financial_confidence,
            usable_xbrl_flag = excluded.usable_xbrl_flag,
            source_id = excluded.source_id,
            review_reason = excluded.review_reason,
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            model_family,
            cik,
            country_text,
            profile,
            standard,
            primary_taxonomy,
            latest_filing,
            latest_form,
            latest_accession,
            fallback,
            confidence,
            usable_xbrl,
            source_id,
            reason,
            now,
            now,
        ),
    )
    return {
        "reporting_profile": profile,
        "reporting_standard": standard,
        "latest_filing_date": latest_filing,
        "latest_form_type": latest_form,
        "financial_confidence": confidence,
        "review_reason": reason,
    }


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def source_status(conn: Any, source_id: str) -> str:
    row = conn.execute("SELECT status FROM source_registry WHERE source_id = ?", (source_id,)).fetchone()
    return str(row["status"]) if row is not None else ""


def start_ingestion_run(conn: Any, *, source_id: str) -> int:
    now = utc_now()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO ingestion_runs(source_id, started_at, status, created_at)
            VALUES (?, ?, 'running', ?)
            """,
            (source_id, now, now),
        )
    if cur.lastrowid is None:
        raise RuntimeError(f"Failed to create ingestion run for {source_id}")
    return int(cur.lastrowid)


def finish_ingestion_run(conn: Any, *, ingestion_run_id: int, status: str, request_count: int, row_count: int, message: str) -> None:
    with conn:
        conn.execute(
            """
            UPDATE ingestion_runs
            SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
            WHERE ingestion_run_id = ?
            """,
            (utc_now(), status, int(request_count), int(row_count), str(message or ""), int(ingestion_run_id)),
        )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    submissions_source_id = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions") or "sec_submissions")
    companyfacts_source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    submissions_template = str(cfg_get(config, "sec_fundamentals.submissions_url_template") or "https://data.sec.gov/submissions/CIK{cik}.json")
    companyfacts_template = str(cfg_get(config, "sec_fundamentals.companyfacts_url_template") or "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    user_agent = str(cfg_get(config, "sec_fundamentals.user_agent", "") or "")
    if "@" not in user_agent:
        LOGGER.warning("SEC user agent should include contact information; current value=%r", user_agent)
    timeout_sec = float(cfg_get(config, "sec_fundamentals.timeout_sec", 30.0))
    max_retries = int(cfg_get(config, "sec_fundamentals.max_retries", 3))
    sleep_sec = float(cfg_get(config, "sec_fundamentals.request_sleep_sec", 0.12))
    start_date = parse_date(cfg_get(config, "sec_fundamentals.start_date", "2015-01-01"))
    allowed_forms = {str(form).upper() for form in (cfg_get(config, "sec_fundamentals.forms", []) or [])}
    cache_dir = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "sec_fundamentals.sync_output_csv"), base_dir=base_dir)
    include_historical = bool(args.include_historical or cfg_get(config, "sec_fundamentals.include_historical_members", False))
    ticker_filter = parse_ticker_list(args.tickers)

    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)))) as conn:
        init_db(conn)
        if not args.skip_source_registry:
            registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
            with conn:
                upsert_source_registry(conn, load_source_registry(registry_path))
        if source_status(conn, submissions_source_id) != "active":
            raise ValueError(f"Source {submissions_source_id} must be active in source_registry.")
        if source_status(conn, companyfacts_source_id) != "active":
            raise ValueError(f"Source {companyfacts_source_id} must be active in source_registry.")

        tickers = load_universe(conn, model_family=model_family, ticker_filter=ticker_filter, include_historical=include_historical)
        if args.max_tickers > 0:
            tickers = tickers[: args.max_tickers]
        if not tickers:
            raise ValueError(f"No tickers found for model_family={model_family}")

        concept_map = load_concept_map(conn)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        submissions_run_id = start_ingestion_run(conn, source_id=submissions_source_id)
        companyfacts_run_id = start_ingestion_run(conn, source_id=companyfacts_source_id)
        report_rows: list[dict[str, Any]] = []
        failures: list[str] = []
        submissions_requests = 0
        companyfacts_requests = 0
        try:
            with conn:
                if ticker_filter:
                    placeholders = ",".join("?" for _ in ticker_filter)
                    conn.execute(f"DELETE FROM data_quality_issues WHERE stage = ? AND ticker IN ({placeholders})", (RUN_TYPE, *ticker_filter))
                else:
                    conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", (RUN_TYPE,))

            for item in tickers:
                ticker = normalize_ticker(item.get("ticker"))
                cik = normalize_cik(item.get("cik"))
                company_name = str(item.get("company_name") or "")
                country = str(item.get("country") or "")
                filing_count = 0
                raw_count = 0
                mapped_count = 0
                status = "success"
                review_reason = ""
                if not ticker:
                    continue
                try:
                    if not cik:
                        status = "review"
                        review_reason = "missing_cik"
                        with conn:
                            add_issue(conn, severity="error", ticker=ticker, source_id=submissions_source_id, issue_type="missing_cik", detail="Ticker has no CIK; SEC financial sync skipped.")
                            profile = classify_reporting_profile(conn, ticker=ticker, cik="", country=country, model_family=model_family, source_id=submissions_source_id)
                    else:
                        submissions_url = submissions_template.format(cik=cik)
                        submissions_cache = cache_path(cache_dir, source_id=submissions_source_id, cik=cik)
                        status_code, submissions_payload, submissions_text, _ = load_or_fetch_json(
                            submissions_url,
                            cache_file=submissions_cache,
                            force=args.force,
                            user_agent=user_agent,
                            timeout_sec=timeout_sec,
                            max_retries=max_retries,
                            sleep_sec=sleep_sec,
                        )
                        with conn:
                            submissions_requests += 1
                            record_raw_response(
                                conn,
                                source_id=submissions_source_id,
                                endpoint=submissions_url,
                                status=status_code,
                                payload_text=submissions_text,
                                asof_date=datetime.utcnow().date().isoformat(),
                                ingestion_run_id=submissions_run_id,
                            )
                            filing_count = upsert_filings(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                source_id=submissions_source_id,
                                payload=submissions_payload,
                                allowed_forms=allowed_forms,
                                start_date=start_date,
                            )

                        companyfacts_url = companyfacts_template.format(cik=cik)
                        companyfacts_cache = cache_path(cache_dir, source_id=companyfacts_source_id, cik=cik)
                        status_code, companyfacts_payload, companyfacts_text, _ = load_or_fetch_json(
                            companyfacts_url,
                            cache_file=companyfacts_cache,
                            force=args.force,
                            user_agent=user_agent,
                            timeout_sec=timeout_sec,
                            max_retries=max_retries,
                            sleep_sec=sleep_sec,
                        )
                        with conn:
                            companyfacts_requests += 1
                            record_raw_response(
                                conn,
                                source_id=companyfacts_source_id,
                                endpoint=companyfacts_url,
                                status=status_code,
                                payload_text=companyfacts_text,
                                asof_date=datetime.utcnow().date().isoformat(),
                                ingestion_run_id=companyfacts_run_id,
                            )
                            raw_count, mapped_count = upsert_companyfacts(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                source_id=companyfacts_source_id,
                                payload=companyfacts_payload,
                                concept_map=concept_map,
                                start_date=start_date,
                            )
                            profile = classify_reporting_profile(conn, ticker=ticker, cik=cik, country=country, model_family=model_family, source_id=companyfacts_source_id)
                            if profile["review_reason"]:
                                add_issue(
                                    conn,
                                    severity="warning",
                                    ticker=ticker,
                                    source_id=companyfacts_source_id,
                                    issue_type="financial_reporting_profile_review",
                                    detail=str(profile["review_reason"]),
                                )
                                status = "review"
                                review_reason = str(profile["review_reason"])
                        time.sleep(sleep_sec)
                except Exception as exc:
                    status = "failed"
                    review_reason = f"{type(exc).__name__}: {exc}"
                    failures.append(f"{ticker}: {review_reason}")
                    with conn:
                        add_issue(conn, severity="error", ticker=ticker, source_id=companyfacts_source_id, issue_type="sec_sync_failed", detail=review_reason)
                        profile = classify_reporting_profile(conn, ticker=ticker, cik=cik, country=country, model_family=model_family, source_id=companyfacts_source_id)
                    if not args.allow_partial:
                        raise

                report_rows.append(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "company_name": company_name,
                        "country": country,
                        "status": status,
                        "reporting_profile": profile.get("reporting_profile", ""),
                        "reporting_standard": profile.get("reporting_standard", ""),
                        "latest_filing_date": profile.get("latest_filing_date", ""),
                        "latest_form_type": profile.get("latest_form_type", ""),
                        "filing_count": filing_count,
                        "raw_fact_count": raw_count,
                        "mapped_fact_count": mapped_count,
                        "review_reason": review_reason or profile.get("review_reason", ""),
                    }
                )

            write_report(output_csv, report_rows)
            status = "success_with_failures" if failures else "success"
            if failures and not args.allow_partial:
                status = "failed"
            finish_run(conn, run_id=run_id, status=status, row_count=len(report_rows), message=f"rows={len(report_rows)} failures={len(failures)} output={output_csv}")
            finish_ingestion_run(
                conn,
                ingestion_run_id=submissions_run_id,
                status=status,
                request_count=submissions_requests,
                row_count=sum(int(row.get("filing_count") or 0) for row in report_rows),
                message=f"tickers={len(report_rows)}",
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=companyfacts_run_id,
                status=status,
                request_count=companyfacts_requests,
                row_count=sum(int(row.get("mapped_fact_count") or 0) for row in report_rows),
                message=f"tickers={len(report_rows)}",
            )
            LOGGER.info("Wrote SEC fundamentals coverage report: %s", output_csv)
            LOGGER.info("SEC fundamentals sync complete: rows=%d failures=%d", len(report_rows), len(failures))
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=len(report_rows), message=f"{type(exc).__name__}: {exc}")
            finish_ingestion_run(
                conn,
                ingestion_run_id=submissions_run_id,
                status="failed",
                request_count=submissions_requests,
                row_count=0,
                message=f"{type(exc).__name__}: {exc}",
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=companyfacts_run_id,
                status="failed",
                request_count=companyfacts_requests,
                row_count=0,
                message=f"{type(exc).__name__}: {exc}",
            )
            raise


if __name__ == "__main__":
    main()
