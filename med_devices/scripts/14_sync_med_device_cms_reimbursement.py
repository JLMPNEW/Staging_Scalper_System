#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests  # type: ignore[reportMissingModuleSource]


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from med_devices.core.text_norm import normalize_code  # noqa: E402


LOGGER = logging.getLogger("sync_med_device_cms_reimbursement")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_COVERAGE_SOURCE_ID = "cms_coverage_api"
DEFAULT_PAYMENT_SOURCE_ID = "cms_payment_files"
DEFAULT_BASE_URL = "https://api.coverage.cms.gov"
FIELDNAMES = [
    "dataset",
    "source_id",
    "status",
    "requests",
    "records_seen",
    "canonical_rows_upserted",
    "review_reason",
]
CODE_TOKEN_RE = re.compile(r"\b(?:[A-Z]\d{4}|\d{5})\b")


@dataclass(frozen=True)
class CoverageEndpoint:
    name: str
    path: str
    enabled: bool
    policy_type: str
    params: dict[str, Any]
    record_path: str
    page_param: str
    page_size_param: str
    page_start: int
    page_size: int
    max_pages: int


@dataclass(frozen=True)
class PaymentFile:
    name: str
    path: str
    enabled: bool
    payment_system: str
    code_type: str
    effective_date: str
    source_id: str
    replace_existing: bool
    url: str = ""
    zip_members: tuple[str, ...] = ()
    header_required_columns: tuple[str, ...] = ()
    excluded_rate_columns: tuple[str, ...] = ()
    refresh_download: bool = False


@dataclass(frozen=True)
class DetailEndpoint:
    name: str
    policy_type: str
    path: str
    id_param: str
    version_param: str
    record_path: str
    enabled: bool
    max_documents: int


@dataclass(frozen=True)
class CmsReimbursementPolicy:
    coverage_source_id: str
    payment_source_id: str
    base_url: str
    timeout_sec: float
    max_retries: int
    sleep_sec: float
    user_agent: str
    detail_parallel_workers: int
    coverage_endpoints: list[CoverageEndpoint]
    detail_endpoints: list[DetailEndpoint]
    payment_files: list[PaymentFile]


@dataclass(frozen=True)
class DetailFetchJob:
    detail: DetailEndpoint
    reimbursement_policy_id: int
    policy_id: str
    effective_date: str
    existing_related_codes: str
    url: str
    params: dict[str, Any]


