from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class TickerScopedConceptRule:
    ticker: str
    taxonomy: str
    concept_name: str
    canonical_metric: str
    financial_statement: str
    period_type: str
    sign_policy: str
    priority: int
    valid_from: str


def load_ticker_scoped_concept_rules(path: Path) -> tuple[TickerScopedConceptRule, ...]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rules: list[TickerScopedConceptRule] = []
    seen: set[tuple[str, str, str, str]] = set()
    for line_number, row in enumerate(rows, start=2):
        if str(row.get("review_status") or "").strip().lower() != "reviewed":
            continue
        rule = TickerScopedConceptRule(
            ticker=str(row.get("ticker") or "").strip().upper(),
            taxonomy=str(row.get("taxonomy") or "").strip(),
            concept_name=str(row.get("concept_name") or "").strip(),
            canonical_metric=str(row.get("canonical_metric") or "").strip(),
            financial_statement=str(row.get("financial_statement") or "").strip(),
            period_type=str(row.get("period_type") or "").strip(),
            sign_policy=str(row.get("sign_policy") or "as_reported").strip(),
            priority=int(str(row.get("priority") or "100")),
            valid_from=date.fromisoformat(str(row.get("valid_from") or "")[:10]).isoformat(),
        )
        if not all(
            (
                rule.ticker,
                rule.taxonomy,
                rule.concept_name,
                rule.canonical_metric,
                rule.financial_statement,
                rule.period_type,
            )
        ):
            raise ValueError(f"{path}:{line_number}: incomplete reviewed rule")
        if rule.sign_policy not in {
            "as_reported",
            "positive_abs",
            "abs",
            "negative_abs",
            "expense_from_net",
        }:
            raise ValueError(f"{path}:{line_number}: unsupported sign policy")
        key = (rule.ticker, rule.taxonomy, rule.concept_name, rule.canonical_metric)
        if key in seen:
            raise ValueError(f"{path}:{line_number}: duplicate rule={key}")
        seen.add(key)
        rules.append(rule)
    if not rules:
        raise ValueError(f"{path}: no reviewed ticker-scoped rules")
    return tuple(rules)


def materialize_ticker_scoped_xbrl_facts(
    connection: sqlite3.Connection,
    *,
    rules: tuple[TickerScopedConceptRule, ...],
    asof: date,
    execute: bool,
) -> dict[str, int]:
    eligible = 0
    existing = 0
    inserted = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for rule in rules:
        if rule.valid_from > asof.isoformat():
            continue
        params = (
            rule.ticker,
            rule.taxonomy,
            rule.concept_name,
            asof.isoformat(),
            asof.isoformat(),
            rule.canonical_metric,
        )
        counts = connection.execute(
            """
            SELECT COUNT(*) AS eligible_count,
                   SUM(CASE WHEN f.fact_id IS NOT NULL THEN 1 ELSE 0 END) AS existing_count
            FROM fact_sec_xbrl_fact_raw AS r
            LEFT JOIN fact_sec_xbrl_fact AS f
              ON f.raw_fact_id=r.raw_fact_id
             AND f.canonical_metric=?6
            WHERE r.ticker=?1 AND r.taxonomy=?2 AND r.concept_name=?3
              AND r.source_detail='sec_companyfacts'
              AND r.raw_value IS NOT NULL AND r.period_end IS NOT NULL
              AND r.period_end<=?4
              AND COALESCE(NULLIF(SUBSTR(r.accepted_at,1,10),''),r.filing_date,r.period_end)<=?5
            """,
            params,
        ).fetchone()
        eligible += int(counts["eligible_count"] or 0)
        existing += int(counts["existing_count"] or 0)
        if not execute:
            continue
        before = connection.total_changes
        connection.execute(
            """
            INSERT INTO fact_sec_xbrl_fact(
                raw_fact_id,ticker,cik,source_id,accession_number,form_type,
                filing_date,accepted_at,fiscal_year,fiscal_period,period_start,
                period_end,frame,taxonomy,concept_name,canonical_metric,
                financial_statement,period_type,unit,value,sign_policy,
                source_priority,source_detail,created_at,updated_at
            )
            SELECT r.raw_fact_id,r.ticker,r.cik,r.source_id,r.accession_number,
                   r.form_type,r.filing_date,r.accepted_at,r.fiscal_year,
                   r.fiscal_period,r.period_start,r.period_end,r.frame,r.taxonomy,
                   r.concept_name,?6,?7,?8,r.unit,
                   CASE ?9
                       WHEN 'positive_abs' THEN ABS(r.raw_value)
                       WHEN 'abs' THEN ABS(r.raw_value)
                       WHEN 'negative_abs' THEN -ABS(r.raw_value)
                       WHEN 'expense_from_net' THEN MAX(-r.raw_value,0.0)
                       ELSE r.raw_value
                   END,
                   ?9,?10,
                   r.source_detail || '_ticker_scoped_reviewed',?11,?11
            FROM fact_sec_xbrl_fact_raw AS r
            WHERE r.ticker=?1 AND r.taxonomy=?2 AND r.concept_name=?3
              AND r.source_detail='sec_companyfacts'
              AND r.raw_value IS NOT NULL AND r.period_end IS NOT NULL
              AND r.period_end<=?4
              AND COALESCE(NULLIF(SUBSTR(r.accepted_at,1,10),''),r.filing_date,r.period_end)<=?5
            ON CONFLICT(
                ticker,source_id,accession_number,taxonomy,concept_name,
                canonical_metric,unit,period_start,period_end,frame
            ) DO UPDATE SET
                financial_statement=excluded.financial_statement,
                period_type=excluded.period_type,
                value=excluded.value,
                sign_policy=excluded.sign_policy,
                source_priority=excluded.source_priority,
                source_detail=excluded.source_detail,
                updated_at=excluded.updated_at
            """,
            (
                rule.ticker,
                rule.taxonomy,
                rule.concept_name,
                asof.isoformat(),
                asof.isoformat(),
                rule.canonical_metric,
                rule.financial_statement,
                rule.period_type,
                rule.sign_policy,
                rule.priority,
                now,
            ),
        )
        inserted += max(0, connection.total_changes - before)
    if execute:
        connection.commit()
    return {
        "rule_count": len(rules),
        "eligible_raw_fact_count": eligible,
        "existing_mapped_fact_count": existing,
        "database_change_count": inserted,
    }
