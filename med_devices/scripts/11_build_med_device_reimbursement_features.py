#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_org_name, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_med_device_reimbursement_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "score",
    "coverage_clarity_score",
    "payment_adequacy_score",
    "policy_evidence_count",
    "company_mention_count",
    "mapped_product_code_count",
    "reimbursement_code_count",
    "rate_row_count",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "review_reason",
]


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str


@dataclass(frozen=True)
class ReimbursementPolicy:
    source_ids: list[str]
    no_data_score: float
    no_data_coverage_clarity_score: float
    no_data_payment_adequacy_score: float
    company_mention_score: float
    policy_evidence_score: float
    rate_evidence_score: float
    coverage_weight: float
    payment_weight: float
    mention_count_boost_per_hit: float
    mention_count_boost_cap: float
    low_confidence_hard_flag: bool
    use_fallback_policy_scan_when_unmapped: bool


@dataclass
class ReimbursementFeatureRow:
    asof_date: str
    company_id: int
    ticker: str
    company_name: str
    score: float = 0.0
    coverage_clarity_score: float = 0.0
    payment_adequacy_score: float = 0.0
    policy_evidence_count: int = 0
    company_mention_count: int = 0
    mapped_product_code_count: int = 0
    reimbursement_code_count: int = 0
    rate_row_count: int = 0
    hard_red_flag: int = 0
    hard_red_flag_reasons: list[str] | None = None
    review_reason: str = ""
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class PolicySearchRow:
    policy_id: str
    policy_type: str
    title: str
    related_codes: str
    payload_json: str


@dataclass(frozen=True)
class CompanyReimbursementEvidence:
    policy_evidence_count: int
    company_mention_count: int
    reimbursement_code_count: int
    rate_row_count: int
    matched_codes: set[str]
    matched_policy_ids: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device reimbursement and market-access feature rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def cfg_float(config: dict[str, Any], dotted_key: str, default: float) -> float:
    value = to_float(cfg_get(config, dotted_key, default))
    if value is None:
        raise ValueError(f"Config value must be numeric: {dotted_key}")
    return value


def cfg_bool(config: dict[str, Any], dotted_key: str, default: bool) -> bool:
    raw = cfg_get(config, dotted_key, default)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def reimbursement_policy(config: dict[str, Any]) -> ReimbursementPolicy:
    source_ids_raw = cfg_get(config, "reimbursement_features.source_ids", ["cms_coverage_api", "cms_payment_files"])
    source_ids = [str(value).strip() for value in source_ids_raw] if isinstance(source_ids_raw, list) else ["cms_coverage_api", "cms_payment_files"]
    return ReimbursementPolicy(
        source_ids=[source_id for source_id in source_ids if source_id],
        no_data_score=cfg_float(config, "reimbursement_features.no_data_score", 25.0),
        no_data_coverage_clarity_score=cfg_float(config, "reimbursement_features.no_data_coverage_clarity_score", 25.0),
        no_data_payment_adequacy_score=cfg_float(config, "reimbursement_features.no_data_payment_adequacy_score", 25.0),
        company_mention_score=cfg_float(config, "reimbursement_features.company_mention_score", 45.0),
        policy_evidence_score=cfg_float(config, "reimbursement_features.policy_evidence_score", 60.0),
        rate_evidence_score=cfg_float(config, "reimbursement_features.rate_evidence_score", 65.0),
        coverage_weight=cfg_float(config, "reimbursement_features.coverage_weight", 0.60),
        payment_weight=cfg_float(config, "reimbursement_features.payment_weight", 0.40),
        mention_count_boost_per_hit=cfg_float(config, "reimbursement_features.mention_count_boost_per_hit", 0.5),
        mention_count_boost_cap=cfg_float(config, "reimbursement_features.mention_count_boost_cap", 8.0),
        low_confidence_hard_flag=cfg_bool(config, "reimbursement_features.low_confidence_hard_flag", False),
        use_fallback_policy_scan_when_unmapped=cfg_bool(
            config,
            "reimbursement_features.use_fallback_policy_scan_when_unmapped",
            True,
        ),
    )


def latest_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM feature_financial_valuation").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    return asof or datetime.now(timezone.utc).date().isoformat()