@dataclass(frozen=True)
class FetchedDetailPage:
    job: DetailFetchJob
    response_status: int
    payload_text: str
    payload: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync CMS coverage and payment data into med-device reimbursement tables.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--endpoints", type=str, default="", help="Optional comma-separated CMS coverage endpoint names.")
    parser.add_argument("--detail-endpoints", type=str, default="", help="Optional comma-separated CMS detail endpoint names.")
    parser.add_argument("--payment-files", type=str, default="", help="Optional comma-separated configured payment file names.")
    parser.add_argument("--max-pages", type=int, default=0, help="Optional max page override for coverage endpoints.")
    parser.add_argument("--skip-coverage-api", action="store_true")
    parser.add_argument("--skip-detail-hcpcs", action="store_true")
    parser.add_argument("--skip-payment-files", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


def as_bool(raw: object, *, default: bool) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def to_float(raw: object) -> float | None:
    text = str(raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def date_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            pass
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        candidate = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
            return candidate
        except ValueError:
            return ""
    return ""


def compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def json_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def json_list(raw: Any) -> list[Any]:
    return raw if isinstance(raw, list) else []


def first_nonempty(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def lower_key_map(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key).strip().lower(): value for key, value in row.items()}


def row_get(row: dict[str, Any], *names: str) -> str:
    lowered = lower_key_map(row)
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def config_text_tuple(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        values = [str(part).strip() for part in raw]
    else:
        values = [str(raw).strip()]
    return tuple(value for value in values if value)


def nested_value(payload: Any, dotted_path: str) -> Any:
    current = payload
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def find_value(payload: Any, names: tuple[str, ...], *, depth: int = 0, max_depth: int = 20) -> str:
    if depth > max_depth:
        return ""
    wanted = {name.lower() for name in names}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in wanted and value is not None and not isinstance(value, (dict, list)):
                text = str(value).strip()
                if text:
                    return text
        for value in payload.values():
            found = find_value(value, names, depth=depth + 1, max_depth=max_depth)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_value(item, names, depth=depth + 1, max_depth=max_depth)
            if found:
                return found
    return ""


def records_from_payload(payload: dict[str, Any], record_path: str) -> list[dict[str, Any]]:
    target: Any = nested_value(payload, record_path) if record_path else None
    if target is None:
        for key in ("results", "data", "items", "documents", "records"):
            if isinstance(payload.get(key), list):
                target = payload.get(key)
                break
            if isinstance(payload.get(key), dict):
                nested = json_dict(payload.get(key))
                for nested_key in ("results", "data", "items", "documents", "records"):
                    if isinstance(nested.get(nested_key), list):
                        target = nested.get(nested_key)
                        break
            if target is not None:
                break
    if target is None:
        list_values = [value for value in payload.values() if isinstance(value, list)]
        dict_lists = [[item for item in value if isinstance(item, dict)] for value in list_values]
        dict_lists = [value for value in dict_lists if value]
        target = max(dict_lists, key=len) if dict_lists else []
    return [item for item in json_list(target) if isinstance(item, dict)]


def extract_codes(payload: Any) -> set[str]:
    values: list[str] = []

    def visit(value: Any, key_hint: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, str(key).lower())
        elif isinstance(value, list):
            for child in value:
                visit(child, key_hint)
        elif value is not None:
            text = str(value).upper()
            if key_hint and any(token in key_hint for token in ("code", "hcpc", "cpt", "procedure")):
                values.append(text)

    visit(payload)
    out: set[str] = set()
    for text in values:
        out.update(match.group(0) for match in CODE_TOKEN_RE.finditer(text))
    return out


def cms_policy(config: dict[str, Any]) -> CmsReimbursementPolicy:
    endpoints_raw = cfg_get(config, "cms_reimbursement_ingestion.coverage_endpoints", {})
    endpoints: list[CoverageEndpoint] = []
    if isinstance(endpoints_raw, dict):
        for name, raw in endpoints_raw.items():
            row = raw if isinstance(raw, dict) else {}
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            raw_params = row.get("params")
            params: dict[str, Any] = (
                {str(key): value for key, value in raw_params.items()} if isinstance(raw_params, dict) else {}
            )
            raw_max_pages = row.get("max_pages")
            endpoints.append(
                CoverageEndpoint(
                    name=str(name),
                    path=path,
                    enabled=as_bool(row.get("enabled"), default=True),
                    policy_type=str(row.get("policy_type") or name).strip().lower(),
                    params=params,
                    record_path=str(row.get("record_path") or "").strip(),
                    page_param=str(row.get("page_param") or "").strip(),
                    page_size_param=str(row.get("page_size_param") or "").strip(),
                    page_start=int(row.get("page_start") or 1),
                    page_size=max(1, int(row.get("page_size") or 100)),
                    max_pages=max(0, int(raw_max_pages if raw_max_pages is not None else 1)),
                )
            )
    files_raw = cfg_get(config, "cms_reimbursement_ingestion.payment_files", [])
    payment_files: list[PaymentFile] = []
    if isinstance(files_raw, list):
        for idx, raw in enumerate(files_raw, start=1):
            row = raw if isinstance(raw, dict) else {}
            file_path = str(row.get("path") or "").strip()
            url = str(row.get("url") or "").strip()
            if not file_path and not url:
                continue
            payment_files.append(
                PaymentFile(
                    name=str(row.get("name") or f"payment_file_{idx}").strip(),
                    path=file_path,
                    enabled=as_bool(row.get("enabled"), default=True),
                    payment_system=str(row.get("payment_system") or "unknown").strip().lower(),
                    code_type=str(row.get("code_type") or "hcpcs").strip().upper(),
                    effective_date=date_text(row.get("effective_date")),
                    source_id=str(row.get("source_id") or cfg_get(config, "cms_reimbursement_ingestion.payment_source_id", DEFAULT_PAYMENT_SOURCE_ID)),
                    replace_existing=as_bool(row.get("replace_existing"), default=True),
                    url=url,
                    zip_members=config_text_tuple(row.get("zip_members")),
                    header_required_columns=config_text_tuple(row.get("header_required_columns")),
                    excluded_rate_columns=config_text_tuple(row.get("excluded_rate_columns")),
                    refresh_download=as_bool(row.get("refresh_download"), default=False),
                )
            )
    detail_raw = cfg_get(config, "cms_reimbursement_ingestion.detail_hcpcs_endpoints", {})
    detail_endpoints: list[DetailEndpoint] = []
    if isinstance(detail_raw, dict):
        for name, raw in detail_raw.items():
            row = raw if isinstance(raw, dict) else {}
            path = str(row.get("path") or "").strip()
            policy_type = str(row.get("policy_type") or "").strip().lower()
            if not path or not policy_type:
                continue
            detail_endpoints.append(
                DetailEndpoint(
                    name=str(name),
                    policy_type=policy_type,
                    path=path,
                    id_param=str(row.get("id_param") or "").strip(),
                    version_param=str(row.get("version_param") or "ver").strip(),
                    record_path=str(row.get("record_path") or "data").strip(),
                    enabled=as_bool(row.get("enabled"), default=True),
                    max_documents=max(0, int(row.get("max_documents") or 0)),
                )
            )
    return CmsReimbursementPolicy(
        coverage_source_id=str(
            cfg_get(config, "cms_reimbursement_ingestion.coverage_source_id", DEFAULT_COVERAGE_SOURCE_ID)
            or DEFAULT_COVERAGE_SOURCE_ID
        ),
        payment_source_id=str(
            cfg_get(config, "cms_reimbursement_ingestion.payment_source_id", DEFAULT_PAYMENT_SOURCE_ID)
            or DEFAULT_PAYMENT_SOURCE_ID
        ),
        base_url=str(cfg_get(config, "cms_reimbursement_ingestion.coverage_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
        timeout_sec=float(cfg_get(config, "cms_reimbursement_ingestion.timeout_sec", 30.0)),
        max_retries=int(cfg_get(config, "cms_reimbursement_ingestion.max_retries", 3)),
        sleep_sec=float(cfg_get(config, "cms_reimbursement_ingestion.request_sleep_sec", 0.15)),
        user_agent=str(cfg_get(config, "cms_reimbursement_ingestion.user_agent", "JL, Independent Research")),
        detail_parallel_workers=max(1, int(cfg_get(config, "cms_reimbursement_ingestion.detail_parallel_workers", 4))),
        coverage_endpoints=endpoints,
        detail_endpoints=detail_endpoints,
        payment_files=payment_files,
    )


def ensure_source_registry(conn: Any, config: dict[str, Any], base_dir: Path, source_ids: set[str]) -> None:
    if not source_ids:
        return
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"SELECT source_id FROM source_registry WHERE source_id IN ({placeholders})",
        sorted(source_ids),
    ).fetchall()
    present = {str(row["source_id"]) for row in rows}
    missing = sorted(source_ids - present)
    if not missing:
        return
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    upsert_source_registry(conn, load_source_registry(registry_path))
    rows = conn.execute(
        f"SELECT source_id FROM source_registry WHERE source_id IN ({placeholders})",
        sorted(source_ids),
    ).fetchall()
    present = {str(row["source_id"]) for row in rows}
    missing = sorted(source_ids - present)
    if missing:
        raise ValueError(f"Source registry missing required source_id(s): {', '.join(missing)}")


def start_ingestion_run(conn: Any, source_id: str) -> int:
    now = utc_now()
    cur = conn.execute(
        "INSERT INTO ingestion_runs(source_id, started_at, status, created_at) VALUES (?, ?, 'running', ?)",
        (source_id, now, now),
    )
    if cur.lastrowid is None:
        raise RuntimeError("Could not create ingestion run")
    return int(cur.lastrowid)


def finish_ingestion_run(
    conn: Any,
    *,
    ingestion_run_id: int,
    status: str,
    request_count: int,
    row_count: int,
    message: str,
) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
        WHERE ingestion_run_id = ?
        """,
        (utc_now(), status, request_count, row_count, message, ingestion_run_id),
    )


def store_raw_response(
    conn: Any,
    *,
    source_id: str,
    endpoint: str,
    query_params: dict[str, Any],
    response_status: int,
    payload_text: str,
    ingestion_run_id: int,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            endpoint,
            compact_json(query_params),
            now,
            response_status,
            hashlib.sha256(payload_text.encode("utf-8", errors="replace")).hexdigest(),
            datetime.now(timezone.utc).date().isoformat(),
            payload_text,
            ingestion_run_id,
            now,
        ),
    )


def endpoint_url(policy: CmsReimbursementPolicy, endpoint: CoverageEndpoint) -> str:
    if endpoint.path.lower().startswith(("http://", "https://")):
        return endpoint.path
    return f"{policy.base_url}/{endpoint.path.lstrip('/')}"


def fetch_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    *,
    policy: CmsReimbursementPolicy,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, str, dict[str, Any]]:
    headers = {"User-Agent": policy.user_agent, "Accept": "application/json,text/plain,*/*"}
    if extra_headers:
        headers.update(extra_headers)
    last_status = 0
    last_text = ""
    last_payload: dict[str, Any] = {}
    for attempt in range(max(1, policy.max_retries)):
        response = session.get(url, params=params, headers=headers, timeout=policy.timeout_sec)
        last_status = int(response.status_code)
        last_text = response.text
        try:
            payload = response.json()
            last_payload = payload if isinstance(payload, dict) else {}
        except ValueError:
            last_payload = {}
        if response.status_code == 200:
            return last_status, last_text, last_payload
        if response.status_code in {429, 500, 502, 503, 504} and attempt < policy.max_retries - 1:
            time.sleep(max(0.1, policy.sleep_sec) * (attempt + 1) * 2)
            continue
        return last_status, last_text, last_payload
    return last_status, last_text, last_payload


def next_link(payload: dict[str, Any]) -> str:
    links = json_dict(payload.get("links"))
    for key in ("next", "Next"):
        value = str(links.get(key) or "").strip()
        if value:
            return value
    value = str(payload.get("next") or payload.get("nextLink") or "").strip()
    return value


def upsert_reimbursement_code(
    conn: Any,
    *,
    code_type: str,
    code: str,
    short_description: str,
    long_description: str,
    effective_date: str,
    source_id: str,
) -> int | None:
    normalized = normalize_code(code)
    if not normalized:
        return None
    row = conn.execute(
        """
        SELECT reimbursement_code_id
        FROM dim_reimbursement_code
        WHERE code_type = ?
          AND code = ?
          AND COALESCE(effective_date, '') = COALESCE(?, '')
        """,
        (code_type, normalized, effective_date or None),
    ).fetchone()
    now = utc_now()
    if row is not None:
        conn.execute(
            """
            UPDATE dim_reimbursement_code
            SET short_description = COALESCE(NULLIF(?, ''), short_description),
                long_description = COALESCE(NULLIF(?, ''), long_description),
                source_id = ?,
                updated_at = ?
            WHERE reimbursement_code_id = ?
            """,
            (short_description, long_description, source_id, now, int(row["reimbursement_code_id"])),
        )
        return int(row["reimbursement_code_id"])
    cur = conn.execute(
        """
        INSERT INTO dim_reimbursement_code(
            code_type, code, short_description, long_description, effective_date,
            source_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (code_type, normalized, short_description, long_description, effective_date or None, source_id, now, now),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else None


def upsert_policy_record(conn: Any, record: dict[str, Any], *, endpoint: CoverageEndpoint, source_id: str) -> int:
    title = find_value(record, ("title", "document_title", "name", "display_title"))
    effective_date = date_text(find_value(record, ("effective_date", "effectiveDate", "start_date", "startDate")))
    retirement_date = date_text(find_value(record, ("retirement_date", "retirementDate", "end_date", "endDate")))
    policy_id = find_value(
        record,
        (
            "policy_id",
            "document_id",
            "documentId",
            "document_number",
            "documentNumber",
            "lcd_id",
            "lcdId",
            "article_id",
            "articleId",
            "ncd_id",
            "ncdId",
            "id",
        ),
    )
    if not policy_id:
        policy_id = hashlib.sha256(compact_json({"title": title, "effective_date": effective_date, "record": record}).encode("utf-8")).hexdigest()[:24]
    related_codes = sorted(extract_codes(record))
    for code in related_codes:
        upsert_reimbursement_code(
            conn,
            code_type="HCPCS",
            code=code,
            short_description="",
            long_description="",
            effective_date=effective_date,
            source_id=source_id,
        )
    contractor = find_value(record, ("contractor_name", "contractorName", "contractor", "mac"))
    jurisdiction = find_value(record, ("jurisdiction", "jurisdiction_name", "jurisdictionName"))
    status = find_value(record, ("status", "document_status", "documentStatus"))
    row = conn.execute(
        """
        SELECT reimbursement_policy_id
        FROM fact_reimbursement_policy
        WHERE policy_type = ?
          AND COALESCE(policy_id, '') = COALESCE(?, '')
          AND COALESCE(effective_date, '') = COALESCE(?, '')
        """,
        (endpoint.policy_type, policy_id, effective_date or None),
    ).fetchone()
    now = utc_now()
    values = (
        policy_id,
        endpoint.policy_type,
        title,
        contractor,
        jurisdiction,
        effective_date or None,
        retirement_date or None,
        status,
        ";".join(related_codes),
        source_id,
        compact_json(record),
    )
    if row is not None:
        conn.execute(
            """
            UPDATE fact_reimbursement_policy
            SET title = ?, contractor_name = ?, jurisdiction = ?, retirement_date = ?,
                status = ?, related_codes = ?, source_id = ?, payload_json = ?, updated_at = ?
            WHERE reimbursement_policy_id = ?
            """,
            (
                title,
                contractor,
                jurisdiction,
                retirement_date or None,
                status,
                ";".join(related_codes),
                source_id,
                compact_json(record),
                now,
                int(row["reimbursement_policy_id"]),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO fact_reimbursement_policy(
                policy_id, policy_type, title, contractor_name, jurisdiction,
                effective_date, retirement_date, status, related_codes, source_id,
                payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, now, now),
        )
    return 1 + len(related_codes)


def sync_coverage_endpoint(
    conn: Any,
    endpoint: CoverageEndpoint,
    *,
    policy: CmsReimbursementPolicy,
    ingestion_run_id: int,
    max_pages_override: int,
) -> dict[str, Any]:
    requests_made = 0
    records_seen = 0
    canonical_rows = 0
    status = "success"
    reason = ""
    current_url = endpoint_url(policy, endpoint)
    next_url = ""
    max_pages = max_pages_override if max_pages_override > 0 else endpoint.max_pages
    with requests.Session() as session:
        page_index = 0
        while max_pages == 0 or page_index < max_pages:
            params = dict(endpoint.params)
            if endpoint.page_param:
                params[endpoint.page_param] = endpoint.page_start + page_index
            if endpoint.page_size_param:
                params[endpoint.page_size_param] = endpoint.page_size
            url = next_url or current_url
            if next_url:
                params = {}
            status_code, payload_text, payload = fetch_json(session, url, params, policy=policy)
            requests_made += 1
            store_raw_response(
                conn,
                source_id=policy.coverage_source_id,
                endpoint=url,
                query_params=params,
                response_status=status_code,
                payload_text=payload_text,
                ingestion_run_id=ingestion_run_id,
            )
            if status_code != 200:
                status = "failed"
                reason = f"http_status_{status_code}"
                break
            records = records_from_payload(payload, endpoint.record_path)
            records_seen += len(records)
            for record in records:
                canonical_rows += upsert_policy_record(conn, record, endpoint=endpoint, source_id=policy.coverage_source_id)
            LOGGER.info(
                "CMS coverage endpoint page processed: endpoint=%s page=%d records=%d canonical_rows=%d",
                endpoint.name,
                page_index + 1,
                len(records),
                canonical_rows,
            )
            page_index += 1
            next_url = next_link(payload)
            if next_url:
                time.sleep(max(0.0, policy.sleep_sec))
                continue
            if not endpoint.page_param or not records or len(records) < endpoint.page_size:
                break
            time.sleep(max(0.0, policy.sleep_sec))
    return {
        "dataset": endpoint.name,
        "source_id": policy.coverage_source_id,
        "status": status,
        "requests": requests_made,
        "records_seen": records_seen,
        "canonical_rows_upserted": canonical_rows,
        "review_reason": reason,
    }


def license_token(policy: CmsReimbursementPolicy) -> str:
    url = f"{policy.base_url}/v1/metadata/license-agreement/"
    with requests.Session() as session:
        status_code, payload_text, payload = fetch_json(session, url, {}, policy=policy)
    if status_code != 200:
        raise RuntimeError(f"Could not obtain CMS license agreement token: status={status_code} body={payload_text[:200]}")
    for row in json_list(payload.get("data")):
        token = first_nonempty(json_dict(row).get("Token"), json_dict(row).get("token"))
        if token:
            return token
    raise RuntimeError("CMS license agreement response did not include a token")


def detail_url(policy: CmsReimbursementPolicy, detail: DetailEndpoint) -> str:
    if detail.path.lower().startswith(("http://", "https://")):
        return detail.path
    return f"{policy.base_url}/{detail.path.lstrip('/')}"


def detail_jobs(conn: Any, *, policy: CmsReimbursementPolicy, detail: DetailEndpoint) -> list[DetailFetchJob]:
    rows = conn.execute(
        """
        SELECT reimbursement_policy_id, policy_id, effective_date, related_codes, payload_json
        FROM fact_reimbursement_policy
        WHERE source_id = ?
          AND policy_type = ?
        ORDER BY reimbursement_policy_id
        """,
        (policy.coverage_source_id, detail.policy_type),
    ).fetchall()
    out: list[DetailFetchJob] = []
    for row in rows:
        payload = json_dict(json.loads(str(row["payload_json"] or "{}")))
        document_id = first_nonempty(payload.get("document_id"), payload.get("documentId"), row["policy_id"])
        document_version = first_nonempty(payload.get("document_version"), payload.get("documentVersion"), payload.get("ver"))
        if not document_id or not document_version or not detail.id_param:
            continue
        out.append(
            DetailFetchJob(
                detail=detail,
                reimbursement_policy_id=int(row["reimbursement_policy_id"]),
                policy_id=str(row["policy_id"] or ""),
                effective_date=str(row["effective_date"] or ""),
                existing_related_codes=str(row["related_codes"] or ""),
                url=detail_url(policy, detail),
                params={detail.id_param: document_id, detail.version_param: document_version},
            )
        )
        if detail.max_documents > 0 and len(out) >= detail.max_documents:
            break
    return out


def fetch_detail_job(job: DetailFetchJob, *, token: str, policy: CmsReimbursementPolicy) -> FetchedDetailPage:
    headers = {"Authorization": f"Bearer {token}"}
    with requests.Session() as session:
        status_code, payload_text, payload = fetch_json(session, job.url, job.params, policy=policy, extra_headers=headers)
    return FetchedDetailPage(job=job, response_status=status_code, payload_text=payload_text, payload=payload)


def upsert_detail_codes(conn: Any, page: FetchedDetailPage, *, source_id: str) -> tuple[int, int, str]:
    if page.response_status != 200:
        return 0, 0, f"http_status_{page.response_status}"
    records = records_from_payload(page.payload, page.job.detail.record_path)
    codes = {normalize_code(code) for code in page.job.existing_related_codes.split(";") if normalize_code(code)}
    upserted = 0
    for record in records:
        code = normalize_code(
            first_nonempty(
                find_value(record, ("hcpc_code_id", "hcpcs_code", "hcpc_code", "code")),
                row_get(record, "hcpc_code_id", "hcpcs_code", "hcpc_code", "code"),
            )
        )
        if not code:
            continue
        codes.add(code)
        upsert_reimbursement_code(
            conn,
            code_type="HCPCS",
            code=code,
            short_description=find_value(record, ("short_description", "shortDescription")),
            long_description=find_value(record, ("long_description", "longDescription")),
            effective_date=date_text(find_value(record, ("effective_date", "effectiveDate", "last_updated", "lastUpdated")))
            or page.job.effective_date,
            source_id=source_id,
        )
        upserted += 1
    if codes:
        conn.execute(
            """
            UPDATE fact_reimbursement_policy
            SET related_codes = ?, updated_at = ?
            WHERE reimbursement_policy_id = ?
            """,
            (";".join(sorted(codes)), utc_now(), page.job.reimbursement_policy_id),
        )
    return len(records), upserted, ""


def sync_detail_endpoint(
    conn: Any,
    detail: DetailEndpoint,
    *,
    policy: CmsReimbursementPolicy,
    ingestion_run_id: int,
    token: str,
) -> dict[str, Any]:
    jobs = detail_jobs(conn, policy=policy, detail=detail)
    requests_made = 0
    records_seen = 0
    canonical_rows = 0
    failed = 0
    reason = ""
    if not jobs:
        return {
            "dataset": detail.name,
            "source_id": policy.coverage_source_id,
            "status": "empty",
            "requests": 0,
            "records_seen": 0,
            "canonical_rows_upserted": 0,
            "review_reason": "no_source_policy_rows",
        }
    if policy.detail_parallel_workers > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=policy.detail_parallel_workers) as executor:
            futures = [executor.submit(fetch_detail_job, job, token=token, policy=policy) for job in jobs]
            for future in as_completed(futures):
                page = future.result()
                requests_made += 1
                store_raw_response(
                    conn,
                    source_id=policy.coverage_source_id,
                    endpoint=page.job.url,
                    query_params=page.job.params,
                    response_status=page.response_status,
                    payload_text=page.payload_text,
                    ingestion_run_id=ingestion_run_id,
                )
                seen, upserted, page_reason = upsert_detail_codes(conn, page, source_id=policy.coverage_source_id)
                records_seen += seen
                canonical_rows += upserted
                if page_reason:
                    failed += 1
                    reason = page_reason
    else:
        for job in jobs:
            page = fetch_detail_job(job, token=token, policy=policy)
            requests_made += 1
            store_raw_response(
                conn,
                source_id=policy.coverage_source_id,
                endpoint=page.job.url,
                query_params=page.job.params,
                response_status=page.response_status,
                payload_text=page.payload_text,
                ingestion_run_id=ingestion_run_id,
            )
            seen, upserted, page_reason = upsert_detail_codes(conn, page, source_id=policy.coverage_source_id)
            records_seen += seen
            canonical_rows += upserted
            if page_reason:
                failed += 1
                reason = page_reason
            time.sleep(max(0.0, policy.sleep_sec))
    status = "partial" if failed else "success"
    LOGGER.info(
        "CMS detail endpoint complete: endpoint=%s documents=%d requests=%d codes=%d failed=%d",
        detail.name,
        len(jobs),
        requests_made,
        canonical_rows,
        failed,
    )
    return {
        "dataset": detail.name,
        "source_id": policy.coverage_source_id,
        "status": status,
        "requests": requests_made,
        "records_seen": records_seen,
        "canonical_rows_upserted": canonical_rows,
        "review_reason": reason,
    }


DEFAULT_RATE_EXCLUDED_COLUMNS = {
    "apc",
    "apc_code",
    "catg",
    "category",
    "code",
    "cpt",
    "cpt_code",
    "desc",
    "descriptor",
    "drg",
    "effective date",
    "effective_date",
    "hcpcs",
    "hcpcs cd",
    "hcpcs/cpt code",
    "hcpcs_code",
    "juris",
    "jurisdiction",
    "locality",
    "long desc",
    "long descriptor",
    "long_description",
    "mod",
    "mod2",
    "modifier",
    "modifier2",
    "payment",
    "payment_rate",
    "procedure_code",
    "rate",
    "short desc",
    "short_description",
    "si",
    "start date",
    "start_date",
    "state",
    "status indicator",
    "status_indicator",
}


def normalized_header(value: object) -> str:
    return str(value or "").strip().lstrip("\ufeff").lower()


def zip_member_selected(member_name: str, payment_file: PaymentFile) -> bool:
    if not payment_file.zip_members:
        return member_name.lower().endswith((".csv", ".txt"))
    wanted = {name.replace("\\", "/").lower() for name in payment_file.zip_members}
    candidate = member_name.replace("\\", "/").lower()
    basename = Path(candidate).name.lower()
    return candidate in wanted or basename in wanted


def header_index(lines: list[str], required_columns: tuple[str, ...]) -> int:
    if not lines:
        return 0
    required = {normalized_header(column) for column in required_columns if str(column).strip()}
    if not required:
        return 0
    for idx, line in enumerate(lines[:75]):
        try:
            columns = {normalized_header(column) for column in next(csv.reader([line]))}
        except csv.Error:
            continue
        if required.issubset(columns):
            return idx
    raise ValueError(f"Could not find CSV header containing required columns: {', '.join(sorted(required))}")


def csv_rows_from_text(text: str, payment_file: PaymentFile) -> list[dict[str, str]]:
    lines = text.splitlines()
    start = header_index(lines, payment_file.header_required_columns)
    reader = csv.DictReader(lines[start:])
    return [dict(row) for row in reader]


def iter_csv_rows(path: Path, payment_file: PaymentFile) -> list[dict[str, str]]:
    if path.suffix.lower() == ".zip":
        out: list[dict[str, str]] = []
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if zip_member_selected(name, payment_file)]
            for name in names:
                with archive.open(name) as raw:
                    text = raw.read().decode("utf-8-sig", errors="replace")
                    out.extend(csv_rows_from_text(text, payment_file))
        return out
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return csv_rows_from_text(handle.read(), payment_file)


def download_payment_file(payment_file: PaymentFile, path: Path, *, policy: CmsReimbursementPolicy | None) -> int:
    if not payment_file.url:
        return 0
    if path.exists() and not payment_file.refresh_download:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": policy.user_agent if policy is not None else "JL, Independent Research"}
    timeout_sec = policy.timeout_sec if policy is not None else 30.0
    max_retries = policy.max_retries if policy is not None else 3
    sleep_sec = policy.sleep_sec if policy is not None else 0.15
    requests_made = 0
    last_status = 0
    last_text = ""
    with requests.Session() as session:
        for attempt in range(max(1, max_retries)):
            response = session.get(payment_file.url, headers=headers, timeout=timeout_sec)
            requests_made += 1
            last_status = int(response.status_code)
            if response.status_code == 200:
                with path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                return requests_made
            last_text = response.text[:300]
            if response.status_code in {429, 500, 502, 503, 504} and attempt < max_retries - 1:
                time.sleep(max(0.1, sleep_sec) * (attempt + 1) * 2)
                continue
            break
    raise RuntimeError(f"download_failed status={last_status} url={payment_file.url} body={last_text}")


def payment_row_code(row: dict[str, Any]) -> str:
    return normalize_code(
        row_get(
            row,
            "hcpcs",
            "hcpcs_code",
            "code",
            "procedure_code",
            "cpt",
            "cpt_code",
            "hcpcs cd",
            "hcpcs/cpt code",
        )
    )


def payment_row_rate(row: dict[str, Any]) -> float | None:
    return to_float(
        row_get(
            row,
            "payment_rate",
            "payment",
            "rate",
            "national_payment_rate",
            "non-facility price",
            "facility price",
            "fee_schedule_amount",
            "fee",
        )
    )


def payment_rate_values(row: dict[str, Any], payment_file: PaymentFile) -> list[tuple[str, float]]:
    configured_rate = payment_row_rate(row)
    if configured_rate is not None:
        locality = row_get(row, "locality", "carrier", "state", "jurisdiction")
        return [(locality, configured_rate)]
    excluded = {normalized_header(column) for column in DEFAULT_RATE_EXCLUDED_COLUMNS}
    excluded.update(normalized_header(column) for column in payment_file.excluded_rate_columns)
    out: list[tuple[str, float]] = []
    seen: set[tuple[str, float]] = set()
    for key, raw_value in row.items():
        header = str(key or "").strip()
        if not header or normalized_header(header) in excluded:
            continue
        rate = to_float(raw_value)
        if rate is None:
            continue
        item = (header, rate)
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def upsert_reimbursement_rate(
    conn: Any,
    *,
    reimbursement_code_id: int,
    payment_system: str,
    effective_date: str,
    locality: str,
    apc: str,
    drg: str,
    payment_rate: float,
    status_indicator: str,
    source_id: str,
    payload_json: str,
    now: str,
) -> None:
    existing = conn.execute(
        """
        SELECT reimbursement_rate_id
        FROM fact_reimbursement_rate
        WHERE reimbursement_code_id = ?
          AND payment_system = ?
          AND COALESCE(effective_date, '') = ?
          AND COALESCE(locality, '') = ?
          AND COALESCE(apc, '') = ?
          AND COALESCE(drg, '') = ?
          AND COALESCE(status_indicator, '') = ?
          AND source_id = ?
        LIMIT 1
        """,
        (
            reimbursement_code_id,
            payment_system,
            effective_date or "",
            locality or "",
            apc or "",
            drg or "",
            status_indicator or "",
            source_id,
        ),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE fact_reimbursement_rate
            SET payment_rate = ?, payload_json = ?, updated_at = ?
            WHERE reimbursement_rate_id = ?
            """,
            (payment_rate, payload_json, now, int(existing["reimbursement_rate_id"])),
        )
        return
    conn.execute(
        """
        INSERT INTO fact_reimbursement_rate(
            reimbursement_code_id, payment_system, effective_date, locality, apc, drg,
            payment_rate, status_indicator, source_id, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reimbursement_code_id,
            payment_system,
            effective_date or None,
            locality or None,
            apc or None,
            drg or None,
            payment_rate,
            status_indicator or None,
            source_id,
            payload_json,
            now,
            now,
        ),
    )


def sync_payment_file(
    conn: Any,
    payment_file: PaymentFile,
    *,
    base_dir: Path,
    policy: CmsReimbursementPolicy | None = None,
) -> dict[str, Any]:
    if not payment_file.path:
        return {
            "dataset": payment_file.name,
            "source_id": payment_file.source_id,
            "status": "failed",
            "requests": 0,
            "records_seen": 0,
            "canonical_rows_upserted": 0,
            "review_reason": "path_required_for_payment_file_cache",
        }
    path = resolve_path(payment_file.path, base_dir=base_dir)
    requests_made = download_payment_file(payment_file, path, policy=policy)
    if not path.exists():
        return {
            "dataset": payment_file.name,
            "source_id": payment_file.source_id,
            "status": "failed",
            "requests": requests_made,
            "records_seen": 0,
            "canonical_rows_upserted": 0,
            "review_reason": f"file_not_found:{path}",
        }
    records = iter_csv_rows(path, payment_file)
    effective_date = payment_file.effective_date
    if payment_file.replace_existing:
        conn.execute(
            """
            DELETE FROM fact_reimbursement_rate
            WHERE source_id = ? AND payment_system = ? AND COALESCE(effective_date, '') = ?
            """,
            (payment_file.source_id, payment_file.payment_system, effective_date or ""),
        )
    canonical_rows = 0
    now = utc_now()
    for record in records:
        code = payment_row_code(record)
        if not code:
            continue
        short_description = row_get(record, "short_description", "short desc", "description", "desc", "descriptor")
        long_description = row_get(record, "long_description", "long desc", "long descriptor")
        row_effective_date = date_text(row_get(record, "effective_date", "effective date", "start_date", "start date")) or effective_date
        code_id = upsert_reimbursement_code(
            conn,
            code_type=payment_file.code_type,
            code=code,
            short_description=short_description,
            long_description=long_description,
            effective_date=row_effective_date,
            source_id=payment_file.source_id,
        )
        canonical_rows += 1
        if code_id is None:
            continue
        for locality, rate in payment_rate_values(record, payment_file):
            payload = dict(record)
            payload["_rate_column"] = locality
            upsert_reimbursement_rate(
                conn,
                reimbursement_code_id=code_id,
                payment_system=payment_file.payment_system,
                effective_date=row_effective_date,
                locality=locality,
                apc=row_get(record, "apc", "apc_code"),
                drg=row_get(record, "drg", "ms_drg"),
                payment_rate=rate,
                status_indicator=row_get(record, "status_indicator", "status indicator", "si"),
                source_id=payment_file.source_id,
                payload_json=compact_json(payload),
                now=now,
            )
            canonical_rows += 1
    return {
        "dataset": payment_file.name,
        "source_id": payment_file.source_id,
        "status": "success",
        "requests": requests_made,
        "records_seen": len(records),
        "canonical_rows_upserted": canonical_rows,
        "review_reason": "",
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in FIELDNAMES} for row in rows])


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    policy = cms_policy(config)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "cms_reimbursement_ingestion.output_csv", "../output/med_devices_reports/med_device_cms_reimbursement_ingestion.csv"),
            base_dir=base_dir,
        )
    )
    endpoint_filter = {value.strip() for value in str(args.endpoints or "").split(",") if value.strip()}
    detail_filter = {value.strip() for value in str(args.detail_endpoints or "").split(",") if value.strip()}
    payment_filter = {value.strip() for value in str(args.payment_files or "").split(",") if value.strip()}
    coverage_endpoints = [
        endpoint
        for endpoint in policy.coverage_endpoints
        if endpoint.enabled and (not endpoint_filter or endpoint.name in endpoint_filter)
    ]
    detail_endpoints = [
        detail
        for detail in policy.detail_endpoints
        if detail.enabled and (not detail_filter or detail.name in detail_filter)
    ]
    payment_files = [
        payment_file
        for payment_file in policy.payment_files
        if payment_file.enabled and (not payment_filter or payment_file.name in payment_filter)
    ]
    if args.skip_coverage_api:
        coverage_endpoints = []
    if args.skip_detail_hcpcs:
        detail_endpoints = []
    if args.skip_payment_files:
        payment_files = []
    if not coverage_endpoints and not detail_endpoints and not payment_files:
        raise ValueError("No CMS reimbursement coverage endpoints or payment files selected")

    coverage_rows: list[dict[str, Any]] = []
    request_count = 0
    payment_request_count = 0
    canonical_rows = 0
    failed: list[str] = []
    source_ids = {policy.coverage_source_id} if coverage_endpoints or detail_endpoints else set()
    source_ids.update(payment_file.source_id for payment_file in payment_files)
    LOGGER.info(
        "CMS reimbursement sync starting: db=%s coverage=%d detail=%d payment_files=%d",
        db_path,
        len(coverage_endpoints),
        len(detail_endpoints),
        len(payment_files),
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source_registry(conn, config, base_dir, source_ids)
        run_id = start_run(conn, run_type="sync_med_device_cms_reimbursement", input_path=config_path)
        coverage_ingestion_id = start_ingestion_run(conn, policy.coverage_source_id) if coverage_endpoints or detail_endpoints else None
        payment_ingestion_id = start_ingestion_run(conn, policy.payment_source_id) if payment_files else None
        try:
            for endpoint in coverage_endpoints:
                if coverage_ingestion_id is None:
                    raise RuntimeError("Coverage ingestion run was not initialized")
                try:
                    row = sync_coverage_endpoint(
                        conn,
                        endpoint,
                        policy=policy,
                        ingestion_run_id=coverage_ingestion_id,
                        max_pages_override=int(args.max_pages),
                    )
                except Exception as exc:
                    row = {
                        "dataset": endpoint.name,
                        "source_id": policy.coverage_source_id,
                        "status": "failed",
                        "requests": 0,
                        "records_seen": 0,
                        "canonical_rows_upserted": 0,
                        "review_reason": f"{type(exc).__name__}: {exc}",
                    }
                    failed.append(endpoint.name)
                    LOGGER.warning("CMS coverage endpoint failed: %s %s", endpoint.name, exc)
                coverage_rows.append(row)
                request_count += int(row.get("requests") or 0)
                canonical_rows += int(row.get("canonical_rows_upserted") or 0)
                if row.get("status") == "failed" and endpoint.name not in failed:
                    failed.append(endpoint.name)
                conn.commit()
            if detail_endpoints:
                if coverage_ingestion_id is None:
                    raise RuntimeError("Coverage ingestion run was not initialized")
                token = license_token(policy)
                for detail in detail_endpoints:
                    try:
                        row = sync_detail_endpoint(
                            conn,
                            detail,
                            policy=policy,
                            ingestion_run_id=coverage_ingestion_id,
                            token=token,
                        )
                    except Exception as exc:
                        row = {
                            "dataset": detail.name,
                            "source_id": policy.coverage_source_id,
                            "status": "failed",
                            "requests": 0,
                            "records_seen": 0,
                            "canonical_rows_upserted": 0,
                            "review_reason": f"{type(exc).__name__}: {exc}",
                        }
                        failed.append(detail.name)
                        LOGGER.warning("CMS detail endpoint failed: %s %s", detail.name, exc)
                    coverage_rows.append(row)
                    request_count += int(row.get("requests") or 0)
                    canonical_rows += int(row.get("canonical_rows_upserted") or 0)
                    if row.get("status") == "failed" and detail.name not in failed:
                        failed.append(detail.name)
                    conn.commit()
            for payment_file in payment_files:
                try:
                    row = sync_payment_file(conn, payment_file, base_dir=base_dir, policy=policy)
                except Exception as exc:
                    row = {
                        "dataset": payment_file.name,
                        "source_id": payment_file.source_id,
                        "status": "failed",
                        "requests": 0,
                        "records_seen": 0,
                        "canonical_rows_upserted": 0,
                        "review_reason": f"{type(exc).__name__}: {exc}",
                    }
                    failed.append(payment_file.name)
                    LOGGER.warning("CMS payment file failed: %s %s", payment_file.name, exc)
                coverage_rows.append(row)
                payment_request_count += int(row.get("requests") or 0)
                canonical_rows += int(row.get("canonical_rows_upserted") or 0)
                if row.get("status") == "failed" and payment_file.name not in failed:
                    failed.append(payment_file.name)
                conn.commit()
            status = "partial" if failed else "success"
            total_requests = request_count + payment_request_count
            message = f"datasets={len(coverage_rows)} requests={total_requests} canonical_rows={canonical_rows} output={output_csv}"
            if failed:
                message += " failed=" + ",".join(sorted(set(failed)))
            if coverage_ingestion_id is not None:
                finish_ingestion_run(
                    conn,
                    ingestion_run_id=coverage_ingestion_id,
                    status=status,
                    request_count=request_count,
                    row_count=sum(int(row.get("canonical_rows_upserted") or 0) for row in coverage_rows if row.get("source_id") == policy.coverage_source_id),
                    message=message,
                )
            if payment_ingestion_id is not None:
                finish_ingestion_run(
                    conn,
                    ingestion_run_id=payment_ingestion_id,
                    status=status,
                    request_count=payment_request_count,
                    row_count=sum(int(row.get("canonical_rows_upserted") or 0) for row in coverage_rows if row.get("source_id") == policy.payment_source_id),
                    message=message,
                )
            finish_run(conn, run_id=run_id, status=status, row_count=canonical_rows, message=message)
        except BaseException as exc:
            message = f"{type(exc).__name__}: {exc}"
            if coverage_ingestion_id is not None:
                finish_ingestion_run(conn, ingestion_run_id=coverage_ingestion_id, status="failed", request_count=request_count, row_count=canonical_rows, message=message)
            if payment_ingestion_id is not None:
                finish_ingestion_run(conn, ingestion_run_id=payment_ingestion_id, status="failed", request_count=payment_request_count, row_count=0, message=message)
            finish_run(conn, run_id=run_id, status="failed", row_count=canonical_rows, message=message)
            raise
    write_csv(output_csv, coverage_rows)
    LOGGER.info("CMS reimbursement sync complete: rows=%d output=%s failed=%d", canonical_rows, output_csv, len(failed))
    if failed and not args.allow_partial:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
