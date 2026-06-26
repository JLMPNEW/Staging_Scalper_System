#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.fda_mapping_governance import audit_fda_mapping_governance  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit FDA manufacturer mapping governance for med-devices.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--mapping-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--manual-overrides-csv", type=Path, default=None)
    parser.add_argument("--regression-cases-csv", type=Path, default=None)
    parser.add_argument("--warn-only", action="store_true", help="Write the review queue but do not fail on critical issues.")
    return parser.parse_args()


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        result = audit_fda_mapping_governance(
            conn,
            config=config,
            base_dir=base_dir,
            mapping_csv=args.mapping_csv.expanduser().resolve() if args.mapping_csv else None,
            output_csv=args.output_csv.expanduser().resolve() if args.output_csv else None,
            overrides_csv=args.manual_overrides_csv.expanduser().resolve() if args.manual_overrides_csv else None,
            regression_cases_csv=args.regression_cases_csv.expanduser().resolve() if args.regression_cases_csv else None,
        )
    fail_on_critical = as_bool(cfg_get(config, "fda_mapping_governance.fail_on_critical", True), True) and not args.warn_only
    print(
        "fda_mapping_governance "
        f"output={result.output_csv} issues={result.issue_count} critical={result.critical_count} "
        f"warnings={result.warning_count} ambiguous={result.ambiguous_count} "
        f"high_volume_unmapped={result.high_volume_unmapped_count} "
        f"low_confidence_mapped={result.low_confidence_mapped_count}"
    )
    if fail_on_critical and result.critical_count > 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
