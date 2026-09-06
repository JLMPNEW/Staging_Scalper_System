"""Build the reviewed Stage 4A SEC reporting-profile census."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.atomic_io import atomic_write_csv  # noqa: E402
from basic_materials.core.config import load_config, resolve_cli_path  # noqa: E402
from basic_materials.core.financial_data_contract import (  # noqa: E402
    load_financial_concept_map,
    load_financial_data_policy,
)
from basic_materials.core.reporting_profiles import (  # noqa: E402
    PROFILE_FIELDS,
    build_reporting_profile_census,
    issuer_seeds,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Basic Materials config path")
    parser.add_argument(
        "--replace-reviewed-contract",
        action="store_true",
        help="Allow replacement of the governed reporting-profile CSV",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Refetch SEC payloads instead of replaying the owned cache",
    )
    parser.add_argument("--output", type=Path, help="Explicit output CSV path")
    parser.add_argument("--cache-dir", type=Path, help="Explicit SEC profile cache directory")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        policy = load_financial_data_policy(config.paths.financial_data_policy)
        metrics = load_financial_concept_map(config.paths.financial_concept_map, policy)
        target = resolve_cli_path(args.output, config.paths.reporting_profiles_csv)
        if target != config.paths.reporting_profiles_csv:
            raise ValueError("Reporting-profile output must remain package-owned")
        if target.exists() and not args.replace_reviewed_contract:
            raise FileExistsError(
                "Reviewed reporting-profile contract exists; "
                "use --replace-reviewed-contract for an intentional rebuild"
            )
        cache = resolve_cli_path(
            args.cache_dir,
            config.paths.cache_root / "sec_reporting_profiles" / policy.as_of_date,
        )
        if not str(cache).casefold().startswith(str(config.paths.cache_root).casefold()):
            raise ValueError("Reporting-profile cache must remain inside the package cache root")
        seeds = issuer_seeds(
            config.paths.universe_csv,
            config.paths.historical_membership_csv,
            as_of=policy.as_of_date,
        )
        source_ids = policy.payload["sources"]
        lag_days = int(
            policy.payload["reporting_profiles"][
                "companyfacts_lag_days_before_fallback"
            ]
        )

        def progress(index: int, total: int, ticker: str) -> None:
            if index == 1 or index % 10 == 0 or index == total:
                print(
                    f"reporting-profile census {index}/{total}: {ticker}",
                    file=sys.stderr,
                    flush=True,
                )

        profiles, cache_stats = build_reporting_profile_census(
            seeds,
            cache_root=cache,
            submissions_url_template=config.sec_fundamentals.submissions_url_template,
            submissions_archive_url_template=(
                config.sec_fundamentals.submissions_archive_url_template
            ),
            companyfacts_url_template=config.sec_fundamentals.companyfacts_url_template,
            user_agent=config.sec_fundamentals.user_agent,
            timeout_seconds=config.sec_fundamentals.timeout_seconds,
            max_retries=config.sec_fundamentals.max_retries,
            request_interval_seconds=config.sec_fundamentals.request_interval_seconds,
            force_refresh=args.force_refresh,
            policy_version=policy.version,
            source_ids=source_ids,
            companyfacts_lag_days=lag_days,
            progress=progress,
        )
        if len(profiles) != policy.expected_total_profiles:
            raise RuntimeError("Built reporting-profile census has the wrong row count")
        atomic_write_csv(target, profiles, PROFILE_FIELDS)
        payload = {
            "succeeded": True,
            "output": str(target),
            "row_count": len(profiles),
            "sha256": _sha256(target),
            "byte_size": target.stat().st_size,
            "policy_version": policy.version,
            "policy_sha256": policy.checksum,
            "metric_count": len(metrics),
            "concept_map_sha256": _sha256(config.paths.financial_concept_map),
            "concept_map_byte_size": config.paths.financial_concept_map.stat().st_size,
            "overrides_sha256": _sha256(config.paths.reporting_overrides_csv),
            "overrides_byte_size": config.paths.reporting_overrides_csv.stat().st_size,
            "profile_status_counts": dict(
                sorted(Counter(row["profile_status"] for row in profiles).items())
            ),
            "annual_form_counts": dict(
                sorted(Counter(row["primary_annual_form"] for row in profiles).items())
            ),
            "accounting_basis_counts": dict(
                sorted(Counter(row["accounting_basis"] for row in profiles).items())
            ),
            "reporting_currency_counts": dict(
                sorted(
                    Counter(
                        row["reporting_currency"] or "UNRESOLVED" for row in profiles
                    ).items()
                )
            ),
            **cache_stats,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"succeeded": False, "error": f"{type(exc).__name__}: {exc}"},
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
