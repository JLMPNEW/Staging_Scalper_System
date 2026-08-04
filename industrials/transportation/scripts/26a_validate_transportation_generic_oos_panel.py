#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    load_yaml,
    resolve_path,
)
from industrials.core.oos_research import (  # noqa: E402
    artifact_sha256,
    finite_float,
    parse_date,
)
from industrials.core.oos_price_lineage import (  # noqa: E402
    audit_panel_return_lineage,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Independently validate transportation's generic weekly "
            "OOS panel and every source-sidecar hash."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    root = (
        args.input_dir.expanduser().resolve()
        if args.input_dir is not None
        else resolve_path(
            cfg_get(
                config,
                "oos_calibration_standards.families.transportation.research_output_root",
            ),
            base_dir=base_dir,
        )
    )
    panel_path = root / "transportation_generic_oos_panel.csv"
    manifest_path = root / "transportation_generic_oos_panel_manifest.json"
    source_index_path = root / "transportation_generic_oos_source_index.csv"
    split_path = root / "transportation_generic_oos_splits.csv"
    price_slice_path = root / "transportation_generic_oos_price_slice.csv"
    for path in (
        panel_path,
        manifest_path,
        source_index_path,
        split_path,
        price_slice_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = read_rows(panel_path)
    split_rows = read_rows(split_path)
    source_rows = read_rows(source_index_path)
    price_rows = read_rows(price_slice_path)
    issues: list[str] = []
    if manifest.get("acceptance") != "PASS":
        issues.append("source panel manifest did not pass")
    if manifest.get("return_basis") != "next_session_open_execution_excess":
        issues.append("return basis is not D+1 adjusted-open execution excess")
    if manifest.get("survivorship_corrected") is not True:
        issues.append("panel does not claim survivorship correction")
    shared_module = Path(str(manifest.get("shared_oos_module") or ""))
    if (
        not shared_module.is_file()
        or artifact_sha256(shared_module)
        != manifest.get("shared_oos_module_sha256")
    ):
        issues.append("shared OOS module hash mismatch")
    if manifest.get("panel_sha256") != artifact_sha256(panel_path):
        issues.append("panel hash mismatch")
    if manifest.get("source_index_sha256") != artifact_sha256(source_index_path):
        issues.append("source index hash mismatch")
    if manifest.get("split_sha256") != artifact_sha256(split_path):
        issues.append("split hash mismatch")
    price_slice_hash_valid = (
        manifest.get("price_slice_path") == str(price_slice_path)
        and manifest.get("price_slice_sha256")
        == artifact_sha256(price_slice_path)
        and int(manifest.get("price_slice_row_count") or -1)
        == len(price_rows)
    )
    if not price_slice_hash_valid:
        issues.append("frozen price slice path/hash/row-count mismatch")
    if int(manifest.get("panel_row_count") or -1) != len(rows):
        issues.append("panel row count mismatch")
    split_map = {
        row["asof_date"]: row["split"]
        for row in split_rows
    }
    keys: set[tuple[str, str, str]] = set()
    eligible = 0
    available = 0
    reasons: Counter[str] = Counter()
    for row in rows:
        key = (
            row.get("asof_date", ""),
            row.get("ticker", ""),
            row.get("horizon_sessions", ""),
        )
        if key in keys:
            issues.append(f"duplicate panel key={key}")
        keys.add(key)
        asof = parse_date(row.get("asof_date"), field="panel asof")
        entry = (
            parse_date(row["entry_date"])
            if row.get("entry_date")
            else None
        )
        exit_date = (
            parse_date(row["exit_date"])
            if row.get("exit_date")
            else None
        )
        if entry is not None and entry <= asof:
            issues.append(f"{key}: entry is not after signal date")
        if entry is not None and exit_date is not None and exit_date <= entry:
            issues.append(f"{key}: exit is not after entry")
        if row.get("split") != split_map.get(row.get("asof_date", "")):
            issues.append(f"{key}: split mapping mismatch")
        is_eligible = row.get("calibration_eligible_flag") == "1"
        is_available = row.get("outcome_available_flag") == "1"
        eligible += int(is_eligible)
        available += int(is_available)
        if is_eligible and (
            row.get("calibration_use") != "core"
            or row.get("development_stage") != "operating"
            or row.get("rank_ready_flag") != "1"
            or row.get("portfolio_role")
            not in {
                "core_candidate",
                "airline_satellite_research",
            }
        ):
            issues.append(f"{key}: invalid production-universe eligibility")
        if is_available and (
            finite_float(row.get("security_forward_return")) is None
            or finite_float(row.get("benchmark_forward_return")) is None
            or finite_float(row.get("forward_excess_return")) is None
        ):
            issues.append(f"{key}: available outcome has missing return")
        if not is_available:
            reasons[row.get("outcome_unavailable_reason", "")] += 1
    for source in source_rows:
        sidecar = Path(source["sidecar_path"])
        rank_manifest = Path(source["rank_manifest_path"])
        if (
            not sidecar.is_file()
            or artifact_sha256(sidecar) != source["sidecar_sha256"]
        ):
            issues.append(
                f"{source['asof_date']}: source sidecar hash mismatch"
            )
        if (
            not rank_manifest.is_file()
            or artifact_sha256(rank_manifest)
            != source["rank_manifest_sha256"]
        ):
            issues.append(
                f"{source['asof_date']}: source rank manifest hash mismatch"
            )
    return_audit = audit_panel_return_lineage(rows, price_rows)
    return_issues = return_audit["issues"]
    issues.extend(
        f"return reconstruction: {item}"
        for item in (
            return_issues if isinstance(return_issues, list) else []
        )
    )
    split_counts = Counter(row["split"] for row in split_rows)
    for required in ("train", "validation", "holdout"):
        if split_counts[required] < 12:
            issues.append(
                f"{required} has fewer than 12 weekly snapshots"
            )
    report_rows = [
        {
            "gate": "panel_hash_integrity",
            "status": "FAIL" if any("hash mismatch" in item for item in issues) else "PASS",
            "observed": artifact_sha256(panel_path),
            "required": str(manifest.get("panel_sha256") or ""),
        },
        {
            "gate": "survivorship_corrected",
            "status": "PASS" if manifest.get("survivorship_corrected") is True else "FAIL",
            "observed": str(manifest.get("survivorship_corrected")),
            "required": "True",
        },
        {
            "gate": "return_basis",
            "status": "PASS" if manifest.get("return_basis") == "next_session_open_execution_excess" else "FAIL",
            "observed": str(manifest.get("return_basis") or ""),
            "required": "next_session_open_execution_excess",
        },
        {
            "gate": "frozen_price_slice",
            "status": "PASS" if price_slice_hash_valid else "FAIL",
            "observed": artifact_sha256(price_slice_path),
            "required": str(manifest.get("price_slice_sha256") or ""),
        },
        {
            "gate": "independent_return_reconstruction",
            "status": str(return_audit["acceptance"]),
            "observed": json.dumps(
                {
                    "available": return_audit["available_row_count"],
                    "recomputed": return_audit["recomputed_row_count"],
                    "max_error": return_audit["maximum_absolute_error"],
                },
                sort_keys=True,
            ),
            "required": "all available returns reconstructed within 1e-9",
        },
        {
            "gate": "minimum_split_history",
            "status": "PASS" if all(split_counts[item] >= 12 for item in ("train", "validation", "holdout")) else "FAIL",
            "observed": json.dumps(dict(split_counts), sort_keys=True),
            "required": ">=12 train/validation/holdout snapshots",
        },
        {
            "gate": "row_contract",
            "status": "PASS" if not issues else "FAIL",
            "observed": str(len(rows)),
            "required": "unique keys, PIT dates, valid outcomes",
        },
    ]
    report_path = root / "transportation_generic_oos_panel_validation.csv"
    validation_path = root / "transportation_generic_oos_panel_validation.json"
    write_csv_atomic(
        report_path,
        ["gate", "status", "observed", "required"],
        report_rows,
    )
    result = {
        "artifact_family": "transportation_generic_oos_panel_validation",
        "model_family": "transportation",
        "acceptance": "PASS" if not issues else "FAIL",
        "panel_path": str(panel_path),
        "panel_sha256": artifact_sha256(panel_path),
        "panel_row_count": len(rows),
        "weekly_snapshot_count": len(split_rows),
        "split_counts": dict(split_counts),
        "eligible_row_count": eligible,
        "outcome_available_row_count": available,
        "outcome_unavailable_reasons": dict(reasons),
        "price_slice_path": str(price_slice_path),
        "price_slice_sha256": artifact_sha256(price_slice_path),
        "price_slice_row_count": len(price_rows),
        "return_reconstruction": {
            key: value
            for key, value in return_audit.items()
            if key != "issues"
        },
        "source_snapshot_count": len(source_rows),
        "issues": issues[:200],
        "report_csv": str(report_path),
        "report_sha256": artifact_sha256(report_path),
    }
    write_text_atomic(
        validation_path,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
