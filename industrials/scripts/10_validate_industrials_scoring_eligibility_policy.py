#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.policy_loader import load_eligibility_policy, resolve_policy  # noqa: E402
from industrials.core.profiles import VALID_REPORTING_PROFILES  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_industrials_scoring_eligibility_policy")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "ticker",
    "asof_date",
    "model_family",
    "development_stage",
    "reporting_profile",
    "profile_source",
    "financial_confidence",
    "data_quality_status",
    "fallback_status",
    "feature_asof_date",
    "rank_ready_policy",
    "calibration_policy",
    "financial_component_policy",
    "minimum_financial_confidence",
    "policy_status",
    "review_reason",
]
VALID_POLICY_STAGES = frozenset({"operating", "development_stage", "any"})
# The rank-table publisher gates on case-sensitive startswith("eligible"); every
# policy verb must open with one of these prefixes or downstream gating misreads it.
RANK_READY_POLICY_PREFIXES = ("eligible", "review", "not_rank_ready", "excluded")
CALIBRATION_POLICY_PREFIXES = ("eligible", "not_eligible", "excluded")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Stage 6 scoring eligibility policy coverage.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="")
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when any policy_review rows are present.")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def as_float(raw: object) -> float | None:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def policy_field(policy: Any, key: str) -> Any:
    if isinstance(policy, dict):
        return policy.get(key)
    return getattr(policy, key, None)


def policy_text(policy: Any, key: str) -> str:
    return str(policy_field(policy, key) or "").strip()


def policy_effective(policy: Any, asof: date) -> bool:
    """PIT gate: a policy row applies only from valid_from (same-day-inclusive) at the evaluation asof.

    Rows without a parseable valid_from are legacy and treated as always effective.
    """
    valid_from = parse_date(policy_field(policy, "valid_from"))
    return valid_from is None or valid_from <= asof


def validate_policy_values(policies: dict[tuple[str, str], Any], *, path: Path) -> None:
    errors: list[str] = []
    for profile, stage in sorted(policies):
        policy = policies[(profile, stage)]
        prefix = f"{path} profile={profile} development_stage={stage}:"
        if profile not in VALID_REPORTING_PROFILES:
            errors.append(f"{prefix} unknown reporting_profile")
        if stage not in VALID_POLICY_STAGES:
            errors.append(f"{prefix} invalid development_stage (expected one of {sorted(VALID_POLICY_STAGES)})")
        minimum_confidence = as_float(policy_field(policy, "minimum_financial_confidence"))
        if minimum_confidence is None or not 0.0 <= minimum_confidence <= 1.0:
            errors.append(f"{prefix} minimum_financial_confidence must be a number in [0, 1]")
        rank_ready = policy_text(policy, "rank_ready_policy")
        if not rank_ready.startswith(RANK_READY_POLICY_PREFIXES):
            errors.append(f"{prefix} rank_ready_policy {rank_ready!r} must start with one of {RANK_READY_POLICY_PREFIXES}")
        calibration = policy_text(policy, "calibration_policy")
        if not calibration.startswith(CALIBRATION_POLICY_PREFIXES):
            errors.append(f"{prefix} calibration_policy {calibration!r} must start with one of {CALIBRATION_POLICY_PREFIXES}")
        if not policy_text(policy, "financial_component_policy"):
            errors.append(f"{prefix} financial_component_policy must not be empty")
    if errors:
        raise ValueError("Invalid scoring eligibility policy values: " + "; ".join(errors[:20]))


def resolve_eligibility_policy_path(config: dict[str, Any], *, base_dir: Path, model_family: str) -> Path:
    family_key = f"scoring_policy.families.{model_family}.eligibility_policy_csv"
    policy_path_raw = str(cfg_get(config, family_key, "") or "").strip()
    if not policy_path_raw and model_family == "defense":
        policy_path_raw = str(cfg_get(config, "scoring_policy.defense_eligibility_policy_csv", "") or "").strip()
    if not policy_path_raw:
        raise ValueError(f"Missing eligibility policy CSV config for model_family={model_family}: set {family_key}")
    policy_path = resolve_path(policy_path_raw, base_dir=base_dir)
    if not policy_path.exists():
        raise FileNotFoundError(f"Eligibility policy CSV not found for model_family={model_family}: {policy_path}")
    return policy_path