def load_companies(conn: Any, *, ticker_filter: set[str], max_tickers: int) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name
        FROM dim_company
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    out: list[Company] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(Company(int(row["company_id"]), ticker, str(row["company_name"] or "")))
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def source_row_counts(conn: Any, source_ids: list[str]) -> tuple[int, int, int]:
    if not source_ids:
        return 0, 0, 0
    placeholders = ",".join("?" for _ in source_ids)
    policies = conn.execute(
        f"SELECT COUNT(*) AS n FROM fact_reimbursement_policy WHERE source_id IN ({placeholders})",
        source_ids,
    ).fetchone()
    codes = conn.execute(
        f"SELECT COUNT(*) AS n FROM dim_reimbursement_code WHERE source_id IN ({placeholders})",
        source_ids,
    ).fetchone()
    rates = conn.execute(
        f"SELECT COUNT(*) AS n FROM fact_reimbursement_rate WHERE source_id IN ({placeholders})",
        source_ids,
    ).fetchone()
    return int(policies["n"] or 0), int(codes["n"] or 0), int(rates["n"] or 0)


def load_policy_search_rows(conn: Any, source_ids: list[str]) -> list[PolicySearchRow]:
    if not source_ids:
        return []
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""
        SELECT policy_id, policy_type, title, related_codes, payload_json
        FROM fact_reimbursement_policy
        WHERE source_id IN ({placeholders})
        """,
        source_ids,
    ).fetchall()
    return [
        PolicySearchRow(
            policy_id=str(row["policy_id"] or ""),
            policy_type=str(row["policy_type"] or ""),
            title=str(row["title"] or ""),
            related_codes=str(row["related_codes"] or ""),
            payload_json=str(row["payload_json"] or ""),
        )
        for row in rows
    ]


def company_terms(company: Company) -> list[str]:
    norm = normalize_org_name(company.company_name)
    terms = [norm]
    ticker = normalize_ticker(company.ticker)
    if ticker:
        terms.append(ticker)
    stripped = re.sub(
        r"\b(INC|INCORPORATED|CORP|CORPORATION|PLC|LTD|LIMITED|LLC|NV|SA|AG|HOLDINGS|HOLDING|GROUP)\b",
        "",
        norm,
    )
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped not in terms:
        terms.append(stripped)
    return [term for term in terms if len(term) >= 3]


def mapped_product_codes(conn: Any, company_id: int) -> set[str]:
    codes: set[str] = set()
    for table in ("fact_fda_approval", "fact_fda_recall", "fact_fda_adverse_event"):
        rows = conn.execute(
            f"""
            SELECT DISTINCT product_code
            FROM {table}
            WHERE company_id = ?
              AND COALESCE(product_code, '') != ''
            """,
            (company_id,),
        ).fetchall()
        codes.update(str(row["product_code"] or "").strip() for row in rows if str(row["product_code"] or "").strip())
    return codes


CODE_TOKEN_RE = re.compile(r"\b(?:[A-Z]\d{4}|\d{5})\b")


def parse_related_codes(raw: object) -> set[str]:
    text = str(raw or "").upper()
    return {match.group(0) for match in CODE_TOKEN_RE.finditer(text)}


def policy_evidence(policy_rows: list[PolicySearchRow], company: Company, product_codes: set[str]) -> tuple[int, int, set[str], list[str]]:
    if not policy_rows:
        return 0, 0, set(), []
    terms = [term.lower() for term in company_terms(company)]
    product_terms = [code.lower() for code in product_codes]
    mention_count = 0
    evidence_count = 0
    matched_codes: set[str] = set()
    matched_policy_ids: list[str] = []
    for row in policy_rows:
        haystack = " ".join([row.title, row.related_codes, row.payload_json]).lower()
        company_hit = any(term and term in haystack for term in terms)
        product_hit = any(term and term in haystack for term in product_terms)
        if company_hit:
            mention_count += 1
        if product_hit:
            evidence_count += 1
            matched_codes.update(parse_related_codes(row.related_codes))
            matched_policy_ids.append(row.policy_id or f"{row.policy_type}:{row.title}"[:120])
    return evidence_count, mention_count, matched_codes, matched_policy_ids


def rate_count_for_codes(conn: Any, source_ids: list[str], codes: set[str]) -> int:
    if not source_ids or not codes:
        return 0
    source_placeholders = ",".join("?" for _ in source_ids)
    code_placeholders = ",".join("?" for _ in codes)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM fact_reimbursement_rate r
        JOIN dim_reimbursement_code c
          ON c.reimbursement_code_id = r.reimbursement_code_id
        WHERE r.source_id IN ({source_placeholders})
          AND c.code IN ({code_placeholders})
        """,
        [*source_ids, *sorted(codes)],
    ).fetchone()
    return int(row["n"] or 0) if row is not None else 0


