#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, expand_env_vars, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402


DEFAULT_CONFIG = PROJECT_ROOT / "industrials" / "config.yaml"
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
TEXT_SUFFIXES = frozenset({".htm", ".html", ".xhtml", ".txt"})
RESULT_FIELDS = (
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hydrate the SEC index plus only primary-form and EX-99 text "
            "documents for tanker event accessions that lacked metadata."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    parser.add_argument("--request-spacing-sec", type=float, default=0.15)
    return parser.parse_args()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _fetch(
    *,
    url: str,
    local_path: Path,
    headers: dict[str, str],
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
                "curl.exe",
                "-L",
                "--silent",
                "--show-error",
                "--max-time",
                str(max(1, int(timeout_sec))),
                "-A",
                headers["User-Agent"],
                "--output",
                str(temporary),
                "--write-out",
                "%{http_code}",
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
            for replace_attempt in range(5):
                try:
                    os.replace(temporary, local_path)
                    break
                except PermissionError:
                    if local_path.is_file() and local_path.stat().st_size > 0:
                        temporary.unlink(missing_ok=True)
                        break
                    if replace_attempt == 4:
                        raise
                    time.sleep(0.10 * (replace_attempt + 1))
            result["fetch_status"] = "HYDRATED"
            result["bytes_written"] = local_path.stat().st_size
            result["error"] = ""
            return result
        if temporary.is_file():
            temporary.unlink()
        result["error"] = completed.stderr.strip() or f"HTTP {status}"
        if status and status not in RETRYABLE_HTTP_STATUS:
            return result
        if attempt < max_retries:
            time.sleep(min(4.0, spacing_sec * (2**attempt)))
    return result


def _selected_documents(index_path: Path, *, form_type: str) -> tuple[str, ...]:
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    output: set[str] = set()
    form_base = form_type.upper().removesuffix("/A")
    for raw in ((payload.get("directory") or {}).get("item") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        document_type = str(raw.get("type") or raw.get("document_type") or "").upper().strip()
        if (
            not name
            or Path(name).name != name
            or Path(name).suffix.lower() not in TEXT_SUFFIXES
        ):
            continue
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


def main() -> int:
    args = parse_args()
    if args.max_retries < 1 or args.timeout_sec <= 0 or args.request_spacing_sec < 0:
        raise ValueError("invalid retry/timeout/spacing arguments")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, "transportation")
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            family["dedicated_parser"]["tanker_delta_output_root"],
            base_dir=config_path.parent,
        )
        / args.asof
    )
    decisions_path = output_dir / "transportation_tanker_delta_source_decisions.csv"
    candidates = [
        row
        for row in _rows(decisions_path)
        if row.get("decision") == "EXCLUDE_NO_METADATA_SIGNAL"
        and row.get("candidate_type") == "supplemental_event"
        and row.get("form_type") in {"6-K", "6-K/A", "8-K", "8-K/A"}
    ]
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=config_path.parent,
    )
    archive_root = (cache_dir / "sec_archive_xbrl").resolve()
    user_agent = expand_env_vars(str(cfg_get(config, "sec_fundamentals.user_agent")))
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    results: list[dict[str, object]] = []

    for index, row in enumerate(candidates, start=1):
        ticker = str(row["ticker"])
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
            headers=headers,
            max_retries=args.max_retries,
            timeout_sec=args.timeout_sec,
            spacing_sec=args.request_spacing_sec,
        )
        results.append(
            {
                "ticker": ticker,
                "cik": cik,
                "accession_number": accession,
                "form_type": row["form_type"],
                "resource_type": "SEC_INDEX",
                "document_name": "index.json",
                "local_path": str(index_path),
                **fetched,
            }
        )
        if fetched["fetch_status"] in {"HYDRATED", "ALREADY_CACHED"}:
            for document_name in _selected_documents(index_path, form_type=str(row["form_type"])):
                local_path = accession_dir / document_name
                fetched_document = _fetch(
                    url=f"{base_url}/{quote(document_name, safe='._-')}",
                    local_path=local_path,
                    headers=headers,
                    max_retries=args.max_retries,
                    timeout_sec=args.timeout_sec,
                    spacing_sec=args.request_spacing_sec,
                )
                results.append(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "accession_number": accession,
                        "form_type": row["form_type"],
                        "resource_type": "PRIMARY_OR_EX99_TEXT",
                        "document_name": document_name,
                        "local_path": str(local_path),
                        **fetched_document,
                    }
                )
        if index % 25 == 0 or index == len(candidates):
            failed = sum(result["fetch_status"] == "FAILED" for result in results)
            print(
                f"excluded-event hydration progress={index}/{len(candidates)} "
                f"resources={len(results)} failed={failed}",
                flush=True,
            )

    failures = [row for row in results if row["fetch_status"] == "FAILED"]
    result_path = output_dir / "transportation_tanker_excluded_event_hydration.csv"
    manifest_path = output_dir / "transportation_tanker_excluded_event_hydration.json"
    write_csv_atomic(result_path, RESULT_FIELDS, results)
    summary = {
        "acceptance": "PASS" if not failures else "NO_GO",
        "asof_date": args.asof,
        "candidate_accession_count": len(candidates),
        "resource_count": len(results),
        "hydrated_resource_count": sum(row["fetch_status"] == "HYDRATED" for row in results),
        "already_cached_resource_count": sum(row["fetch_status"] == "ALREADY_CACHED" for row in results),
        "failed_resource_count": len(failures),
        "network_request_count": sum(int(row["network_requests"]) for row in results),
        "bytes_written": sum(int(row["bytes_written"]) for row in results if row["fetch_status"] == "HYDRATED"),
        "selection_contract": "missing_event_index_plus_primary_or_ex99_text_only",
        "next_gate": "RERUN_EXCLUDED_EVENT_ANCHOR_AUDIT",
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
    }
    write_text_atomic(manifest_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
