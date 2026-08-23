#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402


LOGGER = logging.getLogger("sync_med_device_clinical_trials")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_ID = "clinicaltrials_v2"
NCT_RE = re.compile(r"^NCT\d{8}$")
DECISIONS = {"include", "exclude"}
REQUIRED_COLUMNS = {
    "ticker",
    "nct_id",
    "decision",
    "relationship_type",
    "expected_sponsor",
    "required_terms_json",
    "valid_from",
    "valid_to",
    "reviewed_at",
    "confidence",
    "notes",
}


@dataclass(frozen=True)
class TrialReview:
    ticker: str
    nct_id: str
    decision: str
    relationship_type: str
    expected_sponsor: str
    required_terms: tuple[str, ...]
    valid_from: str
    valid_to: str
    reviewed_at: str
    confidence: float
    notes: str


@dataclass(frozen=True)
class ParsedStudy:
    nct_id: str
    brief_title: str
    overall_status: str
    study_type: str
    enrollment_count: int | None
    start_date: str
    primary_completion_date: str
    completion_date: str
    first_post_date: str
    last_update_post_date: str
    lead_sponsor: str
    interventions: tuple[dict[str, Any], ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync governed ClinicalTrials.gov records without ticker/product-name collisions."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Audit snapshot date, YYYY-MM-DD; defaults to UTC today.")
    parser.add_argument("--review-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--tickers", default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


def normalize_text(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


def parse_iso_date(value: object, *, field_name: str, allow_blank: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_blank:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}: {text!r}") from exc


def parse_required_terms(raw: object, *, row_number: int) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw or ""))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Row {row_number}: required_terms_json must be a JSON array") from exc
    if not isinstance(value, list) or not value:
        raise ValueError(f"Row {row_number}: required_terms_json must be a non-empty JSON array")
    terms = tuple(normalize_text(item) for item in value if normalize_text(item))
    if not terms:
        raise ValueError(f"Row {row_number}: required_terms_json has no usable terms")
    return terms


def load_reviews(path: Path) -> list[TrialReview]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Clinical-trial review CSV missing columns: {sorted(missing)}")
        reviews: list[TrialReview] = []
        seen: set[tuple[str, str]] = set()
        included_ncts: dict[str, str] = {}
        for row_number, row in enumerate(reader, start=2):
            ticker = str(row.get("ticker") or "").strip().upper()
            nct_id = str(row.get("nct_id") or "").strip().upper()
            decision = str(row.get("decision") or "").strip().lower()
            relationship_type = str(row.get("relationship_type") or "").strip().lower()
            if not ticker or not NCT_RE.fullmatch(nct_id):
                raise ValueError(f"Row {row_number}: invalid ticker/NCT identity: {ticker!r}/{nct_id!r}")
            if decision not in DECISIONS or not relationship_type:
                raise ValueError(f"Row {row_number}: invalid decision or relationship_type")
            key = (ticker, nct_id)
            if key in seen:
                raise ValueError(f"Row {row_number}: duplicate review key {key}")
            seen.add(key)
            if decision == "include":
                prior_ticker = included_ncts.get(nct_id)
                if prior_ticker and prior_ticker != ticker:
                    raise ValueError(f"NCT {nct_id} cannot be included for both {prior_ticker} and {ticker}")
                included_ncts[nct_id] = ticker
            valid_from = parse_iso_date(row.get("valid_from"), field_name="valid_from")
            valid_to = parse_iso_date(row.get("valid_to"), field_name="valid_to", allow_blank=True)
            reviewed_at = parse_iso_date(row.get("reviewed_at"), field_name="reviewed_at")
            if valid_to and valid_to < valid_from:
                raise ValueError(f"Row {row_number}: valid_to precedes valid_from")
            confidence = float(row.get("confidence") or 0.0)
            if not 0.0 <= confidence <= 1.0:
                raise ValueError(f"Row {row_number}: confidence must be in [0, 1]")
            expected_sponsor = str(row.get("expected_sponsor") or "").strip()
            if decision == "include" and not expected_sponsor:
                raise ValueError(f"Row {row_number}: included trials require expected_sponsor")
            reviews.append(
                TrialReview(
                    ticker=ticker,
                    nct_id=nct_id,
                    decision=decision,
                    relationship_type=relationship_type,
                    expected_sponsor=expected_sponsor,
                    required_terms=parse_required_terms(row.get("required_terms_json"), row_number=row_number),
                    valid_from=valid_from,
                    valid_to=valid_to,
                    reviewed_at=reviewed_at,
                    confidence=confidence,
                    notes=str(row.get("notes") or "").strip(),
                )
            )
    return reviews


