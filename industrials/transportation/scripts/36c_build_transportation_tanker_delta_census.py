#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation import source_census  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.discovery_contract import (  # noqa: E402
    SCOPE_FIELDS,
    SUPPORTING_SCOPE_FIELDS,
)
from industrials.transportation.investable_universe import (  # noqa: E402
    load_investable_universe_policy,
    validate_investable_universe_policy,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v3.yaml"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a read-only, versioned source census for the 11-name oil-"
            "tanker cohort and the 16 direct marine parser metrics."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _read_csv(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _build_scope_rows(
    *,
    policy: Any,
    registry_path: Path,
) -> list[dict[str, object]]:
    registry = {
        str(row["metric_id"]): row for row in _read_csv(registry_path)
    }
    missing = sorted(set(policy.direct_tanker_metrics) - set(registry))
    if missing:
        raise ValueError(f"direct tanker metrics absent from registry={missing}")
    rows: list[dict[str, object]] = []
    contract_hash = file_sha256(policy.path)
    for ticker in policy.tanker_tickers:
        for metric_id in policy.direct_tanker_metrics:
            metric = registry[metric_id]
            if metric["source_lane"] != "DP":
                raise ValueError(
                    f"{metric_id}: direct tanker metric must use source_lane=DP"
                )
            rows.append(
                {
                    "scope_version": "transportation_tanker_delta_scope_v3",
                    "registry_version": "transportation_metrics_v3_discovery",
                    "policy_version": policy.path.stem,
                    "input_contract_hash": contract_hash,
                    "ticker": ticker,
                    "universe_role": "active",
                    "calibration_cohort": "marine_shipping_and_maritime",
                    "industry": "Marine Shipping",
                    "primary_archetype": "marine_operator",
                    "applicability_tags": "marine_operator|oil_tanker_operator",
                    "development_overlay": "0",
                    "metric_id": metric_id,
                    "metric_pack": metric["metric_pack"],
                    "source_lane": metric["source_lane"],
                    "applicability_status": "APPLICABLE",
                    "applicability_reason": "v3_oil_tanker_operator_scope",
                    "unit_contract": metric["unit_contract"],
                    "period_type": metric["period_type"],
                    "max_staleness_days": metric["max_staleness_days"],
                    "scoring_posture": metric["scoring_posture"],
                    "comparison_population": metric["comparison_population"],
                    "bounds_policy": metric["bounds_policy"],
                    "discovery_status": "coverage_pending",
                }
            )
    return rows


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    universe = family["universe"]
    foundation = resolve_foundation(config_path, args.db)
    policy = load_investable_universe_policy(args.policy)
    errors, _ = validate_investable_universe_policy(policy)
    if errors:
        raise ValueError(f"investable-universe policy is invalid: {errors}")
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            parser_cfg["tanker_delta_output_root"], base_dir=base_dir
        )
        / args.asof
    )
    scope_path = output_dir / "transportation_tanker_delta_scope.csv"
    support_scope_path = output_dir / "transportation_tanker_delta_support_scope.csv"
    dp0_path = output_dir / "transportation_tanker_delta_dp0.json"
    gap_override_path = output_dir / "transportation_tanker_delta_gap_overrides.csv"
    census_path = output_dir / "transportation_tanker_delta_source_census.csv"
    decisions_path = output_dir / "transportation_tanker_delta_source_decisions.csv"
    gaps_path = output_dir / "transportation_tanker_delta_cache_gaps.csv"
    manifest_path = output_dir / "transportation_tanker_delta_census_manifest.json"
    event_audit_path = output_dir / "transportation_tanker_excluded_event_anchor_audit.csv"
    event_audit_manifest_path = output_dir / "transportation_tanker_excluded_event_anchor_audit.json"

    scope_rows = _build_scope_rows(
        policy=policy,
        registry_path=resolve_path(
            parser_cfg["discovery_registry_csv"], base_dir=base_dir
        ),
    )
    write_csv_atomic(scope_path, SCOPE_FIELDS, scope_rows)
    write_csv_atomic(support_scope_path, SUPPORTING_SCOPE_FIELDS, [])
    write_csv_atomic(
        gap_override_path,
        source_census.GAP_OVERRIDE_FIELDS,
        [],
    )
    write_text_atomic(
        dp0_path,
        json.dumps(
            {
                "model_family": MODEL_FAMILY,
                "contract_version": "transportation_tanker_delta_dp0_v3",
                "identity_count": len(policy.tanker_tickers),
                "direct_metric_count": len(policy.direct_tanker_metrics),
                "hashes": {"scope_sha256": file_sha256(scope_path)},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )

    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir
    )
    original_members = source_census._members
    original_registry = source_census.get_registry
    full_registry = original_registry()
    event_metric_anchor_accessions: frozenset[tuple[str, str]] = frozenset()
    event_audit_sha256 = ""
    if event_audit_path.is_file() or event_audit_manifest_path.is_file():
        if not event_audit_path.is_file() or not event_audit_manifest_path.is_file():
            raise ValueError("excluded-event anchor audit CSV/manifest must exist together")
        event_audit_manifest = json.loads(event_audit_manifest_path.read_text(encoding="utf-8"))
        if (
            event_audit_manifest.get("acceptance") != "PASS"
            or event_audit_manifest.get("asof_date") != args.asof
            or event_audit_manifest.get("adapter_version") != full_registry.adapter_version
            or int(event_audit_manifest.get("network_requests", -1)) != 0
        ):
            raise ValueError("excluded-event anchor audit does not match the sealed offline census context")
        event_rows = _read_csv(event_audit_path)
        event_metric_anchor_accessions = frozenset(
            (str(row["ticker"]).upper(), str(row["accession_number"]))
            for row in event_rows
        )
        if len(event_metric_anchor_accessions) != int(
            event_audit_manifest.get("positive_accession_count", -1)
        ):
            raise ValueError("excluded-event anchor audit accession count does not reconcile")
        event_audit_sha256 = file_sha256(event_audit_path)
    direct_requests = tuple(
        request
        for request in full_registry.parser_metrics
        if request.metric_name in set(policy.direct_tanker_metrics)
    )
    if len(direct_requests) != len(policy.direct_tanker_metrics):
        found = {request.metric_name for request in direct_requests}
        raise ValueError(
            "adapter does not expose every direct tanker metric: "
            f"missing={sorted(set(policy.direct_tanker_metrics) - found)}"
        )

    def selected_members(*call_args: Any, **call_kwargs: Any) -> dict[str, dict[str, str]]:
        members = original_members(*call_args, **call_kwargs)
        return {
            ticker: members[ticker]
            for ticker in policy.tanker_tickers
            if ticker in members
        }

    source_census._members = selected_members
    source_census.get_registry = lambda: SimpleNamespace(
        parser_metrics=direct_requests,
        document_keywords=full_registry.document_keywords,
        adapter_version=full_registry.adapter_version,
    )
    try:
        with source_census.read_only_connection(
            foundation.db_path,
            timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)),
        ) as connection:
            first = source_census.build_source_census(
                connection,
                cache_dir=cache_dir,
                submissions_cache_dir=cache_dir / "sec_submissions",
                final_scope_path=scope_path,
                support_scope_path=support_scope_path,
                listing_dates_path=foundation.listing_path,
                continuity_path=resolve_path(
                    universe["security_continuity_overrides_csv"],
                    base_dir=base_dir,
                ),
                dp0_manifest_path=dp0_path,
                gap_override_path=gap_override_path,
                manifest_version="transportation_tanker_delta_census_v3",
                source_id=str(cfg_get(config, "sec_fundamentals.submissions_source_id")),
                active_source_id=foundation.seed_source_id,
                historical_source_id=foundation.historical_source_id,
                start_date="2017-11-28",
                legacy_inactive_start_date="2000-01-01",
                asof_date=args.asof,
                expected_identity_count=len(policy.tanker_tickers),
                expected_base_accession_count=-1,
                event_metric_anchor_accessions=event_metric_anchor_accessions,
            )
            expected_base = int(first[3]["base_accession_count"])
            census_rows, decisions, gaps, summary = source_census.build_source_census(
                connection,
                cache_dir=cache_dir,
                submissions_cache_dir=cache_dir / "sec_submissions",
                final_scope_path=scope_path,
                support_scope_path=support_scope_path,
                listing_dates_path=foundation.listing_path,
                continuity_path=resolve_path(
                    universe["security_continuity_overrides_csv"],
                    base_dir=base_dir,
                ),
                dp0_manifest_path=dp0_path,
                gap_override_path=gap_override_path,
                manifest_version="transportation_tanker_delta_census_v3",
                source_id=str(cfg_get(config, "sec_fundamentals.submissions_source_id")),
                active_source_id=foundation.seed_source_id,
                historical_source_id=foundation.historical_source_id,
                start_date="2017-11-28",
                legacy_inactive_start_date="2000-01-01",
                asof_date=args.asof,
                expected_identity_count=len(policy.tanker_tickers),
                expected_base_accession_count=expected_base,
                event_metric_anchor_accessions=event_metric_anchor_accessions,
            )
    finally:
        source_census._members = original_members
        source_census.get_registry = original_registry

    payload = source_census.write_source_census(
        census_rows=census_rows,
        decisions=decisions,
        gaps=gaps,
        summary=summary,
        census_path=census_path,
        decisions_path=decisions_path,
        gaps_path=gaps_path,
        manifest_path=manifest_path,
    )
    payload["excluded_event_anchor_audit"] = {
        "path": str(event_audit_path) if event_audit_sha256 else "",
        "sha256": event_audit_sha256,
        "positive_accession_count": len(event_metric_anchor_accessions),
        "selection_rule": "supplemental_event_metric_anchor_audit",
        "network_requests": 0,
    }
    payload["execution_scope"] = {
        "tickers": list(policy.tanker_tickers),
        "direct_metric_ids": list(policy.direct_tanker_metrics),
        "derived_metric_ids": list(policy.derived_tanker_metrics),
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
