#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, expand_env_vars, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.dedicated_parser_adapter import ADAPTER_VERSION, metric_search_aliases  # noqa: E402
from industrials.transportation.investable_universe import load_investable_universe_policy  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402
from industrials.transportation.supplemental_event_sources import (  # noqa: E402
    AUDIT_FIELDS,
    HYDRATION_FIELDS,
    audit_cached_event_sources,
    audit_patterns,
    hydrate_event_sources,
    read_rows,
)


SURFACE_SOURCE_MAP = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_surface_metric_source_map_v2.csv"
)
INVESTABLE_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v3.yaml"
)
DEFAULT_SURFACE_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hydrate each supplemental 6-K/8-K source once, then audit the "
            "same cached corpus for every specialized metric in the selected cohort."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--cohort", choices=("surface", "tanker", "both"), default="both")
    parser.add_argument("--surface-output-dir", type=Path, default=None)
    parser.add_argument("--tanker-output-dir", type=Path, default=None)
    parser.add_argument("--hydrate", action="store_true")
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    parser.add_argument("--request-spacing-sec", type=float, default=0.15)
    return parser.parse_args()


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _targets(cohort: str) -> tuple[str, ...]:
    if cohort == "surface":
        aliases = metric_search_aliases()
        return tuple(
            sorted(
                {
                    row["metric_id"]
                    for row in _csv_rows(SURFACE_SOURCE_MAP)
                    if row["metric_id"] in aliases
                }
            )
        )
    policy = load_investable_universe_policy(INVESTABLE_POLICY)
    return tuple(sorted(policy.direct_tanker_metrics))


def _context(
    cohort: str,
    *,
    args: argparse.Namespace,
    config: dict[str, object],
    config_path: Path,
) -> tuple[Path, Path, str]:
    family = family_config(config, "transportation")
    if cohort == "surface":
        output_dir = (
            args.surface_output_dir.expanduser().resolve()
            if args.surface_output_dir
            else DEFAULT_SURFACE_ROOT / args.asof
        )
        prefix = "transportation_surface"
    else:
        output_dir = (
            args.tanker_output_dir.expanduser().resolve()
            if args.tanker_output_dir
            else resolve_path(
                family["dedicated_parser"]["tanker_delta_output_root"],
                base_dir=config_path.parent,
            )
            / args.asof
        )
        prefix = "transportation_tanker"
    decisions_path = output_dir / f"{prefix}_delta_source_decisions.csv"
    return output_dir, decisions_path, prefix


def main() -> int:
    args = parse_args()
    if args.max_retries < 1 or args.timeout_sec <= 0 or args.request_spacing_sec < 0:
        raise ValueError("invalid retry/timeout/spacing arguments")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=config_path.parent,
    )
    user_agent = expand_env_vars(str(cfg_get(config, "sec_fundamentals.user_agent")))
    cohorts = ("surface", "tanker") if args.cohort == "both" else (args.cohort,)
    overall: dict[str, object] = {
        "acceptance": "PASS",
        "adapter_version": ADAPTER_VERSION,
        "asof_date": args.asof,
        "cohorts": {},
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
    }

    for cohort in cohorts:
        output_dir, decisions_path, prefix = _context(
            cohort,
            args=args,
            config=config,
            config_path=config_path,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        if not decisions_path.is_file():
            raise FileNotFoundError(
                f"{cohort} decisions missing; run the initial delta census first: {decisions_path}"
            )
        decisions = read_rows(decisions_path)
        hydration_summary: dict[str, object] = {
            "acceptance": "SKIPPED_OFFLINE_ONLY",
            "network_request_count": 0,
        }
        if args.hydrate:
            hydration_rows, hydration_summary = hydrate_event_sources(
                decision_rows=decisions,
                cache_dir=cache_dir,
                user_agent=user_agent,
                max_retries=args.max_retries,
                timeout_sec=args.timeout_sec,
                spacing_sec=args.request_spacing_sec,
            )
            hydration_csv = output_dir / f"{prefix}_excluded_event_hydration.csv"
            hydration_json = output_dir / f"{prefix}_excluded_event_hydration.json"
            write_csv_atomic(hydration_csv, HYDRATION_FIELDS, hydration_rows)
            hydration_summary.update(
                asof_date=args.asof,
                cohort=cohort,
                output_csv=str(hydration_csv),
            )
            write_text_atomic(
                hydration_json,
                json.dumps(hydration_summary, indent=2, sort_keys=True) + "\n",
            )

        targets = _targets(cohort)
        patterns = audit_patterns(metric_search_aliases(), targets)
        audit_rows, audit_summary = audit_cached_event_sources(
            decision_rows=decisions,
            cache_dir=cache_dir,
            patterns=patterns,
        )
        audit_csv = output_dir / f"{prefix}_excluded_event_anchor_audit.csv"
        audit_json = output_dir / f"{prefix}_excluded_event_anchor_audit.json"
        write_csv_atomic(audit_csv, AUDIT_FIELDS, audit_rows)
        audit_summary.update(
            acceptance="PASS",
            adapter_version=ADAPTER_VERSION,
            asof_date=args.asof,
            cohort=cohort,
            target_metric_ids=list(targets),
            source_decisions_sha256=file_sha256(decisions_path),
            output_csv=str(audit_csv),
            semantic_validation_authorized=True,
            historical_reconstruction_authorized=False,
            calibration_authorized=False,
            production_promotion_authorized=False,
        )
        write_text_atomic(
            audit_json,
            json.dumps(audit_summary, indent=2, sort_keys=True) + "\n",
        )
        cohort_acceptance = (
            "PASS"
            if hydration_summary.get("acceptance") in {"PASS", "SKIPPED_OFFLINE_ONLY"}
            else "NO_GO"
        )
        overall["cohorts"][cohort] = {
            "acceptance": cohort_acceptance,
            "hydration": hydration_summary,
            "audit": audit_summary,
        }
        if cohort_acceptance != "PASS":
            overall["acceptance"] = "NO_GO"

    print(json.dumps(overall, indent=2, sort_keys=True))
    return 0 if overall["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

