from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import quote


RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
TEXT_SUFFIXES = frozenset({".htm", ".html", ".xhtml", ".txt"})
EVENT_FORMS = frozenset({"6-K", "6-K/A", "8-K", "8-K/A"})
HYDRATION_FIELDS = (
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "resource_type",
    "document_name",
    "local_path",
    "fetch_status",
    "request_attempts",
    "network_requests",
    "bytes_written",
    "http_status",
    "error",
)
AUDIT_FIELDS = (
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "document_name",
    "document_description",
    "document_sha256",
    "file_size",
    "matched_metric_ids",
    "matched_anchors",
)

_COMPOUND_ANCHORS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "freight_weight_per_shipment": (
        ("pounds per day and shipments per day", ("pounds per day", "shipments per day")),
    ),
    "operating_ratio": (
        ("segment expense and revenue", ("operating expenses", "segment revenue")),
        ("segment income and revenue", ("operating income", "segment revenue")),
    ),
    "purchased_transportation_ratio": (
        ("purchased transportation and revenue", ("purchased transportation", "revenue")),
        ("direct transportation cost and revenue", ("directly related cost of transportation", "revenue")),
    ),
    "logistics_net_revenue_margin": (
        ("transport cost and revenue", ("transportation", "revenue")),
    ),
    "rail_fuel_efficiency": (
        ("locomotive fuel and gross ton miles", ("locomotive fuel", "gross ton miles")),
    ),
    "fleet_age": (
        ("year built and dwt", ("year built", "dwt")),
        ("built and capacity", ("built", "capacity")),
    ),
    "vessel_count": (
        ("vessel schedule", ("vessel", "year built")),
    ),
    "fleet_capacity": (
        ("vessel and dwt", ("vessel", "dwt")),
    ),
    "vessel_opex_per_day": (
        ("vessel opex and operating days", ("vessel operating expenses", "operating days")),
    ),
    "fleet_utilization": (
        ("revenue and available days", ("revenue days", "available days")),
    ),
    "charter_coverage_next_12m": (
        ("charter schedule", ("vessel", "charter", "expiry")),
        ("fixed and available days", ("fixed", "available days")),
    ),
    "revenue_days": (
        ("available less offhire", ("available days", "off hire days")),
        ("available less drydock", ("available days", "drydock days")),
    ),
}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def supplemental_event_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    return [
        dict(row)
        for row in rows
        if row.get("candidate_type") == "supplemental_event"
        and row.get("form_type") in EVENT_FORMS
    ]


