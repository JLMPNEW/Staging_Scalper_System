#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
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
from med_devices.core.text_norm import normalize_org_name  # noqa: E402


LOGGER = logging.getLogger("sync_med_device_fda_core")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_SOURCE_ID = "openfda_device"
DEFAULT_BASE_URL = "https://api.fda.gov/device"
FIELDNAMES = [
    "endpoint_name",
    "path",
    "search",
    "status",
    "pages_fetched",
    "api_records_seen",
    "canonical_rows_upserted",
    "review_reason",
]


@dataclass(frozen=True)
class EndpointConfig:
    name: str
    path: str
    enabled: bool
    search: str
    sort: str
    max_records: int


@dataclass(frozen=True)
class FdaPolicy:
    source_id: str
    base_url: str
    api_key_env: str
    api_key_file: str
    api_key_file_field: str
    timeout_sec: float
    max_retries: int
    parallel_workers: int
    sleep_sec: float
    page_limit: int
    commit_every_pages: int
    user_agent: str
    endpoints: list[EndpointConfig]


@dataclass(frozen=True)
class FetchedFdaPage:
    endpoint_name: str
    url: str
    public_params: dict[str, Any]
    skip: int
    page_number_hint: int
    response_status: int
    payload_text: str
    payload: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync openFDA core medical-device data into canonical med-device tables.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--endpoints", type=str, default="", help="Optional comma-separated endpoint names.")
    parser.add_argument("--max-records", type=int, default=0, help="Optional per-endpoint max record override.")
    parser.add_argument("--targeted-footprints", action="store_true", help="Fetch exact FDA submission IDs from the footprint CSV.")
    parser.add_argument("--targeted-entity-names", action="store_true", help="Also fetch exact applicant-name rows for device footprint entities.")
    parser.add_argument("--targeted-only", action="store_true", help="Only run targeted footprint fetches, not the broad endpoint sync.")
    parser.add_argument("--footprint-csv", type=Path, default=None, help="Optional FDA footprint CSV override.")
    parser.add_argument("--target-limit", type=int, default=100, help="Max records per targeted footprint query.")
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
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def date_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def normalize_product_code(raw: object) -> str:
    return "".join(ch for ch in str(raw or "").upper().strip() if ch.isalnum())[:16]


def compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def row_get(row: dict[str, str], *keys: str) -> str:
    lowered = {str(key).strip().lower(): str(value or "") for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def split_multi_value(raw: object) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[;|]", str(raw or "")):
        value = item.strip()
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def normalize_submission_identifier(raw: object) -> str:
    value = re.sub(r"[^A-Z0-9-]+", "", str(raw or "").upper().strip())
    if not value or not any(ch.isdigit() for ch in value):
        return ""
    unsupported = {
        "510KDENOVOPIPELINE",
        "510KPIPELINE",
        "CLASSIREGISTRY",
        "DISTRIBUTIONONLY",
        "DMF",
        "FEI-ONLY",
        "FEIONLY",
        "MASTERFILE",
        "PMA-PIPELINE",
        "PMAPIPELINE",
    }
    return "" if value in unsupported else value


def quote_openfda_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def endpoint_slug(raw: object, *, max_length: int = 48) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", str(raw or "").upper()).strip("_")
    return slug[:max_length] or "UNKNOWN"


def json_list(raw: Any) -> list[Any]:
    return raw if isinstance(raw, list) else []


def json_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def field(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def severity_weight(classification: object) -> float:
    text = str(classification or "").strip().lower().replace(" ", "_")
    if "class_i" in text or text == "i":
        return 5.0
    if "class_ii" in text or text == "ii":
        return 2.0
    if "class_iii" in text or text == "iii":
        return 0.5
    return 1.0


def fda_policy(config: dict[str, Any]) -> FdaPolicy:
    endpoints_raw = cfg_get(config, "fda_core_ingestion.endpoints", {})
    endpoints: list[EndpointConfig] = []
    if isinstance(endpoints_raw, dict):
        for name, raw in endpoints_raw.items():
            row = raw if isinstance(raw, dict) else {}
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            endpoints.append(
                EndpointConfig(
                    name=str(name),
                    path=path,
                    enabled=as_bool(row.get("enabled"), default=True),
                    search=str(row.get("search") or "").strip(),
                    sort=str(row.get("sort") or "").strip(),
                    max_records=max(0, int(row.get("max_records") or 0)),
                )
            )
    return FdaPolicy(
        source_id=str(cfg_get(config, "fda_core_ingestion.source_id", DEFAULT_SOURCE_ID) or DEFAULT_SOURCE_ID),
        base_url=str(cfg_get(config, "fda_core_ingestion.base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
        api_key_env=str(cfg_get(config, "fda_core_ingestion.api_key_env", "OPENFDA_API_KEY") or "OPENFDA_API_KEY"),
        api_key_file=str(cfg_get(config, "fda_core_ingestion.api_key_file", "secrets.local.yaml") or "secrets.local.yaml"),
        api_key_file_field=str(
            cfg_get(config, "fda_core_ingestion.api_key_file_field", "openfda_api_key") or "openfda_api_key"
        ),
        timeout_sec=float(cfg_get(config, "fda_core_ingestion.timeout_sec", 30.0)),
        max_retries=int(cfg_get(config, "fda_core_ingestion.max_retries", 3)),
        parallel_workers=max(1, int(cfg_get(config, "fda_core_ingestion.parallel_workers", 1))),
        sleep_sec=float(cfg_get(config, "fda_core_ingestion.request_sleep_sec", 0.15)),
        page_limit=max(1, min(1000, int(cfg_get(config, "fda_core_ingestion.page_limit", 1000)))),
        commit_every_pages=max(1, int(cfg_get(config, "fda_core_ingestion.commit_every_pages", 10))),
        user_agent=str(cfg_get(config, "fda_core_ingestion.user_agent", "JL, Independent Research, jm.357@hotmail.com")),
        endpoints=endpoints,
    )


def resolve_api_key(config: dict[str, Any], *, policy: FdaPolicy, base_dir: Path) -> str:
    env_key = os.environ.get(policy.api_key_env, "").strip()
    if env_key:
        return env_key
    if not policy.api_key_file:
        return ""
    key_path = resolve_path(policy.api_key_file, base_dir=base_dir)
    if not key_path.exists():
        return ""
    payload = load_yaml(key_path)
    key = str(payload.get(policy.api_key_file_field) or "").strip()
    if key:
        return key
    LOGGER.warning("FDA API key file exists but field is empty: %s field=%s", key_path, policy.api_key_file_field)
    return ""


def ensure_source_registry(conn: Any, config: dict[str, Any], base_dir: Path, source_id: str) -> None:
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    if row is not None:
        return
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    upsert_source_registry(conn, load_source_registry(registry_path))
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"Source registry missing required source_id: {source_id}")


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


def fetch_openfda_page(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    *,
    policy: FdaPolicy,
) -> tuple[int, str, dict[str, Any]]:
    last_status = 0
    last_text = ""
    last_payload: dict[str, Any] = {}
    headers = {"User-Agent": policy.user_agent, "Accept": "application/json,text/plain,*/*"}
    total_attempts = max(1, policy.max_retries + 1)
    for attempt in range(total_attempts):
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
        if response.status_code in {429, 500, 502, 503, 504} and attempt < total_attempts - 1:
            time.sleep(max(0.1, policy.sleep_sec) * (attempt + 1) * 2)
            continue
        return last_status, last_text, last_payload
    return last_status, last_text, last_payload


def openfda_total(payload: dict[str, Any]) -> int | None:
    meta = json_dict(payload.get("meta"))
    results = json_dict(meta.get("results"))
    raw_total = results.get("total")
    try:
        total = int(str(raw_total))
    except (TypeError, ValueError):
        return None
    return max(0, total)


def is_openfda_not_found(payload: dict[str, Any]) -> bool:
    error = json_dict(payload.get("error"))
    return str(error.get("code") or "").upper() == "NOT_FOUND"


def endpoint_url(policy: FdaPolicy, endpoint: EndpointConfig) -> str:
    return f"{policy.base_url}/{endpoint.path.lstrip('/')}"


def build_targeted_footprint_endpoints(
    path: Path | None,
    *,
    target_limit: int,
    include_entity_names: bool = False,
) -> list[EndpointConfig]:
    if path is None:
        return []
    if not path.exists():
        LOGGER.warning("Configured FDA footprint CSV does not exist: %s", path)
        return []
    out: list[EndpointConfig] = []
    seen: set[tuple[str, str]] = set()
    for row in read_csv_flexible(path):
        for raw_identifier in split_multi_value(row_get(row, "premarket_numbers", "premarket_number")):
            identifier = normalize_submission_identifier(raw_identifier)
            if not identifier:
                continue
            if identifier.startswith("K") and re.match(r"^K[0-9]{5,}$", identifier):
                endpoint = EndpointConfig(
                    name=f"target_510k_{identifier}",
                    path="510k.json",
                    enabled=True,
                    search=f"k_number:{quote_openfda_value(identifier)}",
                    sort="",
                    max_records=max(1, target_limit),
                )
            elif identifier.startswith("P") and re.match(r"^P[0-9]{5,}$", identifier):
                endpoint = EndpointConfig(
                    name=f"target_pma_{identifier}",
                    path="pma.json",
                    enabled=True,
                    search=f"pma_number:{quote_openfda_value(identifier)}",
                    sort="",
                    max_records=max(1, target_limit),
                )
            else:
                continue
            key = (endpoint.path, endpoint.search)
            if key in seen:
                continue
            seen.add(key)
            out.append(endpoint)
        if include_entity_names and row_get(row, "footprint_category", "category") == "device_manufacturer":
            entity = row_get(row, "primary_fda_entity", "fda_entity", "manufacturer_name")
            if not entity:
                continue
            slug = endpoint_slug(row_get(row, "ticker", "symbol") + "_" + entity)
            for name, path_name in (("510k", "510k.json"), ("pma", "pma.json")):
                endpoint = EndpointConfig(
                    name=f"target_entity_{name}_{slug}",
                    path=path_name,
                    enabled=True,
                    search=f"applicant:{quote_openfda_value(entity)}",
                    sort="decision_date:desc",
                    max_records=max(1, target_limit),
                )
                key = (endpoint.path, endpoint.search)
                if key in seen:
                    continue
                seen.add(key)
                out.append(endpoint)
    LOGGER.info("Built targeted FDA footprint queries: rows=%d path=%s", len(out), path)
    return out


def page_params(
    endpoint: EndpointConfig,
    *,
    skip: int,
    limit: int,
    api_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    public_params: dict[str, Any] = {"limit": limit, "skip": skip}
    if endpoint.search:
        public_params["search"] = endpoint.search
    if endpoint.sort:
        public_params["sort"] = endpoint.sort
    params = dict(public_params)
    if api_key:
        params["api_key"] = api_key
    return params, public_params


def fetch_fda_page_job(
    endpoint: EndpointConfig,
    *,
    policy: FdaPolicy,
    api_key: str,
    skip: int,
    limit: int,
    page_number_hint: int,
) -> FetchedFdaPage:
    url = endpoint_url(policy, endpoint)
    params, public_params = page_params(endpoint, skip=skip, limit=limit, api_key=api_key)
    with requests.Session() as session:
        status_code, payload_text, payload = fetch_openfda_page(session, url, params, policy=policy)
    return FetchedFdaPage(
        endpoint_name=endpoint.name,
        url=url,
        public_params=public_params,
        skip=skip,
        page_number_hint=page_number_hint,
        response_status=status_code,
        payload_text=payload_text,
        payload=payload,
    )


def upsert_product_code(
    conn: Any,
    *,
    product_code: str,
    device_name: str = "",
    medical_specialty: str = "",
    device_class: str = "",
    regulation_number: str = "",
    source_id: str,
) -> None:
    code = normalize_product_code(product_code)
    if not code:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_fda_product_code(
            product_code, device_name, medical_specialty, device_class, regulation_number,
            source_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_code) DO UPDATE SET
            device_name = COALESCE(NULLIF(excluded.device_name, ''), dim_fda_product_code.device_name),
            medical_specialty = COALESCE(NULLIF(excluded.medical_specialty, ''), dim_fda_product_code.medical_specialty),
            device_class = COALESCE(NULLIF(excluded.device_class, ''), dim_fda_product_code.device_class),
            regulation_number = COALESCE(NULLIF(excluded.regulation_number, ''), dim_fda_product_code.regulation_number),
            source_id = excluded.source_id,
            updated_at = excluded.updated_at
        """,
        (code, device_name, medical_specialty, device_class, regulation_number, source_id, now, now),
    )


def upsert_manufacturer(conn: Any, manufacturer_name: str, *, fei_number: str = "") -> int | None:
    name = str(manufacturer_name or "").strip()
    if not name:
        return None
    norm = normalize_org_name(name)
    if not norm:
        return None
    fei = str(fei_number or "").strip() or None
    row = conn.execute(
        """
        SELECT fda_manufacturer_id
        FROM dim_fda_manufacturer
        WHERE manufacturer_name_norm = ?
          AND COALESCE(fei_number, '') = COALESCE(?, '')
        """,
        (norm, fei),
    ).fetchone()
    now = utc_now()
    if row is not None:
        conn.execute(
            """
            UPDATE dim_fda_manufacturer
            SET manufacturer_name = ?, updated_at = ?
            WHERE fda_manufacturer_id = ?
            """,
            (name, now, int(row["fda_manufacturer_id"])),
        )
        return int(row["fda_manufacturer_id"])
    cur = conn.execute(
        """
        INSERT INTO dim_fda_manufacturer(
            manufacturer_name, manufacturer_name_norm, fei_number, mapping_confidence,
            mapping_method, created_at, updated_at
        )
        VALUES (?, ?, ?, 0.0, 'unmapped', ?, ?)
        """,
        (name, norm, fei, now, now),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else None


def upsert_classification(conn: Any, payload: dict[str, Any], *, source_id: str) -> int:
    product_code = normalize_product_code(field(payload, "product_code"))
    if not product_code:
        return 0
    upsert_product_code(
        conn,
        product_code=product_code,
        device_name=field(payload, "device_name", "openfda_device_name"),
        medical_specialty=field(payload, "medical_specialty", "medical_specialty_description"),
        device_class=field(payload, "device_class", "device_classification"),
        regulation_number=field(payload, "regulation_number"),
        source_id=source_id,
    )
    return 1


def upsert_approval(conn: Any, payload: dict[str, Any], *, endpoint_name: str, source_id: str) -> int:
    if (
        endpoint_name == "approvals_510k"
        or endpoint_name.startswith("target_510k_")
        or endpoint_name.startswith("target_entity_510k_")
    ):
        submission_number = field(payload, "k_number")
        submission_type = "510k"
        applicant = field(payload, "applicant")
        device_name = field(payload, "device_name")
        decision = field(payload, "decision_description", "decision_code")
        receipt_date = date_text(field(payload, "date_received"))
    else:
        pma_number = field(payload, "pma_number")
        supplement_number = field(payload, "supplement_number")
        submission_number = f"{pma_number}-{supplement_number}" if supplement_number else pma_number
        submission_type = "PMA_SUPPLEMENT" if supplement_number else "PMA"
        applicant = field(payload, "applicant")
        device_name = field(payload, "trade_name", "generic_name")
        decision = field(payload, "decision")
        receipt_date = date_text(field(payload, "date_received"))
    if not submission_number:
        return 0
    product_code = normalize_product_code(field(payload, "product_code"))
    if product_code:
        upsert_product_code(conn, product_code=product_code, device_name=device_name, source_id=source_id)
    manufacturer_id = upsert_manufacturer(conn, applicant)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO fact_fda_approval(
            company_id, fda_manufacturer_id, product_code, submission_number, submission_type,
            decision_date, receipt_date, device_name, decision, source_id, payload_json, created_at, updated_at
        )
        VALUES (
            (SELECT parent_company_id FROM dim_fda_manufacturer WHERE fda_manufacturer_id = ?),
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(submission_number, submission_type) DO UPDATE SET
            company_id = excluded.company_id,
            fda_manufacturer_id = excluded.fda_manufacturer_id,
            product_code = excluded.product_code,
            decision_date = excluded.decision_date,
            receipt_date = excluded.receipt_date,
            device_name = excluded.device_name,
            decision = excluded.decision,
            source_id = excluded.source_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            manufacturer_id,
            manufacturer_id,
            product_code or None,
            submission_number,
            submission_type,
            date_text(field(payload, "decision_date")),
            receipt_date,
            device_name,
            decision,
            source_id,
            compact_json(payload),
            now,
            now,
        ),
    )
    return 1


def recall_key_part(raw: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(raw or "").upper()).strip()


def recall_key(payload: dict[str, Any], *, endpoint_name: str) -> str:
    # Endpoint is source evidence, not recall identity. Keeping it out of the key
    # lets downstream canonical scoring collapse recall/enforcement duplicates.
    primary = field(payload, "recall_number", "res_event_number")
    event_id = field(payload, "event_id")
    if primary:
        return f"recall_number:{recall_key_part(primary)}"
    if event_id:
        return f"event_id:{recall_key_part(event_id)}"
    material = compact_json(
        {
            "firm": normalize_org_name(field(payload, "recalling_firm", "firm_name")),
            "date": field(payload, "recall_initiation_date", "event_date_initiated"),
            "reason": normalize_org_name(field(payload, "reason_for_recall")),
            "product": normalize_org_name(field(payload, "product_description")),
        }
    )
    return f"hash:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def upsert_recall(conn: Any, payload: dict[str, Any], *, endpoint_name: str, source_id: str) -> int:
    firm = field(payload, "recalling_firm", "firm_name")
    manufacturer_id = upsert_manufacturer(conn, firm)
    product_code = normalize_product_code(field(payload, "product_code"))
    if product_code:
        upsert_product_code(conn, product_code=product_code, device_name=field(payload, "product_description"), source_id=source_id)
    key = recall_key(payload, endpoint_name=endpoint_name)
    classification = field(payload, "classification")
    row = conn.execute(
        "SELECT fda_recall_id FROM fact_fda_recall WHERE recall_key = ? AND source_id = ?",
        (key, source_id),
    ).fetchone()
    now = utc_now()
    values = (
        key,
        manufacturer_id,
        product_code or None,
        field(payload, "recall_number", "res_event_number"),
        field(payload, "event_id"),
        classification,
        severity_weight(classification),
        field(payload, "status", "recall_status"),
        firm,
        field(payload, "reason_for_recall"),
        date_text(field(payload, "recall_initiation_date", "event_date_initiated")),
        date_text(field(payload, "center_classification_date")),
        date_text(field(payload, "termination_date")),
        source_id,
        compact_json(payload),
        now,
    )
    if row is None:
        conn.execute(
            """
            INSERT INTO fact_fda_recall(
                recall_key, company_id, fda_manufacturer_id, product_code, recall_number, event_id,
                classification, severity_weight, status, recalling_firm, reason_for_recall,
                recall_initiation_date, center_classification_date, termination_date, source_id,
                payload_json, created_at, updated_at
            )
            VALUES (
                ?,
                (SELECT parent_company_id FROM dim_fda_manufacturer WHERE fda_manufacturer_id = ?),
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (values[0], values[1], *values[1:], now),
        )
    else:
        conn.execute(
            """
            UPDATE fact_fda_recall
            SET company_id = (SELECT parent_company_id FROM dim_fda_manufacturer WHERE fda_manufacturer_id = ?),
                fda_manufacturer_id = ?,
                product_code = ?,
                recall_number = ?,
                event_id = ?,
                classification = ?,
                severity_weight = ?,
                status = ?,
                recalling_firm = ?,
                reason_for_recall = ?,
                recall_initiation_date = ?,
                center_classification_date = ?,
                termination_date = ?,
                source_id = ?,
                payload_json = ?,
                updated_at = ?
            WHERE fda_recall_id = ?
            """,
            (values[1], *values[1:], int(row["fda_recall_id"])),
        )
    return 1


def flattened_text(raw: Any) -> str:
    parts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif value is not None:
            text = str(value).strip()
            if text:
                parts.append(text)

    visit(raw)
    return " ".join(parts).lower()


def representative_device(payload: dict[str, Any]) -> dict[str, Any]:
    devices = [json_dict(device) for device in json_list(payload.get("device"))]
    if not devices:
        return {}

    def rank(device: dict[str, Any]) -> tuple[int, int, int]:
        has_product_code = 1 if field(device, "device_report_product_code", "product_code") else 0
        has_maker = 1 if field(device, "manufacturer_d_name", "manufacturer_g1_name", "manufacturer_name") else 0
        has_brand = 1 if field(device, "brand_name", "generic_name") else 0
        return (has_product_code, has_maker, has_brand)

    devices.sort(key=rank, reverse=True)
    return devices[0]


def first_device(payload: dict[str, Any]) -> dict[str, Any]:
    return representative_device(payload)


def event_counts(payload: dict[str, Any]) -> tuple[int, int, int]:
    event_type = field(payload, "event_type", "type_of_report").lower()
    all_text = flattened_text(
        {
            "event_type": event_type,
            "patient": payload.get("patient"),
            "device": payload.get("device"),
            "mdr_text": payload.get("mdr_text"),
        }
    )
    death_terms = {"death", "fatal", "fatality", "deceased", "mortality", "died"}
    injury_terms = {"injury", "serious injury", "hospitalization", "disability", "intervention", "life threatening"}
    malfunction_terms = {"malfunction", "failure", "device malfunction", "failed", "broke", "breakage"}

    def has_term(text: str, terms: set[str]) -> bool:
        tokens = set(re.findall(r"[a-z]+", text))
        return any(term in text or term in tokens for term in terms)

    death = 1 if has_term(event_type, death_terms) or has_term(all_text, death_terms) else 0
    injury = 1 if has_term(event_type, injury_terms) or has_term(all_text, injury_terms) else 0
    malfunction = 1 if has_term(event_type, malfunction_terms) or has_term(all_text, malfunction_terms) else 0
    if death:
        injury = max(injury, 1)
    return death, injury, malfunction


def extract_problem_codes(raw: Any) -> list[str]:
    codes: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key).lower()
                if "code" in key_text and child is not None and not isinstance(child, (dict, list)):
                    text = str(child).strip()
                    if text:
                        codes.add(text)
                else:
                    visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(raw)
    return sorted(codes)


def upsert_adverse_event(conn: Any, payload: dict[str, Any], *, source_id: str) -> int:
    device = representative_device(payload)
    event_id = field(payload, "mdr_report_key", "report_number")
    if not event_id:
        event_id = hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()
    manufacturer = field(device, "manufacturer_d_name", "manufacturer_g1_name", "manufacturer_name")
    manufacturer_id = upsert_manufacturer(conn, manufacturer)
    product_code = normalize_product_code(field(device, "device_report_product_code", "product_code"))
    if product_code:
        upsert_product_code(conn, product_code=product_code, device_name=field(device, "brand_name", "generic_name"), source_id=source_id)
    death_count, injury_count, malfunction_count = event_counts(payload)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO fact_fda_adverse_event(
            adverse_event_id, company_id, fda_manufacturer_id, product_code, event_date,
            report_date, report_type, death_count, injury_count, malfunction_count, event_type,
            device_problem_codes, patient_problem_codes, source_id, payload_json, created_at, updated_at
        )
        VALUES (
            ?,
            (SELECT parent_company_id FROM dim_fda_manufacturer WHERE fda_manufacturer_id = ?),
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(adverse_event_id) DO UPDATE SET
            company_id = excluded.company_id,
            fda_manufacturer_id = excluded.fda_manufacturer_id,
            product_code = excluded.product_code,
            event_date = excluded.event_date,
            report_date = excluded.report_date,
            report_type = excluded.report_type,
            death_count = excluded.death_count,
            injury_count = excluded.injury_count,
            malfunction_count = excluded.malfunction_count,
            event_type = excluded.event_type,
            device_problem_codes = excluded.device_problem_codes,
            patient_problem_codes = excluded.patient_problem_codes,
            source_id = excluded.source_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            event_id,
            manufacturer_id,
            manufacturer_id,
            product_code or None,
            date_text(field(payload, "date_of_event")) or date_text(field(payload, "date_received", "date_report", "date_report_to_fda")),
            date_text(field(payload, "date_received", "date_report", "date_report_to_fda")),
            field(payload, "report_source_code", "report_type"),
            death_count,
            injury_count,
            malfunction_count,
            field(payload, "event_type"),
            compact_json(extract_problem_codes(device.get("device_problem_codes", []))),
            compact_json(extract_problem_codes(json_list(payload.get("patient")))),
            source_id,
            compact_json(payload),
            now,
            now,
        ),
    )
    return 1


def upsert_endpoint_records(conn: Any, endpoint_name: str, results: list[Any], *, source_id: str) -> int:
    count = 0
    for raw in results:
        payload = json_dict(raw)
        if not payload:
            continue
        if endpoint_name == "classification":
            count += upsert_classification(conn, payload, source_id=source_id)
        elif (
            endpoint_name == "approvals_510k"
            or endpoint_name.startswith("target_510k_")
            or endpoint_name.startswith("target_entity_510k_")
        ):
            count += upsert_approval(conn, payload, endpoint_name=endpoint_name, source_id=source_id)
        elif (
            endpoint_name == "approvals_pma"
            or endpoint_name.startswith("target_pma_")
            or endpoint_name.startswith("target_entity_pma_")
        ):
            count += upsert_approval(conn, payload, endpoint_name=endpoint_name, source_id=source_id)
        elif endpoint_name in {"recall", "enforcement"}:
            count += upsert_recall(conn, payload, endpoint_name=endpoint_name, source_id=source_id)
        elif endpoint_name == "adverse_event":
            count += upsert_adverse_event(conn, payload, source_id=source_id)
    return count


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in FIELDNAMES} for row in rows])


