#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.non_sec_endpoints import (  # noqa: E402
    ENDPOINT_FIELDS,
    NON_SEC_ENDPOINT_VERSION,
    PAIR_ENDPOINT_FIELDS,
    build_endpoint_rows,
    normalized_domain,
    read_domain_counts,
    select_issuer_domain,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Seal one non-SEC discovery root per transportation issuer and "
            "map every retrieval-eligible metric pair to that root. This "
            "command performs no network retrieval."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _issuer_map(
    *,
    active_path: Path,
    delisted_path: Path,
    historical_path: Path,
) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for row in read_csv(active_path):
        output[row["ticker"].upper()] = {
            "company_name": row["company_name"],
            "start_date": "",
            "end_date": "",
        }
    for row in read_csv(delisted_path):
        output[row["ticker"].upper()] = {
            "company_name": row["company"],
            "start_date": "",
            "end_date": (
                f"{row['exit_year']}-12-31"
                if row.get("exit_year")
                else ""
            ),
        }
    for row in read_csv(historical_path):
        ticker = row["internal_ticker"].upper()
        current = output.setdefault(ticker, {})
        current["company_name"] = (
            row.get("company_name")
            or current.get("company_name")
            or ticker
        )
        current["start_date"] = row.get("start_date") or ""
        current["end_date"] = row.get("end_date") or ""
    return output