def normalize_phrase(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def audit_patterns(
    metric_aliases: Mapping[str, Iterable[str]],
    target_metrics: Iterable[str],
) -> dict[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    output: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {}
    for metric in sorted(set(target_metrics)):
        rules: list[tuple[str, tuple[str, ...]]] = []
        for alias in metric_aliases.get(metric, ()):
            normalized = normalize_phrase(alias)
            if normalized:
                rules.append((str(alias), (normalized,)))
        rules.extend(_COMPOUND_ANCHORS.get(metric, ()))
        output[metric] = tuple(dict.fromkeys(rules))
    return output


def index_items(accession_dir: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads((accession_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    output: list[dict[str, str]] = []
    for raw in ((payload.get("directory") or {}).get("item") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name or Path(name).name != name or Path(name).suffix.lower() not in TEXT_SUFFIXES:
            continue
        output.append(
            {
                "name": name,
                "description": str(raw.get("description") or raw.get("title") or "").strip(),
                "document_type": str(raw.get("type") or raw.get("document_type") or "").upper().strip(),
            }
        )
    return output


def selected_documents(accession_dir: Path, *, form_type: str) -> tuple[str, ...]:
    output: set[str] = set()
    form_base = form_type.upper().removesuffix("/A")
    for item in index_items(accession_dir):
        name = item["name"]
        document_type = item["document_type"]
        filename_signal = re.search(
            r"(?:^|[^a-z0-9])(?:ex(?:hibit)?[-_]?99|form[-_]?[68][-_]?k|[68][-_]?k)(?:[^a-z0-9]|$)",
            name.casefold(),
        )
        if (
            document_type.removesuffix("/A") == form_base
            or document_type.startswith("EX-99")
            or filename_signal is not None
        ):
            output.add(name)
    return tuple(sorted(output, key=str.casefold))


def _fetch(
    *,
    url: str,
    local_path: Path,
    user_agent: str,
    max_retries: int,
    timeout_sec: float,
    spacing_sec: float,
) -> dict[str, object]:
    if local_path.is_file() and local_path.stat().st_size > 0:
        return {
            "fetch_status": "ALREADY_CACHED",
            "request_attempts": 0,
            "network_requests": 0,
            "bytes_written": local_path.stat().st_size,
            "http_status": 0,
            "error": "",
        }
    result: dict[str, object] = {
        "fetch_status": "FAILED",
        "request_attempts": 0,
        "network_requests": 0,
        "bytes_written": 0,
        "http_status": 0,
        "error": "",
    }
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = local_path.with_name(f".{local_path.name}.{os.getpid()}.curl.tmp")
    for attempt in range(1, max_retries + 1):
        time.sleep(max(0.0, spacing_sec))
        result["request_attempts"] = attempt
        result["network_requests"] = int(result["network_requests"]) + 1
        completed = subprocess.run(
            [
                "curl.exe", "-L", "--silent", "--show-error",
                "--max-time", str(max(1, int(timeout_sec))),
                "-A", user_agent,
                "--output", str(temporary),
                "--write-out", "%{http_code}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_sec + 5.0,
            check=False,
        )
        try:
            status = int(completed.stdout.strip()[-3:])
        except ValueError:
            status = 0
        result["http_status"] = status
        if completed.returncode == 0 and status == 200 and temporary.is_file() and temporary.stat().st_size > 0:
            os.replace(temporary, local_path)
            result.update(
                fetch_status="HYDRATED",
                bytes_written=local_path.stat().st_size,
                error="",
            )
            return result
        temporary.unlink(missing_ok=True)
        result["error"] = completed.stderr.strip() or f"HTTP {status}"
        if status and status not in RETRYABLE_HTTP_STATUS:
            return result
        if attempt < max_retries:
            time.sleep(min(4.0, spacing_sec * (2**attempt)))
    return result


def hydrate_event_sources(
    *,
    decision_rows: Iterable[Mapping[str, str]],
    cache_dir: Path,
    user_agent: str,
    max_retries: int = 4,
    timeout_sec: float = 45.0,
    spacing_sec: float = 0.15,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    archive_root = (cache_dir / "sec_archive_xbrl").resolve()
    candidates = supplemental_event_rows(decision_rows)
    results: list[dict[str, object]] = []
    for row in candidates:
        cik = str(row["cik"])
        accession = str(row["accession_number"])
        accession_nodash = accession.replace("-", "")
        accession_dir = archive_root / f"CIK{cik}" / accession_nodash
        if not accession_dir.resolve().is_relative_to(archive_root):
            raise ValueError("accession cache path escaped archive root")
        base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}"
        index_path = accession_dir / "index.json"
        fetched = _fetch(
            url=f"{base_url}/index.json",
            local_path=index_path,
            user_agent=user_agent,
            max_retries=max_retries,
            timeout_sec=timeout_sec,
            spacing_sec=spacing_sec,
        )
        results.append(
            {
                "ticker": row["ticker"],
                "cik": cik,
                "accession_number": accession,
                "form_type": row["form_type"],
                "resource_type": "SEC_INDEX",
                "document_name": "index.json",
                "local_path": str(index_path),
                **fetched,
            }
        )
        if fetched["fetch_status"] not in {"HYDRATED", "ALREADY_CACHED"}:
            continue
        for document_name in selected_documents(accession_dir, form_type=str(row["form_type"])):
            local_path = accession_dir / document_name
            document = _fetch(
                url=f"{base_url}/{quote(document_name, safe='._-')}",
                local_path=local_path,
                user_agent=user_agent,
                max_retries=max_retries,
                timeout_sec=timeout_sec,
                spacing_sec=spacing_sec,
            )
            results.append(
                {
                    "ticker": row["ticker"],
                    "cik": cik,
                    "accession_number": accession,
                    "form_type": row["form_type"],
                    "resource_type": "PRIMARY_OR_EX99_TEXT",
                    "document_name": document_name,
                    "local_path": str(local_path),
                    **document,
                }
            )
    failures = [row for row in results if row["fetch_status"] == "FAILED"]
    summary = {
        "acceptance": "PASS" if not failures else "NO_GO",
        "candidate_accession_count": len(candidates),
        "resource_count": len(results),
        "hydrated_resource_count": sum(row["fetch_status"] == "HYDRATED" for row in results),
        "already_cached_resource_count": sum(row["fetch_status"] == "ALREADY_CACHED" for row in results),
        "failed_resource_count": len(failures),
        "network_request_count": sum(int(row["network_requests"]) for row in results),
        "bytes_written": sum(int(row["bytes_written"]) for row in results if row["fetch_status"] == "HYDRATED"),
        "selection_contract": "all_supplemental_events_index_plus_primary_or_ex99_text_once",
    }
    return results, summary


def audit_cached_event_sources(
    *,
    decision_rows: Iterable[Mapping[str, str]],
    cache_dir: Path,
    patterns: Mapping[str, tuple[tuple[str, tuple[str, ...]], ...]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    output: list[dict[str, object]] = []
    scanned_accessions = 0
    scanned_documents = 0
    scanned_bytes = 0
    for row in supplemental_event_rows(decision_rows):
        cik = str(row["cik"])
        accession = str(row["accession_number"])
        accession_dir = cache_dir / "sec_archive_xbrl" / f"CIK{cik}" / accession.replace("-", "")
        if not (accession_dir / "index.json").is_file():
            continue
        scanned_accessions += 1
        for item in index_items(accession_dir):
            path = accession_dir / item["name"]
            if not path.is_file():
                continue
            payload = path.read_bytes()
            scanned_documents += 1
            scanned_bytes += len(payload)
            text = normalize_phrase(payload.decode("utf-8", errors="ignore"))
            matched: dict[str, list[str]] = {}
            for metric, rules in patterns.items():
                labels = [
                    label
                    for label, required_phrases in rules
                    if all(normalize_phrase(phrase) in text for phrase in required_phrases)
                ]
                if labels:
                    matched[metric] = labels
            if not matched:
                continue
            output.append(
                {
                    "ticker": row["ticker"],
                    "cik": cik,
                    "accession_number": accession,
                    "form_type": row["form_type"],
                    "filing_date": row["filing_date"],
                    "document_name": item["name"],
                    "document_description": item["description"],
                    "document_sha256": hashlib.sha256(payload).hexdigest(),
                    "file_size": len(payload),
                    "matched_metric_ids": "|".join(sorted(matched)),
                    "matched_anchors": json.dumps(matched, sort_keys=True, separators=(",", ":")),
                }
            )
    output.sort(key=lambda row: (str(row["ticker"]), str(row["filing_date"]), str(row["accession_number"]), str(row["document_name"])))
    summary = {
        "network_requests": 0,
        "audited_cached_accession_count": scanned_accessions,
        "scanned_document_count": scanned_documents,
        "scanned_bytes": scanned_bytes,
        "positive_document_count": len(output),
        "positive_accession_count": len({(row["ticker"], row["accession_number"]) for row in output}),
        "positive_metric_document_counts": dict(sorted(Counter(metric for row in output for metric in str(row["matched_metric_ids"]).split("|") if metric).items())),
        "pattern_contract_sha256": hashlib.sha256(json.dumps(patterns, sort_keys=True, default=list).encode()).hexdigest(),
        "scan_method": "raw_cached_text_anchor_census_complete_event_candidate_set",
    }
    return output, summary

