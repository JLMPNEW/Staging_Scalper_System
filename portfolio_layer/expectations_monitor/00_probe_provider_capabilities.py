#!/usr/bin/env python3
"""Probe configured provider capabilities without retaining payloads or credentials."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest, write_text_atomic  # noqa: E402
from portfolio_layer.core.config import load_yaml  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.provider_common import (  # noqa: E402
    ACCESS_STATUSES,
    ProbeResult,
    load_entitlements,
    probe_capability,
    provider_has_access,
    provider_key,
    run_selftest,
)


LOGGER = logging.getLogger("probe_provider_capabilities")
DEFAULT_ENTITLEMENTS = Path(__file__).with_name("provider_entitlements.yaml")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REPORT_FIELDS = [
    "provider",
    "capability",
    "symbol",
    "requested_at_utc",
    "status",
    "http_status",
    "elapsed_ms",
    "payload_kind",
    "row_count",
    "field_names",
    "detail",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--entitlements", type=Path, default=DEFAULT_ENTITLEMENTS)
    parser.add_argument(
        "--provider",
        choices=("all", "alpha_vantage", "fmp", "tiingo"),
        default="all",
    )
    parser.add_argument("--symbols", nargs="*", help="Optional probe symbols; bounded by provider plan")
    parser.add_argument(
        "--symbols-file",
        type=Path,
        help="Optional CSV with a ticker or symbol column; mutually exclusive with --symbols",
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _symbols_from_file(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = {str(value).strip().lower(): str(value) for value in (reader.fieldnames or [])}
        ticker_field = fieldnames.get("ticker") or fieldnames.get("symbol")
        if ticker_field is None:
            raise ValueError(f"Symbols file must contain a ticker or symbol column: {path}")
        return [str(row.get(ticker_field, "")).strip() for row in reader]


def _selected_symbols(
    config: dict[str, Any],
    supplied: list[str] | None,
    symbols_file: Path | None,
    selected_providers: list[str],
) -> list[str]:
    probe = config.get("probe")
    if not isinstance(probe, dict):
        raise ValueError("Entitlements probe block must be a mapping")
    if supplied and symbols_file is not None:
        raise ValueError("--symbols and --symbols-file are mutually exclusive")
    caps = probe.get("max_symbols_by_provider")
    if not isinstance(caps, dict):
        raise ValueError("probe.max_symbols_by_provider must be a mapping")
    missing_caps = [provider for provider in selected_providers if provider not in caps]
    if missing_caps:
        raise ValueError(f"Missing symbol caps for providers: {missing_caps}")
    max_symbols = min(int(caps[provider]) for provider in selected_providers)
    if max_symbols <= 0:
        raise ValueError("Provider symbol caps must be positive")
    raw = (
        _symbols_from_file(symbols_file.resolve())
        if symbols_file is not None
        else supplied
        if supplied
        else probe.get("representative_symbols", [])
    )
    if not isinstance(raw, list):
        raise ValueError("Probe symbols must be a list")
    symbols = list(dict.fromkeys(str(value).strip().upper() for value in raw if str(value).strip()))
    if not symbols:
        raise ValueError("At least one probe symbol is required")
    if len(symbols) > max_symbols:
        raise ValueError(f"Probe requested {len(symbols)} symbols; selected-provider cap is {max_symbols}")
    return symbols


def _markdown_report(results: list[ProbeResult], acceptance: str, as_of: date) -> str:
    grouped: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for result in results:
        grouped[(result.provider, result.capability)][result.status] += 1
    lines = [
        "# Provider Capability Probe",
        "",
        f"- As of: `{as_of.isoformat()}`",
        f"- Acceptance: `{acceptance}`",
        "- Raw provider payloads retained: `NO`",
        "- Credentials retained or printed: `NO`",
        "",
        "| Provider | Capability | Status counts |",
        "|---|---|---|",
    ]
    for (provider, capability), counts in sorted(grouped.items()):
        detail = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        lines.append(f"| {provider} | {capability} | {detail} |")
    lines.extend(
        [
            "",
            "This is an access and schema probe, not proof of point-in-time history, data quality,",
            "retention rights, or suitability for production use.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    paths = resolve_runtime_paths(load_yaml(config_path), config_path)
    configure_utc_logging()
    if args.selftest:
        run_selftest()
        print("provider capability selftest: PASS")
        return 0

    config = load_entitlements(args.entitlements.resolve())
    probe_config = config["probe"]
    providers_config = config["providers"]
    selected = sorted(providers_config) if args.provider == "all" else [args.provider]
    symbols = _selected_symbols(config, args.symbols, args.symbols_file, selected)

    timeout_sec = float(probe_config.get("timeout_sec", 30.0))
    max_bytes = int(probe_config.get("max_response_bytes", 2_000_000))
    max_retries = int(probe_config.get("max_retries", 1))
    default_pause_sec = float(probe_config.get("request_pause_sec", 0.0))
    results: list[ProbeResult] = []
    credential_state: dict[str, dict[str, Any]] = {}

    for provider in selected:
        provider_config = providers_config.get(provider)
        if not isinstance(provider_config, dict) or not bool(provider_config.get("enabled", False)):
            continue
        env_name, key = provider_key(provider_config)
        pause_sec = float(provider_config.get("request_pause_sec", default_pause_sec))
        credential_state[provider] = {"env": env_name, "present": key is not None}
        capabilities = provider_config.get("capabilities")
        if not isinstance(capabilities, dict) or not capabilities:
            raise ValueError(f"Provider {provider} has no configured capabilities")
        for capability, capability_config in sorted(capabilities.items()):
            if not isinstance(capability_config, dict):
                raise ValueError(f"Provider capability {provider}.{capability} must be a mapping")
            for symbol in symbols:
                result = probe_capability(
                    provider=provider,
                    provider_config=provider_config,
                    capability=str(capability),
                    capability_config=capability_config,
                    symbol=symbol,
                    as_of=args.as_of,
                    timeout_sec=timeout_sec,
                    max_response_bytes=max_bytes,
                    max_retries=max_retries,
                )
                results.append(result)
                LOGGER.info("%s %s %s -> %s", provider, capability, symbol, result.status)
                if pause_sec > 0:
                    time.sleep(pause_sec)

    if not results:
        raise RuntimeError("No enabled provider capabilities were selected")
    missing_keys = [provider for provider, state in credential_state.items() if not state["present"]]
    providers_without_access = [provider for provider in selected if not provider_has_access(results, provider)]
    all_capabilities_available = all(
        any(
            row.provider == provider and row.capability == capability and row.status in ACCESS_STATUSES
            for row in results
        )
        for provider in selected
        for capability in providers_config[provider]["capabilities"]
    )
    if missing_keys or providers_without_access:
        acceptance = "FAIL"
    elif all_capabilities_available:
        acceptance = "PASS"
    else:
        acceptance = "PASS_WITH_GAPS"

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / "provider_capabilities" / args.as_of.isoformat()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "provider_capability_results.csv"
    markdown_path = output_dir / "provider_capability_report.md"
    manifest_path = output_dir / "provider_capability_manifest.json"
    write_csv(csv_path, REPORT_FIELDS, [result.as_row() for result in results])
    write_text_atomic(markdown_path, _markdown_report(results, acceptance, args.as_of))

    status_counts = Counter(result.status for result in results)
    input_hashes = {
        str(args.entitlements.resolve()): sha256_file(args.entitlements.resolve()),
        str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
        str(Path(__file__).with_name("provider_common.py").resolve()): sha256_file(
            Path(__file__).with_name("provider_common.py").resolve()
        ),
    }
    if args.symbols_file is not None:
        resolved_symbols_file = args.symbols_file.resolve()
        input_hashes[str(resolved_symbols_file)] = sha256_file(resolved_symbols_file)

    write_manifest(
        manifest_path,
        {
            "schema_version": "provider_capability_manifest_v1",
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "as_of_date": args.as_of.isoformat(),
            "acceptance": acceptance,
            "providers": selected,
            "symbols": symbols,
            "credentials": credential_state,
            "raw_payloads_retained": False,
            "status_counts": dict(sorted(status_counts.items())),
            "blocking": {
                "missing_keys": missing_keys,
                "providers_without_access": providers_without_access,
            },
            "inputs_sha256": input_hashes,
            "outputs_sha256": {
                csv_path.name: sha256_file(csv_path),
                markdown_path.name: sha256_file(markdown_path),
            },
        },
    )
    print(f"PROVIDER CAPABILITY PREFLIGHT: {acceptance}")
    print(f"results: {csv_path}")
    print(f"report:  {markdown_path}")
    return 1 if acceptance == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
