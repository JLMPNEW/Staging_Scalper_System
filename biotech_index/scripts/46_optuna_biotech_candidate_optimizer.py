#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("optuna_biotech_candidate_optimizer")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "biotech_index_reports" / "optuna_candidate_optimizer"

REQUIRED_CALIBRATION_FILES = (
    "tier1_weight_calibration_manifest.json",
    "tier1_weight_calibration_holdout.csv",
)
OPTIONAL_CALIBRATION_FILES = (
    "tier1_weight_calibration_bootstrap_ci.csv",
    "tier1_weight_calibration_best.csv",
)
REQUIRED_IC_FILES = (
    "feature_ic_classification.csv",
    "feature_ic_summary.csv",
)
PROMOTABLE_IC_CLASSES = frozenset({"promote_candidate", "cohort_specific_only"})
AUTHORIZED_FACTOR_VALIDATION_CONTRACT = "factor_validation_v1"
SUCCESS_MANIFEST_STATUSES = frozenset({"success", "completed", "complete"})
WEIGHT_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "w_lcb": (0.50, 2.00),
    "w_mean": (0.00, 0.75),
    "w_hit": (0.00, 0.75),
    "w_profit": (0.00, 0.75),
    "w_sortino": (0.00, 0.60),
    "w_loss20": (0.40, 2.00),
    "w_loss40": (0.40, 2.00),
    "w_top3": (0.20, 1.50),
}
# Fixed (not Optuna-searched) penalty on the train->test overfit gap. If this were a
# searched parameter with a 0.0 lower bound, the maximizer would drive it to 0 and the
# objective would collapse to raw test_score (optimizing directly against the test split).
TRAIN_TEST_GAP_PENALTY = 0.75


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Gated Optuna meta-optimizer for biotech candidate structures. "
            "This runs only after clean historical QA, IC/monotonicity, and candidate calibration."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--input-dir", type=Path, required=True, help="Candidate calibration output directory.")
    parser.add_argument("--feature-ic-dir", type=Path, required=True, help="Feature IC monitor output directory.")
    parser.add_argument("--sequence-dir", type=Path, default=None, help="Clean historical sequence root directory.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="20,60,120")
    parser.add_argument("--top-n", type=str, default="10,20")
    parser.add_argument("--n-trials", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7331)
    parser.add_argument(
        "--top-k-survivors",
        type=int,
        default=3,
        help="Rank-weight this many train-ranked survivors inside each Optuna trial instead of optimizing a single top-1 pick.",
    )
    parser.add_argument("--timeout-sec", type=float, default=None, help="Optional wall-clock timeout for Optuna trials.")
    parser.add_argument(
        "--n-startup-trials",
        type=int,
        default=0,
        help="Optional Optuna TPE startup trials. Defaults to min(25, n_trials // 10).",
    )
    parser.add_argument(
        "--disable-multivariate-tpe",
        action="store_true",
        help="Disable Optuna multivariate TPE. Enabled by default because the weights interact.",
    )
    parser.add_argument("--optuna-log-every", type=int, default=50, help="Log Optuna progress every N completed trials.")
    parser.add_argument(
        "--weight-bounds-json",
        type=str,
        default="",
        help=(
            "Optional JSON object overriding weight bounds, e.g. "
            "'{\"w_lcb\":[0,2],\"w_loss20\":[0.2,2]}'."
        ),
    )
    parser.add_argument("--min-panel-dates", type=int, default=250)
    parser.add_argument("--min-promote-or-cohort-factors", type=int, default=1)
    parser.add_argument("--min-selected-n", type=int, default=60)
    parser.add_argument("--min-lcb-return-pct", type=float, default=0.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.15)
    parser.add_argument(
        "--max-loss20-rate-pct",
        type=float,
        default=30.0,
        help="Maximum 20%% loss rate allowed for Optuna survivors. Defaults to the Tier-1 calibration threshold.",
    )
    parser.add_argument("--max-loss40-rate-pct", type=float, default=12.5)
    parser.add_argument("--max-top3-gain-contribution-pct", type=float, default=50.0)
    parser.add_argument(
        "--allow-missing-clean-qa",
        action="store_true",
        help="For development only. Do not use for production Optuna runs.",
    )
    parser.add_argument("--check-only", action="store_true", help="Run gates and survivor selection without Optuna trials.")
    parser.add_argument("--dry-run", action="store_true", help="Report planned run without importing/running Optuna.")
    return parser.parse_args()


def parse_int_set(raw: str) -> set[int]:
    values: set[int] = set()
    for part in str(raw or "").replace(";", ",").replace("|", ",").split(","):
        text = part.strip()
        if not text:
            continue
        values.add(int(float(text)))
    return values


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "t", "yes", "y", "pass", "passed", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "fail", "failed", "disabled", "off"}:
        return False
    return default