def load_mapped_reimbursement_evidence(conn: Any, source_ids: list[str]) -> dict[int, CompanyReimbursementEvidence]:
    if not source_ids:
        return {}
    source_placeholders = ",".join("?" for _ in source_ids)
    policy_rows = conn.execute(
        f"""
        SELECT m.company_id, p.policy_id, p.policy_type, p.title, m.mapping_method
        FROM map_company_reimbursement_policy m
        JOIN fact_reimbursement_policy p
          ON p.reimbursement_policy_id = m.reimbursement_policy_id
        WHERE m.source_id IN ({source_placeholders})
        """,
        source_ids,
    ).fetchall()
    code_rows = conn.execute(
        f"""
        SELECT m.company_id, m.reimbursement_code_id, c.code
        FROM map_company_reimbursement_code m
        JOIN dim_reimbursement_code c
          ON c.reimbursement_code_id = m.reimbursement_code_id
        WHERE m.source_id IN ({source_placeholders})
        """,
        source_ids,
    ).fetchall()
    code_ids = sorted({int(row["reimbursement_code_id"]) for row in code_rows})
    rate_counts: dict[int, int] = {}
    if code_ids:
        code_placeholders = ",".join("?" for _ in code_ids)
        rate_rows = conn.execute(
            f"""
            SELECT reimbursement_code_id, COUNT(*) AS n
            FROM fact_reimbursement_rate
            WHERE source_id IN ({source_placeholders})
              AND reimbursement_code_id IN ({code_placeholders})
            GROUP BY reimbursement_code_id
            """,
            [*source_ids, *code_ids],
        ).fetchall()
        rate_counts = {int(row["reimbursement_code_id"]): int(row["n"] or 0) for row in rate_rows}
    by_company: dict[int, dict[str, Any]] = {}
    for row in policy_rows:
        company_id = int(row["company_id"])
        item = by_company.setdefault(
            company_id,
            {
                "policy_ids": set(),
                "mention_count": 0,
                "code_ids": set(),
                "codes": set(),
            },
        )
        policy_id = str(row["policy_id"] or f"{row['policy_type']}:{row['title']}"[:120])
        item["policy_ids"].add(policy_id)
        item["mention_count"] += 1
    for row in code_rows:
        company_id = int(row["company_id"])
        item = by_company.setdefault(
            company_id,
            {
                "policy_ids": set(),
                "mention_count": 0,
                "code_ids": set(),
                "codes": set(),
            },
        )
        code_id = int(row["reimbursement_code_id"])
        item["code_ids"].add(code_id)
        item["codes"].add(str(row["code"] or ""))
    out: dict[int, CompanyReimbursementEvidence] = {}
    for company_id, item in by_company.items():
        code_id_set = {int(value) for value in item["code_ids"]}
        out[company_id] = CompanyReimbursementEvidence(
            policy_evidence_count=len(item["policy_ids"]),
            company_mention_count=int(item["mention_count"]),
            reimbursement_code_count=len(item["codes"]),
            rate_row_count=sum(rate_counts.get(code_id, 0) for code_id in code_id_set),
            matched_codes={str(value) for value in item["codes"] if str(value)},
            matched_policy_ids=sorted(str(value) for value in item["policy_ids"])[:50],
        )
    return out


def blended_score(coverage_score: float, payment_score: float, *, policy: ReimbursementPolicy) -> float:
    total = max(1e-12, policy.coverage_weight + policy.payment_weight)
    return round((coverage_score * policy.coverage_weight + payment_score * policy.payment_weight) / total, 2)


