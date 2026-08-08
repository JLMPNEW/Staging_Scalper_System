from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Sequence


def _placeholders(values: Sequence[object]) -> str:
    if not values:
        raise ValueError("SQL placeholder input cannot be empty")
    return ",".join("?" for _ in values)


def repair_transportation_mapped_xbrl_facts(
    connection: sqlite3.Connection,
    *,
    source_ids: Sequence[str],
    tickers: Sequence[str],
    asof: date,
) -> int:
    """Idempotently map transportation raw XBRL facts without key collisions."""

    sources = tuple(dict.fromkeys(str(value) for value in source_ids if str(value)))
    symbols = tuple(dict.fromkeys(str(value) for value in tickers if str(value)))
    if not sources or not symbols:
        return 0
    source_ph = _placeholders(sources)
    ticker_ph = _placeholders(symbols)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes_before = connection.total_changes
    with connection:
        # Keep already-mapped facts aligned with the reviewed concept map. This
        # is metadata/value normalization only; source identity and PIT dates
        # remain the raw fact's own values.
        connection.execute(
            f"""
            UPDATE fact_sec_xbrl_fact AS f
            SET financial_statement = m.financial_statement,
                period_type = m.period_type,
                value = CASE m.sign_policy
                    WHEN 'positive_abs' THEN ABS(r.raw_value)
                    WHEN 'abs' THEN ABS(r.raw_value)
                    WHEN 'negative_abs' THEN -ABS(r.raw_value)
                    WHEN 'expense_from_net' THEN MAX(-r.raw_value, 0.0)
                    ELSE r.raw_value
                END,
                sign_policy = m.sign_policy,
                source_priority = m.priority,
                updated_at = ?
            FROM fact_sec_xbrl_fact_raw AS r
            JOIN dim_xbrl_concept_map AS m
              ON m.taxonomy = r.taxonomy
             AND m.concept_name = r.concept_name
             AND m.active_flag = 1
            WHERE f.raw_fact_id = r.raw_fact_id
              AND f.canonical_metric = m.canonical_metric
              AND r.source_id IN ({source_ph})
              AND r.ticker IN ({ticker_ph})
              AND r.period_end IS NOT NULL
              AND r.period_end <= ?
              AND COALESCE(
                    NULLIF(SUBSTR(r.accepted_at, 1, 10), ''),
                    r.filing_date,
                    r.period_end
                  ) <= ?
              AND (
                    f.financial_statement IS NOT m.financial_statement
                 OR f.period_type IS NOT m.period_type
                 OR f.value IS NOT CASE m.sign_policy
                        WHEN 'positive_abs' THEN ABS(r.raw_value)
                        WHEN 'abs' THEN ABS(r.raw_value)
                        WHEN 'negative_abs' THEN -ABS(r.raw_value)
                        WHEN 'expense_from_net' THEN MAX(-r.raw_value, 0.0)
                        ELSE r.raw_value
                    END
                 OR f.sign_policy IS NOT m.sign_policy
                 OR f.source_priority IS NOT m.priority
              )
            """,
            (now, *sources, *symbols, asof.isoformat(), asof.isoformat()),
        )
        connection.execute(
            f"""
            WITH ranked_candidates AS (
                SELECT r.raw_fact_id, r.ticker, r.cik, r.source_id,
                       r.accession_number, r.form_type, r.filing_date,
                       r.accepted_at, r.fiscal_year, r.fiscal_period,
                       r.period_start, r.period_end, r.frame, r.taxonomy,
                       r.concept_name, m.canonical_metric,
                       m.financial_statement, m.period_type, r.unit,
                       CASE m.sign_policy
                           WHEN 'positive_abs' THEN ABS(r.raw_value)
                           WHEN 'abs' THEN ABS(r.raw_value)
                           WHEN 'negative_abs' THEN -ABS(r.raw_value)
                           WHEN 'expense_from_net' THEN MAX(-r.raw_value, 0.0)
                           ELSE r.raw_value
                       END AS mapped_value,
                       m.sign_policy, m.priority,
                       COALESCE(r.source_detail, 'loaded_raw') || '_mapped'
                           AS mapped_source_detail,
                       ROW_NUMBER() OVER (
                           PARTITION BY
                               r.ticker, r.source_id, r.accession_number,
                               r.taxonomy, r.concept_name,
                               m.canonical_metric, r.unit, r.period_start,
                               r.period_end, r.frame
                           ORDER BY r.raw_fact_id DESC
                       ) AS destination_rank
                FROM fact_sec_xbrl_fact_raw AS r
                JOIN dim_xbrl_concept_map AS m
                  ON m.taxonomy = r.taxonomy
                 AND m.concept_name = r.concept_name
                 AND m.active_flag = 1
                WHERE r.source_id IN ({source_ph})
                  AND r.ticker IN ({ticker_ph})
                  AND r.period_end IS NOT NULL
                  AND r.period_end <= ?
                  AND COALESCE(
                        NULLIF(SUBSTR(r.accepted_at, 1, 10), ''),
                        r.filing_date,
                        r.period_end
                      ) <= ?
                  AND NOT EXISTS (
                        SELECT 1
                        FROM fact_sec_xbrl_fact AS f
                        WHERE f.ticker = r.ticker
                          AND f.source_id = r.source_id
                          AND f.accession_number IS r.accession_number
                          AND f.taxonomy = r.taxonomy
                          AND f.concept_name = r.concept_name
                          AND f.canonical_metric = m.canonical_metric
                          AND f.unit IS r.unit
                          AND f.period_start IS r.period_start
                          AND f.period_end IS r.period_end
                          AND f.frame IS r.frame
                  )
            )
            INSERT INTO fact_sec_xbrl_fact(
                raw_fact_id, ticker, cik, source_id, accession_number,
                form_type, filing_date, accepted_at, fiscal_year, fiscal_period,
                period_start, period_end, frame, taxonomy, concept_name,
                canonical_metric, financial_statement, period_type, unit,
                value, sign_policy, source_priority, source_detail,
                created_at, updated_at
            )
            SELECT raw_fact_id, ticker, cik, source_id,
                   accession_number, form_type, filing_date, accepted_at,
                   fiscal_year, fiscal_period, period_start, period_end,
                   frame, taxonomy, concept_name, canonical_metric,
                   financial_statement, period_type, unit, mapped_value,
                   sign_policy, priority, mapped_source_detail, ?, ?
            FROM ranked_candidates
            WHERE destination_rank = 1
            ON CONFLICT(
                ticker, source_id, accession_number, taxonomy, concept_name,
                canonical_metric, unit, period_start, period_end, frame
            ) DO NOTHING
            """,
            (*sources, *symbols, asof.isoformat(), asof.isoformat(), now, now),
        )
    return max(0, connection.total_changes - changes_before)
