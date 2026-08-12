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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
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
from med_devices.core.point_in_time import row_is_effective_asof  # noqa: E402
from med_devices.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from med_devices.core.text_norm import (  # noqa: E402
    as_bool,
    normalize_org_name,
    normalize_submission_identifier,
    normalize_ticker,
)


LOGGER = logging.getLogger("sync_med_device_fda_core")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_SOURCE_ID = "openfda_device"
DEFAULT_BASE_URL = "https://api.fda.gov/device"
PMA_IDENTIFIER_RE = re.compile(r"^(?:BP|P|H|N|D)[0-9]{5,}$")
FDA_510K_OR_DENOVO_IDENTIFIER_RE = re.compile(r"^(?:K[0-9]{5,}|DEN[0-9]{6})$")
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
FDA_RECALL_LOOKUP_SQL = """
    SELECT fda_recall_id
    FROM fact_fda_recall
    WHERE recall_key IS NOT NULL
      AND recall_key != ''
      AND recall_key = ?
      AND source_id = ?
      AND COALESCE(endpoint_name, '') = ?
"""
FDA_RECALL_LEGACY_ENDPOINT_LOOKUP_SQL = """
    SELECT fda_recall_id
    FROM fact_fda_recall
    WHERE recall_key IS NOT NULL
      AND recall_key != ''
      AND recall_key = ?
      AND source_id = ?
      AND COALESCE(endpoint_name, '') = ''
    ORDER BY fda_recall_id
    LIMIT 1
"""


@dataclass(frozen=True)
class EndpointConfig:
    name: str
    path: str
    enabled: bool
    search: str
    sort: str
    max_records: int
    date_field: str = ""
    window_days: int = 0
    overlap_days: int = 0
    initial_lookback_days: int = 0
    stream_name: str = ""
    scope_hash: str = ""
    window_start: str = ""
    window_end: str = ""
    partition_field: str = ""
    partition_width: int = 0
    scope_product_code_field: str = ""
    scope_manufacturer_field: str = ""
    scope_group_size: int = 0


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
    replayed: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync openFDA core medical-device data into canonical med-device tables."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--endpoints", type=str, default="", help="Optional comma-separated endpoint names.")
    parser.add_argument("--max-records", type=int, default=0, help="Optional per-endpoint max record override.")
    parser.add_argument(
        "--asof",
        type=str,
        default="",
        help="Target as-of date (YYYY-MM-DD) used to partition and replay raw FDA responses.",
    )
    parser.add_argument(
        "--refresh-network",
        action="store_true",
        help="Bypass hash-validated same-date raw responses and fetch every selected FDA request again.",
    )
    parser.add_argument(
        "--incremental-start",
        type=str,
        default="",
        help="Optional inclusive YYYY-MM-DD override for dated FDA streams (governed repair/backfill only).",
    )
    parser.add_argument(
        "--targeted-footprints", action="store_true", help="Fetch exact FDA submission IDs from the footprint CSV."
    )
    parser.add_argument(
        "--targeted-entity-names",
        action="store_true",
        help="Also fetch exact applicant-name rows for device footprint entities.",
    )
    parser.add_argument(
        "--targeted-postmarket",
        action="store_true",
        help="Also fetch recall, enforcement, and adverse-event rows for targeted footprint product codes and entities.",
    )
    parser.add_argument(
        "--targeted-tickers",
        type=str,
        default="",
        help="Optional comma-separated ticker filter for targeted footprint queries.",
    )
    parser.add_argument(
        "--targeted-only", action="store_true", help="Only run targeted footprint fetches, not the broad endpoint sync."
    )
    parser.add_argument("--footprint-csv", type=Path, default=None, help="Optional FDA footprint CSV override.")
    parser.add_argument("--target-limit", type=int, default=0, help="Max records per targeted footprint query.")
    parser.add_argument(
        "--recompute-adverse-event-counts-only",
        action="store_true",
        help="Recompute canonical adverse-event severity counts from stored structured FDA payloads without fetching.",
    )
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


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
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def upsert_manual_approval_evidence(conn: Any, path: Path | None, *, source_id: str) -> int:
    if path is None or not path.exists():
        return 0
    count = 0
    for row in read_csv_flexible(path):
        pma_number = normalize_submission_identifier(row_get(row, "pma_number", "submission_number"))
        supplement_number = normalize_submission_identifier(row_get(row, "supplement_number"))
        if not is_pma_identifier(pma_number):
            LOGGER.warning("Skipping invalid manual PMA identifier: %s", pma_number)
            continue
        applicant = row_get(row, "applicant", "manufacturer_name")
        product_code = normalize_product_code(row_get(row, "product_code"))
        decision_date = date_text(row_get(row, "decision_date"))
        source_url = row_get(row, "source_url")
        if not applicant or not product_code or not decision_date or not source_url:
            LOGGER.warning(
                "Skipping incomplete manual PMA evidence: pma=%s applicant=%s product_code=%s decision_date=%s source_url=%s",
                pma_number,
                applicant,
                product_code,
                decision_date,
                source_url,
            )
            continue
        payload = {
            "pma_number": pma_number,
            "supplement_number": supplement_number,
            "applicant": applicant,
            "product_code": product_code,
            "trade_name": row_get(row, "trade_name", "device_name"),
            "decision": row_get(row, "decision") or "Approved",
            "decision_date": decision_date,
            "date_received": date_text(row_get(row, "date_received", "receipt_date")),
            "source_url": source_url,
            "valid_from": row_get(row, "valid_from"),
            "reviewed_at": row_get(row, "reviewed_at"),
            "evidence_method": "authoritative_manual_fda_accessdata",
        }
        count += upsert_approval(
            conn,
            payload,
            endpoint_name="approvals_pma",
            source_id=source_id,
        )
    return count


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


def ticker_filter(raw: object) -> set[str]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    return {normalize_ticker(value) for value in values if normalize_ticker(value)}


def is_pma_identifier(raw: object) -> bool:
    return bool(PMA_IDENTIFIER_RE.fullmatch(normalize_submission_identifier(raw)))