def build_rows(conn: Any, companies: list[Company], *, asof: str, policy: ReimbursementPolicy) -> list[ReimbursementFeatureRow]:
    policy_count, global_code_count, global_rate_count = source_row_counts(conn, policy.source_ids)
    policy_rows = load_policy_search_rows(conn, policy.source_ids)
    mapped_evidence = load_mapped_reimbursement_evidence(conn, policy.source_ids)
    use_mapped_evidence = bool(mapped_evidence)
    rows: list[ReimbursementFeatureRow] = []
    for company in companies:
        product_codes = mapped_product_codes(conn, company.company_id)
        if company.company_id in mapped_evidence:
            evidence = mapped_evidence.get(
                company.company_id,
                CompanyReimbursementEvidence(0, 0, 0, 0, set(), []),
            )
            evidence_count = evidence.policy_evidence_count
            mention_count = evidence.company_mention_count
            matched_codes = evidence.matched_codes
            matched_policy_ids = evidence.matched_policy_ids
            matched_rate_count = evidence.rate_row_count
        elif not use_mapped_evidence or policy.use_fallback_policy_scan_when_unmapped:
            evidence_count, mention_count, matched_codes, matched_policy_ids = policy_evidence(policy_rows, company, product_codes)
            matched_rate_count = rate_count_for_codes(conn, policy.source_ids, matched_codes)
        else:
            evidence_count = 0
            mention_count = 0
            matched_codes = set()
            matched_policy_ids = []
            matched_rate_count = 0
        row = ReimbursementFeatureRow(
            asof_date=asof,
            company_id=company.company_id,
            ticker=company.ticker,
            company_name=company.company_name,
            policy_evidence_count=evidence_count,
            company_mention_count=mention_count,
            mapped_product_code_count=len(product_codes),
            reimbursement_code_count=len(matched_codes),
            rate_row_count=matched_rate_count,
        )
        reasons: list[str] = []
        if policy_count == 0 and global_code_count == 0 and global_rate_count == 0:
            row.coverage_clarity_score = policy.no_data_coverage_clarity_score
            row.payment_adequacy_score = policy.no_data_payment_adequacy_score
            row.score = policy.no_data_score
            row.review_reason = "cms_reimbursement_data_not_loaded"
        elif evidence_count > 0 and matched_codes:
            row.coverage_clarity_score = policy.policy_evidence_score
            row.payment_adequacy_score = policy.rate_evidence_score if matched_rate_count > 0 else policy.no_data_payment_adequacy_score
            row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)
        elif evidence_count > 0 or mention_count > 0:
            mention_boost = min(policy.mention_count_boost_cap, mention_count * policy.mention_count_boost_per_hit)
            row.coverage_clarity_score = min(policy.policy_evidence_score, policy.company_mention_score + mention_boost)
            row.payment_adequacy_score = policy.no_data_payment_adequacy_score
            row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)
            row.review_reason = "company_mentioned_without_product_code_mapping"
        elif matched_codes:
            row.coverage_clarity_score = policy.company_mention_score
            row.payment_adequacy_score = policy.rate_evidence_score if matched_rate_count > 0 else policy.no_data_payment_adequacy_score
            row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)
            if matched_rate_count <= 0:
                row.review_reason = "code_mapping_without_payment_rate"
        else:
            row.coverage_clarity_score = policy.no_data_coverage_clarity_score
            row.payment_adequacy_score = policy.no_data_payment_adequacy_score
            row.score = policy.no_data_score
            row.review_reason = "no_company_reimbursement_mapping"
        if row.review_reason:
            reasons.append(row.review_reason)
        row.hard_red_flag = 1 if policy.low_confidence_hard_flag and reasons else 0
        row.hard_red_flag_reasons = reasons if row.hard_red_flag else []
        row.payload = {
            "source": "reimbursement_feature_baseline",
            "source_ids": policy.source_ids,
            "mapping_source": "map_company_reimbursement" if use_mapped_evidence else "fallback_policy_scan",
            "source_row_counts": {
                "fact_reimbursement_policy": policy_count,
                "global_dim_reimbursement_code": global_code_count,
                "global_fact_reimbursement_rate": global_rate_count,
            },
            "mapped_product_codes": sorted(product_codes),
            "matched_reimbursement_codes": sorted(matched_codes),
            "matched_policy_ids": matched_policy_ids[:50],
            "evidence": {
                "policy_evidence_count": evidence_count,
                "company_mention_count": mention_count,
                "matched_rate_row_count": matched_rate_count,
            },
            "review_reason": row.review_reason,
            "score_weights": {
                "coverage": policy.coverage_weight,
                "payment": policy.payment_weight,
                "mention_count_boost_per_hit": policy.mention_count_boost_per_hit,
                "mention_count_boost_cap": policy.mention_count_boost_cap,
            },
        }
        rows.append(row)
    return rows