def _profile_websites(path: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    if not path.is_dir():
        return output
    for item in sorted(path.glob("*.json")):
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        website = str(payload.get("website") or "").strip()
        if website and normalized_domain(website):
            output[item.stem.upper()] = website
    return output


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    universe_cfg = family["universe"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Endpoint sealing requires the general parser switch off"
        )
    base_dir = config_path.parent
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / str(parser_cfg["source_census_asof_date"])
    )
    residual_path = (
        output_dir
        / "transportation_non_sec_residual_source_audit.csv"
    )
    residual_manifest_path = (
        output_dir
        / "transportation_non_sec_residual_source_manifest.json"
    )
    base_source_path = resolve_path(
        parser_cfg["source_census_csv"],
        base_dir=base_dir,
    )
    delta_source_path = (
        output_dir / "transportation_delta_parser_source_manifest.csv"
    )
    active_path = resolve_path(
        universe_cfg["seed_csv"],
        base_dir=base_dir,
    )
    delisted_path = resolve_path(
        universe_cfg["delisted_seed_csv"],
        base_dir=base_dir,
    )
    historical_path = resolve_path(
        universe_cfg["historical_membership_csv"],
        base_dir=base_dir,
    )
    profile_path = (
        PROJECT_ROOT
        / "ticker_mapping"
        / "_transportation_yahoo_profile_cache"
    )
    required = (
        residual_path,
        residual_manifest_path,
        base_source_path,
        delta_source_path,
        active_path,
        delisted_path,
        historical_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing endpoint inputs: {missing}")
    residual_manifest = _read_json(residual_manifest_path)
    if (
        residual_manifest.get("acceptance") != "PASS"
        or residual_manifest.get("coverage_prefix")
        != "transportation_repaired_sec_union"
        or str(
            (residual_manifest.get("artifact") or {}).get("sha256")
            or ""
        )
        != file_sha256(residual_path)
    ):
        raise ValueError(
            "Post-repair residual source audit is not sealed"
        )
    residual_rows = read_csv(residual_path)
    eligible_tickers = {
        row["ticker"].upper()
        for row in residual_rows
        if row["retrieval_eligible"] == "1"
    }
    issuers = _issuer_map(
        active_path=active_path,
        delisted_path=delisted_path,
        historical_path=historical_path,
    )
    profiles = _profile_websites(profile_path)
    source_rows = [
        *read_csv(base_source_path),
        *read_csv(delta_source_path),
    ]
    paths_by_ticker: dict[str, list[tuple[str, Path]]] = {}
    for row in source_rows:
        ticker = row["ticker"].upper()
        if ticker not in eligible_tickers:
            continue
        local_path = Path(row.get("local_path") or "")
        if local_path.suffix.lower() == ".pdf":
            continue
        paths_by_ticker.setdefault(ticker, []).append(
            (row.get("filing_date") or "", local_path)
        )
    inferred: dict[str, tuple[str, int, bool, float]] = {}
    local_document_open_count = 0
    for ticker in sorted(eligible_tickers - set(profiles)):
        candidates = [
            path
            for _, path in sorted(
                paths_by_ticker.get(ticker, ()),
                key=lambda item: item[0],
                reverse=True,
            )
        ]
        counts, opened = read_domain_counts(candidates)
        local_document_open_count += opened
        inferred[ticker] = select_issuer_domain(
            ticker=ticker,
            company_name=str(
                issuers.get(ticker, {}).get("company_name") or ticker
            ),
            counts=counts,
        )
    endpoints, pair_rows, errors = build_endpoint_rows(
        residual_rows=residual_rows,
        issuers=issuers,
        profile_websites=profiles,
        inferred_domains=inferred,
    )
    expected_pairs = int(
        residual_manifest.get("retrieval_eligible_pair_count") or 0
    )
    if len(pair_rows) != expected_pairs:
        errors.append(
            f"pair rows={len(pair_rows)} expected={expected_pairs}"
        )
    if len(endpoints) != len(eligible_tickers):
        errors.append(
            f"endpoint rows={len(endpoints)} "
            f"expected={len(eligible_tickers)}"
        )
    endpoint_path = (
        output_dir
        / "transportation_non_sec_endpoint_roots.csv"
    )
    pair_path = (
        output_dir
        / "transportation_non_sec_pair_endpoint_map.csv"
    )
    manifest_path = (
        output_dir
        / "transportation_non_sec_endpoint_manifest.json"
    )
    write_csv_atomic(endpoint_path, ENDPOINT_FIELDS, endpoints)
    write_csv_atomic(pair_path, PAIR_ENDPOINT_FIELDS, pair_rows)
    basis_counts = Counter(
        str(row["source_basis"]) for row in endpoints
    )
    type_counts = Counter(
        str(row["endpoint_type"]) for row in endpoints
    )
    payload = {
        "acceptance": (
            "PASS" if endpoints and pair_rows and not errors else "FAIL"
        ),
        "gate": "DP6K_NON_SEC_ENDPOINT_ROOT_SEAL",
        "endpoint_version": NON_SEC_ENDPOINT_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": str(parser_cfg["source_census_asof_date"]),
        "endpoint_root_count": len(endpoints),
        "mapped_pair_count": len(pair_rows),
        "mapped_ticker_count": len(
            {str(row["ticker"]) for row in pair_rows}
        ),
        "source_basis_counts": dict(sorted(basis_counts.items())),
        "endpoint_type_counts": dict(sorted(type_counts.items())),
        "profile_cache_website_count": sum(
            row["source_basis"]
            == "CACHED_YAHOO_ISSUER_PROFILE_WEBSITE"
            for row in endpoints
        ),
        "filing_inferred_domain_count": sum(
            row["source_basis"]
            == "ISSUER_DOMAIN_INFERRED_FROM_SEC_LINKS"
            for row in endpoints
        ),
        "local_domain_discovery_document_open_count": (
            local_document_open_count
        ),
        "unresolved_endpoint_count": len(errors),
        "endpoint_roots_hash_sealed": not errors,
        "document_urls_enumerated": False,
        "retrieval_authorized": False,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "input": {
            "path": str(residual_path.resolve()),
            "sha256": file_sha256(residual_path),
        },
        "artifacts": {
            "endpoint_roots": {
                "path": str(endpoint_path.resolve()),
                "row_count": len(endpoints),
                "sha256": file_sha256(endpoint_path),
            },
            "pair_endpoint_map": {
                "path": str(pair_path.resolve()),
                "row_count": len(pair_rows),
                "sha256": file_sha256(pair_path),
            },
        },
        "next_gate": (
            "FREEZE_SEMANTIC_FIXTURES_BEFORE_ONE_PASS_HYDRATION"
            if not errors
            else "REVIEW_UNRESOLVED_ISSUER_DOMAINS"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