def resolve_output_csv(args: argparse.Namespace, config: dict[str, Any], *, base_dir: Path, model_family: str) -> Path:
    if args.output_csv:
        return args.output_csv.expanduser().resolve()
    family_key = f"scoring_policy.families.{model_family}.eligibility_audit_csv"
    output_raw = str(cfg_get(config, family_key, "") or "").strip()
    if output_raw:
        return resolve_path(output_raw, base_dir=base_dir)
    return resolve_path(f"../output/industrials/{model_family}/stage6/scoring_eligibility_policy_audit.csv", base_dir=base_dir)


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("At least one value is required")
    return ",".join("?" for _ in values)


def latest_feature_asof(conn: Any, *, model_family: str, source_id: str) -> date | None:
    row = conn.execute(
        """
        SELECT MAX(asof_date) AS asof_date
        FROM feature_financial_statement
        WHERE model_family = ?
          AND source_id = ?
        """,
        (model_family, source_id),
    ).fetchone()
    return parse_date(row["asof_date"] if row is not None else "")


def load_policy_subjects(conn: Any, *, model_family: str, asof: date, feature_source_id: str, profile_source_ids: list[str]) -> list[dict[str, Any]]:
    profile_ph = placeholders(profile_source_ids)
    rows = conn.execute(
        f"""
        SELECT c.ticker,
               t.development_stage,
               p.reporting_profile AS profile_reporting_profile,
               p.financial_confidence AS profile_financial_confidence,
               p.fallback_status AS profile_fallback_status,
               f.reporting_profile AS feature_reporting_profile,
               f.financial_confidence AS feature_financial_confidence,
               f.data_quality_status AS feature_data_quality_status,
               f.asof_date AS feature_asof_date
        FROM dim_company c
        JOIN dim_industrials_taxonomy t
          ON t.company_id = c.company_id
         AND t.model_family = ?
        LEFT JOIN dim_issuer_reporting_profile p
          ON p.ticker = c.ticker
         AND p.model_family = t.model_family
         AND p.source_id IN ({profile_ph})
        LEFT JOIN feature_financial_statement f
          ON f.ticker = c.ticker
         AND f.model_family = t.model_family
         AND f.source_id = ?
         AND f.asof_date = (
             SELECT MAX(f2.asof_date)
             FROM feature_financial_statement f2
             WHERE f2.ticker = c.ticker
               AND f2.model_family = t.model_family
               AND f2.source_id = ?
               AND f2.asof_date <= ?
         )
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family, *profile_source_ids, feature_source_id, feature_source_id, asof.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    policy_path = resolve_eligibility_policy_path(config, base_dir=base_dir, model_family=model_family)
    output_csv = resolve_output_csv(args, config, base_dir=base_dir, model_family=model_family)
    feature_source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    submissions_source_id = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions") or "sec_submissions")
    profile_source_ids = list(dict.fromkeys([feature_source_id, submissions_source_id]))

    asof_raw = str(args.asof or "").strip()
    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)))) as conn:
        init_db(conn)
        if asof_raw:
            effective_asof = parse_date(asof_raw)
            if effective_asof is None:
                raise ValueError(f"Unparseable --asof value {asof_raw!r}; expected YYYY-MM-DD")
        else:
            effective_asof = latest_feature_asof(conn, model_family=model_family, source_id=feature_source_id)
        if effective_asof is None:
            raise ValueError(f"No financial feature asof available for model_family={model_family} source_id={feature_source_id}")
        subjects = load_policy_subjects(conn, model_family=model_family, asof=effective_asof, feature_source_id=feature_source_id, profile_source_ids=profile_source_ids)
    if not subjects:
        raise ValueError(f"No active tickers found for model_family={model_family}")

    # NEW-2: pass the evaluation asof so versioned (profile, stage) keys select the
    # row effective at that asof instead of raising on the first second version.
    policies = load_eligibility_policy(policy_path, asof=effective_asof)
    validate_policy_values(policies, path=policy_path)
    effective_policies = {key: row for key, row in policies.items() if policy_effective(row, effective_asof)}
    if not effective_policies:
        raise ValueError(f"No scoring eligibility policy rows effective at {effective_asof.isoformat()} in {policy_path}")

    report_rows: list[dict[str, Any]] = []
    missing: list[str] = []
    invalid_profiles: list[str] = []
    for subject in subjects:
        ticker = normalize_ticker(subject.get("ticker"))
        development_stage = str(subject.get("development_stage") or "operating")
        profile_row_profile = str(subject.get("profile_reporting_profile") or "").strip()
        feature_row_profile = str(subject.get("feature_reporting_profile") or "").strip()
        # Single COALESCE precedence: profile AND confidence come from the same row,
        # never a profile from one source graded against the other source's confidence.
        if profile_row_profile:
            profile = profile_row_profile
            confidence = as_float(subject.get("profile_financial_confidence"))
            profile_source = "issuer_profile"
        elif feature_row_profile:
            profile = feature_row_profile
            confidence = as_float(subject.get("feature_financial_confidence"))
            profile_source = "financial_feature"
        else:
            profile = "NO_FINANCIALS_REVIEW"
            confidence = 0.0
            profile_source = "default_no_financials"
        if profile not in VALID_REPORTING_PROFILES:
            invalid_profiles.append(f"{ticker}:{profile}")
            continue
        policy = resolve_policy(effective_policies, profile, development_stage)
        if policy is None:
            missing.append(f"{ticker}:{profile}:{development_stage}")
            continue
        minimum_confidence = as_float(policy_field(policy, "minimum_financial_confidence"))
        if minimum_confidence is None:
            raise ValueError(f"Policy row profile={profile} development_stage={development_stage} lost minimum_financial_confidence after validation")
        policy_status = "policy_pass"
        reasons: list[str] = []
        if confidence is None:
            confidence = 0.0
            policy_status = "policy_review"
            reasons.append(f"missing_financial_confidence_from_{profile_source}")
        feature_asof = parse_date(subject.get("feature_asof_date"))
        if feature_asof is not None and feature_asof < effective_asof:
            policy_status = "policy_review"
            reasons.append(f"stale_financial_feature_asof_{feature_asof.isoformat()}")
        if confidence < minimum_confidence:
            policy_status = "policy_review"
            reasons.append(f"financial_confidence_below_policy_min_{minimum_confidence:.2f}")
        policy_review_reason = policy_text(policy, "review_reason")
        if policy_review_reason:
            reasons.append(policy_review_reason)
        report_rows.append(
            {
                "ticker": ticker,
                "asof_date": effective_asof.isoformat(),
                "model_family": model_family,
                "development_stage": development_stage,
                "reporting_profile": profile,
                "profile_source": profile_source,
                "financial_confidence": round(confidence, 4),
                "data_quality_status": str(subject.get("feature_data_quality_status") or ""),
                "fallback_status": str(subject.get("profile_fallback_status") or ""),
                "feature_asof_date": feature_asof.isoformat() if feature_asof is not None else "",
                "rank_ready_policy": policy_text(policy, "rank_ready_policy"),
                "calibration_policy": policy_text(policy, "calibration_policy"),
                "financial_component_policy": policy_text(policy, "financial_component_policy"),
                "minimum_financial_confidence": minimum_confidence,
                "policy_status": policy_status,
                "review_reason": ";".join(reasons),
            }
        )
    errors: list[str] = []
    if invalid_profiles:
        errors.append(f"Unknown reporting profiles (not in VALID_REPORTING_PROFILES): {', '.join(sorted(invalid_profiles)[:20])}")
    if missing:
        errors.append(f"Missing scoring eligibility policy rows: {', '.join(missing[:20])}")
    if errors:
        raise ValueError("; ".join(errors))
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(output_csv, FIELDNAMES, report_rows)
    review_count = sum(1 for row in report_rows if row["policy_status"] != "policy_pass")
    LOGGER.info("Wrote scoring eligibility policy audit: %s", output_csv)
    LOGGER.info("Validated scoring eligibility policy: rows=%d review=%d", len(report_rows), review_count)
    if args.strict and review_count > 0:
        LOGGER.error("Strict gating: %d policy_review rows present in %s", review_count, output_csv)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