def process_fda_page(
    conn: Any,
    page: FetchedFdaPage,
    *,
    source_id: str,
    ingestion_run_id: int,
) -> tuple[int, int, str, str]:
    store_raw_response(
        conn,
        source_id=source_id,
        endpoint=page.url,
        query_params=page.public_params,
        response_status=page.response_status,
        payload_text=page.payload_text,
        ingestion_run_id=ingestion_run_id,
    )
    if page.response_status != 200:
        if page.response_status == 404 and is_openfda_not_found(page.payload):
            return 0, 0, "empty", "not_found_no_records"
        return 0, 0, "failed", f"http_status_{page.response_status}"
    results = json_list(page.payload.get("results"))
    if not results:
        return 0, 0, "empty", "no_results"
    upserted = upsert_endpoint_records(conn, page.endpoint_name, results, source_id=source_id)
    return len(results), upserted, "success", ""


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    policy = fda_policy(config)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "fda_core_ingestion.output_csv", "../output/med_devices_reports/med_device_fda_core_ingestion_coverage.csv"),
            base_dir=base_dir,
        )
    )
    endpoint_filter = {value.strip() for value in str(args.endpoints or "").split(",") if value.strip()}
    endpoints = (
        []
        if args.targeted_only
        else [endpoint for endpoint in policy.endpoints if endpoint.enabled and (not endpoint_filter or endpoint.name in endpoint_filter)]
    )
    if args.targeted_footprints or args.targeted_only:
        footprint_raw = str(
            cfg_get(
                config,
                "fda_core_ingestion.targeted_footprint_csv",
                cfg_get(config, "fda_features.footprint_csv", ""),
            )
            or ""
        ).strip()
        footprint_csv = (
            args.footprint_csv.expanduser().resolve()
            if args.footprint_csv
            else resolve_path(footprint_raw, base_dir=base_dir)
            if footprint_raw
            else None
        )
        targeted_endpoints = build_targeted_footprint_endpoints(
            footprint_csv,
            target_limit=max(1, args.target_limit),
            include_entity_names=args.targeted_entity_names,
        )
        if endpoint_filter:
            targeted_endpoints = [
                endpoint
                for endpoint in targeted_endpoints
                if endpoint.name in endpoint_filter
                or ("approvals_510k" in endpoint_filter and endpoint.name.startswith("target_510k_"))
                or ("approvals_pma" in endpoint_filter and endpoint.name.startswith("target_pma_"))
                or ("entity_510k" in endpoint_filter and endpoint.name.startswith("target_entity_510k_"))
                or ("entity_pma" in endpoint_filter and endpoint.name.startswith("target_entity_pma_"))
            ]
        endpoints.extend(targeted_endpoints)
    if not endpoints:
        raise ValueError("No FDA endpoints selected")
    api_key = resolve_api_key(config, policy=policy, base_dir=base_dir)
    if not api_key:
        LOGGER.warning(
            "No openFDA API key found. Set %s or create configured local key file for higher daily limits.",
            policy.api_key_env,
        )

    coverage_rows: list[dict[str, Any]] = []
    request_count = 0
    canonical_rows = 0
    failed: list[str] = []
    LOGGER.info("FDA core sync starting: db=%s endpoints=%d output=%s", db_path, len(endpoints), output_csv)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source_registry(conn, config, base_dir, policy.source_id)
        run_id = start_run(conn, run_type="sync_med_device_fda_core", input_path=config_path)
        ingestion_run_id = start_ingestion_run(conn, policy.source_id)
        try:
            for endpoint in endpoints:
                pages = 0
                seen = 0
                upserted = 0
                status = "success"
                reason = ""
                max_records = args.max_records if args.max_records > 0 else endpoint.max_records
                max_records = max_records if max_records > 0 else policy.page_limit
                try:
                    first_limit = min(policy.page_limit, max_records)
                    first_page = fetch_fda_page_job(
                        endpoint,
                        policy=policy,
                        api_key=api_key,
                        skip=0,
                        limit=first_limit,
                        page_number_hint=1,
                    )
                    request_count += 1
                    pages += 1
                    page_seen, page_upserted, page_status, page_reason = process_fda_page(
                        conn,
                        first_page,
                        source_id=policy.source_id,
                        ingestion_run_id=ingestion_run_id,
                    )
                    seen += page_seen
                    upserted += page_upserted
                    canonical_rows += page_upserted
                    if page_status == "failed":
                        status = "failed"
                        reason = page_reason
                        failed.append(endpoint.name)
                    elif page_status == "empty":
                        reason = page_reason
                    LOGGER.info(
                        "%s page=%d skip=%d records=%d upserted_total=%d",
                        endpoint.name,
                        pages,
                        first_page.skip,
                        page_seen,
                        upserted,
                    )

                    total = openfda_total(first_page.payload)
                    effective_max = min(max_records, total) if total is not None else max_records
                    if first_page.response_status == 200 and page_seen == policy.page_limit and effective_max > policy.page_limit:
                        page_jobs: list[tuple[int, int, int]] = []
                        for page_number, skip in enumerate(range(policy.page_limit, effective_max, policy.page_limit), start=2):
                            page_jobs.append((page_number, skip, min(policy.page_limit, effective_max - skip)))
                        if policy.parallel_workers > 1 and len(page_jobs) > 1:
                            LOGGER.info(
                                "Fetching FDA endpoint in parallel: endpoint=%s pages=%d workers=%d",
                                endpoint.name,
                                len(page_jobs) + 1,
                                policy.parallel_workers,
                            )
                            with ThreadPoolExecutor(max_workers=policy.parallel_workers) as executor:
                                futures = [
                                    executor.submit(
                                        fetch_fda_page_job,
                                        endpoint,
                                        policy=policy,
                                        api_key=api_key,
                                        skip=skip,
                                        limit=limit,
                                        page_number_hint=page_number,
                                    )
                                    for page_number, skip, limit in page_jobs
                                ]
                                for future in as_completed(futures):
                                    page = future.result()
                                    request_count += 1
                                    pages += 1
                                    page_seen, page_upserted, page_status, page_reason = process_fda_page(
                                        conn,
                                        page,
                                        source_id=policy.source_id,
                                        ingestion_run_id=ingestion_run_id,
                                    )
                                    seen += page_seen
                                    upserted += page_upserted
                                    canonical_rows += page_upserted
                                    if page_status == "failed":
                                        status = "failed"
                                        reason = page_reason
                                        if endpoint.name not in failed:
                                            failed.append(endpoint.name)
                                    LOGGER.info(
                                        "%s page=%d skip=%d records=%d upserted_total=%d",
                                        endpoint.name,
                                        page.page_number_hint,
                                        page.skip,
                                        page_seen,
                                        upserted,
                                    )
                                    if pages % policy.commit_every_pages == 0:
                                        conn.commit()
                                        LOGGER.info("Committed FDA sync progress: endpoint=%s pages=%d", endpoint.name, pages)
                        else:
                            for page_number, skip, limit in page_jobs:
                                time.sleep(max(0.0, policy.sleep_sec))
                                page = fetch_fda_page_job(
                                    endpoint,
                                    policy=policy,
                                    api_key=api_key,
                                    skip=skip,
                                    limit=limit,
                                    page_number_hint=page_number,
                                )
                                request_count += 1
                                pages += 1
                                page_seen, page_upserted, page_status, page_reason = process_fda_page(
                                    conn,
                                    page,
                                    source_id=policy.source_id,
                                    ingestion_run_id=ingestion_run_id,
                                )
                                seen += page_seen
                                upserted += page_upserted
                                canonical_rows += page_upserted
                                if page_status == "failed":
                                    status = "failed"
                                    reason = page_reason
                                    if endpoint.name not in failed:
                                        failed.append(endpoint.name)
                                    break
                                LOGGER.info(
                                    "%s page=%d skip=%d records=%d upserted_total=%d",
                                    endpoint.name,
                                    page.page_number_hint,
                                    page.skip,
                                    page_seen,
                                    upserted,
                                )
                                if pages % policy.commit_every_pages == 0:
                                    conn.commit()
                                    LOGGER.info("Committed FDA sync progress: endpoint=%s pages=%d", endpoint.name, pages)
                except Exception as exc:
                    status = "failed"
                    reason = f"{type(exc).__name__}: {exc}"
                    failed.append(endpoint.name)
                    LOGGER.warning("FDA endpoint failed: %s %s", endpoint.name, exc)
                coverage_rows.append(
                    {
                        "endpoint_name": endpoint.name,
                        "path": endpoint.path,
                        "search": endpoint.search,
                        "status": status,
                        "pages_fetched": pages,
                        "api_records_seen": seen,
                        "canonical_rows_upserted": upserted,
                        "review_reason": reason,
                    }
                )
                conn.commit()
            status = "partial" if failed else "success"
            message = f"endpoints={len(endpoints)} requests={request_count} canonical_rows={canonical_rows} output={output_csv}"
            if failed:
                message += " failed_endpoints=" + ",".join(sorted(set(failed)))
            finish_ingestion_run(
                conn,
                ingestion_run_id=ingestion_run_id,
                status=status,
                request_count=request_count,
                row_count=canonical_rows,
                message=message,
            )
            finish_run(conn, run_id=run_id, status=status, row_count=canonical_rows, message=message)
        except BaseException as exc:
            message = f"{type(exc).__name__}: {exc}"
            finish_ingestion_run(
                conn,
                ingestion_run_id=ingestion_run_id,
                status="failed",
                request_count=request_count,
                row_count=canonical_rows,
                message=message,
            )
            finish_run(conn, run_id=run_id, status="failed", row_count=canonical_rows, message=message)
            raise
    write_csv(output_csv, coverage_rows)
    LOGGER.info("FDA core sync complete: rows=%d output=%s failed=%d", canonical_rows, output_csv, len(failed))
    if failed and not args.allow_partial:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