def review_is_effective(review: TrialReview, asof: str) -> bool:
    return review.valid_from <= asof and review.reviewed_at <= asof and (not review.valid_to or asof <= review.valid_to)


def date_struct(module: dict[str, Any], key: str) -> str:
    value = module.get(key)
    if isinstance(value, dict):
        return str(value.get("date") or "").strip()
    return str(value or "").strip()


def parse_study(payload: dict[str, Any]) -> ParsedStudy:
    protocol = payload.get("protocolSection")
    if not isinstance(protocol, dict):
        raise ValueError("ClinicalTrials.gov payload has no protocolSection")
    identity = protocol.get("identificationModule") or {}
    status = protocol.get("statusModule") or {}
    design = protocol.get("designModule") or {}
    sponsor_module = protocol.get("sponsorCollaboratorsModule") or {}
    arms = protocol.get("armsInterventionsModule") or {}
    if not all(isinstance(item, dict) for item in (identity, status, design, sponsor_module, arms)):
        raise ValueError("ClinicalTrials.gov payload contains malformed protocol modules")
    nct_id = str(identity.get("nctId") or "").strip().upper()
    if not NCT_RE.fullmatch(nct_id):
        raise ValueError(f"ClinicalTrials.gov payload has invalid nctId: {nct_id!r}")
    lead = sponsor_module.get("leadSponsor") or {}
    if not isinstance(lead, dict):
        lead = {}
    interventions_raw = arms.get("interventions") or []
    interventions = tuple(item for item in interventions_raw if isinstance(item, dict))
    enrollment = design.get("enrollmentInfo") or {}
    enrollment_count: int | None = None
    if isinstance(enrollment, dict) and enrollment.get("count") is not None:
        try:
            enrollment_count = int(enrollment["count"])
        except (TypeError, ValueError):
            enrollment_count = None
    return ParsedStudy(
        nct_id=nct_id,
        brief_title=str(identity.get("briefTitle") or "").strip(),
        overall_status=str(status.get("overallStatus") or "").strip(),
        study_type=str(design.get("studyType") or "").strip(),
        enrollment_count=enrollment_count,
        start_date=date_struct(status, "startDateStruct"),
        primary_completion_date=date_struct(status, "primaryCompletionDateStruct"),
        completion_date=date_struct(status, "completionDateStruct"),
        first_post_date=date_struct(status, "studyFirstPostDateStruct"),
        last_update_post_date=date_struct(status, "lastUpdatePostDateStruct"),
        lead_sponsor=str(lead.get("name") or "").strip(),
        interventions=interventions,
    )


def searchable_study_text(payload: dict[str, Any], parsed: ParsedStudy) -> str:
    material = [parsed.brief_title, parsed.lead_sponsor, json.dumps(parsed.interventions, ensure_ascii=True)]
    protocol = payload.get("protocolSection") or {}
    if isinstance(protocol, dict):
        material.append(json.dumps(protocol.get("descriptionModule") or {}, ensure_ascii=True))
    return normalize_text(" ".join(material))


def validate_review(review: TrialReview, payload: dict[str, Any], parsed: ParsedStudy) -> list[str]:
    issues: list[str] = []
    if parsed.nct_id != review.nct_id:
        issues.append(f"nct_mismatch:{parsed.nct_id}")
    if review.expected_sponsor and normalize_text(parsed.lead_sponsor) != normalize_text(review.expected_sponsor):
        issues.append(f"sponsor_mismatch:{parsed.lead_sponsor}")
    searchable = searchable_study_text(payload, parsed)
    missing_terms = [term for term in review.required_terms if term not in searchable]
    if missing_terms:
        issues.append("missing_terms:" + "|".join(missing_terms))
    if review.decision == "include" and not parsed.overall_status:
        issues.append("missing_overall_status")
    return issues


def build_session(user_agent: str, retries: int) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=max(0, retries),
        connect=max(0, retries),
        read=max(0, retries),
        status=max(0, retries),
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        backoff_factor=0.5,
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"Accept": "application/json", "User-Agent": user_agent})
    return session