def is_510k_or_denovo_identifier(raw: object) -> bool:
    return bool(FDA_510K_OR_DENOVO_IDENTIFIER_RE.fullmatch(normalize_submission_identifier(raw)))


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
    text = re.sub(r"[^a-z0-9]+", "_", str(classification or "").strip().lower()).strip("_")
    if text in {"i", "class_i", "class_1", "classi", "class1"}:
        return 5.0
    if text in {"ii", "class_ii", "class_2", "classii", "class2"}:
        return 2.0
    if text in {"iii", "class_iii", "class_3", "classiii", "class3"}:
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
                    date_field=str(row.get("date_field") or "").strip(),
                    window_days=max(0, int(row.get("window_days") or 0)),
                    overlap_days=max(0, int(row.get("overlap_days") or 0)),
                    initial_lookback_days=max(0, int(row.get("initial_lookback_days") or 0)),
                    partition_field=str(row.get("partition_field") or "").strip(),
                    partition_width=max(0, int(row.get("partition_width") or 0)),
                    scope_product_code_field=str(row.get("scope_product_code_field") or "").strip(),
                    scope_manufacturer_field=str(row.get("scope_manufacturer_field") or "").strip(),
                    scope_group_size=max(0, int(row.get("scope_group_size") or 0)),
                )
            )
    return FdaPolicy(
        source_id=str(cfg_get(config, "fda_core_ingestion.source_id", DEFAULT_SOURCE_ID) or DEFAULT_SOURCE_ID),
        base_url=str(cfg_get(config, "fda_core_ingestion.base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
        api_key_env=str(cfg_get(config, "fda_core_ingestion.api_key_env", "OPENFDA_API_KEY") or "OPENFDA_API_KEY"),
        api_key_file=str(
            cfg_get(config, "fda_core_ingestion.api_key_file", "secrets.local.yaml") or "secrets.local.yaml"
        ),
        api_key_file_field=str(
            cfg_get(config, "fda_core_ingestion.api_key_file_field", "openfda_api_key") or "openfda_api_key"
        ),
        timeout_sec=float(cfg_get(config, "fda_core_ingestion.timeout_sec", 30.0)),
        max_retries=int(cfg_get(config, "fda_core_ingestion.max_retries", 3)),
        parallel_workers=max(1, int(cfg_get(config, "fda_core_ingestion.parallel_workers", 1))),
        sleep_sec=float(cfg_get(config, "fda_core_ingestion.request_sleep_sec", 0.15)),
        page_limit=max(1, min(1000, int(cfg_get(config, "fda_core_ingestion.page_limit", 1000)))),
        commit_every_pages=max(1, int(cfg_get(config, "fda_core_ingestion.commit_every_pages", 10))),
        user_agent=str(
            cfg_get(config, "fda_core_ingestion.user_agent", "JL, Independent Research, jm.357@hotmail.com")
        ),
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


def start_ingestion_run(conn: Any, source_id: str, *, stale_after_hours: int = 6) -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = conn.execute(
        """
        SELECT ingestion_run_id, started_at
        FROM ingestion_runs
        WHERE source_id = ? AND status = 'running'
        ORDER BY ingestion_run_id
        """,
        (source_id,),
    ).fetchall()
    active: list[int] = []
    for row in rows:
        try:
            started = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except ValueError:
            active.append(int(row["ingestion_run_id"]))
            continue
        age_hours = (now - started.astimezone(timezone.utc)).total_seconds() / 3600.0
        if age_hours >= stale_after_hours:
            conn.execute(
                """
                UPDATE ingestion_runs
                SET completed_at = ?, status = 'failed', message = ?
                WHERE ingestion_run_id = ? AND status = 'running'
                """,
                (utc_now(), f"stale_running_run_recovered_after_{age_hours:.1f}_hours", row["ingestion_run_id"]),
            )
        else:
            active.append(int(row["ingestion_run_id"]))
    if active:
        raise RuntimeError(f"FDA ingestion already running for {source_id}: run_ids={active}")
    now_text = utc_now()
    cur = conn.execute(
        "INSERT INTO ingestion_runs(source_id, started_at, status, created_at) VALUES (?, ?, 'running', ?)",
        (source_id, now_text, now_text),
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
    asof_date: str,
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
            asof_date,
            payload_text,
            ingestion_run_id,
            now,
        ),
    )


ReplayCache = dict[tuple[str, str], tuple[int, str, dict[str, Any]]]


def replay_cache_key(endpoint: str, query_params: dict[str, Any]) -> tuple[str, str]:
    return endpoint, compact_json(query_params)


def ingestion_response_set(conn: Any, ingestion_run_id: int) -> tuple[int, str]:
    rows = conn.execute(
        """
        SELECT endpoint, COALESCE(query_params_json, '') AS query_params_json,
               COALESCE(response_status, 0) AS response_status, response_hash
        FROM raw_api_responses
        WHERE ingestion_run_id = ?
        ORDER BY endpoint, query_params_json, response_status, response_hash
        """,
        (ingestion_run_id,),
    ).fetchall()
    material = [
        [str(row["endpoint"]), str(row["query_params_json"]), int(row["response_status"]), str(row["response_hash"])]
        for row in rows
    ]
    return len(material), hashlib.sha256(compact_json(material).encode("utf-8")).hexdigest()


def seal_ingestion_run(conn: Any, *, ingestion_run_id: int, source_id: str, asof_date: str) -> str:
    status_row = conn.execute(
        "SELECT status, source_id FROM ingestion_runs WHERE ingestion_run_id = ?",
        (ingestion_run_id,),
    ).fetchone()
    if status_row is None:
        raise ValueError(f"Unknown ingestion_run_id: {ingestion_run_id}")
    if str(status_row["source_id"]) != source_id:
        raise ValueError("Ingestion-run source does not match seal source")
    if str(status_row["status"]) not in {"success", "partial"}:
        raise ValueError("Only completed success/partial ingestion runs may be sealed")
    response_count, response_set_hash = ingestion_response_set(conn, ingestion_run_id)
    conn.execute(
        """
        INSERT INTO ingestion_run_seals(
            ingestion_run_id, source_id, asof_date, response_count, response_set_hash, sealed_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ingestion_run_id) DO UPDATE SET
            source_id = excluded.source_id,
            asof_date = excluded.asof_date,
            response_count = excluded.response_count,
            response_set_hash = excluded.response_set_hash,
            sealed_at = excluded.sealed_at
        """,
        (ingestion_run_id, source_id, asof_date, response_count, response_set_hash, utc_now()),
    )
    return response_set_hash


def load_raw_response_replay_cache(
    conn: Any,
    *,
    source_id: str,
    asof_date: str,
) -> ReplayCache:
    """Load only complete, set-sealed same-date responses for deterministic replay."""

    seal_rows = conn.execute(
        """
        SELECT s.ingestion_run_id, s.response_count, s.response_set_hash
        FROM ingestion_run_seals AS s
        JOIN ingestion_runs AS r ON r.ingestion_run_id = s.ingestion_run_id
        WHERE s.source_id = ? AND s.asof_date = ? AND r.status IN ('success', 'partial')
        ORDER BY s.ingestion_run_id
        """,
        (source_id, asof_date),
    ).fetchall()
    cache: ReplayCache = {}
    invalid_runs = 0
    invalid_rows = 0
    for seal in seal_rows:
        run_id = int(seal["ingestion_run_id"])
        count, digest = ingestion_response_set(conn, run_id)
        if count != int(seal["response_count"]) or digest != str(seal["response_set_hash"]):
            invalid_runs += 1
            continue
        rows = conn.execute(
            """
            SELECT endpoint, query_params_json, response_status, response_hash, payload_text
            FROM raw_api_responses
            WHERE ingestion_run_id = ? AND response_status IN (200, 404)
            ORDER BY raw_response_id
            """,
            (run_id,),
        ).fetchall()
        run_cache: ReplayCache = {}
        run_valid = True
        for row in rows:
            payload_text = str(row["payload_text"] or "")
            expected_hash = str(row["response_hash"] or "")
            actual_hash = hashlib.sha256(payload_text.encode("utf-8", errors="replace")).hexdigest()
            if not payload_text or actual_hash != expected_hash:
                invalid_rows += 1
                run_valid = False
                break
            try:
                parsed = json.loads(payload_text)
            except json.JSONDecodeError:
                invalid_rows += 1
                run_valid = False
                break
            endpoint = str(row["endpoint"] or "")
            query_json = str(row["query_params_json"] or "")
            if not isinstance(parsed, dict) or not endpoint or not query_json:
                invalid_rows += 1
                run_valid = False
                break
            run_cache[(endpoint, query_json)] = (int(row["response_status"]), payload_text, parsed)
        if run_valid:
            cache.update(run_cache)
        else:
            invalid_runs += 1
    if invalid_runs or invalid_rows:
        LOGGER.warning(
            "Ignored invalid same-date FDA sealed response sets: asof=%s runs=%d rows=%d",
            asof_date,
            invalid_runs,
            invalid_rows,
        )
    return cache


def endpoint_scope_hash(endpoint: EndpointConfig) -> str:
    payload = {
        "name": endpoint.name,
        "path": endpoint.path,
        "search": endpoint.search,
        "sort": endpoint.sort,
        "date_field": endpoint.date_field,
        "window_days": endpoint.window_days,
        "overlap_days": endpoint.overlap_days,
        "partition_field": endpoint.partition_field,
        "partition_width": endpoint.partition_width,
    }
    return hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()


def combine_search(base_search: str, date_field: str, start_date: date, end_date: date) -> str:
    date_clause = f"{date_field}:[{start_date:%Y%m%d} TO {end_date:%Y%m%d}]"
    return f"{base_search} AND {date_clause}" if base_search else date_clause


def anchored_date_windows(start_date: date, end_date: date, window_days: int) -> list[tuple[date, date]]:
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if start_date > end_date:
        return []
    epoch = date(1970, 1, 1)
    bucket_start = epoch + timedelta(days=((start_date - epoch).days // window_days) * window_days)
    out: list[tuple[date, date]] = []
    cursor = bucket_start
    while cursor <= end_date:
        bucket_end = min(cursor + timedelta(days=window_days - 1), end_date)
        out.append((cursor, bucket_end))
        cursor += timedelta(days=window_days)
    return out


def get_ingestion_watermark(conn: Any, *, source_id: str, stream_name: str, scope_hash: str) -> date | None:
    row = conn.execute(
        """
        SELECT watermark_date FROM ingestion_watermarks
        WHERE source_id = ? AND stream_name = ? AND scope_hash = ?
        """,
        (source_id, stream_name, scope_hash),
    ).fetchone()
    return date.fromisoformat(str(row["watermark_date"])) if row is not None else None


def upsert_ingestion_watermark(
    conn: Any,
    *,
    source_id: str,
    stream_name: str,
    scope_hash: str,
    date_field: str,
    watermark_date: str,
    ingestion_run_id: int,
) -> None:
    existing = get_ingestion_watermark(conn, source_id=source_id, stream_name=stream_name, scope_hash=scope_hash)
    candidate = date.fromisoformat(watermark_date)
    if existing is not None and candidate < existing:
        return
    conn.execute(
        """
        INSERT INTO ingestion_watermarks(
            source_id, stream_name, scope_hash, date_field, watermark_date,
            last_ingestion_run_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, stream_name, scope_hash) DO UPDATE SET
            date_field = excluded.date_field,
            watermark_date = excluded.watermark_date,
            last_ingestion_run_id = excluded.last_ingestion_run_id,
            updated_at = excluded.updated_at
        """,
        (source_id, stream_name, scope_hash, date_field, watermark_date, ingestion_run_id, utc_now()),
    )


def plan_incremental_endpoints(
    conn: Any,
    endpoints: list[EndpointConfig],
    *,
    source_id: str,
    run_asof: date,
    start_override: date | None = None,
) -> list[EndpointConfig]:
    planned: list[EndpointConfig] = []
    for endpoint in endpoints:
        if not endpoint.date_field or endpoint.window_days <= 0:
            planned.append(endpoint)
            continue
        scope_hash = endpoint_scope_hash(endpoint)
        watermark = get_ingestion_watermark(conn, source_id=source_id, stream_name=endpoint.name, scope_hash=scope_hash)
        if watermark is not None and watermark > run_asof:
            raise ValueError(
                f"FDA watermark is after run as-of: stream={endpoint.name} watermark={watermark} asof={run_asof}"
            )
        if start_override is not None:
            start_date = start_override
        elif watermark is not None:
            start_date = watermark - timedelta(days=max(0, endpoint.overlap_days - 1))
        else:
            lookback = max(endpoint.initial_lookback_days, endpoint.window_days, 1)
            start_date = run_asof - timedelta(days=lookback - 1)
        for window_start, window_end in anchored_date_windows(start_date, run_asof, endpoint.window_days):
            planned.append(
                replace(
                    endpoint,
                    search=combine_search(endpoint.search, endpoint.date_field, window_start, window_end),
                    stream_name=endpoint.name,
                    scope_hash=scope_hash,
                    window_start=window_start.isoformat(),
                    window_end=window_end.isoformat(),
                )
            )
    return planned


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
    include_postmarket: bool = False,
    tickers: set[str] | None = None,
    asof: date | None = None,
) -> list[EndpointConfig]:
    if path is None:
        return []
    if not path.exists():
        LOGGER.warning("Configured FDA footprint CSV does not exist: %s", path)
        return []
    target_asof = asof or datetime.now(timezone.utc).date()
    effective_rows: dict[str, dict[str, str]] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker", "symbol"))
        if not ticker or (tickers and ticker not in tickers):
            continue
        if not row_is_effective_asof(row, target_asof, include_missing=True):
            continue
        # Governed CSV updates are append-only. The last effective row for a
        # ticker supersedes older rows while retaining those rows for PIT runs.
        effective_rows[ticker] = row

    out: list[EndpointConfig] = []
    seen: set[tuple[str, str]] = set()
    for ticker, row in effective_rows.items():
        entity = row_get(row, "primary_fda_entity", "fda_entity", "manufacturer_name")
        expected_records = as_bool(row_get(row, "expected_cdrh_records"), default=False)
        for raw_identifier in split_multi_value(row_get(row, "premarket_numbers", "premarket_number")):
            identifier = normalize_submission_identifier(raw_identifier)
            if not identifier:
                continue
            if is_510k_or_denovo_identifier(identifier):
                endpoint = EndpointConfig(
                    name=f"target_510k_{identifier}",
                    path="510k.json",
                    enabled=True,
                    search=f"k_number:{quote_openfda_value(identifier)}",
                    sort="",
                    max_records=max(1, target_limit),
                )
            elif is_pma_identifier(identifier):
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
        if include_entity_names and expected_records and entity:
            slug = endpoint_slug(ticker + "_" + entity)
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
        if include_postmarket and expected_records:
            for raw_product_code in split_multi_value(row_get(row, "product_codes", "product_code")):
                product_code = normalize_product_code(raw_product_code)
                if not product_code:
                    continue
                slug = endpoint_slug(ticker + "_" + product_code)
                for name, path_name, field_name, sort in (
                    ("recall", "recall.json", "product_code", "event_date_initiated:desc"),
                    ("enforcement", "enforcement.json", "product_code", "recall_initiation_date:desc"),
                    ("event", "event.json", "device.device_report_product_code", "date_received:desc"),
                ):
                    search_parts = [f"{field_name}:{quote_openfda_value(product_code)}"]
                    if entity:
                        entity_field = "device.manufacturer_d_name" if name == "event" else "recalling_firm"
                        search_parts.append(f"{entity_field}:{quote_openfda_value(entity)}")
                    endpoint = EndpointConfig(
                        name=f"target_{name}_code_{slug}",
                        path=path_name,
                        enabled=True,
                        search=" AND ".join(search_parts),
                        sort=sort,
                        max_records=max(1, target_limit),
                    )
                    key = (endpoint.path, endpoint.search)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(endpoint)
            if entity:
                slug = endpoint_slug(ticker + "_" + entity)
                for name, path_name, field_name, sort in (
                    ("recall", "recall.json", "recalling_firm", "event_date_initiated:desc"),
                    ("enforcement", "enforcement.json", "recalling_firm", "recall_initiation_date:desc"),
                    ("event", "event.json", "device.manufacturer_d_name", "date_received:desc"),
                ):
                    endpoint = EndpointConfig(
                        name=f"target_{name}_entity_{slug}",
                        path=path_name,
                        enabled=True,
                        search=f"{field_name}:{quote_openfda_value(entity)}",
                        sort=sort,
                        max_records=max(1, target_limit),
                    )
                    key = (endpoint.path, endpoint.search)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(endpoint)
    LOGGER.info("Built targeted FDA footprint queries: rows=%d path=%s", len(out), path)
    return out


def scope_endpoints_to_footprint_product_codes(
    endpoints: list[EndpointConfig],
    footprint_path: Path | None,
    *,
    asof: date,
    alias_path: Path | None = None,
) -> list[EndpointConfig]:
    scoped_templates = [endpoint for endpoint in endpoints if endpoint.scope_product_code_field]
    if not scoped_templates:
        return endpoints
    if footprint_path is None or not footprint_path.exists():
        raise ValueError("A governed FDA footprint CSV is required for scoped broad endpoint ingestion")
    aliases_by_ticker: dict[str, set[str]] = {}
    if alias_path is not None and alias_path.exists():
        for row in read_csv_flexible(alias_path):
            if not row_is_effective_asof(row, asof, include_missing=True):
                continue
            ticker = normalize_ticker(row_get(row, "ticker", "symbol"))
            alias = row_get(row, "alias_raw", "alias", "manufacturer_name")
            if ticker and alias:
                aliases_by_ticker.setdefault(ticker, set()).add(alias)
    code_entity_pairs: set[tuple[str, str]] = set()
    for row in read_csv_flexible(footprint_path):
        if not row_is_effective_asof(row, asof, include_missing=True):
            continue
        ticker = normalize_ticker(row_get(row, "ticker", "symbol"))
        primary = row_get(row, "primary_fda_entity", "fda_entity", "manufacturer_name")
        entities = set(aliases_by_ticker.get(ticker, set()))
        if primary:
            entities.add(primary)
        for raw_code in split_multi_value(row_get(row, "product_codes", "product_code")):
            code = normalize_product_code(raw_code)
            if code:
                for entity in entities or {""}:
                    code_entity_pairs.add((code, entity))
    if not code_entity_pairs:
        raise ValueError(f"No effective FDA product-code/entity pairs found in governed footprint: {footprint_path}")
    ordered_pairs = sorted(code_entity_pairs)
    out: list[EndpointConfig] = []
    for endpoint in endpoints:
        code_field = endpoint.scope_product_code_field
        if not code_field:
            out.append(endpoint)
            continue
        manufacturer_field = endpoint.scope_manufacturer_field
        if not manufacturer_field:
            raise ValueError(f"Scoped FDA endpoint lacks manufacturer field: {endpoint.name}")
        group_size = max(1, endpoint.scope_group_size)
        for group_number, offset in enumerate(range(0, len(ordered_pairs), group_size), start=1):
            group = ordered_pairs[offset : offset + group_size]
            clauses: list[str] = []
            for code, entity in group:
                code_clause = f"{code_field}:{quote_openfda_value(code)}"
                clauses.append(
                    f"({code_clause} AND {manufacturer_field}:{quote_openfda_value(entity)})" if entity else code_clause
                )
            pair_filter = "(" + " OR ".join(clauses) + ")"
            scoped_search = f"{endpoint.search} AND {pair_filter}" if endpoint.search else pair_filter
            out.append(
                replace(
                    endpoint,
                    name=f"target_event_group_{group_number:03d}",
                    search=scoped_search,
                    scope_product_code_field="",
                    scope_manufacturer_field="",
                    scope_group_size=0,
                )
            )
    LOGGER.info(
        "Scoped FDA endpoints to governed product-code/entity pairs: pairs=%d templates=%d endpoints=%d",
        len(ordered_pairs),
        len(scoped_templates),
        len(out),
    )
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
    replay_cache: ReplayCache | None = None,
    refresh_network: bool = False,
) -> FetchedFdaPage:
    url = endpoint_url(policy, endpoint)
    params, public_params = page_params(endpoint, skip=skip, limit=limit, api_key=api_key)
    if not refresh_network and replay_cache is not None:
        cached = replay_cache.get(replay_cache_key(url, public_params))
        if cached is not None:
            status_code, payload_text, payload = cached
            return FetchedFdaPage(
                endpoint_name=endpoint.name,
                url=url,
                public_params=public_params,
                skip=skip,
                page_number_hint=page_number_hint,
                response_status=status_code,
                payload_text=payload_text,
                payload=payload,
                replayed=True,
            )
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
        submission_type = "DENOVO" if submission_number.upper().startswith("DEN") else "510k"
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


def find_existing_recall_row(conn: Any, *, key: str, source_id: str, endpoint_name: str) -> Any:
    row = conn.execute(FDA_RECALL_LOOKUP_SQL, (key, source_id, endpoint_name)).fetchone()
    if row is not None:
        return row
    return conn.execute(FDA_RECALL_LEGACY_ENDPOINT_LOOKUP_SQL, (key, source_id)).fetchone()


def upsert_recall(conn: Any, payload: dict[str, Any], *, endpoint_name: str, source_id: str) -> int:
    firm = field(payload, "recalling_firm", "firm_name")
    manufacturer_id = upsert_manufacturer(conn, firm)
    product_code = normalize_product_code(field(payload, "product_code"))
    if product_code:
        upsert_product_code(
            conn, product_code=product_code, device_name=field(payload, "product_description"), source_id=source_id
        )
    key = recall_key(payload, endpoint_name=endpoint_name)
    classification = field(payload, "classification")
    row = find_existing_recall_row(conn, key=key, source_id=source_id, endpoint_name=endpoint_name)
    now = utc_now()
    values = (
        key,
        endpoint_name,
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
                recall_key, endpoint_name, company_id, fda_manufacturer_id, product_code, recall_number, event_id,
                classification, severity_weight, status, recalling_firm, reason_for_recall,
                recall_initiation_date, center_classification_date, termination_date, source_id,
                payload_json, created_at, updated_at
            )
            VALUES (
                ?, ?,
                (SELECT parent_company_id FROM dim_fda_manufacturer WHERE fda_manufacturer_id = ?),
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (values[0], values[1], values[2], *values[2:], now),
        )
    else:
        conn.execute(
            """
            UPDATE fact_fda_recall
            SET endpoint_name = ?,
                company_id = (SELECT parent_company_id FROM dim_fda_manufacturer WHERE fda_manufacturer_id = ?),
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
            (values[1], values[2], *values[2:], int(row["fda_recall_id"])),
        )
    return 1


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


def structured_patient_outcomes(payload: dict[str, Any]) -> set[str]:
    outcomes: set[str] = set()
    for raw_patient in json_list(payload.get("patient")):
        patient = json_dict(raw_patient)
        for key in ("sequence_number_outcome", "patient_outcome", "outcome"):
            raw_outcomes = patient.get(key)
            values = raw_outcomes if isinstance(raw_outcomes, list) else [raw_outcomes]
            for raw_value in values:
                value = re.sub(r"^\s*\d+\s*[.)-]?\s*", "", str(raw_value or "")).strip().lower()
                if value:
                    outcomes.add(value)
    return outcomes


def event_counts(payload: dict[str, Any]) -> tuple[int, int, int]:
    event_type = re.sub(r"[^a-z]+", "_", field(payload, "event_type").lower()).strip("_")
    patient_outcomes = structured_patient_outcomes(payload)
    death_outcomes = {"d", "death", "deceased", "fatal", "fatality"}

    # FDA event_type and structured patient outcomes are authoritative. Free-text
    # narratives are intentionally excluded: substring matching previously
    # classified words such as "studied" as containing the death term "died".
    death = int(event_type == "death" or bool(patient_outcomes.intersection(death_outcomes)))
    injury = int(event_type in {"injury", "serious_injury"})
    malfunction = int(event_type == "malfunction")
    return death, injury, malfunction


def recompute_adverse_event_counts(conn: Any) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT adverse_event_id, death_count, injury_count, malfunction_count, payload_json
        FROM fact_fda_adverse_event
        """
    ).fetchall()
    updates: list[tuple[int, int, int, str, str]] = []
    invalid_payloads = 0
    now = utc_now()
    for row in rows:
        try:
            payload = json_dict(json.loads(str(row["payload_json"] or "{}")))
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_payloads += 1
            continue
        counts = event_counts(payload)
        existing = (
            int(row["death_count"] or 0),
            int(row["injury_count"] or 0),
            int(row["malfunction_count"] or 0),
        )
        if counts != existing:
            updates.append((*counts, now, str(row["adverse_event_id"])))
    if updates:
        conn.executemany(
            """
            UPDATE fact_fda_adverse_event
            SET death_count = ?,
                injury_count = ?,
                malfunction_count = ?,
                updated_at = ?
            WHERE adverse_event_id = ?
            """,
            updates,
        )
    return len(updates), invalid_payloads


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
        upsert_product_code(
            conn,
            product_code=product_code,
            device_name=field(device, "brand_name", "generic_name"),
            source_id=source_id,
        )
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
            date_text(field(payload, "date_of_event"))
            or date_text(field(payload, "date_received", "date_report", "date_report_to_fda")),
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
        elif (
            endpoint_name in {"recall", "enforcement"}
            or endpoint_name.startswith("target_recall_")
            or endpoint_name.startswith("target_enforcement_")
        ):
            count += upsert_recall(conn, payload, endpoint_name=endpoint_name, source_id=source_id)
        elif endpoint_name == "adverse_event" or endpoint_name.startswith("target_event_"):
            count += upsert_adverse_event(conn, payload, source_id=source_id)
    return count


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows([{column: row.get(column, "") for column in FIELDNAMES} for row in rows])


def process_fda_page(
    conn: Any,
    page: FetchedFdaPage,
    *,
    source_id: str,
    ingestion_run_id: int,
    asof_date: str,
) -> tuple[int, int, str, str]:
    store_raw_response(
        conn,
        source_id=source_id,
        endpoint=page.url,
        query_params=page.public_params,
        response_status=page.response_status,
        payload_text=page.payload_text,
        ingestion_run_id=ingestion_run_id,
        asof_date=asof_date,
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


@dataclass(frozen=True)
class EndpointSyncResult:
    pages: int = 0
    seen: int = 0
    upserted: int = 0
    requests: int = 0
    replayed: int = 0
    total: int | None = None
    status: str = "success"
    reason: str = ""


def _merge_sync_results(*results: EndpointSyncResult) -> EndpointSyncResult:
    failed = next((row for row in results if row.status == "failed"), None)
    return EndpointSyncResult(
        pages=sum(row.pages for row in results),
        seen=sum(row.seen for row in results),
        upserted=sum(row.upserted for row in results),
        requests=sum(row.requests for row in results),
        replayed=sum(row.replayed for row in results),
        total=sum(int(row.total or 0) for row in results),
        status="failed" if failed is not None else "success",
        reason=failed.reason if failed is not None else "",
    )


def sync_single_endpoint_query(
    conn: Any,
    endpoint: EndpointConfig,
    *,
    policy: FdaPolicy,
    api_key: str,
    max_records: int,
    source_id: str,
    ingestion_run_id: int,
    asof_date: str,
    replay_cache: ReplayCache,
    refresh_network: bool,
) -> EndpointSyncResult:
    first_limit = min(policy.page_limit, max_records)
    first_page = fetch_fda_page_job(
        endpoint,
        policy=policy,
        api_key=api_key,
        skip=0,
        limit=first_limit,
        page_number_hint=1,
        replay_cache=replay_cache,
        refresh_network=refresh_network,
    )
    requests = 0 if first_page.replayed else 1
    replayed = 1 if first_page.replayed else 0
    total = openfda_total(first_page.payload)
    if first_page.response_status == 200 and total is not None and total > max_records:
        store_raw_response(
            conn,
            source_id=source_id,
            endpoint=first_page.url,
            query_params=first_page.public_params,
            response_status=first_page.response_status,
            payload_text=first_page.payload_text,
            ingestion_run_id=ingestion_run_id,
            asof_date=asof_date,
        )
        return EndpointSyncResult(
            pages=1,
            requests=requests,
            replayed=replayed,
            total=total,
            status="failed",
            reason=f"incremental_window_overflow_total_{total}_cap_{max_records}",
        )

    page_seen, page_upserted, status, reason = process_fda_page(
        conn,
        first_page,
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
        asof_date=asof_date,
    )
    pages = 1
    seen = page_seen
    upserted = page_upserted
    if status == "failed":
        return EndpointSyncResult(pages, seen, upserted, requests, replayed, total, status, reason)
    if status == "empty":
        return EndpointSyncResult(pages, seen, upserted, requests, replayed, total, "success", reason)

    effective_max = min(max_records, total) if total is not None else max_records
    page_jobs = [
        (page_number, skip, min(policy.page_limit, effective_max - skip))
        for page_number, skip in enumerate(range(policy.page_limit, effective_max, policy.page_limit), start=2)
    ]
    fetched_pages: list[FetchedFdaPage] = []
    if policy.parallel_workers > 1 and len(page_jobs) > 1:
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
                    replay_cache=replay_cache,
                    refresh_network=refresh_network,
                )
                for page_number, skip, limit in page_jobs
            ]
            fetched_pages = [future.result() for future in as_completed(futures)]
        fetched_pages.sort(key=lambda page: page.page_number_hint)
    else:
        for page_number, skip, limit in page_jobs:
            time.sleep(max(0.0, policy.sleep_sec))
            fetched_pages.append(
                fetch_fda_page_job(
                    endpoint,
                    policy=policy,
                    api_key=api_key,
                    skip=skip,
                    limit=limit,
                    page_number_hint=page_number,
                    replay_cache=replay_cache,
                    refresh_network=refresh_network,
                )
            )
    for page in fetched_pages:
        requests += 0 if page.replayed else 1
        replayed += 1 if page.replayed else 0
        pages += 1
        row_seen, row_upserted, row_status, row_reason = process_fda_page(
            conn,
            page,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            asof_date=asof_date,
        )
        seen += row_seen
        upserted += row_upserted
        if row_status == "failed":
            status = "failed"
            reason = row_reason
            break
        if pages % policy.commit_every_pages == 0:
            conn.commit()
    return EndpointSyncResult(pages, seen, upserted, requests, replayed, total, status, reason)


def _partition_probe_value(
    conn: Any,
    endpoint: EndpointConfig,
    *,
    direction: str,
    policy: FdaPolicy,
    api_key: str,
    source_id: str,
    ingestion_run_id: int,
    asof_date: str,
    replay_cache: ReplayCache,
    refresh_network: bool,
) -> tuple[int, EndpointSyncResult]:
    probe_endpoint = replace(endpoint, sort=f"{endpoint.partition_field}:{direction}")
    page = fetch_fda_page_job(
        probe_endpoint,
        policy=policy,
        api_key=api_key,
        skip=0,
        limit=1,
        page_number_hint=1,
        replay_cache=replay_cache,
        refresh_network=refresh_network,
    )
    store_raw_response(
        conn,
        source_id=source_id,
        endpoint=page.url,
        query_params=page.public_params,
        response_status=page.response_status,
        payload_text=page.payload_text,
        ingestion_run_id=ingestion_run_id,
        asof_date=asof_date,
    )
    result = EndpointSyncResult(
        pages=1,
        requests=0 if page.replayed else 1,
        replayed=1 if page.replayed else 0,
        total=openfda_total(page.payload),
        status="success" if page.response_status == 200 else "failed",
        reason="" if page.response_status == 200 else f"partition_probe_http_status_{page.response_status}",
    )
    rows = json_list(page.payload.get("results"))
    raw_value = json_dict(rows[0]).get(endpoint.partition_field) if rows else None
    try:
        value = int(str(raw_value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {direction} partition probe for {endpoint.name}: {raw_value!r}") from exc
    return value, result


def sync_endpoint_with_partitions(
    conn: Any,
    endpoint: EndpointConfig,
    *,
    policy: FdaPolicy,
    api_key: str,
    max_records: int,
    source_id: str,
    ingestion_run_id: int,
    asof_date: str,
    replay_cache: ReplayCache,
    refresh_network: bool,
) -> EndpointSyncResult:
    primary = sync_single_endpoint_query(
        conn,
        endpoint,
        policy=policy,
        api_key=api_key,
        max_records=max_records,
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
        asof_date=asof_date,
        replay_cache=replay_cache,
        refresh_network=refresh_network,
    )
    if primary.status != "failed" or not primary.reason.startswith("incremental_window_overflow_"):
        return primary
    if not endpoint.partition_field or endpoint.partition_width <= 0:
        return primary
    if endpoint.partition_width > max_records:
        return replace(primary, reason="partition_width_exceeds_max_records")

    try:
        min_key, min_probe = _partition_probe_value(
            conn,
            endpoint,
            direction="asc",
            policy=policy,
            api_key=api_key,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            asof_date=asof_date,
            replay_cache=replay_cache,
            refresh_network=refresh_network,
        )
        max_key, max_probe = _partition_probe_value(
            conn,
            endpoint,
            direction="desc",
            policy=policy,
            api_key=api_key,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            asof_date=asof_date,
            replay_cache=replay_cache,
            refresh_network=refresh_network,
        )
    except ValueError as exc:
        return replace(primary, reason=f"partition_probe_invalid:{exc}")
    if min_probe.status == "failed" or max_probe.status == "failed" or min_key > max_key:
        return replace(primary, reason="partition_probe_failed_or_reversed")

    width = endpoint.partition_width
    cursor = (min_key // width) * width
    shard_results: list[EndpointSyncResult] = []
    while cursor <= max_key:
        shard_end = cursor + width - 1
        shard = replace(
            endpoint,
            search=(f"{endpoint.search} AND {endpoint.partition_field}:[{cursor} TO {shard_end}]"),
        )
        shard_result = sync_single_endpoint_query(
            conn,
            shard,
            policy=policy,
            api_key=api_key,
            max_records=max_records,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            asof_date=asof_date,
            replay_cache=replay_cache,
            refresh_network=refresh_network,
        )
        shard_results.append(shard_result)
        if shard_result.status == "failed":
            break
        cursor += width
    combined = _merge_sync_results(primary, min_probe, max_probe, *shard_results)
    shard_total = sum(int(row.total or 0) for row in shard_results)
    if any(row.status == "failed" for row in shard_results):
        return replace(combined, status="failed", reason="partition_shard_failed")
    if primary.total is None or shard_total != primary.total:
        return replace(
            combined,
            status="failed",
            reason=f"partition_total_mismatch_expected_{primary.total}_actual_{shard_total}",
        )
    return replace(
        combined,
        seen=sum(row.seen for row in shard_results),
        upserted=sum(row.upserted for row in shard_results),
        total=primary.total,
        status="success",
        reason="deterministic_numeric_partition",
    )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    try:
        run_asof = date.fromisoformat(args.asof) if args.asof else datetime.now(timezone.utc).date()
    except ValueError as exc:
        raise ValueError(f"Invalid --asof date {args.asof!r}; expected YYYY-MM-DD") from exc
    run_asof_iso = run_asof.isoformat()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    policy = fda_policy(config)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "fda_core_ingestion.output_csv",
                "../output/med_devices_reports/med_device_fda_core_ingestion_coverage.csv",
            ),
            base_dir=base_dir,
        )
    )
    manual_approval_raw = str(cfg_get(config, "fda_core_ingestion.manual_approval_evidence_csv", "") or "").strip()
    manual_approval_csv = resolve_path(manual_approval_raw, base_dir=base_dir) if manual_approval_raw else None
    manual_approval_source_id = str(
        cfg_get(
            config,
            "fda_core_ingestion.manual_approval_source_id",
            "fda_accessdata_cber",
        )
        or "fda_accessdata_cber"
    ).strip()
    endpoint_filter = {value.strip() for value in str(args.endpoints or "").split(",") if value.strip()}
    cli_target_tickers = ticker_filter(args.targeted_tickers)
    configured_target_tickers = ticker_filter(cfg_get(config, "fda_core_ingestion.targeted_tickers", []))
    selected_target_tickers = cli_target_tickers or configured_target_tickers
    include_targeted_entities = args.targeted_entity_names or as_bool(
        cfg_get(config, "fda_core_ingestion.targeted_include_entity_names", False),
        default=False,
    )
    include_targeted_postmarket = args.targeted_postmarket or as_bool(
        cfg_get(config, "fda_core_ingestion.targeted_include_postmarket", False),
        default=False,
    )
    target_limit = (
        args.target_limit
        if args.target_limit > 0
        else max(1, int(cfg_get(config, "fda_core_ingestion.targeted_limit", 100)))
    )
    targeted_requested = bool(
        args.targeted_footprints
        or args.targeted_only
        or args.targeted_entity_names
        or args.targeted_postmarket
        or configured_target_tickers
    )
    endpoints = (
        []
        if args.targeted_only
        else [
            endpoint
            for endpoint in policy.endpoints
            if endpoint.enabled and (not endpoint_filter or endpoint.name in endpoint_filter)
        ]
    )
    if targeted_requested:
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
            target_limit=target_limit,
            include_entity_names=include_targeted_entities,
            include_postmarket=include_targeted_postmarket,
            tickers=selected_target_tickers or None,
            asof=run_asof,
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
                or ("recall" in endpoint_filter and endpoint.name.startswith("target_recall_"))
                or ("enforcement" in endpoint_filter and endpoint.name.startswith("target_enforcement_"))
                or ("adverse_event" in endpoint_filter and endpoint.name.startswith("target_event_"))
            ]
        endpoints.extend(targeted_endpoints)
    scope_footprint_raw = str(
        cfg_get(
            config,
            "fda_core_ingestion.targeted_footprint_csv",
            cfg_get(config, "fda_features.footprint_csv", ""),
        )
        or ""
    ).strip()
    scope_footprint_csv = resolve_path(scope_footprint_raw, base_dir=base_dir) if scope_footprint_raw else None
    alias_raw = str(cfg_get(config, "fda_entity_linking.extra_alias_csv", "") or "").strip()
    alias_csv = resolve_path(alias_raw, base_dir=base_dir) if alias_raw else None
    endpoints = scope_endpoints_to_footprint_product_codes(
        endpoints,
        scope_footprint_csv,
        asof=run_asof,
        alias_path=alias_csv,
    )
    if args.recompute_adverse_event_counts_only:
        with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
            init_db(conn)
            updated, invalid_payloads = recompute_adverse_event_counts(conn)
            conn.commit()
        print(f"adverse_event_counts_updated={updated} invalid_payloads={invalid_payloads} db={db_path}")
        return
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
    replayed_page_count = 0
    failed: list[str] = []
    LOGGER.info("FDA core sync starting: db=%s endpoints=%d output=%s", db_path, len(endpoints), output_csv)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        severity_rows_updated, invalid_severity_payloads = recompute_adverse_event_counts(conn)
        conn.commit()
        LOGGER.info(
            "Recomputed stored FDA adverse-event severity counts: updated=%d invalid_payloads=%d",
            severity_rows_updated,
            invalid_severity_payloads,
        )
        ensure_source_registry(conn, config, base_dir, policy.source_id)
        if manual_approval_csv is not None:
            ensure_source_registry(
                conn,
                config,
                base_dir,
                manual_approval_source_id,
            )
        try:
            incremental_start = date.fromisoformat(args.incremental_start) if args.incremental_start else None
        except ValueError as exc:
            raise ValueError(f"Invalid --incremental-start {args.incremental_start!r}; expected YYYY-MM-DD") from exc
        if incremental_start is not None and incremental_start > run_asof:
            raise ValueError("--incremental-start cannot be after --asof")
        endpoints = plan_incremental_endpoints(
            conn,
            endpoints,
            source_id=policy.source_id,
            run_asof=run_asof,
            start_override=incremental_start,
        )
        replay_cache = load_raw_response_replay_cache(
            conn,
            source_id=policy.source_id,
            asof_date=run_asof_iso,
        )
        if replay_cache and not args.refresh_network:
            LOGGER.info(
                "Loaded hash-validated FDA replay cache: asof=%s responses=%d",
                run_asof_iso,
                len(replay_cache),
            )
        ingestion_run_id = start_ingestion_run(conn, policy.source_id)
        run_id = start_run(conn, run_type="sync_med_device_fda_core", input_path=config_path)
        pending_watermarks: list[tuple[str, str, str, str]] = []
        blocked_incremental_streams: set[str] = set()
        try:
            if manual_approval_csv is not None:
                manual_approval_count = upsert_manual_approval_evidence(
                    conn,
                    manual_approval_csv,
                    source_id=manual_approval_source_id,
                )
                canonical_rows += manual_approval_count
                coverage_rows.append(
                    {
                        "endpoint_name": "manual_cber_approval_evidence",
                        "path": str(manual_approval_csv),
                        "search": "",
                        "status": "success" if manual_approval_count else "empty",
                        "pages_fetched": 0,
                        "api_records_seen": manual_approval_count,
                        "canonical_rows_upserted": manual_approval_count,
                        "review_reason": (
                            "authoritative_fda_accessdata_evidence"
                            if manual_approval_count
                            else "no_manual_approval_rows"
                        ),
                    }
                )
                conn.commit()
            for endpoint in endpoints:
                if endpoint.stream_name and endpoint.stream_name in blocked_incremental_streams:
                    failed.append(endpoint.stream_name)
                    coverage_rows.append(
                        {
                            "endpoint_name": endpoint.stream_name,
                            "path": endpoint.path,
                            "search": endpoint.search,
                            "status": "failed",
                            "pages_fetched": 0,
                            "api_records_seen": 0,
                            "canonical_rows_upserted": 0,
                            "review_reason": "blocked_by_earlier_failed_incremental_window",
                        }
                    )
                    continue
                pages = 0
                seen = 0
                upserted = 0
                endpoint_replayed = 0
                status = "success"
                reason = ""
                max_records = args.max_records if args.max_records > 0 else endpoint.max_records
                max_records = max_records if max_records > 0 else policy.page_limit
                try:
                    result = sync_endpoint_with_partitions(
                        conn,
                        endpoint,
                        policy=policy,
                        api_key=api_key,
                        max_records=max_records,
                        source_id=policy.source_id,
                        ingestion_run_id=ingestion_run_id,
                        asof_date=run_asof_iso,
                        replay_cache=replay_cache,
                        refresh_network=args.refresh_network,
                    )
                    pages = result.pages
                    seen = result.seen
                    upserted = result.upserted
                    request_count += result.requests
                    replayed_page_count += result.replayed
                    endpoint_replayed += result.replayed
                    canonical_rows += result.upserted
                    status = result.status
                    reason = result.reason
                    if status == "failed":
                        failed.append(endpoint.stream_name or endpoint.name)
                    LOGGER.info(
                        "%s window=%s..%s pages=%d records=%d upserted=%d status=%s",
                        endpoint.name,
                        endpoint.window_start,
                        endpoint.window_end,
                        pages,
                        seen,
                        upserted,
                        status,
                    )
                except Exception as exc:
                    status = "failed"
                    reason = f"{type(exc).__name__}: {exc}"
                    failed.append(endpoint.stream_name or endpoint.name)
                    LOGGER.warning("FDA endpoint failed: %s %s", endpoint.name, exc)
                if endpoint.stream_name:
                    if status == "failed":
                        blocked_incremental_streams.add(endpoint.stream_name)
                    elif endpoint.window_end:
                        pending_watermarks.append(
                            (
                                endpoint.stream_name,
                                endpoint.scope_hash,
                                endpoint.date_field,
                                endpoint.window_end,
                            )
                        )
                if not reason and endpoint_replayed:
                    reason = (
                        "hash_validated_same_date_replay"
                        if endpoint_replayed == pages
                        else "partial_hash_validated_same_date_replay"
                    )
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
            message = (
                f"asof={run_asof_iso} endpoints={len(endpoints)} requests={request_count} "
                f"replayed_pages={replayed_page_count} canonical_rows={canonical_rows} output={output_csv}"
            )
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
            seal_ingestion_run(
                conn,
                ingestion_run_id=ingestion_run_id,
                source_id=policy.source_id,
                asof_date=run_asof_iso,
            )
            for stream_name, scope_hash, date_field, watermark_date in pending_watermarks:
                upsert_ingestion_watermark(
                    conn,
                    source_id=policy.source_id,
                    stream_name=stream_name,
                    scope_hash=scope_hash,
                    date_field=date_field,
                    watermark_date=watermark_date,
                    ingestion_run_id=ingestion_run_id,
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
