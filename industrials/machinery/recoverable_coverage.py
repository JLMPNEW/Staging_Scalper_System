from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from industrials.core.db import utc_now


MISSING_STATUSES = frozenset({"DISCLOSED_UNPARSED", "NOT_DISCLOSED", "PARSER_FAILURE"})
COVERED_STATUSES = frozenset({"REPORTED", "PROXY"})
RECOVERABILITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NONE": 3}

DISCLOSURE_SOURCE_METRICS = {
    "orders": "orders",
    "orders_yoy_growth": "orders",
    "book_to_bill": "orders",
    "funded_backlog": "funded_backlog",
    "backlog_yoy_growth": "funded_backlog",
    "backlog_to_revenue": "funded_backlog",
    "reported_backlog": "reported_backlog",
    "reported_backlog_yoy_growth": "reported_backlog",
    "reported_backlog_to_revenue": "reported_backlog",
    "remaining_performance_obligation": "remaining_performance_obligation",
    "rpo_current": "remaining_performance_obligation",
    "rpo_yoy_growth": "remaining_performance_obligation",
    "rpo_to_revenue": "remaining_performance_obligation",
    "rpo_implied_orders": "remaining_performance_obligation",
    "rpo_implied_book_to_bill": "remaining_performance_obligation",
    "contract_load_proxy": "contract_load",
    "contract_load_proxy_yoy_growth": "contract_load",
    "contract_load_proxy_to_revenue": "contract_load",
}

SOURCE_CONCEPT_PATTERNS = {
    "orders": re.compile(r"(?:order|booking)", re.IGNORECASE),
    "funded_backlog": re.compile(r"(?:funded|authorized).*backlog|backlog.*(?:funded|authorized)", re.IGNORECASE),
    "reported_backlog": re.compile(r"backlog", re.IGNORECASE),
    "remaining_performance_obligation": re.compile(
        r"remaining.*performance.*obligation|performance.*obligation.*remaining",
        re.IGNORECASE,
    ),
    "contract_load": re.compile(r"backlog|order|booking|performance.*obligation", re.IGNORECASE),
}

REGISTRATION_FORMS = frozenset(
    {
        "10-12B",
        "10-12B/A",
        "10-12G",
        "10-12G/A",
        "424B3",
        "424B4",
        "F-1",
        "F-1/A",
        "F-4",
        "F-4/A",
        "S-1",
        "S-1/A",
        "S-4",
        "S-4/A",
    }
)

LEDGER_FIELDS = [
    "ticker",
    "asof_date",
    "metric_name",
    "availability_status",
    "status_reason",
    "source_metric",
    "evidence_class",
    "recoverability",
    "source_lane",
    "missing_operands",
    "raw_matching_fact_count",
    "unmapped_matching_fact_count",
    "mapped_matching_fact_count",
    "candidate_count",
    "current_candidate_count",
    "accepted_candidate_count",
    "review_candidate_count",
    "rejected_candidate_count",
    "filing_count",
    "registration_filing_count",
    "membership_start_date",
    "latest_accession_number",
    "latest_form_type",
    "latest_filing_date",
    "latest_document_name",
    "latest_candidate_status",
    "latest_confidence",
    "evidence_text",
    "provenance_json",
]

ISSUER_IR_REQUEST_FIELDS = [
    "ticker",
    "asof_date",
    "source_metrics",
    "blocked_metrics",
    "missing_cell_count",
    "preferred_document_types",
    "search_terms",
    "source_policy",
    "manifest_status",
    "notes",
]
ISSUER_IR_SEARCH_TERMS = {
    "contract_load": "backlog|order book|orders|bookings|remaining performance obligations",
    "funded_backlog": "funded backlog|authorized backlog|appropriated backlog",
    "orders": "orders|bookings|order intake|book-to-bill",
    "remaining_performance_obligation": (
        "remaining performance obligations|RPO|unsatisfied performance obligations"
    ),
    "reported_backlog": "backlog|order book",
}
ISSUER_IR_DOCUMENT_TYPES = (
    "EARNINGS_RELEASE",
    "INVESTOR_PRESENTATION",
    "SUPPLEMENTAL_KPI_REPORT",
)


LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_machinery_metric_recovery_evidence (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    availability_status TEXT NOT NULL,
    status_reason TEXT,
    source_metric TEXT,
    evidence_class TEXT NOT NULL,
    recoverability TEXT NOT NULL,
    source_lane TEXT NOT NULL,
    missing_operands TEXT NOT NULL,
    raw_matching_fact_count INTEGER NOT NULL DEFAULT 0,
    unmapped_matching_fact_count INTEGER NOT NULL DEFAULT 0,
    mapped_matching_fact_count INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    current_candidate_count INTEGER NOT NULL DEFAULT 0,
    accepted_candidate_count INTEGER NOT NULL DEFAULT 0,
    review_candidate_count INTEGER NOT NULL DEFAULT 0,
    rejected_candidate_count INTEGER NOT NULL DEFAULT 0,
    filing_count INTEGER NOT NULL DEFAULT 0,
    registration_filing_count INTEGER NOT NULL DEFAULT 0,
    membership_start_date TEXT,
    latest_accession_number TEXT,
    latest_form_type TEXT,
    latest_filing_date TEXT,
    latest_document_name TEXT,
    latest_candidate_status TEXT,
    latest_confidence REAL,
    evidence_text TEXT,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_machinery_metric_recovery_priority
ON fact_machinery_metric_recovery_evidence(asof_date, recoverability, evidence_class, ticker);
"""


@dataclass(frozen=True)
class RecoveryClassification:
    evidence_class: str
    recoverability: str
    source_lane: str


def ensure_recovery_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(LEDGER_SCHEMA)
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(fact_machinery_metric_recovery_evidence)"
        ).fetchall()
    }
    migrations = {
        "current_candidate_count": "INTEGER NOT NULL DEFAULT 0",
        "unmapped_matching_fact_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, declaration in migrations.items():
        if column not in columns:
            conn.execute(
                f"ALTER TABLE fact_machinery_metric_recovery_evidence "
                f"ADD COLUMN {column} {declaration}"
            )


def parse_missing_operands(status_reason: str | None) -> tuple[str, ...]:
    prefix = "insufficient_comparable_history_or_missing_operands:"
    reason = str(status_reason or "")
    if not reason.startswith(prefix):
        return ()
    return tuple(item.strip() for item in reason[len(prefix) :].split(",") if item.strip())


def source_metric_for(metric_name: str) -> str:
    return DISCLOSURE_SOURCE_METRICS.get(metric_name, "")


def is_recent_public(*, membership_start: str, asof: str) -> bool:
    try:
        start_date = date.fromisoformat(membership_start)
        asof_date = date.fromisoformat(asof)
    except ValueError:
        return False
    return timedelta(0) <= asof_date - start_date <= timedelta(days=730)


def parse_json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {"unparsed_value": str(value or "")}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def is_current_candidate(candidate: dict[str, Any], *, anchor_period: str, asof: str) -> bool:
    try:
        anchor = date.fromisoformat(anchor_period)
    except ValueError:
        anchor = None
    try:
        period_end = date.fromisoformat(str(candidate.get("period_end") or ""))
    except ValueError:
        period_end = None
    if anchor is not None and period_end is not None:
        return period_end >= anchor - timedelta(days=183)
    try:
        filing_date = date.fromisoformat(str(candidate.get("filing_date") or "")[:10])
        asof_date = date.fromisoformat(asof)
    except ValueError:
        return False
    return timedelta(0) <= asof_date - filing_date <= timedelta(days=548)


def classify_recovery(
    *,
    metric_name: str,
    availability_status: str,
    missing_operands: tuple[str, ...],
    source_metric: str,
    source_status: str,
    accepted_candidate_count: int,
    review_candidate_count: int,
    rejected_candidate_count: int,
    unmapped_matching_fact_count: int,
    registration_filing_count: int,
    recent_public: bool,
) -> RecoveryClassification:
    if availability_status == "PARSER_FAILURE":
        return RecoveryClassification("SOURCE_RETRIEVAL_FAILURE", "HIGH", "SEC_FILING_ARCHIVE")
    if availability_status == "DISCLOSED_UNPARSED":
        return RecoveryClassification("DISCLOSED_REQUIRES_REVIEW", "HIGH", "SEC_FILING_TEXT")
    if metric_name == source_metric and review_candidate_count:
        return RecoveryClassification("DISCLOSED_REQUIRES_REVIEW", "HIGH", "SEC_FILING_TEXT")
    if metric_name == source_metric and accepted_candidate_count:
        return RecoveryClassification("ACCEPTED_FACT_NOT_PROJECTED", "HIGH", "PIPELINE_PROJECTION")
    if unmapped_matching_fact_count:
        return RecoveryClassification("UNMAPPED_XBRL_CONCEPT", "HIGH", "CUSTOM_XBRL")

    prior_operands = tuple(operand for operand in missing_operands if operand.startswith("prior_"))
    if prior_operands and source_status in COVERED_STATUSES:
        lane = "REGISTRATION_STATEMENT" if registration_filing_count else "SEC_FILING_ARCHIVE"
        return RecoveryClassification("INSUFFICIENT_COMPARABLE_HISTORY", "MEDIUM", lane)
    if missing_operands and source_status in COVERED_STATUSES:
        return RecoveryClassification("DERIVATION_ALIGNMENT_GAP", "MEDIUM", "PIPELINE_ALIGNMENT")
    if review_candidate_count:
        return RecoveryClassification("DISCLOSED_REQUIRES_REVIEW", "HIGH", "SEC_FILING_TEXT")
    if accepted_candidate_count and source_status not in COVERED_STATUSES:
        return RecoveryClassification("ACCEPTED_FACT_NOT_PROJECTED", "HIGH", "PIPELINE_PROJECTION")
    if rejected_candidate_count:
        return RecoveryClassification("DISCLOSURE_REJECTED_BY_POLICY", "MEDIUM", "SEC_FILING_TEXT")
    if recent_public and registration_filing_count and not source_metric:
        return RecoveryClassification(
            "REGISTRATION_HISTORY_RECOVERABLE",
            "MEDIUM",
            "REGISTRATION_STATEMENT",
        )
    if source_metric:
        if metric_name == "rpo_current":
            return RecoveryClassification(
                "CURRENT_RPO_TEXT_DISAGGREGATION_NEEDED",
                "MEDIUM",
                "SEC_FILING_TEXT",
            )
        if source_metric in {"orders", "funded_backlog", "reported_backlog", "contract_load"}:
            return RecoveryClassification(
                "NO_QUALIFYING_SEC_DISCLOSURE_FOUND",
                "MEDIUM",
                "ISSUER_IR",
            )
        return RecoveryClassification(
            "NO_QUALIFYING_SEC_DISCLOSURE_FOUND",
            "LOW",
            "ISSUER_IR",
        )
    if missing_operands:
        return RecoveryClassification("DERIVATION_OPERAND_GAP", "MEDIUM", "STANDARD_XBRL")
    return RecoveryClassification("NO_RECOVERY_EVIDENCE", "LOW", "NONE")


def _date_known_expression(alias: str) -> str:
    return f"""
        CASE
            WHEN COALESCE({alias}.accepted_at, '') GLOB '????-??-??*'
                THEN SUBSTR({alias}.accepted_at, 1, 10)
            WHEN COALESCE({alias}.accepted_at, '') GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'
                THEN SUBSTR({alias}.accepted_at, 1, 4) || '-' || SUBSTR({alias}.accepted_at, 5, 2) || '-' || SUBSTR({alias}.accepted_at, 7, 2)
            ELSE COALESCE(NULLIF({alias}.filing_date, ''), '9999-12-31')
        END
    """


def _grouped_rows(rows: Iterable[sqlite3.Row], key: str) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        output.setdefault(str(item[key]), []).append(item)
    return output


def build_recovery_evidence(
    conn: sqlite3.Connection,
    *,
    asof: str,
) -> list[dict[str, Any]]:
    missing_rows = conn.execute(
        """
            SELECT a.ticker, a.metric_name, a.availability_status, a.status_reason,
                   a.period_end, a.provenance_json,
                   (
                       SELECT MAX(f.fiscal_period_end)
                       FROM feature_financial_statement f
                       WHERE f.ticker = a.ticker AND f.asof_date = a.asof_date
                         AND f.model_family = a.model_family
                   ) AS feature_fiscal_period_end
        FROM feature_financial_metric_availability a
        WHERE a.model_family = 'machinery'
          AND a.asof_date = ?
          AND a.availability_status IN ('DISCLOSED_UNPARSED', 'NOT_DISCLOSED', 'PARSER_FAILURE')
        ORDER BY ticker, metric_name
        """,
        (asof,),
    ).fetchall()
    if not missing_rows:
        return []
    tickers = sorted({str(row["ticker"]) for row in missing_rows})
    placeholders = ",".join("?" for _ in tickers)
    memberships = {
        str(row["ticker"]): str(row["start_date"] or "")
        for row in conn.execute(
            f"""
            SELECT ticker, MIN(start_date) AS start_date
            FROM dim_universe_membership
            WHERE model_family = 'machinery' AND ticker IN ({placeholders})
            GROUP BY ticker
            """,
            tickers,
        )
    }
    source_statuses = {
        (str(row["ticker"]), str(row["metric_name"])): str(row["availability_status"])
        for row in conn.execute(
            f"""
            SELECT ticker, metric_name, availability_status
            FROM feature_financial_metric_availability
            WHERE model_family = 'machinery' AND asof_date = ?
              AND ticker IN ({placeholders})
            """,
            (asof, *tickers),
        )
    }
    filing_rows = conn.execute(
        f"""
        SELECT ticker, COUNT(*) AS filing_count,
               SUM(CASE WHEN form_type IN ({','.join('?' for _ in REGISTRATION_FORMS)}) THEN 1 ELSE 0 END)
                   AS registration_filing_count
        FROM fact_sec_filing f
        WHERE ticker IN ({placeholders})
          AND {_date_known_expression('f')} <= ?
        GROUP BY ticker
        """,
        (*sorted(REGISTRATION_FORMS), *tickers, asof),
    ).fetchall()
    filings = {str(row["ticker"]): dict(row) for row in filing_rows}
    candidates = _grouped_rows(
        conn.execute(
            f"""
            SELECT ticker, metric_name, candidate_status, confidence, accession_number,
                   form_type, filing_date, period_end, document_name, evidence_text
            FROM fact_sec_metric_disclosure_candidate c
            WHERE model_family = 'machinery'
              AND ticker IN ({placeholders})
              AND {_date_known_expression('c')} <= ?
            ORDER BY ticker, metric_name,
                     candidate_status = 'ACCEPTED' DESC,
                     candidate_status = 'REVIEW_REQUIRED' DESC,
                     confidence DESC, filing_date DESC
            """,
            (*tickers, asof),
        ),
        "ticker",
    )
    raw_rows = conn.execute(
        f"""
        SELECT ticker, taxonomy, concept_name, COUNT(*) AS fact_count
        FROM fact_sec_xbrl_fact_raw r
        WHERE ticker IN ({placeholders})
          AND {_date_known_expression('r')} <= ?
        GROUP BY ticker, taxonomy, concept_name
        """,
        (*tickers, asof),
    ).fetchall()
    mapped_rows = conn.execute(
        f"""
        SELECT ticker, taxonomy, concept_name, COUNT(*) AS fact_count
        FROM fact_sec_xbrl_fact x
        WHERE ticker IN ({placeholders})
          AND {_date_known_expression('x')} <= ?
        GROUP BY ticker, taxonomy, concept_name
        """,
        (*tickers, asof),
    ).fetchall()
    raw_concepts = _grouped_rows(raw_rows, "ticker")
    mapped_concepts = _grouped_rows(mapped_rows, "ticker")

    output: list[dict[str, Any]] = []
    for row in missing_rows:
        ticker = str(row["ticker"])
        metric_name = str(row["metric_name"])
        status_reason = str(row["status_reason"] or "")
        availability_status = str(row["availability_status"])
        source_metric = source_metric_for(metric_name)
        candidate_source_metrics = (
            {"orders", "reported_backlog", "remaining_performance_obligation"}
            if source_metric == "contract_load"
            else {source_metric}
        )
        relevant_candidates = [
            item
            for item in candidates.get(ticker, [])
            if str(item["metric_name"]) in candidate_source_metrics
        ]
        anchor_period = str(row["feature_fiscal_period_end"] or row["period_end"] or "")
        current_candidates = [
            item
            for item in relevant_candidates
            if is_current_candidate(item, anchor_period=anchor_period, asof=asof)
        ]
        candidate_statuses = Counter(str(item["candidate_status"]) for item in relevant_candidates)
        current_statuses = Counter(str(item["candidate_status"]) for item in current_candidates)
        latest = (current_candidates or relevant_candidates or [{}])[0]
        pattern = SOURCE_CONCEPT_PATTERNS.get(source_metric)
        raw_matching = sum(
            int(item["fact_count"])
            for item in raw_concepts.get(ticker, [])
            if pattern and pattern.search(str(item["concept_name"]))
        )
        mapped_matching = sum(
            int(item["fact_count"])
            for item in mapped_concepts.get(ticker, [])
            if pattern and pattern.search(str(item["concept_name"]))
        )
        mapped_concept_keys = {
            (str(item["taxonomy"]).lower(), str(item["concept_name"]).lower())
            for item in mapped_concepts.get(ticker, [])
        }
        unmapped_matching = sum(
            int(item["fact_count"])
            for item in raw_concepts.get(ticker, [])
            if pattern
            and pattern.search(str(item["concept_name"]))
            and str(item["taxonomy"]).lower()
            not in {"dei", "ifrs-full", "sec-footnote", "sec-text", "us-gaap"}
            and (str(item["taxonomy"]).lower(), str(item["concept_name"]).lower())
            not in mapped_concept_keys
        )
        missing_operands = parse_missing_operands(status_reason)
        source_status_metric = (
            "contract_load_proxy" if source_metric == "contract_load" else source_metric
        )
        source_status = source_statuses.get((ticker, source_status_metric), "")
        registration_count = int(filings.get(ticker, {}).get("registration_filing_count") or 0)
        membership_start = memberships.get(ticker, "")
        recent_public = is_recent_public(membership_start=membership_start, asof=asof)
        classification = classify_recovery(
            metric_name=metric_name,
            availability_status=availability_status,
            missing_operands=missing_operands,
            source_metric=source_metric,
            source_status=source_status,
            accepted_candidate_count=current_statuses["ACCEPTED"],
            review_candidate_count=current_statuses["REVIEW_REQUIRED"],
            rejected_candidate_count=sum(
                count for status, count in current_statuses.items() if status.startswith("REJECTED")
            ),
            unmapped_matching_fact_count=unmapped_matching,
            registration_filing_count=registration_count,
            recent_public=recent_public,
        )
        evidence = str(latest.get("evidence_text") or "")
        provenance = {
            "availability_provenance": parse_json_object(row["provenance_json"]),
            "candidate_status_counts": dict(candidate_statuses),
            "current_candidate_status_counts": dict(current_statuses),
            "feature_fiscal_period_end": anchor_period,
            "raw_matching_fact_count": raw_matching,
            "unmapped_matching_fact_count": unmapped_matching,
            "mapped_matching_fact_count": mapped_matching,
            "classification_version": 1,
        }
        output.append(
            {
                "ticker": ticker,
                "asof_date": asof,
                "metric_name": metric_name,
                "availability_status": availability_status,
                "status_reason": status_reason,
                "source_metric": source_metric,
                "evidence_class": classification.evidence_class,
                "recoverability": classification.recoverability,
                "source_lane": classification.source_lane,
                "missing_operands": ",".join(missing_operands),
                "raw_matching_fact_count": raw_matching,
                "unmapped_matching_fact_count": unmapped_matching,
                "mapped_matching_fact_count": mapped_matching,
                "candidate_count": len(relevant_candidates),
                "current_candidate_count": len(current_candidates),
                "accepted_candidate_count": candidate_statuses["ACCEPTED"],
                "review_candidate_count": candidate_statuses["REVIEW_REQUIRED"],
                "rejected_candidate_count": sum(
                    count
                    for status, count in candidate_statuses.items()
                    if status.startswith("REJECTED")
                ),
                "filing_count": int(filings.get(ticker, {}).get("filing_count") or 0),
                "registration_filing_count": registration_count,
                "membership_start_date": membership_start,
                "latest_accession_number": latest.get("accession_number"),
                "latest_form_type": latest.get("form_type"),
                "latest_filing_date": latest.get("filing_date"),
                "latest_document_name": latest.get("document_name"),
                "latest_candidate_status": latest.get("candidate_status"),
                "latest_confidence": latest.get("confidence"),
                "evidence_text": evidence[:1000],
                "provenance_json": json.dumps(provenance, sort_keys=True, separators=(",", ":")),
            }
        )
    return sorted(
        output,
        key=lambda item: (
            RECOVERABILITY_ORDER[str(item["recoverability"])],
            str(item["evidence_class"]),
            str(item["ticker"]),
            str(item["metric_name"]),
        ),
    )


def build_issuer_ir_recovery_requests(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if str(row.get("source_lane") or "") != "ISSUER_IR":
            continue
        key = (
            str(row.get("ticker") or ""),
            str(row.get("asof_date") or ""),
        )
        grouped.setdefault(key, []).append(row)

    requests: list[dict[str, Any]] = []
    for (ticker, asof_date), ticker_rows in sorted(grouped.items()):
        source_metrics = sorted(
            {
                str(row.get("source_metric") or "")
                for row in ticker_rows
                if str(row.get("source_metric") or "")
            }
        )
        blocked_metrics = sorted(
            {str(row.get("metric_name") or "") for row in ticker_rows}
        )
        search_terms = sorted(
            {
                term
                for metric in source_metrics
                for term in ISSUER_IR_SEARCH_TERMS.get(metric, metric).split("|")
                if term
            }
        )
        requests.append(
            {
                "ticker": ticker,
                "asof_date": asof_date,
                "source_metrics": "|".join(source_metrics),
                "blocked_metrics": "|".join(blocked_metrics),
                "missing_cell_count": len(ticker_rows),
                "preferred_document_types": "|".join(ISSUER_IR_DOCUMENT_TYPES),
                "search_terms": "|".join(search_terms),
                "source_policy": "official_issuer_domain_only",
                "manifest_status": "RESEARCH_REQUIRED",
                "notes": "Add a reviewed dated document URL to machinery_issuer_ir_documents.csv.",
            }
        )
    return requests


def replace_recovery_evidence(
    conn: sqlite3.Connection,
    *,
    asof: str,
    rows: list[dict[str, Any]],
) -> None:
    ensure_recovery_schema(conn)
    conn.execute(
        "DELETE FROM fact_machinery_metric_recovery_evidence WHERE asof_date = ?",
        (asof,),
    )
    if not rows:
        return
    now = utc_now()
    columns = [*LEDGER_FIELDS, "created_at", "updated_at"]
    placeholders = ",".join("?" for _ in columns)
    conn.executemany(
        f"INSERT INTO fact_machinery_metric_recovery_evidence ({','.join(columns)}) VALUES ({placeholders})",
        [tuple(row.get(column, now if column in {"created_at", "updated_at"} else None) for column in columns) for row in rows],
    )


def recovery_summary(rows: list[dict[str, Any]], *, asof: str) -> dict[str, Any]:
    def counts(field: str) -> dict[str, int]:
        return dict(sorted(Counter(str(row[field]) for row in rows).items()))

    return {
        "asof_date": asof,
        "missing_cell_count": len(rows),
        "recoverability_counts": counts("recoverability"),
        "evidence_class_counts": counts("evidence_class"),
        "source_lane_counts": counts("source_lane"),
        "metric_counts": counts("metric_name"),
        "high_priority_tickers": sorted(
            {str(row["ticker"]) for row in rows if row["recoverability"] == "HIGH"}
        ),
    }