def store_raw_response(
    conn: Any,
    *,
    ingestion_run_id: int,
    endpoint: str,
    nct_id: str,
    status_code: int,
    payload_text: str,
    asof: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SOURCE_ID,
            endpoint,
            json.dumps({"nct_id": nct_id}, sort_keys=True, separators=(",", ":")),
            now,
            status_code,
            hashlib.sha256(payload_text.encode("utf-8", errors="replace")).hexdigest(),
            asof,
            payload_text,
            ingestion_run_id,
            now,
        ),
    )


def upsert_trial(
    conn: Any,
    *,
    company_id: int,
    review: TrialReview,
    parsed: ParsedStudy,
    payload: dict[str, Any],
    asof: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO fact_clinical_trial_status(
            nct_id, company_id, brief_title, overall_status, study_type, enrollment_count,
            start_date, primary_completion_date, completion_date, last_update_post_date,
            lead_sponsor, interventions_json, relationship_type, mapping_confidence,
            mapping_method, valid_from, valid_to, reviewed_at, source_snapshot_asof_date,
            source_id, payload_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET
            brief_title = excluded.brief_title,
            overall_status = excluded.overall_status,
            study_type = excluded.study_type,
            enrollment_count = excluded.enrollment_count,
            start_date = excluded.start_date,
            primary_completion_date = excluded.primary_completion_date,
            completion_date = excluded.completion_date,
            last_update_post_date = excluded.last_update_post_date,
            lead_sponsor = excluded.lead_sponsor,
            interventions_json = excluded.interventions_json,
            relationship_type = excluded.relationship_type,
            mapping_confidence = excluded.mapping_confidence,
            mapping_method = excluded.mapping_method,
            valid_from = excluded.valid_from,
            valid_to = excluded.valid_to,
            reviewed_at = excluded.reviewed_at,
            source_snapshot_asof_date = excluded.source_snapshot_asof_date,
            source_id = excluded.source_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            parsed.nct_id,
            company_id,
            parsed.brief_title,
            parsed.overall_status,
            parsed.study_type,
            parsed.enrollment_count,
            parsed.start_date,
            parsed.primary_completion_date,
            parsed.completion_date,
            parsed.last_update_post_date,
            parsed.lead_sponsor,
            json.dumps(parsed.interventions, ensure_ascii=True, sort_keys=True),
            review.relationship_type,
            review.confidence,
            "manual_trial_review",
            review.valid_from,
            review.valid_to or None,
            review.reviewed_at,
            asof,
            SOURCE_ID,
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            now,
            now,
        ),
    )


