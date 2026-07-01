#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from contextlib import closing
from dataclasses import dataclass
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
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_industrials_scoring_eligibility_policy")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "ticker",
    "asof_date",
    "model_family",
    "development_stage",
    "reporting_profile",
    "financial_confidence",
    "data_quality_status",
    "rank_ready_policy",
    "calibration_policy",
    "financial_component_policy",
    "minimum_financial_confidence",
    "policy_status",
    "review_reason",
]


@dataclass(frozen=True)
class PolicyRow:
    reporting_profile: str
    development_stage: str
    rank_ready_policy: str
    calibration_policy: str
    financial_component_policy: str
    minimum_financial_confidence: float
    review_reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Stage 6 scoring eligibility policy coverage.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="")
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
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


def csv_value(row: dict[str, str], key: str) -> str:
    return str(row.get(key) or "").strip()


def load_policy(path: Path) -> dict[tuple[str, str], PolicyRow]:
    policies: dict[tuple[str, str], PolicyRow] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            profile = csv_value(row, "reporting_profile")
            stage = csv_value(row, "development_stage") or "any"
            if not profile:
                raise ValueError(f"{path}:{line_number} missing reporting_profile")
            key = (profile, stage)
            if key in policies:
                raise ValueError(f"{path}:{line_number} duplicate policy profile={profile} development_stage={stage}")
            minimum_confidence = as_float(csv_value(row, "minimum_financial_confidence"))
            if minimum_confidence is None:
                raise ValueError(f"{path}:{line_number} missing minimum_financial_confidence")
            policies[key] = PolicyRow(
                reporting_profile=profile,
                development_stage=stage,
                rank_ready_policy=csv_value(row, "rank_ready_policy"),
                calibration_policy=csv_value(row, "calibration_policy"),
                financial_component_policy=csv_value(row, "financial_component_policy"),
                minimum_financial_confidence=minimum_confidence,
                review_reason=csv_value(row, "review_reason"),
            )
    return policies


def resolve_policy(policies: dict[tuple[str, str], PolicyRow], *, profile: str, development_stage: str) -> PolicyRow | None:
    return policies.get((profile, development_stage)) or policies.get((profile, "any")) or policies.get(("NO_FINANCIALS_REVIEW", "any"))


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
        SELECT c.ticker, t.development_stage,
               COALESCE(p.reporting_profile, f.reporting_profile, 'NO_FINANCIALS_REVIEW') AS reporting_profile,
               COALESCE(f.financial_confidence, p.financial_confidence, 0.0) AS financial_confidence,
               COALESCE(f.data_quality_status, p.fallback_status, 'review') AS data_quality_status
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
         AND f.asof_date = ?
         AND f.source_id = ?
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family, *profile_source_ids, asof.isoformat(), feature_source_id),
    ).fetchall()
    return [dict(row) for row in rows]


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    policy_path_raw = str(cfg_get(config, "scoring_policy.defense_eligibility_policy_csv", "") or "").strip()
    if not policy_path_raw:
        raise ValueError("scoring_policy.defense_eligibility_policy_csv is required")
    policy_path = resolve_path(policy_path_raw, base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path("../output/industrials/defense/stage6/scoring_eligibility_policy_audit.csv", base_dir=base_dir)
    policies = load_policy(policy_path)
    feature_source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    submissions_source_id = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions") or "sec_submissions")
    profile_source_ids = list(dict.fromkeys([feature_source_id, submissions_source_id]))

    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)))) as conn:
        init_db(conn)
        effective_asof = parse_date(args.asof) or latest_feature_asof(conn, model_family=model_family, source_id=feature_source_id)
        if effective_asof is None:
            raise ValueError(f"No financial feature asof available for model_family={model_family} source_id={feature_source_id}")
        subjects = load_policy_subjects(conn, model_family=model_family, asof=effective_asof, feature_source_id=feature_source_id, profile_source_ids=profile_source_ids)
    if not subjects:
        raise ValueError(f"No active tickers found for model_family={model_family}")

    report_rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for subject in subjects:
        ticker = normalize_ticker(subject.get("ticker"))
        development_stage = str(subject.get("development_stage") or "operating")
        profile = str(subject.get("reporting_profile") or "NO_FINANCIALS_REVIEW")
        policy = resolve_policy(policies, profile=profile, development_stage=development_stage)
        if policy is None:
            missing.append(f"{ticker}:{profile}:{development_stage}")
            continue
        confidence = as_float(subject.get("financial_confidence")) or 0.0
        policy_status = "policy_pass"
        reasons: list[str] = []
        if confidence < policy.minimum_financial_confidence:
            policy_status = "policy_review"
            reasons.append(f"financial_confidence_below_policy_min_{policy.minimum_financial_confidence:.2f}")
        if policy.review_reason:
            reasons.append(policy.review_reason)
        report_rows.append(
            {
                "ticker": ticker,
                "asof_date": effective_asof.isoformat(),
                "model_family": model_family,
                "development_stage": development_stage,
                "reporting_profile": profile,
                "financial_confidence": round(confidence, 4),
                "data_quality_status": subject.get("data_quality_status", ""),
                "rank_ready_policy": policy.rank_ready_policy,
                "calibration_policy": policy.calibration_policy,
                "financial_component_policy": policy.financial_component_policy,
                "minimum_financial_confidence": policy.minimum_financial_confidence,
                "policy_status": policy_status,
                "review_reason": ";".join(reasons),
            }
        )
    if missing:
        raise ValueError(f"Missing scoring eligibility policy rows: {', '.join(missing[:20])}")
    write_report(output_csv, report_rows)
    review_count = sum(1 for row in report_rows if row["policy_status"] != "policy_pass")
    LOGGER.info("Wrote scoring eligibility policy audit: %s", output_csv)
    LOGGER.info("Validated scoring eligibility policy: rows=%d review=%d", len(report_rows), review_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
