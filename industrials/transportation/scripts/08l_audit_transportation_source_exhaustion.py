#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    metric_search_aliases,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)
from industrials.transportation.source_census import (  # noqa: E402
    _members,
    _registration_anchors,
    read_only_connection,
)
from industrials.transportation.source_exhaustion import (  # noqa: E402
    SOURCE_EXHAUSTION_VERSION,
    build_source_exhaustion,
    read_csv,
    write_source_exhaustion,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the complete cached SEC submissions universe for "
            "transportation and emit a metadata-only DP6E delta manifest. "
            "This command performs no network requests, parser work, feature "
            "builds, materialization, calibration, or production changes."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args(argv)


def _input_artifact(
    path: Path,
    *,
    row_count: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
    }
    if row_count is not None:
        payload["row_count"] = row_count
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    universe_cfg = family["universe"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Source-exhaustion audit requires "
            "parser_execution_authorized=false"
        )
    foundation = resolve_foundation(config_path, args.db)
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else (
            resolve_path(parser_cfg["output_root"], base_dir=base_dir)
            / asof_date
        )
    )
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    submissions_cache_dir = cache_dir / "sec_submissions"
    scope_path = resolve_path(
        parser_cfg["scope_manifest_csv"],
        base_dir=base_dir,
    )
    decisions_path = resolve_path(
        parser_cfg["source_decisions_csv"],
        base_dir=base_dir,
    )
    dp3_manifest_path = resolve_path(
        parser_cfg["source_census_manifest_json"],
        base_dir=base_dir,
    )
    metric_acceptance_path = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
        / "transportation_post_review_metric_acceptance.csv"
    )
    post_review_manifest_path = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
        / "transportation_post_review_coverage_manifest.json"
    )
    required = (
        scope_path,
        decisions_path,
        dp3_manifest_path,
        metric_acceptance_path,
        post_review_manifest_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing DP6E source-exhaustion inputs: {missing}"
        )
    dp3_manifest = json.loads(
        dp3_manifest_path.read_text(encoding="utf-8")
    )
    post_review_manifest = json.loads(
        post_review_manifest_path.read_text(encoding="utf-8")
    )
    disposition_counts = post_review_manifest.get(
        "metric_disposition_counts"
    )
    disposition_metric_total = (
        sum(int(value) for value in disposition_counts.values())
        if isinstance(disposition_counts, dict) and disposition_counts
        else -1
    )
    if (
        dp3_manifest.get("acceptance") != "PASS"
        or post_review_manifest.get("acceptance") != "PASS"
        or disposition_metric_total != 90
    ):
        raise ValueError(
            "DP6E requires passing DP3 and DP6D manifests"
        )
    scope_rows = read_csv(scope_path)
    decision_rows = read_csv(decisions_path)
    metric_rows = read_csv(metric_acceptance_path)
    listing_path = resolve_path(
        universe_cfg["listing_dates_csv"],
        base_dir=base_dir,
    )
    continuity_path = resolve_path(
        universe_cfg["security_continuity_overrides_csv"],
        base_dir=base_dir,
    )
    registration_anchors = _registration_anchors(
        listing_dates_path=listing_path,
        continuity_path=continuity_path,
        clipped_history_start="2019-01-02",
    )
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        members = _members(
            connection,
            asof_date=asof_date,
            active_source_id=str(universe_cfg["seed_source_id"]),
            historical_source_id=str(
                universe_cfg["historical_membership_source_id"]
            ),
        )
        (
            filing_rows,
            delta_rows,
            gap_rows,
            form_rows,
            target_rows,
            errors,
            summary,
        ) = build_source_exhaustion(
            connection,
            members=members,
            submissions_cache_dir=submissions_cache_dir,
            cache_dir=cache_dir,
            scope_rows=scope_rows,
            metric_acceptance_rows=metric_rows,
            dp3_decisions=decision_rows,
            metric_aliases=metric_search_aliases(),
            registration_anchors=registration_anchors,
            source_id=str(
                cfg_get(config, "sec_fundamentals.submissions_source_id")
            ),
            active_start_date=str(
                parser_cfg["source_census_start_date"]
            ),
            inactive_start_date=str(
                parser_cfg["source_census_legacy_inactive_start_date"]
            ),
            asof_date=asof_date,
            expected_identity_count=int(
                parser_cfg["source_census_expected_identity_count"]
            ),
            manifest_version=str(
                parser_cfg.get(
                    "source_exhaustion_version",
                    SOURCE_EXHAUSTION_VERSION,
                )
            ),
        )
    summary = {
        **summary,
        "asof_date": asof_date,
        "database_path": str(foundation.db_path),
        "submissions_cache_dir": str(submissions_cache_dir.resolve()),
        "errors": errors,
    }
    payload = write_source_exhaustion(
        filing_rows=filing_rows,
        delta_rows=delta_rows,
        gap_rows=gap_rows,
        form_rows=form_rows,
        metric_rows=target_rows,
        summary=summary,
        input_artifacts={
            "scope": _input_artifact(
                scope_path,
                row_count=len(scope_rows),
            ),
            "dp3_decisions": _input_artifact(
                decisions_path,
                row_count=len(decision_rows),
            ),
            "dp3_manifest": _input_artifact(dp3_manifest_path),
            "post_review_metric_acceptance": _input_artifact(
                metric_acceptance_path,
                row_count=len(metric_rows),
            ),
            "post_review_manifest": _input_artifact(
                post_review_manifest_path
            ),
            "listing_dates": _input_artifact(listing_path),
            "security_continuity": _input_artifact(continuity_path),
        },
        output_dir=output_dir,
        manifest_version=str(
            parser_cfg.get(
                "source_exhaustion_version",
                SOURCE_EXHAUSTION_VERSION,
            )
        ),
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2 if payload["acceptance"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