def write_output(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "asof_date",
        "ticker",
        "nct_id",
        "decision",
        "relationship_type",
        "validation_status",
        "validation_reason",
        "observed_sponsor",
        "observed_status",
        "brief_title",
        "first_post_date",
        "last_update_post_date",
        "mapping_confidence",
        "reviewed_at",
        "notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = parse_iso_date(args.asof or datetime.now(timezone.utc).date().isoformat(), field_name="asof")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    review_path = (
        args.review_csv.expanduser().resolve()
        if args.review_csv
        else resolve_path(
            cfg_get(config, "clinical_trial_ingestion.mapping_reviews_csv", "data/clinical_trial_mapping_reviews.csv"),
            base_dir=base_dir,
        )
    )
    output_path = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "clinical_trial_ingestion.output_csv",
                "../output/med_devices_reports/med_device_clinical_trial_audit.csv",
            ),
            base_dir=base_dir,
        )
    )
    base_url = str(cfg_get(config, "clinical_trial_ingestion.base_url", "https://clinicaltrials.gov/api/v2/studies"))
    timeout_sec = float(cfg_get(config, "clinical_trial_ingestion.timeout_sec", 30.0))
    retries = int(cfg_get(config, "clinical_trial_ingestion.max_retries", 3))
    user_agent = str(
        cfg_get(
            config,
            "clinical_trial_ingestion.user_agent",
            cfg_get(config, "fda_core_ingestion.user_agent", "med-devices-research/1.0"),
        )
    )
    requested = {item.strip().upper() for item in str(args.tickers or "").split(",") if item.strip()}
    reviews = [row for row in load_reviews(review_path) if not requested or row.ticker in requested]
    if requested.difference({row.ticker for row in reviews}):
        raise ValueError(f"Requested tickers absent from clinical-trial reviews: {sorted(requested)}")
    effective = [row for row in reviews if review_is_effective(row, asof)]
    output_rows: list[dict[str, Any]] = []
    failures = 0
    upserted = 0
    excluded_links_removed = 0
    request_count = 0
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        init_db(conn)
        registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
        upsert_source_registry(conn, load_source_registry(registry_path))
        run_id = start_run(conn, run_type="sync_med_device_clinical_trials", input_path=review_path)
        now = utc_now()
        cursor = conn.execute(
            "INSERT INTO ingestion_runs(source_id, started_at, status, created_at) VALUES (?, ?, 'running', ?)",
            (SOURCE_ID, now, now),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Could not create ClinicalTrials.gov ingestion run")
        ingestion_run_id = int(cursor.lastrowid)
        company_rows = conn.execute(
            "SELECT company_id, ticker FROM dim_company WHERE ticker IN ({})".format(
                ",".join("?" for _ in {row.ticker for row in effective}) or "NULL"
            ),
            tuple(sorted({row.ticker for row in effective})),
        ).fetchall()
        company_ids = {str(row["ticker"]): int(row["company_id"]) for row in company_rows}
        with build_session(user_agent, retries) as session:
            for review in reviews:
                result: dict[str, Any] = {
                    "asof_date": asof,
                    "ticker": review.ticker,
                    "nct_id": review.nct_id,
                    "decision": review.decision,
                    "relationship_type": review.relationship_type,
                    "mapping_confidence": review.confidence,
                    "reviewed_at": review.reviewed_at,
                    "notes": review.notes,
                }
                if not review_is_effective(review, asof):
                    result.update(validation_status="not_effective_asof", validation_reason="manual_review_not_effective")
                    output_rows.append(result)
                    continue
                company_id = company_ids.get(review.ticker)
                if company_id is None:
                    failures += 1
                    result.update(validation_status="failed", validation_reason="ticker_missing_from_dim_company")
                    output_rows.append(result)
                    continue
                endpoint = f"{base_url.rstrip('/')}/{review.nct_id}"
                request_count += 1
                try:
                    response = session.get(endpoint, timeout=timeout_sec)
                    payload_text = response.text
                    store_raw_response(
                        conn,
                        ingestion_run_id=ingestion_run_id,
                        endpoint=endpoint,
                        nct_id=review.nct_id,
                        status_code=int(response.status_code),
                        payload_text=payload_text,
                        asof=asof,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("ClinicalTrials.gov response is not an object")
                    parsed = parse_study(payload)
                    issues = validate_review(review, payload, parsed)
                    result.update(
                        observed_sponsor=parsed.lead_sponsor,
                        observed_status=parsed.overall_status,
                        brief_title=parsed.brief_title,
                        first_post_date=parsed.first_post_date,
                        last_update_post_date=parsed.last_update_post_date,
                    )
                    if issues:
                        failures += 1
                        result.update(validation_status="failed", validation_reason=";".join(issues))
                    elif review.decision == "include":
                        upsert_trial(
                            conn,
                            company_id=company_id,
                            review=review,
                            parsed=parsed,
                            payload=payload,
                            asof=asof,
                        )
                        upserted += 1
                        result.update(validation_status="validated_include", validation_reason="ok")
                    else:
                        removed = conn.execute(
                            "DELETE FROM fact_clinical_trial_status WHERE nct_id = ? AND company_id = ?",
                            (review.nct_id, company_id),
                        ).rowcount
                        excluded_links_removed += max(0, int(removed))
                        result.update(validation_status="validated_exclude", validation_reason="identity_collision_blocked")
                except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                    failures += 1
                    result.update(validation_status="failed", validation_reason=f"{type(exc).__name__}:{exc}")
                output_rows.append(result)
        ingestion_status = "success" if failures == 0 else "partial"
        message = (
            f"asof={asof} reviewed={len(reviews)} effective={len(effective)} requests={request_count} "
            f"upserted={upserted} excluded_links_removed={excluded_links_removed} failures={failures}"
        )
        conn.execute(
            """
            UPDATE ingestion_runs
            SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
            WHERE ingestion_run_id = ?
            """,
            (utc_now(), ingestion_status, request_count, upserted, message, ingestion_run_id),
        )
        finish_run(conn, run_id=run_id, status=ingestion_status, row_count=upserted, message=message)
    write_output(output_path, output_rows)
    LOGGER.info(
        "Clinical-trial audit complete: asof=%s upserted=%d exclusions_removed=%d failures=%d output=%s",
        asof,
        upserted,
        excluded_links_removed,
        failures,
        output_path,
    )
    if failures and not args.allow_partial:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
