#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    load_investable_universe_policy,
    validate_investable_universe_policy,
)


DEFAULT_CONFIG = PROJECT_ROOT / "industrials" / "config.yaml"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v3.yaml"
)
EXPECTED_MANIFEST_VERSION = "transportation_tanker_delta_census_v3"
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
RESULT_FIELDS = (
    "ticker",
    "cik",
    "accession_number",
    "document_name",
    "local_path",
    "fetch_status",
    "request_attempts",
    "network_requests",
    "bytes_written",
    "http_status",
    "error",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hydrate only the exact document rows in the sealed transportation "
            "tanker delta cache-gap manifest. Each document is retried independently."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--gaps-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    parser.add_argument("--request-spacing-sec", type=float, default=0.20)
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def is_valid_cached_document(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def fetch_document(
    row: dict[str, str],
    *,
    cache_root: Path,
    user_agent: str,
    max_retries: int,
    timeout_sec: float,
    request_spacing_sec: float,
) -> dict[str, object]:
    ticker = str(row.get("ticker") or "").strip().upper()
    cik = str(row.get("cik") or "").strip()
    accession = str(row.get("accession_number") or "").strip()
    document_name = str(row.get("document_name") or "").strip()
    local_path = Path(str(row.get("local_path") or "")).expanduser().resolve()
    result: dict[str, object] = {
        "ticker": ticker,
        "cik": cik,
        "accession_number": accession,
        "document_name": document_name,
        "local_path": str(local_path),
        "fetch_status": "",
        "request_attempts": 0,
        "network_requests": 0,
        "bytes_written": 0,
        "http_status": 0,
        "error": "",
    }
    if not local_path.is_relative_to(cache_root):
        result["fetch_status"] = "REJECTED_PATH_OUTSIDE_CACHE"
        result["error"] = f"local_path must remain under {cache_root}"
        return result
    if is_valid_cached_document(local_path):
        result["fetch_status"] = "ALREADY_CACHED"
        result["bytes_written"] = local_path.stat().st_size
        return result
    if not (ticker and cik.isdigit() and accession and document_name):
        result["fetch_status"] = "INVALID_MANIFEST_ROW"
        result["error"] = "ticker, numeric cik, accession_number, and document_name are required"
        return result

    try:
        import requests  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependency
        raise RuntimeError("Package 'requests' is required for SEC document hydration") from exc

    accession_nodash = accession.replace("-", "")
    url = (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{accession_nodash}/{quote(document_name, safe='._-')}"
    )
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }
    last_error = ""
    last_status = 0
    attempts = max(1, max_retries)
    for attempt in range(1, attempts + 1):
        time.sleep(max(0.0, request_spacing_sec))
        result["request_attempts"] = attempt
        result["network_requests"] = int(result["network_requests"]) + 1
        try:
            response = requests.get(url, headers=headers, timeout=timeout_sec)
            last_status = int(response.status_code)
            result["http_status"] = last_status
            if last_status == 200 and response.content:
                atomic_write_bytes(local_path, bytes(response.content))
                result["fetch_status"] = "HYDRATED"
                result["bytes_written"] = len(response.content)
                return result
            last_error = f"HTTP {last_status}"
            if last_status not in RETRYABLE_HTTP_STATUS:
                break
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(min(5.0, request_spacing_sec * (2**attempt)))

    result["fetch_status"] = "FAILED"
    result["http_status"] = last_status
    result["error"] = last_error or "document fetch failed"
    return result


def main() -> int:
    args = parse_args()
    if args.max_retries < 1:
        raise ValueError("--max-retries must be at least 1")
    if args.timeout_sec <= 0 or args.request_spacing_sec < 0:
        raise ValueError("timeout must be positive and request spacing cannot be negative")

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    policy = load_investable_universe_policy(args.policy.expanduser().resolve())
    policy_errors, _ = validate_investable_universe_policy(policy)
    if policy_errors:
        raise ValueError(f"investable-universe policy is invalid: {policy_errors}")

    output_root = resolve_path(
        cfg_get(
            config,
            "model_families.transportation.dedicated_parser.tanker_delta_output_root",
        ),
        base_dir=base_dir,
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else output_root / args.asof
    )
    gaps_path = (
        args.gaps_csv.expanduser().resolve()
        if args.gaps_csv
        else output_dir / "transportation_tanker_delta_cache_gaps.csv"
    )
    rows = read_rows(gaps_path)
    allowed_tickers = set(policy.tanker_tickers)
    seen_keys: set[tuple[str, str, str]] = set()
    for row in rows:
        if str(row.get("manifest_version") or "") != EXPECTED_MANIFEST_VERSION:
            raise ValueError("cache-gap row does not belong to the v3 tanker census")
        if str(row.get("gap_type") or "") != "SOURCE_DOCUMENT":
            raise ValueError("exact hydrator accepts SOURCE_DOCUMENT gaps only")
        if str(row.get("required_action") or "") != "HYDRATE_SEALED_DOCUMENT":
            raise ValueError("cache-gap row is not authorized for hydration")
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker not in allowed_tickers:
            raise ValueError(f"out-of-scope tanker ticker in gap manifest: {ticker}")
        key = (
            ticker,
            str(row.get("accession_number") or "").strip(),
            str(row.get("document_name") or "").strip(),
        )
        if key in seen_keys:
            raise ValueError(f"duplicate exact document in gap manifest: {key}")
        seen_keys.add(key)

    cache_dir = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    cache_root = (cache_dir / "sec_archive_xbrl").resolve()
    user_agent = expand_env_vars(str(cfg_get(config, "sec_fundamentals.user_agent")))
    results: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        result = fetch_document(
            row,
            cache_root=cache_root,
            user_agent=user_agent,
            max_retries=args.max_retries,
            timeout_sec=args.timeout_sec,
            request_spacing_sec=args.request_spacing_sec,
        )
        results.append(result)
        if index % 25 == 0 or index == len(rows):
            hydrated = sum(result_row["fetch_status"] == "HYDRATED" for result_row in results)
            failed = sum(result_row["fetch_status"] == "FAILED" for result_row in results)
            print(
                f"tanker exact hydration progress={index}/{len(rows)} "
                f"hydrated={hydrated} failed={failed}",
                flush=True,
            )

    failure_rows = [row for row in results if row["fetch_status"] not in {"HYDRATED", "ALREADY_CACHED"}]
    summary: dict[str, Any] = {
        "acceptance": "PASS" if not failure_rows else "NO_GO",
        "asof_date": args.asof,
        "manifest_version": EXPECTED_MANIFEST_VERSION,
        "requested_document_count": len(rows),
        "hydrated_document_count": sum(row["fetch_status"] == "HYDRATED" for row in results),
        "already_cached_document_count": sum(row["fetch_status"] == "ALREADY_CACHED" for row in results),
        "failed_document_count": len(failure_rows),
        "network_request_count": sum(int(row["network_requests"]) for row in results),
        "calibration_authorized": False,
        "historical_reconstruction_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "RERUN_TANKER_DELTA_CENSUS",
    }
    write_csv_atomic(
        output_dir / "transportation_tanker_delta_exact_hydration.csv",
        RESULT_FIELDS,
        results,
    )
    write_text_atomic(
        output_dir / "transportation_tanker_delta_exact_hydration.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not failure_rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