def upsert_feature_rows(conn: Any, rows: list[ReimbursementFeatureRow]) -> int:
    if not rows:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO feature_reimbursement(
            asof_date, company_id, ticker, company_name, score,
            coverage_clarity_score, payment_adequacy_score, policy_evidence_count,
            company_mention_count, mapped_product_code_count, reimbursement_code_count,
            rate_row_count, hard_red_flag, hard_red_flag_reasons, review_reason,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            ticker = excluded.ticker,
            company_name = excluded.company_name,
            score = excluded.score,
            coverage_clarity_score = excluded.coverage_clarity_score,
            payment_adequacy_score = excluded.payment_adequacy_score,
            policy_evidence_count = excluded.policy_evidence_count,
            company_mention_count = excluded.company_mention_count,
            mapped_product_code_count = excluded.mapped_product_code_count,
            reimbursement_code_count = excluded.reimbursement_code_count,
            rate_row_count = excluded.rate_row_count,
            hard_red_flag = excluded.hard_red_flag,
            hard_red_flag_reasons = excluded.hard_red_flag_reasons,
            review_reason = excluded.review_reason,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row.asof_date,
                row.company_id,
                row.ticker,
                row.company_name,
                row.score,
                row.coverage_clarity_score,
                row.payment_adequacy_score,
                row.policy_evidence_count,
                row.company_mention_count,
                row.mapped_product_code_count,
                row.reimbursement_code_count,
                row.rate_row_count,
                row.hard_red_flag,
                ";".join(row.hard_red_flag_reasons or []),
                row.review_reason,
                json.dumps(row.payload or {}, ensure_ascii=True, sort_keys=True),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def replace_data_quality_issues(conn: Any, rows: list[ReimbursementFeatureRow], *, asof: str) -> int:
    conn.execute(
        "DELETE FROM data_quality_issues WHERE asof_date = ? AND table_name = ?",
        (asof, "feature_reimbursement"),
    )
    issue_rows: list[tuple[Any, ...]] = []
    now = utc_now()
    for row in rows:
        if not row.review_reason:
            continue
        issue_rows.append(
            (
                asof,
                row.company_id,
                None,
                "feature_reimbursement",
                "score",
                row.review_reason,
                "warning",
                f"{row.ticker}: {row.review_reason}",
                now,
            )
        )
    if issue_rows:
        conn.executemany(
            """
            INSERT INTO data_quality_issues(
                asof_date, company_id, source_id, table_name, field_name, issue_type,
                severity, message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            issue_rows,
        )
    return len(issue_rows)


def row_to_dict(row: ReimbursementFeatureRow) -> dict[str, Any]:
    out = {field: getattr(row, field) for field in FIELDNAMES if hasattr(row, field)}
    out["hard_red_flag_reasons"] = ";".join(row.hard_red_flag_reasons or [])
    return out


def write_csv(path: Path, rows: list[ReimbursementFeatureRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_to_dict(row) for row in rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "reimbursement_features.output_csv", "../output/med_devices_reports/med_device_reimbursement_features.csv"),
            base_dir=base_dir,
        )
    )
    policy = reimbursement_policy(config)
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        parsed_asof = parse_date(args.asof) if args.asof else parse_date(latest_asof(conn))
        if parsed_asof is None:
            raise ValueError(f"Invalid as-of date: {args.asof}")
        asof = parsed_asof.isoformat()
        companies = load_companies(conn, ticker_filter=ticker_filter, max_tickers=int(args.max_tickers))
        if not companies:
            raise ValueError("No active companies selected")
        run_id = start_run(conn, run_type="build_med_device_reimbursement_features", input_path=config_path)
        try:
            rows = build_rows(conn, companies, asof=asof, policy=policy)
            upserted = upsert_feature_rows(conn, rows)
            issue_count = replace_data_quality_issues(conn, rows, asof=asof)
            write_csv(output_csv, rows)
            flagged = sum(1 for row in rows if row.hard_red_flag)
            message = f"asof={asof} rows={upserted} flagged={flagged} issues={issue_count} output={output_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=upserted, message=message)
            LOGGER.info("Reimbursement features complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