def authorization_output_fields(authorization: dict[str, Any]) -> dict[str, Any]:
    return {
        "factor_validation_authorization_status": str(
            authorization.get("authorization_status") or "research_only"
        ),
        "production_promotion_authorized": int(
            bool(authorization.get("production_promotion_authorized"))
        ),
        "factor_validation_contracts": "|".join(
            str(value) for value in authorization.get("contract_versions", []) if str(value)
        ),
        "factor_validation_evidence_statuses": "|".join(
            str(value) for value in authorization.get("evidence_statuses", []) if str(value)
        ),
        "authorized_factor_count": int(authorization.get("authorized_factor_count") or 0),
        "research_candidate_factor_count": int(
            authorization.get("research_candidate_factor_count") or 0
        ),
    }


def to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    try:
        text = str(raw).strip().replace(",", "")
        if not text:
            return default
        value = float(text)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def required_float(row: dict[str, Any], key: str, default: float) -> float:
    value = to_float(row.get(key), None)
    return default if value is None else value


def threshold_slug(value: float) -> str:
    return f"{float(value):g}".replace("-", "neg").replace(".", "p")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    keys.append(key)
                    seen.add(key)
        fieldnames = keys
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_sequence_dir(input_dir: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    if input_dir.name == "candidate_calibration":
        return input_dir.parent.resolve()
    return input_dir.resolve()


def validate_required_files(input_dir: Path, feature_ic_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for filename in REQUIRED_CALIBRATION_FILES:
        path = input_dir / filename
        rows.append(
            {
                "gate": f"calibration_file:{filename}",
                "status": "PASS" if path.exists() else "FAIL",
                "value": str(path),
                "details": "candidate calibration prerequisite",
            }
        )
    for filename in OPTIONAL_CALIBRATION_FILES:
        path = input_dir / filename
        rows.append(
            {
                "gate": f"optional_calibration_file:{filename}",
                "status": "PASS" if path.exists() else "WARN",
                "value": str(path),
                "details": "optional calibration diagnostic; not required for optimizer execution",
            }
        )
    for filename in REQUIRED_IC_FILES:
        path = feature_ic_dir / filename
        rows.append(
            {
                "gate": f"feature_ic_file:{filename}",
                "status": "PASS" if path.exists() else "FAIL",
                "value": str(path),
                "details": "IC/monotonicity prerequisite",
            }
        )
    return rows


def validate_calibration_manifest(
    manifest: dict[str, Any],
    *,
    requested_start_asof: str,
    requested_end_asof: str,
    manifest_path: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    status = str(manifest.get("status") or "").strip().lower()
    if status:
        rows.append(
            {
                "gate": "calibration_manifest_status",
                "status": "PASS" if status in SUCCESS_MANIFEST_STATUSES else "FAIL",
                "value": status,
                "details": str(manifest_path),
            }
        )
    else:
        rows.append(
            {
                "gate": "calibration_manifest_status",
                "status": "WARN",
                "value": "",
                "details": f"manifest has no explicit status; file={manifest_path}",
            }
        )

    snapshot_dates = [
        str(value)[:10]
        for value in (manifest.get("snapshot_dates") or [])
        if str(value or "").strip()
    ]
    for key, requested in (("start_asof", requested_start_asof), ("end_asof", requested_end_asof)):
        requested_clean = str(requested or "").strip()
        if not requested_clean:
            continue
        manifest_value = str(manifest.get(key) or "").strip()
        if not manifest_value:
            if snapshot_dates:
                if key == "start_asof":
                    out_of_range = [value for value in snapshot_dates if value < requested_clean]
                else:
                    out_of_range = [value for value in snapshot_dates if value > requested_clean]
                rows.append(
                    {
                        "gate": f"calibration_manifest_{key}",
                        "status": "PASS" if not out_of_range else "FAIL",
                        "value": f"snapshot_dates={len(snapshot_dates)}",
                        "details": (
                            f"requested={requested_clean}; manifest lacks {key}; "
                            f"out_of_range_snapshot_dates={len(out_of_range)}"
                        ),
                    }
                )
                continue
            rows.append(
                {
                    "gate": f"calibration_manifest_{key}",
                    "status": "WARN",
                    "value": "",
                    "details": f"requested {key}={requested_clean}, but manifest has no {key} metadata",
                }
            )
        else:
            rows.append(
                {
                    "gate": f"calibration_manifest_{key}",
                    "status": "PASS" if manifest_value == requested_clean else "FAIL",
                    "value": manifest_value,
                    "details": f"requested={requested_clean}; file={manifest_path}",
                }
            )
    return rows


def validate_db_path(db_path: Path) -> list[dict[str, Any]]:
    return [
        {
            "gate": "database_path_exists",
            "status": "PASS" if db_path.exists() else "FAIL",
            "value": str(db_path),
            "details": "Resolved --db path or paths.database_path from config",
        }
    ]


def validate_clean_qa(sequence_dir: Path, *, min_panel_dates: int, allow_missing: bool) -> list[dict[str, Any]]:
    summary_path = sequence_dir / "clean_historical_qa_summary.csv"
    panel_path = sequence_dir / "clean_historical_panel_qa_by_asof.csv"
    if allow_missing and (not summary_path.exists() or not panel_path.exists()):
        return [
            {
                "gate": "clean_panel_qa",
                "status": "WARN",
                "value": "missing_allowed",
                "details": "Missing clean QA was explicitly allowed. Do not use for production promotion.",
            }
        ]
    summary = read_csv(summary_path)
    panel = read_csv(panel_path)
    failed = [row for row in summary if str(row.get("status") or "").strip().upper() == "FAIL"]
    warning_count = sum(1 for row in summary if str(row.get("status") or "").strip().upper() == "WARN")
    panel_failed = [row for row in panel if str(row.get("status") or "").strip().upper() == "FAIL"]
    asof_dates = {str(row.get("asof_date") or "") for row in panel if str(row.get("asof_date") or "").strip()}
    return [
        {
            "gate": "clean_qa_summary_no_fail",
            "status": "PASS" if not failed else "FAIL",
            "value": len(failed),
            "details": f"warnings={warning_count}; file={summary_path}",
        },
        {
            "gate": "clean_panel_dates",
            "status": "PASS" if len(asof_dates) >= min_panel_dates else "FAIL",
            "value": len(asof_dates),
            "details": f"min_panel_dates={min_panel_dates}; file={panel_path}",
        },
        {
            "gate": "clean_panel_date_rows_no_fail",
            "status": "PASS" if not panel_failed else "FAIL",
            "value": len(panel_failed),
            "details": f"file={panel_path}",
        },
    ]


def validate_ic(
    feature_ic_dir: Path,
    *,
    min_promotable: int,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    rows = read_csv(feature_ic_dir / "feature_ic_classification.csv")
    class_counts: dict[str, int] = {}
    research_candidates: set[str] = set()
    authorized_candidates: set[str] = set()
    contract_versions: set[str] = set()
    evidence_statuses: set[str] = set()
    for row in rows:
        classification = str(row.get("classification") or "").strip()
        factor = str(row.get("factor") or "").strip()
        contract = str(row.get("factor_validation_contract") or "").strip()
        evidence_status = str(row.get("evidence_status") or "").strip()
        if contract:
            contract_versions.add(contract)
        if evidence_status:
            evidence_statuses.add(evidence_status)
        if not classification:
            continue
        class_counts[classification] = class_counts.get(classification, 0) + 1
        if factor and classification in PROMOTABLE_IC_CLASSES:
            research_candidates.add(factor)
            if (
                contract == AUTHORIZED_FACTOR_VALIDATION_CONTRACT
                and as_bool(row.get("promotion_eligible"), False)
            ):
                authorized_candidates.add(factor)
    minimum_research_candidates = max(0, int(min_promotable))
    minimum_authorized_candidates = max(1, int(min_promotable))
    production_authorized = len(authorized_candidates) >= minimum_authorized_candidates
    authorization = {
        "authorization_status": "authorized" if production_authorized else "research_only",
        "production_promotion_authorized": production_authorized,
        "authorized_factor_count": len(authorized_candidates),
        "research_candidate_factor_count": len(research_candidates),
        "contract_versions": sorted(contract_versions),
        "evidence_statuses": sorted(evidence_statuses),
        "required_contract": AUTHORIZED_FACTOR_VALIDATION_CONTRACT,
    }
    gate_rows = [
        {
            "gate": "feature_ic_classification_rows",
            "status": "PASS" if rows else "FAIL",
            "value": len(rows),
            "details": str(feature_ic_dir / "feature_ic_classification.csv"),
        },
        {
            "gate": "feature_ic_promote_or_cohort_specific_factor_count",
            "status": "PASS" if len(research_candidates) >= minimum_research_candidates else "FAIL",
            "value": len(research_candidates),
            "details": (
                f"research_only_candidates; min_promote_or_cohort_factors="
                f"{minimum_research_candidates}"
            ),
        },
        {
            "gate": "feature_ic_production_promotion_authorization",
            "status": "PASS" if production_authorized else "WARN",
            "value": len(authorized_candidates),
            "details": (
                f"required_contract={AUTHORIZED_FACTOR_VALIDATION_CONTRACT}; "
                f"authorization_status={authorization['authorization_status']}; "
                f"contracts={','.join(sorted(contract_versions)) or 'missing'}"
            ),
        },
    ]
    return gate_rows, class_counts, authorization


def candidate_key(row: dict[str, str]) -> str:
    return "|".join(
        [
            str(row.get("candidate_id") or ""),
            str(row.get("candidate_name") or ""),
            str(row.get("selection_policy_name") or ""),
            str(row.get("horizon_days") or ""),
            str(row.get("top_n") or ""),
        ]
    )


def filter_survivors(
    rows: list[dict[str, str]],
    *,
    horizons: set[int],
    top_ns: set[int],
    min_selected_n: int,
    min_lcb: float,
    min_profit_factor: float,
    max_loss20: float,
    max_loss40: float,
    max_top3: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    survivors: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        horizon = int(required_float(row, "horizon_days", -1.0))
        top_n = int(required_float(row, "top_n", -1.0))
        if horizons and horizon not in horizons:
            continue
        if top_ns and top_n not in top_ns:
            continue
        train_pass = as_bool(row.get("train_calibration_pass"), False) or str(row.get("train_calibration_pass_state")) == "pass"
        test_pass = as_bool(row.get("test_calibration_pass"), False) or str(row.get("test_calibration_pass_state")) == "pass"
        train_n = required_float(row, "train_n", 0.0)
        test_n = required_float(row, "test_n", 0.0)
        test_lcb = required_float(row, "test_selected_lcb_return_pct", -1e9)
        test_profit = required_float(row, "test_selected_profit_factor", 0.0)
        test_loss20 = required_float(row, "test_selected_large_loss_20pct_rate_pct", 100.0)
        test_loss40 = required_float(row, "test_selected_large_loss_40pct_rate_pct", 100.0)
        test_top3 = required_float(row, "test_selected_top3_gain_contribution_pct", 100.0)
        fail_reasons: list[str] = []
        if not train_pass:
            fail_reasons.append("train_calibration_failed")
        if not test_pass:
            fail_reasons.append("test_calibration_failed")
        if train_n < min_selected_n or test_n < min_selected_n:
            fail_reasons.append("selected_n_below_min")
        if test_lcb < min_lcb:
            fail_reasons.append("test_lcb_below_min")
        if test_profit < min_profit_factor:
            fail_reasons.append("test_profit_factor_below_min")
        if test_loss20 > max_loss20:
            fail_reasons.append(f"test_loss20_above_max_{threshold_slug(max_loss20)}pct")
        if test_loss40 > max_loss40:
            fail_reasons.append(f"test_loss40_above_max_{threshold_slug(max_loss40)}pct")
        if test_top3 > max_top3:
            fail_reasons.append(f"test_top3_concentration_above_max_{threshold_slug(max_top3)}pct")
        out = dict(row)
        out["optimizer_candidate_key"] = candidate_key(row)
        out["optimizer_reject_reasons"] = "|".join(fail_reasons)
        if fail_reasons:
            rejected.append(out)
        else:
            survivors.append(out)
    return survivors, rejected


def metric(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = to_float(row.get(key), None)
    return default if value is None else value


def parse_weight_bounds(raw: str) -> dict[str, tuple[float, float]]:
    bounds = dict(WEIGHT_PARAM_BOUNDS)
    text = str(raw or "").strip()
    if not text:
        return bounds
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("--weight-bounds-json must be a JSON object")
    for name, pair in payload.items():
        if name not in bounds:
            raise ValueError(f"Unsupported optimizer weight bound: {name!r}")
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"Weight bound for {name!r} must be [min, max]")
        low = to_float(pair[0], None)
        high = to_float(pair[1], None)
        if low is None or high is None or low > high:
            raise ValueError(f"Invalid weight bound for {name!r}: {pair!r}")
        bounds[name] = (float(low), float(high))
    return bounds


def normalizer_group_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("horizon_days") or ""), str(row.get("top_n") or ""))


def build_score_normalizers(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], tuple[float, float]]:
    metric_names = (
        "lcb_return_pct",
        "mean_return_pct",
        "hit_rate_pct",
        "profit_factor",
        "sortino_like",
        "large_loss_20pct_rate_pct",
        "large_loss_40pct_rate_pct",
        "top3_gain_contribution_pct",
    )
    # Normalize within each (horizon_days, top_n) group so long horizons (with
    # mechanically larger return magnitudes) do not dominate the pooled z-scores.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(normalizer_group_key(row), []).append(row)
    normalizers: dict[tuple[str, str, str], tuple[float, float]] = {}
    for group_key, group_rows in groups.items():
        for split in ("train", "test"):
            prefix = f"{split}_selected_"
            for metric_name in metric_names:
                key = prefix + metric_name
                values = [to_float(row.get(key), None) for row in group_rows]
                clean = [float(value) for value in values if value is not None]
                if not clean:
                    normalizers[group_key + (key,)] = (0.0, 1.0)
                    continue
                avg = sum(clean) / len(clean)
                variance = sum((value - avg) ** 2 for value in clean) / max(1, len(clean))
                std = math.sqrt(variance)
                normalizers[group_key + (key,)] = (avg, std if std > 1e-9 else 1.0)
    return normalizers


def normalized_metric(
    row: dict[str, Any],
    key: str,
    normalizers: dict[tuple[str, str, str], tuple[float, float]],
    *,
    default: float = 0.0,
    clip: float = 3.0,
) -> float:
    value = metric(row, key, default)
    avg, std = normalizers.get(normalizer_group_key(row) + (key,), (0.0, 1.0))
    z_value = (value - avg) / max(1e-9, std)
    return max(-clip, min(clip, z_value))


def trial_candidate_score(
    row: dict[str, Any],
    params: dict[str, float],
    *,
    split: str,
    normalizers: dict[tuple[str, str, str], tuple[float, float]],
) -> float:
    prefix = f"{split}_selected_"
    lcb = normalized_metric(row, prefix + "lcb_return_pct", normalizers)
    mean = normalized_metric(row, prefix + "mean_return_pct", normalizers)
    hit = normalized_metric(row, prefix + "hit_rate_pct", normalizers)
    profit = normalized_metric(row, prefix + "profit_factor", normalizers)
    sortino = normalized_metric(row, prefix + "sortino_like", normalizers)
    loss20 = normalized_metric(row, prefix + "large_loss_20pct_rate_pct", normalizers)
    loss40 = normalized_metric(row, prefix + "large_loss_40pct_rate_pct", normalizers)
    top3 = normalized_metric(row, prefix + "top3_gain_contribution_pct", normalizers)
    return (
        params["w_lcb"] * lcb
        + params["w_mean"] * mean
        + params["w_hit"] * hit
        + params["w_profit"] * profit
        + params["w_sortino"] * sortino
        - params["w_loss20"] * loss20
        - params["w_loss40"] * loss40
        - params["w_top3"] * top3
    )


def run_optuna_trials(
    survivors: list[dict[str, Any]],
    *,
    n_trials: int,
    seed: int,
    top_k_survivors: int,
    timeout_sec: float | None,
    n_startup_trials: int,
    multivariate_tpe: bool,
    log_every: int,
    weight_bounds: dict[str, tuple[float, float]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        import optuna  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Optuna is not installed. Install with `pip install optuna` before running this step.") from exc

    normalizers = build_score_normalizers(survivors)
    top_k = max(1, int(top_k_survivors))

    def objective(trial: Any) -> float:
        params = {
            name: trial.suggest_float(name, bounds[0], bounds[1])
            for name, bounds in weight_bounds.items()
        }
        train_scored = [
            (trial_candidate_score(row, params, split="train", normalizers=normalizers), row)
            for row in survivors
        ]
        train_scored.sort(key=lambda item: item[0], reverse=True)
        top_rows = train_scored[: min(top_k, len(train_scored))]
        weights = [1.0 / (rank + 1.0) for rank in range(len(top_rows))]
        weight_sum = sum(weights) or 1.0
        selected = top_rows[0][1]
        train_scores = [score for score, _row in top_rows]
        test_scores = [
            trial_candidate_score(row, params, split="test", normalizers=normalizers)
            for _score, row in top_rows
        ]
        train_score = sum(weight * score for weight, score in zip(weights, train_scores)) / weight_sum
        test_score = sum(weight * score for weight, score in zip(weights, test_scores)) / weight_sum
        gap = sum(
            weight * max(0.0, train_value - test_value)
            for weight, train_value, test_value in zip(weights, train_scores, test_scores)
        ) / weight_sum
        trial.set_user_attr("selected_candidate_key", selected["optimizer_candidate_key"])
        trial.set_user_attr(
            "top_k_candidate_keys",
            "||".join(str(row["optimizer_candidate_key"]) for _score, row in top_rows),
        )
        trial.set_user_attr("top_k_survivors", len(top_rows))
        trial.set_user_attr("candidate_name", selected.get("candidate_name", ""))
        trial.set_user_attr("selection_policy_name", selected.get("selection_policy_name", ""))
        trial.set_user_attr("horizon_days", selected.get("horizon_days", ""))
        trial.set_user_attr("top_n", selected.get("top_n", ""))
        trial.set_user_attr("train_score", round(train_score, 6))
        trial.set_user_attr("test_score", round(test_score, 6))
        trial.set_user_attr("train_test_overfit_gap", round(gap, 6))
        trial.set_user_attr("raw_test_objective", round(test_score, 6))
        trial.set_user_attr("test_lcb_return_pct", selected.get("test_selected_lcb_return_pct", ""))
        trial.set_user_attr("test_profit_factor", selected.get("test_selected_profit_factor", ""))
        trial.set_user_attr("test_large_loss_20pct_rate_pct", selected.get("test_selected_large_loss_20pct_rate_pct", ""))
        trial.set_user_attr("test_top3_gain_contribution_pct", selected.get("test_selected_top3_gain_contribution_pct", ""))
        trial.set_user_attr("train_test_gap_penalty", TRAIN_TEST_GAP_PENALTY)
        return test_score - TRAIN_TEST_GAP_PENALTY * gap

    startup_trials = int(n_startup_trials) if int(n_startup_trials) > 0 else min(25, max(1, int(n_trials) // 10))
    try:
        sampler = optuna.samplers.TPESampler(
            seed=seed,
            multivariate=bool(multivariate_tpe),
            n_startup_trials=startup_trials,
            constant_liar=True,
        )
    except TypeError:
        sampler = optuna.samplers.TPESampler(seed=seed, n_startup_trials=startup_trials)
    study = optuna.create_study(direction="maximize", sampler=sampler)

    def log_callback(study_obj: Any, trial: Any) -> None:
        every = int(log_every)
        if every <= 0:
            return
        completed = [item for item in study_obj.trials if item.value is not None]
        completed_count = len(completed)
        if completed_count <= 0 or (completed_count != 1 and completed_count % every != 0):
            return
        try:
            best_trial = study_obj.best_trial
            LOGGER.info(
                "Optuna progress: completed=%d/%d best_value=%.6f best_candidate=%s",
                completed_count,
                max(1, int(n_trials)),
                float(study_obj.best_value),
                best_trial.user_attrs.get("candidate_name", ""),
            )
        except ValueError:
            LOGGER.info("Optuna progress: completed=%d/%d no completed best trial yet", completed_count, max(1, int(n_trials)))

    study.optimize(
        objective,
        n_trials=max(1, int(n_trials)),
        timeout=float(timeout_sec) if timeout_sec and timeout_sec > 0 else None,
        callbacks=[log_callback],
        show_progress_bar=False,
    )
    trial_rows: list[dict[str, Any]] = []
    for trial in study.trials:
        row = {
            "trial_number": trial.number,
            "state": str(trial.state),
            "datetime_start": trial.datetime_start.isoformat() if trial.datetime_start else "",
            "datetime_complete": trial.datetime_complete.isoformat() if trial.datetime_complete else "",
            "duration_sec": round(trial.duration.total_seconds(), 6) if trial.duration else "",
            "objective_value": trial.value if trial.value is not None else "",
            **trial.params,
            **trial.user_attrs,
        }
        trial_rows.append(row)
    completed_trials = [trial for trial in study.trials if trial.value is not None]
    if not completed_trials:
        raise RuntimeError("All Optuna trials failed; no completed trial is available.")
    try:
        best_trial = study.best_trial
    except ValueError as exc:
        raise RuntimeError("All Optuna trials failed; no best trial is available.") from exc
    best = {
        "best_trial_number": best_trial.number,
        "best_value": study.best_value,
        "datetime_start": best_trial.datetime_start.isoformat() if best_trial.datetime_start else "",
        "datetime_complete": best_trial.datetime_complete.isoformat() if best_trial.datetime_complete else "",
        "duration_sec": round(best_trial.duration.total_seconds(), 6) if best_trial.duration else "",
        **best_trial.params,
        **best_trial.user_attrs,
    }
    return trial_rows, best


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    start = time.perf_counter()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    input_dir = args.input_dir.expanduser().resolve()
    feature_ic_dir = args.feature_ic_dir.expanduser().resolve()
    sequence_dir = resolve_sequence_dir(input_dir, args.sequence_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / f"{sequence_dir.name}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    horizons = parse_int_set(args.horizons)
    top_ns = parse_int_set(args.top_n)
    weight_bounds = parse_weight_bounds(args.weight_bounds_json)

    gate_rows = validate_required_files(input_dir, feature_ic_dir)
    gate_rows.extend(validate_db_path(db_path))
    manifest_path = input_dir / "tier1_weight_calibration_manifest.json"
    if manifest_path.exists():
        calibration_manifest = read_json(manifest_path)
        gate_rows.extend(
            validate_calibration_manifest(
                calibration_manifest,
                requested_start_asof=args.start_asof,
                requested_end_asof=args.end_asof,
                manifest_path=manifest_path,
            )
        )
    else:
        calibration_manifest = {}
    try:
        gate_rows.extend(
            validate_clean_qa(
                sequence_dir,
                min_panel_dates=max(1, int(args.min_panel_dates)),
                allow_missing=bool(args.allow_missing_clean_qa),
            )
        )
    except FileNotFoundError as exc:
        gate_rows.append(
            {
                "gate": "clean_panel_qa",
                "status": "WARN" if args.allow_missing_clean_qa else "FAIL",
                "value": str(exc),
                "details": "Missing clean historical QA artifacts",
            }
        )
    ic_file_gates = [row for row in gate_rows if str(row.get("gate") or "").startswith("feature_ic_file:")]
    ic_authorization: dict[str, Any] = {
        "authorization_status": "research_only",
        "production_promotion_authorized": False,
        "authorized_factor_count": 0,
        "research_candidate_factor_count": 0,
        "contract_versions": [],
        "evidence_statuses": ["missing_or_unreadable"],
        "required_contract": AUTHORIZED_FACTOR_VALIDATION_CONTRACT,
    }
    if ic_file_gates and all(row["status"] == "PASS" for row in ic_file_gates):
        ic_gates, ic_class_counts, ic_authorization = validate_ic(
            feature_ic_dir,
            min_promotable=max(0, int(args.min_promote_or_cohort_factors)),
        )
        gate_rows.extend(ic_gates)
    else:
        ic_class_counts = {}

    authorization_fields = authorization_output_fields(ic_authorization)
    # This legacy meta-optimizer uses the calibration holdout while selecting
    # objective weights. It remains useful for research diagnostics, but only
    # the nested walk-forward runner may authorize production promotion.
    authorization_fields["factor_validation_production_promotion_authorized"] = authorization_fields[
        "production_promotion_authorized"
    ]
    authorization_fields["production_promotion_authorized"] = 0
    write_json(
        output_dir / "factor_validation_authorization.json",
        {
            **ic_authorization,
            **authorization_fields,
            "feature_ic_dir": str(feature_ic_dir),
        },
    )
    write_csv(output_dir / "optuna_gate_report.csv", gate_rows)
    gate_failures = [row for row in gate_rows if row["status"] == "FAIL"]
    if gate_failures:
        write_json(
            output_dir / "optuna_optimizer_manifest.json",
            {
                "status": "gated",
                "reason": "prerequisite_gate_failed",
                "failures": gate_failures[:20],
                "input_dir": str(input_dir),
                "feature_ic_dir": str(feature_ic_dir),
                "sequence_dir": str(sequence_dir),
                "db_path": str(db_path),
            },
        )
        raise RuntimeError("Optuna prerequisite gates failed. See optuna_gate_report.csv.")

    holdout_rows = read_csv(input_dir / "tier1_weight_calibration_holdout.csv")
    survivors, rejected = filter_survivors(
        holdout_rows,
        horizons=horizons,
        top_ns=top_ns,
        min_selected_n=max(1, int(args.min_selected_n)),
        min_lcb=float(args.min_lcb_return_pct),
        min_profit_factor=float(args.min_profit_factor),
        max_loss20=float(args.max_loss20_rate_pct),
        max_loss40=float(args.max_loss40_rate_pct),
        max_top3=float(args.max_top3_gain_contribution_pct),
    )
    write_csv(
        output_dir / "optuna_survivor_candidates.csv",
        [{**row, **authorization_fields} for row in survivors],
    )
    write_csv(
        output_dir / "optuna_rejected_candidates.csv",
        [{**row, **authorization_fields} for row in rejected],
    )
    if not survivors:
        write_json(
            output_dir / "optuna_optimizer_manifest.json",
            {
                "status": "gated",
                "reason": "no_candidate_calibration_survivors",
                "input_holdout_rows": len(holdout_rows),
                "rejected_rows": len(rejected),
                "constraints": {
                    "horizons": sorted(horizons),
                    "top_n": sorted(top_ns),
                    "min_selected_n": args.min_selected_n,
                    "min_lcb_return_pct": args.min_lcb_return_pct,
                    "min_profit_factor": args.min_profit_factor,
                    "max_loss20_rate_pct": args.max_loss20_rate_pct,
                    "max_loss40_rate_pct": args.max_loss40_rate_pct,
                    "max_top3_gain_contribution_pct": args.max_top3_gain_contribution_pct,
                },
            },
        )
        raise RuntimeError("No candidate structures survived calibration constraints; Optuna not run.")

    if args.dry_run or args.check_only:
        status = "dry_run" if args.dry_run else "check_only"
        write_json(
            output_dir / "optuna_optimizer_manifest.json",
            {
                "status": status,
                "db_path": str(db_path),
                "input_dir": str(input_dir),
                "feature_ic_dir": str(feature_ic_dir),
                "sequence_dir": str(sequence_dir),
                "survivor_count": len(survivors),
                "ic_class_counts": ic_class_counts,
                "n_trials_planned": args.n_trials,
                "top_k_survivors": max(1, int(args.top_k_survivors)),
                "weight_bounds": weight_bounds,
                "elapsed_sec": round(time.perf_counter() - start, 3),
                **authorization_fields,
            },
        )
        LOGGER.info("Optuna candidate optimizer %s: survivors=%d output_dir=%s", status, len(survivors), output_dir)
        return

    if len(survivors) < 2:
        # With a single survivor every z-scored metric is 0, so every trial objective is 0
        # and the "best" Optuna trial would be arbitrary. Emit the survivor deterministically.
        best = {"selection_method": "single_survivor_short_circuit", **survivors[0], **authorization_fields}
        write_csv(output_dir / "optuna_trial_results.csv", [])
        write_csv(output_dir / "optuna_best_candidate.csv", [best])
        write_json(
            output_dir / "optuna_optimizer_manifest.json",
            {
                "status": "success",
                "optimizer_type": "gated_candidate_survivor_meta_optimizer",
                "reason": "single_survivor_short_circuit",
                "db_path": str(db_path),
                "input_dir": str(input_dir),
                "feature_ic_dir": str(feature_ic_dir),
                "sequence_dir": str(sequence_dir),
                "start_asof": args.start_asof,
                "end_asof": args.end_asof,
                "survivor_count": len(survivors),
                "trial_count": 0,
                "best": best,
                "ic_class_counts": ic_class_counts,
                "elapsed_sec": round(time.perf_counter() - start, 3),
                **authorization_fields,
                "notes": [
                    "Only one candidate structure survived calibration constraints; Optuna trials were skipped.",
                    "Weight search over a single survivor is degenerate (all normalized objectives are 0).",
                ],
            },
        )
        LOGGER.info(
            "Optuna candidate optimizer short-circuit: single survivor %s emitted deterministically without trials (output_dir=%s)",
            survivors[0].get("optimizer_candidate_key", ""),
            output_dir,
        )
        return

    trial_rows, best = run_optuna_trials(
        survivors,
        n_trials=args.n_trials,
        seed=args.seed,
        top_k_survivors=max(1, int(args.top_k_survivors)),
        timeout_sec=args.timeout_sec,
        n_startup_trials=int(args.n_startup_trials),
        multivariate_tpe=not bool(args.disable_multivariate_tpe),
        log_every=int(args.optuna_log_every),
        weight_bounds=weight_bounds,
    )
    write_csv(
        output_dir / "optuna_trial_results.csv",
        [{**row, **authorization_fields} for row in trial_rows],
    )
    write_csv(output_dir / "optuna_best_candidate.csv", [{**best, **authorization_fields}])
    write_json(
        output_dir / "optuna_optimizer_manifest.json",
        {
            "status": "success",
            "optimizer_type": "gated_candidate_survivor_meta_optimizer",
            "db_path": str(db_path),
            "input_dir": str(input_dir),
            "feature_ic_dir": str(feature_ic_dir),
            "sequence_dir": str(sequence_dir),
            "start_asof": args.start_asof,
            "end_asof": args.end_asof,
            "survivor_count": len(survivors),
            "trial_count": len(trial_rows),
            "top_k_survivors": max(1, int(args.top_k_survivors)),
            "timeout_sec": args.timeout_sec,
            "weight_bounds": weight_bounds,
            "train_test_gap_penalty": TRAIN_TEST_GAP_PENALTY,
            "calibration_manifest_status": calibration_manifest.get("status", ""),
            "best": best,
            "ic_class_counts": ic_class_counts,
            "elapsed_sec": round(time.perf_counter() - start, 3),
            **authorization_fields,
            "notes": [
                "This optimizer selects among candidate structures that already survived calibration constraints.",
                "It does not discover factor structure from scratch.",
                "Use output as a challenger selection aid, not as automatic production promotion.",
            ],
        },
    )
    LOGGER.info("Optuna candidate optimizer complete: survivors=%d trials=%d output_dir=%s", len(survivors), len(trial_rows), output_dir)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in (0, None):
            raise
        LOGGER.exception("Unhandled exception in main()")
        sys.exit(1)
